import unittest

from digest.dashboard import _link_issue, _link_state


def _link(**kw) -> dict:
    base = {"status": "saved", "sender_tag": "jeff", "to_pool": None, "issue_sent_at": None, "issue_volume": None}
    return {**base, **kw}


class LinkStateTest(unittest.TestCase):
    def test_recorded_choice_beats_current_setting(self):
        self.assertEqual(_link_state(_link(to_pool=True), {"jeff": False}), "in pool")
        self.assertEqual(_link_state(_link(to_pool=False), {"jeff": True}), "library")

    def test_legacy_rows_fall_back_to_current_senders(self):
        self.assertEqual(_link_state(_link(sender_tag="from-me"), {"from-me": True}), "in pool")
        self.assertEqual(_link_state(_link(sender_tag="gone"), {"jeff": True}), "untracked")

    def test_printed_and_failed(self):
        printed = _link(to_pool=True, issue_sent_at="2026-11-21T06:00:00+00:00", issue_volume=1)
        self.assertEqual(_link_state(printed, {}), "printed")
        self.assertEqual(_link_issue(printed), "Sat 21 Nov")
        self.assertEqual(_link_issue({**printed, "issue_volume": 2}), "Sat 21 Nov · vol. 2")
        self.assertEqual(_link_state(_link(status="error"), {}), "failed")


if __name__ == "__main__":
    unittest.main()
