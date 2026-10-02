"""explain_phase: the whole project in one glance — phase map, hotspots, coupling, gate history."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..ingest.git_reader import GitRepo
from ..store.db import Store
from ..signals.phase import classify_module, NO_DATA
from ..signals.cochange import partners_of
from ..signals.connectedness import connectedness_of
from ..signals.churn import hotspots
from .context import freshness
from .. import __version__


def explain_project(store: Store, repo: GitRepo | None = None, cfg: Config | None = None, now: datetime | None = None,
                    top: int = 8) -> dict[str, Any]:
    cfg = cfg or store.load_config()
    modules = store.modules()
    rows = []
    by_phase: dict[str, list[str]] = {"growth": [], "stabilizing": [], "decline": [], NO_DATA: []}
    for m in modules:
        ph = classify_module(store, m, cfg, now)
        conn = connectedness_of(store, m, cfg, now)
        mm = store.get_module_metrics(m)
        rows.append({
            "module": m, "phase": ph.phase, "since": ph.since, "confidence": ph.confidence,
            "connectedness": conn.status, "fan_in": conn.fan_in, "partners": conn.partners,
            "churn_level": round(ph.churn_level, 2), "churn_trend": ph.churn_trend, "size_trend": ph.size_trend,
            "nloc": mm["nloc"] if mm else None, "maintainability": mm["maintainability"] if mm else None,
        })
        by_phase.setdefault(ph.phase, []).append(m)

    # strongest couplings (deduplicated pairs)
    seen = set()
    couplings = []
    for m in modules:
        for p in partners_of(store, m, cfg, now, top_k=3):
            key = tuple(sorted((m, p.module)))
            if key in seen:
                continue
            seen.add(key)
            couplings.append({"modules": list(key), "weight": round(p.weight, 2), "lifetime": p.lifetime, "trend": p.trend})
    couplings.sort(key=lambda c: -c["weight"])

    counts = store.counts()
    since_ts = int(time.time()) - 30 * 86400
    gate = [{"module": r["module"], "rule": r["rule"], "count": r["n"], "max_deviation": r["max_dev"]} for r in store.gate_summary(since_ts)]
    first_ts, last_ts = store.get_meta("first_ts"), store.get_meta("last_ts")
    stable_lb = [r["module"] for r in rows if r["phase"] == "stabilizing" and r["connectedness"] == "load-bearing"]
    growth_iso = [r["module"] for r in rows if r["phase"] == "growth" and r["connectedness"] == "isolated"]

    summary = (
        f"{len(modules)} modules over {counts['commits']} commits ({counts['noise_commits']} filtered as noise), "
        f"{cfg.scale} periods. {len(by_phase['growth'])} growing, {len(by_phase['stabilizing'])} stabilizing, "
        f"{len(by_phase['decline'])} declining, {len(by_phase[NO_DATA])} with too little history."
    )
    if stable_lb:
        summary += f" Handle with care (stable and load-bearing): {', '.join(stable_lb[:5])}."
    if growth_iso:
        summary += f" Active but nothing depends on them yet: {', '.join(growth_iso[:5])}."

    return {
        "summary": summary,
        "repository": store.get_meta("repo_root"),
        "scale": cfg.scale,
        "history": {
            "first_commit": datetime.fromtimestamp(int(first_ts), tz=timezone.utc).date().isoformat() if first_ts else None,
            "last_commit": datetime.fromtimestamp(int(last_ts), tz=timezone.utc).date().isoformat() if last_ts else None,
            **counts,
        },
        "phases": {k: v for k, v in by_phase.items()},
        "modules": sorted(rows, key=lambda r: (r["phase"], -r["churn_level"]))[: max(top * 4, 40)],
        "hotspots": [h.to_dict() for h in hotspots(store, cfg, now, limit=top)],
        "strongest_couplings": couplings[:top],
        "gate_last_30_days": gate[:top],
        "freshness": freshness(store, repo),
        "senso_lens": __version__,
    }
