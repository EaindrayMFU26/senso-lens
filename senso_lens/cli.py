"""`senso` — the command-line face of SENSO-Lens.

Every command is a thin wrapper over the library (`init_index`, `update_index`,
`evolution_context`, `check_change`, `explain_project`, `replay`), prints a
human view by default and the exact MCP payload with `--json`. The CLI exists
for three reasons: a post-commit hook has to call *something*; the evaluation
scripts need the same code path an agent gets; and a developer without an MCP
client should still be able to ask the index a question.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Config
from .ingest.git_reader import GitError, GitRepo
from .ingest.pipeline import ShallowRepositoryError, init_index, update_index
from .periods import SCALES
from .serve.context import evolution_context, freshness
from .serve.explain import explain_project
from .serve.gate import check_change
from .serve.rules import EXAMPLE_RULES_TOML, RULES, RULES_FILE, load_rule_deck
from .signals.phase import classify_module
from .store.db import open_store, store_path

app = typer.Typer(name="senso", help="SENSO-Lens: evolution context for coding agents, from one shared incremental index.",
                  no_args_is_help=True, add_completion=False, context_settings={"help_option_names": ["-h", "--help"]})
hook_app = typer.Typer(help="Install or remove the post-commit hook that keeps the index current.", no_args_is_help=True)
app.add_typer(hook_app, name="hook")

# when piped (tests, hooks, agents) Rich would assume 80 columns and truncate module names; give it room
console = Console(width=None if sys.stdout.isatty() else 160)
err = Console(stderr=True)

PHASE_STYLE = {"growth": "green", "stabilizing": "cyan", "decline": "yellow", "no_data": "dim"}
VERDICT_STYLE = {"ok": "bold green", "warn": "bold yellow", "block": "bold red"}

PathArg = typer.Argument(".", help="Repository root (default: current directory).", show_default=False)
JsonOpt = typer.Option(False, "--json", help="Print the raw payload (what an MCP client receives).")


def _version(value: bool) -> None:
    if value:
        console.print(f"senso-lens {__version__}")
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show version and exit.")) -> None:
    pass


def _dump(obj) -> None:
    console.print_json(json.dumps(obj, default=str), indent=2, sort_keys=False)


def _repo(path: str) -> GitRepo:
    try:
        return GitRepo(path)
    except GitError as exc:
        err.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2)


def _store_or_exit(repo: GitRepo):
    store = open_store(repo.root)
    if store is None:
        err.print(f"[yellow]no index at {store_path(repo.root)}[/yellow] — run [bold]senso init {repo.root}[/bold] first")
        raise typer.Exit(3)
    return store


def _progress(label: str, done: int, total: Optional[int]) -> None:
    if total:
        err.print(f"  {label}: {done}/{total}", end="\r")
    else:
        err.print(f"  {label}: {done}", end="\r")


def _timing_line(res) -> str:
    return (f"git {res.t_git_ms:.0f} ms · tier1 {res.t_tier1_ms:.0f} ms · tier2 {res.t_tier2_ms:.0f} ms "
            f"· tier3 {res.t_tier3_ms:.0f} ms · total {res.total_ms:.0f} ms")


def _result_dict(res) -> dict:
    d = {k: getattr(res, k) for k in ("status", "from_hash", "to_hash", "commits", "noise_commits", "files_parsed",
                                      "files_touched", "t_git_ms", "t_tier1_ms", "t_tier2_ms", "t_tier3_ms", "scale", "notes")}
    d["total_ms"] = res.total_ms
    return d


# ----------------------------------------------------------------------------------------- init / update
@app.command()
def init(path: str = PathArg,
         scale: str = typer.Option("auto", "--scale", help="Period length: auto | " + " | ".join(SCALES) + ". `auto` applies the time-gap rule."),
         depth: Optional[int] = typer.Option(None, "--depth", min=1, max=6, help="Directory depth that defines a module (default 2)."),
         json_out: bool = JsonOpt) -> None:
    """Mine the whole history once and create `.senso/index.db` (the shared incremental index)."""
    if scale != "auto" and scale not in SCALES:
        err.print(f"[red]error:[/red] --scale must be auto or one of {', '.join(SCALES)}")
        raise typer.Exit(2)
    repo = _repo(path)
    t0 = time.perf_counter()
    try:
        res = init_index(repo.root, scale=scale, module_depth=depth, progress=None if json_out else _progress)
    except ShallowRepositoryError as exc:
        err.print(f"[red]refused:[/red] {exc}")
        raise typer.Exit(4)
    wall = (time.perf_counter() - t0) * 1000
    if json_out:
        _dump({**_result_dict(res), "wall_ms": wall, "index": str(store_path(repo.root))})
        return
    err.print(" " * 60, end="\r")
    console.print(f"[green]✓[/green] indexed [bold]{repo.root.name}[/bold] → {store_path(repo.root)}")
    console.print(f"  {res.commits} commits ({res.noise_commits} noise) · {res.files_parsed} files parsed · scale [bold]{res.scale}[/bold]")
    console.print(f"  {_timing_line(res)} · wall {wall:.0f} ms")
    for n in res.notes:
        console.print(f"  [yellow]note:[/yellow] {n}")
    console.print("  next: [bold]senso hook install[/bold] keeps it current; [bold]senso explain[/bold] shows the phase map")


@app.command()
def update(path: str = PathArg, quiet: bool = typer.Option(False, "--quiet", "-q", help="Print nothing unless something fails."),
           json_out: bool = JsonOpt) -> None:
    """Process the commits since the bookmark (what the post-commit hook runs)."""
    repo = _repo(path)
    if open_store(repo.root) is None:
        if quiet:
            raise typer.Exit(0)
        _store_or_exit(repo)
    res = update_index(repo.root)
    if json_out:
        _dump(_result_dict(res))
        return
    if quiet:
        return
    if res.status == "up_to_date":
        console.print(f"[green]✓[/green] up to date at {res.to_hash[:10]}")
        return
    console.print(f"[green]✓[/green] {res.status}: {res.commits} commit(s) "
                  f"{(res.from_hash or 'start')[:10]}..{res.to_hash[:10]} · {res.files_parsed} files parsed · {_timing_line(res)}")
    for n in res.notes:
        console.print(f"  [yellow]note:[/yellow] {n}")


# ----------------------------------------------------------------------------------------- status
@app.command()
def status(path: str = PathArg, json_out: bool = JsonOpt) -> None:
    """Is there an index, is it fresh, and what is in it."""
    repo = _repo(path)
    store = open_store(repo.root)
    if store is None:
        payload = {"index_present": False, "repository": str(repo.root), "index": str(store_path(repo.root))}
        if json_out:
            _dump(payload)
        else:
            console.print(f"[yellow]no index[/yellow] for {repo.root} — run [bold]senso init[/bold]")
        raise typer.Exit(0)
    cfg = store.load_config()
    fresh = freshness(store, repo)
    counts = store.counts()
    last = store.conn.execute("SELECT * FROM update_log ORDER BY id DESC LIMIT 1").fetchone()
    payload = {
        "index_present": True, "repository": str(repo.root), "index": str(store_path(repo.root)),
        "schema_version": store.schema_version, "scale": cfg.scale, "module_depth": cfg.module_depth,
        "freshness": fresh, "counts": counts,
        "last_update": dict(last) if last else None, "senso_lens": __version__,
    }
    if json_out:
        _dump(payload)
        return
    state = "[green]fresh[/green]" if fresh.get("stale") is False else (
        "[red]history rewritten[/red]" if fresh.get("rewritten") else f"[yellow]stale ({fresh.get('commits_behind')} behind)[/yellow]")
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_row("repository", str(repo.root))
    t.add_row("index", f"{store_path(repo.root)} (schema v{store.schema_version})")
    t.add_row("bookmark", f"{(fresh['bookmark'] or '')[:10]} @ {fresh.get('bookmark_at')}  {state}")
    t.add_row("scale", f"{cfg.scale} periods, module depth {cfg.module_depth}")
    t.add_row("content", f"{counts['modules']} modules · {counts['commits']} commits ({counts['noise_commits']} noise) · "
                         f"{counts['cochange']} co-change rows · {counts['file_state']} files · {counts['imports']} import edges")
    if last:
        t.add_row("last update", f"{last['started_at']} · {last['commits']} commits · git {last['t_git_ms']:.0f} / t1 {last['t_tier1_ms']:.0f} / "
                                 f"t2 {last['t_tier2_ms']:.0f} / t3 {last['t_tier3_ms']:.0f} ms ({last['mode']})")
    console.print(t)


# ----------------------------------------------------------------------------------------- context
@app.command()
def context(module: str = typer.Argument(..., help="Module id, e.g. core/parser (see `senso explain` for the list)."),
            path: str = PathArg, json_out: bool = JsonOpt) -> None:
    """get_evolution_context for one module — the packet an agent receives."""
    repo = _repo(path)
    store = _store_or_exit(repo)
    t0 = time.perf_counter()
    payload = evolution_context(store, module, repo)
    ms = (time.perf_counter() - t0) * 1000
    if json_out:
        _dump(payload)
        return
    if not payload["known"]:
        console.print(f"[yellow]{module}[/yellow] is not in the index. Known modules: {', '.join(store.modules()[:12])}"
                      + (" …" if len(store.modules()) > 12 else ""))
        raise typer.Exit(1)
    ph = payload["phase"]
    head = (f"[{PHASE_STYLE.get(ph, '')}]{ph}[/] since {payload['phase_since']} ({payload['confidence']} confidence) · "
            f"{payload['connectedness']['status']} (fan-in {payload['connectedness']['fan_in']}, "
            f"{payload['connectedness']['cochange_partners']} co-change partners)")
    body = [head, "", "[bold]why[/bold]: " + "; ".join(payload["reasons"])]
    tr = payload["trends"]
    body.append(f"[bold]churn[/bold] {tr['churn']['direction']} {tr['churn']['last_periods']}  ·  [bold]size[/bold] {tr['size']['direction']}"
                f"  ·  authors {tr['authors']['recent']} recent / {tr['authors']['prior']} prior  ·  window closed through {payload['window']['closed_through']}")
    if payload["co_changes_with"]:
        body.append("[bold]changes with[/bold]: " + ", ".join(
            f"{p['module']} (w {p['weight']}, {p['lifetime']} commits, {p['trend']})" for p in payload["co_changes_with"]))
    if payload["maintainability"]:
        m = payload["maintainability"]
        body.append(f"[bold]maintainability[/bold] {m['composite']} · {m['files']} files · {m['nloc']} NLOC · CCN avg {m['ccn_avg']} max {m['ccn_max']}")
    if payload["package_metrics"]:
        pm = payload["package_metrics"]
        body.append(f"[bold]package[/bold] Ca {pm['ca']} Ce {pm['ce']} I {pm['instability']} A {pm['abstractness']} D {pm['distance']}"
                    + (" [dim](stale)[/dim]" if pm["stale"] else ""))
    ev = payload["evidence"]
    body.append(f"[bold]evidence[/bold] {ev['commits_analyzed']} commits · " + "; ".join(
        f"{c['hash']} {c['when']} {c['subject']!r}" for c in ev["reference_commits"]))
    body.append("")
    body.append(f"[bold]advice[/bold]: {payload['advice']}")
    fr = payload["freshness"]
    foot = f"index {'fresh' if fr.get('stale') is False else 'STALE'} · {ms:.1f} ms"
    console.print(Panel("\n".join(body), title=f"[bold]{module}[/bold]", subtitle=foot, expand=False))


# ----------------------------------------------------------------------------------------- check
@app.command()
def check(path: str = PathArg,
          diff: Optional[Path] = typer.Option(None, "--diff", help="Unified diff file ('-' for stdin)."),
          staged: bool = typer.Option(False, "--staged", help="Check `git diff --cached`."),
          working: bool = typer.Option(False, "--working", help="Check `git diff` (unstaged changes)."),
          allow_stale: bool = typer.Option(False, "--allow-stale", help="Do not fail-safe on a stale index."),
          no_record: bool = typer.Option(False, "--no-record", help="Do not write to the gate log (no escalation)."),
          json_out: bool = JsonOpt) -> None:
    """check_change: the fail-safe pre-edit gate. Exit 0 ok, 1 warn, 2 block."""
    repo = _repo(path)
    if staged:
        text = repo.diff_staged()
    elif working:
        text = repo.diff_working()
    elif diff is not None and str(diff) != "-":
        text = diff.read_text(encoding="utf-8", errors="replace")
    elif diff is not None or not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        err.print("[red]error:[/red] give --diff FILE, --staged, --working, or pipe a diff on stdin")
        raise typer.Exit(2)
    store = open_store(repo.root)
    result = check_change(store, repo, text, allow_stale=allow_stale, record=not no_record)
    if json_out:
        _dump(result)
    else:
        v = result["verdict"]
        console.print(f"[{VERDICT_STYLE[v]}]{v.upper()}[/] deviation {result['deviation']}/4 · {result['reason']}")
        for f in result["findings"]:
            tag = "[dim]suppressed[/dim] " if f.get("suppressed_by") else ""
            console.print(f"  [{'yellow' if f['severity'] == 'warn' else 'dim'}]{f['rule']}[/] {tag}{f['module']}: {f['message']}")
        if result["authority"]:
            console.print(f"  [green]authority[/green] (new code may go here freely): {', '.join(result['authority'])}")
        if result["rule_deck"]["expired_suppressions"]:
            console.print(f"  [yellow]expired suppressions:[/yellow] {len(result['rule_deck']['expired_suppressions'])} (see `senso rules`)")
    raise typer.Exit({"ok": 0, "warn": 1, "block": 2}[result["verdict"]])


# ----------------------------------------------------------------------------------------- explain
@app.command()
def explain(path: str = PathArg, module: Optional[str] = typer.Option(None, "--module", "-m", help="Explain one module's phase in depth."),
            top: int = typer.Option(8, "--top", help="Rows per table."), json_out: bool = JsonOpt) -> None:
    """explain_phase: the phase map of the whole repository, or why one module has its phase."""
    repo = _repo(path)
    store = _store_or_exit(repo)
    if module:
        cfg = store.load_config()
        if module not in set(store.modules()):
            err.print(f"[yellow]{module}[/yellow] is not in the index")
            raise typer.Exit(1)
        ph = classify_module(store, module, cfg)
        payload = {"module": module, **ph.to_dict(), "rules": _phase_rules_text(cfg)}
        if json_out:
            _dump(payload)
            return
        console.print(f"[bold]{module}[/bold]: [{PHASE_STYLE.get(ph.phase, '')}]{ph.phase}[/] since {ph.since} ({ph.confidence})")
        for r in ph.reasons:
            console.print(f"  • {r}")
        t = Table(title=f"last {len(ph.window)} closed {cfg.scale} periods (open period {ph.open_period.get('period')} excluded)", box=None)
        t.add_column("period")
        t.add_column("commits", justify="right")
        t.add_column("net lines", justify="right")
        for p, c, n in zip(ph.window, ph.commits_window, ph.net_lines_window or [None] * len(ph.window)):
            t.add_row(p, str(c), "" if n is None else f"{n:+d}")
        console.print(t)
        console.print(f"  churn p={ph.p_churn} ({ph.churn_trend}) · size p={ph.p_size} ({ph.size_trend}) · authors {ph.authors_recent} recent / {ph.authors_prior} prior")
        console.print(f"[dim]{_phase_rules_text(cfg)}[/dim]")
        return
    payload = explain_project(store, repo, top=top)
    if json_out:
        _dump(payload)
        return
    console.print(Panel(payload["summary"], title="[bold]SENSO-Lens[/bold] " + str(payload["repository"]), expand=False))
    t = Table(title="modules", box=None)
    for col in ("module", "phase", "since", "conf", "connectedness", "fan-in", "partners", "churn/period", "NLOC", "MI"):
        t.add_column(col, justify="right" if col in ("fan-in", "partners", "churn/period", "NLOC", "MI") else "left",
                     no_wrap=col in ("module", "phase", "since", "connectedness"))
    for r in payload["modules"][: top * 3]:
        t.add_row(r["module"], f"[{PHASE_STYLE.get(r['phase'], '')}]{r['phase']}[/]", r["since"] or "", r["confidence"] or "",
                  r["connectedness"], str(r["fan_in"]), str(r["partners"]), str(r["churn_level"]),
                  "" if r["nloc"] is None else str(r["nloc"]), "" if r["maintainability"] is None else str(r["maintainability"]))
    console.print(t)
    if payload["hotspots"]:
        h = Table(title="hotspots (churn × complexity over the window)", box=None)
        for col in ("module", "commits", "lines", "ccn avg", "score"):
            h.add_column(col, justify="left" if col == "module" else "right")
        for r in payload["hotspots"]:
            h.add_row(r["module"], str(r["commits"]), str(r["lines"]), "" if r["ccn_avg"] is None else str(r["ccn_avg"]), str(r["score"]))
        console.print(h)
    if payload["strongest_couplings"]:
        c = Table(title="strongest co-change couplings (decayed weight)", box=None)
        for col in ("modules", "weight", "lifetime", "trend"):
            c.add_column(col, justify="left" if col in ("modules", "trend") else "right")
        for r in payload["strongest_couplings"]:
            c.add_row(" ↔ ".join(r["modules"]), str(r["weight"]), str(r["lifetime"]), r["trend"])
        console.print(c)
    if payload["gate_last_30_days"]:
        g = Table(title="gate findings, last 30 days", box=None)
        for col in ("module", "rule", "count", "max deviation"):
            g.add_column(col, justify="left" if col in ("module", "rule") else "right")
        for r in payload["gate_last_30_days"]:
            g.add_row(r["module"], r["rule"], str(r["count"]), str(r["max_deviation"]))
        console.print(g)
    fr = payload["freshness"]
    console.print(f"[dim]{payload['history']['first_commit']} → {payload['history']['last_commit']} · {payload['scale']} periods · "
                  f"index {'fresh' if fr.get('stale') is False else 'STALE'}[/dim]")


def _phase_rules_text(cfg: Config) -> str:
    return (f"rules: window W={cfg.phase_window} closed periods, K={cfg.phase_k_stable}, low churn ≤ {cfg.phase_low_churn:g} commits/period, "
            f"Mann-Kendall α={cfg.phase_alpha}; decline needs ≥2 of (churn falling, net deletions, authors leaving) after prior activity; "
            f"stabilizing needs {cfg.phase_k_stable} consecutive low periods; ties → growth, low confidence; < {cfg.phase_min_periods} periods → no_data")


# ----------------------------------------------------------------------------------------- rules
@app.command()
def rules(path: str = PathArg, init_file: bool = typer.Option(False, "--init", help=f"Write an example {RULES_FILE} if none exists."),
          json_out: bool = JsonOpt) -> None:
    """Show the rule deck: every rule id, its effective severity, and active/expired suppressions."""
    repo = _repo(path)
    target = repo.root / RULES_FILE
    if init_file:
        if target.exists():
            err.print(f"{target} already exists; not overwriting")
            raise typer.Exit(1)
        target.write_text(EXAMPLE_RULES_TOML, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {target}")
        return
    deck = load_rule_deck(repo.root)
    payload = {
        "source": deck.source,
        "rules": [{"id": r.id, "name": r.name, "severity": deck.severity(r.id), "default": r.severity, "family": r.family,
                   "description": r.description, "rationale": r.rationale} for r in RULES.values()],
        "suppressions": [{**s.to_dict(), "active": s.active()} for s in deck.suppressions],
    }
    if json_out:
        _dump(payload)
        return
    t = Table(title=f"rule deck ({deck.source or 'defaults; no ' + RULES_FILE})", box=None)
    for col in ("id", "name", "severity", "description"):
        t.add_column(col)
    for r in payload["rules"]:
        sev = r["severity"] + ("" if r["severity"] == r["default"] else f" (default {r['default']})")
        t.add_row(r["id"], r["name"], sev, r["description"])
    console.print(t)
    if payload["suppressions"]:
        s = Table(title="suppressions", box=None)
        for col in ("rule", "module", "by", "until", "state", "reason"):
            s.add_column(col)
        for x in payload["suppressions"]:
            s.add_row(x["rule"], x["module"], x["by"], x["until"], "[green]active[/green]" if x["active"] else "[yellow]expired[/yellow]", x["reason"])
        console.print(s)


# ----------------------------------------------------------------------------------------- hook
HOOK_BEGIN = "# >>> senso-lens post-commit >>>"
HOOK_END = "# <<< senso-lens post-commit <<<"
HOOK_BODY = f"""{HOOK_BEGIN}
# Advance the shared incremental index by the commit that was just made.
# Never blocks or fails the commit: if `senso` is missing or errors, git carries on.
if command -v senso >/dev/null 2>&1; then
  senso update "$(git rev-parse --show-toplevel)" --quiet || true
fi
{HOOK_END}
"""


@hook_app.command("install")
def hook_install(path: str = PathArg) -> None:
    """Append the SENSO-Lens block to .git/hooks/post-commit (idempotent, chains with an existing hook)."""
    repo = _repo(path)
    hook = repo.hooks_dir() / "post-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    existing = hook.read_text(encoding="utf-8") if hook.exists() else ""
    if HOOK_BEGIN in existing:
        console.print(f"already installed: {hook}")
        return
    content = (existing.rstrip("\n") + "\n\n" if existing else "#!/bin/sh\n") + HOOK_BODY
    hook.write_text(content, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    console.print(f"[green]✓[/green] installed post-commit hook: {hook}")


@hook_app.command("remove")
def hook_remove(path: str = PathArg) -> None:
    """Remove the SENSO-Lens block from the post-commit hook."""
    repo = _repo(path)
    hook = repo.hooks_dir() / "post-commit"
    if not hook.exists():
        console.print("no post-commit hook")
        return
    text = hook.read_text(encoding="utf-8")
    if HOOK_BEGIN not in text:
        console.print("senso-lens block not present")
        return
    start, end = text.index(HOOK_BEGIN), text.index(HOOK_END) + len(HOOK_END)
    rest = (text[:start] + text[end:]).strip()
    if rest in ("", "#!/bin/sh"):
        hook.unlink()
        console.print(f"[green]✓[/green] removed {hook}")
    else:
        hook.write_text(rest + "\n", encoding="utf-8")
        console.print(f"[green]✓[/green] removed senso-lens block from {hook}")


# ----------------------------------------------------------------------------------------- export
@app.command()
def export(path: str = PathArg, fmt: str = typer.Option("csv", "--format", help="csv | json"),
           out: Path = typer.Option(Path("senso-export"), "--out", help="Output directory.")) -> None:
    """Dump the index tables (churn per period, co-change, metrics, phases) for the evaluation scripts."""
    import csv as _csv
    repo = _repo(path)
    store = _store_or_exit(repo)
    cfg = store.load_config()
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "module_period": "SELECT * FROM module_period ORDER BY module, period",
        "cochange": "SELECT * FROM cochange ORDER BY module_a, module_b, period",
        "module_metrics": "SELECT * FROM module_metrics ORDER BY module",
        "package_metrics": "SELECT * FROM package_metrics ORDER BY module",
        "imports": "SELECT * FROM imports ORDER BY src_module, dst_module",
        "commits": "SELECT hash, ts, author, period, files, added, deleted, is_noise, noise_reason, modules FROM commits ORDER BY ts",
        "update_log": "SELECT * FROM update_log ORDER BY id",
        "gate_log": "SELECT * FROM gate_log ORDER BY id",
    }
    data = {name: [dict(r) for r in store.conn.execute(sql)] for name, sql in tables.items()}
    data["phases"] = []
    for m in store.modules():
        ph = classify_module(store, m, cfg)
        data["phases"].append({"module": m, "phase": ph.phase, "since": ph.since, "confidence": ph.confidence,
                               "churn_trend": ph.churn_trend, "size_trend": ph.size_trend, "churn_level": round(ph.churn_level, 3),
                               "low_streak": ph.low_streak, "authors_recent": ph.authors_recent, "authors_prior": ph.authors_prior,
                               "p_churn": ph.p_churn, "p_size": ph.p_size, "reasons": "; ".join(ph.reasons)})
    if fmt == "json":
        (out / "index.json").write_text(json.dumps({"scale": cfg.scale, **data}, indent=1, default=str), encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {out / 'index.json'}")
        return
    for name, rows in data.items():
        p = out / f"{name}.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            if not rows:
                f.write("")
                continue
            w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    console.print(f"[green]✓[/green] wrote {len(data)} CSV files to {out}/ (scale {cfg.scale})")


# ----------------------------------------------------------------------------------------- replay (RQ2)
@app.command()
def replay(path: str = PathArg, mode: str = typer.Option("both", "--mode", help="shared | isolated | both"),
           out: Path = typer.Option(Path("benchmarks/results/replay.csv"), "--out", help="CSV of per-commit timings."),
           limit: Optional[int] = typer.Option(None, "--limit", help="Replay only the last N commits (warm-up to the commit before)."),
           scale: str = typer.Option("monthly", "--scale"), json_out: bool = JsonOpt) -> None:
    """Commit-replay benchmark: per-commit update cost, one shared index vs three isolated ones (same code)."""
    from .replay import replay as _replay, summarize, write_csv
    if mode not in ("shared", "isolated", "both"):
        err.print("[red]error:[/red] --mode must be shared, isolated or both")
        raise typer.Exit(2)
    repo = _repo(path)

    def prog(m: str, i: int, n: int) -> None:
        if not json_out:
            err.print(f"  {m}: {i}/{n}", end="\r")

    rows = _replay(repo.root, mode=mode, limit=limit, scale=scale, progress=prog)
    write_csv(rows, out)
    summary = summarize(rows)
    if json_out:
        _dump({"csv": str(out), "rows": len(rows), "summary": summary})
        return
    err.print(" " * 40, end="\r")
    t = Table(title=f"per-commit update cost, median ms by changeset size ({len(rows)} rows → {out})", box=None)
    for col in ("mode", "files", "n", "median", "p95", "git", "tier1", "tier2", "tier3"):
        t.add_column(col, justify="left" if col in ("mode", "files") else "right")
    for s in summary:
        t.add_row(s["mode"], s["bucket"], str(s["n"]), str(s["median_total_ms"]), str(s["p95_total_ms"]), str(s["median_git_ms"]),
                  str(s["median_tier1_ms"]), str(s["median_tier2_ms"]), str(s["median_tier3_ms"]))
    console.print(t)


# ----------------------------------------------------------------------------------------- mcp
@app.command()
def mcp(path: str = PathArg) -> None:
    """Serve get_evolution_context / check_change / explain_phase over MCP (stdio)."""
    repo = _repo(path)
    try:
        from .mcp_server import serve
    except ImportError:
        err.print("[red]error:[/red] the MCP extra is not installed: pip install 'senso-lens[mcp]'")
        raise typer.Exit(2)
    serve(repo.root)


def main() -> None:  # console_scripts entry point
    app()


if __name__ == "__main__":
    main()
