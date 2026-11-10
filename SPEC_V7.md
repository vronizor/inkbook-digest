# Kiosk — v7 spec (subscribers as intake senders)

Extends `SPEC_V6.md`. Subscribers replace `INTAKE_SENDERS` as the main list of people who can send links.

## Senders

- Every subscriber with a sender name is an intake sender, paused or not. Without one, their emails are ignored.
- **Sender name** (`subscribers.sender_name`, editable in the dashboard): the display name Reader shows as the email document's `author`, matched exactly, case-insensitive. The API exposes no sender address (see SPEC_V6).
- **Tag**: first word of the subscriber's name, lowercased, accents stripped, only `[a-z0-9-]` kept. Falls back to the first word of the sender name, then the address local part. Subscribers with the same first name share a tag.
- **Links to pool** (`subscribers.to_pool`, checkbox, default off): ticked → links saved with `<tag>` + `READER_TAG_TRIGGER` (Morning Paper queue); unticked → `<tag>` only (library). The source email is tagged and archived either way, as in v6.
- `INTAKE_SENDERS` (`Display Name|tag`) still works for non-subscribers and always adds the trigger tag. A subscriber with the same sender name overrides that entry.

## Scheduling

The intake job is always scheduled and re-lists the last 7 days of email documents each run (SPEC_V6). A run with no senders returns without calling Reader.

## Dashboard

- Subscribers table gets a **Links** column: sender-name input (saves on change) and a checkbox showing the tags that will be applied.
- The add form gets the sender name and a "links to pool" checkbox.
- "Links from friends" shows when there is at least one subscriber or `INTAKE_SENDERS` entry.
