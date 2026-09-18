"""Clock abstraction so cross-timezone / deadline logic is testable."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse_instant(value):
    """Parse an ISO-8601 string into an aware UTC datetime.

    Naive timestamps are assumed UTC.  The trailing ``Z`` is accepted.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_instant(dt: datetime) -> str:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_local_deadline(value, timezone_name: str) -> datetime:
    """Parse a deadline expressed in the project's local timezone.

    A deadline written as ``2026-09-30T17:00`` in ``Asia/Singapore``
    denotes a different instant than the same wall-clock time in UTC.
    """
    tz = ZoneInfo(timezone_name)
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            return parse_instant(text)
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=tz).astimezone(timezone.utc)


class Clock:
    """Returns the current time; tests freeze it."""

    def __init__(self, fixed: datetime | None = None):
        self._fixed = parse_instant(fixed) if fixed else None

    def now(self) -> datetime:
        return self._fixed if self._fixed else datetime.now(timezone.utc)

    def freeze(self, value):
        self._fixed = parse_instant(value)
        return self

    def advance(self, **delta):
        from datetime import timedelta
        base = self._fixed or datetime.now(timezone.utc)
        self._fixed = base + timedelta(**delta)
        return self
