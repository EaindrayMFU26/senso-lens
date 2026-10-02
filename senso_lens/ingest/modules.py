"""Module mapper: file path -> module (a directory prefix), with ignore rules.

Modules are the unit of every signal. The mapping is deterministic and cheap:
strip a leading source prefix (src/, lib/, ...) when a directory follows it,
then take the first `module_depth` path components. Files at the repository
root form the module "(root)". Ignored paths (vendored, generated, lockfiles,
binaries) map to None. Test modules are recognised so that their imports do not
make production modules look load-bearing.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

from ..config import Config, IGNORE_PATH_PATTERNS, SOURCE_EXTENSIONS, TEST_MODULE_PATTERNS

ROOT_MODULE = "(root)"
_TEST_RX = [re.compile(p) for p in TEST_MODULE_PATTERNS]


def is_test_module(module: str) -> bool:
    return any(rx.search(module) for rx in _TEST_RX)


class ModuleMapper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ignore = [re.compile(p) for p in IGNORE_PATH_PATTERNS]
        self._strip = set(cfg.strip_prefixes)

    def is_ignored(self, path: str) -> bool:
        return any(rx.search(path) for rx in self._ignore)

    def is_source(self, path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS

    def module_of(self, path: str) -> str | None:
        """Module for a path, or None if the path is ignored."""
        if not path or self.is_ignored(path):
            return None
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        # strip one leading source prefix (src/, lib/, packages/ ...) — but only when a directory
        # follows it; a flat `lib/*.js` layout is the module "lib", not the repository root
        if len(parts) > 2 and parts[0] in self._strip:
            parts = parts[1:]
        dirs = parts[:-1]
        if not dirs:
            return ROOT_MODULE
        depth = max(1, self.cfg.module_depth)
        return "/".join(dirs[:depth])

    def resolve_import_target(self, src_path: str, target: str, known_paths: set[str]) -> str | None:
        """Best-effort resolution of an import specifier to a repository path's module.

        `target` is a dotted (python/java) or slash/relative (js/ts/go) name.
        We return the module of the first known path that matches.
        """
        candidates: list[str] = []
        if target.startswith("."):
            # relative import (JS/TS): resolve against the source directory
            base = PurePosixPath(src_path).parent
            rel = PurePosixPath(os.path.normpath(str(base / target)))
            candidates.append(str(rel))
        else:
            slashed = target.replace(".", "/")
            candidates.extend([slashed, target])
        for cand in candidates:
            for ext in ("", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".kt", "/__init__.py", "/index.ts", "/index.js", "/mod.rs"):
                p = cand + ext
                if p in known_paths:
                    return self.module_of(p)
                for prefix in self._strip:
                    pp = f"{prefix}/{p}"
                    if pp in known_paths:
                        return self.module_of(pp)
        # package-level match: any known path under the target directory
        slashed = target.replace(".", "/").lstrip("./")
        if slashed:
            for p in known_paths:
                if p.startswith(slashed + "/") or any(p.startswith(f"{pre}/{slashed}/") for pre in self._strip):
                    return self.module_of(p)
        return None
