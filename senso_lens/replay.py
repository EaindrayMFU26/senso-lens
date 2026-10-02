"""Commit-replay benchmark harness (RQ2): per-commit update cost, shared vs isolated.

Replays a repository's history one mainline step at a time (the first-parent
chain of HEAD: a direct commit or a merged branch) through the same pipeline the
post-commit hook uses, timing each tier. In `shared` mode one store receives
every signal family; in `isolated` mode three separate stores — churn+co-change,
file metrics, package metrics — each do their own git walk and their own parse,
which is exactly what three single-purpose tools do today. Nothing is shared
between the isolated stores, including the blob cache.

Output: one CSV row per (mode, step) with commits and files touched, a size
bucket and tier timings, plus a per-bucket median summary.
"""
from __future__ import annotations

import csv
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable

from .config import Config
from .ingest.git_reader import GitRepo
from .ingest.pipeline import update_index
from .store.db import Store

ISOLATED_GROUPS = {
    "history": frozenset({"churn", "cochange"}),
    "metrics": frozenset({"metrics"}),
    "packages": frozenset({"packages"}),
}

BUCKETS = [(0, 0, "0"), (1, 3, "1-3"), (4, 10, "4-10"), (11, 50, "11-50"), (51, 10**9, "51+")]


def bucket_of(files: int) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= files <= hi:
            return name
    return "0"


@dataclass
class ReplayRow:
    mode: str
    idx: int
    hash: str
    ts: int
    files: int
    bucket: str
    t_git_ms: float
    t_tier1_ms: float
    t_tier2_ms: float
    t_tier3_ms: float
    total_ms: float
    files_parsed: int


def _fresh_store(tmp: Path, name: str, cfg: Config, repo_root: Path) -> Store:
    s = Store(tmp / f"{name}.db")
    with s.tx():
        s.save_config(cfg)
        s.set_meta("repo_root", str(repo_root))
    return s


def replay(root: str | Path, mode: str = "both", limit: int | None = None, scale: str = "monthly",
           progress: Callable[[str, int, int], None] | None = None, warmup: bool = True) -> list[ReplayRow]:
    repo = GitRepo(root)
    # the first-parent chain of HEAD, oldest first: each step is one mainline commit or one merged
    # branch — exactly the unit a post-commit/post-merge hook processes, so ranges never overlap
    hashes = repo._run(["rev-list", "--first-parent", "--reverse", "HEAD"]).stdout.split()
    if limit:
        hashes = hashes[-limit:]
    cfg = Config(scale=scale)
    rows: list[ReplayRow] = []
    tmp = Path(tempfile.mkdtemp(prefix="senso-replay-"))
    try:
        modes = ["shared", "isolated"] if mode == "both" else [mode]
        for m in modes:
            if m == "shared":
                store = _fresh_store(tmp, "shared", cfg, repo.root)
                if warmup and limit:
                    # bring the store to the commit before the replay window so costs are incremental, not cold
                    before = repo._run(["rev-parse", f"{hashes[0]}^"], check=False).stdout.strip()
                    if before:
                        update_index(repo.root, store=store, until=before, parse_at=before, mode="warmup")
                for i, h in enumerate(hashes):
                    r = update_index(repo.root, store=store, until=h, parse_at=h, mode="replay-shared")
                    rows.append(ReplayRow(m, i, h, _ts(store, h), r.files_touched, bucket_of(r.files_touched), r.t_git_ms,
                                          r.t_tier1_ms, r.t_tier2_ms, r.t_tier3_ms, r.total_ms, r.files_parsed))
                    if progress:
                        progress(m, i + 1, len(hashes))
                store.close()
            else:
                stores = {g: _fresh_store(tmp, f"iso_{g}", cfg, repo.root) for g in ISOLATED_GROUPS}
                if warmup and limit:
                    before = repo._run(["rev-parse", f"{hashes[0]}^"], check=False).stdout.strip()
                    if before:
                        for g, fams in ISOLATED_GROUPS.items():
                            update_index(repo.root, store=stores[g], until=before, parse_at=before, families=fams, mode="warmup")
                for i, h in enumerate(hashes):
                    tg = t1 = t2 = t3 = 0.0
                    files = parsed = 0
                    for g, fams in ISOLATED_GROUPS.items():
                        r = update_index(repo.root, store=stores[g], until=h, parse_at=h, families=fams, mode=f"replay-iso-{g}")
                        tg += r.t_git_ms
                        t1 += r.t_tier1_ms
                        t2 += r.t_tier2_ms
                        t3 += r.t_tier3_ms
                        files = max(files, r.files_touched)
                        parsed += r.files_parsed
                    rows.append(ReplayRow(m, i, h, _ts(stores["history"], h), files, bucket_of(files), tg, t1, t2, t3,
                                          tg + t1 + t2 + t3, parsed))
                    if progress:
                        progress(m, i + 1, len(hashes))
                for s in stores.values():
                    s.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


def _ts(store: Store, h: str) -> int:
    row = store.conn.execute("SELECT ts FROM commits WHERE hash=?", (h,)).fetchone()
    return int(row["ts"]) if row else 0


def write_csv(rows: Iterable[ReplayRow], path: str | Path) -> None:
    rows = list(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else ["mode"])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def summarize(rows: list[ReplayRow]) -> list[dict]:
    out = []
    for mode in sorted({r.mode for r in rows}):
        for _, _, name in BUCKETS:
            sel = [r for r in rows if r.mode == mode and r.bucket == name]
            if not sel:
                continue
            out.append({
                "mode": mode, "bucket": name, "n": len(sel),
                "median_total_ms": round(statistics.median(r.total_ms for r in sel), 1),
                "p95_total_ms": round(sorted(r.total_ms for r in sel)[int(0.95 * (len(sel) - 1))], 1),
                "median_tier1_ms": round(statistics.median(r.t_tier1_ms for r in sel), 1),
                "median_tier2_ms": round(statistics.median(r.t_tier2_ms for r in sel), 1),
                "median_tier3_ms": round(statistics.median(r.t_tier3_ms for r in sel), 1),
                "median_git_ms": round(statistics.median(r.t_git_ms for r in sel), 1),
            })
    return out
