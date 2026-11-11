import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from digest import backfill_pool, store

TZ = "Europe/Paris"


class EntryTimeTest(unittest.TestCase):
    def test_prefers_tag_created_ms(self):
        doc = {"tags": {"toepub": {"name": "toepub", "created": 1762300800000}}, "saved_at": "2025-01-01T00:00:00Z"}
        ts, source = backfill_pool.entry_time(doc, "toepub")
        self.assertEqual(source, "tag created")
        self.assertEqual(ts, datetime(2025, 11, 5, tzinfo=timezone.utc))

    def test_falls_back_to_saved_at(self):
        doc = {"tags": {"toepub": {"name": "toepub"}}, "saved_at": "2026-10-01T08:00:00.123Z"}
        ts, source = backfill_pool.entry_time(doc, "toepub")
        self.assertEqual(source, "saved_at")
        self.assertEqual(ts, datetime(2026, 10, 1, 8, 0, 0, 123000, tzinfo=timezone.utc))

    def test_nothing_usable(self):
        self.assertEqual(backfill_pool.entry_time({"tags": {}}, "toepub"), (None, "none"))


class DailyPoolTest(unittest.TestCase):
    def test_article_leaves_on_send_day(self):
        spans = [(date(2026, 11, 1), date(2026, 11, 3), 1000), (date(2026, 11, 2), None, 500)]
        series = backfill_pool.daily_pool(spans, date(2026, 10, 31), date(2026, 11, 4))
        self.assertEqual(series[date(2026, 10, 31)], (0, 0))
        self.assertEqual(series[date(2026, 11, 1)], (1, 1000))
        self.assertEqual(series[date(2026, 11, 2)], (2, 1500))
        self.assertEqual(series[date(2026, 11, 3)], (1, 500))


class BackfillRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.cfg = SimpleNamespace(data_dir=self.data, tz=TZ, reader_token="x", reader_tag_trigger="toepub")
        self.today = datetime.now(ZoneInfo(TZ)).date()

    def tearDown(self):
        self.tmp.cleanup()

    def _iso(self, days_ago: int) -> str:
        return datetime.combine(self.today - timedelta(days=days_ago), datetime.min.time(), timezone.utc).replace(
            hour=12).isoformat()

    def test_writes_only_missing_days_and_keeps_live(self):
        conn = store.connect(self.data)
        cur = conn.execute("INSERT INTO digests (sent_at, article_count, status) VALUES (?, 1, 'sent')",
                           (self._iso(3),))
        conn.execute("INSERT INTO sent_articles VALUES ('a', ?, 't', 'u', ?, 2000)", (cur.lastrowid, self._iso(3)))
        store.record_pool_snapshot(conn, (self.today - timedelta(days=2)).isoformat(), 9, 9999)
        conn.close()
        docs = [
            {"id": "a", "tags": {"toepub": {}}, "saved_at": self._iso(10), "word_count": 2000},
            {"id": "b", "tags": {"toepub": {}}, "saved_at": self._iso(5), "word_count": 700},
        ]
        fake = mock.MagicMock()
        fake.list_queue.return_value = docs
        with mock.patch.object(backfill_pool, "Reader", return_value=fake):
            backfill_pool.backfill(self.cfg, 12, dry_run=False)
        conn = store.connect(self.data)
        rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT day, words, source FROM pool_snapshots")}
        conn.close()
        day = lambda n: (self.today - timedelta(days=n)).isoformat()
        self.assertEqual(rows[day(11)], (0, "backfill"))
        self.assertEqual(rows[day(6)], (2000, "backfill"))
        self.assertEqual(rows[day(4)], (2700, "backfill"))
        self.assertEqual(rows[day(3)], (700, "backfill"))
        self.assertEqual(rows[day(2)], (9999, "live"))
        self.assertNotIn(self.today.isoformat(), rows)

    def test_dry_run_writes_nothing(self):
        fake = mock.MagicMock()
        fake.list_queue.return_value = [{"id": "b", "tags": {"toepub": {}}, "saved_at": self._iso(5), "word_count": 700}]
        with mock.patch.object(backfill_pool, "Reader", return_value=fake):
            backfill_pool.backfill(self.cfg, 12, dry_run=True)
        conn = store.connect(self.data)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pool_snapshots").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
