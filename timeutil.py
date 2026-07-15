"""
Singapore-time helpers (UTC+8, no DST) plus the daily pace-marker math that
drives §8's delivery gating.

One lesson slot per calendar day, unlocked at the single daily trigger time
(AM_TRIGGER_HOUR/MINUTE — the name is legacy from a since-removed twice-daily
design; it's just "the daily trigger" now). A "pace marker" is the date's
ordinal: every day past the trigger time grants one credit, each delivery
consumes exactly one.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config import TIMEZONE, AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE

SGT = ZoneInfo(TIMEZONE)

_TRIGGER = (AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE)


def sgt_now() -> datetime:
    return datetime.now(SGT)


def sgt_today() -> date:
    return sgt_now().date()


def to_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def from_iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def dates_between(start_exclusive: date, end_inclusive: date):
    d = start_exclusive + timedelta(days=1)
    out = []
    while d <= end_inclusive:
        out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Daily pace marker (§8)
# --------------------------------------------------------------------------- #

def current_marker(now: datetime) -> int:
    """The day-ordinal slot that's open right now: today's ordinal once the
    daily trigger time has passed, otherwise yesterday's (nothing new is due
    yet). Compares the full (hour, minute) tuple, not just the hour — a bare
    `hour >=` check is exactly the bug class that once fired a lesson at
    00:05 SGT in the reference bot."""
    d = now.date()
    if (now.hour, now.minute) < _TRIGGER:
        return d.toordinal() - 1
    return d.toordinal()


def marker_to_fields(marker: int) -> tuple[str, str]:
    """(date_iso, slot_label) for a marker ordinal. slot_label defaults to
    'daily'; callers that need to distinguish an on-time delivery from a
    catch-up one override the label themselves (see scheduler.py) — the
    stored value is informational only, never used to recompute the marker."""
    return to_iso(date.fromordinal(marker)), "daily"


def fields_to_marker(date_iso: str | None, slot: str | None) -> int | None:
    """Inverse of marker_to_fields. `slot` is accepted but ignored: rows
    written under the old twice-daily am/pm scheme already have `pace_date`
    set to the last calendar day a delivery was consumed, which is exactly
    the right value under the once-daily model too — no migration needed."""
    if not date_iso:
        return None
    return from_iso(date_iso).toordinal()


def initial_pace_marker(now: datetime) -> int:
    """Pace marker to seed on activation: one day before today's slot, so a
    freshly-onboarded user is owed exactly one delivery credit (§8)."""
    return current_marker(now) - 1
