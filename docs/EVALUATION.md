# Evaluation plan and current results

**Research question.** Can one shared incremental index reliably derive a module's evolution phase from Git history,
serve it in one sub-second call, and change what a coding agent writes?

Three sub-questions, each with a pre-registered threshold. Thresholds were fixed before measurement; a result that
misses a threshold is reported as such, not tuned away.

## RQ1 — Does the shared index compute the signals correctly? (coverage / convergent validity)

*Method.* Compare every signal family against an independent reference implementation on the same repositories.

| family | reference | comparison | threshold |
|---|---|---|---|
| churn (commits, lines per module and period) | direct `git log --name-only` recount | exact equality of per-module totals | 100 % on the fixture; ≥ 99 % of modules on real repositories (differences must be explained by the noise filter) |
| co-change (pair counts) | direct recount from the same log | exact equality of lifetime pair counts | same |
| complexity (per-file CCN, NLOC) | radon `cc_visit` (Python) | per-file agreement; total ratio | ≥ 75 % of files identical; total within 0.85–1.05 (radon counts `and`/`or`/ternaries) |
| package metrics (Ca, Ce, I, A, D) | Martin's formulas applied to the import table | exact | 100 % |
| phase labels | Mann-Kendall run independently on exported series (`senso export`) | label agreement | ≥ 95 % |

*Status.* Implemented as tests: `tests/test_reference.py` (churn and co-change agree **exactly** with the recount on
the fixture; complexity agrees exactly on 20 of 25 files of this repository, the rest differ by radon's convention
for boolean operators and conditional expressions, overall ratio 0.91) and `tests/test_pipeline.py::test_incremental_
updates_equal_full_rebuild` (the index built in three deltas is table-for-table identical to one built in a single
pass — the core correctness property of an incremental index). Real-repository agreement runs are a semester-1
deliverable still to be executed on the target set (§4).

## RQ2 — Is it fast enough, and does sharing pay? (latency and ablation)

*Serve latency.* `get_evolution_context` must answer in p50 < 1 s, p95 < 1 s on repositories up to 10 000 commits.
Measured (this container):

| repository | commits (non-merge) | modules | context p50 / p95 | `check_change` | `explain_phase` |
|---|---|---|---|---|---|
| fixture | 284 | 5 | 3 / 6 ms | 3 ms | 16 ms |
| pallets/click | 2 171 | 21 | 12 / 24 ms | 2 ms | 31 ms |
| expressjs/express | 5 688 | 75 | 20 / 57 ms | 38 ms | 247 ms |

Over MCP (stdio, client round trip included) the three tools answered in 16–27 ms on the fixture.

*Update cost.* Per mainline step (a commit or a merged branch, the unit a post-commit hook processes), with tier
timings, in two configurations of **the same code**: `shared` (one store, one walk, one parse) and `isolated` (three
stores — history, metrics, packages — each with its own walk and parse, nothing shared, which is what three tools do
today). `senso replay --mode both --limit N` writes one CSV row per (mode, step) and a per-bucket median table.

click, last 40 mainline steps, median ms:

| files in step | n | shared | isolated | ratio |
|---|---|---|---|---|
| 1–3 | 18 | 50 | 104 | 2.1× |
| 4–10 | 11 | 169 | 320 | 1.9× |
| 11–50 | 4 | 266 | 508 | 1.9× |

Parse (tier 2) dominates both; the shared index parses each blob once and walks Git once. The break-even question —
at what changeset size, if any, does the shared bookkeeping cost more than it saves — is answered by the same table
on the full target set; **"when not to consolidate" is a valid result** and will be reported if observed.

*Full mine.* click 1.4 s, express 1.6 s (parallel parse for batches ≥ 64 blobs). Threshold: < 60 s at 10 000
commits.

## RQ3 — Does it change what an agent writes? (semester 2)

*Design.* Paired tasks on repositories with known phases (the fixture plus real ones): the same agent, the same task,
with and without SENSO-Lens tools available. Outcomes: whether additions land in stabilizing/declining modules, new
couplings introduced, whether the agent cites the context in its plan, task success. Error classes are defined up
front: a **wrong-side** error is a false `ok` (the gate let a harmful edit through); a **right-side** error is a
needless `warn`. The fail-safe design trades right-side for wrong-side errors deliberately; the experiment measures
both rates and the cost of each (developer time lost to needless warnings vs. architectural debt let through).

## 4. Target repositories

Selection criteria: public, ≥ 3 years of history, ≥ 2 000 commits, at least two of {Python, JavaScript/TypeScript,
Java, Go}, a known refactoring or module retirement in the history (ground truth for decline), and an active
module (ground truth for growth). Current candidates: pallets/click, expressjs/express (used above), plus one Java
and one Go project to be fixed before the agreement runs.

## 5. How to reproduce

```bash
pip install -e ".[dev,mcp]"
pytest -q                                      # RQ1 tests, incremental == full, gate, CLI, MCP
git clone https://github.com/pallets/click && cd click
senso init . && senso status .
senso replay . --limit 40 --out ../click-replay.csv      # RQ2 ablation table
senso export . --out ../click-export                     # per-period series and phase labels for external checks
```

`benchmarks/README.md` describes the result files and how the tables in this document were produced.
