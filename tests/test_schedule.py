import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from digest import schedule, store

TZ = ZoneInfo("Europe/Paris")


def _sched(days: tuple[int, ...], weeks: int = 1, anchor: date = date(2026, 10, 26), hm: tuple[int, int] = (7, 30)):
    return schedule.Schedule(days, hm[0], hm[1], weeks, anchor)


class ScheduleLogicTest(unittest.TestCase):
    def test_weekly_saturday(self):
        s = _sched((5,), anchor=date(2026, 10, 19))
        now = datetime(2026, 10, 25, 21, 59, tzinfo=TZ)
        runs = schedule.next_runs(s, now, 2)
        self.assertEqual([r.date() for r in runs], [date(2026, 10, 31), date(2026, 11, 7)])
        self.assertEqual(runs[0].hour, 7)
        self.assertEqual(runs[0].minute, 30)

    def test_every_other_week_respects_anchor(self):
        s = _sched((5,), weeks=2, anchor=date(2026, 11, 2))
        now = datetime(2026, 10, 25, 21, 59, tzinfo=TZ)
        self.assertEqual([r.date() for r in schedule.next_runs(s, now, 2)], [date(2026, 11, 7), date(2026, 11, 21)])
        self.assertFalse(schedule.is_on_week(s, date(2026, 10, 31)))
        self.assertTrue(schedule.is_on_week(s, date(2026, 11, 7)))
        self.assertFalse(schedule.is_on_week(s, date(2026, 11, 14)))

    def test_same_day_later_time_counts(self):
        s = _sched((6,), anchor=date(2026, 10, 19), hm=(23, 0))
        now = datetime(2026, 10, 25, 21, 59, tzinfo=TZ)
        self.assertEqual(schedule.next_runs(s, now, 1)[0].date(), date(2026, 10, 25))

    def test_describe(self):
        self.assertEqual(schedule.describe(_sched((5,))), "Every Saturday at 07:30")
        self.assertEqual(schedule.describe(_sched((5,), weeks=2)), "Every other Saturday at 07:30")
        self.assertEqual(schedule.describe(_sched((5,), weeks=3)), "Saturday every 3 weeks at 07:30")
        self.assertEqual(schedule.describe(_sched(tuple(range(7)))), "Every day at 07:30")
        self.assertEqual(schedule.describe(_sched((0, 3), weeks=2)), "Mon, Thu at 07:30, every other week")

    def test_cron_trigger(self):
        trig = schedule.trigger(_sched((0, 5)), "Europe/Paris")
        fields = {f.name: str(f) for f in trig.fields}
        self.assertEqual(fields["day_of_week"], "mon,sat")
        self.assertEqual((fields["hour"], fields["minute"]), ("7", "30"))

    def test_parse_validation(self):
        with self.assertRaises(ValueError):
            schedule.parse_days("")
        with self.assertRaises(ValueError):
            schedule.parse_days("7")
        with self.assertRaises(ValueError):
            schedule.parse_time("25:00")
        self.assertEqual(schedule.parse_days("5,0,5"), (0, 5))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_weekly_interval_becomes_weekday_of_last_digest(self):
        store.set_setting(self.conn, "digest_interval_days", "7")
        self.conn.execute("INSERT INTO digests (sent_at, article_count, status) VALUES ('2026-10-25T19:57:51+00:00', 2, 'sent')")
        with mock.patch.dict(os.environ, {"DIGEST_HOUR": "6", "DIGEST_MINUTE": "30"}):
            s = schedule.load(self.conn)
        self.assertEqual((s.days, s.weeks, s.time_str), ((6,), 1, "06:30"))
        self.assertEqual(schedule.load(self.conn), s)

    def test_daily_interval_becomes_every_day(self):
        s = schedule.load(self.conn)
        self.assertEqual(s.days, tuple(range(7)))


if __name__ == "__main__":
    unittest.main()
