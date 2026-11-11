"""Reconstruct past daily pool sizes from Reader timestamps + sent_articles.

    python -m digest.backfill_pool [--days 120] [--dry-run]

An article is in the pool from the moment it got the trigger tag (tag `created`
if Reader exposes it, else the document's saved_at) until its sent_at. Articles
untagged or deleted without being sent are invisible, so past pools are a lower
bound on that side; saved_at entry times push the other way. Only days without a
live snapshot are written, marked source='backfill'; reruns replace them.
"""
import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from digest import config, store
from digest.pool import approx_words, monday
from digest.reader import Reader, word_count

log = logging.getLogger("digest.backfill_pool")


def _parse_ts(raw: object) -> datetime | None:
    if isinstance(raw, (int, float)) and raw > 0:
        return datetime.fromtimestamp(raw / 1000 if raw > 1e11 else raw, timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def entry_time(doc: dict, tag: str) -> tuple[datetime | None, str]:
    tags = doc.get("tags")
    if isinstance(tags, dict) and isinstance(tags.get(tag), dict):
        ts = _parse_ts(tags[tag].get("created"))
        if ts:
            return ts, "tag created"
    for field in ("saved_at", "created_at"):
        ts = _parse_ts(doc.get(field))
        if ts:
            return ts, field
    return None, "none"


def daily_pool(
    spans: list[tuple[date, date | None, int]], first: date, last: date
) -> dict[date, tuple[int, int]]:
    out = {}
    d = first
    while d <= last:
        live = [w for start, end, w in spans if start <= d and (end is None or end > d)]
        out[d] = (len(live), sum(live))
        d += timedelta(days=1)
    return out


def backfill(cfg: config.Config, days: int, *, dry_run: bool) -> int:
    zone = ZoneInfo(cfg.tz)
    today = datetime.now(zone).date()
    first = today - timedelta(days=days)

    reader = Reader(cfg.reader_token)
    try:
        docs = reader.list_queue(cfg.reader_tag_trigger)
    finally:
        reader.close()

    conn = store.connect(cfg.data_dir)
    try:
        sent = {
            r[0]: (datetime.fromisoformat(r[1]), r[2])
            for r in conn.execute("SELECT reader_document_id, sent_at, word_count FROM sent_articles")
        }
        sources: dict[str, int] = {}
        spans: list[tuple[date, date | None, int]] = []
        for doc in docs:
            entered, source = entry_time(doc, cfg.reader_tag_trigger)
            sources[source] = sources.get(source, 0) + 1
            if entered is None:
                continue
            sent_at, sent_words = sent.get(doc["id"], (None, None))
            words = sent_words if sent_words else word_count(doc)
            spans.append((
                entered.astimezone(zone).date(),
                sent_at.astimezone(zone).date() if sent_at else None,
                words,
            ))
        listed = {d["id"] for d in docs}
        sent_unlisted = sum(1 for i in sent if i not in listed)

        log.info(f"{len(docs)} documents tagged {cfg.reader_tag_trigger!r}")
        log.info(f"entry time from: {', '.join(f'{k} {v}' for k, v in sorted(sources.items()))}")
        if sample := next((d for d in docs if isinstance(d.get("tags"), dict)), None):
            log.info(f"sample tag payload: {sample['tags'].get(cfg.reader_tag_trigger)!r}")
        log.info(f"{sent_unlisted} sent article(s) no longer carry the tag (ignored, entry time unknown)")

        series = daily_pool(spans, first, today)
        now_n, now_w = series.pop(today)
        live_today = conn.execute(
            "SELECT articles, words FROM pool_snapshots WHERE day = ? AND source = 'live'", (today.isoformat(),)
        ).fetchone()
        if live_today:
            log.info(
                f"check against today's live snapshot: reconstructed {now_n} art. / {now_w:,} words, "
                f"live {live_today[0]} art. / {live_today[1]:,} words"
            )
        else:
            log.info(f"reconstructed today: {now_n} art. / {now_w:,} words (no live snapshot to compare)")

        live_days = {
            r[0] for r in conn.execute("SELECT day FROM pool_snapshots WHERE source = 'live' AND day >= ?",
                                       (first.isoformat(),))
        }
        to_write = {d: v for d, v in series.items() if d.isoformat() not in live_days}

        print("\nweek of     pool at week end")
        weeks: dict[date, tuple[int, int]] = {}
        for d, v in sorted(series.items()):
            weeks[monday(d)] = v
        for wk, (n, w) in weeks.items():
            print(f"{wk:%a %d %b}  {n:>4} art.  {w:>9,} words  ({approx_words(w)})")
        print(f"\n{len(to_write)} day(s) to write, {len(live_days)} live day(s) left untouched")

        if dry_run:
            log.info("dry run: nothing written")
            return 0
        conn.execute("DELETE FROM pool_snapshots WHERE source = 'backfill'")
        for d, (n, w) in to_write.items():
            conn.execute(
                "INSERT OR IGNORE INTO pool_snapshots (day, taken_at, articles, words, source) "
                "VALUES (?, ?, ?, ?, 'backfill')",
                (d.isoformat(), datetime.now(timezone.utc).isoformat(), n, w),
            )
        log.info(f"wrote {len(to_write)} backfilled day(s)")
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest.backfill_pool")
    parser.add_argument("--days", type=int, default=120, help="how far back to reconstruct (default 120)")
    parser.add_argument("--dry-run", action="store_true", help="print the reconstruction, write nothing")
    args = parser.parse_args()
    cfg = config.load(require_smtp=False)
    logging.basicConfig(level=cfg.log_level, format="%(levelname)s %(name)s: %(message)s")
    return backfill(cfg, args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
