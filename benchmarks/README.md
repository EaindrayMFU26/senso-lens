# Benchmarks

Everything here is produced by `senso replay` and `senso export`; nothing is hand-entered.

## Per-step update cost (RQ2 ablation)

```bash
senso replay /path/to/repo --mode both --limit 200 --out benchmarks/results/<repo>-replay.csv
```

One CSV row per `(mode, step)` along the first-parent chain of `HEAD` (a direct commit or a merged branch — the unit
a post-commit hook processes):

| column | meaning |
|---|---|
| `mode` | `shared` (one store) or `isolated` (three stores: history, metrics, packages; nothing shared) |
| `idx`, `hash`, `ts` | position, mainline commit, author time |
| `files`, `bucket` | source files touched by the step, size bucket `0 / 1-3 / 4-10 / 11-50 / 51+` |
| `t_git_ms` | time inside `git log` (the delta read) |
| `t_tier1_ms` | churn, authors, co-change writes |
| `t_tier2_ms` | parse of touched blobs, file/module metrics, imports |
| `t_tier3_ms` | package metrics for dirty modules |
| `total_ms`, `files_parsed` | sum of the four, blobs actually parsed |

`--limit N` warms the store up to the step before the window so every measured step is incremental, not cold.
The command prints the per-bucket median/p95 table used in `docs/EVALUATION.md`.

## Serve latency

```bash
python - <<'PY'
import time, statistics
from senso_lens.store.db import open_store
from senso_lens.ingest.git_reader import GitRepo
from senso_lens.serve.context import evolution_context
s, repo = open_store("."), GitRepo(".")
ts = sorted((lambda t: (evolution_context(s, m, repo), (time.perf_counter() - t) * 1000)[1])(time.perf_counter()) for m in s.modules())
print(f"p50 {statistics.median(ts):.1f} ms  p95 {ts[int(0.95 * (len(ts) - 1))]:.1f} ms  n={len(ts)}")
PY
```

## Results

`benchmarks/results/` is git-ignored; commit a summary table here when a run is final.

| repository | date | steps | shared 1–3 files | isolated 1–3 files | context p50 / p95 |
|---|---|---|---|---|---|
| pallets/click | 2026-10-02 | 40 | 50 ms | 104 ms | 12 / 24 ms |
| expressjs/express | 2026-10-02 | — | — | — | 20 / 57 ms |
