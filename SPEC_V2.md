# inkbook-digest — v2 spec (queue, pause, dashboard)

Supersedes `SPEC_DASHBOARD.md`. Extends `SPEC.md` and `CLAUDE.md`, both of which still apply.

## Goal

Transform the digest from a daily flush into a paced trickle:

- Articles tagged `toepub` form a queue (Reader is the source of truth).
- Each digest run randomly selects from the queue until a soft word-count cap is exceeded.
- A pause toggle on the dashboard blocks scheduled runs (manual trigger still works).
- A status dashboard at `http://<pi>:8080/` shows queue stats, history, runs, and a 30-day chart.
- Multiple same-day digests are versioned with Vol. N numbering.

## Non-goals

- No FIFO/LIFO/priority queue rules — selection is purely random.
- No bandwidth slider, no multi-level pause, no pause-until-date.
- No local queue table — Reader stays SOT.
- No queue ordering, "up next" preview, or per-article controls in the dashboard.
- No auth (Tailscale-only).

## Behavioral model

The queue is a **pool**, not a line. At digest time:

1. Fetch all `toepub`-tagged articles from Reader (re-check tags fresh).
2. Drop any whose `reader_document_id` is already in `sent_articles`.
3. Shuffle randomly.
4. Iterate, accumulating word counts. Add the article that pushes total over `WORD_BUDGET` (stop-when-exceeded), then stop.
5. Send digest.

If queue is empty, log empty run, no email, exit 0.

## Pause semantics

- `paused = True` → scheduled runs at 06:30 do nothing (log + exit).
- `paused = True` + manual trigger via dashboard → run executes normally.
- Toggle is sticky (persisted in SQLite); survives container restarts.

## Volume numbering

Unchanged from `SPEC_DASHBOARD.md`:

- Vol. 1: `morning-paper-{YYYY-MM-DD}.epub`, title `Morning Paper {YYYY-MM-DD}`
- Vol. 2+: `morning-paper-{YYYY-MM-DD}-vol-{N}.epub`, title `Morning Paper {YYYY-MM-DD} (Vol. {N})`
- Volume number = (count of `digests` rows on same calendar day where `status = 'sent'`) + 1.
- Failed and empty runs do not count.

## Word counts and budget

Reader's API returns `word_count` per document. Use it directly. If missing or zero on a given article, fall back to a quick `len(text.split())` on the parsed HTML body.

**Where word counts come from at each stage**:
- **Digest build (queue selection)**: live from Reader API, used immediately for the soft cap, never stored as a queue cache.
- **Dashboard queue stats**: live from Reader API, summed for display, cached in-memory for 60s only (TTL dict, not SQLite).
- **Dashboard chart and totals**: read from `sent_articles.word_count`.

`sent_articles.word_count` is written **only when an article is sent**, as a historical record. Do not create a local table or SQLite cache of pending-article word counts. Reader stays SOT for everything pre-send.

**Word budget lookup**: at digest run time, read the current word budget from `settings` table (`store.get_setting("word_budget")`), parse to int. If somehow missing or unparseable, fall back to the env var, log a warning. Never read the env var directly from the digest run path otherwise.

## Configuration additions

```
WORD_BUDGET=5000          # soft cap, stop-when-exceeded; initial value only
```

All other env vars from `SPEC.md` unchanged.

**Word budget precedence**: the env var is the *initial* value, written into the `settings` table on first startup if no `word_budget` row exists. After that, the settings table is the single source of truth and the env var is ignored. This avoids the "which one wins" ambiguity. The dashboard input edits the settings table directly.

## SQLite schema changes

Existing tables get one additive change:

```sql
ALTER TABLE digests ADD COLUMN volume INTEGER NOT NULL DEFAULT 1;
ALTER TABLE digests ADD COLUMN total_words INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sent_articles ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0;
```

New table:

```sql
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- Initial rows on first startup:
--   ('paused', 'false', <now>)
--   ('word_budget', <value of WORD_BUDGET env var>, <now>)
```

WAL mode enabled at connection setup: `PRAGMA journal_mode=WAL;`.

Use `IF NOT EXISTS` patterns and tolerate ALTERs that fail because the column already exists (catch and log). No alembic.

## Stack additions

- `fastapi`
- `uvicorn[standard]`
- `jinja2`

Chart.js loaded from CDN. Tailscale-only deployment so CDN reach is fine.

## Process model

`main.py` becomes the FastAPI entry point. APScheduler runs as a `BackgroundScheduler` started in the FastAPI lifespan startup hook, stopped in shutdown. Single process, single SQLite file in WAL mode for safe concurrent reads + writes.

Container CMD: `uvicorn digest.main:app --host 0.0.0.0 --port 8080`.

A `threading.Lock` (`is_running`) prevents overlapping digest runs (scheduled vs manual trigger).

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/runs/{run_id}/log` | Plain-text log buffer for a single run |
| POST | `/trigger` | Fire on-demand digest run, redirect to `/?triggered=...` |
| POST | `/pause` | Toggle paused setting, redirect to `/` |
| POST | `/word-budget` | Update word budget, redirect to `/?budget=ok\|invalid` |
| GET | `/healthz` | Returns `{"ok": true}` |

`POST /trigger`:
- If `is_running` lock held → 303 redirect with `?triggered=already_running`.
- Otherwise → enqueue via FastAPI `BackgroundTasks`, 303 redirect with `?triggered=ok`.

`POST /pause`:
- Toggle the `paused` setting in SQLite, 303 redirect to `/`.

`POST /word-budget`:
- Accepts a single form field `value` (positive integer, 500 ≤ N ≤ 50000).
- On valid input: write to `settings`, 303 redirect to `/?budget=ok`.
- On invalid input: do not write, 303 redirect to `/?budget=invalid`.

No CSRF, no auth (Tailscale-only).

## Dashboard layout

```
┌─────────────────────────────────────────────────┐
│  Morning Paper             [Pause]   [Trigger]  │
├─────────────────────────────────────────────────┤
│  Status                                         │
│   Last digest    2026-04-29 06:30  Vol 1  ~5400 words │
│   Next run       2026-04-30 06:30 (or "paused")  │
│   Queue          12 articles, ~38,500 words     │
│   Total sent     127 articles across 42 digests │
├─────────────────────────────────────────────────┤
│  Articles per day, last 30 days                 │
│  [bar chart]                                    │
├─────────────────────────────────────────────────┤
│  Recent digests                                 │
│   Date         Vol  Articles  Words  Status     │
│   2026-04-29   1    3         5,400  sent       │
│   2026-04-28   1    2         3,800  sent       │
│   2026-04-27   1    0         0      empty      │
├─────────────────────────────────────────────────┤
│  Recent runs                                    │
│   Started               Outcome  Duration  Log  │
│   2026-04-29 06:30:01   ok       2.3s      ▸   │
│   2026-04-28 06:30:01   error    0.8s      ▸   │
├─────────────────────────────────────────────────┤
│  Recently sent articles                         │
│   Title    Source       Words   Sent            │
├─────────────────────────────────────────────────┤
│  Settings (rarely needed)                       │
│   Daily word budget  [ 5000 ]  [Save]           │
│   Soft cap; the digest stops after the article  │
│   that pushes the total above this number.      │
└─────────────────────────────────────────────────┘
```

Pause button label is dynamic: shows `[Pause]` when running, `[Resume]` when paused. When paused, the "Next run" line says `paused` in muted red instead of the next datetime.

**Settings section** is at the bottom of the page, below the article list — placement signals "this is set-and-forget, not a control surface." The header reads "Settings (rarely needed)". The single field is a `<input type="number">` with the current value, a Save button, and a one-sentence explanation of the soft-cap rule. No slider, no presets, no live preview, no history of past values. On invalid input (out of range, non-numeric), redirect back with `?budget=invalid` and show a muted red flash near the field.

Styling: single CSS file, system font stack, monospace for timestamps and durations, restrained palette. ~60 lines of CSS. No theming, no dark mode, no mobile responsive (laptop only).

## Queue stats

Computed on each `GET /`:

1. Fetch `toepub`-tagged articles from Reader.
2. Subtract already-sent IDs from SQLite.
3. Sum `word_count` from Reader (use the same fallback as digest run).

Cache result in-memory for 60 seconds (TTL dict). Avoids hammering Reader on rapid refreshes.

If the Reader API call fails, render `Queue  unavailable` in muted red. Don't block the rest of the dashboard. Log the failure.

## Chart

Bar chart, last 30 days inclusive, articles sent per calendar day. Days with zero rendered as empty bars (gap shown explicitly). Data:

```sql
SELECT DATE(sent_at) AS day, COUNT(*) AS n, SUM(word_count) AS words
FROM sent_articles
WHERE sent_at >= DATE('now', '-30 days')
GROUP BY day
ORDER BY day;
```

Fill in zero-rows for missing days in Python before passing to template. Pass as JSON, render via Chart.js inline `<script>`.

Y-axis: article count. Tooltip on each bar shows both article count and total words.

## Logs

Each row in the recent runs table has a `▸` toggle. Clicking expands a `<details>` element containing a `<pre>` with the captured log buffer from `runs.log`. No AJAX. Cap log display at ~20KB per run with a "truncated" notice if longer.

## Project layout additions

```
src/digest/
├── dashboard.py          # FastAPI app, routes, BackgroundTasks
├── templates/
│   └── index.html        # Single-page Jinja template
└── static/
    └── style.css
```

## Manual testing checklist

Queue + soft cap:
- [ ] Tag 1 short article (<5000 words). Run --once. Single article in digest.
- [ ] Tag 6 articles totaling >5000 words. Run --once. Verify N articles selected, total exceeds 5000 but stops at first overshoot. Re-run: only remaining articles are eligible.
- [ ] Tag 1 article >5000 words. Run --once. The single article is sent (stop-when-exceeded ensures it).
- [ ] Random selection: tag the same 5 articles, untag any sent ones, re-tag, run --once twice. Verify selection differs (probabilistically).

Pause:
- [ ] Pause via dashboard. Wait for scheduled time. Verify no email sent, log shows "paused, skipping."
- [ ] While paused, hit Trigger. Verify digest sends normally.
- [ ] Resume. Verify next scheduled run executes.
- [ ] Pause state survives container restart.

Word budget:
- [ ] Fresh DB: settings row created with WORD_BUDGET env value.
- [ ] Existing DB on upgrade: settings row created if missing, otherwise left alone.
- [ ] Edit budget via dashboard input → next digest respects the new value.
- [ ] Invalid input (negative, zero, >50000, non-numeric) → flash error, settings unchanged.
- [ ] Container restart with a new env value: setting in DB takes precedence (env value ignored).

Volume numbering:
- [ ] Tag 2 articles, run --once → Vol 1 EPUB on Inkbook.
- [ ] Tag 1 more, hit Trigger → Vol 2 EPUB on Inkbook with distinct filename.

Reader-as-SOT:
- [ ] Tag an article. Run --once. Article sent.
- [ ] Tag a second article. Untag the first one in Reader. Run --once. Only the second article is in the digest, first is silently dropped from queue.
- [ ] Article with `toepub` tag but Reader hasn't finished parsing yet (no html content): skipped without error, retries next day.

Dashboard:
- [ ] Loads at `http://<pi>:8080/`.
- [ ] Trigger button: success path, already-running path.
- [ ] Pause button: toggles, label updates, persists across restart.
- [ ] Queue stats render correctly with N articles.
- [ ] Reader API down: queue shows "unavailable," rest of dashboard renders.
- [ ] Chart renders with zero-bars on empty days.
- [ ] Recent runs log expansion works for both ok and error rows.
- [ ] /healthz returns 200 even if SQLite is locked or Reader is down.
- [ ] First load on a fresh DB: tables show "no data yet" rather than empty <table>.

Concurrency:
- [ ] Trigger button twice in quick succession: second click shows already_running flash.
- [ ] Stop container while triggered run is in progress: WAL mode handles this without corruption.

## Decisions taken

- **Selection**: `ORDER BY RANDOM()`, no priority, no FIFO.
- **Word cap rule**: stop-when-exceeded (include the article that pushes total over).
- **Queue source**: derived from Reader at runtime, no local queue table.
- **Tag re-check**: full Reader fetch each run, drop articles that lost the tag.
- **Pause**: indefinite toggle, manual trigger overrides.
- **Manual trigger budget**: same word budget as scheduled run (read from settings).
- **Word budget UI**: plain number input + Save button at the bottom of the dashboard, under "Settings (rarely needed)". No slider, no presets.
- **Word budget precedence**: env var seeds initial value on first startup; settings table is SOT thereafter.
- **Empty queue**: log empty, no email.
- **Queue UX**: pool, not line — totals only, no "next up" view.
- **Dashboard**: word counts visible alongside article counts.

## Things explicitly not in scope

- Pause schedule (start/end dates, calendar)
- Multiple word-budget profiles
- Per-article priority or pinning
- Force-include / "send this one tomorrow" controls
- Stale-article detection (articles sat in queue too long)
- Editing past digests, resending, deleting individual articles
- Search, filtering, pagination on dashboard tables
- Theming, dark mode, mobile responsive
- Anything that involves predicting or surfacing what will be in the next digest
