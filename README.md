# inkbook-digest

A self-hosted reader pipeline. Two things in one container:

1. **Morning Paper** — articles you tag `toepub` in Readwise Reader form a queue. Each daily run randomly picks from it until a soft word-budget is exceeded, builds an EPUB, and tags the sent articles in Reader.
2. **Library** — manually-uploaded EPUBs and PDFs through the dashboard.

Both are exposed as **OPDS catalogs** so KOReader on the Inkbook (or any OPDS-aware reader) can subscribe and pull books over the network. The original SMTP/Inkbook-email path is gone — KOReader + OPDS replaces it.

A status dashboard at `http://<pi>:8080/` shows queue stats, history, runs, a 30-day chart, the library list, an upload form, and the OPDS feed URL. Trigger button for on-demand digest runs, Pause toggle blocks scheduled runs.

See [SPEC.md](SPEC.md), [SPEC_V2.md](SPEC_V2.md), and [SPEC_V3.md](SPEC_V3.md) for the full design.

## What it does

### Daily digest (Morning Paper)

1. Fetch all `toepub`-tagged `category=article` documents from Reader.
2. Drop anything in `sent_articles` (SQLite) — Reader stays SOT.
3. Skip articles Reader hasn't finished parsing yet.
4. Shuffle randomly, accumulate word counts. Add the article that pushes total past `WORD_BUDGET`, then stop.
5. Build a single EPUB (cover + TOC + chapters with embedded images, serif/justified CSS) into `$DATA_DIR/epubs/`.
6. PATCH Reader to add `sent-to-inkbook` to each sent article.
7. Persist digest + per-article rows in SQLite.
8. The new digest appears in the OPDS `Morning Papers` feed within seconds.

If the queue is empty, log empty run, no alert. Same-day re-runs produce versioned volumes (`-vol-2.epub`, etc.).

EPUBs are kept in `$DATA_DIR/epubs/` for 30 days, then pruned at startup.

### Library

- Drag-and-drop EPUB or PDF files onto the dashboard.
- EPUB metadata (title, author, language) is extracted via ebooklib; cover thumbnail extracted, resized to ≤600px, saved as JPEG.
- PDFs get a generic placeholder cover.
- 200MB upload cap (env-configurable).
- Duplicate filename: rejected.
- Books appear in the OPDS `Library` feed, recently-added first.
- Delete via the dashboard with a confirmation dialog.

### OPDS

| Path | Returns |
|---|---|
| `/opds/` | Navigation feed listing the two catalogs |
| `/opds/digests/` | Last 30 days of `status='sent'` digests |
| `/opds/library/` | All library books, recently-added first |
| `/opds/file/digest/{id}` | EPUB download |
| `/opds/file/library/{id}` | EPUB or PDF download |
| `/opds/cover/{type}/{id}` | Cover thumbnail (placeholder for PDFs / missing covers) |

### Adding the OPDS catalog to KOReader (Inkbook)

1. Open KOReader → File browser → top menu → **Cloud storage** → **OPDS catalog**.
2. **Add** a new catalog. URL: `<BASE_URL>/opds/` (e.g. `http://pi.tailnet.ts.net:8080/opds/`).
3. Leave username/password blank (no auth).
4. Save. Open the catalog: you'll see **Morning Papers** and **Library**.

Tap into either, tap a book to download, tap again to read.

## Run modes

```bash
uvicorn digest.main:app --host 0.0.0.0 --port 8080  # server: scheduler + dashboard + OPDS
python -m digest.main --once                         # CLI: one digest run, exit
python -m digest.main --dry-run                      # CLI: build EPUB only, no Reader tag / DB write
python -m digest.main --once --manual                # CLI: bypass paused setting
```

In **server mode** (the container default) FastAPI runs the dashboard, the OPDS endpoints, and an APScheduler `BackgroundScheduler` that fires the daily run at `DIGEST_HOUR:DIGEST_MINUTE`. A `threading.Lock` gates scheduled and triggered runs.

## Reader API behaviour (verified)

- `GET /api/v3/list/?tag=toepub` filters server-side.
- `PATCH /api/v3/update/{id}/` accepts `{"tags": ["name1", ...]}`. PATCH replaces the tag list wholesale, so we send `existing_names + [new_tag]`.

## Configuration (env vars)

```
READER_TOKEN=                       # https://readwise.io/access_token
READER_TAG_TRIGGER=toepub
READER_TAG_DONE=sent-to-inkbook

SMTP_HOST=smtp.protonmail.ch
SMTP_PORT=587
SMTP_USER=                          # Proton SMTP username
SMTP_PASSWORD=                      # Proton SMTP token (one-time view)
SMTP_FROM=digest@vinceth.net
ALERT_EMAIL=                        # error notifications

DIGEST_HOUR=6
DIGEST_MINUTE=30
TZ=Europe/Paris

WORD_BUDGET=5000                    # initial soft-cap; settings table is SOT after first startup
IMAGE_SOFT_CAP_MB=10                # per-article warning threshold; still send
DATA_DIR=/data
LOG_LEVEL=INFO

LIBRARY_MAX_UPLOAD_MB=200           # per-file upload cap for the library
BASE_URL=                           # optional; the publicly-reachable host, e.g. http://pi.tailnet.ts.net:8080
                                    # used in OPDS feeds for absolute URLs and the dashboard hint
```

**SMTP is now alerts-only.** No `INKBOOK_EMAIL` — books are pulled by KOReader via OPDS, not pushed by email. SMTP creds remain so we can still mail you on digest failures.

**`BASE_URL` matters for OPDS.** KOReader needs absolute URLs in feed entries. Set `BASE_URL` to the Tailscale hostname (or whatever the device hits) so links resolve cleanly across devices. If unset, the request host is used, which may not be reachable from the device that fetched the feed.

**Word budget precedence**: `WORD_BUDGET` seeds the `settings` table on first startup. After that, the dashboard input edits `settings.word_budget` directly and the env var is ignored. Edit via dashboard → Settings → Daily word budget. Valid range: 500 – 50,000.

## Dashboard

Tailscale-only, no auth. After `docker compose up -d`, browse to `http://<pi>:8080/`.

```
┌───────────────────────────────────────────────────────┐
│  E-books & Morning Paper        [Pause]   [Trigger]   │
│  OPDS feed: http://<pi>:8080/opds/                    │
├───────────────────────────────────────────────────────┤
│  Library                                  [Upload]    │
│   [📕] book1.epub                                      │
│        Author · EPUB · 412 KB · 2026-04-12       [×]  │
│   [📄] thesis.pdf                                      │
│        Unknown · PDF · 4.2 MB · 2026-04-10       [×]  │
├───────────────────────────────────────────────────────┤
│  Status                                               │
│   Last digest    2026-04-29 06:30  Vol 1  5,400w     │
│   Next run       2026-04-30 06:30:00                  │
│   Queue          12 articles, ~38,500 words           │
│   Total sent     127 articles, 312,000 words…         │
├───────────────────────────────────────────────────────┤
│  Articles per day, last 30 days  [bar chart]          │
├───────────────────────────────────────────────────────┤
│  Recent digests   (last 30, with ↓ epub link)         │
│  Recent runs      (last 30, expandable logs)          │
│  Recently sent articles                               │
├───────────────────────────────────────────────────────┤
│  Settings (rarely needed)                             │
│   Daily word budget  [ 5000 ]  [Save]                 │
└───────────────────────────────────────────────────────┘
```

**Upload**: file picker accepts `.epub` and `.pdf`, multiple at once. Each file flashes its result individually (uploaded / conflict / rejected).

**Trigger** posts to `/trigger`, fires a digest run via `BackgroundTasks`. If a run is in progress, the redirect carries `?triggered=already_running`.

**Pause** toggles `settings.paused`. While paused, the scheduled run logs "paused — skipping" and exits. The Trigger button still works (manual override).

**Queue** is computed live from Reader on each `GET /` (60 s in-memory TTL). On Reader API failure, renders `unavailable` in muted red.

**Health**: `GET /healthz` → `{"ok": true}`. Doesn't touch SQLite or Reader.

## Deployment (Raspberry Pi)

1. Create a Proton SMTP token paired with `digest@vinceth.net` (Settings → IMAP/SMTP → SMTP tokens). Save it immediately.
2. Get a Reader token from <https://readwise.io/access_token>.
3. Clone the repo, `cp .env.example .env`, fill in values. Set `BASE_URL` to the Tailscale hostname (e.g. `http://pi.tailnet.ts.net:8080`).
4. `docker compose up -d --build`
5. `docker logs -f inkbook-digest` → expect `scheduler started, next run: ...`.
6. Browse `http://<pi-host>:8080/` (over Tailscale) — dashboard should load.
7. On the Inkbook, add the OPDS catalog URL to KOReader (see above).

## Dry test

Tag a fresh article in Reader as `toepub`, then either:

- Click **Trigger** on the dashboard, or
- `docker exec inkbook-digest python -m digest.main --once`

Verify:

- The new digest appears in the dashboard's recent digests table with a `↓ epub` link.
- The OPDS `Morning Papers` feed lists it (`http://<pi>:8080/opds/digests/`).
- Tap it in KOReader on the Inkbook, downloads and opens.
- `sent-to-inkbook` tag appears on the article in Reader.

For multi-article testing: tag 6+ articles totaling >5000 words; expect 3–5 selected (random shuffle + stop-when-exceeded).

To inspect SQLite directly inside the container:

```bash
docker exec inkbook-digest sqlite3 /data/digest.sqlite \
  "SELECT sent_at, title, url, word_count FROM sent_articles ORDER BY sent_at DESC LIMIT 10;"
```

## Error handling

| Condition | Alert email | Run outcome |
|---|---|---|
| Empty queue | no | `empty` |
| Paused (scheduled) | no | `paused` |
| Paused + manual trigger | no (if articles eligible) | `ok` |
| All sends ok | no | `ok` |
| Partial failure (some Reader tag PATCHes failed) | yes | `error` |
| Total failure (no digest produced) | yes (if SMTP up) | `error` |

If Proton SMTP is broken, no alert reaches you. Fall back to `docker logs inkbook-digest`.

## Schema changes during dev

Schema is idempotent: `CREATE TABLE IF NOT EXISTS` + additive `ALTER TABLE` with "duplicate column" tolerated. To start clean: stop the container, `rm data/digest.sqlite` (also `rm -rf data/library data/epubs` if you want a full reset), restart.

## Layout

```
src/digest/
  config.py            # env loading + validation
  store.py             # SQLite schema, WAL, settings, volume + word_count helpers, EPUB path/prune
  reader.py            # Readwise Reader client (list_queue, add_tag, word_count)
  epub.py              # cover, chapters, image embedding (volume-aware)
  library.py           # upload, delete, EPUB metadata + cover extraction
  mailer.py            # alert email (digest delivery removed)
  opds.py              # OPDS root, digests, library, file + cover endpoints
  main.py              # FastAPI app, lifespan-managed scheduler, run logic, CLI entry
  dashboard.py         # FastAPI routes (/, /trigger, /pause, /word-budget, /library/*, /digests/{id}/epub)
  templates/
    index.html         # Dashboard (Jinja)
    opds_navigation.xml
    opds_acquisition.xml
  static/
    style.css
    placeholder-epub.png  # generated at startup if missing
    placeholder-pdf.png   # generated at startup if missing
```
