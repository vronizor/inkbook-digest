import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY,
  sent_at TEXT NOT NULL,
  article_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  volume INTEGER NOT NULL DEFAULT 1,
  total_words INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sent_articles (
  reader_document_id TEXT PRIMARY KEY,
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  title TEXT,
  url TEXT,
  sent_at TEXT NOT NULL,
  word_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  log TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_books (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL UNIQUE,
  title TEXT,
  author TEXT,
  language TEXT,
  format TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  has_cover INTEGER NOT NULL DEFAULT 0,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscribers (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT,
  format TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  to_pool INTEGER NOT NULL DEFAULT 0,
  sender_name TEXT,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY,
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  email TEXT NOT NULL,
  format TEXT NOT NULL,
  status TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  manual INTEGER NOT NULL DEFAULT 0,
  sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_emails (
  reader_document_id TEXT PRIMARY KEY,
  sender_tag TEXT NOT NULL,
  kind TEXT NOT NULL,
  link_count INTEGER NOT NULL DEFAULT 0,
  processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_links (
  id INTEGER PRIMARY KEY,
  email_document_id TEXT NOT NULL,
  url TEXT NOT NULL,
  sender_tag TEXT NOT NULL,
  reader_document_id TEXT,
  status TEXT NOT NULL,
  error TEXT,
  saved_at TEXT NOT NULL,
  to_pool INTEGER,
  UNIQUE (email_document_id, url)
);

CREATE TABLE IF NOT EXISTS pool_snapshots (
  day TEXT PRIMARY KEY,
  taken_at TEXT NOT NULL,
  articles INTEGER NOT NULL,
  words INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'live'
);
"""

ADDITIVE_ALTERS = (
    "ALTER TABLE digests ADD COLUMN volume INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE digests ADD COLUMN total_words INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sent_articles ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN to_pool INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE subscribers ADD COLUMN sender_name TEXT",
    "ALTER TABLE pool_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'live'",
    "ALTER TABLE intake_links ADD COLUMN to_pool INTEGER",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _apply_additive_alters(conn: sqlite3.Connection) -> None:
    for stmt in ADDITIVE_ALTERS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            log.warning(f"ALTER failed (continuing): {stmt!r}: {e}")


def _seed_settings(conn: sqlite3.Connection) -> None:
    initial_word_budget = os.environ.get("WORD_BUDGET", "5000")
    rows = (
        ("paused", "false"),
        ("word_budget", initial_word_budget),
        ("digest_interval_days", "1"),
    )
    for k, v in rows:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
            (k, v, _now()),
        )


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        data_dir / "digest.sqlite",
        isolation_level=None,
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    _apply_additive_alters(conn)
    _seed_settings(conn)
    return conn


def already_sent_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT reader_document_id FROM sent_articles")}


def record_run_start(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    return cur.lastrowid


def record_run_end(conn: sqlite3.Connection, run_id: int, outcome: str, log_text: str) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, outcome = ?, log = ? WHERE id = ?",
        (_now(), outcome, log_text, run_id),
    )


def record_missed_run(conn: sqlite3.Connection, scheduled_at: datetime, log_text: str) -> None:
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, outcome, log) VALUES (?, ?, 'missed', ?)",
        (scheduled_at.astimezone(timezone.utc).isoformat(), _now(), log_text),
    )

def record_digest(
    conn: sqlite3.Connection,
    article_count: int,
    status: str,
    volume: int = 1,
    total_words: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO digests (sent_at, article_count, status, volume, total_words) "
        "VALUES (?, ?, ?, ?, ?)",
        (_now(), article_count, status, volume, total_words),
    )
    return cur.lastrowid


def record_sent_article(
    conn: sqlite3.Connection,
    digest_id: int,
    reader_document_id: str,
    title: str | None,
    url: str | None,
    word_count: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sent_articles "
        "(reader_document_id, digest_id, title, url, sent_at, word_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (reader_document_id, digest_id, title, url, _now(), word_count),
    )


def prune_old_runs(conn: sqlite3.Connection, days: int = 30) -> None:
    conn.execute(
        "DELETE FROM runs WHERE started_at < datetime('now', ?)",
        (f"-{days} days",),
    )


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, _now()),
    )


def get_word_budget(conn: sqlite3.Connection) -> int:
    raw = get_setting(conn, "word_budget")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            log.warning(f"settings.word_budget is unparseable: {raw!r}; falling back to env")
    env_raw = os.environ.get("WORD_BUDGET", "5000")
    try:
        return int(env_raw)
    except ValueError:
        log.warning(f"WORD_BUDGET env var is unparseable: {env_raw!r}; defaulting to 5000")
        return 5000


def get_digest_interval_days(conn: sqlite3.Connection) -> int:
    raw = get_setting(conn, "digest_interval_days")
    if raw is not None:
        try:
            n = int(raw)
            if 1 <= n < 100:
                return n
        except ValueError:
            pass
    return 1


def get_last_sent_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT DATE(sent_at) FROM digests WHERE status = 'sent' "
        "ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def build_epub_path(data_dir: Path, sent_at: str, volume: int) -> Path:
    day = sent_at[:10]
    suffix = "" if volume == 1 else f"-vol-{volume}"
    return data_dir / "epubs" / f"morning-paper-{day}{suffix}.epub"


def prune_old_epubs(data_dir: Path, retention_days: int) -> int:
    epub_dir = data_dir / "epubs"
    if retention_days <= 0 or not epub_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    stale = [f for f in epub_dir.glob("*.epub") if f.stat().st_mtime < cutoff]
    for f in stale:
        f.unlink()
    return len(stale)


def migrate_root_epubs(data_dir: Path) -> int:
    """Move any morning-paper-*.epub from data_dir root into data_dir/epubs/."""
    epub_dir = data_dir / "epubs"
    epub_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in data_dir.glob("morning-paper-*.epub"):
        target = epub_dir / f.name
        if target.exists():
            log.warning(f"migrate_root_epubs: target exists, leaving in place: {f}")
            continue
        f.rename(target)
        moved += 1
    return moved


def get_today_volume_number(conn: sqlite3.Connection) -> int:
    """Volume = count of today's status='sent' digests + 1."""
    row = conn.execute(
        "SELECT COUNT(*) FROM digests "
        "WHERE DATE(sent_at) = DATE('now') AND status = 'sent'"
    ).fetchone()
    return (row[0] or 0) + 1


_SUBSCRIBER_COLS = "id, email, name, format, active, to_pool, sender_name, added_at"


def _subscriber(r: tuple) -> dict:
    return {
        "id": r[0], "email": r[1], "name": r[2], "format": r[3],
        "active": bool(r[4]), "to_pool": bool(r[5]), "sender_name": r[6], "added_at": r[7],
    }


def list_subscribers(conn: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    sql = f"SELECT {_SUBSCRIBER_COLS} FROM subscribers"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY added_at"
    return [_subscriber(r) for r in conn.execute(sql)]


def get_subscriber(conn: sqlite3.Connection, subscriber_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_SUBSCRIBER_COLS} FROM subscribers WHERE id = ?", (subscriber_id,)
    ).fetchone()
    return _subscriber(row) if row else None


def add_subscriber(
    conn: sqlite3.Connection, email: str, name: str, fmt: str,
    to_pool: bool = False, sender_name: str = "",
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO subscribers (email, name, format, to_pool, sender_name, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email, name or None, fmt, int(to_pool), sender_name or None, _now()),
    )
    return cur.rowcount == 1


def set_subscriber_sender_name(conn: sqlite3.Connection, subscriber_id: int, sender_name: str) -> None:
    conn.execute(
        "UPDATE subscribers SET sender_name = ? WHERE id = ?", (sender_name or None, subscriber_id)
    )


def set_subscriber_pool(conn: sqlite3.Connection, subscriber_id: int, to_pool: bool) -> None:
    conn.execute("UPDATE subscribers SET to_pool = ? WHERE id = ?", (int(to_pool), subscriber_id))


def toggle_subscriber(conn: sqlite3.Connection, subscriber_id: int) -> None:
    conn.execute("UPDATE subscribers SET active = 1 - active WHERE id = ?", (subscriber_id,))


def set_subscriber_format(conn: sqlite3.Connection, subscriber_id: int, fmt: str) -> None:
    conn.execute("UPDATE subscribers SET format = ? WHERE id = ?", (fmt, subscriber_id))


def delete_subscriber(conn: sqlite3.Connection, subscriber_id: int) -> None:
    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))


def record_delivery(
    conn: sqlite3.Connection,
    digest_id: int,
    email: str,
    fmt: str,
    status: str,
    size_bytes: int = 0,
    error: str | None = None,
    manual: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO deliveries (digest_id, email, format, status, size_bytes, error, manual, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (digest_id, email, fmt, status, size_bytes, error, int(manual), _now()),
    )


def last_delivery_by_email(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT email, status, sent_at, error FROM deliveries d "
        "WHERE id = (SELECT MAX(id) FROM deliveries WHERE email = d.email)"
    )
    return {r[0]: {"status": r[1], "sent_at": r[2], "error": r[3]} for r in rows}


def recent_deliveries(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT d.id, d.digest_id, d.email, d.format, d.status, d.size_bytes, d.error, d.manual, "
        "d.sent_at, g.sent_at, g.volume "
        "FROM deliveries d LEFT JOIN digests g ON g.id = d.digest_id "
        "ORDER BY d.id DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "id": r[0], "digest_id": r[1], "email": r[2], "format": r[3], "status": r[4],
            "size_bytes": r[5], "error": r[6], "manual": bool(r[7]), "sent_at": r[8],
            "issue_date": (r[9] or "")[:10], "volume": r[10] or 1,
        }
        for r in rows
    ]


def intake_processed_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT reader_document_id FROM intake_emails")}


def intake_handled_urls(conn: sqlite3.Connection, email_document_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT url FROM intake_links WHERE email_document_id = ?", (email_document_id,)
    )
    return {r[0] for r in rows}


def record_intake_link(
    conn: sqlite3.Connection,
    email_document_id: str,
    url: str,
    sender_tag: str,
    status: str,
    *,
    reader_document_id: str | None = None,
    error: str | None = None,
    to_pool: bool | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO intake_links "
        "(email_document_id, url, sender_tag, reader_document_id, status, error, saved_at, to_pool) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (email_document_id, url, sender_tag, reader_document_id, status, error, _now(),
         None if to_pool is None else int(to_pool)),
    )


def record_intake_email(
    conn: sqlite3.Connection, reader_document_id: str, sender_tag: str, kind: str, link_count: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO intake_emails "
        "(reader_document_id, sender_tag, kind, link_count, processed_at) VALUES (?, ?, ?, ?, ?)",
        (reader_document_id, sender_tag, kind, link_count, _now()),
    )


def recent_intake_links(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT url, sender_tag, status, error, saved_at FROM intake_links "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        {"url": r[0], "sender_tag": r[1], "status": r[2], "error": r[3], "saved_at": r[4]}
        for r in rows
    ]


def record_pool_snapshot(conn: sqlite3.Connection, day: str, articles: int, words: int) -> None:
    conn.execute(
        "INSERT INTO pool_snapshots (day, taken_at, articles, words, source) VALUES (?, ?, ?, ?, 'live') "
        "ON CONFLICT(day) DO UPDATE SET taken_at = excluded.taken_at, "
        "articles = excluded.articles, words = excluded.words, source = 'live'",
        (day, _now(), articles, words),
    )


def pool_snapshots(conn: sqlite3.Connection, since_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT day, articles, words FROM pool_snapshots WHERE day >= ? ORDER BY day", (since_day,)
    )
    return [{"day": r[0], "articles": r[1], "words": r[2]} for r in rows]


def recent_intake_links_with_issue(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT l.url, l.sender_tag, l.status, l.error, l.saved_at, a.title, d.sent_at, d.volume, l.to_pool "
        "FROM intake_links l "
        "LEFT JOIN sent_articles a ON a.reader_document_id = l.reader_document_id "
        "LEFT JOIN digests d ON d.id = a.digest_id "
        "ORDER BY l.id DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "url": r[0], "sender_tag": r[1], "status": r[2], "error": r[3], "saved_at": r[4],
            "title": r[5], "issue_sent_at": r[6], "issue_volume": r[7],
            "to_pool": None if r[8] is None else bool(r[8]),
        }
        for r in rows
    ]
