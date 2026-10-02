"""Co-change (logical) coupling with time decay.

Two modules that keep changing in the same commit are coupled whether or not an
import says so (Gall, Hajek & Jazayeri, 1998). The edge weight is an
exponentially decayed count — the slime-mould rule: reinforce what carries
change, let the rest fade — so a coupling that last fired three years ago does
not look like one that fired last month:

    weight = sum over periods p of count(p) * 0.5 ** (age(p) / half_life)

`age` is measured in periods back from the last closed period.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Config
from ..periods import last_closed_period, period_range, periods_between
from ..store.db import Store
from .trend import mann_kendall


@dataclass
class Partner:
    module: str
    weight: float            # decayed weight
    lifetime: int            # raw count over all time
    periods_active: int
    last_ts: int | None
    last_hash: str | None
    trend: str               # rising | flat | falling (per-period counts over the window)
    share: float             # fraction of this module's co-change weight carried by this partner

    def to_dict(self) -> dict:
        return {"module": self.module, "weight": round(self.weight, 2), "lifetime": self.lifetime,
                "periods_active": self.periods_active, "last_commit": self.last_hash,
                "trend": self.trend, "share": round(self.share, 3)}


def _age_in_periods(period: str, ref: str, scale: str) -> int:
    return max(0, periods_between(period, ref, scale))


def _decayed(rows: list[tuple[str, int]], ref: str, cfg: Config) -> float:
    hl = max(0.1, cfg.cochange_half_life_periods)
    return sum(c * (0.5 ** (_age_in_periods(p, ref, cfg.scale) / hl)) for p, c in rows)


def pair_weight(store: Store, a: str, b: str, cfg: Config, now: datetime | None = None) -> tuple[float, int]:
    """(decayed weight, lifetime count) for one pair."""
    ref = last_closed_period(cfg.scale, now)
    rows = [(r["period"], int(r["count"])) for r in store.cochange_pair(a, b)]
    return _decayed(rows, ref, cfg), sum(c for _, c in rows)


def partners_of(store: Store, module: str, cfg: Config, now: datetime | None = None, top_k: int | None = None) -> list[Partner]:
    ref = last_closed_period(cfg.scale, now)
    by_partner: dict[str, list[tuple[str, int, str | None, int | None]]] = {}
    for r in store.cochange_rows(module):
        other = r["module_b"] if r["module_a"] == module else r["module_a"]
        by_partner.setdefault(other, []).append((r["period"], int(r["count"]), r["last_hash"], r["last_ts"]))
    out: list[Partner] = []
    total = 0.0
    tmp = []
    for other, rows in by_partner.items():
        w = _decayed([(p, c) for p, c, _, _ in rows], ref, cfg)
        lifetime = sum(c for _, c, _, _ in rows)
        last = max(rows, key=lambda x: (x[3] or 0))
        # trend over the window: counts per period including zeros
        periods = sorted(p for p, _, _, _ in rows)
        counts = {p: c for p, c, _, _ in rows}
        start = periods[0]
        axis = period_range(start, ref, cfg.scale)[-cfg.phase_window:] if periods_between(start, ref, cfg.scale) >= 0 else []
        series = [float(counts.get(p, 0)) for p in axis]
        trend = mann_kendall(series, cfg.phase_alpha).direction if len(series) >= 3 else "flat"
        tmp.append((other, w, lifetime, len(rows), last[3], last[2], trend))
        total += w
    for other, w, lifetime, n_act, last_ts, last_hash, trend in tmp:
        if w < cfg.cochange_min_weight:
            continue
        out.append(Partner(other, w, lifetime, n_act, last_ts, last_hash, trend, (w / total) if total else 0.0))
    out.sort(key=lambda p: (-p.weight, p.module))
    k = top_k if top_k is not None else cfg.cochange_top_k
    return out[:k] if k else out
