"""
Singapore-time helpers (UTC+8, no DST) plus the slot-window / pace-marker math
that drives §8's delivery gating.

Slot windows tile the full 24h into two:
  AM window: [09:30:00, 14:29:59] SGT
  PM window: [14:30:00, 09:29:59 next day] SGT   (this one spans midnight)

A "slot marker" is a single integer ordinal identifying one (date, slot) pair, so
that ordering / one-step advancement is unambiguous even across the midnight
boundary of the PM window. marker = date.toordinal()*2 + (0 for am, 1 for pm).
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config import (
    TIMEZONE, AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE, PM_TRIGGER_HOUR, PM_TRIGGER_MINUTE,
)

SGT = ZoneInfo(TIMEZONE)

_AM = (AM_TRIGGER_HOUR, AM_TRIGGER_MINUTE)
_PM = (PM_TRIGGER_HOUR, PM_TRIGGER_MINUTE)


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
# Slot windows & pace markers (§8)
# --------------------------------------------------------------------------- #

def slot_of(now: datetime) -> str:
    """Which window ('am'|'pm') the clock time falls in. Times 00:00–09:29 are the
    *previous day's* PM window, so they classify as 'pm'."""
    t = (now.hour, now.minute)
    if _AM <= t < _PM:
        return "am"
    return "pm"


def slot_marker(now: datetime) -> int:
    """The integer ordinal of the slot `now` is currently in (see module docstring)."""
    t = (now.hour, now.minute)
    d = now.date()
    if t < _AM:
        # before the AM window opens: still the previous day's PM slot
        return (d.toordinal() - 1) * 2 + 1
    if t < _PM:
        return d.toordinal() * 2 + 0  # today's AM
    return d.toordinal() * 2 + 1      # today's PM


def marker_to_fields(marker: int) -> tuple[str, str]:
    """(date_iso, slot_str) for a marker ordinal — for storing pace_date/pace_slot."""
    d = date.fromordinal(marker // 2)
    slot = "am" if marker % 2 == 0 else "pm"
    return to_iso(d), slot


def fields_to_marker(date_iso: str | None, slot: str | None) -> int | None:
    """Inverse of marker_to_fields; None if either field is unset."""
    if not date_iso or not slot:
        return None
    return from_iso(date_iso).toordinal() * 2 + (0 if slot == "am" else 1)


def initial_pace_marker(now: datetime) -> int:
    """Pace marker to seed on activation: one slot BEFORE the current slot, so a
    freshly-onboarded user is owed exactly one delivery credit (§8)."""
    return slot_marker(now) - 1
