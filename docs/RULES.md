# The rule deck

`senso check` / `check_change` evaluates these rules. Ids are stable (scripts and suppressions refer to them);
default severities can be changed per repository in `senso.rules.toml` (`senso rules --init` writes an example).
`block` is never a rule's severity — it is reached only by escalation.

| id | name | default | fires when | why |
|---|---|---|---|---|
| **SL-F00** | `no_index` | warn | no `.senso/index.db` | Proceed must be proven; with no evidence the gate cannot say ok. Run `senso init`. |
| **SL-F01** | `index_stale` | warn | bookmark behind HEAD | A stale index can miss the commits that changed a phase. The verdict reports how many commits behind; `--allow-stale` accepts it. |
| **SL-F02** | `history_rewritten` | warn | bookmark not an ancestor of HEAD | After a rebase or force-push the counters may be wrong until `senso update` rebuilds. |
| **SL-W01** | `add_to_stabilizing` | warn | an *addition* lands in a module in the stabilizing phase | Lehman II: complexity rises unless work is spent against it; stable, load-bearing code is where additions erode architecture fastest. Suggests the top co-change partner as the place to extend. |
| **SL-W02** | `add_to_declining` | warn | net lines added to a module in decline | The history is withdrawing from this module; new behaviour belongs in its successor, which the finding names when it can be inferred. |
| **SL-W03** | `new_coupling` | warn | two touched modules have never changed together and one is stable or load-bearing | Logical coupling (Gall 1998) costs every future change; new coupling into stable code is a decision, not a default. |
| **SL-W04** | `coupling_against_trend` | warn | two touched modules used to change together but the decayed weight fell below the reporting threshold | A coupling that faded was being removed; re-introducing it reverses that trend. |
| **SL-I01** | `no_data` | info | a touched module has fewer than 4 closed periods of history | Young modules are the blind zone of trend tests; the gate says so rather than guessing. |
| **SL-I02** | `unknown_module` | info | a touched path belongs to no module in the index | New code has nothing to protect yet; reported, not warned about. |

*An addition* means more than `gate_small_addition` (10) net lines, or more than 5 added lines with fewer than half as
many deleted. Deleting code from a declining module never fires anything: that is the point of decline.

## Deviation (0–4)

A likelihood × consequence lookup, per finding: phase rules start at 2; +1 if the module is load-bearing; +1 if the
change adds ≥ `gate_large_addition` (100) net lines. `SL-W03` is 3 when a load-bearing module is involved, else 2.
Informational findings are 0 (1 for `no_data` on a load-bearing module). The verdict carries the maximum.

## Verdicts

* `ok` — index present and fresh, every touched module has a phase, no warn-severity rule fired.
* `warn` — at least one warn-severity finding; proceed only with a reason. The verdict lists an **authority**:
  modules where new code may go freely (growth modules in the diff that fired nothing, and growing partners of the
  warned modules).
* `block` — a warn-family rule (`SL-W*`) on the same module was recorded unresolved `gate_escalation_count` (3) times
  within `gate_escalation_hours` (24). A commit that touches the module resolves its open findings; `--no-record`
  evaluates without recording.

Exit codes of `senso check`: 0 ok · 1 warn · 2 block.

## `senso.rules.toml`

```toml
# severities: warn | info | off
[rules.SL-W03]
severity = "info"

# a suppression needs all five fields or it is ignored; it expires on `until`
# and is shown (as suppressed_by) in every verdict it affects
[[suppress]]
rule = "SL-W01"
module = "core/parser"        # or "*"
reason = "parser v3 work approved in ADR-012"
by = "alice"
until = "2026-12-31"
```

`senso rules` prints the effective deck; expired suppressions are listed in every verdict under
`rule_deck.expired_suppressions` so that a waiver cannot silently outlive its reason.
