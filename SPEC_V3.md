# inkbook-digest — v3 spec (library, OPDS, email removal)

Supersedes the email delivery path. Extends `SPEC.md`, `SPEC_V2.md`, and `CLAUDE.md`. The repo and container name stay `inkbook-digest`; only the dashboard title changes.

## Goal

Replace the broken Inkbook email ingestion with a Pi-hosted OPDS server that KOReader on the Inkbook subscribes to. Add a dashboard-driven library for manually-uploaded EPUBs and PDFs. Two top-level OPDS catalogs: morning papers (auto-generated digests) and library (manual uploads).

## Non-goals

- No external metadata fetching (Google Books, Open Library, etc.)
- No PDF thumbnail generation (generic placeholder only)
- No reading-progress sync, no annotations, no highlights
- No multi-user, no auth (Tailscale-only)
- No nested folders within the library (flat directory)
- No tag-based organization within the library
- No search

## Identity

- Repo name: `inkbook-digest` (unchanged)
- Container name: `inkbook-digest` (unchanged)
- Dashboard title: **"E-books & Morning Paper"** (changed from "Morning Paper")
- README intro updated to describe the broader mission

## Email removal

- Remove `mailer.py`'s digest-send code path entirely
- Keep the alert email path: errors still notify via SMTP to ALERT_EMAIL
- Remove `INKBOOK_EMAIL` env var and references
- The `mailer.py` module shrinks to just the alert function (or rename to `alerts.py` if cleaner)
- Remove the dry-test step in README that checks for email arrival on Inkbook

## OPDS

OPDS 1.2 (Atom-based). Two top-level catalogs.

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/opds/` | OPDS root, lists two child feeds |
| GET | `/opds/digests/` | Acquisition feed of recent morning papers (last 30 days) |
| GET | `/opds/library/` | Acquisition feed of all library books, recently-added first |
| GET | `/opds/file/digest/{digest_id}` | Direct EPUB download for a digest |
| GET | `/opds/file/library/{book_id}` | Direct download for a library book |
| GET | `/opds/cover/digest/{digest_id}` | Cover thumbnail for a digest |
| GET | `/opds/cover/library/{book_id}` | Cover thumbnail for a library book |

All routes return appropriate Content-Type headers:
- OPDS feeds: `application/atom+xml;profile=opds-catalog;kind=acquisition` (or `kind=navigation` for the root)
- EPUBs: `application/epub+zip`
- PDFs: `application/pdf`
- Covers: `image/jpeg` or `image/png`

### Atom structure

Standard OPDS 1.2 feed elements per entry:
- `<id>` (stable URN per item)
- `<title>`
- `<author><name>` (single author)
- `<updated>` (ISO 8601 with timezone)
- `<published>` (when added)
- `<summary>` (article list for digests, book description if available for library)
- `<dc:language>` (language code)
- `<link rel="http://opds-spec.org/image" type="image/jpeg" href="...">` (cover)
- `<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="...">` (download)

The digests feed limits to last 30 days of `status='sent'` digests, sorted recent-first. The library feed shows all books, recently-added first.

## Library

### Storage

```
$DATA_DIR/
├── digest.db                      # existing
├── epubs/                         # existing, digest output
└── library/                       # new
    ├── files/                     # uploaded EPUBs and PDFs
    └── covers/                    # extracted cover thumbnails (.jpg)
```

The `library/covers/` directory caches extracted covers keyed by `book_id`. Recreate on demand if missing. Generic placeholder served for books without a cover and for all PDFs.

### SQLite schema

```sql
CREATE TABLE library_books (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL UNIQUE,            -- as stored on disk, also the user-visible name
  title TEXT,                               -- extracted from EPUB metadata, NULL if extraction failed
  author TEXT,                              -- extracted from EPUB metadata, "Unknown" if missing
  language TEXT,                            -- extracted from EPUB metadata, NULL if missing
  format TEXT NOT NULL,                     -- 'epub' | 'pdf'
  size_bytes INTEGER NOT NULL,
  has_cover INTEGER NOT NULL DEFAULT 0,     -- boolean: 1 if cover extracted, 0 if placeholder
  added_at TEXT NOT NULL                    -- ISO 8601, UTC
);
```

Filename is `UNIQUE` — duplicate uploads are rejected at the SQLite layer too, not just at the filesystem layer.

### Upload behavior

- Endpoint: `POST /library/upload` with `multipart/form-data`, single file field
- Accept: `.epub` and `.pdf` extensions only (case-insensitive)
- Max size: 200MB (env-configurable: `LIBRARY_MAX_UPLOAD_MB=200`)
- On filename conflict: reject with HTTP 409, redirect to `/?library_upload=conflict&name=X`
- On valid upload:
  1. Stream to disk in `library/files/`
  2. For EPUBs: parse metadata (title, author, language) using ebooklib, extract cover image, save to `library/covers/{book_id}.jpg`
  3. For PDFs: skip metadata extraction, no cover
  4. Insert row in `library_books`
  5. Redirect to `/?library_upload=ok&name=X`
- On parse failure during step 2: log warning, insert row with NULL/Unknown metadata, mark `has_cover=0`. Do not reject the upload.

User-visible identifier in the dashboard is **always the filename**. Extracted metadata is for the OPDS feed only.

### Delete behavior

- Endpoint: `POST /library/{book_id}/delete`
- Two-step: dashboard renders a confirmation dialog (HTML `<dialog>` element with JS to show, or an inline confirm flow), only POSTs after confirm
- On delete: remove file from `library/files/`, remove cover from `library/covers/`, delete SQLite row
- If file missing on disk but row exists: still delete the row, log warning
- Redirect to `/?library_delete=ok&name=X`

### Cover extraction

For EPUBs:
1. Open with `ebooklib.epub.read_epub()`
2. Find item with `media_type` starting with `image/` and a name like `cover` (case-insensitive substring match)
3. If found, save to `library/covers/{book_id}.{ext}`, set `has_cover=1`
4. If not found, fall back to first image item; if still nothing, `has_cover=0`
5. Resize to max 600px on long edge, JPEG quality 80

For PDFs: skip entirely. `has_cover=0`. Use placeholder.

Generic placeholder: a simple SVG-rendered image (book icon, format label "EPUB" or "PDF") generated at startup once and cached.

## Dashboard changes

### New library section (top of page, above Status)

```
┌─────────────────────────────────────────────────────────┐
│  E-books & Morning Paper        [Pause]   [Trigger]     │
├─────────────────────────────────────────────────────────┤
│  Library                                  [Upload]      │
│   ┌───┐                                                 │
│   │ 📕│ Tractatus Logico-Philosophicus                  │
│   │   │ Wittgenstein  · EPUB · 412 KB · 2026-04-12      │
│   │   │                                          [×]    │
│   ├───┤                                                 │
│   │ 📄│ thesis-draft.pdf                                │
│   │   │ Unknown · PDF · 4.2 MB · 2026-04-10             │
│   │   │                                          [×]    │
│   └───┘                                                 │
│   ...                                                   │
├─────────────────────────────────────────────────────────┤
│  Status                                                 │
│   Last digest    ...                                    │
│   ...                                                   │
└─────────────────────────────────────────────────────────┘
```

Compact list rows:
- 60×90px thumbnail on the left (extracted cover or placeholder)
- Filename as primary text (large)
- Metadata line: author · format · size · added date
- Delete button (×) right-aligned

Upload button at the section header opens a file picker. Multiple files can be selected; each uploads independently, results shown as flash messages on redirect (one per file).

### OPDS info line

Below the header, a small muted line: `OPDS feed: http://<pi>:8080/opds/` so you can see at a glance where to point KOReader. No need to dynamically detect the host — just render the env-configured `BASE_URL` if set, else show the current request host.

### Flash messages

Single flash area below the header, supports multiple stacked messages. Query params handled:

- `?library_upload=ok&name=X` → green flash "Uploaded: X"
- `?library_upload=conflict&name=X` → red flash "X already exists. Rename and try again."
- `?library_upload=invalid&name=X&reason=Y` → red flash "X rejected: Y"
- `?library_delete=ok&name=X` → green flash "Deleted: X"
- `?triggered=ok` → existing
- `?triggered=already_running` → existing
- `?budget=ok` → existing
- `?budget=invalid` → existing

Auto-dismiss after a few seconds via a small `setTimeout` in the existing inline script.

## Configuration

New env vars:

```
LIBRARY_MAX_UPLOAD_MB=200
BASE_URL=                          # optional, e.g. http://pi.tailnet.ts.net:8080
                                   # used in OPDS feeds for absolute URLs and dashboard hint
```

Removed env vars:

```
INKBOOK_EMAIL                      # no longer used
```

## OPDS URLs and absolute paths

OPDS feeds reference acquisition links and cover links. KOReader needs absolute URLs (or at least URLs resolvable from the feed's location).

Strategy:
- If `BASE_URL` is set, use it for all link href values.
- If not set, use the request's `request.base_url` from FastAPI.

Tailscale hostnames work fine as `BASE_URL`. Set it once in `.env` and forget.

## Project layout additions

```
src/digest/
├── library.py                    # upload, delete, metadata extraction, cover handling
├── opds.py                       # OPDS feed generation (digest + library)
├── templates/
│   ├── index.html                # extended with library section
│   └── opds_acquisition.xml      # Jinja for OPDS feeds
│   └── opds_navigation.xml       # Jinja for the root navigation feed
└── static/
    ├── style.css                 # extended for library list
    └── placeholder-epub.png      # generic cover for missing covers
    └── placeholder-pdf.png       # generic cover for PDFs
```

## Manual testing checklist

OPDS:
- [ ] `/opds/` returns valid Atom XML, navigable in KOReader
- [ ] `/opds/digests/` lists recent digests with covers
- [ ] `/opds/library/` lists library books with covers
- [ ] Tap a digest in KOReader, downloads and opens
- [ ] Tap a library EPUB in KOReader, downloads and opens
- [ ] Tap a library PDF in KOReader, downloads and opens
- [ ] Empty library: feed still validates, shows zero entries
- [ ] Empty digests: feed still validates, shows zero entries

Library upload:
- [ ] Upload single EPUB: appears in library, metadata extracted, cover shown
- [ ] Upload single PDF: appears in library, placeholder cover, "Unknown" author
- [ ] Upload EPUB with broken metadata: appears with filename as title, "Unknown" author, no error
- [ ] Upload duplicate filename: 409 redirect, conflict flash, original unchanged
- [ ] Upload `.txt` or other format: rejected, invalid flash
- [ ] Upload >200MB file: rejected, invalid flash
- [ ] Upload multiple files at once: each shows individual flash result

Library delete:
- [ ] Click delete: confirm dialog appears, cancel preserves file
- [ ] Confirm delete: file removed from disk, cover removed, row removed
- [ ] OPDS feed reflects deletion immediately

Dashboard:
- [ ] Library section renders at top
- [ ] Compact list with thumbnails displays correctly
- [ ] Empty library: shows "no books yet" placeholder text
- [ ] OPDS feed URL shown below header
- [ ] All existing dashboard functionality (digests, runs, chart, settings) still works

Flash messages:
- [ ] All new flash variants render correctly (upload ok, conflict, invalid, delete ok)
- [ ] Auto-dismiss works
- [ ] Multiple flashes stack

Email removal:
- [ ] No INKBOOK_EMAIL references remain in code or .env.example
- [ ] No mailer.send_digest() calls anywhere
- [ ] Alert email on failure still works (intentional break test: bad Reader token)

## Decisions taken

- Two top-level OPDS catalogs (digests, library), no flat or merged feed
- Library is flat, no folders, no tags, no search
- Filename is user-visible identifier; metadata is for OPDS only
- Duplicate filename = reject (no auto-rename, no overwrite)
- 200MB upload cap, env-configurable
- PDFs allowed but no thumbnails (placeholder)
- Cover extraction happens on upload, cached on disk
- Delete requires confirmation dialog
- Library books have no automatic retention (manual delete only)
- Email digest delivery removed entirely; alert email kept

## Things explicitly not in scope

- Renaming the repo or container
- Authentication on OPDS or upload endpoints
- Hierarchical library (folders, tags)
- Search across digests or library
- Reading-progress sync
- Sending books to other users
- External metadata enrichment
- ZIP/CBZ/MOBI/AZW3 support
- PDF cover extraction
- Background re-extraction of metadata for already-uploaded books
- Library-side OPDS pagination (small library, no paging needed)
