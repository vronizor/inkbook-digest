import os
import smtplib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from digest import config, mailer, sharing, store


def _cfg(data_dir: Path, share_tag: str = "toshare") -> config.Config:
    env = {
        "READER_TOKEN": "x", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
        "SMTP_FROM": "digest@example.net", "ALERT_EMAIL": "me@example.net",
        "DATA_DIR": str(data_dir), "SHARE_TAG": share_tag, "SHARE_SENDER_NAME": "Test Paper",
    }
    with mock.patch.dict(os.environ, env):
        return config.load()


def _article(i: int, tags: list[str], html: str = "<p>Hello world.</p>", summary: str = "") -> dict:
    return {
        "id": f"doc{i}", "title": f"Title {i}", "author": "Ann Author",
        "source_url": f"https://www.example.com/post/{i}", "published_date": "2026-10-20",
        "word_count": 460, "tags": {t: {} for t in tags}, "html_content": html, "summary": summary,
    }


class SanitizeTest(unittest.TestCase):
    def test_drops_dangerous_and_keeps_text(self):
        out = sharing.sanitize_html(
            '<div class="x" style="color:red"><script>alert(1)</script><p onclick="x()">Hi <b>there</b></p>'
            '<iframe src="https://evil"></iframe><form><input></form></div>'
        )
        self.assertEqual(out, "<p>Hi <b>there</b></p>")

    def test_resolves_links_and_images(self):
        out = sharing.sanitize_html(
            '<p><a href="/a" class="k">rel</a> <a href="javascript:x()">js</a></p>'
            '<img src="img/1.png" alt="one" width="900"><img src="data:image/png;base64,AAA">'
            '<img data-src="//cdn.example.com/2.jpg">',
            "https://site.org/posts/x",
        )
        self.assertIn('<a href="https://site.org/a">rel</a>', out)
        self.assertIn("<a>js</a>", out)
        self.assertIn('<img src="https://site.org/posts/img/1.png" alt="one">', out)
        self.assertIn('<img src="https://cdn.example.com/2.jpg" alt="">', out)
        self.assertNotIn("data:", out)

    def test_demotes_headings_and_breaks_unwrapped_blocks(self):
        out = sharing.sanitize_html("<h1>Big</h1><div>one</div><div>two</div><span>three</span>")
        self.assertEqual(out, "<h3>Big</h3>one<br>two<br>three")

    def test_closes_unbalanced_and_removes_empty(self):
        out = sharing.sanitize_html("<p></p><ul><li>a<li>b</ul><p><em>open")
        self.assertNotIn("<p></p>", out)
        self.assertTrue(out.endswith("<p><em>open</em></p>"))

    def test_escapes_text(self):
        self.assertEqual(sharing.sanitize_html("<p>a &lt;b&gt; &amp; c</p>"), "<p>a &lt;b&gt; &amp; c</p>")


class SelectionAndRenderTest(unittest.TestCase):
    def test_is_shared(self):
        self.assertTrue(sharing.is_shared(_article(1, ["toepub", "toshare"]), "toshare"))
        self.assertFalse(sharing.is_shared(_article(1, ["toepub"]), "toshare"))
        self.assertTrue(sharing.is_shared(_article(1, ["toepub"]), ""))

    def test_teaser_uses_summary_then_first_words(self):
        long_body = "<p>" + " ".join(f"w{i}" for i in range(200)) + "</p>"
        arts = [_article(1, [], summary="A crisp summary."), _article(2, [], html=long_body)]
        out = sharing.render("teaser", arts, today=date(2026, 10, 25), volume=1, sender_name="P")
        self.assertIn("A crisp summary.", out["html"])
        self.assertIn("w59 …", out["html"])
        self.assertNotIn("w60", out["html"])
        self.assertTrue(out["subject"].startswith("Morning Paper headlines · Sun 25 Oct"))

    def test_full_has_toc_body_and_source_links(self):
        arts = [_article(1, [], html="<p>Body one</p>"), _article(2, [], html="<p>Body two</p>")]
        out = sharing.render("full", arts, today=date(2026, 10, 25), volume=2, sender_name="P")
        self.assertIn('href="#a2"', out["html"])
        self.assertIn("Body two", out["html"])
        self.assertIn("Read at example.com →", out["html"])
        self.assertIn("Body one", out["text"])
        self.assertIn("https://www.example.com/post/1", out["text"])
        self.assertEqual(out["subject"], "Morning Paper · Sun 25 Oct (Vol. 2) · 2 articles")

    def test_leading_title_in_body_is_dropped(self):
        art = _article(1, [], html="<div><h1>Title  1</h1><p>Body</p></div>")
        out = sharing.render("full", [art], today=date(2026, 10, 25), volume=1, sender_name="P")
        self.assertEqual(out["html"].count("Title 1"), 1)
        self.assertIn("<p>Body</p>", out["html"])

    def test_title_is_escaped(self):
        art = _article(1, [])
        art["title"] = "<script>x</script>"
        out = sharing.render("full", [art], today=date(2026, 10, 25), volume=1, sender_name="P")
        self.assertNotIn("<script>x", out["html"])


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.cfg = _cfg(self.data_dir)
        self.conn = store.connect(self.data_dir)
        self.digest_id = store.record_digest(self.conn, 3, "sent")
        store.add_subscriber(self.conn, "full@example.com", "F", "full")
        store.add_subscriber(self.conn, "both@example.com", "B", "both")
        store.add_subscriber(self.conn, "paused@example.com", "P", "teaser")
        store.toggle_subscriber(self.conn, 3)
        self.articles = [_article(1, ["toshare"]), _article(2, []), _article(3, ["toshare"])]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_share_digest_sends_per_format_and_skips_paused(self):
        sent = []
        with mock.patch.object(mailer, "_send", lambda h, p, u, pw, msg: sent.append(msg)):
            errors = sharing.share_digest(self.cfg, self.conn, self.digest_id, date(2026, 10, 25), 1, self.articles)
        self.assertEqual(errors, [])
        self.assertEqual(sorted((m["To"], m["Subject"].split(" · ")[0]) for m in sent), [
            ("both@example.com", "Morning Paper"),
            ("both@example.com", "Morning Paper headlines"),
            ("full@example.com", "Morning Paper"),
        ])
        msg = sent[0]
        self.assertEqual(msg["From"], "Test Paper <digest@example.net>")
        self.assertIn("List-Unsubscribe", msg)
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        html = msg.get_body(("html",)).get_content()
        self.assertIn("Title 3", html)
        self.assertNotIn("Title 2", html)
        rows = store.recent_deliveries(self.conn)
        self.assertEqual({r["status"] for r in rows}, {"sent"})
        self.assertEqual(len(rows), 3)
        self.assertTrue((self.data_dir / "emails" / str(self.digest_id) / "teaser.txt").exists())

    def test_failure_is_recorded_and_others_continue(self):
        def flaky(h, p, u, pw, msg):
            if msg["To"] == "full@example.com":
                raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no")})
        with mock.patch.object(mailer, "_send", flaky):
            errors = sharing.share_digest(self.cfg, self.conn, self.digest_id, date(2026, 10, 25), 1, self.articles)
        self.assertEqual(len(errors), 1)
        last = store.last_delivery_by_email(self.conn)
        self.assertEqual(last["full@example.com"]["status"], "failed")
        self.assertEqual(last["both@example.com"]["status"], "sent")

    def test_nothing_shared_sends_nothing(self):
        with mock.patch.object(mailer, "_send") as send:
            errors = sharing.share_digest(
                self.cfg, self.conn, self.digest_id, date(2026, 10, 25), 1, [_article(1, [])]
            )
        self.assertEqual(errors, [])
        send.assert_not_called()
        self.assertIsNone(sharing.latest_issue(self.data_dir))


if __name__ == "__main__":
    unittest.main()
