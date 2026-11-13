import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from digest import pool, store

TZ = "Europe/Paris"


class PhrasingTest(unittest.TestCase):
    def test_approx_words(self):
        self.assertEqual(pool.approx_words(61_400), "61k")
        self.assertEqual(pool.approx_words(6_420), "6.4k")
        self.assertEqual(pool.approx_words(15_000), "15k")
        self.assertEqual(pool.approx_words(9_990), "10k")
        self.assertEqual(pool.approx_words(842), "840")
        self.assertEqual(pool.approx_words(40), "40")

    def test_issues_worth_uses_average_issue(self):
        self.assertEqual(pool.issues_worth(61_400, 17_500), "roughly four issues' worth")
        self.assertEqual(pool.issues_worth(20_000, 17_500), "about one issue's worth")
        self.assertEqual(pool.issues_worth(5_000, 17_500), "less than one issue's worth")
        self.assertEqual(pool.issues_worth(300_000, 15_000), "roughly 20 issues' worth")

    def test_approx_articles(self):
        self.assertEqual(pool.approx_articles(1), "1 article")
        self.assertEqual(pool.approx_articles(7), "7 articles")
        self.assertEqual(pool.approx_articles(10), "10+ articles")
        self.assertEqual(pool.approx_articles(43), "40+ articles")
        self.assertEqual(pool.approx_articles(49), "40+ articles")

    def test_summary_articles(self):
        self.assertEqual(pool.summary(61_400, 43, 15_000, None)["articles"], "40+ articles")

    def test_summary_trend(self):
        self.assertEqual(pool.summary(61_400, 43, 15_000, None)["trend"], "")
        self.assertEqual(pool.summary(61_400, 43, 15_000, 50_200)["trend"], "A month ago it was around 50k.")
        self.assertEqual(pool.summary(61_400, 43, 15_000, 60_000)["trend"], "About the same as a month ago.")

    def test_next_issue_phrase(self):
        now = datetime(2026, 11, 11, 7, 0, tzinfo=ZoneInfo(TZ))
        self.assertEqual(pool.next_issue_phrase(datetime(2026, 11, 14, 7, 30, tzinfo=ZoneInfo(TZ)), now),
                         "Next issue Saturday, 07:30.")
        self.assertEqual(pool.next_issue_phrase(datetime(2026, 11, 12, 7, 30, tzinfo=ZoneInfo(TZ)), now),
                         "Next issue tomorrow, 07:30.")
        self.assertEqual(pool.next_issue_phrase(datetime(2026, 11, 21, 7, 30, tzinfo=ZoneInfo(TZ)), now),
                         "Next issue Saturday 21 November, 07:30.")
        self.assertEqual(pool.next_issue_phrase(None, now), "No issue scheduled.")


class WeeklyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _digest(self, sent_at: str, words: list[int]) -> None:
        cur = self.conn.execute(
            "INSERT INTO digests (sent_at, article_count, status, total_words) VALUES (?, ?, 'sent', ?)",
            (sent_at, len(words), sum(words)),
        )
        for i, w in enumerate(words):
            self.conn.execute(
                "INSERT INTO sent_articles (reader_document_id, digest_id, title, url, sent_at, word_count) "
                "VALUES (?, ?, 't', 'u', ?, ?)",
                (f"{sent_at}-{i}", cur.lastrowid, sent_at, w),
            )

    def test_buckets_by_local_monday_and_pool(self):
        self._digest("2026-11-07T06:30:00+00:00", [3000, 4000])
        self._digest("2026-10-31T06:30:00+00:00", [5000])
        self._digest("2026-11-01T23:30:00+00:00", [1000])
        store.record_pool_snapshot(self.conn, "2026-10-28", 10, 50_000)
        store.record_pool_snapshot(self.conn, "2026-11-01", 11, 52_000)
        store.record_pool_snapshot(self.conn, "2026-11-08", 12, 55_000)
        rows = pool.weekly(self.conn, TZ, date(2026, 11, 11), weeks=3)
        self.assertEqual([r["monday"] for r in rows], [date(2026, 10, 26), date(2026, 11, 2), date(2026, 11, 9)])
        self.assertEqual([(r["words"], r["n"]) for r in rows], [(5000, 1), (8000, 3), (0, 0)])
        self.assertEqual([r["pool"] for r in rows], [52_000, 55_000, None])
        self.assertEqual([r["pool_n"] for r in rows], [11, 12, None])

    def test_chart_view_cumulative_and_added(self):
        rows = [
            {"monday": date(2026, 10, 26), "words": 5000, "n": 1, "pool": 52_000, "pool_n": 10},
            {"monday": date(2026, 11, 2), "words": 8000, "n": 3, "pool": 55_000, "pool_n": 12},
            {"monday": date(2026, 11, 9), "words": 0, "n": 0, "pool": None, "pool_n": None},
        ]
        view = pool.chart_view(rows, 15_000)
        self.assertTrue(view["has_pool_history"])
        self.assertEqual(view["weeks"][1]["added"], 55_000 - 52_000 + 8000)
        self.assertIsNone(view["weeks"][0]["added"])
        self.assertEqual(view["weeks"][1]["added_n"], 12 - 10 + 3)
        self.assertEqual([w["growth"] for w in view["weeks"]], [0, 3000, None])
        self.assertEqual([w["label"] for w in view["weeks"]], ["26 Oct", "2 Nov", "9"])
        self.assertEqual(len(view["read_pts"].split()), 3)
        self.assertEqual(len(view["saved_pts"].split()), 2)

    def test_cumulative_gap_is_pool_growth(self):
        rows = [
            {"monday": date(2026, 10, 26), "words": 5000, "n": 1, "pool": 160_000, "pool_n": 10},
            {"monday": date(2026, 11, 2), "words": 3000, "n": 1, "pool": 170_000, "pool_n": 10},
        ]
        view = pool.chart_view(rows, 4000)
        saved_first = view["saved_pts"].split()[0]
        read_first = view["read_pts"].split()[0]
        self.assertEqual(saved_first, read_first)
        self.assertEqual(view["history_from"], "26 Oct")

    def test_single_snapshot_does_not_set_scale(self):
        rows = [
            {"monday": date(2026, 10, 26), "words": 5000, "n": 1, "pool": None, "pool_n": None},
            {"monday": date(2026, 11, 2), "words": 3000, "n": 1, "pool": 160_000, "pool_n": 10},
        ]
        view = pool.chart_view(rows, 4000)
        self.assertFalse(view["has_pool_history"])
        top_y = min(float(pt.split(",")[1]) for pt in view["read_pts"].split())
        self.assertLess(top_y, pool.VIEW_H * 0.2)

    def test_average_issue_words_falls_back_to_budget(self):
        self.assertEqual(pool.average_issue_words(self.conn, 15_000), 15_000)
        self._digest("2026-11-07T06:30:00+00:00", [10_000, 8_000])
        self._digest("2026-10-31T06:30:00+00:00", [16_000])
        self.assertEqual(pool.average_issue_words(self.conn, 15_000), 17_000)


if __name__ == "__main__":
    unittest.main()
