import html as html_lib
import json
import logging
import math
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from digest import mailer, store
from digest.config import Config
from digest.reader import tag_names, word_count

log = logging.getLogger(__name__)

FORMATS = ("full", "teaser")
SUBSCRIPTION_CHOICES = ("full", "teaser", "both")
TEASER_WORDS = 60
WORDS_PER_MINUTE = 230
SIZE_WARN_BYTES = 100 * 1024

_KEEP = {
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote",
    "pre", "code", "em", "i", "strong", "b", "a", "img", "figure", "figcaption",
    "hr", "sup", "sub",
}
_DROP_WITH_CONTENT = {
    "script", "style", "iframe", "noscript", "svg", "form", "button", "video", "audio",
    "object", "head", "nav", "select", "textarea", "template", "canvas", "math", "title",
}
_VOID = {"br", "hr", "img"}
_BLOCK = {"p", "h3", "h4", "ul", "ol", "li", "blockquote", "pre", "figure", "figcaption", "hr"}
_UNWRAPPED_BLOCK = {
    "div", "section", "article", "main", "header", "footer", "aside", "table", "tr",
    "dl", "dt", "dd", "caption", "center", "address", "details", "summary",
}
_HEADING_MAP = {"h1": "h3", "h2": "h3", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4"}

_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)


def _absolute(url: str, base: str | None, schemes: tuple[str, ...]) -> str | None:
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    resolved = urljoin(base, url) if base else url
    return resolved if urlparse(resolved).scheme in schemes else None


class _Sanitizer(HTMLParser):
    def __init__(self, base_url: str | None):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop_depth = 0
        self.after_block = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_WITH_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth:
            return
        if tag in _UNWRAPPED_BLOCK:
            self._break()
            return
        if tag not in _KEEP:
            return
        tag = _HEADING_MAP.get(tag, tag)
        a = dict(attrs)
        if tag == "a":
            href = _absolute(a.get("href") or "", self.base_url, ("http", "https", "mailto"))
            self.out.append(f'<a href="{html_lib.escape(href)}">' if href else "<a>")
        elif tag == "img":
            src = _absolute(a.get("src") or a.get("data-src") or "", self.base_url, ("http", "https"))
            if src:
                alt = html_lib.escape(a.get("alt") or "")
                self.out.append(f'<img src="{html_lib.escape(src)}" alt="{alt}">')
            return
        else:
            self.out.append(f"<{tag}>")
        if tag in _VOID:
            self.after_block = tag != "br" or self.after_block
            return
        self.stack.append(tag)
        if tag in _BLOCK:
            self.after_block = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_WITH_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth:
            return
        if tag in _UNWRAPPED_BLOCK:
            self._break()
            return
        tag = _HEADING_MAP.get(tag, tag)
        if tag in _VOID or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.out.append(f"</{open_tag}>")
            if open_tag == tag:
                break
        if tag in _BLOCK:
            self.after_block = True

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        if data.strip():
            self.after_block = False
        self.out.append(html_lib.escape(data, quote=False))

    def _break(self) -> None:
        if not self.after_block:
            self.out.append("<br>")
            self.after_block = True

    def result(self) -> str:
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        s = "".join(self.out)
        s = re.sub(r"<(p|a|li|em|strong|b|i|figure|figcaption)>\s*</\1>", "", s)
        s = re.sub(r'<a href="[^"]*">\s*</a>', "", s)
        s = re.sub(r"(<br>\s*){3,}", "<br><br>", s)
        s = re.sub(r"<p>\s*(<br>\s*)+", "<p>", s)
        return s.strip()


def sanitize_html(html: str, base_url: str | None = None) -> str:
    parser = _Sanitizer(base_url)
    parser.feed(html or "")
    return parser.result()


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_WITH_CONTENT:
            self.skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n\n- ")
        elif tag in _BLOCK or tag in _UNWRAPPED_BLOCK or tag in _HEADING_MAP:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK or tag in _UNWRAPPED_BLOCK or tag in _HEADING_MAP:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _first_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words) if len(words) <= n else " ".join(words[:n]) + " …"


def _domain(url: str | None) -> str:
    netloc = urlparse(url or "").netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def describe_mode(share_tag: str) -> str:
    return f"articles also tagged {share_tag!r}" if share_tag else "all digest articles (SHARE_TAG empty)"


def is_shared(article: dict, share_tag: str) -> bool:
    return not share_tag or share_tag in tag_names(article)


def _normalize(s: str) -> str:
    return re.sub(r"\W+", " ", s).strip().lower()


def _strip_leading_title(body: str, title: str) -> str:
    m = re.match(r"\s*<(h3|h4|p)>(.*?)</\1>", body, re.S)
    if m and title and _normalize(html_to_text(m.group(2))) == _normalize(title):
        return body[m.end():].lstrip()
    return body


def _prepare(article: dict) -> dict:
    url = article.get("source_url") or article.get("url") or ""
    raw = article.get("html_content") or article.get("content") or ""
    body = _strip_leading_title(sanitize_html(raw, url or None), article.get("title") or "")
    words = word_count(article)
    meta = [
        article.get("author") or "",
        _domain(url),
        (article.get("published_date") or "")[:10],
        f"{max(1, math.ceil(words / WORDS_PER_MINUTE))} min read" if words else "",
    ]
    summary = (article.get("summary") or "").strip()
    return {
        "title": article.get("title") or "(untitled)",
        "url": url,
        "domain": _domain(url) or "source",
        "meta": " · ".join(m for m in meta if m),
        "body": body,
        "body_text": html_to_text(body),
        "blurb": summary or _first_words(html_to_text(body), TEASER_WORDS),
        "words": words,
    }


def _issue_label(today: date, volume: int) -> str:
    label = f"{today:%a} {today.day} {today:%b}"
    return label if volume == 1 else f"{label} (Vol. {volume})"


def subject_for(fmt: str, today: date, volume: int, count: int) -> str:
    prefix = "Morning Paper" if fmt == "full" else "Morning Paper headlines"
    return f"{prefix} · {_issue_label(today, volume)} · {count} article{'s' if count != 1 else ''}"


def _render_text(fmt: str, articles: list[dict], sender_name: str, issue: str) -> str:
    out = [f"{sender_name} — {issue}", ""]
    if fmt == "full":
        out += [f"{i}. {a['title']} ({a['domain']})" for i, a in enumerate(articles, 1)]
        out.append("")
    for a in articles:
        out += ["=" * 60, a["title"], a["meta"], f"Read at {a['domain']}: {a['url']}", ""]
        out.append(a["body_text"] if fmt == "full" else a["blurb"])
        out.append("")
    out.append("Reply to this email to stop receiving it.")
    return "\n".join(out)


def render(fmt: str, articles: list[dict], *, today: date, volume: int, sender_name: str) -> dict:
    prepared = [_prepare(a) for a in articles]
    issue = _issue_label(today, volume)
    total_words = sum(a["words"] for a in prepared)
    html = _templates.get_template("email.html").render(
        fmt=fmt, articles=prepared, issue=issue, sender_name=sender_name,
        minutes=max(1, math.ceil(total_words / WORDS_PER_MINUTE)) if total_words else 0,
    )
    if len(html.encode()) > SIZE_WARN_BYTES:
        log.warning(f"{fmt} email is {len(html.encode()) // 1024} KB; Gmail web would clip it")
    return {
        "subject": subject_for(fmt, today, volume, len(prepared)),
        "html": html,
        "text": _render_text(fmt, prepared, sender_name, issue),
    }


def emails_dir(data_dir: Path, digest_key: int | str) -> Path:
    return data_dir / "emails" / str(digest_key)


def render_and_save(cfg: Config, digest_key: int | str, today: date, volume: int, articles: list[dict]) -> Path:
    out_dir = emails_dir(cfg.data_dir, digest_key)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {"articles": len(articles), "date": today.isoformat(), "volume": volume, "subjects": {}}
    for fmt in FORMATS:
        rendered = render(fmt, articles, today=today, volume=volume, sender_name=cfg.share_sender_name)
        (out_dir / f"{fmt}.html").write_text(rendered["html"])
        (out_dir / f"{fmt}.txt").write_text(rendered["text"])
        meta["subjects"][fmt] = rendered["subject"]
    (out_dir / "meta.json").write_text(json.dumps(meta))
    return out_dir


def load_issue(data_dir: Path, digest_id: int) -> dict | None:
    out_dir = emails_dir(data_dir, digest_id)
    try:
        meta = json.loads((out_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    meta["digest_id"] = digest_id
    meta["dir"] = out_dir
    return meta


def latest_issue(data_dir: Path) -> dict | None:
    root = data_dir / "emails"
    if not root.exists():
        return None
    for digest_id in sorted((int(p.name) for p in root.iterdir() if p.name.isdigit()), reverse=True):
        issue = load_issue(data_dir, digest_id)
        if issue:
            return issue
    return None


def formats_for(choice: str) -> tuple[str, ...]:
    return FORMATS if choice == "both" else (choice,)


def deliver(
    cfg: Config, conn, issue: dict, subscriber: dict, *, manual: bool = False
) -> list[str]:
    errors: list[str] = []
    for fmt in formats_for(subscriber["format"]):
        try:
            msg = mailer.build_html_message(
                sender=cfg.smtp_from,
                sender_name=cfg.share_sender_name,
                recipient=subscriber["email"],
                subject=issue["subjects"][fmt],
                html=(issue["dir"] / f"{fmt}.html").read_text(),
                text=(issue["dir"] / f"{fmt}.txt").read_text(),
                reply_to=cfg.share_reply_to,
            )
            size = mailer.send_message(
                host=cfg.smtp_host, port=cfg.smtp_port,
                user=cfg.smtp_user, password=cfg.smtp_password, msg=msg,
            )
            store.record_delivery(conn, issue["digest_id"], subscriber["email"], fmt, "sent",
                                  size_bytes=size, manual=manual)
        except Exception as e:
            log.warning(f"delivery to {subscriber['email']} ({fmt}) failed: {e}")
            store.record_delivery(conn, issue["digest_id"], subscriber["email"], fmt, "failed",
                                  error=str(e)[:500], manual=manual)
            errors.append(f"{subscriber['email']} ({fmt}): {e}")
    return errors


def share_digest(
    cfg: Config, conn, digest_id: int, today: date, volume: int, articles: list[dict]
) -> list[str]:
    """Render and email the shared subset of a digest. Returns per-delivery error strings; never raises."""
    shared = [a for a in articles if is_shared(a, cfg.share_tag)]
    if not shared:
        log.info(f"sharing: no articles tagged {cfg.share_tag!r} in this digest, nothing to send")
        return []
    try:
        render_and_save(cfg, digest_id, today, volume, shared)
    except Exception as e:
        log.exception(f"sharing: render failed: {e}")
        return [f"render failed: {e}"]
    issue = load_issue(cfg.data_dir, digest_id)
    subscribers = store.list_subscribers(conn, active_only=True)
    log.info(f"sharing: {len(shared)} article(s) to {len(subscribers)} active subscriber(s)")
    errors: list[str] = []
    for sub in subscribers:
        errors += deliver(cfg, conn, issue, sub)
    return errors


def render_dry_run(cfg: Config, today: date, volume: int, articles: list[dict]) -> None:
    shared = [a for a in articles if is_shared(a, cfg.share_tag)]
    if not shared:
        log.info(f"dry-run sharing: no articles tagged {cfg.share_tag!r}")
        return
    out_dir = render_and_save(cfg, "dry-run", today, volume, shared)
    log.info(f"dry-run sharing: {len(shared)} article(s) rendered to {out_dir}, nothing sent")
