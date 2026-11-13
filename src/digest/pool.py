"""Pool (Reader queue) measurement, daily snapshots and the dashboard's reading chart."""

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from digest import store
from digest.reader import Reader, word_count

log = logging.getLogger(__name__)

JOB_ID = "pool-snapshot"
CHART_WEEKS = 12
AVG_ISSUES = 8
MONTH_LOOKBACK_DAYS = 30
MONTH_TOLERANCE_DAYS = 7
VIEW_W = 1000
VIEW_H = 220

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def measure(cfg, conn: sqlite3.Connection) -> dict:
    reader = Reader(cfg.reader_token)
    try:
        articles = reader.list_queue(cfg.reader_tag_trigger)
    finally:
        reader.close()
    already = store.already_sent_ids(conn)
    pending = [a for a in articles if a["id"] not in already]
    return {"count": len(pending), "total_words": sum(word_count(a) for a in pending)}


def record(conn: sqlite3.Connection, tz: str, stats: dict) -> None:
    today = datetime.now(ZoneInfo(tz)).date().isoformat()
    store.record_pool_snapshot(conn, today, stats["count"], stats["total_words"])


def snapshot_job(cfg) -> None:
    conn = store.connect(cfg.data_dir)
    try:
        stats = measure(cfg, conn)
        record(conn, cfg.tz, stats)
        log.info(f"pool snapshot: {stats['count']} articles, {stats['total_words']} words")
    except Exception as e:
        log.warning(f"pool snapshot failed (next one retries): {e}")
    finally:
        conn.close()


def approx_words(words: int) -> str:
    if words < 1000:
        return f"{round(words, -1):,}" if words >= 100 else str(words)
    k = words / 1000
    return f"{k:.1f}k".replace(".0k", "k") if k < 10 else f"{round(k):,}k"


def approx_articles(n: int) -> str:
    if n < 10:
        return f"{n} article" + ("" if n == 1 else "s")
    return f"{n // 10 * 10}+ articles"


def _number(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def issues_worth(pool_words: int, avg_issue_words: int) -> str:
    if avg_issue_words <= 0:
        return ""
    x = pool_words / avg_issue_words
    if x < 0.75:
        return "less than one issue's worth"
    if x < 1.5:
        return "about one issue's worth"
    return f"roughly {_number(round(x))} issues' worth"


def average_issue_words(conn: sqlite3.Connection, fallback: int) -> int:
    rows = conn.execute(
        "SELECT total_words FROM digests WHERE status = 'sent' AND total_words > 0 "
        "ORDER BY sent_at DESC LIMIT ?", (AVG_ISSUES,),
    ).fetchall()
    return round(sum(r[0] for r in rows) / len(rows)) if rows else fallback


def words_around(conn: sqlite3.Connection, target: date) -> int | None:
    lo = (target - timedelta(days=MONTH_TOLERANCE_DAYS)).isoformat()
    hi = (target + timedelta(days=MONTH_TOLERANCE_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT day, words FROM pool_snapshots WHERE day BETWEEN ? AND ?", (lo, hi)
    ).fetchall()
    if not rows:
        return None
    best = min(rows, key=lambda r: abs((date.fromisoformat(r[0]) - target).days))
    return best[1]


def summary(pool_words: int, pool_articles: int, avg_issue: int, month_ago: int | None) -> dict:
    out = {
        "words": approx_words(pool_words), "articles": approx_articles(pool_articles),
        "worth": issues_worth(pool_words, avg_issue), "trend": "",
    }
    if month_ago is not None:
        if month_ago and abs(pool_words - month_ago) / month_ago < 0.05:
            out["trend"] = "About the same as a month ago."
        else:
            out["trend"] = f"A month ago it was around {approx_words(month_ago)}."
    return out


def next_issue_phrase(next_run: datetime | None, now: datetime) -> str:
    if next_run is None:
        return "No issue scheduled."
    days = (next_run.date() - now.date()).days
    if days == 0:
        when = "today"
    elif days == 1:
        when = "tomorrow"
    elif days < 7:
        when = f"{next_run:%A}"
    else:
        when = f"{next_run:%A} {next_run.day} {next_run:%B}"
    return f"Next issue {when}, {next_run:%H:%M}."


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly(conn: sqlite3.Connection, tz: str, today: date, weeks: int = CHART_WEEKS) -> list[dict]:
    zone = ZoneInfo(tz)
    first = monday(today) - timedelta(weeks=weeks - 1)
    start_utc = datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc).isoformat()
    buckets = {first + timedelta(weeks=i): {"words": 0, "n": 0} for i in range(weeks)}
    for sent_at, words in conn.execute(
        "SELECT sent_at, word_count FROM sent_articles WHERE sent_at >= ?", (start_utc,)
    ):
        wk = monday(datetime.fromisoformat(sent_at).astimezone(zone).date())
        if wk in buckets:
            buckets[wk]["words"] += words or 0
            buckets[wk]["n"] += 1
    pool_end: dict[date, dict] = {}
    for snap in store.pool_snapshots(conn, first.isoformat()):
        pool_end[monday(date.fromisoformat(snap["day"]))] = snap
    return [
        {
            "monday": wk, "words": b["words"], "n": b["n"],
            "pool": pool_end[wk]["words"] if wk in pool_end else None,
            "pool_n": pool_end[wk]["articles"] if wk in pool_end else None,
        }
        for wk, b in sorted(buckets.items())
    ]


def _month(wk: date, prev: date | None) -> str:
    return f"{wk:%b}" if prev is None or prev.month != wk.month else ""


def chart_view(rows: list[dict], budget: int) -> dict:
    n = len(rows)
    top = max([r["words"] for r in rows] + [budget]) * 1.15
    xs = [round(i / (n - 1) * VIEW_W, 1) if n > 1 else 0 for i in range(n)]

    read_cum, total = [], 0
    for r in rows:
        total += r["words"]
        read_cum.append(total)
    known = [i for i, r in enumerate(rows) if r["pool"] is not None]
    base = rows[known[0]]["pool"] if known else 0
    saved_cum = [rc + r["pool"] - base if r["pool"] is not None else None for rc, r in zip(read_cum, rows)]
    has_history = len(known) > 1
    plotted = read_cum + ([saved_cum[i] for i in known] if has_history else [])
    lo = min(plotted + [0])
    hi = max(plotted + [1]) * 1.08
    span = hi - lo or 1

    def y(v: float) -> float:
        return round(VIEW_H - (v - lo) / span * VIEW_H, 1)

    saved_pts = " ".join(f"{xs[i]},{y(saved_cum[i])}" for i in known)
    read_pts = " ".join(f"{xs[i]},{y(v)}" for i, v in enumerate(read_cum))
    gap_pts = saved_pts + " " + " ".join(f"{xs[i]},{y(read_cum[i])}" for i in reversed(known))

    weeks, prev = [], None
    for i, r in enumerate(rows):
        before = rows[i - 1] if i else None
        has_pair = r["pool"] is not None and before is not None and before["pool"] is not None
        added = r["pool"] - before["pool"] + r["words"] if has_pair else None
        added_n = r["pool_n"] - before["pool_n"] + r["n"] if has_pair else None
        weeks.append({
            "day": r["monday"].day, "month": _month(r["monday"], prev),
            "full": f"{r['monday'].day} {r['monday']:%b}",
            "words": r["words"], "n": r["n"],
            "words_k": approx_words(r["words"]) if r["words"] else "",
            "h": round(r["words"] / top * 100, 1),
            "pool": r["pool"], "pool_n": r["pool_n"], "added": added, "added_n": added_n,
            "growth": r["pool"] - base if r["pool"] is not None and has_history else None,
            "x": xs[i],
        })
        prev = r["monday"]
    return {
        "weeks": weeks,
        "budget_pct": round(budget / top * 100, 1),
        "budget_k": approx_words(budget),
        "saved_pts": saved_pts if has_history else "",
        "gap_pts": gap_pts if has_history else "",
        "read_pts": read_pts,
        "has_pool_history": has_history,
        "history_from": f"{rows[known[0]]['monday'].day} {rows[known[0]]['monday']:%b}" if known else "",
        "view_w": VIEW_W, "view_h": VIEW_H,
    }
