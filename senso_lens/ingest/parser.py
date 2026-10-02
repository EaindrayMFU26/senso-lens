"""The shared parser: each changed file is parsed once per content (git blob hash).

Produces, per file: size (NLOC), function count, cyclomatic complexity (avg, max),
a maintainability score, class / abstract-class counts, and import targets.
Every signal family that needs file content reads from this one result — that
is the "one parse" half of the shared index.

Complexity and size come from lizard (multi-language, one engine for every
language so numbers are comparable across a polyglot repository). The
maintainability score is a documented, Halstead-free Maintainability Index
(`mi_lite`, see docs/DESIGN.md); radon's full MI was dropped from the pipeline
because its Halstead pass costs ~170 ms on a 2,000-line file and bottoms out at 0
for any large file, which made the composite useless exactly where it matters.
radon remains a *reference implementation* in the test-suite (tests/test_reference.py).
Predictive validity of the composite is out of scope (see README).
"""
from __future__ import annotations

import ast
import math
import re
from pathlib import PurePosixPath
from typing import Any

import lizard

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".go": "go",
    ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala", ".m": "objc", ".mm": "objc",
    ".dart": "dart", ".lua": "lua", ".pl": "perl", ".sh": "shell", ".sql": "sql",
}

_JS_IMPORT = re.compile(r"""(?:import\s+(?:[^'"]*?\s+from\s+)?|import\s*\(\s*|require\s*\(\s*|export\s+[^'"]*?\s+from\s+)['"]([^'"]+)['"]""")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;", re.M)
_GO_IMPORT = re.compile(r'"([\w./\-]+)"')
_GO_BLOCK = re.compile(r"import\s*\((.*?)\)", re.S)
_GO_SINGLE = re.compile(r'^\s*import\s+"([\w./\-]+)"', re.M)
_RUST_USE = re.compile(r"^\s*use\s+crate::([\w:]+)", re.M)
_CLASS = re.compile(r"\b(?:class|struct|record|enum)\s+\w+")
_ABSTRACT = re.compile(r"\b(?:interface|abstract\s+class|protocol|trait)\s+\w+")


def language_of(path: str) -> str | None:
    return LANG_BY_EXT.get(PurePosixPath(path).suffix.lower())


def mi_lite(nloc: int, ccn_total: int, functions: int) -> float:
    """Halstead-free maintainability proxy on a 0-100 scale.

    Follows the shape of the SEI/Oman MI (size and complexity terms) without the
    Halstead volume term, which lizard does not provide:
        171 - 0.23*CC - 16.2*ln(LOC)   normalized by 100/171 and clamped.
    `CC` is the summed cyclomatic complexity, `LOC` the non-blank/non-comment lines.
    """
    loc = max(1, nloc)
    cc = max(1, ccn_total)
    raw = 171.0 - 0.23 * cc - 16.2 * math.log(loc)
    return max(0.0, min(100.0, raw * 100.0 / 171.0))


def _python_structure(code: str, path: str) -> tuple[int, int, list[str]]:
    """(classes, abstract_classes, import_targets) for Python source."""
    try:
        tree = ast.parse(code)
    except Exception:
        return 0, 0, []
    classes = abstract = 0
    targets: list[str] = []
    pkg_dir = PurePosixPath(path).parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes += 1
            base_names = []
            for b in node.bases:
                base_names.append(getattr(b, "id", None) or getattr(b, "attr", None) or "")
            kw_meta = any(k.arg == "metaclass" for k in node.keywords)
            has_abstract_method = any(
                isinstance(d, ast.expr) and (getattr(d, "id", None) == "abstractmethod" or getattr(d, "attr", None) == "abstractmethod")
                for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) for d in f.decorator_list)
            if "ABC" in base_names or "Protocol" in base_names or kw_meta or has_abstract_method:
                abstract += 1
        elif isinstance(node, ast.Import):
            for a in node.names:
                targets.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                base = pkg_dir
                for _ in range(node.level - 1):
                    base = base.parent
                mod = (node.module or "").replace(".", "/")
                targets.append(str(base / mod) if mod else str(base))
            elif node.module:
                targets.append(node.module)
    return classes, abstract, targets


def _generic_structure(code: str, lang: str) -> tuple[int, int, list[str]]:
    classes = len(_CLASS.findall(code))
    abstract = len(_ABSTRACT.findall(code))
    targets: list[str] = []
    if lang in ("javascript", "typescript"):
        targets = [t for t in _JS_IMPORT.findall(code)]
    elif lang in ("java", "kotlin", "scala"):
        targets = _JAVA_IMPORT.findall(code)
    elif lang == "go":
        for block in _GO_BLOCK.findall(code):
            targets.extend(_GO_IMPORT.findall(block))
        targets.extend(_GO_SINGLE.findall(code))
    elif lang == "rust":
        targets = [t.replace("::", "/") for t in _RUST_USE.findall(code)]
    return classes, abstract, targets


def parse_source(path: str, data: bytes) -> dict[str, Any] | None:
    """Parse one file's content. Returns None for non-source or undecodable files."""
    lang = language_of(path)
    if lang is None:
        return None
    try:
        code = data.decode("utf-8")
    except UnicodeDecodeError:
        code = data.decode("latin-1", errors="replace")
    if len(code) > 2_000_000:  # generated monsters are not worth a minute of CPU
        return {"language": lang, "nloc": code.count("\n"), "functions": 0, "ccn_avg": 0.0, "ccn_max": 0, "mi": None,
                "classes": 0, "abstract_classes": 0, "imports": [], "skipped": "too_large"}
    try:
        info = lizard.analyze_file.analyze_source_code(path, code)
        nloc = int(info.nloc)
        funcs = list(info.function_list)
        ccn_list = [int(f.cyclomatic_complexity) for f in funcs]
        ccn_total = sum(ccn_list)
        ccn_avg = (ccn_total / len(ccn_list)) if ccn_list else 0.0
        ccn_max = max(ccn_list) if ccn_list else 0
    except Exception:
        nloc = sum(1 for ln in code.splitlines() if ln.strip())
        funcs, ccn_total, ccn_avg, ccn_max = [], 0, 0.0, 0

    if lang == "python":
        classes, abstract, targets = _python_structure(code, path)
    else:
        classes, abstract, targets = _generic_structure(code, lang)
    mi = mi_lite(nloc, ccn_total, len(funcs))
    mi_kind = "mi_lite"

    return {
        "language": lang, "nloc": nloc, "functions": len(funcs), "ccn_avg": round(ccn_avg, 3), "ccn_max": ccn_max,
        "ccn_total": ccn_total, "mi": round(mi, 2), "mi_kind": mi_kind, "classes": classes,
        "abstract_classes": abstract, "imports": sorted(set(targets)),
    }
