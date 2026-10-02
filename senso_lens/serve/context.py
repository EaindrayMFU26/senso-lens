"""get_evolution_context: the self-contained evidence packet for one module.

Task-scoped, evidence with every claim, token-budgeted. Nothing here touches
Git or a parser — every value is a read from the store — except the freshness
check, which is one `git rev-parse HEAD` (about a millisecond) so that a stale
index is reported as such rather than served as current.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..ingest.git_reader import GitRepo
from ..store.db import Store
from ..signals.phase import classify_module, NO_DATA
from ..signals.cochange import partners_of
from ..signals.connectedness import connectedness_of
from .. import __version__


def freshness(store: Store, repo: GitRepo | None) -> dict[str, Any]:
    """Epoch (bookmark) and how far behind HEAD it is — a TLE-style age stamp."""
    bookmark = store.bookmark
    info: dict[str, Any] = {"bookmark": bookmark, "bookmark_at": store.get_meta("bookmark_at"), "head": None,
                            "commits_behind": None, "stale": None, "index_present": bookmark is not None}
    if repo is None or bookmark is None:
        info["stale"] = True if bookmark is None else None
        return info
    try:
        head = repo.head()
        info["head"] = head
        if head == bookmark:
            info["commits_behind"], info["stale"] = 0, False
        elif repo.is_ancestor(bookmark, head):
            info["commits_behind"] = repo.count_commits(f"{bookmark}..{head}")
            info["stale"] = True
        else:
            info["commits_behind"], info["stale"], info["rewritten"] = None, True, True
    except Exception as exc:  # pragma: no cover - git unavailable
        info["stale"], info["error"] = True, str(exc)
    return info


def _advice(phase: str, conn_status: str, partners: list, module: str) -> str:
    top = partners[0].module if partners else None
    if phase == "stabilizing":
        if top:
            return f"Prefer extending a caller ({top}) over adding code to {module}; it has been stable and others depend on it."
        return f"Prefer extending a caller over adding code to {module}; it has been stable."
    if phase == "decline":
        return f"{module} is being withdrawn from (shrinking, fewer authors). Route new behaviour to its successor; keep changes here to fixes and removals."
    if phase == NO_DATA:
        return f"Too little history to judge {module}; treat as new code and add tests with any change."
    if conn_status == "isolated":
        return (f"{module} is in active development; nothing in the repository imports it or changes with it, "
                f"so it is an entry point or new code — new code belongs here, keep it decoupled.")
    return f"{module} is in active development; new code belongs here. Keep coupling to its usual partners only."


def evolution_context(store: Store, module: str, repo: GitRepo | None = None, cfg: Config | None = None,
                      now: datetime | None = None, evidence_commits: int = 3) -> dict[str, Any]:
    cfg = cfg or store.load_config()
    known = module in set(store.modules())
    fresh = freshness(store, repo)
    if not known:
        return {
            "module": module, "known": False, "phase": NO_DATA, "confidence": None,
            "reasons": ["module has no recorded history in the index"],
            "advice": _advice(NO_DATA, "isolated", [], module),
            "freshness": fresh, "senso_lens": __version__,
        }
    ph = classify_module(store, module, cfg, now)
    partners = partners_of(store, module, cfg, now)
    conn = connectedness_of(store, module, cfg, now)
    mm = store.get_module_metrics(module)
    pm = store.get_package_metrics(module)
    recent = store.module_recent_commits(module, evidence_commits)
    total_commits = store.conn.execute("SELECT COUNT(*) AS n FROM commit_modules WHERE module=?", (module,)).fetchone()["n"]

    payload: dict[str, Any] = {
        "module": module,
        "known": True,
        **ph.to_dict(),
        "connectedness": conn.to_dict(),
        "co_changes_with": [p.to_dict() for p in partners],
        "maintainability": None if not mm else {
            "composite": mm["maintainability"], "files": mm["files"], "nloc": mm["nloc"], "functions": mm["functions"],
            "ccn_avg": mm["ccn_avg"], "ccn_max": mm["ccn_max"],
        },
        "package_metrics": None if not pm or pm["ca"] is None else {
            "ca": pm["ca"], "ce": pm["ce"], "instability": None if pm["instability"] is None else round(pm["instability"], 3),
            "abstractness": None if pm["abstractness"] is None else round(pm["abstractness"], 3),
            "distance": None if pm["distance"] is None else round(pm["distance"], 3), "stale": bool(pm["dirty"]),
        },
        "evidence": {
            "commits_analyzed": int(total_commits),
            "reference_commits": [{"hash": r["hash"][:10], "when": datetime.fromtimestamp(r["ts"], tz=timezone.utc).date().isoformat(),
                                   "subject": (r["subject"] or "")[:80], "added": r["added"], "deleted": r["deleted"]} for r in recent],
            "scale": cfg.scale,
        },
        "advice": _advice(ph.phase, conn.status, partners, module),
        "freshness": fresh,
        "senso_lens": __version__,
    }
    return payload
