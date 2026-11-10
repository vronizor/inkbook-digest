# Kiosk — v7 spec (subscribers as intake senders)

Extends `SPEC_V6.md`. Subscribers replace `INTAKE_SENDERS` as the main list of people who can send links.

## Senders

- Every subscriber is an intake sender, paused or not.
- **Sender name** (`subscribers.sender_name`, editable in the dashboard): the display name Reader shows as the email document's `author`. Matched exactly, case-insensitive. The subscriber's address is also matched, as in v6.
- **Tag**: first word of the subscriber's name, lowercased, accents stripped, only `[a-z0-9-]` kept. Falls back to the first word of the sender name, then the address local part. Subscribers with the same first name share a tag.
- **Links to pool** (`subscribers.to_pool`, checkbox, default off): ticked → links saved with `<tag>` + `READER_TAG_TRIGGER` (Morning Paper queue); unticked → `<tag>` only (library). The source email is tagged and archived either way, as in v6.
- `INTAKE_SENDERS` still works for non-subscribers and always adds the trigger tag. A subscriber whose address or sender name equals an `INTAKE_SENDERS` key overrides that entry.

## Scheduling

The intake job is always scheduled. A run with no senders returns without calling Reader.

## Dashboard

- Subscribers table gets a **Links** column: sender-name input (saves on change) and a checkbox showing the tags that will be applied.
- The add form gets the sender name and a "links to pool" checkbox.
- "Links from friends" shows when there is at least one subscriber or `INTAKE_SENDERS` entry.
