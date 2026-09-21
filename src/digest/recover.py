"""Rebuild EPUBs for sent digests whose files are missing, from Reader + sent_articles.

    python -m digest.recover --since 2026-06-01 [--dry-run] [--force]
"""
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

from digest import config, epub, store
from digest.reader import Reader

log = logging.getLogger("digest.recover")

PER_DOC_DELAY_S = 3.2


def _missing_digests(conn, data_dir, since: date, force: bool) -> list[tuple[int, str, int]]:
    rows = conn.execute(
        "SELECT id, sent_at, volume FROM digests "
        "WHERE status = 'sent' AND DATE(sent_at) >= ? ORDER BY sent_at",
        (since.isoformat(),),
    ).fetchall()
    return [r for r in rows if force or not store.build_epub_path(data_dir, r[1], r[2]).exists()]


def _article_ids(conn, digest_id: int) -> list[str]:
    # rowid preserves the original (shuffled) selection order
    rows = conn.execute(
        "SELECT reader_document_id FROM sent_articles WHERE digest_id = ? ORDER BY rowid",
        (digest_id,),
    ).fetchall()
    return [r[0] for r in rows]


def recover(cfg: config.Config, since: date, *, dry_run: bool, force: bool) -> int:
    conn = store.connect(cfg.data_dir)
    try:
        targets = _missing_digests(conn, cfg.data_dir, since, force)
        log.info(f"{len(targets)} digest(s) to rebuild since {since.isoformat()}")
        if not targets:
            return 0
        wanted = {d: _article_ids(conn, d) for d, _, _ in targets}
    finally:
        conn.close()

    all_ids = {i for ids in wanted.values() for i in ids}
    reader = Reader(cfg.reader_token)
    try:
        docs = {
            a["id"]: a for a in reader.list_tagged_articles(cfg.reader_tag_done)
            if a["id"] in all_ids
        }
        log.info(f"bulk fetch via tag {cfg.reader_tag_done!r}: {len(docs)}/{len(all_ids)} found")
        for doc_id in sorted(all_ids - docs.keys()):
            time.sleep(PER_DOC_DELAY_S)
            doc = reader.get_document(doc_id)
            if doc:
                docs[doc_id] = doc
            else:
                log.warning(f"not found in Reader (deleted?): {doc_id}")
    finally:
        reader.close()

    failures = 0
    for digest_id, sent_at, volume in targets:
        articles = [docs[i] for i in wanted[digest_id] if i in docs]
        ready = [a for a in articles if a.get("html_content") or a.get("content")]
        out_path = store.build_epub_path(cfg.data_dir, sent_at, volume)
        n_orig = len(wanted[digest_id])
        if not ready:
            log.error(f"digest {digest_id} ({sent_at[:10]} vol {volume}): 0/{n_orig} recoverable, skipped")
            failures += 1
            continue
        if len(ready) < n_orig:
            log.warning(f"digest {digest_id}: only {len(ready)}/{n_orig} articles recoverable")
        if dry_run:
            log.info(f"dry-run: would write {out_path.name} ({len(ready)} articles)")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        epub.build_epub(
            date.fromisoformat(sent_at[:10]), ready, out_path, cfg.image_soft_cap_mb, volume=volume
        )
        sent_ts = datetime.fromisoformat(sent_at)
        if sent_ts.tzinfo is None:
            sent_ts = sent_ts.replace(tzinfo=timezone.utc)
        os.utime(out_path, (sent_ts.timestamp(), sent_ts.timestamp()))
        log.info(f"rebuilt {out_path.name} ({len(ready)}/{n_orig} articles)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest.recover")
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="rebuild even if the EPUB exists")
    args = parser.parse_args()
    cfg = config.load(require_smtp=False)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s")
    return recover(cfg, args.since, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
