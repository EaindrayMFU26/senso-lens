# SENSO-Lens

**Evolution context for AI coding agents — from one shared incremental index over a repository's Git history, served in one sub-second call.**

A coding agent sees the code as it is *now*. It does not see that `core/parser` has not changed in a year and
everything depends on it, that `legacy/auth` is being emptied out into `auth/service`, or that `api/handlers`
and `core/parser` used to change together and stopped. SENSO-Lens mines that history once, keeps the index
current with a post-commit hook, and answers three questions over MCP:

| tool | question it answers |
|---|---|
| `get_evolution_context(module)` | What phase is this module in (**growth / stabilizing / decline**), since when, how sure are we, what changes with it, is it load-bearing — with the commits behind every claim |
| `check_change(diff)` | Before applying an edit: does it add code to a stable or declining module, or couple modules that never changed together? Verdict `ok / warn / block`, fail-safe |
| `explain_phase()` | The whole repository: phase map, hotspots, strongest couplings, gate history — or the full trend window behind one module's label |

> Senior project, School of Applied Digital Technology, Mae Fah Luang University — team **4musketeerz**, advisor Dr. Nacha Chondamrongkul.
> A slice of the SENSO program on software evolution analysis ([cnacha-mfu/senso-framework](https://github.com/cnacha-mfu/senso-framework)).

## Quick start

```bash
pip install -e ".[mcp]"          # Python 3.11+, git on PATH
cd /path/to/your/repo
senso init .                      # mine the history once -> .senso/index.db  (2k commits ≈ 1.5 s)
senso hook install .              # post-commit hook keeps the index current (one commit ≈ 50–150 ms)
senso explain .                   # phase map of the repository
senso context core/parser         # what an agent receives for one module
git diff | senso check .          # the gate on your working changes (exit 0 ok, 1 warn, 2 block)
```

Register the MCP server in Claude Code, Cursor or any MCP client:

```json
{ "mcpServers": { "senso-lens": { "command": "senso", "args": ["mcp", "/path/to/your/repo"] } } }
```

Every command takes `--json` and prints exactly what the MCP tool returns.

## What an agent receives

```jsonc
{
  "module": "core/parser",
  "phase": "stabilizing", "phase_since": "2025-11", "confidence": "high",
  "reasons": ["17 consecutive low-churn periods (<= 1 commits)", "size rising"],
  "trends": { "churn": {"direction": "flat", "last_periods": [0,0,0,1,0,0,0,1], "p": 0.40},
              "size":  {"direction": "rising", "net_lines_last_periods": [0,0,0,2,0,0,0,2], "p": 0.01},
              "authors": {"recent": 1, "prior": 2} },
  "window": { "periods": ["2026-02", "…", "2026-09"], "closed_through": "2026-09", "churn_level": 0.25, "low_streak": 17 },
  "connectedness": { "status": "load-bearing", "fan_in": 1, "cochange_partners": 0 },
  "co_changes_with": [],
  "maintainability": { "composite": 15.25, "files": 2, "nloc": 326, "ccn_avg": 2.79, "ccn_max": 3 },
  "package_metrics": { "ca": 1, "ce": 0, "instability": 0.0, "abstractness": null, "distance": null },
  "evidence": { "commits_analyzed": 61, "reference_commits": [{"hash": "5c50063c81", "when": "2026-09-05", "subject": "fix: parser edge case 5"}] },
  "advice": "Prefer extending a caller over adding code to core/parser; it has been stable.",
  "freshness": { "bookmark": "47c45391…", "head": "47c45391…", "commits_behind": 0, "stale": false }
}
```

And the gate, on a diff that adds 30 lines to that module:

```
WARN deviation 3/4 · [SL-W01] core/parser has been stabilizing since 2025-11 (17 low-churn periods) and this change adds +30 net lines.
```

## How it works

```
 git history ──► commit-delta reader ──► tier 1  churn · authors · co-change edges        (per commit, synchronous)
   (bookmark..HEAD)       │              tier 2  parse touched files once per blob         (metrics, imports)
                          │              tier 3  package metrics for dirty modules         (lazy)
                          ▼
                   .senso/index.db  (SQLite, one per repository)  ──►  get_evolution_context · check_change · explain_phase
```

* **One shared incremental index.** A `HEAD` bookmark marks what has been processed. Each update reads only
  `bookmark..HEAD` (`git log --reverse --no-merges -M --numstat`), writes idempotently, and moves the bookmark
  last. Rewritten history (bookmark no longer an ancestor of HEAD) triggers a rebuild; shallow clones are refused.
  Every signal family reads the *same* walk and the *same* parse, which is what three separate tools cannot do.
* **Discretization** (the UTG step applied to a repository): commits become per-period counts on a calendar axis
  (weekly / monthly / quarterly / yearly). `senso init` picks the finest scale with no empty period over the last
  three years (the time-gap rule); the open, partial period is never used in a trend test.
* **Phase** from Mann-Kendall trend tests over a window of 8 closed periods: *stabilizing* = 4+ consecutive low-churn
  periods with size not falling; *decline* = at least two of {churn falling, net deletions, authors leaving} after
  prior activity; otherwise *growth*; fewer than 4 periods = *no_data*. Ties resolve to growth with low confidence,
  because a false "stabilizing" is the costlier error for an agent. `phase_since` comes from a backward scan with
  one-period hysteresis.
* **Co-change coupling with decay**: edge weight = Σ count · 0.5^(age / 4 periods). What carries change is
  reinforced, what stopped fades — a coupling that last fired three years ago is not one that fired last month.
* **Connectedness** (load-bearing vs isolated) is a second axis, never folded into phase: fan-in from the import
  graph (test modules excluded) plus co-change partners.
* **Fail-safe gate**: `ok` must be proven (index present and fresh, phases known, no rule fired). Missing or stale
  index ⇒ `warn` with the reason. `block` only by escalation — the same rule on the same module unresolved three
  times in 24 h; a commit on the module resets it. Rules have stable ids (`SL-W01` …), tunable severities and
  suppressions that need a reason, an author and an expiry (`senso.rules.toml`).

Design notes: [docs/DESIGN.md](docs/DESIGN.md) · rule deck: [docs/RULES.md](docs/RULES.md) · evaluation plan and
results: [docs/EVALUATION.md](docs/EVALUATION.md).

## Command line

| command | purpose |
|---|---|
| `senso init [path] [--scale auto\|weekly\|monthly\|quarterly\|yearly] [--depth N]` | full mine; creates the index and picks the period scale |
| `senso update [path]` | process `bookmark..HEAD` (what the hook runs); `--quiet` for hooks |
| `senso hook install \| remove [path]` | idempotent post-commit hook, chains with an existing one, never fails a commit |
| `senso status [path]` | index present? fresh? counts and last update timings |
| `senso context MODULE [path]` | `get_evolution_context` |
| `senso check [path] --diff FILE \| --staged \| --working \| (stdin)` | `check_change`; exit code = verdict; `--allow-stale`, `--no-record` |
| `senso explain [path] [-m MODULE]` | `explain_phase` |
| `senso rules [path] [--init]` | the rule deck with effective severities and suppressions; `--init` writes an example file |
| `senso export [path] --format csv\|json --out DIR` | dump every table and the phase labels for analysis |
| `senso replay [path] --mode shared\|isolated\|both --limit N` | RQ2 benchmark: per-step update cost, one index vs three isolated ones |
| `senso mcp [path]` | serve the three tools over MCP (stdio) |

## Current numbers (this container, single core unless noted)

| repository | commits (non-merge) | `senso init` | `get_evolution_context` p50 / p95 | `check_change` | `explain_phase` |
|---|---|---|---|---|---|
| scripted fixture (`tests/fixture_repo.py`) | 284 | 0.2 s | 3 / 6 ms | 3 ms | 16 ms |
| pallets/click | 2 171 | 1.4 s | 12 / 24 ms | 2 ms | 31 ms |
| expressjs/express | 5 688 | 1.6 s | 20 / 57 ms | 38 ms | 247 ms |

Per-step update cost on click (last 40 mainline steps, median): **shared 50 ms** vs **isolated 104 ms** for 1–3 file
steps, 169 vs 320 ms for 4–10 files — the same code run as one index versus as three single-purpose indexes.
`senso replay` reproduces the table; see [docs/EVALUATION.md](docs/EVALUATION.md) for what the numbers do and do not show.

## Development

```bash
pip install -e ".[dev,mcp]"
pytest -q                                  # 39 tests: unit, fixture-repo integration, gate, CLI, MCP, reference agreement
python tests/fixture_repo.py demo-repo     # the scripted repository with known phases, for manual exploration
```

The fixture is a three-year history with a stabilizing load-bearing parser, a growing API, a declining `legacy/auth`
with its successor `auth/service`, an isolated scripts module, bot noise and a rename — every test asserts against
what the script did, and `tests/test_reference.py` checks churn and co-change counts against a direct `git log`
recount and complexity against radon.

## Scope and honesty

* Modules are directories (depth 2 after stripping `src/`, `lib/` …). Git cannot see runtime wiring, so *isolated*
  means "nothing in the repository imports it or changes with it"; entry points look isolated.
* Phase is a trajectory read from history, not a judgement of quality. A module that finished declining and went
  silent reads as *stabilizing* (and usually isolated): decline is the withdrawal, not the grave.
* The maintainability composite is a documented, Halstead-free Maintainability Index; its predictive validity is
  out of scope for this project.
* Whether evolution context changes what an agent *writes* (RQ3) is the semester-2 experiment; semester 1 delivers
  the index, the signals, the gate and their measurement.

MIT License — see [LICENSE](LICENSE).
