"""Unit tests: the time axis (UTG discretization) and the Mann-Kendall test."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from senso_lens.periods import (choose_scale, current_period, last_closed_period, next_period, period_key,
                                period_range, previous_period)
from senso_lens.signals.trend import mann_kendall

T = datetime(2026, 3, 15, 12, tzinfo=timezone.utc)


def test_period_keys():
    assert period_key(T, "weekly") == "2026-W11"
    assert period_key(T, "monthly") == "2026-03"
    assert period_key(T, "quarterly") == "2026-Q1"
    assert period_key(T, "yearly") == "2026"
    with pytest.raises(ValueError):
        period_key(T, "daily")


def test_next_previous_and_range_cross_year():
    assert next_period("2025-12", "monthly") == "2026-01"
    assert next_period("2025-Q4", "quarterly") == "2026-Q1"
    assert next_period("2025-W52", "weekly") == "2026-W01"
    assert previous_period("2026-01", "monthly") == "2025-12"
    assert period_range("2025-11", "2026-02", "monthly") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert period_range("2026-02", "2025-11", "monthly") == []
    # ISO week 53 years are handled by date arithmetic, not string arithmetic
    assert period_range("2020-W52", "2021-W01", "weekly") == ["2020-W52", "2020-W53", "2021-W01"]


def test_open_window_is_excluded():
    assert current_period("monthly", T) == "2026-03"
    assert last_closed_period("monthly", T) == "2026-02"
    assert last_closed_period("quarterly", T) == "2025-Q4"


def test_time_gap_rule_picks_finest_scale_without_empty_period():
    def ts(y, m, d=5):
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())
    monthly = [ts(2025, m) for m in range(1, 13)] + [ts(2026, m) for m in range(1, 4)]
    assert choose_scale(monthly, now=T) == "monthly"           # every month has a commit, but not every week
    sparse = [ts(2025, 1), ts(2025, 4), ts(2025, 9), ts(2025, 11), ts(2026, 1), ts(2026, 3)]
    assert choose_scale(sparse, now=T) == "quarterly"           # every quarter has a commit, several months do not
    assert choose_scale(sparse[:3] + sparse[4:], now=T) == "yearly"   # 2025-Q4 empty -> quarterly rejected
    weekly = [int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) + 86400 * d for d in range(0, 70, 3)]
    assert choose_scale(weekly, now=T) == "weekly"
    assert choose_scale([], now=T) == "monthly"


def test_mann_kendall_directions():
    up = mann_kendall([1, 2, 3, 4, 5, 6, 7, 8.0])
    assert up.trend == "increasing" and up.slope == pytest.approx(1.0) and up.p < 0.01
    down = mann_kendall([8, 7, 6, 5, 4, 3, 2, 1.0])
    assert down.trend == "decreasing" and down.z < 0
    flat = mann_kendall([3, 3, 3, 3, 3, 3, 3, 3.0])
    assert flat.trend == "no_trend" and flat.p == 1.0
    noisy = mann_kendall([2, 3, 2, 3, 2, 3, 2, 3.0])
    assert noisy.trend == "no_trend"
    assert mann_kendall([1.0, 2.0]).trend == "no_trend"


def test_mann_kendall_tie_correction_matches_textbook():
    # Gilbert (1987) tie-corrected variance: n=8 with one group of 6 equal values and one of 2
    r = mann_kendall([1, 0, 1, 0, 0, 0, 0, 0.0])
    assert r.s == -10
    assert r.z == pytest.approx(-1.5)          # var = (8*7*21 - 6*5*17 - 2*1*9)/18 = 36 -> sd 6 -> (S+1)/6
    assert r.p == pytest.approx(0.1336, abs=1e-3)
