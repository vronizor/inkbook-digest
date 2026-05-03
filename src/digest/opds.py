import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from digest import library, store

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"

router = APIRouter(prefix="/opds")


def _base(cfg, request: Request) -> str:
    return cfg.base_url or str(request.base_url).rstrip("/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_entry(d: dict, base: str) -> dict:
    sent_at = d["sent_at"]
    day = sent_at[:10]
    vol = d["volume"]
    title = f"Morning Paper {day}" + (f" (Vol. {vol})" if vol > 1 else "")
    summary = (
        f"{d['article_count']} article" + ("s" if d["article_count"] != 1 else "")
        + f", {d['total_words']:,} words"
    )
    return {
        "id": f"urn:inkbook-digest:digest:{d['id']}",
        "title": title,
        "author": "Readwise Reader Digest",
        "updated": sent_at,
        "published": sent_at,
        "summary": summary,
        "language": "en",
        "cover_url": f"{base}/opds/cover/digest/{d['id']}",
        "acquisition_url": f"{base}/opds/file/digest/{d['id']}",
        "media_type": "application/epub+zip",
    }


def _library_entry(b: dict, base: str) -> dict:
    media_type = "application/epub+zip" if b["format"] == "epub" else "application/pdf"
    title = b["title"] or b["filename"]
    return {
        "id": f"urn:inkbook-digest:library:{b['id']}",
        "title": title,
        "author": b["author"] or "Unknown",
        "updated": b["added_at"],
        "published": b["added_at"],
        "summary": b["filename"],
        "language": b["language"] or "",
        "cover_url": f"{base}/opds/cover/library/{b['id']}",
        "acquisition_url": f"{base}/opds/file/library/{b['id']}",
        "media_type": media_type,
    }


@router.get("/")
def root(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    entries = [
        {
            "id": "urn:inkbook-digest:catalog:digests",
            "title": "Morning Papers",
            "summary": "Daily digests built from Readwise Reader articles.",
            "href": f"{base}/opds/digests/",
        },
        {
            "id": "urn:inkbook-digest:catalog:library",
            "title": "Library",
            "summary": "Manually uploaded EPUBs and PDFs.",
            "href": f"{base}/opds/library/",
        },
    ]
    body = templates.get_template("opds_navigation.xml").render(
        feed_id="urn:inkbook-digest:root",
        feed_title="E-books & Morning Paper",
        updated=_now_iso(),
        self_url=f"{base}/opds/",
        entries=entries,
    )
    return Response(content=body, media_type=NAV_TYPE)


@router.get("/digests/")
def digests_feed(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    conn = store.connect(cfg.data_dir)
    try:
        rows = conn.execute(
            "SELECT id, sent_at, volume, article_count, total_words FROM digests "
            "WHERE status = 'sent' AND sent_at >= datetime('now', '-30 days') "
            "ORDER BY sent_at DESC"
        ).fetchall()
    finally:
        conn.close()
    digests_data = [
        {"id": r[0], "sent_at": r[1], "volume": r[2],
         "article_count": r[3], "total_words": r[4]}
        for r in rows
    ]
    entries = [_digest_entry(d, base) for d in digests_data]
    body = templates.get_template("opds_acquisition.xml").render(
        feed_id="urn:inkbook-digest:catalog:digests",
        feed_title="Morning Papers",
        updated=_now_iso(),
        self_url=f"{base}/opds/digests/",
        root_url=f"{base}/opds/",
        entries=entries,
    )
    return Response(content=body, media_type=ACQ_TYPE)


@router.get("/library/")
def library_feed(request: Request) -> Response:
    cfg = request.app.state.cfg
    base = _base(cfg, request)
    conn = store.connect(cfg.data_dir)
    try:
        books = library.list_books(conn)
    finally:
        conn.close()
    entries = [_library_entry(b, base) for b in books]
    body = templates.get_template("opds_acquisition.xml").render(
        feed_id="urn:inkbook-digest:catalog:library",
        feed_title="Library",
        updated=_now_iso(),
        self_url=f"{base}/opds/library/",
        root_url=f"{base}/opds/",
        entries=entries,
    )
    return Response(content=body, media_type=ACQ_TYPE)


@router.get("/file/digest/{digest_id}")
def file_digest(digest_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT sent_at, volume FROM digests WHERE id = ? AND status = 'sent'",
            (digest_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return Response("not found", status_code=404, media_type="text/plain")
    path = store.build_epub_path(cfg.data_dir, row[0], row[1])
    if not path.exists():
        return Response("EPUB no longer available", status_code=404, media_type="text/plain")
    return FileResponse(path, media_type="application/epub+zip", filename=path.name)


@router.get("/file/library/{book_id}")
def file_library(book_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT filename, format FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return Response("not found", status_code=404, media_type="text/plain")
    filename, fmt = row
    path = library.file_path(cfg.data_dir, filename)
    if not path.exists():
        return Response("file missing", status_code=404, media_type="text/plain")
    media = "application/epub+zip" if fmt == "epub" else "application/pdf"
    return FileResponse(path, media_type=media, filename=filename)


@router.get("/cover/digest/{digest_id}")
def cover_digest(digest_id: int, request: Request):
    cfg = request.app.state.cfg
    placeholder = _PKG_DIR / "static" / "placeholder-epub.png"
    return FileResponse(placeholder, media_type="image/png")


@router.get("/cover/library/{book_id}")
def cover_library(book_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT format, has_cover FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return Response("not found", status_code=404, media_type="text/plain")
    fmt, has_cover = row
    if has_cover:
        cp = library.cover_path(cfg.data_dir, book_id)
        if cp.exists():
            return FileResponse(cp, media_type="image/jpeg")
    placeholder_name = "placeholder-pdf.png" if fmt == "pdf" else "placeholder-epub.png"
    return FileResponse(_PKG_DIR / "static" / placeholder_name, media_type="image/png")
