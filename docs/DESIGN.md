# SENSO-Lens — design notes

This document records *why* the code is the way it is. Each section names the decision, the alternative that was
rejected, and the place in the code where it lives. Numbers are as of October 2026; see `docs/EVALUATION.md` for
how they are measured.

## 1. The problem in one paragraph

Coding agents (Claude Code, Cursor, Codex) read a repository as a snapshot. Software evolves under laws that only
history reveals (Lehman: continuing change, increasing complexity, conservation of familiarity); the signals that
describe that evolution — churn and hotspots, logical coupling, maintainability, lifecycle phase — exist today as
three separate tools (temporal/co-change miners, hotspot/health scorers, metric suites), each walking the Git log
and parsing the tree on its own, none designed to be called in the sub-second budget of an agent's turn. SENSO-Lens
asks whether **one shared incremental index** can derive a module's **evolution phase** reliably, serve it in **one
sub-second call**, and (semester 2) change what an agent writes.

## 2. The shared incremental index

**Bookmark and delta reader** (`senso_lens/ingest/git_reader.py`, `pipeline.py`). The store keeps the hash of the
last processed `HEAD` under `meta.bookmark`. An update streams `git log --reverse --no-merges -M --numstat
bookmark..HEAD` (one process, one pass, renames detected) and processes commits oldest-first. The bookmark is
written **after** the whole batch, so a crash leaves the index at the previous consistent state and the next run
redoes the batch; per-commit writes are `INSERT OR IGNORE` / upserts keyed by hash so a redo is harmless.
*Rejected:* storing per-file deltas and recomputing on read (too slow for the sub-second call); a daemon that
watches the working tree (adds a process to run and a failure mode to explain).

**Rewritten history.** If the bookmark is not an ancestor of HEAD (`git merge-base --is-ancestor`), the counters
cannot be trusted, so the history tables are dropped and rebuilt from scratch. The blob cache survives (content
addressed, still valid). *Rejected:* trying to subtract the vanished commits — fragile, and a rebuild costs 1–2 s.

**Shallow clones are refused** at `init`: a truncated history produces confident, wrong phases.

**Three tiers**, each timed separately (`UpdateResult.t_tier{1,2,3}_ms`, `update_log` table):

| tier | when | what | cost driver |
|---|---|---|---|
| 1 | every commit, synchronous | churn counters per module and period, author sets, co-change edges | number of files in the commit |
| 2 | files touched by the batch | parse once per **blob hash**; file state, module metrics, import edges | size of the touched files (lizard) |
| 3 | lazily, for *dirty* modules | Martin's Ca / Ce / instability / abstractness / distance | number of modules whose import neighbourhood changed |

The one parse feeds every family: metrics, imports (→ package metrics and connectedness) and the maintainability
composite all read `blob_metrics`. Three isolated tools would walk the log three times and parse twice; `senso
replay --mode isolated` runs exactly that with the same code (see §9).

**Blob cache.** `blob_metrics` is keyed by the Git blob hash: a file that moves, is duplicated, or is reverted to an
earlier content is never parsed twice. Parsing happens only for the tree at HEAD (metrics describe the present);
history supplies the counters.

**Parallel parse.** Batches of ≥ 64 unparsed blobs (a full mine) use a process pool; deltas stay sequential
(pool start-up costs more than three files). Full mine of pallets/click: 3.6 s → 0.5 s for tier 2.

**`.senso/` is excluded locally** (`.git/info/exclude`) at `init` so `git add -A` never commits the index. This was
found the hard way: a test that committed the index and then `git reset --hard` rolled the bookmark back.

## 3. Modules

A module is a directory prefix: strip one leading source prefix (`src`, `lib`, `app`, `pkg`, `packages`) **when a
directory follows it**, then take the first `module_depth` (default 2) components; root files form `(root)`.
A flat `lib/*.js` layout is therefore the module `lib`, not `(root)` — the express repository made the case.
Vendored, generated, lock and binary paths are ignored (`IGNORE_PATH_PATTERNS`). Only source files (by extension)
contribute to churn and co-change: docs, lockfiles and assets never form a module.

Test modules (`tests/`, `__tests__/`, `spec/`, `test_*` …) are recognised so that their imports do not make
production code look load-bearing — tests depend on everything.

*Known limit:* flat packages (all of `click` in `src/click/*.py`) become one module. A file-level fallback for
flat packages is a semester-2 item; `--depth` is the current knob.

## 4. Noise

A commit is noise when it reflects no decision about the code's structure: bot authors (`dependabot`,
`pre-commit-ci`, `renovate` …), maintenance subjects (`Bump …`, `Release 8.3.3`, `apply black`), bulk changesets
(> 500 files) and commits with no source files. Noise commits are stored (nothing is lost) but excluded from churn,
co-change and phase. On click 40 % of non-merge commits are noise, two thirds of them docs-only.

## 5. Discretization — the time axis

This is the UTG discretization step (Huang et al., 2024, §4.1) applied to a repository: events (commits) are bucketed
into snapshots at level Δ; a period key is a sortable string on a **calendar** axis (`2026-W05`, `2026-03`,
`2026-Q1`, `2026`) so that "since 2025-Q4" reads as people expect.

**Scale selection** (`periods.choose_scale`) is the **time-gap rule**: the finest scale with no empty period over the
most recent three years, requiring at least four periods; otherwise yearly. Evaluated over three years so a sparse
early history does not force a coarse axis on an active project. click → quarterly, express → quarterly, the fixture
(a commit every month) → monthly. `senso init --scale` overrides.

**The open window is excluded.** All trend tests run up to `last_closed_period`; the current, partial period is
reported separately (`open_period`) — a half-finished quarter would otherwise look like the onset of decline.

Period arithmetic is integer ordinals (`period_index`), not string or datetime stepping; that single change took
`explain_phase` on express from 1.7 s to 0.25 s.

## 6. Phase rules (`signals/phase.py`)

Inputs per module: commits, net lines and distinct authors per closed period, from the module's first period to the
last closed one (gaps filled with zeros). Window W = 8 closed periods, K = 4, low churn ≤ 1 commit/period,
Mann-Kendall at α = 0.10 (tie-corrected, Sen's slope reported). Direction is Mann-Kendall's; magnitude is the mean
of the last K periods.

1. `no_data` — fewer than 4 closed periods since first activity. Young modules are the blind zone of trend tests;
   the gate says so (`SL-I01`) rather than guessing.
2. `decline` — the module **used to be active** (earlier mean > low), the recent level is below it, and at least two
   of: churn falling (MK), **net deletions** (size MK falling, *or* the last K periods only shrank by ≥ 5 % of the
   module), **authors leaving** (recent distinct authors at most half of what the module had over a long memory of
   3 W periods). The long memory matters: people leave long before the last deletion lands.
3. `stabilizing` — K+ consecutive trailing periods at or below low churn, churn not rising, size not falling.
4. `growth` — otherwise (churn or size rising, or level above low). **Ties resolve to growth with low confidence**:
   for an agent, a false "stabilizing" (which would stop legitimate work) is the costlier error.

`phase_since` is found by re-classifying the axis truncated at each earlier period and walking back until the label
changes, with **one-period hysteresis**: window classifiers flicker at boundaries, and a single dissenting period
does not end a run. On the fixture this moved `legacy/auth`'s since from 2026-01 (a one-period flicker) to 2025-10,
two periods after the scripted withdrawal began.

*Semantics worth stating:* phase is a trajectory, not a verdict. A module that finished declining and went silent
reads as `stabilizing` (and usually `isolated`). Decline is the withdrawal; once it is over, "stable, nobody touches
it" is the accurate description and the gate's advice (do not add code here) is the same.

## 7. Co-change with decay (`signals/cochange.py`)

Two modules changed in the same non-noise commit form an edge for that period; commits touching more than 30
modules are excluded from edges (a reformat is not coupling). Weight = Σ count(p) · 0.5^(age(p)/half-life), half-life
4 periods; partners below weight 1.0 are not reported; top 5 are returned with lifetime count, periods active, last
commit, trend over the window and share. The decay is the slime-mould rule — reinforce what carries change, let the
rest fade — and it is what makes `SL-W04` (re-coupling against a faded trend) expressible.

## 8. Connectedness (`signals/connectedness.py`)

`load-bearing` if fan-in ≥ 1 (imports from non-test modules) **or** co-change partners ≥ 2; otherwise `isolated`.
Kept as a separate axis: a module constantly modified that nothing depends on is *growth + isolated*, and both facts
are reported. Git cannot see runtime wiring (HTTP routing, DI containers), so entry points look isolated — the advice
text says so.

## 9. The gate (`serve/gate.py`, `serve/rules.py`)

Built like a railway interlocking: **`ok` is the state that has to be proven.** The diff is parsed (unified format,
renames and new files understood) and aggregated per module; then:

* no index → `SL-F00` warn; index behind HEAD → `SL-F01` warn with the number of commits behind (`--allow-stale`
  to accept); history rewritten → `SL-F02`.
* per touched module: `SL-W01` addition to a stabilizing module (an *addition* is > 10 net lines, or > 5 added lines
  with fewer than half as many deleted); `SL-W02` additions to a declining module, with the probable **successor**
  (a growing sibling, a growing co-change partner, or a growing module that imports this one); `SL-I01` no data;
  `SL-I02` unknown module.
* per pair of touched modules: `SL-W03` never co-changed and one is stable or load-bearing; `SL-W04` the coupling had
  faded below the reporting threshold.

Each finding carries a 0–4 **deviation** (a likelihood × consequence lookup: base 2 for a phase rule, +1
load-bearing, +1 for ≥ 100 net lines), the evidence used, and the rule's rationale is one lookup away
(`senso rules`). The verdict names an **authority**: growth modules in the diff that fired nothing, plus growing
partners of warned modules — where the new code may go freely.

**Escalation (andon).** Warnings are recorded in `gate_log`. The same rule on the same module, unresolved three times
within 24 h, turns the verdict into `block`; a commit touching the module resolves its open warnings, so the line
restarts after the developer acts. `--no-record` evaluates without recording.

**Rule deck.** Rules are data with stable ids, default severity and rationale. `senso.rules.toml` can set
`warn | info | off` per rule and list suppressions; a suppression needs all five fields (rule, module, reason, by,
until) or it is ignored by design, it expires, and it is shown in every verdict it affects; expired ones are listed.

## 10. The payload (`serve/context.py`)

Task-scoped, self-contained, evidence with every claim, token-budgeted: phase block, trends with the raw window,
connectedness, top partners, maintainability (files, NLOC, functions, CCN avg/max, composite), package metrics with
a stale flag, three reference commits, one line of advice, and a **freshness** stamp (bookmark, head, commits behind,
stale) so a stale answer is never served as current. The only Git call on the read path is `git rev-parse HEAD`.

The maintainability composite is `mi_lite = clamp((171 − 0.23·CC − 16.2·ln LOC)·100/171)` — the SEI/Oman MI
without the Halstead volume term — for every language, computed from lizard's NLOC and CCN. radon's full MI was
removed from the pipeline: its Halstead pass cost ~170 ms on a 2 000-line file and returns 0 for any large file,
which made the composite useless exactly where it matters; radon stays as a *reference implementation* in the
test-suite (`tests/test_reference.py`).

## 11. What three tools cannot share, and what one index does

| | three single-purpose tools | SENSO-Lens |
|---|---|---|
| git walks per commit | 3 | 1 |
| parses of a changed file | 2 (metrics, package graph) | 1, cached by blob |
| time axis | each its own (or none) | one calendar axis, time-gap scale |
| freshness | unknown per tool | one bookmark, reported in every answer |
| noise policy | each its own | one, stored with reasons |
| phase | not provided | derived from the shared counters |

The ablation in `senso replay` runs **the same code** in both configurations; the difference is only what is shared.
Measured on click (last 40 mainline steps): shared 50 ms vs isolated 104 ms median for 1–3 file steps. "When not
to consolidate" is a valid result: the shared design pays off when at least two families are wanted and the parse
dominates — which is the case for an agent that wants phase *and* coupling *and* metrics in one answer.

## 12. Inspirations that became decisions

* **Railway interlocking / JPL safe mode** → proceed must be proven; unknown reads as warn (§9).
* **Andon cord** → alert first, stop the line only when nothing changes (§9).
* **Design-rule checks / flight rules** → rules as data with ids, rationale and visible, expiring waivers (§9).
* **Two-line elements (TLE) epoch** → every answer stamped with the bookmark and its age (§10).
* **Torino scale** → one graded deviation number from likelihood × consequence (§9).
* **Physarum (slime mould) networks** → reinforce what carries flow, let unused links fade (§7).
* **UTG discretization and the time-gap rule** → the calendar axis and per-repository Δ (§5).
* **Lehman's laws** → what stabilizing and decline mean, and why ties go to growth (§6).
