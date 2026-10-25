# Kiosk — v5 spec (dashboard cleanup, schedule)

Extends `SPEC.md`–`SPEC_V4.md` and `CLAUDE.md`.

## Dashboard cleanup

- Recent digests, runs and sent articles show the last 5 rows; "Show N more" reveals the rest (up to 30).
- Timestamps render in local time as `Sun 25 Oct, 20:57` (year added when not the current year).
- "Settings (rarely needed)" → "Settings"; one form, one Save.

## Schedule

Replaces the `digest_interval_days` setting and the fixed `DIGEST_HOUR:DIGEST_MINUTE` cron.

- **Days**: any subset of Mon–Sun (≥1). Presets: every day, weekdays, weekends.
- **Time**: HH:MM in `TZ`.
- **Repeat every N weeks** (1–8), counted in calendar weeks from an anchor Monday stored in settings. A run fires only in weeks where `(monday(today) - anchor) / 7` is ≥ 0 and divisible by N; otherwise it logs `off-week` and exits.
- **Starting this week / next week** (shown when N > 1) sets the anchor to this or next Monday. Saving without choosing keeps the anchor. N = 1 resets it to this Monday.
- Manual Trigger ignores the schedule and does not shift it.
- Saving reschedules the APScheduler job immediately (no restart).
- The dashboard shows a plain-English summary and the next two digests, computed with the off-week rule (not APScheduler's `next_run_time`, which ignores it).

Settings keys: `schedule_days` (`5` or `0,3`, Mon = 0), `schedule_time` (`07:30`), `schedule_weeks`, `schedule_anchor` (ISO date, a Monday).

### Migration

On first start without `schedule_days`: `digest_interval_days` ≤ 1 → every day; otherwise weekly on the weekday of the last sent digest, `N = round(interval / 7)`. Time from `DIGEST_HOUR`/`DIGEST_MINUTE`. `digest_interval_days` is no longer read after that.

"Daily word budget" is renamed "Word budget per digest"; semantics unchanged.
