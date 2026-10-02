"""A scripted repository with a known evolution, used by the tests and the demo.

Timeline (relative to `now`, monthly periods, ~36 months):

  core/parser    months 0-17 active (2-4 commits/month), months 18+ one small fix every ~4 months
                 -> stabilizing, load-bearing (api/handlers imports it; early co-change with api)
  api/handlers   months 3+ active, rising, 2 authors                      -> growth, load-bearing
  legacy/auth    months 0-20 active with 2-3 authors, then one author, net deletions,
                 churn falling, months 30+ silent                         -> decline
  auth/service   months 22+ growing, the successor of legacy/auth         -> growth
  scripts/tools  months 6-14 active, nothing imports it, no co-change     -> isolated
  docs           markdown only, ignored by the source filter
  bot noise      dependabot bumps of requirements.txt every other month
  rename         months 24: api/handlers/legacy_names.py -> api/handlers/names.py

Commits are created with GIT_AUTHOR_DATE/GIT_COMMITTER_DATE so the history
is deterministic given `now`.
"""
from __future__ import annotations

import os
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUTHORS = {
    "alice": ("Alice Example", "alice@example.com"),
    "bob": ("Bob Example", "bob@example.com"),
    "carol": ("Carol Example", "carol@example.com"),
    "bot": ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
}


def _git(root: Path, *args: str, env: dict | None = None) -> str:
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    if env:
        e.update(env)
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=e).stdout


def _commit(root: Path, when: datetime, who: str, msg: str) -> None:
    name, email = AUTHORS[who]
    iso = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": iso,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": iso}
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", msg, env=env)


def _append(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _func(name: str, branches: int = 1) -> str:
    body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(branches))
    return f"\n\ndef {name}(x):\n{body}    return x\n"


def build(root: Path, now: datetime | None = None, months: int = 36, seed: int = 7) -> Path:
    """Create the fixture repository at `root` and return its path."""
    rnd = random.Random(seed)
    now = now or datetime.now(tz=timezone.utc)
    # month index `months - 1` is the current (open) month, so the history runs right up to today
    y, mo = now.year, now.month - (months - 1)
    while mo <= 0:
        y, mo = y - 1, mo + 12
    start = datetime(y, mo, 1, 9, tzinfo=timezone.utc)
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.com")

    # month 0: skeleton
    _write(root, "README.md", "# fixture\n")
    _write(root, ".gitignore", ".senso/\n")
    _write(root, "requirements.txt", "requests==2.0.0\n")
    _write(root, "src/core/parser/__init__.py", "from .lexer import tokenize\n")
    _write(root, "src/core/parser/lexer.py", "import re\n\n\ndef tokenize(s):\n    return re.findall(r'\\w+', s)\n")
    _write(root, "src/legacy/auth/__init__.py", "")
    _write(root, "src/legacy/auth/session.py", "import hashlib\n\n\ndef session_id(user):\n    return hashlib.sha1(user.encode()).hexdigest()\n")
    _write(root, "src/legacy/auth/tokens.py", "def make_token(user):\n    return 'tok-' + user\n")
    _commit(root, start, "alice", "initial skeleton")

    def month_dt(m: int, day: int = 3, hour: int = 10) -> datetime:
        y = start.year + (start.month - 1 + m) // 12
        mo = (start.month - 1 + m) % 12 + 1
        d = datetime(y, mo, min(day, 28), hour, tzinfo=timezone.utc)
        if d > now:  # inside the open month: never date a commit in the future
            d = now - timedelta(hours=max(1, 28 - min(day, 28)))
        return d

    parser_n = api_n = legacy_n = auth_n = tools_n = 0
    for m in range(months):
        day = 2
        # --- core/parser: active 0-17, then rare small fixes
        if m < 18:
            for _ in range(rnd.choice([2, 3, 4])):
                parser_n += 1
                _append(root, "src/core/parser/lexer.py", _func(f"rule_{parser_n}", rnd.choice([1, 2, 3])))
                if m >= 3 and rnd.random() < 0.5:
                    # early co-change with api/handlers (the parser's API was still moving)
                    api_n += 1
                    _append(root, "src/api/handlers/routes.py", _func(f"route_{api_n}", 1))
                _commit(root, month_dt(m, day), rnd.choice(["alice", "bob"]), f"parser: add rule {parser_n}")
                day += 2
        elif (m - 18) % 4 == 0:
            parser_n += 1
            _append(root, "src/core/parser/lexer.py", f"\n# fix {parser_n}\n")
            _commit(root, month_dt(m, day), "alice", f"fix: parser edge case {parser_n}")
            day += 2

        # --- api/handlers: starts month 3, grows, two authors
        if m >= 3:
            if m == 3:
                _write(root, "src/api/handlers/__init__.py", "")
                _write(root, "src/api/handlers/routes.py", "from core.parser import tokenize\n\n\ndef handle(req):\n    return tokenize(req)\n")
                _write(root, "src/api/handlers/legacy_names.py", "NAMES = ['a']\n")
            if m == 24:
                _git(root, "mv", "src/api/handlers/legacy_names.py", "src/api/handlers/names.py")
                _commit(root, month_dt(m, day), "bob", "api: rename legacy_names -> names")
                day += 1
            for _ in range(1 + (m - 3) // 8 + rnd.choice([0, 1])):
                api_n += 1
                _append(root, "src/api/handlers/routes.py", _func(f"handler_{api_n}", rnd.choice([1, 2, 4])))
                if rnd.random() < 0.3:
                    _append(root, "src/api/handlers/legacy_names.py" if m < 24 else "src/api/handlers/names.py", f"NAMES.append('n{api_n}')\n")
                _commit(root, month_dt(m, day), rnd.choice(["bob", "carol"]), f"api: handler {api_n}")
                day += 1

        # --- legacy/auth: active 0-20 with 2-3 authors, then withdrawal
        if m <= 20:
            for _ in range(rnd.choice([2, 3])):
                legacy_n += 1
                _append(root, "src/legacy/auth/session.py", _func(f"check_{legacy_n}", rnd.choice([1, 2])))
                _commit(root, month_dt(m, day), rnd.choice(["alice", "bob", "carol"]), f"auth: session check {legacy_n}")
                day += 1
        elif m <= 29:
            if m % 2 == 1:
                # deletions, one author
                p = root / "src/legacy/auth/session.py"
                lines = p.read_text().splitlines(keepends=True)
                keep = max(8, len(lines) - rnd.choice([10, 14, 18]))
                p.write_text("".join(lines[:keep]))
                _commit(root, month_dt(m, day), "alice", "auth: remove dead session code (moving to auth/service)")
                day += 1
        # months 30+: silent

        # --- auth/service: successor, from month 22
        if m >= 22:
            if m == 22:
                _write(root, "src/auth/service/__init__.py", "")
                _write(root, "src/auth/service/core.py", "from legacy.auth import tokens\n\n\ndef login(user):\n    return tokens.make_token(user)\n")
            for _ in range(1 + rnd.choice([0, 1, 2])):
                auth_n += 1
                _append(root, "src/auth/service/core.py", _func(f"flow_{auth_n}", rnd.choice([1, 2, 3])))
                _commit(root, month_dt(m, day), rnd.choice(["alice", "carol"]), f"auth-service: flow {auth_n}")
                day += 1

        # --- scripts/tools: isolated burst months 6-14
        if 6 <= m <= 14:
            if m == 6:
                _write(root, "scripts/tools/__init__.py", "")
            tools_n += 1
            _append(root, "scripts/tools/clean.py", _func(f"clean_{tools_n}", 1))
            _commit(root, month_dt(m, day), "carol", f"tools: cleanup script {tools_n}")
            day += 1

        # --- docs and bot noise
        if m % 3 == 0:
            _append(root, "docs/guide.md", f"\n## month {m}\n")
            _commit(root, month_dt(m, day), "bob", "docs: update guide")
            day += 1
        if m % 2 == 0:
            _write(root, "requirements.txt", f"requests==2.{m}.0\n")
            _commit(root, month_dt(m, day), "bot", f"Bump requests from 2.{m - 2}.0 to 2.{m}.0")
    return root


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture-repo")
    build(target)
    print(target.resolve())
