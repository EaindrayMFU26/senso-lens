"""The rule deck: rules as data, each with a stable id, a default severity and a rationale.

Like a design-rule check or a book of flight rules: the engine only reports
violations, it never edits code; severities can be tuned per repository in
`senso.rules.toml`; a suppression needs a reason, an author and an expiry date,
and stays visible in every verdict it affects.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

RULES_FILE = "senso.rules.toml"


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str          # warn | info | off  (block is reached only by escalation)
    description: str
    rationale: str
    family: str            # F = fail-safe, W = warning, I = informational


RULES: dict[str, Rule] = {r.id: r for r in [
    Rule("SL-F00", "no_index", "warn", "No index exists for this repository.",
         "Proceed must be proven; with no evidence the gate cannot say ok. Run `senso init`.", "F"),
    Rule("SL-F01", "index_stale", "warn", "The index is behind HEAD.",
         "A stale index can miss the commits that changed a module's phase; fail safe, report the age, and update.", "F"),
    Rule("SL-F02", "history_rewritten", "warn", "The bookmark is no longer an ancestor of HEAD.",
         "After a rebase or force-push the counters may be wrong until the index is rebuilt.", "F"),
    Rule("SL-W01", "add_to_stabilizing", "warn", "The change adds code to a module in the stabilizing phase.",
         "Lehman II: complexity rises unless work is spent against it. A stable, load-bearing module is where added "
         "code erodes architecture fastest; prefer extending a caller.", "W"),
    Rule("SL-W02", "add_to_declining", "warn", "The change adds code to a module in the decline phase.",
         "The history is withdrawing from this module; new behaviour belongs in its successor.", "W"),
    Rule("SL-W03", "new_coupling", "warn", "The change couples modules that have never changed together, one of them stable or load-bearing.",
         "Gall 1998: coupling that is not in the import graph still costs every future change. New coupling into stable code is a decision, not a default.", "W"),
    Rule("SL-W04", "coupling_against_trend", "warn", "The change re-couples modules whose co-change had faded.",
         "A coupling that decayed was being removed on purpose; reintroducing it reverses that trend.", "W"),
    Rule("SL-I01", "no_data", "info", "A touched module has too little history for a phase.",
         "Young modules are the blind zone of trend tests; the gate says so rather than guessing.", "I"),
    Rule("SL-I02", "unknown_module", "info", "A touched path belongs to no module in the index.",
         "New code has nothing to protect yet; it is reported, not warned about.", "I"),
]}


@dataclass
class Suppression:
    rule: str
    module: str
    reason: str
    by: str
    until: date

    def active(self, today: date | None = None) -> bool:
        return (today or datetime.now(tz=timezone.utc).date()) <= self.until

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "module": self.module, "reason": self.reason, "by": self.by, "until": self.until.isoformat()}


@dataclass
class RuleDeck:
    severities: dict[str, str]
    suppressions: list[Suppression]
    source: str | None

    def severity(self, rule_id: str) -> str:
        return self.severities.get(rule_id, RULES[rule_id].severity)

    def suppressed(self, rule_id: str, module: str) -> Suppression | None:
        for s in self.suppressions:
            if s.rule == rule_id and (s.module == module or s.module == "*") and s.active():
                return s
        return None

    @property
    def expired(self) -> list[Suppression]:
        return [s for s in self.suppressions if not s.active()]


def load_rule_deck(repo_root: str | Path) -> RuleDeck:
    path = Path(repo_root) / RULES_FILE
    if not path.exists():
        return RuleDeck({}, [], None)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sev: dict[str, str] = {}
    for rid, body in (data.get("rules") or {}).items():
        if rid in RULES and isinstance(body, dict) and body.get("severity") in ("warn", "info", "off"):
            sev[rid] = body["severity"]
    sups: list[Suppression] = []
    for s in data.get("suppress") or []:
        try:
            if not all(k in s for k in ("rule", "module", "reason", "by", "until")):
                continue  # a suppression without reason/author/expiry is ignored, by design
            until = s["until"] if isinstance(s["until"], date) else date.fromisoformat(str(s["until"]))
            sups.append(Suppression(str(s["rule"]), str(s["module"]), str(s["reason"]), str(s["by"]), until))
        except Exception:
            continue
    return RuleDeck(sev, sups, str(path))


EXAMPLE_RULES_TOML = """# SENSO-Lens rule deck. Severities: warn | info | off. Block is reached only by escalation.
[rules.SL-W01]
severity = "warn"

# A suppression needs all five fields or it is ignored; it expires on `until`
# and is shown in every verdict it affects.
# [[suppress]]
# rule = "SL-W01"
# module = "core/parser"
# reason = "parser v3 work approved in ADR-012"
# by = "alice"
# until = "2026-12-31"
"""
