"""The MCP server: three tools over the shared incremental index.

The server owns nothing. Every tool is one read of the store (plus one
`git rev-parse HEAD` for the freshness stamp); the hook keeps the store
current. If the index is missing or stale the tools say so in the payload —
the gate fails safe to `warn` — rather than erroring, because an agent that
gets an exception simply proceeds without context.

Run: `senso mcp [repo]`, or register in a client:

    {"mcpServers": {"senso-lens": {"command": "senso", "args": ["mcp", "/path/to/repo"]}}}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .ingest.git_reader import GitRepo
from .serve.context import evolution_context, freshness
from .serve.explain import explain_project
from .serve.gate import check_change as _check_change
from .signals.phase import classify_module
from .store.db import open_store

INSTRUCTIONS = (
    "SENSO-Lens serves the evolution context of one Git repository: for each module its phase "
    "(growth, stabilizing, decline, no_data — from trend tests over closed periods of its own history), "
    "its connectedness (load-bearing or isolated), its co-change partners with decayed weights, maintainability "
    "and package metrics, with the commits behind each claim. Call get_evolution_context(module) before editing a "
    "module, check_change(diff) before applying an edit, and explain_phase() to see the whole map. Verdicts fail safe: "
    "a missing or stale index is reported as warn, never as ok."
)


def build_server(repo_root: str | Path) -> FastMCP:
    root = Path(repo_root).resolve()
    repo = GitRepo(root)
    mcp = FastMCP("senso-lens", instructions=INSTRUCTIONS, log_level="WARNING")

    def _store():
        return open_store(root)

    @mcp.tool()
    def get_evolution_context(module: str) -> dict[str, Any]:
        """Evolution context for one module (e.g. "core/parser"): phase with phase_since, confidence and reasons,
        connectedness, co-change partners, maintainability, package metrics, evidence commits, advice, index freshness.
        Use it before planning an edit; the module id is the first two path segments after src/lib/app."""
        store = _store()
        if store is None:
            return {"module": module, "known": False, "phase": "no_data", "reasons": ["no index: run `senso init`"],
                    "freshness": {"index_present": False, "stale": True}, "senso_lens": __version__}
        try:
            return evolution_context(store, module, repo)
        finally:
            store.close()

    @mcp.tool()
    def check_change(diff: str, allow_stale: bool = False) -> dict[str, Any]:
        """Fail-safe pre-edit gate for a unified diff (output of `git diff`). Returns verdict ok|warn|block, a 0-4
        deviation score, the rules that fired (SL-W01 add to stabilizing, SL-W02 add to declining, SL-W03 new coupling,
        SL-W04 coupling against trend, SL-F01 stale index ...), findings with evidence, and `authority`: modules where
        new code may go freely. `warn` means proceed only with a reason; `block` means the same warning has been ignored repeatedly."""
        store = _store()
        try:
            return _check_change(store, repo, diff, allow_stale=allow_stale)
        finally:
            if store is not None:
                store.close()

    @mcp.tool()
    def explain_phase(module: str | None = None, top: int = 8) -> dict[str, Any]:
        """Without a module: the whole repository — summary, phase map, module table, hotspots, strongest couplings,
        gate history, freshness. With a module: the full trend window behind that module's phase (commits and net lines
        per closed period, Mann-Kendall p-values, authors) and the rules that produced the label."""
        store = _store()
        if store is None:
            return {"summary": "no index: run `senso init`", "freshness": {"index_present": False, "stale": True},
                    "senso_lens": __version__}
        try:
            if module:
                cfg = store.load_config()
                if module not in set(store.modules()):
                    return {"module": module, "known": False, "reasons": ["module has no recorded history in the index"],
                            "freshness": freshness(store, repo), "senso_lens": __version__}
                ph = classify_module(store, module, cfg)
                return {"module": module, "known": True, **ph.to_dict(), "scale": cfg.scale,
                        "rules": {"window_periods": cfg.phase_window, "k_stable": cfg.phase_k_stable, "low_churn": cfg.phase_low_churn,
                                  "alpha": cfg.phase_alpha, "min_periods": cfg.phase_min_periods,
                                  "decline": "at least two of: churn falling, net deletions, authors leaving — after prior activity",
                                  "stabilizing": f"{cfg.phase_k_stable} consecutive closed periods at or below low churn, size not rising",
                                  "growth": "churn or size rising, or churn above the low threshold; ties resolve to growth with low confidence"},
                        "freshness": freshness(store, repo), "senso_lens": __version__}
            return explain_project(store, repo, top=top)
        finally:
            store.close()

    return mcp


def serve(repo_root: str | Path) -> None:
    build_server(repo_root).run(transport="stdio")


if __name__ == "__main__":  # python -m senso_lens.mcp_server [repo]
    import sys
    serve(sys.argv[1] if len(sys.argv) > 1 else ".")
