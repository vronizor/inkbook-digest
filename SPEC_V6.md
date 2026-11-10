# Kiosk — v6 spec (friend link intake)

Extends `SPEC.md`–`SPEC_V5.md` and `CLAUDE.md`.

Friends contribute articles by emailing links to the Reader **library** address. Reader does not fetch emailed links: it stores the email itself as a one-word `email` document. The intake job turns those into real articles.

## Job

Every `INTAKE_INTERVAL_MINUTES` (default 60), and on the dashboard's **Check now**:

1. List `category=email` documents updated in the last 7 days. Skip ids already in `intake_emails`. A fixed window instead of a stored cursor means a sender added to the config, or an outage under a week, still catches earlier emails.
2. Match the sender against `INTAKE_SENDERS` (`Display Name|tag;…`), case-insensitively against the document's `author`. The API does not expose the sender address: `author` is the display name and `source_url` is an opaque `mailto:reader-forwarded-email/<hash>`. Unmatched emails are untouched.
3. Fetch the body and extract `http(s)` links (hrefs and plain text, deduplicated, `INTAKE_IGNORE_DOMAINS` dropped — default `proton.me,protonmail.com,pm.me,readwise.io` to skip mail signatures).
4. Classify:
   - **links**: 1–10 links and ≤ 40 words of non-URL text. Each link is saved with `POST /save/` tagged `<sender tag>` + `READER_TAG_TRIGGER`, so it joins the Morning Paper queue. If Reader already has the URL (200), the two tags are added to the existing document. The email gets the sender tag and is archived.
   - **newsletter** (more text or links) / **no-links**: the email gets the sender tag and stays where it is.

## Errors

- Per link 4xx (except 429): recorded as `error` in `intake_links`, other links continue. An email whose links all failed is tagged but not archived.
- 429, 5xx, network: the run stops without marking the email; the next run retries, skipping links already saved.

## Dashboard

"Links from friends" section (only when `INTAKE_SENDERS` is set): last 30 links with sender tag, status (`saved`, `existing`, `error` with the reason on hover) and time, plus **Check now**.

## CLI

- `python -m digest.intake --once` — one intake pass.
- `python -m digest.intake --inspect DAYS` — print metadata of recent email documents and the sender match, to find a friend's display name.

Run both inside the container: `docker compose exec inkbook-digest python -m digest.intake …`.

## Caveats

- Display-name matching breaks silently if a friend renames their mail account, and anyone who knows the library address could send under a configured name.
- `PATCH /update/` with `location: archive` is not yet verified against the live API (tag PATCH is).
