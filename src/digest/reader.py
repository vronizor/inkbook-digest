import logging
from typing import Iterator

import httpx

BASE = "https://readwise.io/api/v3"
log = logging.getLogger(__name__)


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

    def add_tag(self, doc_id: str, current_tag_names: list[str], new_tag: str) -> None:
        """PATCH replaces the full tag list. Send existing names + new tag."""
        if new_tag in current_tag_names:
            return
        names = current_tag_names + [new_tag]
        r = self._client.patch(f"{BASE}/update/{doc_id}/", json={"tags": names})
        if r.status_code not in (200, 204):
            raise RuntimeError(
                f"PATCH /update/{doc_id}/ failed: {r.status_code} {r.text[:200]}"
            )


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
