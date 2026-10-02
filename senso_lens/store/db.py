"""The temporal graph store: one embedded SQLite file per repository (`.senso/index.db`).

Everything an agent query reads is precomputed here. Tier-1 tables are
running totals that accept per-commit contributions; tier-2 tables are keyed
by git blob hash so a file is parsed once per content; tier-3 tables carry a
dirty flag so transitive package metrics can be refreshed lazily.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import SCHEMA_VERSION
from ..config import Config

SENSO_DIR = ".senso"
DB_NAME = "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS commits (
  hash TEXT PRIMARY KEY, ts INTEGER NOT NULL, author TEXT, email TEXT, subject TEXT,
  period TEXT NOT NULL, files INTEGER NOT NULL, added INTEGER NOT NULL, deleted INTEGER NOT NULL,
  is_noise INTEGER NOT NULL DEFAULT 0, noise_reason TEXT, modules INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commits_ts ON commits(ts);

CREATE TABLE IF NOT EXISTS commit_modules (
  hash TEXT NOT NULL, module TEXT NOT NULL, files INTEGER NOT NULL, added INTEGER NOT NULL, deleted INTEGER NOT NULL,
  PRIMARY KEY (hash, module)
);
CREATE INDEX IF NOT EXISTS idx_commit_modules_module ON commit_modules(module);

CREATE TABLE IF NOT EXISTS module_period (
  module TEXT NOT NULL, period TEXT NOT NULL, commits INTEGER NOT NULL DEFAULT 0,
  added INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, files INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (module, period)
);

CREATE TABLE IF NOT EXISTS module_period_authors (
  module TEXT NOT NULL, period TEXT NOT NULL, author TEXT NOT NULL, commits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (module, period, author)
);

CREATE TABLE IF NOT EXISTS cochange (
  module_a TEXT NOT NULL, module_b TEXT NOT NULL, period TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  last_hash TEXT, last_ts INTEGER,
  PRIMARY KEY (module_a, module_b, period)
);
CREATE INDEX IF NOT EXISTS idx_cochange_a ON cochange(module_a);
CREATE INDEX IF NOT EXISTS idx_cochange_b ON cochange(module_b);

CREATE TABLE IF NOT EXISTS blob_metrics (
  blob TEXT PRIMARY KEY, language TEXT, nloc INTEGER, functions INTEGER, ccn_avg REAL, ccn_max INTEGER,
  mi REAL, classes INTEGER, abstract_classes INTEGER, imports TEXT
);

CREATE TABLE IF NOT EXISTS file_state (
  path TEXT PRIMARY KEY, module TEXT NOT NULL, blob TEXT NOT NULL, updated_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_state_module ON file_state(module);

CREATE TABLE IF NOT EXISTS module_metrics (
  module TEXT PRIMARY KEY, files INTEGER, nloc INTEGER, functions INTEGER, ccn_avg REAL, ccn_max INTEGER,
  maintainability REAL, classes INTEGER, abstract_classes INTEGER, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS imports (
  src_module TEXT NOT NULL, dst_module TEXT NOT NULL, src_path TEXT NOT NULL,
  PRIMARY KEY (src_path, dst_module)
);
CREATE INDEX IF NOT EXISTS idx_imports_dst ON imports(dst_module);

CREATE TABLE IF NOT EXISTS package_metrics (
  module TEXT PRIMARY KEY, ca INTEGER, ce INTEGER, instability REAL, abstractness REAL, distance REAL,
  dirty INTEGER NOT NULL DEFAULT 1, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS renames (
  old_path TEXT NOT NULL, new_path TEXT NOT NULL, hash TEXT NOT NULL, ts INTEGER NOT NULL,
  PRIMARY KEY (old_path, new_path, hash)
);

CREATE TABLE IF NOT EXISTS update_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, from_hash TEXT, to_hash TEXT,
  commits INTEGER, files_parsed INTEGER, t_git_ms REAL, t_tier1_ms REAL, t_tier2_ms REAL, t_tier3_ms REAL,
  mode TEXT
);

CREATE TABLE IF NOT EXISTS gate_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, module TEXT NOT NULL, rule TEXT NOT NULL,
  verdict TEXT NOT NULL, deviation INTEGER NOT NULL, diff_hash TEXT, resolved INTEGER NOT NULL DEFAULT 0
);
"""


def store_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / SENSO_DIR / DB_NAME


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def bookmark(self) -> str | None:
        return self.get_meta("bookmark")

    @bookmark.setter
    def bookmark(self, value: str) -> None:
        self.set_meta("bookmark", value)
        self.set_meta("bookmark_at", datetime.now(tz=timezone.utc).isoformat(timespec="seconds"))

    def load_config(self) -> Config:
        raw = self.get_meta("config")
        return Config.from_dict(json.loads(raw)) if raw else Config()

    def save_config(self, cfg: Config) -> None:
        self.set_meta("config", json.dumps(cfg.to_dict()))

    @property
    def schema_version(self) -> int:
        return int(self.get_meta("schema_version") or 0)

    # ------------------------------------------------------------------ txn
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    def reset_history(self) -> None:
        """Drop every derived table but keep the blob cache (content-addressed, still valid)."""
        with self.tx() as c:
            for t in ("commits", "commit_modules", "module_period", "module_period_authors", "cochange",
                      "file_state", "module_metrics", "imports", "package_metrics", "renames"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM meta WHERE key IN ('bookmark','bookmark_at','first_ts','last_ts')")

    # ------------------------------------------------------------------ tier 1 writes
    def has_commit(self, h: str) -> bool:
        return self.conn.execute("SELECT 1 FROM commits WHERE hash=?", (h,)).fetchone() is not None

    def add_commit(self, h: str, ts: int, author: str, email: str, subject: str, period: str,
                   files: int, added: int, deleted: int, is_noise: bool, noise_reason: str | None,
                   modules: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO commits(hash,ts,author,email,subject,period,files,added,deleted,is_noise,noise_reason,modules)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, ts, author, email, subject, period, files, added, deleted, int(is_noise), noise_reason, modules))

    def add_commit_module(self, h: str, module: str, files: int, added: int, deleted: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO commit_modules(hash,module,files,added,deleted) VALUES(?,?,?,?,?)",
                          (h, module, files, added, deleted))

    def bump_module_period(self, module: str, period: str, added: int, deleted: int, files: int, author: str) -> None:
        self.conn.execute(
            "INSERT INTO module_period(module,period,commits,added,deleted,files) VALUES(?,?,1,?,?,?)"
            " ON CONFLICT(module,period) DO UPDATE SET commits=commits+1, added=added+excluded.added,"
            " deleted=deleted+excluded.deleted, files=files+excluded.files", (module, period, added, deleted, files))
        self.conn.execute(
            "INSERT INTO module_period_authors(module,period,author,commits) VALUES(?,?,?,1)"
            " ON CONFLICT(module,period,author) DO UPDATE SET commits=commits+1", (module, period, author))

    def bump_cochange(self, a: str, b: str, period: str, h: str, ts: int) -> None:
        if a > b:
            a, b = b, a
        self.conn.execute(
            "INSERT INTO cochange(module_a,module_b,period,count,last_hash,last_ts) VALUES(?,?,?,1,?,?)"
            " ON CONFLICT(module_a,module_b,period) DO UPDATE SET count=count+1, last_hash=excluded.last_hash, last_ts=excluded.last_ts",
            (a, b, period, h, ts))

    def add_rename(self, old: str, new: str, h: str, ts: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO renames(old_path,new_path,hash,ts) VALUES(?,?,?,?)", (old, new, h, ts))

    # ------------------------------------------------------------------ tier 2
    def get_blob_metrics(self, blob: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM blob_metrics WHERE blob=?", (blob,)).fetchone()

    def put_blob_metrics(self, blob: str, m: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO blob_metrics(blob,language,nloc,functions,ccn_avg,ccn_max,mi,classes,abstract_classes,imports)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (blob, m.get("language"), m.get("nloc"), m.get("functions"), m.get("ccn_avg"), m.get("ccn_max"),
             m.get("mi"), m.get("classes"), m.get("abstract_classes"), json.dumps(m.get("imports") or [])))

    def set_file_state(self, path: str, module: str, blob: str, updated_hash: str | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO file_state(path,module,blob,updated_hash) VALUES(?,?,?,?)",
                          (path, module, blob, updated_hash))

    def delete_file_state(self, path: str) -> None:
        self.conn.execute("DELETE FROM file_state WHERE path=?", (path,))
        self.conn.execute("DELETE FROM imports WHERE src_path=?", (path,))

    def replace_imports(self, src_path: str, src_module: str, dst_modules: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM imports WHERE src_path=?", (src_path,))
        for dst in set(dst_modules):
            if dst and dst != src_module:
                self.conn.execute("INSERT OR IGNORE INTO imports(src_module,dst_module,src_path) VALUES(?,?,?)",
                                  (src_module, dst, src_path))

    def module_files(self, module: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT f.path, f.blob, b.* FROM file_state f LEFT JOIN blob_metrics b ON b.blob=f.blob WHERE f.module=?",
            (module,)).fetchall()

    def put_module_metrics(self, module: str, m: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO module_metrics(module,files,nloc,functions,ccn_avg,ccn_max,maintainability,classes,abstract_classes,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (module, m.get("files"), m.get("nloc"), m.get("functions"), m.get("ccn_avg"), m.get("ccn_max"),
             m.get("maintainability"), m.get("classes"), m.get("abstract_classes"),
             datetime.now(tz=timezone.utc).isoformat(timespec="seconds")))

    def get_module_metrics(self, module: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM module_metrics WHERE module=?", (module,)).fetchone()

    # ------------------------------------------------------------------ tier 3
    def mark_dirty(self, modules: Iterable[str]) -> None:
        for m in set(modules):
            self.conn.execute(
                "INSERT INTO package_metrics(module,dirty) VALUES(?,1) ON CONFLICT(module) DO UPDATE SET dirty=1", (m,))

    def dirty_modules(self) -> list[str]:
        return [r["module"] for r in self.conn.execute("SELECT module FROM package_metrics WHERE dirty=1")]

    def put_package_metrics(self, module: str, ca: int, ce: int, instability: float | None,
                            abstractness: float | None, distance: float | None) -> None:
        self.conn.execute(
            "INSERT INTO package_metrics(module,ca,ce,instability,abstractness,distance,dirty,updated_at) VALUES(?,?,?,?,?,?,0,?)"
            " ON CONFLICT(module) DO UPDATE SET ca=excluded.ca, ce=excluded.ce, instability=excluded.instability,"
            " abstractness=excluded.abstractness, distance=excluded.distance, dirty=0, updated_at=excluded.updated_at",
            (module, ca, ce, instability, abstractness, distance, datetime.now(tz=timezone.utc).isoformat(timespec="seconds")))

    def get_package_metrics(self, module: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM package_metrics WHERE module=?", (module,)).fetchone()

    # ------------------------------------------------------------------ reads
    def modules(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT module FROM module_period ORDER BY module").fetchall()
        return [r["module"] for r in rows]

    def module_series(self, module: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT period, commits, added, deleted, files FROM module_period WHERE module=? ORDER BY period", (module,)).fetchall()

    def module_authors_by_period(self, module: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT period, COUNT(*) AS n FROM module_period_authors WHERE module=? GROUP BY period", (module,)).fetchall()
        return {r["period"]: r["n"] for r in rows}

    def module_recent_commits(self, module: str, limit: int = 3) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.hash, c.ts, c.author, c.subject, cm.added, cm.deleted FROM commit_modules cm JOIN commits c ON c.hash=cm.hash"
            " WHERE cm.module=? AND c.is_noise=0 ORDER BY c.ts DESC LIMIT ?", (module, limit)).fetchall()

    def cochange_rows(self, module: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT module_a, module_b, period, count, last_hash, last_ts FROM cochange WHERE module_a=? OR module_b=?",
            (module, module)).fetchall()

    def cochange_pair(self, a: str, b: str) -> list[sqlite3.Row]:
        if a > b:
            a, b = b, a
        return self.conn.execute("SELECT period, count FROM cochange WHERE module_a=? AND module_b=? ORDER BY period", (a, b)).fetchall()

    def fan_in(self, module: str, include_tests: bool = False) -> int:
        """Distinct modules importing `module`. Test modules import everything, so they are not counted
        unless asked: a module only tests depend on is not load-bearing."""
        from ..ingest.modules import is_test_module
        srcs = [r["src_module"] for r in self.conn.execute("SELECT DISTINCT src_module FROM imports WHERE dst_module=?", (module,))]
        return len(srcs) if include_tests else sum(1 for m in srcs if not is_test_module(m))

    def fan_out(self, module: str) -> int:
        row = self.conn.execute("SELECT COUNT(DISTINCT dst_module) AS n FROM imports WHERE src_module=?", (module,)).fetchone()
        return int(row["n"]) if row else 0

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("commits", "module_period", "cochange", "file_state", "blob_metrics", "imports", "gate_log"):
            out[t] = int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
        out["noise_commits"] = int(self.conn.execute("SELECT COUNT(*) AS n FROM commits WHERE is_noise=1").fetchone()["n"])
        out["modules"] = len(self.modules())
        return out

    def log_update(self, **kw: Any) -> None:
        self.conn.execute(
            "INSERT INTO update_log(started_at,from_hash,to_hash,commits,files_parsed,t_git_ms,t_tier1_ms,t_tier2_ms,t_tier3_ms,mode)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kw.get("started_at"), kw.get("from_hash"), kw.get("to_hash"), kw.get("commits"), kw.get("files_parsed"),
             kw.get("t_git_ms"), kw.get("t_tier1_ms"), kw.get("t_tier2_ms"), kw.get("t_tier3_ms"), kw.get("mode")))

    # ------------------------------------------------------------------ gate log
    def log_gate(self, ts: int, module: str, rule: str, verdict: str, deviation: int, diff_hash: str | None) -> None:
        self.conn.execute("INSERT INTO gate_log(ts,module,rule,verdict,deviation,diff_hash) VALUES(?,?,?,?,?,?)",
                          (ts, module, rule, verdict, deviation, diff_hash))

    def unresolved_count(self, module: str, rule: str, since_ts: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM gate_log WHERE module=? AND rule=? AND resolved=0 AND ts>=?",
            (module, rule, since_ts)).fetchone()
        return int(row["n"]) if row else 0

    def resolve_gate(self, module: str) -> None:
        self.conn.execute("UPDATE gate_log SET resolved=1 WHERE module=? AND resolved=0", (module,))

    def gate_summary(self, since_ts: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT module, rule, COUNT(*) AS n, MAX(deviation) AS max_dev FROM gate_log WHERE ts>=? GROUP BY module, rule ORDER BY n DESC",
            (since_ts,)).fetchall()


def open_store(repo_root: str | Path, create: bool = False) -> Store | None:
    p = store_path(repo_root)
    if not p.exists() and not create:
        return None
    return Store(p)
