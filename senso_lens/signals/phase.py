"""Evolution phase: growth / stabilizing / decline / no_data, per module.

Phase is the *trajectory* of a module read from its own history over a rolling
window of closed periods (the open, partial period is never used for a trend
test — a half-finished quarter would look like the onset of decline).

Grounding: Lehman's laws of software evolution (1980) — software in use keeps
changing (I), complexity rises unless work is spent against it (II), and work
rate is statistically stable (IV) — so a module that has stopped changing has
either settled or is being abandoned. Direction comes from Mann-Kendall; ties
resolve to growth with low confidence, because a false "stabilizing" warning is
the costlier error for an agent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Config
from ..periods import last_closed_period, period_range, current_period
from ..store.db import Store
from .trend import mann_kendall

GROWTH, STABILIZING, DECLINE, NO_DATA = "growth", "stabilizing", "decline", "no_data"


@dataclass
class PhaseResult:
    phase: str
    since: str | None
    confidence: str | None            # high | medium | low | None (no_data)
    reasons: list[str]
    churn_trend: str                  # rising | flat | falling
    size_trend: str
    churn_level: float                # mean commits per period over the last K closed periods
    low_streak: int                   # consecutive trailing closed periods at/below the low threshold
    authors_recent: int
    authors_prior: int
    periods_total: int                # closed periods since first activity
    closed_through: str               # last closed period used
    window: list[str] = field(default_factory=list)
    commits_window: list[int] = field(default_factory=list)
    net_lines_window: list[int] = field(default_factory=list)
    open_period: dict[str, Any] = field(default_factory=dict)
    p_churn: float | None = None
    p_size: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase, "phase_since": self.since, "confidence": self.confidence, "reasons": self.reasons,
            "trends": {
                "churn": {"direction": self.churn_trend, "last_periods": self.commits_window, "p": self.p_churn},
                "size": {"direction": self.size_trend, "net_lines_last_periods": self.net_lines_window, "p": self.p_size},
                "authors": {"recent": self.authors_recent, "prior": self.authors_prior},
            },
            "window": {"periods": self.window, "closed_through": self.closed_through, "periods_total": self.periods_total,
                       "churn_level": round(self.churn_level, 2), "low_streak": self.low_streak},
            "open_period": self.open_period,
        }


def module_axis(store: Store, module: str, cfg: Config, now: datetime | None = None) -> tuple[list[str], dict[str, tuple[int, int, int]], dict[str, int]]:
    """Closed-period axis for a module: (periods, period->(commits, added, deleted), period->authors)."""
    rows = store.module_series(module)
    data = {r["period"]: (int(r["commits"]), int(r["added"]), int(r["deleted"])) for r in rows}
    authors = store.module_authors_by_period(module)
    if not data:
        return [], {}, {}
    closed = last_closed_period(cfg.scale, now)
    first = min(data)
    if first > closed:
        return [], data, authors
    axis = period_range(first, closed, cfg.scale)
    return axis, data, authors


def _classify(axis: list[str], data: dict, authors: dict[str, int], cfg: Config) -> PhaseResult:
    n = len(axis)
    closed_through = axis[-1] if axis else ""
    if n < cfg.phase_min_periods:
        return PhaseResult(NO_DATA, None, None, [f"only {n} closed period(s) of history; need {cfg.phase_min_periods}"],
                           "flat", "flat", 0.0, 0, 0, 0, n, closed_through, axis[-cfg.phase_window:],
                           [data.get(p, (0, 0, 0))[0] for p in axis[-cfg.phase_window:]], [])
    commits = [data.get(p, (0, 0, 0))[0] for p in axis]
    net = [data.get(p, (0, 0, 0))[1] - data.get(p, (0, 0, 0))[2] for p in axis]
    size = []
    acc = 0
    for v in net:
        acc += v
        size.append(acc)
    W, K = cfg.phase_window, cfg.phase_k_stable
    w_axis, w_c, w_s, w_net = axis[-W:], commits[-W:], size[-W:], net[-W:]
    mk_c = mann_kendall([float(v) for v in w_c], cfg.phase_alpha)
    mk_s = mann_kendall([float(v) for v in w_s], cfg.phase_alpha)
    level = sum(commits[-K:]) / K
    low = cfg.phase_low_churn
    streak = 0
    for v in reversed(commits):
        if v <= low:
            streak += 1
        else:
            break
    a_recent = max([authors.get(p, 0) for p in axis[-K:]] or [0])
    # "authors leaving" compares with what the module *used to* have, over a long memory (3 windows),
    # because withdrawal is slow: the people usually leave long before the last deletion lands
    memory = max(3 * W, 2 * K)
    prior_axis = axis[-K - memory:-K] if n > K else []
    a_prior = max([authors.get(p, 0) for p in prior_axis] or [0])
    earlier = commits[:-K]
    earlier_mean = (sum(earlier) / len(earlier)) if earlier else 0.0
    net_recent = sum(net[-K:])

    churn_rising = mk_c.trend == "increasing"
    churn_falling = mk_c.trend == "decreasing"
    size_before = max(1, size[-1] - net_recent)   # size at the start of the K window
    only_shrank = net_recent < 0 and max(net[-K:]) <= 0 and abs(net_recent) >= 0.05 * size_before
    size_falling = mk_s.trend == "decreasing" or only_shrank
    size_rising = mk_s.trend == "increasing"
    authors_leaving = a_recent < a_prior and a_recent <= max(1, a_prior // 2)

    reasons: list[str] = []
    # decline: withdrawal from a module that used to be active
    decline_votes = [v for v, ok in (("churn falling", churn_falling), ("net deletions", size_falling),
                                     ("authors leaving", authors_leaving)) if ok]
    if earlier_mean > low and level < earlier_mean and len(decline_votes) >= 2:
        phase = DECLINE
        reasons = decline_votes + [f"churn {level:.1f}/period vs {earlier_mean:.1f} earlier"]
        conf = "high" if (mk_c.p <= 0.05 or mk_s.p <= 0.05) and len(decline_votes) == 3 else ("medium" if len(decline_votes) >= 2 else "low")
    elif streak >= K and not churn_rising and not size_falling:
        phase = STABILIZING
        reasons = [f"{streak} consecutive low-churn periods (<= {low:g} commits)", f"size {mk_s.direction}"]
        conf = "high" if streak >= K + 2 else "medium"
    elif churn_rising or size_rising or level > low:
        phase = GROWTH
        reasons = [f"churn {mk_c.direction}", f"size {mk_s.direction}", f"{level:.1f} commits/period"]
        conf = "high" if (churn_rising or size_rising) and (mk_c.p <= 0.05 or mk_s.p <= 0.05) else ("medium" if level > 2 * low else "low")
    else:
        phase = GROWTH
        reasons = ["mixed signals; defaulting to growth (fail-safe against a false stabilizing label)"]
        conf = "low"

    open_key = current_period(cfg.scale)
    oc = data.get(open_key, (0, 0, 0))
    return PhaseResult(phase, None, conf, reasons, mk_c.direction, mk_s.direction, level, streak, a_recent, a_prior, n,
                       closed_through, w_axis, w_c, w_net,
                       {"period": open_key, "commits": oc[0], "added": oc[1], "deleted": oc[2]},
                       round(mk_c.p, 4), round(mk_s.p, 4))


def classify_module(store: Store, module: str, cfg: Config, now: datetime | None = None) -> PhaseResult:
    axis, data, authors = module_axis(store, module, cfg, now)
    result = _classify(axis, data, authors, cfg)
    if result.phase == NO_DATA:
        return result
    # phase_since: walk the end of the axis backwards until the label changes.
    # One dissenting period does not end a run (window classifiers flicker at phase boundaries);
    # two consecutive ones do.
    since = axis[-1]
    misses = 0
    for end in range(len(axis) - 1, cfg.phase_min_periods - 1, -1):
        sub = _classify(axis[:end], data, authors, cfg)
        if sub.phase != result.phase:
            misses += 1
            if misses > 1:
                break
            continue
        misses = 0
        since = axis[end - 1]
    result.since = since
    return result
