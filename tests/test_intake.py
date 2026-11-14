import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from digest import config, intake, store

FRIEND = "Paul Pagel"
IGNORE = ("proton.me", "protonmail.com", "pm.me", "readwise.io")


def _cfg(data_dir: Path) -> config.Config:
    env = {
        "READER_TOKEN": "x", "DATA_DIR": str(data_dir),
        "INTAKE_SENDERS": f"{FRIEND}|from-paul; Vincent Thorne|from-me",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return config.load(require_smtp=False)


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://readwise.io/api/v3/save/")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class FakeReader:
    def __init__(self, emails: list[dict], existing: set[str] = frozenset(), fail: dict[str, int] | None = None):
        self.emails = {e["id"]: e for e in emails}
        self.existing = set(existing)
        self.fail = fail or {}
        self.saved: list[tuple[str, list[str]]] = []
        self.patches: list[tuple[str, list[str], bool]] = []

    def list_documents(self, *, category: str, updated_after: str):
        for e in self.emails.values():
            if e["updated_at"] > updated_after:
                yield {k: v for k, v in e.items() if k != "html_content"}

    def get_document(self, doc_id: str) -> dict | None:
        return self.emails.get(doc_id, {"id": doc_id, "tags": {"old": {}}})

    def save_url(self, url: str, tags: list[str], *, saved_using: str) -> tuple[str, bool]:
        if url in self.fail:
            raise _http_error(self.fail[url])
        self.saved.append((url, tags))
        return f"art-{len(self.saved)}", url not in self.existing

    def add_tags(self, doc_id: str, current: list[str], new: list[str], *, archive: bool = False) -> None:
        self.patches.append((doc_id, current + [t for t in new if t not in current], archive))

    def close(self) -> None:
        pass


def _email(i: int, body: str, author: str = FRIEND, age: timedelta = timedelta(days=2)) -> dict:
    return {
        "id": f"em{i}", "author": author, "tags": {}, "html_content": body,
        "updated_at": (datetime.now(timezone.utc) - age).isoformat(),
        "source_url": "mailto:reader-forwarded-email/935f98597a2f24f9a9aec707ad524329",
    }


class ParsingTest(unittest.TestCase):
    def test_extracts_links_from_href_and_text_once(self):
        body = (
            '<p><a href="https://www.not-ship.com/how-amazon/">https://www.not-ship.com/how-amazon/</a></p>'
            '<p>and https://example.com/a?x=1&amp;y=2.</p>'
            '<p>Sent with <a href="https://proton.me/mail/home">Proton Mail</a> secure email.</p>'
        )
        self.assertEqual(
            intake.extract_links(body, IGNORE),
            ["https://www.not-ship.com/how-amazon/", "https://example.com/a?x=1&y=2"],
        )

    def test_prose_words_ignores_urls_and_markup(self):
        self.assertEqual(intake.prose_words('<p>read this https://x.com/a <b>now</b></p>'), 3)

    def test_matches_display_name_case_insensitively(self):
        senders = (("paul pagel", "paul", False), ("vincent thorne", "from-me", True))
        self.assertEqual(intake.match_sender({"author": " vincent thorne "}, senders), ("from-me", True))
        self.assertIsNone(intake.match_sender({"author": "Paul"}, senders))
        self.assertIsNone(intake.match_sender({"author": None}, senders))

    def test_subscriber_tag_is_normalized_first_name_with_fallbacks(self):
        tag = intake.subscriber_tag
        self.assertEqual(tag({"name": "Jeff Pagel", "sender_name": None, "email": "j@x.org"}), "jeff")
        self.assertEqual(tag({"name": "Éléonore", "sender_name": None, "email": "e@x.org"}), "eleonore")
        self.assertEqual(tag({"name": "Jean-Marc", "sender_name": None, "email": "j@x.org"}), "jean-marc")
        self.assertEqual(tag({"name": None, "sender_name": "Eero M.", "email": "e@x.org"}), "eero")
        self.assertEqual(tag({"name": "", "sender_name": "", "email": "pagel.jm2@x.org"}), "pageljm2")

    def test_bad_sender_config_fails(self):
        with self.assertRaises(ValueError):
            config._parse_senders("Paul Pagel")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fake: FakeReader) -> int:
        with mock.patch.object(intake, "Reader", return_value=fake):
            return intake.run(self.cfg)

    def test_saves_each_link_tags_and_archives_email(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p><p>https://b.com/2</p>")])
        self.assertEqual(self._run(fake), 0)
        self.assertEqual(fake.saved, [
            ("https://a.com/1", ["from-paul", "toepub"]),
            ("https://b.com/2", ["from-paul", "toepub"]),
        ])
        self.assertEqual(fake.patches, [("em1", ["from-paul"], True)])

    def test_second_run_skips_processed_email(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>")])
        self._run(fake)
        self._run(fake)
        self.assertEqual(len(fake.saved), 1)

    def test_ignores_unknown_senders(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", author="Some Newsletter")])
        self._run(fake)
        self.assertEqual(fake.saved, [])

    def test_forwarded_newsletter_is_tagged_not_exploded(self):
        prose = " ".join(["word"] * 80)
        fake = FakeReader([_email(1, f"<p>{prose} https://a.com/1 https://b.com/2</p>")])
        self._run(fake)
        self.assertEqual(fake.saved, [])
        self.assertEqual(fake.patches, [("em1", ["from-paul"], False)])

    def test_existing_article_gets_tags_added(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>")], existing={"https://a.com/1"})
        self._run(fake)
        self.assertIn(("art-1", ["old", "from-paul", "toepub"], False), fake.patches)

    def test_refused_link_is_logged_and_others_still_saved(self):
        fake = FakeReader([_email(1, "<p>https://bad.com/x https://a.com/1</p>")], fail={"https://bad.com/x": 400})
        self._run(fake)
        self.assertEqual([u for u, _ in fake.saved], ["https://a.com/1"])
        conn = store.connect(self.cfg.data_dir)
        statuses = {l["url"]: l["status"] for l in store.recent_intake_links(conn)}
        conn.close()
        self.assertEqual(statuses, {"https://bad.com/x": "error", "https://a.com/1": "saved"})

    def test_email_older_than_previous_run_is_still_picked_up(self):
        self._run(FakeReader([]))
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>")])
        self._run(fake)
        self.assertEqual([u for u, _ in fake.saved], ["https://a.com/1"])

    def test_ignores_emails_outside_lookback(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", age=timedelta(days=30))])
        self._run(fake)
        self.assertEqual(fake.saved, [])

    def test_transient_error_leaves_email_for_next_run(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1 https://b.com/2</p>")], fail={"https://b.com/2": 503})
        self.assertEqual(self._run(fake), 1)
        self.assertEqual(fake.patches, [])
        fake.fail = {}
        self.assertEqual(self._run(fake), 0)
        self.assertEqual([u for u, _ in fake.saved], ["https://a.com/1", "https://b.com/2"])
        self.assertEqual(fake.patches, [("em1", ["from-paul"], True)])


class SubscriberSendersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(Path(self.tmp.name))
        self.conn = store.connect(self.cfg.data_dir)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _run(self, fake: FakeReader) -> int:
        with mock.patch.object(intake, "Reader", return_value=fake):
            return intake.run(self.cfg)

    def test_matched_by_sender_name_and_pooled(self):
        store.add_subscriber(self.conn, "jeff@example.org", "Jeff", "both", to_pool=True, sender_name="Jeff Pagel")
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", author="Jeff Pagel")])
        self._run(fake)
        self.assertEqual(fake.saved, [("https://a.com/1", ["jeff", "toepub"])])
        self.assertEqual(fake.patches, [("em1", ["jeff"], True)])

    def test_unpooled_subscriber_links_go_to_library_only(self):
        store.add_subscriber(self.conn, "eero@example.org", "Eero", "teaser", sender_name="Eero M")
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", author="Eero M")])
        self._run(fake)
        self.assertEqual(fake.saved, [("https://a.com/1", ["eero"])])

    def test_link_records_pool_choice_at_intake(self):
        store.add_subscriber(self.conn, "jeff@example.org", "Jeff", "both", to_pool=True, sender_name="Jeff Pagel")
        store.add_subscriber(self.conn, "eero@example.org", "Eero", "teaser", sender_name="Eero M")
        fake = FakeReader([
            _email(1, "<p>https://a.com/1</p>", author="Jeff Pagel"),
            _email(2, "<p>https://b.com/2</p>", author="Eero M"),
            _email(3, "<p>https://c.com/3</p>", author="Vincent Thorne"),
        ])
        self._run(fake)
        pooled = {l["url"]: l["to_pool"] for l in store.recent_intake_links_with_issue(self.conn)}
        self.assertEqual(pooled, {"https://a.com/1": True, "https://b.com/2": False, "https://c.com/3": True})

    def test_subscriber_without_sender_name_is_not_a_sender(self):
        store.add_subscriber(self.conn, "eero@example.org", "Eero", "teaser")
        self.assertNotIn("eero", [tag for _, tag, _ in intake.senders(self.cfg, self.conn)])

    def test_subscriber_overrides_env_entry_for_same_name(self):
        store.add_subscriber(self.conn, "paul@example.org", "Paul", "full", sender_name=FRIEND)
        found = intake.senders(self.cfg, self.conn)
        self.assertIn(("paul pagel", "paul", False), found)
        self.assertNotIn(("paul pagel", "from-paul", True), found)
        self.assertIn(("vincent thorne", "from-me", True), found)

    def test_sender_name_set_later_still_catches_earlier_email(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", author="Jeff Pagel")])
        self._run(fake)
        self.assertEqual(fake.saved, [])
        store.add_subscriber(self.conn, "jeff@example.org", "Jeff", "both", sender_name="Jeff Pagel")
        self._run(fake)
        self.assertEqual(fake.saved, [("https://a.com/1", ["jeff"])])

    def test_no_senders_skips_reader(self):
        with mock.patch.dict(os.environ, {"READER_TOKEN": "x", "DATA_DIR": self.tmp.name}, clear=True):
            cfg = config.load(require_smtp=False)
        with mock.patch.object(intake, "Reader") as reader:
            self.assertEqual(intake.run(cfg), 0)
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
