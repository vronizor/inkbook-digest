import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from digest import store
from digest.reader import Reader, word_count as reader_word_count

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

LOG_DISPLAY_CAP_BYTES = 20 * 1024
TABLE_LIMIT = 30
QUEUE_TTL_OK = 60
QUEUE_TTL_ERR = 30
WB_MIN = 500
WB_MAX = 50000

_queue_cache: dict = {"data": None, "expires": 0.0, "error": None}

router = APIRouter()


def _domain(url):
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


templates.env.filters["domain"] = _domain


def _truncate_log(s: str | None) -> str:
    if not s:
        return ""
    b = s.encode("utf-8")
    if len(b) <= LOG_DISPLAY_CAP_BYTES:
        return s
    return b[:LOG_DISPLAY_CAP_BYTES].decode("utf-8", errors="ignore") + "\n... (truncated)"


def _get_queue_stats(cfg, conn) -> tuple[dict | None, str | None]:
    now = time.time()
    if _queue_cache["expires"] > now:
        return _queue_cache["data"], _queue_cache["error"]
    try:
        reader = Reader(cfg.reader_token)
        try:
            articles = reader.list_queue(cfg.reader_tag_trigger)
        finally:
            reader.close()
        already = store.already_sent_ids(conn)
        pending = [a for a in articles if a["id"] not in already]
        total_words = sum(reader_word_count(a) for a in pending)
        stats = {"count": len(pending), "total_words": total_words}
        _queue_cache.update(data=stats, error=None, expires=now + QUEUE_TTL_OK)
        return stats, None
    except Exception as e:
        log.warning(f"queue stats failed: {e}")
        _queue_cache.update(data=None, error=str(e), expires=now + QUEUE_TTL_ERR)
        return None, str(e)


def _chart_data(conn) -> list[dict]:
    today = date.today()
    rows = conn.execute(
        "SELECT DATE(sent_at) AS day, COUNT(*) AS n, COALESCE(SUM(word_count), 0) AS words "
        "FROM sent_articles WHERE sent_at >= DATE('now', '-29 days') "
        "GROUP BY day"
    ).fetchall()
    by_day = {r[0]: (r[1], r[2]) for r in rows}
    out = []
    for i in range(30):
        d = (today - timedelta(days=29 - i)).isoformat()
        n, w = by_day.get(d, (0, 0))
        out.append({"day": d, "n": n, "words": w})
    return out


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = request.app.state.cfg
    scheduler = request.app.state.scheduler
    is_running = request.app.state.is_running

    conn = store.connect(cfg.data_dir)
    try:
        last = conn.execute(
            "SELECT sent_at, article_count, volume, total_words FROM digests "
            "WHERE status = 'sent' ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()

        totals_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(article_count), 0), COALESCE(SUM(total_words), 0) "
            "FROM digests WHERE status = 'sent'"
        ).fetchone()
        totals = {
            "digests": totals_row[0],
            "articles": totals_row[1],
            "words": totals_row[2],
        }

        digests = [
            {
                "id": r[0], "sent_at": r[1], "volume": r[2], "article_count": r[3],
                "total_words": r[4], "status": r[5],
            }
            for r in conn.execute(
                "SELECT id, sent_at, volume, article_count, total_words, status FROM digests "
                "ORDER BY sent_at DESC LIMIT ?", (TABLE_LIMIT,),
            )
        ]

        runs = []
        for r in conn.execute(
            "SELECT id, started_at, finished_at, outcome, log FROM runs "
            "ORDER BY started_at DESC LIMIT ?", (TABLE_LIMIT,),
        ):
            duration = None
            if r[2]:
                duration = (
                    datetime.fromisoformat(r[2]) - datetime.fromisoformat(r[1])
                ).total_seconds()
            runs.append({
                "id": r[0], "started_at": r[1], "outcome": r[3] or "running",
                "duration": duration, "log": _truncate_log(r[4]),
            })

        articles = [
            {"title": r[0] or "(untitled)", "url": r[1] or "", "word_count": r[2], "sent_at": r[3]}
            for r in conn.execute(
                "SELECT title, url, word_count, sent_at FROM sent_articles "
                "ORDER BY sent_at DESC LIMIT ?", (TABLE_LIMIT,),
            )
        ]

        paused = store.get_setting(conn, "paused") == "true"
        word_budget = store.get_word_budget(conn)
        queue_stats, queue_error = _get_queue_stats(cfg, conn)
        chart = _chart_data(conn)
    finally:
        conn.close()

    next_run = None
    if not paused:
        job = scheduler.get_job("daily-digest")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

    return templates.TemplateResponse(
        request, "index.html", {
            "last_digest": last, "next_run": next_run, "paused": paused,
            "queue_stats": queue_stats, "queue_error": queue_error,
            "totals": totals, "digests": digests, "runs": runs, "articles": articles,
            "chart_data": chart, "word_budget": word_budget,
            "wb_min": WB_MIN, "wb_max": WB_MAX,
            "triggered": request.query_params.get("triggered"),
            "budget_flash": request.query_params.get("budget"),
            "is_running": is_running.locked(),
        },
    )


@router.get("/digests/{digest_id}/epub")
def download_epub(digest_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT sent_at, volume FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    path = store.build_epub_path(cfg.data_dir, row[0], row[1])
    if not path.exists():
        return PlainTextResponse("EPUB no longer available", status_code=404)
    return FileResponse(path, filename=path.name)


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int, request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute("SELECT log FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(row[0] or "")


@router.post("/trigger")
def trigger(bg: BackgroundTasks, request: Request) -> RedirectResponse:
    is_running = request.app.state.is_running
    if is_running.locked():
        return RedirectResponse(url="/?triggered=already_running", status_code=303)
    cfg = request.app.state.cfg
    run_with_lock = request.app.state.run_with_lock
    bg.add_task(run_with_lock, cfg, manual_trigger=True)
    return RedirectResponse(url="/?triggered=ok", status_code=303)


@router.post("/pause")
def pause(request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        current = store.get_setting(conn, "paused") or "false"
        new_value = "false" if current == "true" else "true"
        store.set_setting(conn, "paused", new_value)
        log.info(f"pause toggled: {current} -> {new_value}")
    finally:
        conn.close()
    return RedirectResponse(url="/", status_code=303)


@router.post("/word-budget")
def word_budget(request: Request, value: str = Form(...)) -> RedirectResponse:
    try:
        n = int(value.strip())
    except (ValueError, AttributeError):
        return RedirectResponse(url="/?budget=invalid", status_code=303)
    if n < WB_MIN or n > WB_MAX:
        return RedirectResponse(url="/?budget=invalid", status_code=303)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        store.set_setting(conn, "word_budget", str(n))
        log.info(f"word_budget updated to {n}")
    finally:
        conn.close()
    return RedirectResponse(url="/?budget=ok", status_code=303)
