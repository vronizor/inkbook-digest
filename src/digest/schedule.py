import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from digest import store

log = logging.getLogger(__name__)

JOB_ID = "digest"
CRON_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKS_MAX = 8


@dataclass(frozen=True)
class Schedule:
    days: tuple[int, ...]
    hour: int
    minute: int
    weeks: int
    anchor: date

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_days(raw: str) -> tuple[int, ...]:
    days = sorted({int(x) for x in raw.split(",") if x.strip() != ""})
    if not days or any(d < 0 or d > 6 for d in days):
        raise ValueError(f"invalid days: {raw!r}")
    return tuple(days)


def parse_time(raw: str) -> tuple[int, int]:
    hh, sep, mm = raw.strip().partition(":")
    hour, minute = int(hh), int(mm[:2])
    if not sep or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time: {raw!r}")
    return hour, minute


def _migrate_from_interval(conn: sqlite3.Connection) -> Schedule:
    interval = store.get_digest_interval_days(conn)
    last = store.get_last_sent_date(conn)
    last_day = date.fromisoformat(last) if last else None
    hour = int(os.environ.get("DIGEST_HOUR", "6"))
    minute = int(os.environ.get("DIGEST_MINUTE", "30"))
    if interval <= 1:
        sched = Schedule(tuple(range(7)), hour, minute, 1, monday(date.today()))
    else:
        weekday = last_day.weekday() if last_day else 5
        weeks = max(1, min(WEEKS_MAX, round(interval / 7)))
        sched = Schedule((weekday,), hour, minute, weeks, monday(last_day or date.today()))
    save(conn, sched)
    log.info(f"schedule initialised from digest_interval_days={interval}: {describe(sched)}")
    return sched


def load(conn: sqlite3.Connection) -> Schedule:
    days_raw = store.get_setting(conn, "schedule_days")
    if days_raw is None:
        return _migrate_from_interval(conn)
    hour, minute = parse_time(store.get_setting(conn, "schedule_time") or "06:30")
    return Schedule(
        days=parse_days(days_raw),
        hour=hour,
        minute=minute,
        weeks=int(store.get_setting(conn, "schedule_weeks") or "1"),
        anchor=date.fromisoformat(store.get_setting(conn, "schedule_anchor") or monday(date.today()).isoformat()),
    )


def save(conn: sqlite3.Connection, sched: Schedule) -> None:
    store.set_setting(conn, "schedule_days", ",".join(str(d) for d in sched.days))
    store.set_setting(conn, "schedule_time", sched.time_str)
    store.set_setting(conn, "schedule_weeks", str(sched.weeks))
    store.set_setting(conn, "schedule_anchor", sched.anchor.isoformat())


def trigger(sched: Schedule, tz: str) -> CronTrigger:
    return CronTrigger(
        day_of_week=",".join(CRON_DAYS[d] for d in sched.days),
        hour=sched.hour,
        minute=sched.minute,
        timezone=ZoneInfo(tz),
    )


def week_offset(sched: Schedule, d: date) -> int:
    return (monday(d) - sched.anchor).days // 7


def is_on_week(sched: Schedule, d: date) -> bool:
    offset = week_offset(sched, d)
    return offset >= 0 and offset % sched.weeks == 0


def next_runs(sched: Schedule, now: datetime, count: int = 2) -> list[datetime]:
    out: list[datetime] = []
    today = now.date()
    for i in range(7 * sched.weeks * count + 14):
        d = today + timedelta(days=i)
        if d.weekday() not in sched.days or not is_on_week(sched, d):
            continue
        run = datetime.combine(d, time(sched.hour, sched.minute), tzinfo=now.tzinfo)
        if run > now:
            out.append(run)
            if len(out) == count:
                break
    return out


def describe(sched: Schedule) -> str:
    t = sched.time_str
    days = sched.days
    if len(days) == 1:
        if sched.weeks == 1:
            return f"Every {FULL[days[0]]} at {t}"
        if sched.weeks == 2:
            return f"Every other {FULL[days[0]]} at {t}"
        return f"{FULL[days[0]]} every {sched.weeks} weeks at {t}"
    if len(days) == 7:
        base = f"Every day at {t}"
    elif days == (0, 1, 2, 3, 4):
        base = f"Weekdays at {t}"
    elif days == (5, 6):
        base = f"Weekends at {t}"
    else:
        base = f"{', '.join(SHORT[d] for d in days)} at {t}"
    if sched.weeks == 1:
        return base
    return f"{base}, every other week" if sched.weeks == 2 else f"{base}, every {sched.weeks} weeks"
