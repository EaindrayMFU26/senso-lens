"""Tunable defaults. Everything here is a documented knob, not a hidden constant.

The values are deliberately conservative defaults; a repository can override
them in `.senso/config.json` (written by `senso init`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".swift", ".scala", ".m", ".mm", ".dart", ".lua", ".pl", ".sh", ".sql",
}

# Paths that never count as source for churn/metrics (vendored, generated, lockfiles).
IGNORE_PATH_PATTERNS = [
    r"(^|/)node_modules/", r"(^|/)vendor/", r"(^|/)third_party/", r"(^|/)dist/",
    r"(^|/)build/", r"(^|/)target/", r"(^|/)\.git/", r"(^|/)__pycache__/",
    r"\.min\.(js|css)$", r"\.lock$", r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$",
    r"(^|/)poetry\.lock$", r"(^|/)Pipfile\.lock$", r"(^|/)go\.sum$", r"(^|/)Cargo\.lock$",
    r"\.(png|jpg|jpeg|gif|svg|ico|pdf|zip|gz|tar|jar|woff2?|ttf|eot|mp4|mp3)$",
    r"(^|/)(\.idea|\.vscode)/", r"\.generated\.", r"_pb2\.py$", r"\.pb\.go$",
    r"(^|/)migrations/\d+_", r"\.snap$",
]

# Modules whose imports do not make another module load-bearing (tests depend on everything).
TEST_MODULE_PATTERNS = [r"(^|/)(tests?|__tests__|spec|specs|testing|e2e|fixtures?)(/|$)", r"(^|/)test_[^/]+$"]

# Commit authors/emails that are bots.
BOT_AUTHOR_PATTERNS = [
    r"\[bot\]$", r"dependabot", r"renovate", r"greenkeeper", r"github-actions",
    r"snyk-bot", r"pre-commit-ci", r"codecov", r"semantic-release", r"imgbot",
]

# Commit subjects that are maintenance noise for evolution purposes.
NOISE_SUBJECT_PATTERNS = [
    r"^(chore|build|ci)\(deps(-dev)?\)", r"^bump ", r"\bbump(ed|ing)? (version|deps|dependencies)\b",
    r"^(chore: )?update dependenc", r"^\[?auto\]?[- ]?format", r"^(style|chore): (format|lint|prettier|black)",
    r"^apply (black|prettier|isort|clang-format)", r"^(re)?generated? ", r"^merge (branch|pull request)",
    r"^release v?\d", r"^version bump", r"^\[skip ci\]",
]


@dataclass
class Config:
    # Time axis
    scale: str = "monthly"            # weekly | monthly | quarterly | yearly (set by time-gap rule at init)
    module_depth: int = 2             # path components that define a module (src/ and lib/ are stripped first)
    strip_prefixes: list[str] = field(default_factory=lambda: ["src", "lib", "app", "pkg", "packages"])

    # Noise and sanity limits
    max_changeset_modules: int = 30   # commits touching more modules than this are excluded from co-change
    max_files_per_commit: int = 500   # commits touching more files than this are treated as bulk/noise

    # Co-change
    cochange_half_life_periods: float = 4.0   # exponential decay half-life of co-change weight, in periods
    cochange_min_weight: float = 1.0          # partners below this decayed weight are not reported
    cochange_top_k: int = 5

    # Phase rules
    phase_window: int = 8             # closed periods examined by the trend test
    phase_min_periods: int = 4        # fewer closed periods since first activity -> no_data
    phase_low_churn: float = 1.0      # mean commits/period at or below this is "low"
    phase_k_stable: int = 4           # consecutive low periods needed for stabilizing
    phase_alpha: float = 0.10         # Mann-Kendall significance for a trend direction

    # Connectedness
    load_bearing_fan_in: int = 1
    load_bearing_partners: int = 2

    # Gate
    gate_small_addition: int = 10
    gate_large_addition: int = 100
    gate_escalation_count: int = 3    # same rule+module unresolved this many times -> block
    gate_escalation_hours: float = 24.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Config":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)
