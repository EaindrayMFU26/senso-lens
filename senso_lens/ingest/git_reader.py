"""The delta reader: the only component that talks to Git during an update.

Four steps, every update:
  1. `HEAD` vs bookmark (a millisecond) — nothing to do if equal.
  2. `merge-base --is-ancestor <bookmark> HEAD` — history still linear?
  3. `git log --reverse --no-merges -M --numstat <bookmark>..HEAD` — only the delta.
  4. Emit one normalized CommitEvent per commit; the engines consume the same stream.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

FIELD_SEP = "\x1f"
RECORD_START = "\x1e"


class GitError(RuntimeError):
    pass


@dataclass
class FileChange:
    path: str
    added: int
    deleted: int
    old_path: str | None = None  # set when git reports a rename
    binary: bool = False


@dataclass
class CommitEvent:
    hash: str
    ts: int
    author: str
    email: str
    subject: str
    parents: list[str]
    files: list[FileChange] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def deleted(self) -> int:
        return sum(f.deleted for f in self.files)


_RENAME_BRACES = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


def parse_numstat_path(raw: str) -> tuple[str, str | None]:
    """Return (new_path, old_path) from a numstat path, handling rename syntax.

    git prints renames as `dir/{old => new}/file`, `old => new`, or with braces
    anywhere in the path. We expand to the full old and new paths.
    """
    m = _RENAME_BRACES.match(raw)
    if m:
        prefix, old, new, suffix = m.groups()
        old_path = (prefix + old + suffix).replace("//", "/")
        new_path = (prefix + new + suffix).replace("//", "/")
        return new_path, old_path
    if " => " in raw:
        old, new = raw.split(" => ", 1)
        return new.strip(), old.strip()
    return raw, None


class GitRepo:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists() and self._run(["rev-parse", "--git-dir"], check=False).returncode != 0:
            raise GitError(f"{self.root} is not a git repository")

    # ------------------------------------------------------------------ plumbing
    def _run(self, args: list[str], check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=text)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() if text else proc.stderr}")
        return proc

    def head(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def branch(self) -> str | None:
        out = self._run(["rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
        return out or None

    def is_shallow(self) -> bool:
        return self._run(["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        return self._run(["merge-base", "--is-ancestor", maybe_ancestor, descendant], check=False).returncode == 0

    def commit_exists(self, h: str) -> bool:
        return self._run(["cat-file", "-e", f"{h}^{{commit}}"], check=False).returncode == 0

    def count_commits(self, rev_range: str | None = None) -> int:
        args = ["rev-list", "--count", "--no-merges", rev_range or "HEAD"]
        return int(self._run(args).stdout.strip() or 0)

    def commit_timestamps(self, rev_range: str | None = None) -> list[int]:
        out = self._run(["log", "--no-merges", "--format=%ct", rev_range or "HEAD"]).stdout
        return [int(x) for x in out.split() if x]

    # ------------------------------------------------------------------ the delta
    def iter_commits(self, since: str | None = None, until: str = "HEAD", first_parent: bool = False) -> Iterator[CommitEvent]:
        """Yield commits oldest-first between `since` (exclusive) and `until`, no merges, renames detected."""
        rev = f"{since}..{until}" if since else until
        fmt = f"{RECORD_START}%H{FIELD_SEP}%ct{FIELD_SEP}%an{FIELD_SEP}%ae{FIELD_SEP}%P{FIELD_SEP}%s"
        args = ["log", "--reverse", "--no-merges", "-M", "--numstat", f"--format={fmt}", rev]
        if first_parent:
            args.insert(1, "--first-parent")
        proc = subprocess.Popen(["git", "-C", str(self.root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        current: CommitEvent | None = None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith(RECORD_START):
                if current is not None:
                    yield current
                parts = line[1:].split(FIELD_SEP)
                if len(parts) < 6:
                    parts += [""] * (6 - len(parts))
                h, ts, an, ae, parents, subject = parts[:6]
                current = CommitEvent(hash=h, ts=int(ts or 0), author=an, email=ae, subject=subject,
                                      parents=[p for p in parents.split() if p])
                continue
            if not line.strip() or current is None:
                continue
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            a, d, raw_path = cols[0], cols[1], "\t".join(cols[2:])
            binary = a == "-" or d == "-"
            new_path, old_path = parse_numstat_path(raw_path)
            current.files.append(FileChange(path=new_path, added=0 if binary else int(a), deleted=0 if binary else int(d),
                                            old_path=old_path, binary=binary))
        if current is not None:
            yield current
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read() if proc.stderr else ""
            raise GitError(f"git log failed: {err.strip()}")

    # ------------------------------------------------------------------ content access (tier 2)
    def ls_tree(self, rev: str = "HEAD") -> dict[str, str]:
        """path -> blob sha for every tracked file at `rev` (the content hash we cache metrics by)."""
        out = self._run(["ls-tree", "-r", "-z", rev]).stdout
        result: dict[str, str] = {}
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob":
                result[path] = parts[2]
        return result

    def blob_bytes(self, blob: str) -> bytes:
        proc = self._run(["cat-file", "blob", blob], text=False)
        return proc.stdout

    def read_many_blobs(self, blobs: list[str]) -> dict[str, bytes]:
        """Batch read via `git cat-file --batch` (one process for all blobs)."""
        if not blobs:
            return {}
        proc = subprocess.run(["git", "-C", str(self.root), "cat-file", "--batch"],
                              input="\n".join(blobs).encode() + b"\n", capture_output=True)
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode(errors="replace"))
        data = proc.stdout
        out: dict[str, bytes] = {}
        i = 0
        n = len(data)
        while i < n:
            nl = data.find(b"\n", i)
            if nl < 0:
                break
            header = data[i:nl].decode(errors="replace").split()
            i = nl + 1
            if len(header) < 3:
                continue  # "<sha> missing"
            sha, _type, size = header[0], header[1], int(header[2])
            out[sha] = data[i:i + size]
            i += size + 1  # trailing newline
        return out

    def diff_staged(self) -> str:
        return self._run(["diff", "--cached", "--no-color", "-M"]).stdout

    def diff_working(self) -> str:
        return self._run(["diff", "--no-color", "-M"]).stdout

    def hooks_dir(self) -> Path:
        out = self._run(["rev-parse", "--git-path", "hooks"]).stdout.strip()
        p = Path(out)
        return p if p.is_absolute() else (self.root / p)

    def exclude_locally(self, pattern: str) -> None:
        """Add a pattern to .git/info/exclude (the repository's private ignore list) if it is not there."""
        out = self._run(["rev-parse", "--git-path", "info/exclude"], check=False).stdout.strip()
        if not out:
            return
        p = Path(out)
        p = p if p.is_absolute() else (self.root / p)
        try:
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
            if pattern not in existing.splitlines():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(existing.rstrip("\n") + ("\n" if existing else "") + pattern + "\n", encoding="utf-8")
        except OSError:
            pass
