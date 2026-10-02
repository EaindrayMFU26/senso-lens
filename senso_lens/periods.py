"""The time axis: discretization of commit events into periods.

This is the UTG discretization step applied to a repository. A period key is a
sortable string; `period_range` fills the gaps so every module series has one
entry per period between its first activity and the last closed period.

Scales use calendar units (ISO weeks, calendar months/quarters/years) rather
than fixed-length seconds, so "since 2023-Q2" means what a reader expects.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

SCALES = ("weekly", "monthly", "quarterly", "yearly")


def _utc(ts: int | float | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def period_key(ts: int | float | datetime, scale: str) -> str:
    d = _utc(ts)
    if scale == "weekly":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if scale == "monthly":
        return f"{d.year}-{d.month:02d}"
    if scale == "quarterly":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if scale == "yearly":
        return f"{d.year}"
    raise ValueError(f"unknown scale {scale!r}; expected one of {SCALES}")


def period_start(key: str, scale: str) -> datetime:
    if scale == "weekly":
        y, w = key.split("-W")
        return datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=timezone.utc)
    if scale == "monthly":
        y, m = key.split("-")
        return datetime(int(y), int(m), 1, tzinfo=timezone.utc)
    if scale == "quarterly":
        y, q = key.split("-Q")
        return datetime(int(y), (int(q) - 1) * 3 + 1, 1, tzinfo=timezone.utc)
    if scale == "yearly":
        return datetime(int(key), 1, 1, tzinfo=timezone.utc)
    raise ValueError(scale)


def next_period(key: str, scale: str) -> str:
    start = period_start(key, scale)
    if scale == "weekly":
        return period_key(start + timedelta(days=7), scale)
    if scale == "monthly":
        y, m = start.year, start.month + 1
        if m == 13:
            y, m = y + 1, 1
        return f"{y}-{m:02d}"
    if scale == "quarterly":
        y, q = key.split("-Q")
        q = int(q) + 1
        y = int(y)
        if q == 5:
            y, q = y + 1, 1
        return f"{y}-Q{q}"
    if scale == "yearly":
        return str(int(key) + 1)
    raise ValueError(scale)


def period_index(key: str, scale: str) -> int:
    """Integer ordinal of a period key (consecutive periods differ by exactly 1)."""
    if scale == "weekly":
        return period_start(key, scale).toordinal() // 7
    if scale == "monthly":
        y, m = key.split("-")
        return int(y) * 12 + int(m) - 1
    if scale == "quarterly":
        y, q = key.split("-Q")
        return int(y) * 4 + int(q) - 1
    if scale == "yearly":
        return int(key)
    raise ValueError(scale)


def period_from_index(i: int, scale: str) -> str:
    if scale == "weekly":
        return period_key(datetime.fromordinal(i * 7).replace(tzinfo=timezone.utc) + timedelta(days=3), scale)
    if scale == "monthly":
        return f"{i // 12}-{i % 12 + 1:02d}"
    if scale == "quarterly":
        return f"{i // 4}-Q{i % 4 + 1}"
    if scale == "yearly":
        return str(i)
    raise ValueError(scale)


def periods_between(first: str, last: str, scale: str) -> int:
    """Signed distance in periods from `first` to `last` (0 for the same period)."""
    return period_index(last, scale) - period_index(first, scale)


def period_range(first: str, last: str, scale: str) -> list[str]:
    """All period keys from `first` to `last` inclusive (empty if first > last)."""
    a, b = period_index(first, scale), period_index(last, scale)
    if b - a > 20000:
        raise RuntimeError("period_range runaway")
    return [period_from_index(i, scale) for i in range(a, b + 1)]


def previous_period(key: str, scale: str) -> str:
    start = period_start(key, scale)
    return period_key(start - timedelta(seconds=1), scale)


def current_period(scale: str, now: datetime | None = None) -> str:
    return period_key(now or datetime.now(tz=timezone.utc), scale)


def last_closed_period(scale: str, now: datetime | None = None) -> str:
    """The most recent period that has fully elapsed (the open window is excluded)."""
    return previous_period(current_period(scale, now), scale)


def choose_scale(timestamps: list[int], lookback_years: float = 3.0, now: datetime | None = None) -> str:
    """Time-gap rule (UTG, Definition 7): the finest scale with no empty period.

    Evaluated over the most recent `lookback_years` so that a sparse early
    history does not force a coarse axis on an active project.
    """
    if not timestamps:
        return "monthly"
    now_dt = now or datetime.now(tz=timezone.utc)
    cutoff = now_dt - timedelta(days=365.25 * lookback_years)
    recent = [t for t in timestamps if _utc(t) >= cutoff] or timestamps
    first, last = min(recent), max(recent)
    for scale in SCALES:
        keys = set(period_key(t, scale) for t in recent)
        full = period_range(period_key(first, scale), period_key(last, scale), scale)
        if len(full) >= 4 and all(k in keys for k in full):
            return scale
    return "yearly"
