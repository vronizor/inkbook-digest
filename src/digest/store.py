import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY,
  sent_at TEXT NOT NULL,
  article_count INTEGER NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_articles (
  reader_document_id TEXT PRIMARY KEY,
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  title TEXT,
  url TEXT,
  sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,
  log TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "digest.sqlite", isolation_level=None)
    conn.executescript(SCHEMA)
    return conn


def already_sent_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT reader_document_id FROM sent_articles")}


def record_run_start(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
    return cur.lastrowid


def record_run_end(conn: sqlite3.Connection, run_id: int, outcome: str, log: str) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, outcome = ?, log = ? WHERE id = ?",
        (_now(), outcome, log, run_id),
    )


def record_digest(conn: sqlite3.Connection, article_count: int, status: str) -> int:
    cur = conn.execute(
        "INSERT INTO digests (sent_at, article_count, status) VALUES (?, ?, ?)",
        (_now(), article_count, status),
    )
    return cur.lastrowid


def record_sent_article(
    conn: sqlite3.Connection,
    digest_id: int,
    reader_document_id: str,
    title: str | None,
    url: str | None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sent_articles "
        "(reader_document_id, digest_id, title, url, sent_at) VALUES (?, ?, ?, ?, ?)",
        (reader_document_id, digest_id, title, url, _now()),
    )


def prune_old_runs(conn: sqlite3.Connection, days: int = 30) -> None:
    conn.execute(
        "DELETE FROM runs WHERE started_at < datetime('now', ?)",
        (f"-{days} days",),
    )
