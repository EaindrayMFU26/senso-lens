"""The incremental pipeline: one git walk and one parse feed every signal family.

Tiers (each timed separately, which is the data RQ2 needs):
  tier 1 — synchronous per commit: churn counters, author sets, co-change edges
  tier 2 — touched files only: parse once per blob, file/module metrics, import edges
  tier 3 — dirty packages: Martin's Ca/Ce/instability/abstractness/distance

Invariants: per-commit writes are idempotent (INSERT OR IGNORE / upserts keyed by
hash); the bookmark moves only after the whole batch is written; rewritten history
(bookmark no longer an ancestor of HEAD) triggers a rebuild rather than a guess.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ..config import Config
from ..periods import choose_scale, period_key
from ..store.db import Store, store_path
from .git_reader import CommitEvent, GitError, GitRepo
from .modules import ModuleMapper
from .noise import classify_noise
from .parser import parse_source

ALL_FAMILIES = frozenset({"churn", "cochange", "metrics", "packages"})
Progress = Callable[[str, int, int | None], None]
PARALLEL_MIN_BLOBS = 64   # below this a process pool costs more than it saves


@dataclass
class UpdateResult:
    status: str
    from_hash: str | None
    to_hash: str
    commits: int = 0
    noise_commits: int = 0
    files_parsed: int = 0
    files_touched: int = 0
    t_git_ms: float = 0.0
    t_tier1_ms: float = 0.0
    t_tier2_ms: float = 0.0
    t_tier3_ms: float = 0.0
    scale: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def total_ms(self) -> float:
        return self.t_git_ms + self.t_tier1_ms + self.t_tier2_ms + self.t_tier3_ms


class ShallowRepositoryError(GitError):
    pass


def _author_id(ev: CommitEvent) -> str:
    return (ev.email or ev.author or "unknown").strip().lower()


def init_index(root: str | Path, scale: str = "auto", module_depth: int | None = None,
               cfg: Config | None = None, progress: Progress | None = None) -> UpdateResult:
    """Full mine: build the store from the whole history and set the bookmark."""
    repo = GitRepo(root)
    if repo.is_shallow():
        raise ShallowRepositoryError("shallow clone: run `git fetch --unshallow` first; a truncated history would produce wrong phases")
    store = Store(store_path(repo.root))
    repo.exclude_locally(".senso/")  # never let `git add -A` commit the index
    store.reset_history()
    cfg = cfg or store.load_config()
    if module_depth is not None:
        cfg.module_depth = module_depth
    if scale == "auto":
        cfg.scale = choose_scale(repo.commit_timestamps())
    else:
        cfg.scale = scale
    with store.tx():
        store.save_config(cfg)
        store.set_meta("repo_root", str(repo.root))
        store.set_meta("created_at", datetime.now(tz=timezone.utc).isoformat(timespec="seconds"))
    res = update_index(repo.root, store=store, progress=progress)
    res.status = "initialized" if res.status in ("updated", "rebuilt") else res.status
    return res


def update_index(root: str | Path, store: Store | None = None, until: str = "HEAD", parse_at: str | None = None,
                 families: Iterable[str] = ALL_FAMILIES, defer_tier3: bool = False,
                 progress: Progress | None = None, mode: str = "shared") -> UpdateResult:
    """Process commits bookmark..until. Returns timing per tier."""
    families = frozenset(families)
    repo = GitRepo(root)
    own_store = store is None
    store = store or Store(store_path(repo.root))
    cfg = store.load_config()
    mapper = ModuleMapper(cfg)
    started = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    head = repo.head() if until == "HEAD" else repo._run(["rev-parse", until]).stdout.strip()
    bookmark = store.bookmark
    status = "updated"
    notes: list[str] = []

    if bookmark == head:
        res = UpdateResult(status="up_to_date", from_hash=bookmark, to_hash=head, scale=cfg.scale)
        if not defer_tier3 and "packages" in families and store.dirty_modules():
            t3 = time.perf_counter()
            with store.tx():
                _tier3(store)
            res.t_tier3_ms = (time.perf_counter() - t3) * 1000
            res.status = "updated"
        if own_store:
            store.close()
        return res

    if bookmark and (not repo.commit_exists(bookmark) or not repo.is_ancestor(bookmark, head)):
        notes.append(f"history rewritten since bookmark {bookmark[:10]}; rebuilding from scratch")
        store.reset_history()
        bookmark = None
        status = "rebuilt"

    res = UpdateResult(status=status, from_hash=bookmark, to_hash=head, scale=cfg.scale, notes=notes)

    # ------------------------------------------------------------- tier 1 (+ git read)
    touched: dict[str, str] = {}       # new path -> last commit touching it
    removed_paths: set[str] = set()     # old paths from renames
    t_git = 0.0
    t1 = 0.0
    n_commits = 0
    n_noise = 0
    first_ts = int(store.get_meta("first_ts") or 0) or None
    last_ts = int(store.get_meta("last_ts") or 0) or None

    it = repo.iter_commits(since=bookmark, until=head)
    with store.tx():
        while True:
            g0 = time.perf_counter()
            try:
                ev = next(it)
            except StopIteration:
                t_git += time.perf_counter() - g0
                break
            t_git += time.perf_counter() - g0
            p0 = time.perf_counter()
            n_commits += 1
            if progress and n_commits % 200 == 0:
                progress("commits", n_commits, None)
            period = period_key(ev.ts, cfg.scale)
            first_ts = ev.ts if first_ts is None else min(first_ts, ev.ts)
            last_ts = ev.ts if last_ts is None else max(last_ts, ev.ts)

            per_module: dict[str, list[int]] = {}
            source_files = 0
            for fc in ev.files:
                if fc.old_path and fc.old_path != fc.path:
                    store.add_rename(fc.old_path, fc.path, ev.hash, ev.ts)
                    removed_paths.add(fc.old_path)
                if not mapper.is_source(fc.path):
                    continue  # churn and co-change are about code: docs, lockfiles and assets never form a module
                mod = mapper.module_of(fc.path)
                if mod is None:
                    continue
                source_files += 1
                touched[fc.path] = ev.hash
                agg = per_module.setdefault(mod, [0, 0, 0])
                agg[0] += 1
                agg[1] += fc.added
                agg[2] += fc.deleted

            reason = classify_noise(ev, cfg, source_files)
            store.add_commit(ev.hash, ev.ts, ev.author, ev.email, ev.subject, period, len(ev.files), ev.added,
                             ev.deleted, reason is not None, reason, len(per_module))
            if reason is not None:
                n_noise += 1
                t1 += time.perf_counter() - p0
                continue

            author = _author_id(ev)
            mods = sorted(per_module)
            for mod in mods:
                files, added, deleted = per_module[mod]
                store.add_commit_module(ev.hash, mod, files, added, deleted)
                if "churn" in families:
                    store.bump_module_period(mod, period, added, deleted, files, author)
                store.resolve_gate(mod)  # andon: a commit on the module answers its open warnings; escalation restarts
            if "cochange" in families and 1 < len(mods) <= cfg.max_changeset_modules:
                for i in range(len(mods)):
                    for j in range(i + 1, len(mods)):
                        store.bump_cochange(mods[i], mods[j], period, ev.hash, ev.ts)
            t1 += time.perf_counter() - p0

        if first_ts is not None:
            store.set_meta("first_ts", str(first_ts))
        if last_ts is not None:
            store.set_meta("last_ts", str(last_ts))

    res.commits, res.noise_commits = n_commits, n_noise
    res.t_git_ms, res.t_tier1_ms = t_git * 1000, t1 * 1000

    # ------------------------------------------------------------- tier 2
    need_parse = bool(families & {"metrics", "packages"})
    if need_parse:
        t2 = time.perf_counter()
        with store.tx():
            parsed, n_touched = _tier2(repo, store, mapper, touched, removed_paths, parse_at or head,
                                        full=(bookmark is None), progress=progress, families=families)
        res.files_parsed, res.files_touched = parsed, n_touched
        res.t_tier2_ms = (time.perf_counter() - t2) * 1000
    else:
        res.files_touched = len(touched)

    # ------------------------------------------------------------- tier 3
    if "packages" in families and not defer_tier3:
        t3 = time.perf_counter()
        with store.tx():
            _tier3(store)
        res.t_tier3_ms = (time.perf_counter() - t3) * 1000

    with store.tx():
        store.bookmark = head
        store.set_meta("head_branch", repo.branch() or "")
        store.log_update(started_at=started, from_hash=bookmark, to_hash=head, commits=n_commits,
                         files_parsed=res.files_parsed, t_git_ms=res.t_git_ms, t_tier1_ms=res.t_tier1_ms,
                         t_tier2_ms=res.t_tier2_ms, t_tier3_ms=res.t_tier3_ms, mode=mode)
    if own_store:
        store.close()
    return res


# ---------------------------------------------------------------------------------------------
def _parse_item(item: tuple[str, str, bytes]):
    return parse_source(item[1], item[2])


def _resolve_targets(mapper: ModuleMapper, src_path: str, targets: list[str], known: set[str]) -> set[str]:
    out: set[str] = set()
    for t in targets:
        mod = mapper.resolve_import_target(src_path, t, known)
        if mod is None and "/" in t and not t.startswith("."):
            # Go-style full import path: try progressively shorter suffixes
            parts = t.split("/")
            for k in range(1, len(parts)):
                mod = mapper.resolve_import_target(src_path, "/".join(parts[k:]), known)
                if mod:
                    break
        if mod:
            out.add(mod)
    return out


def _tier2(repo: GitRepo, store: Store, mapper: ModuleMapper, touched: dict[str, str], removed: set[str],
           rev: str, full: bool, progress: Progress | None, families: frozenset[str]) -> tuple[int, int]:
    tree = repo.ls_tree(rev)
    known = set(tree.keys())
    if full:
        paths = [p for p in known if mapper.is_source(p) and mapper.module_of(p) is not None]
    else:
        paths = [p for p in touched if p in known and mapper.module_of(p) is not None]
    affected_modules: set[str] = set()

    # deletions and renames
    for old in list(removed) + [p for p in touched if p not in known]:
        row = store.conn.execute("SELECT module FROM file_state WHERE path=?", (old,)).fetchone()
        if row:
            affected_modules.add(row["module"])
            store.delete_file_state(old)

    # parse once per blob (content-addressed: a file that moved or is duplicated is never parsed twice)
    to_parse: dict[str, str] = {}  # blob -> representative path
    for p in paths:
        blob = tree[p]
        if store.get_blob_metrics(blob) is None:
            to_parse.setdefault(blob, p)
    blobs = list(to_parse)
    parsed = 0
    CHUNK = 400
    pool = ProcessPoolExecutor() if len(blobs) >= PARALLEL_MIN_BLOBS and (os.cpu_count() or 1) > 1 else None
    try:
        for i in range(0, len(blobs), CHUNK):
            chunk = blobs[i:i + CHUNK]
            contents = repo.read_many_blobs(chunk)
            items = [(blob, to_parse[blob], contents[blob]) for blob in chunk if contents.get(blob) is not None]
            if pool is not None:
                results = pool.map(_parse_item, items, chunksize=8)
            else:
                results = map(_parse_item, items)
            for blob, m in zip((it[0] for it in items), results):
                if m is None:
                    m = {"language": None, "nloc": 0, "functions": 0, "ccn_avg": 0.0, "ccn_max": 0, "mi": None,
                         "classes": 0, "abstract_classes": 0, "imports": []}
                store.put_blob_metrics(blob, m)
                parsed += 1
            if progress:
                progress("parse", min(i + CHUNK, len(blobs)), len(blobs))
    finally:
        if pool is not None:
            pool.shutdown()

    # file state + imports
    import json as _json
    for p in paths:
        mod = mapper.module_of(p)
        if mod is None:
            continue
        blob = tree[p]
        prev = store.conn.execute("SELECT module FROM file_state WHERE path=?", (p,)).fetchone()
        if prev and prev["module"] != mod:
            affected_modules.add(prev["module"])
        store.set_file_state(p, mod, blob, touched.get(p))
        affected_modules.add(mod)
        if "packages" in families:
            bm = store.get_blob_metrics(blob)
            targets = _json.loads(bm["imports"]) if bm and bm["imports"] else []
            old_dsts = {r["dst_module"] for r in store.conn.execute("SELECT dst_module FROM imports WHERE src_path=?", (p,))}
            new_dsts = _resolve_targets(mapper, p, targets, known)
            store.replace_imports(p, mod, new_dsts)
            affected_modules |= (old_dsts ^ new_dsts)

    # module metrics for affected modules
    if "metrics" in families:
        for mod in affected_modules:
            _recompute_module_metrics(store, mod)
    if "packages" in families:
        neighbors: set[str] = set()
        for mod in affected_modules:
            for r in store.conn.execute("SELECT dst_module AS m FROM imports WHERE src_module=? UNION SELECT src_module FROM imports WHERE dst_module=?", (mod, mod)):
                neighbors.add(r["m"])
        store.mark_dirty(affected_modules | neighbors)
    return parsed, len(paths)


def _recompute_module_metrics(store: Store, module: str) -> None:
    rows = store.module_files(module)
    rows = [r for r in rows if r["blob"] is not None]
    if not rows:
        store.conn.execute("DELETE FROM module_metrics WHERE module=?", (module,))
        return
    files = len(rows)
    nloc = sum(int(r["nloc"] or 0) for r in rows)
    functions = sum(int(r["functions"] or 0) for r in rows)
    ccn_w = sum(float(r["ccn_avg"] or 0) * int(r["functions"] or 0) for r in rows)
    ccn_avg = (ccn_w / functions) if functions else 0.0
    ccn_max = max(int(r["ccn_max"] or 0) for r in rows)
    mi_w = sum(float(r["mi"]) * max(1, int(r["nloc"] or 0)) for r in rows if r["mi"] is not None)
    mi_n = sum(max(1, int(r["nloc"] or 0)) for r in rows if r["mi"] is not None)
    maint = (mi_w / mi_n) if mi_n else None
    classes = sum(int(r["classes"] or 0) for r in rows)
    abstract = sum(int(r["abstract_classes"] or 0) for r in rows)
    store.put_module_metrics(module, {"files": files, "nloc": nloc, "functions": functions, "ccn_avg": round(ccn_avg, 3),
                                      "ccn_max": ccn_max, "maintainability": round(maint, 2) if maint is not None else None,
                                      "classes": classes, "abstract_classes": abstract})


def _tier3(store: Store) -> None:
    """Martin's package metrics for every dirty module (exact: Ca/Ce are live counts)."""
    for mod in store.dirty_modules():
        ca, ce = store.fan_in(mod), store.fan_out(mod)
        inst = (ce / (ca + ce)) if (ca + ce) > 0 else None
        mm = store.get_module_metrics(mod)
        abstr = None
        if mm and mm["classes"]:
            abstr = (mm["abstract_classes"] or 0) / mm["classes"]
        dist = abs(abstr + inst - 1.0) if (abstr is not None and inst is not None) else None
        store.put_package_metrics(mod, ca, ce, inst, abstr, dist)
