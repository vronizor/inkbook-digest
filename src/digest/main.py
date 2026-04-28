import argparse
import io
import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from digest import config, epub, mailer, store
from digest.reader import Reader, tag_names

log = logging.getLogger("digest")


def _setup_logging(level: str) -> io.StringIO:
    buf = io.StringIO()
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    bh = logging.StreamHandler(buf)
    bh.setFormatter(fmt)
    root.addHandler(bh)
    return buf


def run_once(cfg: config.Config, *, dry_run: bool = False) -> int:
    log_buf = _setup_logging(cfg.log_level)
    today = datetime.now(ZoneInfo(cfg.tz)).date()
    mode = " (dry-run)" if dry_run else ""
    log.info(f"=== digest run start: {today.isoformat()}{mode} ===")

    conn = store.connect(cfg.data_dir)
    store.prune_old_runs(conn)
    run_id = store.record_run_start(conn)

    try:
        reader = Reader(cfg.reader_token)
        try:
            already = store.already_sent_ids(conn)
            log.info(f"previously sent ids: {len(already)}")

            candidates = list(reader.list_tagged_articles(cfg.reader_tag_trigger))
            log.info(f"reader returned {len(candidates)} tagged articles")

            new = [a for a in candidates if a["id"] not in already]
            ready = [a for a in new if (a.get("html_content") or a.get("content"))]
            unparsed = [a for a in new if a not in ready]
            for a in unparsed:
                log.info(f"skipping unparsed article (no html yet): {a.get('title')!r} ({a['id']})")

            log.info(f"new articles to send: {len(ready)} (skipped {len(unparsed)} unparsed)")

            if not ready:
                store.record_digest(conn, 0, "empty")
                store.record_run_end(conn, run_id, "empty", log_buf.getvalue())
                log.info("empty run — no email")
                return 0

            out_path = cfg.data_dir / f"morning-paper-{today.isoformat()}.epub"
            epub.build_epub(today, ready, out_path, cfg.image_soft_cap_mb)
            log.info(f"epub built: {out_path} ({out_path.stat().st_size} bytes)")

            if dry_run:
                store.record_run_end(conn, run_id, "ok", log_buf.getvalue())
                log.info(f"=== dry-run done: {len(ready)} articles, epub at {out_path} ===")
                log.info("dry-run: skipped SMTP send, Reader tag-add, and sent_articles recording")
                return 0

            mailer.send_digest(
                host=cfg.smtp_host, port=cfg.smtp_port,
                user=cfg.smtp_user, password=cfg.smtp_password,
                sender=cfg.smtp_from, recipient=cfg.inkbook_email,
                subject=f"Morning Paper {today.isoformat()}",
                epub_path=out_path,
            )

            digest_id = store.record_digest(conn, len(ready), "sent")
            tag_errors: list[str] = []
            for a in ready:
                store.record_sent_article(
                    conn, digest_id, a["id"],
                    a.get("title"), a.get("source_url") or a.get("url"),
                )
                try:
                    reader.add_tag(a["id"], tag_names(a), cfg.reader_tag_done)
                except Exception as e:
                    tag_errors.append(f"{a['id']}: {e}")
                    log.warning(f"failed to add done-tag to {a['id']}: {e}")

            outcome = "ok" if not tag_errors else "error"
            store.record_run_end(conn, run_id, outcome, log_buf.getvalue())

            if tag_errors:
                _try_send_alert(cfg, today, log_buf.getvalue(),
                                f"[inkbook-digest] partial failure on {today.isoformat()}")
            log.info(f"=== digest run done: {len(ready)} sent ===")
            return 0
        finally:
            reader.close()

    except Exception as e:
        log.exception(f"digest run failed: {e}")
        store.record_run_end(conn, run_id, "error", log_buf.getvalue())
        if not dry_run:
            _try_send_alert(cfg, today, log_buf.getvalue(),
                            f"[inkbook-digest] failure on {today.isoformat()}")
        return 1


def _try_send_alert(cfg: config.Config, today: date, body: str, subject: str) -> None:
    try:
        mailer.send_alert(
            host=cfg.smtp_host, port=cfg.smtp_port,
            user=cfg.smtp_user, password=cfg.smtp_password,
            sender=cfg.smtp_from, recipient=cfg.alert_email,
            subject=subject, body=body,
        )
    except Exception as e:
        log.error(f"alert email failed too: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="digest")
    parser.add_argument("--once", action="store_true", help="run a single digest and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build EPUB but skip SMTP send, Reader tag-add, and sent_articles recording",
    )
    args = parser.parse_args()

    cfg = config.load(require_smtp=not args.dry_run)

    if args.once or args.dry_run:
        return run_once(cfg, dry_run=args.dry_run)

    _setup_logging(cfg.log_level)
    sched = BlockingScheduler(timezone=ZoneInfo(cfg.tz))
    trigger = CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute)
    sched.add_job(lambda: run_once(cfg), trigger, id="daily-digest")
    next_run = trigger.get_next_fire_time(None, datetime.now(ZoneInfo(cfg.tz)))
    log.info(f"scheduler started, next run: {next_run.isoformat()} ({cfg.tz})")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
