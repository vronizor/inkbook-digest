# Kiosk — v4 spec (email subscribers)

Extends `SPEC.md`–`SPEC_V3.md` and `CLAUDE.md`. Reintroduces outbound email, this time to people, not to the Inkbook.

## Goal

Let a few friends receive the Morning Paper by email. Only articles I explicitly opt in are shared. Each subscriber gets `full`, `teaser`, or `both` (two separate emails per issue, to compare).

## Non-goals

- No self-service sign-up or unsubscribe page (Kiosk stays tailnet-only; I add/remove people from the dashboard)
- No open/click tracking
- No per-subscriber article selection, word budget or schedule — subscribers get a filtered view of *my* digest
- No embedded images (hotlinked only)
- No newsletter service

## Selection

- Articles in a digest that also carry `SHARE_TAG` (default `toshare`) are shared.
- `SHARE_TAG` set to empty → every article in the digest is shared.
- Tagging an article `toshare` after it went out in a digest does not send it retroactively.
- Digest with no shared articles → no email to anyone.

## Formats

| Format | Content per article |
|---|---|
| `full` | Title (linked to source), meta line (author · domain · date · reading time), "Read at {domain} →", sanitized full text |
| `teaser` | Title (linked), meta line, Reader `summary` if present else first 60 words, "Read at {domain} →" |

Both are `multipart/alternative` (HTML + plain text), one email per digest per format per subscriber, one SMTP message per recipient (no shared To/CC). Subjects differ (`Morning Paper · …` vs `Morning Paper headlines · …`) so `both` subscribers can tell them apart.

`full` starts with a table of contents with in-message anchors.

### Sanitizing

Stdlib `html.parser` allowlist, no new dependency:

- Kept: `p br h1–h6 ul ol li blockquote pre code em i strong b a img figure figcaption hr sup sub`. Headings demoted below the article title.
- Dropped with content: `script style iframe noscript svg form button video audio object head nav select textarea template canvas math`.
- Everything else unwrapped (content kept; block containers leave a line break).
- Attributes: only `a[href]` (http/https/mailto, resolved against the source URL) and `img[src, alt]` (http/https only; `data:` URIs dropped).
- Empty paragraphs/links removed. Rendered HTML over 100 KB logs a warning.

## Headers

`From: "{SHARE_SENDER_NAME}" <SMTP_FROM>`, `Date`, `Message-ID`, `List-Unsubscribe: <mailto:…?subject=unsubscribe>`, optional `Reply-To: SHARE_REPLY_TO`.

## Storage

```sql
CREATE TABLE subscribers (id, email UNIQUE, name, format, active, added_at);
CREATE TABLE deliveries (id, digest_id, email, format, status, size_bytes, error, manual, sent_at);
```

Rendered emails are written to `$DATA_DIR/emails/{digest_id}/{full,teaser}.{html,txt}` plus `meta.json` (subject, article count). They back the dashboard preview and "send latest". `--dry-run` writes to `$DATA_DIR/emails/dry-run/` and sends nothing.

## Run integration

After the EPUB is built and Reader tags are added. A delivery failure is recorded, logged and alerted, and marks the run `error`, but never blocks the digest or other subscribers.

`sent` means Proton SMTP accepted the message. Bounces arrive in the `SMTP_FROM` mailbox; Kiosk does not read them.

## Dashboard

A **Subscribers** section:

- Last shared issue, with preview links for both formats
- Subscriber list: name, email, format, last delivery status, actions (send latest issue, pause/resume, remove)
- Add form: name, email, version (full / teaser / both)
- Recent deliveries (last 20) in a collapsible table

## Env

| Var | Default | |
|---|---|---|
| `SHARE_TAG` | `toshare` | empty = share everything |
| `SHARE_SENDER_NAME` | `Morning Paper` | display name in From and the email header |
| `SHARE_REPLY_TO` | empty | optional Reply-To |
