# Kiosk — v6 spec (friend link intake)

Extends `SPEC.md`–`SPEC_V5.md` and `CLAUDE.md`.

Friends contribute articles by emailing links to the Reader **library** address. Reader does not fetch emailed links: it stores the email itself as a one-word `email` document. The intake job turns those into real articles.

## Job

Every `INTAKE_INTERVAL_MINUTES` (default 60), and on the dashboard's **Check now**:

1. List `category=email` documents updated since the stored cursor (`intake_cursor` setting; first run looks back 3 days). Skip ids already in `intake_emails`.
2. Match the sender against `INTAKE_SENDERS` (`address|tag;address|tag`). The address is searched in every metadata string field of the document (not body, summary or notes); a key without `@` matches the `author` display name exactly. Unmatched emails are untouched.
3. Fetch the body and extract `http(s)` links (hrefs and plain text, deduplicated, `INTAKE_IGNORE_DOMAINS` dropped — default `proton.me,protonmail.com,pm.me,readwise.io` to skip mail signatures).
4. Classify:
   - **links**: 1–10 links and ≤ 40 words of non-URL text. Each link is saved with `POST /save/` tagged `<sender tag>` + `READER_TAG_TRIGGER`, so it joins the Morning Paper queue. If Reader already has the URL (200), the two tags are added to the existing document. The email gets the sender tag and is archived.
   - **newsletter** (more text or links) / **no-links**: the email gets the sender tag and stays where it is.
5. Advance the cursor to run start minus 10 minutes.

## Errors

- Per link 4xx (except 429): recorded as `error` in `intake_links`, other links continue. An email whose links all failed is tagged but not archived.
- 429, 5xx, network: the run stops without advancing the cursor or marking the email; the next run retries, skipping links already saved.

## Dashboard

"Links from friends" section (only when `INTAKE_SENDERS` is set): last 30 links with sender tag, status (`saved`, `existing`, `error` with the reason on hover) and time, plus **Check now**.

## CLI

- `python -m digest.intake --once` — one intake pass.
- `python -m digest.intake --inspect DAYS` — print metadata of recent email documents and the sender match, to check which field carries the sender address.

## Unverified against the live API

- Which field of an email document holds the sender address (hence the all-fields match and `--inspect`).
- `PATCH /update/` with `location: archive` (tag PATCH is already verified).
