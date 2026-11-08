"""Turns link emails sent to the Reader library address into real Reader articles.

Reader stores an emailed link as a one-word `email` document. For each email from a
configured sender, this saves every link as an article tagged with the sender's tag and
the Morning Paper trigger tag, then archives the original. Longer emails (forwarded
newsletters) are only tagged and left in place. See SPEC_V6.md.
"""

import argparse
import html
import json
import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from digest import config, store
from digest.reader import Reader, tag_names

log = logging.getLogger(__name__)

JOB_ID = "intake"
SAVED_USING = "kiosk-intake"
# A fixed window rather than a stored cursor: sender config changes and outages up to
# this long are picked up on the next run, and intake_emails makes re-listing harmless.
LOOKBACK = timedelta(days=7)
MAX_PROSE_WORDS = 40
MAX_LINKS = 10

_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
_TAG_RE = re.compile(r"<[^>]+>")
_CONTENT_FIELDS = {"html_content", "content", "summary", "notes"}

is_running = threading.Lock()


class TransientError(Exception):
    pass


def match_sender(doc: dict, senders: tuple[tuple[str, str], ...]) -> str | None:
    """The API exposes no sender address on email documents, only the display name in `author`."""
    author = (doc.get("author") or "").strip().lower()
    return next((tag for name, tag in senders if name == author), None)


def extract_links(body_html: str, ignore_domains: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for raw in _URL_RE.findall(html.unescape(body_html)):
        url = raw.rstrip(".,;:!?")
        host = (urlsplit(url).hostname or "").lower()
        if not host or any(host == d or host.endswith(f".{d}") for d in ignore_domains):
            continue
        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def prose_words(body_html: str) -> int:
    text = _URL_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(body_html)))
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


def _is_transient(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code == 429 or code >= 500
    return False


def process_email(cfg: config.Config, conn, reader: Reader, doc: dict, sender_tag: str) -> str:
    full = reader.get_document(doc["id"]) or doc
    body = full.get("html_content") or full.get("content") or ""
    links = extract_links(body, cfg.intake_ignore_domains)
    words = prose_words(body)

    if not links or words > MAX_PROSE_WORDS or len(links) > MAX_LINKS:
        kind = "no-links" if not links else "newsletter"
        reader.add_tags(doc["id"], tag_names(full), [sender_tag])
        store.record_intake_email(conn, doc["id"], sender_tag, kind, 0)
        log.info(f"intake: {doc['id']} is a {kind} email ({len(links)} links, {words} words), tagged only")
        return kind

    handled = store.intake_handled_urls(conn, doc["id"])
    article_tags = [sender_tag, cfg.reader_tag_trigger]
    saved = failed = 0
    for url in links:
        if url in handled:
            saved += 1
            continue
        try:
            new_id, created = reader.save_url(url, article_tags, saved_using=SAVED_USING)
            if not created:
                existing = reader.get_document(new_id)
                reader.add_tags(new_id, tag_names(existing or {}), article_tags)
            store.record_intake_link(
                conn, doc["id"], url, sender_tag,
                "saved" if created else "existing", reader_document_id=new_id,
            )
            saved += 1
            log.info(f"intake: saved {url} ({'new' if created else 'already in Reader'})")
        except httpx.HTTPStatusError as e:
            if _is_transient(e):
                raise TransientError(f"{url}: HTTP {e.response.status_code}") from e
            store.record_intake_link(
                conn, doc["id"], url, sender_tag, "error",
                error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
            )
            failed += 1
            log.warning(f"intake: Reader refused {url}: HTTP {e.response.status_code}")
        except httpx.TransportError as e:
            raise TransientError(f"{url}: {e}") from e

    kind = "links" if saved else "error"
    reader.add_tags(doc["id"], tag_names(full), [sender_tag], archive=bool(saved))
    store.record_intake_email(conn, doc["id"], sender_tag, kind, saved)
    log.info(f"intake: {doc['id']} done, {saved} saved, {failed} refused")
    return kind


def run(cfg: config.Config) -> int:
    if not cfg.intake_senders:
        log.info("intake: no INTAKE_SENDERS configured, nothing to do")
        return 0
    conn = store.connect(cfg.data_dir)
    reader = Reader(cfg.reader_token)
    try:
        since = (datetime.now(timezone.utc) - LOOKBACK).isoformat()
        done = store.intake_processed_ids(conn)
        processed = 0
        try:
            for doc in reader.list_documents(category="email", updated_after=since):
                if doc["id"] in done:
                    continue
                sender_tag = match_sender(doc, cfg.intake_senders)
                if sender_tag is None:
                    continue
                try:
                    process_email(cfg, conn, reader, doc, sender_tag)
                except httpx.HTTPStatusError as e:
                    if _is_transient(e):
                        raise TransientError(f"{doc['id']}: HTTP {e.response.status_code}") from e
                    log.warning(f"intake: skipping {doc['id']}: HTTP {e.response.status_code}")
                    store.record_intake_email(conn, doc["id"], sender_tag, "error", 0)
                processed += 1
        except (TransientError, httpx.TransportError, httpx.HTTPStatusError) as e:
            if isinstance(e, httpx.HTTPStatusError) and not _is_transient(e):
                raise
            log.warning(f"intake: stopping early, will retry next run: {e}")
            return 1
        if processed:
            log.info(f"intake: processed {processed} email(s)")
        return 0
    finally:
        reader.close()
        conn.close()


def run_with_lock(cfg: config.Config) -> None:
    if not is_running.acquire(blocking=False):
        log.info("intake: previous run still in progress, skipping")
        return
    try:
        run(cfg)
    except Exception as e:
        log.exception(f"intake run failed: {e}")
    finally:
        is_running.release()


def inspect(cfg: config.Config, days: int) -> None:
    reader = Reader(cfg.reader_token)
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for doc in reader.list_documents(category="email", updated_after=since):
            meta = {k: v for k, v in doc.items() if k not in _CONTENT_FIELDS}
            meta["_matched_tag"] = match_sender(doc, cfg.intake_senders)
            print(json.dumps(meta, ensure_ascii=False, indent=2))
    finally:
        reader.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest.intake")
    parser.add_argument("--once", action="store_true", help="process new link emails and exit")
    parser.add_argument(
        "--inspect", type=int, metavar="DAYS",
        help="print metadata of email documents updated in the last DAYS days, with the sender match",
    )
    args = parser.parse_args()
    cfg = config.load(require_smtp=False)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.inspect is not None:
        inspect(cfg, args.inspect)
        return 0
    if args.once:
        return run(cfg)
    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
