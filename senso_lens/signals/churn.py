"""Churn and hotspots.

Churn = commits (and lines) per module per period. A hotspot is churn weighted
by complexity (Tornhill): code that is both hard and actively changing. We score
over the trend window so the ranking reflects the present, not the whole past.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Config
from ..periods import last_closed_period, period_range
from ..store.db import Store


@dataclass
class Hotspot:
    module: str
    commits_window: int
    lines_window: int
    ccn_avg: float | None
    nloc: int | None
    score: float

    def to_dict(self) -> dict:
        return {"module": self.module, "commits": self.commits_window, "lines": self.lines_window,
                "ccn_avg": self.ccn_avg, "nloc": self.nloc, "score": round(self.score, 2)}


def hotspots(store: Store, cfg: Config, now: datetime | None = None, limit: int = 10) -> list[Hotspot]:
    ref = last_closed_period(cfg.scale, now)
    out: list[Hotspot] = []
    for module in store.modules():
        rows = store.module_series(module)
        if not rows:
            continue
        first = rows[0]["period"]
        axis = period_range(first, ref, cfg.scale)[-cfg.phase_window:]
        data = {r["period"]: r for r in rows}
        commits = sum(int(data[p]["commits"]) for p in axis if p in data)
        lines = sum(int(data[p]["added"]) + int(data[p]["deleted"]) for p in axis if p in data)
        mm = store.get_module_metrics(module)
        ccn = float(mm["ccn_avg"]) if mm and mm["ccn_avg"] is not None else None
        nloc = int(mm["nloc"]) if mm and mm["nloc"] is not None else None
        score = commits * (ccn if ccn else 1.0)
        if commits:
            out.append(Hotspot(module, commits, lines, ccn, nloc, score))
    out.sort(key=lambda h: -h.score)
    return out[:limit]
