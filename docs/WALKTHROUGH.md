# SENSO-Lens — code walkthrough

A guided tour of the repository for someone who has to work on it. Read `README.md` first (what it is),
`docs/DESIGN.md` for *why* each decision was made, and this file for *where things are and how the data flows*.
Line numbers are not given on purpose; function names are, and they are stable.

Contents

1. [The shape of the repository](#1-the-shape-of-the-repository)
2. [The data flow in one picture](#2-the-data-flow-in-one-picture)
3. [`config.py` — the knobs](#3-configpy--the-knobs)
4. [`periods.py` — discretization](#4-periodspy--discretization)
5. [`ingest/` — Git → index](#5-ingest--git--index)
6. [`store/db.py` — the index itself](#6-storedbpy--the-index-itself)
7. [`signals/` — counters → statements](#7-signals--counters--statements)
8. [`serve/` — the three tools](#8-serve--the-three-tools)
9. [`cli.py`, `mcp_server.py` — the two front doors](#9-clipy-mcp_serverpy--the-two-front-doors)
10. [`replay.py` — the RQ2 instrument](#10-replaypy--the-rq2-instrument)
11. [`tests/` — the evidence](#11-tests--the-evidence)
12. [Tracing one call end to end](#12-tracing-one-call-end-to-end)
13. [Where to change things](#13-where-to-change-things)
14. [How the code maps to the research question](#14-how-the-code-maps-to-the-research-question)

---

## 1. The shape of the repository

```
senso-lens/
├── senso_lens/                 the package (installed as the `senso` command)
│   ├── __init__.py             __version__, SCHEMA_VERSION
│   ├── config.py               every tunable knob; noise, ignore and test-module patterns
│   ├── periods.py              the time axis: calendar discretization, time-gap rule, open window
│   ├── ingest/                 Git → index   (the WRITE path: `senso init`, `senso update`, the hook)
│   │   ├── git_reader.py       commit-delta reader, bookmark checks, tree/blob reads, diffs
│   │   ├── modules.py          path → module, ignore rules, import resolution, test-module detection
│   │   ├── noise.py            real/bogus commit filter
│   │   ├── parser.py           one parse per file content: NLOC, CCN, MI-lite, classes, imports
│   │   └── pipeline.py         init_index / update_index — the three tiers
│   ├── store/db.py             the SQLite schema and every read/write the system performs
│   ├── signals/                index → meaning  (pure reads; never touches Git or a parser)
│   │   ├── trend.py            Mann-Kendall + Sen's slope
│   │   ├── phase.py            growth / stabilizing / decline / no_data, phase_since
│   │   ├── cochange.py         decayed logical coupling, partners
│   │   ├── connectedness.py    load-bearing vs isolated
│   │   └── churn.py            hotspots (churn × complexity)
│   ├── serve/                  meaning → the three tools
│   │   ├── context.py          get_evolution_context payload + freshness stamp
│   │   ├── rules.py            the SL rule deck, severities, suppressions (senso.rules.toml)
│   │   ├── gate.py             check_change: diff parser, rules, deviation, escalation
│   │   └── explain.py          explain_phase: the whole-repository view
│   ├── cli.py                  the `senso` command — every command has --json
│   ├── mcp_server.py           the MCP server exposing the three tools (mcp 1.x and 2.x)
│   └── replay.py               RQ2 harness: shared vs isolated update cost, same code
├── tests/                      39 tests; fixture_repo.py scripts a repository with known phases
├── docs/                       DESIGN.md · RULES.md · EVALUATION.md · WALKTHROUGH.md (this file)
├── benchmarks/README.md        how the numbers are produced; results are git-ignored
├── .github/workflows/ci.yml    pytest on Python 3.11 and 3.12, then a smoke run on the repo itself
├── pyproject.toml              runtime deps: typer, rich, lizard · extras: mcp, dev (pytest, radon)
└── senso.rules.example.toml    an example rule deck
```

Sizes, so you know what you are getting into: `pipeline.py` 366 lines, `db.py` 345, `gate.py` 304, `cli.py` 544,
`phase.py` 171; everything else is under 220 lines. The whole package is about 2 900 lines.

**The one structural rule.** `ingest/` writes the store; `signals/` and `serve/` only read it. The agent's read path
never spawns a parser and runs exactly one Git command (`git rev-parse HEAD`, for the freshness stamp). That
separation is what makes the sub-second call possible, and it is where "the shared incremental index" physically
lives: `store/db.py` (the tables) plus `ingest/pipeline.py` (the only writer).

## 2. The data flow in one picture

```
                 WRITE PATH (hook, `senso update`)                        READ PATH (agent, `senso context` …)
                 ─────────────────────────────────                        ──────────────────────────────────
 git log bookmark..HEAD ─► git_reader.iter_commits ─► CommitEvent         get_evolution_context(module)
        (one walk, -M --numstat, no merges)              │                        │
                                                         ▼                        ▼
                                  modules.module_of · noise.classify_noise   signals.phase.classify_module
                                                         │                   signals.cochange.partners_of
                     tier 1  module_period · authors · cochange · commits     signals.connectedness.connectedness_of
                                                         │                        │
                     tier 2  parser.parse_source once per BLOB ──► blob_metrics,   ▼
                             file_state, imports, module_metrics             serve.context.evolution_context
                                                         │                   serve.gate.check_change
                     tier 3  package_metrics for DIRTY modules               serve.explain.explain_project
                                                         │                        │
                              meta.bookmark = HEAD (last)                         ▼
                                                         │                 cli.py (--json) · mcp_server.py
                                                         ▼
                                              .senso/index.db  ◄──────────── SELECTs only
```

## 3. `config.py` — the knobs

`Config` is a dataclass that `senso init` saves *into the index* (`meta.config`), so a repository's settings travel
with its database and every later read uses the same values the counters were built with.

```python
scale = "monthly"                 # weekly | monthly | quarterly | yearly   (set by the time-gap rule at init)
module_depth = 2                  # directories that define a module, after stripping src/ lib/ app/ pkg/ packages/
max_changeset_modules = 30        # commits touching more modules than this form no co-change edges
max_files_per_commit = 500        # more files than this -> bulk noise
cochange_half_life_periods = 4.0  # decay of co-change weight
cochange_min_weight = 1.0         # partners below this decayed weight are not reported
cochange_top_k = 5
phase_window = 8                  # W closed periods examined by the trend tests
phase_min_periods = 4             # fewer closed periods since first activity -> no_data
phase_low_churn = 1.0             # <= this many commits/period is "low"
phase_k_stable = 4                # K consecutive low periods -> stabilizing
phase_alpha = 0.10                # Mann-Kendall significance for a direction
load_bearing_fan_in = 1           # non-test importers needed for load-bearing
load_bearing_partners = 2         # or this many co-change partners
gate_small_addition = 10          # net lines that make a change an "addition"
gate_large_addition = 100         # net lines that add +1 deviation
gate_escalation_count = 3         # same rule + module unresolved this many times -> block
gate_escalation_hours = 24.0
```

The module-level constants are the pattern lists: `SOURCE_EXTENSIONS` (what counts as code), `IGNORE_PATH_PATTERNS`
(vendored, generated, lockfiles, binaries, migrations, snapshots), `TEST_MODULE_PATTERNS`, `BOT_AUTHOR_PATTERNS`,
`NOISE_SUBJECT_PATTERNS`. When someone asks "why 8 periods / why α = 0.10 / why half-life 4", the answer is: a
documented default here, overridable per repository, and one of the things the evaluation varies.

## 4. `periods.py` — discretization

Commits are events; everything downstream works on **periods**. This is the UTG discretization step applied to a
repository: events are bucketed into snapshots at a level Δ, and Δ is chosen per repository.

```python
period_key(ts, "weekly")    -> "2026-W05"     # ISO week
period_key(ts, "monthly")   -> "2026-03"
period_key(ts, "quarterly") -> "2026-Q1"
period_key(ts, "yearly")    -> "2026"
```

Keys are calendar units (so "since 2025-Q4" means what a reader expects) and sort as strings. The functions that
carry the research idea:

| function | role |
|---|---|
| `choose_scale(timestamps, lookback_years=3)` | the **time-gap rule**: over the most recent three years, the finest scale in which no period is empty, requiring at least four periods; otherwise `yearly`. click and express → quarterly; the fixture → monthly. |
| `current_period`, `last_closed_period` | the **open window is excluded** from every trend test; the partial current period is reported separately as `open_period`. |
| `period_index`, `period_from_index`, `periods_between`, `period_range` | integer ordinals for period arithmetic (monthly = `year*12 + month-1`, weekly = ISO-week start ordinal // 7). Replacing string/datetime stepping with this took `explain_phase` on express from 1.7 s to 0.25 s. |

## 5. `ingest/` — Git → index

### `git_reader.py` — the commit-delta reader

`GitRepo(root)` wraps the few Git calls the system is allowed to make. The heart is `iter_commits(since, until)`:

```python
rev  = f"{since}..{until}" if since else until
args = ["log", "--reverse", "--no-merges", "-M", "--numstat", f"--format={fmt}", rev]
```

One process, one pass, oldest first, merges skipped, renames detected (`-M`); it streams
`CommitEvent(hash, ts, author, email, subject, parents, files=[FileChange(path, added, deleted, old_path, binary)])`
and `parse_numstat_path` unbraces rename paths like `src/{old => new}/x.py`. Everything else is bookkeeping the design
depends on:

- `head()`, `branch()`, `commit_exists()`, `is_ancestor(bookmark, head)` — the rewritten-history check
  (`git merge-base --is-ancestor`), `count_commits(range)` — how far behind a stale index is.
- `is_shallow()` — `init` refuses shallow clones.
- `ls_tree(rev)` — path → blob hash for the whole tree at a revision; `read_many_blobs(blobs)` — one
  `git cat-file --batch` for a chunk of blobs (tier 2 never spawns one process per file).
- `diff_staged()`, `diff_working()` — inputs for `senso check --staged / --working`.
- `hooks_dir()`, `exclude_locally(".senso/")` — hook installation and keeping the index out of `git add -A`.

### `modules.py` — what a module is

`ModuleMapper.module_of(path)`: strip one leading `src/ lib/ app/ pkg/ packages/` **only when a directory follows
it**, then keep the first `module_depth` directories; files at the repository root form `(root)`; ignored paths return
`None`. Examples: `src/core/parser/lexer.py → core/parser`, `lib/express.js → lib`, `lib/router/index.js → router`,
`node_modules/x.js → None`. `is_source(path)` is an extension check. `is_test_module(module)` recognises `tests/`,
`__tests__/`, `spec/`, `test_*` … so that test imports never make production code look load-bearing.
`resolve_import_target(src_path, target, known_paths)` turns an import string — `core.parser`, `./button`,
`github.com/org/repo/pkg/x` (with a suffix fallback in the pipeline) — into the module of a real file in the tree, or
`None` for third-party imports.

### `noise.py` — real or bogus

`classify_noise(ev, cfg, source_files)` returns a reason or `None`, checked in this order: `bot_author`
(dependabot, renovate, pre-commit-ci, `[bot]` …), `maintenance_subject` (`Bump …`, `Release 8.3.3`, `apply black`,
`update dependencies` …), `bulk_changeset` (> `max_files_per_commit`), `no_source_files`. Noise commits are stored
with their reason but excluded from churn, co-change and phase. On click 40 % of non-merge commits are noise, two
thirds of them docs-only.

### `parser.py` — the one parse

`parse_source(path, data) -> dict | None`. lizard supplies NLOC and per-function cyclomatic complexity for every
language, so numbers are comparable across a polyglot repository. Python additionally goes through `ast`
(`_python_structure`) for classes, abstract classes (ABC / Protocol / `@abstractmethod` / metaclass) and imports,
including relative ones resolved against the file's package; other languages use regexes (`_generic_structure`) for
imports and class-like declarations. The maintainability value is

```python
mi_lite(nloc, ccn_total, functions) = clamp((171 − 0.23·CC − 16.2·ln LOC) · 100/171, 0, 100)
```

the SEI/Oman Maintainability Index without the Halstead volume term, labelled `mi_kind = "mi_lite"` in the payload.
radon's full MI was removed from the runtime (≈ 170 ms per 2 000-line file, and it returns 0 for any large file);
radon is kept as a *reference implementation* in `tests/test_reference.py`. Files over 2 MB are recorded but not parsed.

### `pipeline.py` — the shared incremental update (the essential file)

`init_index(root, scale="auto", module_depth=None)` refuses shallow clones, calls `exclude_locally(".senso/")`,
`reset_history()` (keeps the blob cache), picks the scale, saves config/repo_root/created_at, and delegates to
`update_index`. `update_index` is the function the post-commit hook runs:

```python
head = repo.head(); bookmark = store.bookmark
if bookmark == head:                                    # nothing new; run tier 3 if anything is dirty
    return UpdateResult("up_to_date", ...)
if bookmark and (not repo.commit_exists(bookmark) or not repo.is_ancestor(bookmark, head)):
    store.reset_history(); bookmark = None; status = "rebuilt"       # rebase / force-push: never guess

# ---- tier 1: one git walk; git time and processing time are measured separately
for ev in repo.iter_commits(since=bookmark, until=head):
    period = period_key(ev.ts, cfg.scale)
    per_module = {}                                     # module -> [files, added, deleted], SOURCE files only
    for fc in ev.files:
        if fc.old_path: store.add_rename(...); removed_paths.add(fc.old_path)
        if not mapper.is_source(fc.path): continue
        mod = mapper.module_of(fc.path); touched[fc.path] = ev.hash; aggregate into per_module
    reason = classify_noise(ev, cfg, source_files)
    store.add_commit(ev.hash, ..., is_noise=reason is not None, reason, len(per_module))
    if reason: continue
    for mod in mods:
        store.add_commit_module(...); store.bump_module_period(mod, period, added, deleted, files, author)
        store.resolve_gate(mod)                         # andon: a commit on the module answers its open warnings
    if 1 < len(mods) <= cfg.max_changeset_modules:
        for every pair (a, b) of mods: store.bump_cochange(a, b, period, ev.hash, ev.ts)

# ---- tier 2 (if "metrics" or "packages" requested): _tier2(...)
#      ls_tree once; delete file_state for removed/renamed paths; parse each blob not yet in blob_metrics
#      (process pool when >= 64 blobs); set file_state; replace_imports; recompute module metrics for
#      affected modules; mark affected modules + their import neighbours dirty
# ---- tier 3 (if "packages" requested): _tier3(store) -> Ca/Ce/instability/abstractness/distance for dirty modules

store.bookmark = head                                   # moves LAST: a crash leaves the previous consistent state
store.log_update(started_at, from_hash, to_hash, commits, files_parsed, t_git_ms, t_tier1_ms, t_tier2_ms, t_tier3_ms, mode)
```

Two parameters matter for the evaluation: `families` (a subset of `{churn, cochange, metrics, packages}`) lets a
caller run only some signal families — that is how `replay.py` builds the "three isolated tools" baseline from the
*same* code; and the returned `UpdateResult` carries the per-tier timings that appear in `senso status`, the
`update_log` table and the replay CSV. `_recompute_module_metrics` aggregates file rows into a module row
(NLOC-weighted MI, function-weighted CCN average, max CCN). `_tier3` is Martin's formulas verbatim:
`I = Ce/(Ca+Ce)`, `A = abstract/classes`, `D = |A + I − 1|`.

## 6. `store/db.py` — the index itself

One SQLite file per repository at `.senso/index.db`, WAL mode, created by `Store(path)` from the `SCHEMA` string.
The tables *are* the design:

| tier | tables | keyed by | written when |
|---|---|---|---|
| meta | `meta` — bookmark, bookmark_at, config (JSON), first_ts, last_ts, repo_root, head_branch, schema_version | key | every update |
| 1 | `commits` (hash, ts, author, period, files, added, deleted, is_noise, noise_reason, modules) · `commit_modules` · `module_period` (commits / added / deleted / files per module per period) · `module_period_authors` · `cochange` (module_a < module_b, period, count, last_hash, last_ts) · `renames` | hash, (module, period), (a, b, period) | per commit, synchronously |
| 2 | `blob_metrics` (per **content hash**: language, nloc, functions, ccn_avg, ccn_max, mi, classes, abstract_classes, imports JSON) · `file_state` (path → module, blob, updated_hash) · `module_metrics` · `imports` (src_module, dst_module, src_path) | blob, path, module | touched files only |
| 3 | `package_metrics` (ca, ce, instability, abstractness, distance, **dirty**) | module | lazily, for dirty modules |
| ops | `update_log` (per-tier ms of every update — the RQ2 data) · `gate_log` (every warn: ts, module, rule, verdict, deviation, diff_hash, resolved) | id | on update / on check |

The `Store` methods are grouped the same way: meta (`bookmark` property also stamps `bookmark_at`,
`load_config/save_config`), tier-1 writes (`add_commit` = `INSERT OR IGNORE`; `bump_module_period`, `bump_cochange`
= upserts), tier-2 (`get/put_blob_metrics`, `set/delete_file_state`, `replace_imports`, `module_files`,
`put/get_module_metrics`), tier-3 (`mark_dirty`, `dirty_modules`, `put/get_package_metrics`), reads
(`modules()`, `module_series`, `module_authors_by_period`, `module_recent_commits`, `cochange_rows`,
`cochange_pair`, `fan_in` — **excludes test modules** —, `fan_out`, `counts`), gate log (`log_gate`,
`unresolved_count`, `resolve_gate`, `gate_summary`). `open_store(root, create=False)` returns `None` when there is no
index, which is how the gate learns to say `SL-F00`.

Three invariants live here: writes are upserts keyed by hash or (module, period), so re-processing a batch is
harmless; `reset_history()` drops every derived table **except** `blob_metrics` (content-addressed, still valid after
a rebuild) and `gate_log` (operational history, not derived); the only thing a reader ever needs is a `SELECT`.

## 7. `signals/` — counters → statements

### `trend.py`

`mann_kendall(x, alpha=0.10) -> MKResult(trend, p, z, s, slope, n)`: the S statistic, tie-corrected variance
(Gilbert 1987), continuity-corrected z, two-sided p via `math.erfc(|z|/√2)` (no scipy), Sen's slope as the median
of pairwise slopes. `trend` is `increasing | decreasing | no_trend`; `.direction` renders it as rising / falling / flat.

### `phase.py` — the main point of the project

`module_axis(store, module, cfg, now)` builds the closed-period axis from the module's first period to
`last_closed_period`, with zeros filled in, plus per-period `(commits, added, deleted)` and author counts.
`_classify(axis, data, authors, cfg)` applies the rules in this order:

```python
n < phase_min_periods                                   -> NO_DATA  ("only n closed period(s); need 4")

# derived over the window W = axis[-8:], K = 4
level          = mean commits over the last K periods
earlier_mean   = mean commits over everything before the last K periods
churn_falling  = Mann-Kendall(commits window) decreasing       churn_rising = increasing
size series    = cumulative net lines; size_rising = MK increasing
only_shrank    = last K periods net-negative, no period added, and the loss >= 5 % of the module
size_falling   = MK(size) decreasing or only_shrank
authors_leaving= recent distinct authors < prior and <= max(1, prior // 2)   # prior = max over a 3·W memory
streak         = trailing closed periods with commits <= phase_low_churn

earlier_mean > low and level < earlier_mean and >= 2 of {churn_falling, size_falling, authors_leaving}
                                                        -> DECLINE      (confidence from p-values and vote count)
streak >= K and not churn_rising and not size_falling   -> STABILIZING  (high if streak >= K+2)
churn_rising or size_rising or level > low              -> GROWTH
otherwise                                               -> GROWTH, confidence "low"   # ties fail safe toward growth
```

`classify_module()` adds `phase_since`: it re-classifies the axis truncated at each earlier period and walks back until
the label changes, with **one-period hysteresis** (a single dissenting period does not end a run; two do). The result
is a `PhaseResult` whose `to_dict()` is the phase block of the payload: `phase, phase_since, confidence, reasons,
trends{churn, size, authors}, window{periods, closed_through, periods_total, churn_level, low_streak}, open_period`.

Semantics worth repeating to anyone reading the output: phase is a *trajectory*. A module that finished declining and
went silent reads as `stabilizing` (and usually `isolated`) — decline is the withdrawal, not the grave.

### `cochange.py`

`weight = Σ_p count(p) · 0.5^(age(p) / half_life)` with `age` in periods back from the last closed period
(`_decayed`). `partners_of(store, module, cfg, now, top_k)` groups the `cochange` rows by partner, computes decayed
weight, lifetime count, periods active, last commit, a Mann-Kendall trend over the window and the partner's share of
the module's total weight, drops partners under `cochange_min_weight`, and returns the top k (`top_k=0` → all, which
connectedness uses). `pair_weight(a, b)` is what the gate's coupling rules call.

### `connectedness.py`

`connectedness_of()` → `Connectedness(status, fan_in, fan_out, partners, imports_available)`; `load-bearing` if
fan-in ≥ 1 (non-test importers) **or** partners ≥ 2, otherwise `isolated`. A separate axis by design: "growth +
isolated" is a legitimate, reported combination (an entry point, or new code nothing is wired to yet).

### `churn.py`

`hotspots(store, cfg, now, limit)`: commits over the window × average CCN of the module (Tornhill's hotspot idea),
ranked; the lines column is added+deleted over the window.

## 8. `serve/` — the three tools

### `context.py` → `get_evolution_context`

`freshness(store, repo)` compares the bookmark with HEAD and returns `{bookmark, bookmark_at, head, commits_behind,
stale, rewritten?, index_present}` — the TLE-style age stamp attached to every answer. `evolution_context(store,
module, repo, cfg, now, evidence_commits=3)` assembles, in this order: `module, known`, the phase block, `connectedness`,
`co_changes_with`, `maintainability` (composite, files, nloc, functions, ccn_avg, ccn_max), `package_metrics`
(ca, ce, instability, abstractness, distance, `stale` = dirty flag), `evidence` (`commits_analyzed`, three
`reference_commits` with hash/date/subject/added/deleted, `scale`), `advice` (one sentence chosen from phase ×
connectedness in `_advice`), `freshness`, `senso_lens` version. An unknown module returns `known: false, phase:
no_data` with a reason rather than an error, because an agent that gets an exception simply proceeds without context.

### `rules.py` — the deck

`RULES` is a dict of frozen `Rule(id, name, severity, description, rationale, family)`:

| id | name | default | family |
|---|---|---|---|
| SL-F00 / F01 / F02 | no_index / index_stale / history_rewritten | warn | F (fail-safe) |
| SL-W01 / W02 | add_to_stabilizing / add_to_declining | warn | W (warning) |
| SL-W03 / W04 | new_coupling / coupling_against_trend | warn | W |
| SL-I01 / I02 | no_data / unknown_module | info | I (informational) |

`load_rule_deck(repo_root)` reads `senso.rules.toml` if present: `[rules.SL-xxx] severity = "warn|info|off"` and
`[[suppress]]` entries that must carry all of `rule, module, reason, by, until` — an incomplete suppression is ignored
by design — with `module = "*"` allowed. `RuleDeck.severity(id)`, `.suppressed(id, module)` (active ones only) and
`.expired` are what the gate consults. `block` is deliberately not a configurable severity.

### `gate.py` → `check_change`

`parse_unified_diff(text)` → `FileDelta(path, added, deleted, old_path, is_new, is_deleted)` per file, understanding
`diff --git`, `--- /dev/null`, `+++ /dev/null`, `rename from/to`. `check_change(store, repo, diff_text, cfg, now,
allow_stale, record, deck)` is the interlocking:

```python
if store is None or store.bookmark is None:        -> warn [SL-F00]            # ok must be proven
if freshness.rewritten: SL-F02   elif freshness.stale and not allow_stale: SL-F01 (with commits_behind)

aggregate the diff per module (source files only; deleted files skipped)
for each touched module:
    not in index                     -> SL-I02 (info)
    phase no_data                    -> SL-I01 (info; deviation 1 if load-bearing)
    is_addition = net_added > 10 or (added > 5 and added > 2·deleted)
    stabilizing and is_addition      -> SL-W01, message names the top co-change partner as the place to extend
    decline and net_added > 0        -> SL-W02, evidence.successor = _successor(module)
                                        (a growing sibling, a growing partner, or a growing module that imports it)
    growth and nothing fired         -> module joins `authority`
for each pair of touched known modules:
    lifetime == 0 and one is stable/load-bearing            -> SL-W03 (deviation 3 if load-bearing, else 2)
    lifetime > 0 and decayed weight < min_weight and same    -> SL-W04 (deviation 2)

deviation per phase finding = 2, +1 if load-bearing, +1 if net_added >= 100, clamped to 0..4
apply the deck: severity "off" drops a finding; an active suppression turns it into info and attaches `suppressed_by`
verdict = "warn" if any warn-severity finding remains
          "block" if an SL-W rule on the same module is already unresolved >= 2 times in the last 24 h (this is the 3rd)
if record and verdict != ok: write the warn findings to gate_log      (a later commit on the module resolves them)
authority = growth modules untouched by a warning + growing partners of warned modules
```

The result: `{verdict, deviation, rules_fired, findings[{rule, name, module, severity, deviation, message,
evidence, suppressed_by?}], authority, touched, diff_hash, freshness, rule_deck{source, expired_suppressions},
reason}`. `senso check` maps the verdict to exit codes 0 / 1 / 2.

### `explain.py` → `explain_phase`

`explain_project(store, repo, cfg, now, top)` classifies every module once and returns: a summary sentence
(counts per phase, "handle with care" = stabilizing + load-bearing, "active but nothing depends on them" = growth +
isolated), `phases` (phase → modules), the module table (phase, since, confidence, connectedness, fan-in, partners,
churn level, trends, NLOC, maintainability), `hotspots`, `strongest_couplings` (deduplicated pairs by decayed weight),
`gate_last_30_days`, history span and counts, `freshness`.

## 9. `cli.py`, `mcp_server.py` — the two front doors

`cli.py` is a typer app (`senso`); every command is a thin wrapper around the functions above and `--json` prints
exactly what the MCP tool returns (`_dump`). Human output uses rich tables/panels; when stdout is not a terminal the
console is given 160 columns so module names are never truncated in logs or tests.

| command | calls | notes |
|---|---|---|
| `init [path] --scale --depth` | `init_index` | exit 4 on a shallow clone; prints per-tier timings |
| `update [path] --quiet` | `update_index` | `--quiet` exits 0 silently when there is no index (hook safety) |
| `status [path]` | `freshness`, `counts`, last `update_log` row | |
| `context MODULE [path]` | `evolution_context` | exit 1 for an unknown module (lists known ones) |
| `check [path] --diff FILE\|- / --staged / --working / stdin` | `check_change` | `--allow-stale`, `--no-record`; exit = verdict |
| `explain [path] [-m MODULE] [--top N]` | `explain_project` / `classify_module` | `-m` prints the full window table and the rule text |
| `rules [path] [--init]` | `load_rule_deck` | effective severities, active/expired suppressions |
| `export [path] --format csv\|json --out DIR` | raw tables + phase labels | input for external checks (RQ1) |
| `replay [path] --mode --limit --out --scale` | `replay.replay` | RQ2 table |
| `hook install \| remove [path]` | `hooks_dir` | idempotent block between `# >>> senso-lens` markers, chains with an existing hook |
| `mcp [path]` | `mcp_server.serve` | stdio transport |

The installed hook is six lines of `sh`: if `senso` is on `PATH`, run `senso update "$(git rev-parse --show-toplevel)"
--quiet || true`. It can never fail a commit.

`mcp_server.py` builds the server (`MCPServer` on mcp ≥ 2, `FastMCP` on 1.x — same constructor, `.tool()` and
`.run()`), sets `instructions` that tell an agent when to call what, and registers `get_evolution_context(module)`,
`check_change(diff, allow_stale=False)`, `explain_phase(module=None, top=8)`. Each tool opens the store, answers,
closes it; a missing index yields a payload that says so. Clients register it as
`{"command": "senso", "args": ["mcp", "/path/to/repo"]}`.

## 10. `replay.py` — the RQ2 instrument

`replay(root, mode, limit, scale, progress)` walks the **first-parent chain** of HEAD (`git rev-list --first-parent
--reverse HEAD`): one step is a direct commit or a merged branch, exactly the unit a post-commit/post-merge hook
processes, so ranges never overlap. With `--limit N` it first warms the store to the step before the window, so every
measured step is incremental rather than cold. `shared` mode runs `update_index` with every family on one store;
`isolated` mode runs it three times per step on three separate stores (`ISOLATED_GROUPS`: history = churn+cochange,
metrics, packages) with nothing shared, not even the blob cache — which is what three single-purpose tools do today.
`write_csv` emits one row per (mode, step) with `files`, `bucket` (0 / 1-3 / 4-10 / 11-50 / 51+) and per-tier ms;
`summarize` gives median and p95 per bucket. On click: 50 ms shared vs 104 ms isolated for 1–3-file steps.

## 11. `tests/` — the evidence

`fixture_repo.py` scripts a three-year repository whose truth is known (dates set with `GIT_AUTHOR_DATE` /
`GIT_COMMITTER_DATE`, deterministic given today's date):

| module | script | expected |
|---|---|---|
| `core/parser` | 2–4 commits/month for 18 months, then one small fix every 4 months; imported by `api/handlers` | stabilizing, load-bearing |
| `api/handlers` | from month 3, rising, two authors, early co-change with the parser, one rename | growth |
| `legacy/auth` | active with 3 authors for 21 months, then deletions by one author on odd months, then silent | decline (since ≈ month 23) |
| `auth/service` | from month 22, growing, imports `legacy.auth` | growth; the inferred successor of `legacy/auth` |
| `scripts/tools` | a burst in months 6–14, nothing imports it | stabilizing, isolated |
| `docs`, `requirements.txt` | docs-only commits, dependabot bumps | noise; never form a module |

`conftest.py` builds it once per session, indexes a copy, and hands tests a fresh writable copy when they need to
commit. The files:

- `test_pipeline.py` — phases match the script; noise reasons and the rename are recorded; **the index built in three
  deltas is table-for-table identical to one built in a single pass** (the core correctness property of an
  incremental index); a new commit is processed incrementally; `git reset --hard` + new commit triggers a rebuild;
  shallow clones are refused; `scale="auto"` picks monthly; `.senso/` lands in `.git/info/exclude`.
- `test_gate.py` — the diff parser; each SL rule on the diff that should fire it, including the successor and the
  authority; no index / stale / `allow_stale`; escalation `warn, warn, block` and reset by a commit; severities and
  suppressions from a TOML deck (incomplete one ignored, expired one reported).
- `test_signals.py`, `test_periods_trend.py` — mapper, test-module detection, import resolution, noise, parser output
  for Python, `mi_lite` bounds, decay arithmetic, every phase rule on synthetic series (including the fail-safe tie),
  period keys and ranges across year boundaries, the time-gap rule, Mann-Kendall against a textbook tie-corrected case.
- `test_cli_mcp.py` — the CLI end to end through typer's `CliRunner` (init, status, explain, context, check, rules,
  export, update, hook install/remove, `--staged`/`--working`, replay) and a real MCP client round trip against the
  spawned server.
- `test_reference.py` — churn and co-change totals equal a direct `git log --name-only` recount; per-file complexity
  agrees with radon on most files and within a bounded ratio overall (radon counts `and`/`or`/ternaries).

`.github/workflows/ci.yml` runs the suite on Python 3.11 and 3.12 and then `senso init . --scale weekly`,
`senso status .`, `senso explain .` on the repository itself.

## 12. Tracing one call end to end

**`senso context core/parser`** (or the MCP tool): `cli.context` → `GitRepo(path)`, `open_store` → `evolution_context`
→ `classify_module` (`module_series` + `module_authors_by_period` → `module_axis` → `_classify` W times for
`phase_since`) → `partners_of` (`cochange_rows`) → `connectedness_of` (`fan_in`, `fan_out`, `partners_of(top_k=0)`) →
`get_module_metrics`, `get_package_metrics`, `module_recent_commits` → `freshness` (`git rev-parse HEAD`,
`merge-base --is-ancestor`) → payload. No parser, no log walk; 3–20 ms.

**`git diff | senso check .`**: `cli.check` reads stdin → `check_change` → `parse_unified_diff` → `load_rule_deck` →
`freshness` → per module `classify_module` + `connectedness_of` → pair rules via `pair_weight` → deck → escalation via
`unresolved_count` → `log_gate` → verdict → exit code.

**A commit with the hook installed**: `post-commit` → `senso update --quiet` → `update_index` → `iter_commits(bookmark,
HEAD)` → tier 1 for the one commit → `_tier2` parses only the blobs not already in `blob_metrics` (`ls_tree` once,
`read_many_blobs` once) → `_tier3` for the modules marked dirty → bookmark advances → `update_log` row. 50–150 ms on
the repositories measured so far.

## 13. Where to change things

| you want to… | touch | and keep in mind |
|---|---|---|
| add a gate rule | `serve/rules.py` (new `Rule` with an `SL-` id) and the branch that fires it in `serve/gate.py`; a test in `tests/test_gate.py`; a row in `docs/RULES.md` | ids are stable once published; `block` is reached only by escalation |
| support another language | `config.SOURCE_EXTENSIONS`, `parser.LANG_BY_EXT`, an import regex in `_generic_structure`, maybe a resolution case in `modules.resolve_import_target` | lizard already measures it; only imports and class detection are per-language |
| change what a module is | `ingest/modules.py` (`module_of`), `Config.module_depth`, `strip_prefixes` | re-run `senso init`: the mapping is baked into the counters |
| tune a phase rule | `Config` thresholds first; the rule order in `signals/phase._classify` second | the fixture tests and `test_signals.py` are the regression net; document the change in `docs/DESIGN.md` §6 |
| add a signal family | a tier in `ingest/pipeline.py`, tables in `store/db.py` (`SCHEMA`, bump `SCHEMA_VERSION`), a reader in `signals/`, a block in `serve/context.py` | add it to `ALL_FAMILIES` and to an `ISOLATED_GROUPS` entry so `replay` measures it |
| change the payload | `serve/context.py` | the CLI's human view in `cli.context` and `README.md`'s sample follow it |
| add a CLI command | `cli.py` (`@app.command()`), a `CliRunner` test in `tests/test_cli_mcp.py` | give it `--json`; keep it a thin wrapper |
| publish a new MCP tool | `mcp_server.py` | the docstring is what the agent reads; say when to call it |

## 14. How the code maps to the research question

*Can one shared incremental index reliably derive a module's evolution phase from Git history, serve it in one
sub-second call, and change what a coding agent writes?*

| clause | where it lives | what shows it |
|---|---|---|
| one shared incremental index | `store/db.py`, `ingest/pipeline.py` (bookmark, delta reader, tiers, blob cache, `families`) | `test_incremental_updates_equal_full_rebuild`; `update_log`; `senso replay` shared vs isolated |
| reliably derive a module's evolution phase | `periods.py`, `signals/trend.py`, `signals/phase.py` | fixture phases match the script; synthetic rule tests; `senso export` for independent Mann-Kendall checks; `test_reference.py` for the counters underneath |
| serve it in one sub-second call | the read-only path: `serve/context.py`, `mcp_server.py` | 12–20 ms p50 on click/express, 16–27 ms MCP round trip (`docs/EVALUATION.md`) |
| change what a coding agent writes | `serve/gate.py` + `serve/rules.py` are the instrument; the experiment is semester 2 | RQ3 design and the wrong-side / right-side error classes in `docs/EVALUATION.md` |
