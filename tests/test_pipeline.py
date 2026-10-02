"""Integration tests on the fixture repository: the shared incremental index end to end."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from senso_lens.ingest.git_reader import GitRepo
from senso_lens.ingest.pipeline import ShallowRepositoryError, init_index, update_index
from senso_lens.serve.context import evolution_context
from senso_lens.serve.explain import explain_project
from senso_lens.signals.phase import classify_module
from senso_lens.store.db import Store, open_store, store_path

ENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
       "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def commit_file(root: Path, rel: str, text: str, msg: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD")


# ------------------------------------------------------------------ what the fixture must look like
def test_fixture_phases_match_the_script(store):
    cfg = store.load_config()
    phases = {m: classify_module(store, m, cfg) for m in store.modules()}
    assert set(phases) == {"core/parser", "api/handlers", "legacy/auth", "auth/service", "scripts/tools"}
    assert phases["core/parser"].phase == "stabilizing"
    assert phases["api/handlers"].phase == "growth"
    assert phases["auth/service"].phase == "growth"
    assert phases["legacy/auth"].phase == "decline"
    assert "authors leaving" in phases["legacy/auth"].reasons
    assert phases["scripts/tools"].phase == "stabilizing"
    # phase_since is a closed period key inside the history
    assert all(len(p.since) == 7 and p.since[4] == "-" for p in phases.values())
    assert phases["legacy/auth"].since >= "2025-08"            # withdrawal starts in month 21 of the script


def test_noise_rename_and_counts(store):
    counts = store.counts()
    assert counts["noise_commits"] >= 18                      # dependabot bumps + docs-only commits
    bots = store.conn.execute("SELECT COUNT(*) n FROM commits WHERE noise_reason='bot_author'").fetchone()["n"]
    assert bots >= 15
    docs = store.conn.execute("SELECT COUNT(*) n FROM commits WHERE noise_reason='no_source_files'").fetchone()["n"]
    assert docs >= 10
    assert "docs" not in store.modules() and "(root)" not in store.modules()
    ren = store.conn.execute("SELECT old_path,new_path FROM renames").fetchall()
    assert [(r["old_path"], r["new_path"]) for r in ren] == [("src/api/handlers/legacy_names.py", "src/api/handlers/names.py")]
    assert store.conn.execute("SELECT 1 FROM file_state WHERE path='src/api/handlers/legacy_names.py'").fetchone() is None
    assert store.conn.execute("SELECT 1 FROM file_state WHERE path='src/api/handlers/names.py'").fetchone() is not None


def test_connectedness_and_imports(store):
    cfg = store.load_config()
    ctx = evolution_context(store, "core/parser", GitRepo(store.get_meta("repo_root")), cfg)
    assert ctx["connectedness"]["status"] == "load-bearing" and ctx["connectedness"]["fan_in"] == 1
    assert ctx["package_metrics"]["ca"] == 1 and ctx["package_metrics"]["instability"] == 0.0
    tools = evolution_context(store, "scripts/tools", None, cfg)
    assert tools["connectedness"]["status"] == "isolated"
    legacy = evolution_context(store, "legacy/auth", None, cfg)
    assert legacy["connectedness"]["status"] == "load-bearing"       # auth/service still imports it
    assert legacy["phase"] == "decline" and legacy["evidence"]["reference_commits"]
    unknown = evolution_context(store, "nope/nothing", None, cfg)
    assert unknown["known"] is False and unknown["phase"] == "no_data"


def test_blob_cache_parses_each_content_once(store):
    files = store.counts()["file_state"]
    distinct = store.conn.execute("SELECT COUNT(DISTINCT blob) n FROM file_state").fetchone()["n"]
    blobs = store.counts()["blob_metrics"]
    assert files >= 9 and distinct < files                    # the empty __init__.py files share one blob
    assert blobs == distinct                                  # one metrics row per distinct content
    log = store.conn.execute("SELECT files_parsed FROM update_log ORDER BY id LIMIT 1").fetchone()
    assert log["files_parsed"] == blobs                       # the full mine parsed each blob exactly once


# ------------------------------------------------------------------ the invariant: incremental == full
def _snapshot(store: Store) -> dict:
    q = lambda sql: [tuple(r) for r in store.conn.execute(sql)]
    return {
        "module_period": q("SELECT module,period,commits,added,deleted,files FROM module_period ORDER BY 1,2"),
        "authors": q("SELECT module,period,author,commits FROM module_period_authors ORDER BY 1,2,3"),
        "cochange": q("SELECT module_a,module_b,period,count FROM cochange ORDER BY 1,2,3"),
        "file_state": q("SELECT path,module,blob FROM file_state ORDER BY 1"),
        "module_metrics": q("SELECT module,files,nloc,functions,ccn_avg,ccn_max,maintainability,classes,abstract_classes FROM module_metrics ORDER BY 1"),
        "imports": q("SELECT src_module,dst_module,src_path FROM imports ORDER BY 1,2,3"),
        "package": q("SELECT module,ca,ce,instability,abstractness,distance,dirty FROM package_metrics ORDER BY 1"),
        "bookmark": store.bookmark,
    }


def test_incremental_updates_equal_full_rebuild(fixture_repo: Path, tmp_path: Path):
    repo = GitRepo(fixture_repo)
    hashes = [ev.hash for ev in repo.iter_commits()]
    cut1, cut2 = hashes[len(hashes) // 3], hashes[2 * len(hashes) // 3]

    inc = Store(tmp_path / "inc.db")
    with inc.tx():
        inc.save_config(inc.load_config())
    r1 = update_index(fixture_repo, store=inc, until=cut1, parse_at=cut1)
    r2 = update_index(fixture_repo, store=inc, until=cut2, parse_at=cut2)
    r3 = update_index(fixture_repo, store=inc, until="HEAD")
    assert r1.from_hash is None and r2.from_hash == cut1 and r3.from_hash == cut2
    assert r1.commits + r2.commits + r3.commits == len(hashes)
    assert r2.files_parsed < r1.files_parsed                  # only changed blobs are parsed in a delta

    full = Store(tmp_path / "full.db")
    with full.tx():
        full.save_config(full.load_config())
    update_index(fixture_repo, store=full)
    a, b = _snapshot(inc), _snapshot(full)
    for key in a:
        assert a[key] == b[key], f"{key} differs between incremental and full index"
    assert update_index(fixture_repo, store=inc).status == "up_to_date"
    inc.close(), full.close()


# ------------------------------------------------------------------ history edge cases
def test_update_after_new_commit_is_incremental(repo_copy: Path):
    before = open_store(repo_copy)
    bm = before.bookmark
    before.close()
    h = commit_file(repo_copy, "src/api/handlers/routes.py", "\n\ndef added(x):\n    return x\n", "api: one more handler")
    res = update_index(repo_copy)
    assert res.status == "updated" and res.commits == 1 and res.from_hash == bm and res.to_hash == h
    assert res.files_parsed == 1 and res.files_touched == 1
    s = open_store(repo_copy)
    assert s.bookmark == h
    assert s.conn.execute("SELECT 1 FROM commits WHERE hash=?", (h,)).fetchone()
    s.close()


def test_rewritten_history_triggers_rebuild(repo_copy: Path):
    commit_file(repo_copy, "src/api/handlers/routes.py", "\n# temp\n", "api: temp")
    update_index(repo_copy)
    git(repo_copy, "reset", "-q", "--hard", "HEAD~1")        # the bookmark is no longer an ancestor of HEAD
    assert not (repo_copy / ".senso").exists() or open_store(repo_copy).bookmark != git(repo_copy, "rev-parse", "HEAD")
    assert ".senso/" in (repo_copy / ".git" / "info" / "exclude").read_text()
    commit_file(repo_copy, "src/api/handlers/routes.py", "\n# other\n", "api: other")
    res = update_index(repo_copy)
    assert res.status == "rebuilt" and res.from_hash is None and any("rewritten" in n for n in res.notes)
    s = open_store(repo_copy)
    assert s.bookmark == git(repo_copy, "rev-parse", "HEAD")
    assert s.conn.execute("SELECT COUNT(*) n FROM commits WHERE subject='api: temp'").fetchone()["n"] == 0
    s.close()


def test_shallow_clone_is_refused(fixture_repo: Path, tmp_path: Path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "5", f"file://{fixture_repo}", str(shallow)], check=True, env=ENV)
    with pytest.raises(ShallowRepositoryError):
        init_index(shallow)


def test_init_chooses_scale_and_explain_runs(fixture_repo: Path, tmp_path: Path):
    root = tmp_path / "auto"
    shutil.copytree(fixture_repo, root)
    res = init_index(root, scale="auto")
    assert res.status == "initialized" and res.scale == "monthly"   # every month has a commit, weeks do not
    s = open_store(root)
    out = explain_project(s, GitRepo(root))
    assert out["phases"]["decline"] == ["legacy/auth"] and "core/parser" in out["phases"]["stabilizing"]
    assert out["freshness"]["stale"] is False and out["hotspots"][0]["module"] == "api/handlers"
    s.close()
    assert store_path(root).exists()
