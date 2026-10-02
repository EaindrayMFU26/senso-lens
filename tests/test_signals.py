"""Unit tests: module mapping, noise filter, co-change decay, phase rules on synthetic series."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from senso_lens.config import Config
from senso_lens.ingest.git_reader import CommitEvent, FileChange
from senso_lens.ingest.modules import ModuleMapper
from senso_lens.ingest.noise import classify_noise
from senso_lens.ingest.parser import mi_lite, parse_source
from senso_lens.signals.cochange import _decayed
from senso_lens.signals.phase import DECLINE, GROWTH, NO_DATA, STABILIZING, _classify

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


# ------------------------------------------------------------------ modules
def test_module_mapping_strips_prefix_and_uses_depth():
    m = ModuleMapper(Config())
    assert m.module_of("src/core/parser/lexer.py") == "core/parser"
    assert m.module_of("core/parser/lexer.py") == "core/parser"
    assert m.module_of("lib/a/b/c/d.py") == "a/b"
    assert m.module_of("setup.py") == "(root)"
    assert m.module_of("src/setup.py") == "src"           # a flat src/ or lib/ layout is a module of its own
    assert m.module_of("lib/express.js") == "lib" and m.module_of("lib/router/index.js") == "router"
    assert m.module_of("node_modules/x/y.js") is None
    assert m.module_of("web/dist/bundle.min.js") is None
    assert m.module_of("vendor/lib/x.go") is None
    assert ModuleMapper(Config(module_depth=1)).module_of("src/core/parser/lexer.py") == "core"


def test_test_modules_are_recognised():
    from senso_lens.ingest.modules import is_test_module
    assert is_test_module("tests") and is_test_module("tests/unit") and is_test_module("src/__tests__")
    assert is_test_module("spec/models") and is_test_module("pkg/test_utils")
    assert not is_test_module("core/parser") and not is_test_module("contest/rules") and not is_test_module("testing_tools_x")


def test_source_filter_and_import_resolution():
    m = ModuleMapper(Config())
    assert m.is_source("a/b.py") and m.is_source("a/b.tsx") and not m.is_source("a/b.md") and not m.is_source("x.lock")
    known = {"src/core/parser/__init__.py", "src/core/parser/lexer.py", "src/api/handlers/routes.py", "web/ui/button/index.ts"}
    assert m.resolve_import_target("src/api/handlers/routes.py", "core.parser", known) == "core/parser"
    assert m.resolve_import_target("src/api/handlers/routes.py", "core.parser.lexer", known) == "core/parser"
    assert m.resolve_import_target("web/ui/app.ts", "./button", known) == "web/ui"   # depth 2: web/ui
    assert m.resolve_import_target("src/api/handlers/routes.py", "numpy", known) is None


# ------------------------------------------------------------------ noise
def _ev(author="Alice", email="a@x", subject="feat: thing", files=1):
    return CommitEvent("h" * 40, 1700000000, author, email, subject, [], [FileChange(f"src/m/f{i}.py", 1, 0) for i in range(files)])


def test_noise_classifier():
    cfg = Config()
    assert classify_noise(_ev(), cfg, 1) is None
    assert classify_noise(_ev(author="dependabot[bot]", email="x@users.noreply.github.com"), cfg, 1) == "bot_author"
    assert classify_noise(_ev(subject="Bump requests from 2.0 to 2.1"), cfg, 1) == "maintenance_subject"
    assert classify_noise(_ev(subject="chore(deps): update lodash"), cfg, 1) == "maintenance_subject"
    assert classify_noise(_ev(subject="apply black"), cfg, 1) == "maintenance_subject"
    assert classify_noise(_ev(), cfg, 0) == "no_source_files"
    assert classify_noise(_ev(files=501), cfg, 501) == "bulk_changeset"


# ------------------------------------------------------------------ parser
def test_parse_python_source_metrics_and_imports():
    code = b"import os\nfrom core.parser import tokenize\nfrom . import sibling\nfrom abc import ABC\n\nclass Base(ABC):\n    pass\n\nclass Impl(Base):\n    pass\n\ndef f(x):\n    if x:\n        return 1\n    return 0\n"
    m = parse_source("src/api/handlers/routes.py", code)
    assert m["language"] == "python" and m["functions"] == 1 and m["ccn_max"] == 2
    assert m["classes"] == 2 and m["abstract_classes"] == 1
    assert "core.parser" in m["imports"] and "os" in m["imports"]
    assert m["mi_kind"] == "mi_lite" and 0 <= m["mi"] <= 100


def test_mi_lite_bounds():
    assert mi_lite(0, 0, 0) == pytest.approx(100.0, abs=0.2)   # empty file: CC clamps to 1
    assert 0 <= mi_lite(5000, 2000, 300) < mi_lite(50, 10, 5) <= 100


# ------------------------------------------------------------------ co-change decay
def test_cochange_decay_halves_every_half_life():
    cfg = Config(scale="monthly", cochange_half_life_periods=4.0)
    assert _decayed([("2026-09", 2)], "2026-09", cfg) == pytest.approx(2.0)            # current closed period: no decay
    assert _decayed([("2026-05", 2)], "2026-09", cfg) == pytest.approx(1.0)            # 4 periods old: half
    assert _decayed([("2025-09", 8)], "2026-09", cfg) == pytest.approx(1.0)            # 12 periods old: 1/8
    assert _decayed([("2026-05", 2), ("2026-09", 2)], "2026-09", cfg) == pytest.approx(3.0)


# ------------------------------------------------------------------ phase rules
def _range(n: int):
    out = []
    y, m = 2024, 1
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _series(commits, net=None, authors=None):
    axis = _range(len(commits))
    data = {p: (c, max(0, n), max(0, -n)) for p, c, n in zip(axis, commits, net or [10] * len(commits))}
    auth = {p: a for p, a in zip(axis, authors or [2] * len(commits))}
    return axis, data, auth


def test_phase_no_data_under_min_periods():
    axis, data, auth = _series([3, 3, 3])
    r = _classify(axis, data, auth, Config())
    assert r.phase == NO_DATA and r.confidence is None


def test_phase_growth_rising_churn():
    axis, data, auth = _series([1, 1, 2, 2, 3, 4, 5, 6, 7, 8])
    r = _classify(axis, data, auth, Config())
    assert r.phase == GROWTH and r.churn_trend == "rising" and r.confidence == "high"


def test_phase_stabilizing_after_k_low_periods():
    axis, data, auth = _series([4, 5, 4, 3, 2, 1, 0, 1, 0, 0, 1, 0], net=[20, 30, 20, 10, 5, 1, 0, 1, 0, 0, 1, 0])
    r = _classify(axis, data, auth, Config())
    assert r.phase == STABILIZING and r.low_streak >= 4 and r.size_trend != "falling"


def test_phase_decline_needs_two_votes():
    # churn fading, net deletions, authors from 3 to 1 -> decline
    commits = [4, 4, 5, 4, 4, 3, 3, 2, 2, 1, 1, 1, 0, 1]
    net = [40, 40, 50, 40, 40, 10, 0, -20, -30, -25, -20, -15, 0, -10]
    authors = [3, 3, 3, 3, 3, 2, 2, 1, 1, 1, 1, 1, 0, 1]
    axis, data, auth = _series(commits, net, authors)
    r = _classify(axis, data, auth, Config())
    assert r.phase == DECLINE and "net deletions" in r.reasons and "authors leaving" in r.reasons
    # the same churn fade with growth in size and a steady team is NOT decline (it is settling)
    axis, data, auth = _series(commits, [40, 40, 50, 40, 40, 10, 5, 5, 5, 2, 2, 2, 0, 2], [3] * 14)
    r2 = _classify(axis, data, auth, Config())
    assert r2.phase != DECLINE


def test_phase_ties_fail_safe_to_growth_low_confidence():
    # nothing significant, churn just above the low threshold, size neither rising nor falling by test
    axis, data, auth = _series([2, 1, 2, 1, 2, 1, 2, 1], net=[1, -1, 1, -1, 1, -1, 1, -1])
    r = _classify(axis, data, auth, Config())
    assert r.phase == GROWTH and r.confidence in ("low", "medium")
