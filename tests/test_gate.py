"""The fail-safe gate: diff parsing, rules, suppressions, escalation, staleness."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from senso_lens.ingest.git_reader import GitRepo
from senso_lens.ingest.pipeline import update_index
from senso_lens.serve.gate import check_change, parse_unified_diff
from senso_lens.serve.rules import RULES, RULES_FILE, load_rule_deck
from senso_lens.store.db import open_store
from tests.test_pipeline import commit_file


def diff(path: str, added: int, deleted: int = 0, new: bool = False, old_path: str | None = None) -> str:
    a = old_path or path
    lines = [f"diff --git a/{a} b/{path}"]
    if old_path:
        lines += [f"rename from {a}", f"rename to {path}"]
    if new:
        lines += ["new file mode 100644", "--- /dev/null", f"+++ b/{path}"]
    else:
        lines += [f"--- a/{a}", f"+++ b/{path}"]
    lines.append(f"@@ -1,{deleted} +1,{added} @@")
    lines += [f"-old {i}" for i in range(deleted)] + [f"+new {i}" for i in range(added)]
    return "\n".join(lines) + "\n"


def test_parse_unified_diff_counts_and_renames():
    d = parse_unified_diff(diff("a/b.py", 3, 1) + diff("c/d.py", 2, new=True) + diff("n/new.py", 1, old_path="n/old.py"))
    assert [(f.path, f.added, f.deleted, f.is_new, f.old_path) for f in d] == [
        ("a/b.py", 3, 1, False, None), ("c/d.py", 2, 0, True, None), ("n/new.py", 1, 0, False, "n/old.py")]
    assert parse_unified_diff("") == []


def _check(root: Path, text: str, **kw):
    store = open_store(root)
    try:
        return check_change(store, GitRepo(root), text, **kw)
    finally:
        if store:
            store.close()


def test_rules_fire_as_designed(indexed_repo: Path):
    r = _check(indexed_repo, diff("src/core/parser/lexer.py", 30), record=False)
    assert r["verdict"] == "warn" and r["rules_fired"] == ["SL-W01"] and r["deviation"] >= 3   # stable + load-bearing
    assert r["findings"][0]["evidence"]["phase"] == "stabilizing"

    r = _check(indexed_repo, diff("src/legacy/auth/session.py", 12), record=False)
    assert r["rules_fired"] == ["SL-W02"] and r["findings"][0]["evidence"]["successor"] == "auth/service"

    r = _check(indexed_repo, diff("src/api/handlers/routes.py", 40), record=False)
    assert r["verdict"] == "ok" and r["authority"] == ["api/handlers"] and r["deviation"] == 0

    r = _check(indexed_repo, diff("src/core/parser/lexer.py", 8) + diff("src/auth/service/core.py", 20), record=False)
    assert set(r["rules_fired"]) == {"SL-W01", "SL-W03"} and "auth/service" in r["authority"]

    r = _check(indexed_repo, diff("src/core/parser/lexer.py", 2, 2), record=False)
    assert r["verdict"] == "ok"                                     # a small fix is not an addition

    r = _check(indexed_repo, diff("src/legacy/auth/session.py", 0, 25), record=False)
    assert r["verdict"] == "ok"                                     # removing code from a declining module is the point

    r = _check(indexed_repo, diff("src/billing/core.py", 50, new=True), record=False)
    assert r["verdict"] == "ok" and [f["rule"] for f in r["findings"]] == ["SL-I02"]

    r = _check(indexed_repo, diff("docs/guide.md", 50) + diff("README.md", 3), record=False)
    assert r["verdict"] == "ok" and r["findings"] == []


def test_no_index_and_stale_index_fail_safe(repo_copy: Path, tmp_path: Path):
    r = check_change(None, None, diff("src/x/y.py", 5))
    assert r["verdict"] == "warn" and r["rules_fired"] == ["SL-F00"]

    commit_file(repo_copy, "src/api/handlers/routes.py", "\n# tweak\n", "api: tweak")
    r = _check(repo_copy, diff("src/api/handlers/routes.py", 3), record=False)
    assert r["verdict"] == "warn" and r["rules_fired"] == ["SL-F01"] and r["freshness"]["commits_behind"] == 1
    r = _check(repo_copy, diff("src/api/handlers/routes.py", 3), record=False, allow_stale=True)
    assert r["verdict"] == "ok"
    update_index(repo_copy)
    r = _check(repo_copy, diff("src/api/handlers/routes.py", 3), record=False)
    assert r["verdict"] == "ok" and r["freshness"]["stale"] is False


def test_escalation_blocks_then_a_commit_resets_it(repo_copy: Path):
    d = diff("src/legacy/auth/session.py", 12)
    verdicts = [_check(repo_copy, d)["verdict"] for _ in range(3)]
    assert verdicts == ["warn", "warn", "block"]
    assert _check(repo_copy, d, record=False)["findings"][0]["evidence"].get("escalated") is True
    # a commit touching the module answers the warning: escalation starts over
    commit_file(repo_copy, "src/legacy/auth/session.py", "\n# moved to auth/service\n", "auth: shrink")
    update_index(repo_copy)
    assert _check(repo_copy, d)["verdict"] == "warn"


def test_rule_deck_severity_and_suppression(repo_copy: Path):
    until = (date.today() + timedelta(days=30)).isoformat()
    (repo_copy / RULES_FILE).write_text(
        '[rules.SL-W03]\nseverity = "info"\n\n'
        f'[[suppress]]\nrule = "SL-W01"\nmodule = "core/parser"\nreason = "parser v3 approved"\nby = "alice"\nuntil = "{until}"\n'
        '[[suppress]]\nrule = "SL-W02"\nmodule = "legacy/auth"\nreason = "missing fields are ignored"\n'
        '[[suppress]]\nrule = "SL-W02"\nmodule = "*"\nreason = "expired"\nby = "bob"\nuntil = "2020-01-01"\n', encoding="utf-8")
    deck = load_rule_deck(repo_copy)
    assert deck.severity("SL-W03") == "info" and deck.severity("SL-W01") == "warn"
    assert len(deck.suppressions) == 2 and len(deck.expired) == 1      # the incomplete one is dropped
    r = _check(repo_copy, diff("src/core/parser/lexer.py", 30) + diff("src/auth/service/core.py", 20), record=False)
    assert r["verdict"] == "ok"
    kinds = {f["rule"]: f for f in r["findings"]}
    assert kinds["SL-W01"]["suppressed_by"]["by"] == "alice" and kinds["SL-W01"]["severity"] == "info"
    assert kinds["SL-W03"]["severity"] == "info"
    assert r["rule_deck"]["expired_suppressions"][0]["rule"] == "SL-W02"
    assert all(rid in RULES for rid in kinds)
