# Kiosk — v8 spec (dashboard redesign, pool history)

Extends `SPEC_V7.md`. Same features, reorganised into two pages with an e-ink look (greyscale, Literata + IBM Plex Mono), plus a daily record of the pool size.

## Pool snapshots

- New table `pool_snapshots(day, taken_at, articles, words)`, one row per local day, latest measurement wins.
- Written by a daily job at 03:15 local (also two minutes after startup) and whenever the dashboard refreshes its queue stats.
- A failed snapshot logs a warning; the next one retries.

## Kiosk page (`/`)

Sidebar: Kiosk (with anchors to Editions, Readers, Press room), Library, schedule status, Pause / Send now, OPDS URL. Collapses to a row of pills on phones.

1. **Header.** "Next issue Saturday, 07:30." plus a rounded pool line: "About 61k words in the pool, roughly four issues' worth. A month ago it was around 50k."
   - Issues' worth = pool words ÷ average words of the last 8 sent issues (the budget is a soft cap the issue always overshoots, so the budget would overstate it). Falls back to the budget with no history.
   - The month-ago clause uses the snapshot closest to 30 days ago, within ±7 days; omitted otherwise.
2. **Chart**, last 12 weeks (Monday-based, local time), toggle remembered per browser:
   - *Weekly*: words sent per week, article count inside each bar, dashed budget line.
   - *Cumulative*: running total of words read, and words added to the pool since the first snapshot in the window (read + pool − first pool). The gap is pool growth over the window, not the pool itself: the pool can be many times what is read in 12 weeks, which would flatten the read line. The added line appears once two weeks have snapshots; until then the y-axis scales to the read line alone.
   - Clicking a week shows words, articles, average length, words added that week (Δpool + read) and pool size.
3. **Editions**: expandable rows (date, articles, words, emailed count, status / failed emails), article list with domain and length, EPUB and email preview links. Six shown, the rest behind "Show older".
4. **Readers**: subscriber cards (Send latest, Pause, Edit → version, sender name, pool checkbox, remove), add form, recent deliveries; "Sent by readers" list with status: failed, in issue <date> (joined through `sent_articles`), in pool, library.
5. **Press room**: schedule, budget, email mode, pool tag, with the existing schedule editor behind "Edit"; run log with expandable logs.

## Library page (`/library`)

Upload button and drop zone, books as covers (EPUB cover if any, typographic otherwise), download and delete. Upload/delete redirect back here.
