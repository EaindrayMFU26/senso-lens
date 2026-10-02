"""Convergent validity against independent reference implementations (the RQ1 method, in miniature).

* complexity: our per-file cyclomatic complexity (lizard) against radon's `cc_visit` on every
  Python file of this repository. The two engines use different conventions for boolean operators
  and conditional expressions, so the check is agreement on most files and a bounded ratio overall,
  and the differences are reported rather than hidden.
* churn: our per-module commit counts against a direct `git log` count on the fixture repository.
* co-change: our lifetime pair counts against a direct recount from `git log --name-only`.
"""
from __future__ import annotations

import glob
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from senso_lens.config import Config
from senso_lens.ingest.modules import ModuleMapper
from senso_lens.ingest.parser import parse_source

radon_cc = pytest.importorskip("radon.complexity", reason="radon (reference implementation) not installed")

ROOT = Path(__file__).resolve().parents[1]


def test_complexity_agrees_with_radon_on_this_repository():
    files = sorted(glob.glob(str(ROOT / "senso_lens" / "**" / "*.py"), recursive=True))
    exact, ours_total, ref_total, diffs = 0, 0, 0, []
    for f in files:
        code = Path(f).read_text(encoding="utf-8")
        m = parse_source(f, code.encode("utf-8"))
        ours = int(round(m["ccn_avg"] * m["functions"]))
        blocks = radon_cc.cc_visit(code)
        ref = sum(b.complexity for b in blocks if b.letter in ("F", "M"))
        ours_total += ours
        ref_total += ref
        if ours == ref:
            exact += 1
        else:
            diffs.append((Path(f).name, ours, ref))
    assert len(files) >= 20
    assert exact / len(files) >= 0.75, diffs                      # most files agree exactly
    assert 0.85 <= ours_total / max(1, ref_total) <= 1.05, diffs  # radon adds +1 per `and`/`or`/ternary


def test_churn_and_cochange_agree_with_git_log(store, fixture_repo: Path):
    cfg = store.load_config()
    mapper = ModuleMapper(cfg)
    out = subprocess.run(["git", "-C", str(fixture_repo), "log", "--no-merges", "--format=%x1e%H%x1f%an%x1f%s", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    commits_per_module: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    for rec in out.split("\x1e"):
        if not rec.strip():
            continue
        head, _, body = rec.partition("\n")
        _h, author, subject = head.split("\x1f")
        if "[bot]" in author or subject.lower().startswith("bump "):
            continue
        mods = sorted({mapper.module_of(p) for p in body.split("\n") if p.strip() and mapper.is_source(p)} - {None})
        for m in mods:
            commits_per_module[m] += 1
        for i in range(len(mods)):
            for j in range(i + 1, len(mods)):
                pairs[(mods[i], mods[j])] += 1
    ours = {r["module"]: r["n"] for r in store.conn.execute("SELECT module, SUM(commits) n FROM module_period GROUP BY module")}
    assert ours == dict(commits_per_module)
    ours_pairs = {(r["module_a"], r["module_b"]): r["n"] for r in store.conn.execute(
        "SELECT module_a, module_b, SUM(count) n FROM cochange GROUP BY module_a, module_b")}
    assert ours_pairs == dict(pairs)
