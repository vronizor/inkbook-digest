import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from digest import intake, library, pool, schedule, sharing, store

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))

LOG_DISPLAY_CAP_BYTES = 20 * 1024
TABLE_LIMIT = 30
QUEUE_TTL_OK = 60
QUEUE_TTL_ERR = 30
WB_MIN = 500
WB_MAX = 50000
DELIVERY_LIMIT = 20
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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


def _human_size(b):
    if b is None:
        return ""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024


templates.env.filters["domain"] = _domain
templates.env.filters["human_size"] = _human_size


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        return iso
    local = dt.astimezone(ZoneInfo(os.environ.get("TZ", "Europe/Paris")))
    year = "" if local.year == datetime.now(local.tzinfo).year else f" {local.year}"
    return f"{local:%a} {local.day} {local:%b}{year}, {local:%H:%M}"


templates.env.filters["when"] = _when
templates.env.globals["asset_version"] = int((_PKG_DIR / "static" / "style.css").stat().st_mtime)


def _opds_url(cfg, request: Request) -> str:
    base = cfg.base_url or str(request.base_url).rstrip("/")
    return f"{base}/opds/"


def _upload_flashes(request: Request) -> list[dict]:
    kinds = request.query_params.getlist("library_upload")
    names = request.query_params.getlist("name")
    reasons = request.query_params.getlist("reason")
    out = []
    for i, kind in enumerate(kinds):
        out.append({
            "kind": kind,
            "name": names[i] if i < len(names) else "",
            "reason": reasons[i] if i < len(reasons) else "",
        })
    return out


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
        stats = pool.measure(cfg, conn)
        pool.record(conn, cfg.tz, stats)
        _queue_cache.update(data=stats, error=None, expires=now + QUEUE_TTL_OK)
        return stats, None
    except Exception as e:
        log.warning(f"queue stats failed: {e}")
        _queue_cache.update(data=None, error=str(e), expires=now + QUEUE_TTL_ERR)
        return None, str(e)


def _editions(conn, limit: int) -> list[dict]:
    digests = conn.execute(
        "SELECT id, sent_at, volume, article_count, total_words, status FROM digests "
        "ORDER BY sent_at DESC LIMIT ?", (limit,),
    ).fetchall()
    if not digests:
        return []
    ids = [d[0] for d in digests]
    marks = ",".join("?" * len(ids))
    articles: dict[int, list[dict]] = {i: [] for i in ids}
    for digest_id, title, url, words in conn.execute(
        f"SELECT digest_id, title, url, word_count FROM sent_articles WHERE digest_id IN ({marks}) "
        "ORDER BY rowid", ids,
    ):
        articles[digest_id].append({"title": title or "(untitled)", "url": url or "", "words": words})
    mailed: dict[int, dict[str, int]] = {i: {} for i in ids}
    for digest_id, status, n in conn.execute(
        f"SELECT digest_id, status, COUNT(DISTINCT email) FROM deliveries WHERE digest_id IN ({marks}) "
        "GROUP BY digest_id, status", ids,
    ):
        mailed[digest_id][status] = n
    return [
        {
            "id": d[0], "sent_at": d[1], "volume": d[2], "article_count": d[3], "total_words": d[4],
            "status": d[5], "articles": articles[d[0]],
            "mailed": mailed[d[0]].get("sent", 0), "mail_failed": mailed[d[0]].get("failed", 0),
        }
        for d in digests
    ]


def _link_state(link: dict, pool_by_tag: dict[str, bool]) -> str:
    if link["status"] == "error":
        return "failed"
    if link["issue_sent_at"]:
        return f"in issue {_when(link['issue_sent_at']).split(',')[0]}"
    if link["sender_tag"] in pool_by_tag:
        return "in pool" if pool_by_tag[link["sender_tag"]] else "library"
    return "saved"


def _sidebar(cfg, conn, request: Request) -> dict:
    paused = store.get_setting(conn, "paused") == "true"
    sched = schedule.load(conn)
    now = datetime.now(ZoneInfo(cfg.tz))
    upcoming = schedule.next_runs(sched, now, 2)
    return {
        "paused": paused,
        "sched": sched,
        "now": now,
        "upcoming": upcoming,
        "next_run": upcoming[0] if upcoming and not paused else None,
        "is_running": request.app.state.is_running.locked(),
        "opds_url": _opds_url(cfg, request),
        "book_count": conn.execute("SELECT COUNT(*) FROM library_books").fetchone()[0],
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        side = _sidebar(cfg, conn, request)
        totals_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(article_count), 0), COALESCE(SUM(total_words), 0) "
            "FROM digests WHERE status = 'sent'"
        ).fetchone()
        totals = {"digests": totals_row[0], "articles": totals_row[1], "words": totals_row[2]}
        editions = _editions(conn, TABLE_LIMIT)

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

        word_budget = store.get_word_budget(conn)
        queue_stats, queue_error = _get_queue_stats(cfg, conn)
        pool_summary = None
        if queue_stats is not None:
            pool_summary = pool.summary(
                queue_stats["total_words"],
                pool.average_issue_words(conn, word_budget),
                pool.words_around(conn, side["now"].date() - timedelta(days=pool.MONTH_LOOKBACK_DAYS)),
            )
        chart = pool.chart_view(pool.weekly(conn, cfg.tz, side["now"].date()), word_budget)

        subscribers = store.list_subscribers(conn)
        last_delivery = store.last_delivery_by_email(conn)
        for sub in subscribers:
            sub["last"] = last_delivery.get(sub["email"])
            sub["tag"] = intake.subscriber_tag(sub)
        pool_by_tag = {s["tag"]: s["to_pool"] for s in subscribers}
        links = store.recent_intake_links_with_issue(conn, TABLE_LIMIT)
        for link in links:
            link["state"] = _link_state(link, pool_by_tag)
        deliveries = store.recent_deliveries(conn, DELIVERY_LIMIT)
    finally:
        conn.close()

    today = side["now"].date()
    return templates.TemplateResponse(
        request, "index.html", {
            **side,
            "page": "kiosk",
            "next_phrase": "Paused. No issue scheduled." if side["paused"]
                           else pool.next_issue_phrase(side["next_run"], side["now"]),
            "pool_summary": pool_summary, "queue_error": queue_error,
            "chart": chart,
            "chart_weeks_json": [
                {k: w[k] for k in ("full", "words", "n", "avg", "pool", "added")} for w in chart["weeks"]
            ],
            "totals": totals, "editions": editions, "runs": runs,
            "word_budget": word_budget, "wb_min": WB_MIN, "wb_max": WB_MAX,
            "sched_summary": schedule.describe(side["sched"]),
            "sched_upcoming": [r.isoformat() for r in side["upcoming"]],
            "this_week_on": schedule.is_on_week(side["sched"], today),
            "next_week_on": schedule.is_on_week(side["sched"], today + timedelta(days=7)),
            "weekday_names": schedule.FULL,
            "weeks_max": schedule.WEEKS_MAX,
            "triggered": request.query_params.get("triggered"),
            "settings_flash": request.query_params.get("settings"),
            "settings_detail": request.query_params.get("detail", ""),
            "subscribers": subscribers,
            "deliveries": deliveries,
            "latest_issue": sharing.latest_issue(cfg.data_dir),
            "share_tag": cfg.share_tag,
            "share_mode": sharing.describe_mode(cfg.share_tag),
            "subscription_choices": sharing.SUBSCRIPTION_CHOICES,
            "sub_flash": request.query_params.get("sub"),
            "sub_flash_detail": request.query_params.get("detail", ""),
            "intake_enabled": bool(subscribers or cfg.intake_senders),
            "trigger_tag": cfg.reader_tag_trigger,
            "links": links,
            "failed_links": sum(1 for l in links[:10] if l["state"] == "failed"),
            "intake_flash": request.query_params.get("intake"),
        },
    )


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        side = _sidebar(cfg, conn, request)
        books = library.list_books(conn)
    finally:
        conn.close()
    delete_flash = (
        request.query_params.get("name", "")
        if request.query_params.get("library_delete") == "ok"
        else None
    )
    return templates.TemplateResponse(
        request, "library.html", {
            **side,
            "page": "library",
            "books": books,
            "total_bytes": sum(b["size_bytes"] for b in books),
            "upload_flashes": _upload_flashes(request),
            "delete_flash": delete_flash,
            "max_upload_mb": cfg.library_max_upload_mb,
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


@router.post("/intake/run")
def intake_run(bg: BackgroundTasks, request: Request) -> RedirectResponse:
    if intake.is_running.locked():
        return RedirectResponse(url="/?intake=already_running#intake", status_code=303)
    bg.add_task(intake.run_with_lock, request.app.state.cfg)
    return RedirectResponse(url="/?intake=ok#intake", status_code=303)


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


def _settings_redirect(flash: str, detail: str = "") -> RedirectResponse:
    qs = f"settings={flash}" + (f"&detail={quote(detail, safe='')}" if detail else "")
    return RedirectResponse(url=f"/?{qs}#settings", status_code=303)


@router.post("/settings")
def save_settings(
    request: Request,
    days: list[str] = Form([]),
    send_time: str = Form(..., alias="time"),
    weeks: str = Form("1"),
    start: str = Form("keep"),
    word_budget: str = Form(...),
) -> RedirectResponse:
    cfg = request.app.state.cfg
    try:
        day_tuple = schedule.parse_days(",".join(days))
    except ValueError:
        return _settings_redirect("invalid", "pick at least one day")
    try:
        hour, minute = schedule.parse_time(send_time)
        n_weeks = int(weeks)
        budget = int(word_budget.strip())
    except ValueError:
        return _settings_redirect("invalid", "check the time, weeks and word budget")
    if not 1 <= n_weeks <= schedule.WEEKS_MAX:
        return _settings_redirect("invalid", f"repeat must be 1–{schedule.WEEKS_MAX} weeks")
    if not WB_MIN <= budget <= WB_MAX:
        return _settings_redirect("invalid", f"word budget must be {WB_MIN}–{WB_MAX}")

    this_monday = schedule.monday(datetime.now(ZoneInfo(cfg.tz)).date())
    conn = store.connect(cfg.data_dir)
    try:
        current = schedule.load(conn)
        if n_weeks == 1 or start == "this":
            anchor = this_monday
        elif start == "next":
            anchor = this_monday + timedelta(days=7)
        else:
            anchor = current.anchor
        new = schedule.Schedule(day_tuple, hour, minute, n_weeks, anchor)
        schedule.save(conn, new)
        store.set_setting(conn, "word_budget", str(budget))
    finally:
        conn.close()
    request.app.state.scheduler.reschedule_job(schedule.JOB_ID, trigger=schedule.trigger(new, cfg.tz))
    log.info(f"settings saved: {schedule.describe(new)} (anchor {anchor}), word budget {budget}")
    return _settings_redirect("ok")


def _upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    upload.file.seek(0, 2)
    n = upload.file.tell()
    upload.file.seek(0)
    return n


@router.post("/library/upload")
async def library_upload(
    request: Request, files: list[UploadFile] = File(...)
) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    pairs: list[tuple[str, str]] = []
    try:
        for f in files:
            name = Path(f.filename or "").name
            if not name:
                pairs += [("library_upload", "invalid"), ("name", ""), ("reason", "missing filename")]
                continue
            size = _upload_size(f)
            ok, reason = library.validate_upload(conn, name, size, cfg.library_max_upload_mb)
            if not ok:
                kind = "conflict" if "exists" in reason else "invalid"
                pairs += [("library_upload", kind), ("name", name), ("reason", reason if kind == "invalid" else "")]
                continue
            try:
                f.file.seek(0)
                library.store_upload(conn, cfg.data_dir, f.file, name)
                pairs += [("library_upload", "ok"), ("name", name), ("reason", "")]
            except Exception as e:
                log.exception(f"upload failed for {name}: {e}")
                pairs += [("library_upload", "invalid"), ("name", name), ("reason", "store error")]
    finally:
        conn.close()
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)
    return RedirectResponse(url=f"/library?{qs}" if qs else "/library", status_code=303)


@router.post("/library/{book_id}/delete")
def library_delete(book_id: int, request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT filename FROM library_books WHERE id = ?", (book_id,)
        ).fetchone()
        name = row[0] if row else ""
        library.delete_book(conn, cfg.data_dir, book_id)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/library?library_delete=ok&name={quote(name, safe='')}", status_code=303
    )


def _sub_redirect(flash: str, detail: str = "") -> RedirectResponse:
    qs = f"sub={flash}" + (f"&detail={quote(detail, safe='')}" if detail else "")
    return RedirectResponse(url=f"/?{qs}#subscribers", status_code=303)


@router.post("/subscribers")
def subscriber_add(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    version: str = Form("full"),
    sender_name: str = Form(""),
    to_pool: str = Form(""),
) -> RedirectResponse:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email) or version not in sharing.SUBSCRIPTION_CHOICES:
        return _sub_redirect("invalid", email)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        added = store.add_subscriber(
            conn, email, name.strip(), version, to_pool=bool(to_pool), sender_name=sender_name.strip()
        )
    finally:
        conn.close()
    if not added:
        return _sub_redirect("exists", email)
    log.info(f"subscriber added: {email} ({version})")
    return _sub_redirect("added", email)


@router.post("/subscribers/{subscriber_id}/toggle")
def subscriber_toggle(subscriber_id: int, request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        store.toggle_subscriber(conn, subscriber_id)
    finally:
        conn.close()
    return RedirectResponse(url="/#subscribers", status_code=303)


@router.post("/subscribers/{subscriber_id}/version")
def subscriber_version(subscriber_id: int, request: Request, version: str = Form(...)) -> RedirectResponse:
    if version not in sharing.SUBSCRIPTION_CHOICES:
        return _sub_redirect("invalid", version)
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        sub = store.get_subscriber(conn, subscriber_id)
        if not sub:
            return _sub_redirect("invalid")
        store.set_subscriber_format(conn, subscriber_id, version)
    finally:
        conn.close()
    log.info(f"subscriber {sub['email']} version: {sub['format']} -> {version}")
    return _sub_redirect("version", f"{sub['email']} now gets {version}")


@router.post("/subscribers/{subscriber_id}/sender")
def subscriber_sender(subscriber_id: int, request: Request, sender_name: str = Form("")) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        sub = store.get_subscriber(conn, subscriber_id)
        if not sub:
            return _sub_redirect("invalid")
        store.set_subscriber_sender_name(conn, subscriber_id, sender_name.strip())
    finally:
        conn.close()
    log.info(f"subscriber {sub['email']} sender name: {sub['sender_name']!r} -> {sender_name.strip()!r}")
    return _sub_redirect("sender", f"{sub['email']} matched as \"{sender_name.strip()}\"" if sender_name.strip()
                         else f"{sub['email']} has no sender name, their link emails are ignored")


@router.post("/subscribers/{subscriber_id}/pool")
def subscriber_pool(subscriber_id: int, request: Request, to_pool: str = Form("")) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        sub = store.get_subscriber(conn, subscriber_id)
        if not sub:
            return _sub_redirect("invalid")
        store.set_subscriber_pool(conn, subscriber_id, bool(to_pool))
    finally:
        conn.close()
    log.info(f"subscriber {sub['email']} links to pool: {sub['to_pool']} -> {bool(to_pool)}")
    where = f"the pool ({cfg.reader_tag_trigger})" if to_pool else "your library only"
    return _sub_redirect("pool", f"links from {sub['email']} now go to {where}")


@router.post("/subscribers/{subscriber_id}/delete")
def subscriber_delete(subscriber_id: int, request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    conn = store.connect(cfg.data_dir)
    try:
        sub = store.get_subscriber(conn, subscriber_id)
        store.delete_subscriber(conn, subscriber_id)
    finally:
        conn.close()
    if sub:
        log.info(f"subscriber removed: {sub['email']}")
    return _sub_redirect("removed", sub["email"] if sub else "")


@router.post("/subscribers/{subscriber_id}/send-latest")
def subscriber_send_latest(subscriber_id: int, request: Request) -> RedirectResponse:
    cfg = request.app.state.cfg
    issue = sharing.latest_issue(cfg.data_dir)
    if not issue:
        return _sub_redirect("nothing")
    conn = store.connect(cfg.data_dir)
    try:
        sub = store.get_subscriber(conn, subscriber_id)
        if not sub:
            return _sub_redirect("invalid")
        errors = sharing.deliver(cfg, conn, issue, sub, manual=True)
    finally:
        conn.close()
    if errors:
        return _sub_redirect("send_failed", "; ".join(errors)[:300])
    return _sub_redirect("sent", sub["email"])


@router.get("/issues/{digest_id}/{fmt}", response_class=HTMLResponse)
def issue_preview(digest_id: int, fmt: str, request: Request):
    if fmt not in sharing.FORMATS:
        return PlainTextResponse("not found", status_code=404)
    cfg = request.app.state.cfg
    path = sharing.emails_dir(cfg.data_dir, digest_id) / f"{fmt}.html"
    if not path.exists():
        return PlainTextResponse("not found", status_code=404)
    return HTMLResponse(path.read_text())
