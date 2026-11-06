import logging
import re
import time
from typing import Iterator

import httpx

BASE = "https://readwise.io/api/v3"
log = logging.getLogger(__name__)

_TAG_STRIP_RE = re.compile(r"<[^>]+>")


class Reader:
    def __init__(self, token: str):
        self._client = httpx.Client(
            headers={"Authorization": f"Token {token}"},
            timeout=60.0,
        )

    def close(self) -> None:
        self._client.close()

    def list_tagged_articles(self, tag: str) -> Iterator[dict]:
        """Yields all article-category docs server-side filtered by tag.

        Server-side filter verified working. Client-side filter on `tags`
        kept as a defensive check in case the server ever stops honoring it.
        """
        cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "category": "article",
                "tag": tag,
                "withHtmlContent": "true",
            }
            if cursor:
                params["pageCursor"] = cursor
            r = self._client.get(f"{BASE}/list/", params=params)
            r.raise_for_status()
            data = r.json()
            for item in data.get("results", []):
                if _has_tag(item, tag):
                    yield item
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

    def list_queue(self, tag: str) -> list[dict]:
        """All toepub-tagged articles, fully materialized (queue is small)."""
        return list(self.list_tagged_articles(tag))

    def get_document(self, doc_id: str) -> dict | None:
        r = self._client.get(
            f"{BASE}/list/", params={"id": doc_id, "withHtmlContent": "true"}
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "60"))
            log.info(f"rate limited, sleeping {wait}s")
            time.sleep(wait)
            return self.get_document(doc_id)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def add_tag(self, doc_id: str, current_tag_names: list[str], new_tag: str) -> None:
        self.add_tags(doc_id, current_tag_names, [new_tag])

    def add_tags(
        self, doc_id: str, current_tag_names: list[str], new_tags: list[str], *, archive: bool = False
    ) -> None:
        """PATCH replaces the full tag list, so send existing names + new tags."""
        names = current_tag_names + [t for t in new_tags if t not in current_tag_names]
        body: dict[str, object] = {}
        if names != current_tag_names:
            body["tags"] = names
        if archive:
            body["location"] = "archive"
        if not body:
            return
        r = self._client.patch(f"{BASE}/update/{doc_id}/", json=body)
        r.raise_for_status()

    def list_documents(self, *, category: str, updated_after: str) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"category": category, "updatedAfter": updated_after}
            if cursor:
                params["pageCursor"] = cursor
            r = self._client.get(f"{BASE}/list/", params=params)
            r.raise_for_status()
            data = r.json()
            yield from data.get("results", [])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

    def save_url(self, url: str, tags: list[str], *, saved_using: str) -> tuple[str, bool]:
        """Returns (document id, created). Reader answers 200 instead of 201 for URLs it already has."""
        r = self._client.post(
            f"{BASE}/save/", json={"url": url, "tags": tags, "saved_using": saved_using}
        )
        r.raise_for_status()
        return r.json()["id"], r.status_code == 201


def _has_tag(item: dict, tag: str) -> bool:
    tags = item.get("tags") or {}
    if isinstance(tags, dict):
        return tag in tags
    if isinstance(tags, list):
        return any(
            (t.get("name") if isinstance(t, dict) else t) == tag for t in tags
        )
    return False


def tag_names(item: dict) -> list[str]:
    tags = item.get("tags") or {}
    if isinstance(tags, dict):
        return list(tags.keys())
    if isinstance(tags, list):
        return [t.get("name") if isinstance(t, dict) else t for t in tags]
    return []


def word_count(item: dict) -> int:
    """Word count for an article. Reader's value if present and >0; else fallback to body length."""
    raw = item.get("word_count")
    try:
        if raw is not None and int(raw) > 0:
            return int(raw)
    except (TypeError, ValueError):
        pass
    html = item.get("html_content") or item.get("content") or ""
    if not html:
        return 0
    text = _TAG_STRIP_RE.sub(" ", html)
    fallback = len(text.split())
    log.info(
        f"word_count fallback for {item.get('id')!r}: Reader missing/zero, computed {fallback}"
    )
    return fallback
