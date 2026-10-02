"""The command line and the MCP server, driven the way a user and an agent would."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from senso_lens.cli import app
from tests.test_gate import diff
from tests.test_pipeline import commit_file

runner = CliRunner()


def run(*args: str, input: str | None = None):
    return runner.invoke(app, list(args), input=input, catch_exceptions=False)


def test_cli_init_status_explain_context(fixture_repo: Path, tmp_path: Path):
    root = tmp_path / "cli"
    shutil.copytree(fixture_repo, root)
    r = run("init", str(root), "--scale", "monthly", "--json")
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["status"] == "initialized" and res["commits"] > 200 and res["scale"] == "monthly"

    r = run("status", str(root), "--json")
    st = json.loads(r.output)
    assert st["index_present"] and st["freshness"]["stale"] is False and st["counts"]["modules"] == 5

    r = run("explain", str(root))
    assert r.exit_code == 0 and "legacy/auth" in r.output and "decline" in r.output

    r = run("explain", str(root), "-m", "core/parser", "--json")
    assert json.loads(r.output)["phase"] == "stabilizing"

    r = run("context", "core/parser", str(root), "--json")
    ctx = json.loads(r.output)
    assert ctx["phase"] == "stabilizing" and ctx["connectedness"]["status"] == "load-bearing" and ctx["freshness"]["stale"] is False
    r = run("context", "nope/none", str(root))
    assert r.exit_code == 1

    r = run("check", str(root), "--diff", "-", "--no-record", input=diff("src/core/parser/lexer.py", 30))
    assert r.exit_code == 1 and "SL-W01" in r.output
    r = run("check", str(root), "--diff", "-", "--no-record", "--json", input=diff("src/api/handlers/routes.py", 30))
    assert r.exit_code == 0 and json.loads(r.output)["verdict"] == "ok"

    r = run("rules", str(root), "--init")
    assert r.exit_code == 0 and (root / "senso.rules.toml").exists()
    r = run("rules", str(root), "--json")
    assert {x["id"] for x in json.loads(r.output)["rules"]} >= {"SL-F00", "SL-W01", "SL-I02"}

    r = run("export", str(root), "--out", str(tmp_path / "exp"))
    assert r.exit_code == 0 and (tmp_path / "exp" / "phases.csv").read_text().startswith("module,phase,since")


def test_cli_update_and_hook(repo_copy: Path):
    r = run("hook", "install", str(repo_copy))
    assert r.exit_code == 0
    hook = repo_copy / ".git" / "hooks" / "post-commit"
    assert hook.exists() and "senso update" in hook.read_text()
    assert run("hook", "install", str(repo_copy)).output.startswith("already installed")
    commit_file(repo_copy, "src/api/handlers/routes.py", "\n# tweak\n", "api: tweak")
    # the hook ran `senso update` if `senso` is on PATH; either way `update` must converge
    r = run("update", str(repo_copy), "--json")
    assert json.loads(r.output)["status"] in ("updated", "up_to_date")
    r = run("status", str(repo_copy), "--json")
    assert json.loads(r.output)["freshness"]["stale"] is False
    r = run("hook", "remove", str(repo_copy))
    assert r.exit_code == 0 and not hook.exists()


def test_cli_check_staged_and_working(repo_copy: Path):
    p = repo_copy / "src/core/parser/lexer.py"
    p.write_text(p.read_text() + "".join(f"\ndef extra_{i}(x):\n    return x\n" for i in range(12)))
    r = run("check", str(repo_copy), "--working", "--no-record", "--json")
    assert json.loads(r.output)["rules_fired"] == ["SL-W01"]
    import subprocess
    subprocess.run(["git", "-C", str(repo_copy), "add", "-A"], check=True)
    r = run("check", str(repo_copy), "--staged", "--no-record", "--json")
    assert json.loads(r.output)["rules_fired"] == ["SL-W01"]


def test_cli_replay_runs_both_modes(indexed_repo: Path, tmp_path: Path):
    out = tmp_path / "replay.csv"
    r = run("replay", str(indexed_repo), "--limit", "12", "--out", str(out), "--json")
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    modes = {s["mode"] for s in res["summary"]}
    assert modes == {"shared", "isolated"} and out.exists() and res["rows"] == 24


def test_mcp_server_serves_three_tools(indexed_repo: Path):
    pytest.importorskip("mcp", reason="the mcp extra is not installed")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "senso_lens.mcp_server", str(indexed_repo)])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = [t.name for t in (await s.list_tools()).tools]
                ctx = json.loads((await s.call_tool("get_evolution_context", {"module": "legacy/auth"})).content[0].text)
                gate = json.loads((await s.call_tool("check_change", {"diff": diff("src/core/parser/lexer.py", 30)})).content[0].text)
                exp = json.loads((await s.call_tool("explain_phase", {})).content[0].text)
                one = json.loads((await s.call_tool("explain_phase", {"module": "core/parser"})).content[0].text)
                return names, ctx, gate, exp, one

    names, ctx, gate, exp, one = asyncio.run(go())
    assert names == ["get_evolution_context", "check_change", "explain_phase"]
    assert ctx["phase"] == "decline" and ctx["advice"]
    assert gate["verdict"] == "warn" and gate["rules_fired"] == ["SL-W01"]
    assert "legacy/auth" in exp["phases"]["decline"]
    assert one["phase"] == "stabilizing" and one["rules"]["window_periods"] == 8
