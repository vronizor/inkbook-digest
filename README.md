# inkbook-digest

Daily "morning paper" pipeline. Pulls Readwise Reader articles tagged `toepub`, builds a single EPUB, emails it to an Inkbook reader at 06:30 Europe/Paris.

See [SPEC.md](SPEC.md) for the full design.

## What it does, briefly

- Lists `category=article AND tag=toepub` from Readwise Reader (server-side filter).
- Drops anything already recorded in `data/digest.sqlite`.
- Skips articles that Reader hasn't finished parsing yet (no html content).
- Builds one EPUB: cover + TOC + chapters with embedded inline images. CSS is serif, justified, 1.5 line-height.
- SMTPs the EPUB to the Inkbook ingestion address.
- PATCHes Reader to add `sent-to-inkbook` to each successfully sent article.
- On failure, mails the captured log buffer to `ALERT_EMAIL`.

Idempotent: re-running with the same tagged articles will skip and exit empty.

## Reader API behaviour (verified)

Both unverified assumptions in SPEC.md were tested with a live token before the implementation went in:

- **`GET /api/v3/list/?tag=toepub` filters server-side.** Confirmed.
- **`PATCH /api/v3/update/{id}/`** accepts tag mutations with body shape `{"tags": ["name1", "name2", ...]}`. The dict-shape body that mirrors the response is rejected with 400. **PATCH replaces the tag list wholesale**, so to add a tag we send `existing_names + [new_tag]`.

## Run modes

```
python -m digest.main             # scheduler, daily at DIGEST_HOUR:DIGEST_MINUTE
python -m digest.main --once      # run a single digest immediately and exit
python -m digest.main --dry-run   # build EPUB, skip SMTP, skip Reader tag-add, skip SQLite recording
```

`--dry-run` only requires `READER_TOKEN`. The other env vars can be empty. Use it to iterate on EPUB rendering without sending mail.

## Local development

```
uv sync
export READER_TOKEN=...
export DATA_DIR=./data
uv run python -m digest.main --dry-run
```

The EPUB is written to `$DATA_DIR/morning-paper-YYYY-MM-DD.epub`. Open it in Calibre or another EPUB reader to verify rendering before pointing it at SMTP.

## Deployment (Raspberry Pi)

1. Create a Proton SMTP token paired with `digest@vinceth.net` (Settings → IMAP/SMTP → SMTP tokens). Save the token immediately — it's shown once.
2. Get a Reader access token from <https://readwise.io/access_token>.
3. Confirm the Inkbook ingestion email address.
4. Clone the repo to the Pi, copy `.env.example` to `.env`, fill in:
   ```
   READER_TOKEN=...
   SMTP_USER=...           # your Proton SMTP username
   SMTP_PASSWORD=...       # the Proton SMTP token
   SMTP_FROM=digest@vinceth.net
   INKBOOK_EMAIL=...       # Inkbook ingestion address
   ALERT_EMAIL=...         # where errors go
   ```
5. `docker compose up -d --build`.
6. `docker logs -f inkbook-digest` — should show `scheduler started, next run: ...`.

## Dry test

Tag one article in Reader as `toepub`, then on the Pi:

```
docker exec inkbook-digest python -m digest.main --once
```

Confirm:

- Email arrives at the Inkbook
- EPUB renders on device with serif font, justified text, working TOC
- Embedded images display
- Cover page renders
- `sent-to-inkbook` tag appears on the article in Reader

Then tag a second article and re-run: the first should be skipped (idempotency), only the second sent. Re-run a third time with no new tags: `empty run — no email`, exit 0.

If the dry test fails, run `--dry-run` mode and inspect the EPUB locally:

```
docker exec inkbook-digest python -m digest.main --dry-run
docker cp inkbook-digest:/data/morning-paper-$(date +%F).epub ./
```

## Error handling

| Condition | Inkbook email | Alert email | Exit |
|---|---|---|---|
| No tagged articles | no | no | 0 |
| All articles sent | yes | no | 0 |
| Partial failure (digest sent, some `sent-to-inkbook` tag-adds failed) | yes | yes | 0 |
| Total failure (no digest sent) | no | yes (if SMTP works) | 1 |

If Proton SMTP itself is broken, neither email reaches you. Fall back to `docker logs inkbook-digest`.

## Schema changes during dev

No migrations. Delete `data/digest.sqlite` and let `CREATE TABLE IF NOT EXISTS` rebuild it on next startup.

## Layout

```
src/digest/
  config.py    # env loading + validation (fails fast on missing vars)
  store.py     # SQLite schema + helpers
  reader.py    # Readwise Reader client (list + tag-add)
  epub.py      # cover, chapters, image embedding
  mailer.py    # SMTP send + alert send
  main.py      # scheduler, --once / --dry-run, end-to-end run
```
