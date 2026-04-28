# inkbook-digest — Specification

A daily "morning paper" pipeline: pulls articles tagged `toepub` from Readwise Reader, builds one EPUB per day, emails it to an Inkbook e-reader.

## Goal

Deliver a single EPUB digest by email at 06:30 Europe/Paris each day, containing all Reader articles tagged `toepub` since the last digest. Idempotent. Self-contained. Runs as a Docker container on a Raspberry Pi 5.

## Non-goals

- Not a Reader replacement, not an archive, not a web UI.
- No mobile app, no browser extension, no OPDS feed.
- No annotations sync, no highlights export.
- Not multi-user.

## Architecture

```
06:30 daily (APScheduler in container):
  1. Reader API: list docs where tag=toepub AND category=article
  2. Filter against SQLite: drop already-sent reader_document_ids
  3. For each new article:
     - Reader API fetch with withHtmlContent=true
     - Download inline images (with size cap), rewrite <img> srcs
     - Build EpubHtml chapter
  4. Assemble EpubBook: cover + TOC + chapters + CSS
  5. SMTP send to Inkbook with EPUB attached
  6. For each successfully sent article: PATCH Reader to add tag sent-to-inkbook
  7. Persist to SQLite (digest row + sent_articles rows + run row)
  8. On any error: send error email to ALERT_EMAIL with captured log buffer
  9. If no tagged articles: log empty run, no email, exit 0
```

## Stack

- Python 3.12, no framework
- `httpx` — Reader API
- `ebooklib` — EPUB generation
- `apscheduler` — in-process daily scheduler
- `pillow` — image processing/cover generation
- `sqlite3`, `smtplib`, `email`, `logging` — stdlib
- `uv` for dependency management
- Docker, single container, `python:3.12-slim` base

Target ≤300 lines of Python total.

## Project layout

```
inkbook-digest/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
├── src/digest/
│   ├── __init__.py
│   ├── main.py        # entry point, scheduler, --once flag
│   ├── reader.py      # Reader API client
│   ├── epub.py        # EPUB construction
│   ├── mailer.py      # SMTP send + error notifications
│   ├── store.py       # SQLite layer
│   └── config.py      # env var loading + validation
├── tests/
│   └── test_smoke.py
└── data/              # mounted volume (gitignored)
```

## Configuration (env vars)

```
READER_TOKEN=                       # https://readwise.io/access_token
READER_TAG_TRIGGER=toepub
READER_TAG_DONE=sent-to-inkbook

SMTP_HOST=smtp.protonmail.ch
SMTP_PORT=587
SMTP_USER=                          # generated with the Proton SMTP token
SMTP_PASSWORD=                      # the Proton SMTP token (one-time view)
SMTP_FROM=digest@vinceth.net

INKBOOK_EMAIL=                      # the Inkbook ingestion address
ALERT_EMAIL=                        # where errors go (likely vincent@vinceth.net)

DIGEST_HOUR=6
DIGEST_MINUTE=30
TZ=Europe/Paris

IMAGE_SOFT_CAP_MB=10                # per-article warning threshold; still send
DATA_DIR=/data
LOG_LEVEL=INFO
```

`config.py` validates all required vars at startup, fails fast with a clear message naming the missing var.

## SQLite schema

```sql
CREATE TABLE digests (
  id INTEGER PRIMARY KEY,
  sent_at TEXT NOT NULL,            -- ISO 8601, UTC
  article_count INTEGER NOT NULL,
  status TEXT NOT NULL              -- 'sent' | 'failed' | 'empty'
);

CREATE TABLE sent_articles (
  reader_document_id TEXT PRIMARY KEY,
  digest_id INTEGER NOT NULL REFERENCES digests(id),
  title TEXT,
  url TEXT,
  sent_at TEXT NOT NULL
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,                     -- 'ok' | 'error' | 'empty'
  log TEXT                          -- captured log buffer
);
```

`runs.log` is what gets emailed on failure. Pruned to last 30 days on each run.

## EPUB structure

- **Filename**: `morning-paper-{YYYY-MM-DD}.epub`
- **Metadata**: title `Morning Paper {YYYY-MM-DD}`, author `Readwise Reader Digest`, language `en`
- **Cover**: auto-generated PNG from PIL. Layout: "Morning Paper" + ISO date + article count + serif. White background. ~600×900px.
- **TOC**: standard EPUB nav, one entry per article, format `{title} — {source_domain}`
- **Per-article chapter**:
  - `<h1>` title
  - `<p class="meta">` author · source_domain · publish_date · word_count
  - `<a class="source">` original URL
  - article body HTML from Reader
- **CSS** (~30 lines): serif body, 1.5 line-height, justified, modest margins, no embedded fonts
- **Images**: downloaded at fetch time, embedded as `EpubImage`, `<img>` srcs rewritten to internal refs. If a single article exceeds `IMAGE_SOFT_CAP_MB`, log a warning but still include all images.

## Reader API details

Endpoints used:

- `GET /api/v3/list/?category=article&tag=toepub&withHtmlContent=true&pageCursor=...`
- `PATCH /api/v3/update/{document_id}/` to add `sent-to-inkbook` tag

**Verify before coding**: confirm the v3 update endpoint accepts tag mutations. The docs are thin here. If tag-add is not supported, fallback: track only in SQLite, skip the `sent-to-inkbook` tag entirely. Document the decision in code comments and README.

Rate limits: 20 req/min on most endpoints, 50 req/min on list/detail. Well under our usage.

Reader server-side `tag` filter behaviour is unverified. If it doesn't work, filter client-side on the `tags` array. Either way, write the client to be tolerant.

## SMTP

Proton SMTP submission, paired with `digest@vinceth.net`:
- Host: `smtp.protonmail.ch`, port 587, STARTTLS
- Username + token created via Proton Settings → IMAP/SMTP → SMTP tokens
- Token is shown once at creation; user has saved it

## Error handling

In-process `logging` configured to write to both stdout and a `StringIO` buffer. End-of-run logic:

| Condition | Inkbook email | Alert email | Exit |
|---|---|---|---|
| No tagged articles | no | no | 0 |
| All articles sent | yes | no | 0 |
| Partial failure (some articles errored, digest sent) | yes (with successes) | yes (with errors) | 0 |
| Total failure (no digest sent) | no | yes (if SMTP works) | 1 |

Alert email: subject `[inkbook-digest] failure on {YYYY-MM-DD}`, body = full log buffer, plain text, sent via the same SMTP credentials.

**Known blind spot**: if Proton SMTP itself is broken, neither email reaches Vincent. The fix in that case is `docker logs inkbook-digest`. Acceptable for v1.

Edge case to handle gracefully: an article tagged `toepub` but still being parsed by Reader (no html content yet). Skip it, do not error, retry next day.

## Docker

`docker-compose.yml`:
```yaml
services:
  inkbook-digest:
    build: .
    container_name: inkbook-digest
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data
    environment:
      - DATA_DIR=/data
```

`Dockerfile`: python:3.12-slim, copy source, install via `uv pip install --system`, `CMD ["python", "-m", "digest.main"]`.

The script runs APScheduler in foreground. Container stays up. `--once` flag for manual invocation: `docker exec inkbook-digest python -m digest.main --once`.

Resource footprint expected: ~50MB RAM idle, brief spike during digest build. Trivial on Pi 5.

## Deployment steps

1. Create Proton SMTP token paired with `digest@vinceth.net`. Save token immediately (one-time view).
2. Get Reader access token from `readwise.io/access_token`.
3. Confirm Inkbook ingestion email address.
4. Clone repo to Pi, copy `.env.example` to `.env`, fill in values.
5. `docker compose up -d`.
6. Verify: `docker logs -f inkbook-digest` shows scheduler started with next run time.
7. **Dry test**: tag one article in Reader as `toepub`, run `docker exec inkbook-digest python -m digest.main --once`. Confirm:
   - Email arrives at Inkbook
   - EPUB renders on device with correct fonts, justified text, working TOC
   - Embedded images display
   - Cover page renders
   - `sent-to-inkbook` tag appears in Reader (or skip-if-API-doesn't-support)
8. Tag 3+ articles, run again, verify multi-article digest.
9. Run `--once` a third time with the same articles still tagged: should detect already-sent and exit empty.
10. Leave scheduler to fire at 06:30 next morning.

## Manual testing checklist

- [ ] Single-article digest renders correctly on Inkbook
- [ ] Multi-article digest with TOC navigation works
- [ ] Embedded images display
- [ ] Idempotency: re-running with same tagged articles does not re-send
- [ ] `sent-to-inkbook` tag added in Reader (or fallback documented)
- [ ] Article still being parsed by Reader: skipped without error
- [ ] Error path: temporarily break SMTP_PASSWORD, confirm error email *would* fire (will fail to send, check stdout)
- [ ] Empty run (no `toepub` tags): no email sent, log shows empty run
- [ ] Image-heavy article (>10MB): warning logged, article still included
- [ ] Container survives Pi reboot (`restart: unless-stopped`)

## Decisions taken (for context)

- **Trigger**: tag `toepub` in Reader, category=article only
- **Cadence**: daily digest at 06:30 Europe/Paris
- **Cleanup**: add `sent-to-inkbook` tag in Reader, leave everything else unchanged
- **Time horizon**: none — anything tagged that hasn't been sent gets sent
- **SMTP**: Proton SMTP submission via `digest@vinceth.net`
- **Errors**: stdout + email alert via same SMTP
- **Image handling**: 10MB soft cap per article (warn, still send)
- **Language**: hardcoded `en` in EPUB metadata

## Known unknowns (verify during build)

1. Does Reader's `tag` query parameter on `/api/v3/list/` actually filter server-side? Test, fall back to client-side filter if not.
2. Does `PATCH /api/v3/update/{id}/` accept tag additions? Test, fall back to SQLite-only tracking if not.
3. Does the Inkbook stock reader handle EPUBs with embedded images well, or does it choke on certain image formats / sizes? Verify with the dry test.
4. Does Reader's `withHtmlContent=true` reliably return clean HTML for archive.is URLs? Not blocking — if it fails, we'll see it on a specific article and decide what to do then.
