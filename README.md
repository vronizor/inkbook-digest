# inkbook-digest

A "morning paper" pipeline for Readwise Reader → Inkbook. Articles tagged `toepub` form a queue. Each daily run randomly picks from the queue until a soft word-budget is exceeded, ships the result as an EPUB by email at 06:30 Europe/Paris, and tags the sent articles in Reader. A status dashboard at `http://<pi>:8080/` shows queue stats, history, runs, and a 30-day chart, with a Trigger button for on-demand runs and a Pause toggle that blocks scheduled runs.

See [SPEC.md](SPEC.md) and [SPEC_V2.md](SPEC_V2.md) for the full design.

## What it does

1. Fetch all `toepub`-tagged `category=article` documents from Reader.
2. Drop anything in `sent_articles` (SQLite) — Reader stays SOT, no local queue table.
3. Skip articles Reader hasn't finished parsing yet (no html content).
4. Shuffle randomly, then accumulate word counts. Add the article that pushes total past `WORD_BUDGET`, then stop (stop-when-exceeded).
5. Build a single EPUB: cover + TOC + chapters with embedded inline images. CSS is serif, 1.5 line-height, justified.
6. SMTP the EPUB to `INKBOOK_EMAIL`.
7. PATCH Reader to add `sent-to-inkbook` to each sent article.
8. Persist digest + per-article rows in SQLite.
9. On failure, mail the captured log buffer to `ALERT_EMAIL`.

If the queue is empty, log empty run, no email.

Same-day re-runs produce versioned volumes: `morning-paper-YYYY-MM-DD.epub` (Vol 1), then `-vol-2.epub`, etc., so the Inkbook doesn't overwrite. Empty/failed runs do not bump the volume.

## Reader API behaviour (verified)

- `GET /api/v3/list/?tag=toepub` **filters server-side** (confirmed live).
- `PATCH /api/v3/update/{id}/` accepts `{"tags": ["name1", ...]}`. The dict-shape body is rejected with 400. **PATCH replaces the tag list wholesale**, so we send `existing_names + [new_tag]`.

## Run modes

```bash
uvicorn digest.main:app --host 0.0.0.0 --port 8080  # server: scheduler + dashboard
python -m digest.main --once                         # CLI: one digest now, exit
python -m digest.main --dry-run                      # CLI: build EPUB only, no SMTP / tag / DB write
python -m digest.main --once --manual                # CLI: bypass paused setting
```

In **server mode** (the container default) FastAPI runs the dashboard, plus an APScheduler `BackgroundScheduler` that fires the daily run at `DIGEST_HOUR:DIGEST_MINUTE`. Same `is_running` `threading.Lock` gates scheduled and triggered runs to prevent overlap.

In **CLI mode**, `--dry-run` only requires `READER_TOKEN`.

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

INKBOOK_EMAIL=                      # Inkbook ingestion address
ALERT_EMAIL=                        # error notifications

DIGEST_HOUR=6
DIGEST_MINUTE=30
TZ=Europe/Paris

WORD_BUDGET=5000                    # initial soft-cap; settings table is SOT after first startup
IMAGE_SOFT_CAP_MB=10                # per-article warning threshold; still send
DATA_DIR=/data
LOG_LEVEL=INFO
```

**Word budget precedence**: `WORD_BUDGET` seeds the `settings` table on first startup. After that, the dashboard input edits `settings.word_budget` directly and the env var is ignored. Edit via `http://<pi>:8080/` → Settings → Daily word budget. Valid range: 500 – 50,000.

## Dashboard

Tailscale-only, no auth. After `docker compose up -d`, browse to `http://<pi>:8080/`.

```
┌─────────────────────────────────────────────────┐
│  Morning Paper             [Pause]  [Trigger]   │
├─────────────────────────────────────────────────┤
│  Status                                         │
│   Last digest    2026-04-29 06:30  Vol 1  5,400w│
│   Next run       2026-04-30 06:30:00            │
│   Queue          12 articles, ~38,500 words     │
│   Total sent     127 articles, 312,000 words…   │
├─────────────────────────────────────────────────┤
│  Articles per day, last 30 days  [bar chart]    │
├─────────────────────────────────────────────────┤
│  Recent digests   (last 30, table)              │
│  Recent runs      (last 30, expandable logs)    │
│  Recently sent articles  (last 30, table)       │
├─────────────────────────────────────────────────┤
│  Settings (rarely needed)                       │
│   Daily word budget  [ 5000 ]  [Save]           │
│   Soft cap; the digest stops after the article  │
│   that pushes the total above this number.      │
└─────────────────────────────────────────────────┘
```

**Trigger** posts to `/trigger`, fires a digest run via `BackgroundTasks`. If a run is already in progress, the redirect carries `?triggered=already_running` and a flash banner appears.

**Pause** toggles `settings.paused`. While paused, the scheduled run logs "paused — skipping" and exits without sending mail. The Trigger button still works (manual override). The button label flips to `Resume` and the "Next run" line shows muted `paused`.

**Queue** is computed live from Reader on each `GET /` (60 s in-memory TTL). On Reader API failure, renders `unavailable` in muted red without 500-ing the page.

**Logs**: each row in the runs table has a `▸` toggle that expands the captured log buffer (capped at 20 KB; `/runs/{id}/log` exposes the full text as plain text).

**Health**: `GET /healthz` → `{"ok": true}`. Doesn't touch SQLite or Reader, stays green even when those don't.

## Deployment (Raspberry Pi)

1. Create a Proton SMTP token paired with `digest@vinceth.net` (Settings → IMAP/SMTP → SMTP tokens). Save it immediately.
2. Get a Reader token from <https://readwise.io/access_token>.
3. Confirm the Inkbook ingestion email address.
4. Clone the repo, `cp .env.example .env`, fill in all values.
5. `docker compose up -d --build`
6. `docker logs -f inkbook-digest` → expect `scheduler started, next run: ...`.
7. Browse `http://<pi-host>:8080/` (over Tailscale) — dashboard should load.

## Dry test

Tag a fresh article in Reader as `toepub`, then either:

- Click **Trigger** on the dashboard, or
- `docker exec inkbook-digest python -m digest.main --once`

Verify:

- Email arrives at the Inkbook with the correct EPUB
- EPUB renders on device (serif, justified, working TOC, embedded images)
- `sent-to-inkbook` tag appears on the article in Reader
- The article shows up in the dashboard's recent-articles table

For multi-article testing: tag 6+ articles totaling >5000 words; expect 3–5 selected (random shuffle + stop-when-exceeded). Re-run: only the unsent ones are eligible.

To inspect SQLite directly inside the container:

```bash
docker exec inkbook-digest sqlite3 /data/digest.sqlite \
  "SELECT sent_at, title, url, word_count FROM sent_articles ORDER BY sent_at DESC LIMIT 10;"
```

## Error handling

| Condition | Inkbook email | Alert email | Run outcome |
|---|---|---|---|
| Empty queue | no | no | `empty` |
| Paused (scheduled) | no | no | `paused` |
| Paused + manual trigger | yes (if articles eligible) | no | `ok` |
| All sends ok | yes | no | `ok` |
| Partial failure (digest sent, some tag PATCHes failed) | yes | yes | `error` |
| Total failure (no digest sent) | no | yes (if SMTP up) | `error` |

If Proton SMTP itself is broken, neither email reaches you. Fall back to `docker logs inkbook-digest`.

## Schema changes during dev

Schema is idempotent: `CREATE TABLE IF NOT EXISTS` + additive `ALTER TABLE` with "duplicate column" tolerated. To start clean: `rm data/digest.sqlite` and let startup rebuild it.

## Layout

```
src/digest/
  config.py            # env loading + validation, fails fast on missing vars
  store.py             # SQLite schema, WAL, settings, volume + word_count helpers
  reader.py            # Readwise Reader client (list_queue, add_tag, word_count)
  epub.py              # cover, chapters, image embedding (volume-aware)
  mailer.py            # SMTP send + alert send
  main.py              # FastAPI app, lifespan-managed scheduler, run logic, CLI entry
  dashboard.py         # FastAPI routes (/, /trigger, /pause, /word-budget, /runs/{id}/log)
  templates/index.html # Single-page dashboard (Jinja)
  static/style.css     # Dashboard styles
```
