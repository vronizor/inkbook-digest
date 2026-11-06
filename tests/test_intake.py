import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from digest import config, intake, store

FRIEND = "paul@example.org"
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
        yield from ({k: v for k, v in e.items() if k != "html_content"} for e in self.emails.values())

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


def _email(i: int, body: str, sender: str = FRIEND, author: str = "Paul") -> dict:
    return {"id": f"em{i}", "author": author, "source_url": f"mailto:{sender}", "tags": {}, "html_content": body}


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

    def test_matches_address_in_any_metadata_field_or_display_name(self):
        senders = (("paul@example.org", "from-paul"), ("vincent thorne", "from-me"))
        self.assertEqual(intake.match_sender({"source_url": "mailto:Paul@Example.org"}, senders), "from-paul")
        self.assertEqual(intake.match_sender({"author": "Vincent Thorne"}, senders), "from-me")
        self.assertIsNone(intake.match_sender({"author": "Stranger", "summary": FRIEND}, senders))

    def test_bad_sender_config_fails(self):
        with self.assertRaises(ValueError):
            config._parse_senders("paul@example.org")


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
        fake = FakeReader([_email(1, "<p>https://a.com/1</p>", sender="news@letter.com", author="Letter")])
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

    def test_transient_error_leaves_email_for_next_run(self):
        fake = FakeReader([_email(1, "<p>https://a.com/1 https://b.com/2</p>")], fail={"https://b.com/2": 503})
        self.assertEqual(self._run(fake), 1)
        self.assertEqual(fake.patches, [])
        fake.fail = {}
        self.assertEqual(self._run(fake), 0)
        self.assertEqual([u for u, _ in fake.saved], ["https://a.com/1", "https://b.com/2"])
        self.assertEqual(fake.patches, [("em1", ["from-paul"], True)])


if __name__ == "__main__":
    unittest.main()
