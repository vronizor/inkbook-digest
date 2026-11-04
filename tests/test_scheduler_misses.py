import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from digest import main, schedule, store

TZ = ZoneInfo("Europe/Paris")


def _saturday_7am() -> schedule.Schedule:
    return schedule.Schedule((5,), 7, 0, 1, date(2026, 10, 26))


class LatenessTest(unittest.TestCase):
    def test_late_same_morning(self):
        now = datetime(2026, 10, 31, 7, 3, 47, tzinfo=TZ)
        self.assertEqual(schedule.lateness(_saturday_7am(), now), timedelta(minutes=3, seconds=47))
        self.assertEqual(schedule.format_delay(timedelta(minutes=3, seconds=47)), "3m47s")

    def test_after_midnight_counts_from_previous_day(self):
        s = schedule.Schedule((4,), 23, 30, 1, date(2026, 10, 26))
        now = datetime(2026, 10, 31, 1, 0, tzinfo=TZ)
        self.assertEqual(schedule.lateness(s, now), timedelta(hours=1, minutes=30))
        self.assertEqual(schedule.format_delay(timedelta(hours=1, minutes=30)), "1h30m")

    def test_no_scheduled_fire_nearby(self):
        now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
        self.assertEqual(schedule.lateness(_saturday_7am(), now), timedelta(0))


class MissedRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = SimpleNamespace(data_dir=Path(self.tmp.name), tz="Europe/Paris")
        conn = store.connect(self.cfg.data_dir)
        schedule.save(conn, _saturday_7am())
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _runs(self) -> list[tuple[str, str]]:
        conn = store.connect(self.cfg.data_dir)
        try:
            return conn.execute("SELECT outcome, log FROM runs").fetchall()
        finally:
            conn.close()

    def test_missed_run_is_recorded(self):
        event = SimpleNamespace(scheduled_run_time=datetime(2026, 10, 31, 7, 0, tzinfo=TZ))
        main.record_missed(self.cfg, event)
        rows = self._runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "missed")
        self.assertIn("Sat 31 Oct 07:00", rows[0][1])

    def test_paused_miss_is_not_recorded(self):
        conn = store.connect(self.cfg.data_dir)
        store.set_setting(conn, "paused", "true")
        conn.close()
        main.record_missed(self.cfg, SimpleNamespace(scheduled_run_time=datetime(2026, 10, 31, 7, 0, tzinfo=TZ)))
        self.assertEqual(self._runs(), [])


    def test_late_run_logs_delay(self):
        conn = store.connect(self.cfg.data_dir)
        store.set_setting(conn, "paused", "true")
        conn.close()
        cfg = SimpleNamespace(data_dir=self.cfg.data_dir, tz="Europe/Paris", share_tag="toshare")
        main.run_once(cfg, late_by=timedelta(minutes=3, seconds=47))
        self.assertIn("scheduled run started 3m47s late", self._runs()[0][1])

if __name__ == "__main__":
    unittest.main()
