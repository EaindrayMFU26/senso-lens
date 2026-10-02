"""check_change: a fail-safe pre-edit gate, built like a railway interlocking.

`ok` is the state that has to be proven: the index exists and is fresh, every
touched module has a known phase, and no rule fires. Anything unknown or stale
reads as `warn` with the reason — never as `ok`. `block` is reached only by
escalation (the same rule on the same module kept firing unresolved), the
andon pattern: alert first, stop the line only if nothing changes.

The verdict carries a stable rule id, a graded 0-4 deviation score (a
likelihood x consequence lookup: phase certainty x connectedness x size), an
authority (where the change may go freely) and the evidence behind each claim.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Config
from ..ingest.git_reader import GitRepo
from ..ingest.modules import ModuleMapper
from ..store.db import Store
from ..signals.phase import classify_module, STABILIZING, DECLINE, GROWTH, NO_DATA
from ..signals.cochange import pair_weight, partners_of
from ..signals.connectedness import connectedness_of
from .context import freshness
from .rules import RULES, RuleDeck, load_rule_deck

_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")


@dataclass
class FileDelta:
    path: str
    added: int = 0
    deleted: int = 0
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False


def parse_unified_diff(text: str) -> list[FileDelta]:
    files: list[FileDelta] = []
    cur: FileDelta | None = None
    for line in text.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            cur = FileDelta(path=m.group(2), old_path=m.group(1) if m.group(1) != m.group(2) else None)
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("--- "):
            if line.strip() == "--- /dev/null":
                cur.is_new = True
            continue
        if line.startswith("+++ "):
            if line.strip() == "+++ /dev/null":
                cur.is_deleted = True
            else:
                p = line[4:].strip()
                cur.path = p[2:] if p.startswith("b/") else p
            continue
        if line.startswith("rename to "):
            cur.path = line[len("rename to "):].strip()
            continue
        if line.startswith("rename from "):
            cur.old_path = line[len("rename from "):].strip()
            continue
        if line.startswith("+") and not line.startswith("+++"):
            cur.added += 1
        elif line.startswith("-") and not line.startswith("---"):
            cur.deleted += 1
    return files


@dataclass
class Finding:
    rule: str
    module: str
    severity: str
    deviation: int
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suppressed_by: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"rule": self.rule, "name": RULES[self.rule].name, "module": self.module, "severity": self.severity,
             "deviation": self.deviation, "message": self.message, "evidence": self.evidence}
        if self.suppressed_by:
            d["suppressed_by"] = self.suppressed_by
        return d


def _deviation(phase: str, load_bearing: bool, net_added: int, cfg: Config, base: int) -> int:
    d = base
    if load_bearing:
        d += 1
    if net_added >= cfg.gate_large_addition:
        d += 1
    return max(0, min(4, d))


def _successor(store: Store, module: str, cfg: Config, now: datetime | None) -> str | None:
    """Where the work went: a growing sibling (same parent directory), a growing co-change partner,
    or a growing module that imports this one (the usual shape of a replacement that still delegates)."""
    parent = module.rsplit("/", 1)[0] if "/" in module else None
    best, best_level = None, 0.0
    for m in store.modules():
        if m == module:
            continue
        if parent and not m.startswith(parent + "/"):
            continue
        ph = classify_module(store, m, cfg, now)
        if ph.phase == GROWTH and ph.churn_level > best_level:
            best, best_level = m, ph.churn_level
    if best:
        return best
    for p in partners_of(store, module, cfg, now):
        if classify_module(store, p.module, cfg, now).phase == GROWTH:
            return p.module
    dependents = [r["src_module"] for r in store.conn.execute(
        "SELECT DISTINCT src_module FROM imports WHERE dst_module=?", (module,))]
    for m in sorted(dependents):
        ph = classify_module(store, m, cfg, now)
        if ph.phase == GROWTH and ph.churn_level > best_level:
            best, best_level = m, ph.churn_level
    return best


def check_change(store: Store | None, repo: GitRepo | None, diff_text: str, cfg: Config | None = None,
                 now: datetime | None = None, allow_stale: bool = False, record: bool = True,
                 deck: RuleDeck | None = None) -> dict[str, Any]:
    ts_now = int(time.time())
    diff_hash = hashlib.sha1(diff_text.encode("utf-8", errors="replace")).hexdigest()[:12]
    deltas = parse_unified_diff(diff_text)
    findings: list[Finding] = []
    verdict = "ok"

    if store is None or store.bookmark is None:
        f = Finding("SL-F00", "*", "warn", 1, RULES["SL-F00"].description + " Run `senso init` first.")
        return _result("warn", [f], 1, [], {}, deltas, diff_hash, None, deck)

    cfg = cfg or store.load_config()
    deck = deck or load_rule_deck(repo.root if repo else store.path.parent.parent)
    mapper = ModuleMapper(cfg)
    fresh = freshness(store, repo)

    if fresh.get("rewritten"):
        findings.append(Finding("SL-F02", "*", deck.severity("SL-F02"), 1, RULES["SL-F02"].description + " Run `senso update` to rebuild.", fresh))
    elif fresh.get("stale") and not allow_stale:
        behind = fresh.get("commits_behind")
        findings.append(Finding("SL-F01", "*", deck.severity("SL-F01"), 1,
                                f"Index is {behind if behind is not None else 'an unknown number of'} commit(s) behind HEAD; run `senso update`.", fresh))

    # aggregate the diff per module
    per_module: dict[str, dict[str, int]] = {}
    unknown_paths: list[str] = []
    for d in deltas:
        if d.is_deleted:
            continue
        mod = mapper.module_of(d.path)
        if mod is None or not mapper.is_source(d.path):
            continue
        agg = per_module.setdefault(mod, {"added": 0, "deleted": 0, "files": 0})
        agg["added"] += d.added
        agg["deleted"] += d.deleted
        agg["files"] += 1

    known = set(store.modules())
    phases: dict[str, Any] = {}
    conns: dict[str, Any] = {}
    authority: list[str] = []
    max_dev = 0

    for mod, agg in sorted(per_module.items()):
        net_added = agg["added"] - agg["deleted"]
        if mod not in known:
            unknown_paths.append(mod)
            findings.append(Finding("SL-I02", mod, deck.severity("SL-I02"), 0, f"{mod} is not in the index (new module or no history yet).",
                                    {"added": agg["added"], "deleted": agg["deleted"]}))
            continue
        ph = classify_module(store, mod, cfg, now)
        conn = connectedness_of(store, mod, cfg, now)
        phases[mod], conns[mod] = ph, conn
        load_bearing = conn.status == "load-bearing"
        is_addition = net_added > cfg.gate_small_addition or (agg["added"] > 5 and agg["added"] > 2 * agg["deleted"])
        ev = {"phase": ph.phase, "phase_since": ph.since, "confidence": ph.confidence, "connectedness": conn.status,
              "fan_in": conn.fan_in, "added": agg["added"], "deleted": agg["deleted"], "files": agg["files"],
              "churn_last_periods": ph.commits_window}

        if ph.phase == NO_DATA:
            findings.append(Finding("SL-I01", mod, deck.severity("SL-I01"), 1 if load_bearing else 0,
                                    f"{mod}: {ph.reasons[0]}.", ev))
            continue
        fired = False
        if ph.phase == STABILIZING and is_addition:
            dev = _deviation(ph.phase, load_bearing, net_added, cfg, 2)
            top = partners_of(store, mod, cfg, now, top_k=1)
            where = f" Consider extending {top[0].module} instead." if top else ""
            findings.append(Finding("SL-W01", mod, deck.severity("SL-W01"), dev,
                                    f"{mod} has been stabilizing since {ph.since} ({ph.low_streak} low-churn periods) and this change adds "
                                    f"{net_added:+d} net lines.{where}", ev))
            fired = True
        elif ph.phase == DECLINE and net_added > 0:
            succ = _successor(store, mod, cfg, now)
            dev = _deviation(ph.phase, load_bearing, net_added, cfg, 2)
            ev["successor"] = succ
            findings.append(Finding("SL-W02", mod, deck.severity("SL-W02"), dev,
                                    f"{mod} is in decline since {ph.since} ({', '.join(ph.reasons[:2])}); adds {net_added:+d} net lines."
                                    + (f" Successor appears to be {succ}." if succ else ""), ev))
            fired = True
        if not fired and ph.phase == GROWTH:
            authority.append(mod)

    # coupling rules over pairs of touched, known modules
    mods = [m for m in sorted(per_module) if m in known]
    for i in range(len(mods)):
        for j in range(i + 1, len(mods)):
            a, b = mods[i], mods[j]
            w, lifetime = pair_weight(store, a, b, cfg, now)
            pa, pb = phases.get(a), phases.get(b)
            sensitive = any(p and p.phase in (STABILIZING, DECLINE) for p in (pa, pb)) or \
                any(c and c.status == "load-bearing" for c in (conns.get(a), conns.get(b)))
            if lifetime == 0 and sensitive:
                target = a if (pa and pa.phase == STABILIZING) else b
                dev = 3 if any(c and c.status == "load-bearing" for c in (conns.get(a), conns.get(b))) else 2
                findings.append(Finding("SL-W03", target, deck.severity("SL-W03"), dev,
                                        f"{a} and {b} have never changed together; this change couples them.",
                                        {"pair": [a, b], "lifetime": 0}))
            elif lifetime > 0 and w < cfg.cochange_min_weight and sensitive:
                target = a if (pa and pa.phase == STABILIZING) else b
                findings.append(Finding("SL-W04", target, deck.severity("SL-W04"), 2,
                                        f"{a} and {b} used to change together ({lifetime} commits) but that coupling had faded (weight {w:.2f}); this change re-couples them.",
                                        {"pair": [a, b], "lifetime": lifetime, "weight": round(w, 2)}))

    # suppressions, severities, escalation
    active: list[Finding] = []
    for f in findings:
        if f.severity == "off":
            continue
        sup = deck.suppressed(f.rule, f.module)
        if sup:
            f.suppressed_by = sup.to_dict()
            f.severity = "info"
        active.append(f)

    warn_rules = [f for f in active if f.severity == "warn"]
    max_dev = max([f.deviation for f in active] + [0])
    if warn_rules:
        verdict = "warn"
        since_ts = ts_now - int(cfg.gate_escalation_hours * 3600)
        for f in warn_rules:
            if RULES[f.rule].family == "W" and store.unresolved_count(f.module, f.rule, since_ts) >= cfg.gate_escalation_count - 1:
                verdict = "block"
                f.evidence["escalated"] = True
    if record and verdict != "ok":
        with store.tx():
            for f in warn_rules:
                if RULES[f.rule].family == "W":
                    store.log_gate(ts_now, f.module, f.rule, verdict, f.deviation, diff_hash)

    # authority: growth modules untouched by a warning + growth partners of warned modules
    warned = {f.module for f in warn_rules}
    for f in warn_rules:
        for p in partners_of(store, f.module, cfg, now):
            if p.module not in warned and phases.get(p.module, None) is None:
                try:
                    if classify_module(store, p.module, cfg, now).phase == GROWTH:
                        authority.append(p.module)
                except Exception:
                    pass
    authority = sorted(set(a for a in authority if a not in warned))
    return _result(verdict, active, max_dev, authority, fresh, deltas, diff_hash, cfg, deck)


def _result(verdict: str, findings: list[Finding], deviation: int, authority: list[str], fresh: dict[str, Any],
            deltas: list[FileDelta], diff_hash: str, cfg: Config | None, deck: RuleDeck | None) -> dict[str, Any]:
    rules_fired = sorted({f.rule for f in findings if f.severity == "warn"})
    return {
        "verdict": verdict,
        "deviation": deviation,
        "rules_fired": rules_fired,
        "findings": [f.to_dict() for f in findings],
        "authority": authority,
        "touched": [{"path": d.path, "added": d.added, "deleted": d.deleted, "new": d.is_new, "deleted_file": d.is_deleted} for d in deltas],
        "diff_hash": diff_hash,
        "freshness": fresh,
        "rule_deck": {"source": deck.source if deck else None,
                      "expired_suppressions": [s.to_dict() for s in deck.expired] if deck else []},
        "reason": _summary(verdict, findings),
    }


def _summary(verdict: str, findings: list[Finding]) -> str:
    warns = [f for f in findings if f.severity == "warn"]
    if verdict == "ok":
        infos = [f for f in findings if f.severity == "info"]
        return "No rule fired." + (f" {len(infos)} informational note(s)." if infos else "")
    head = "Blocked after repeated unresolved warnings: " if verdict == "block" else ""
    return head + " ".join(f"[{f.rule}] {f.message}" for f in warns)
