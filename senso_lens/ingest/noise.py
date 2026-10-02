"""Real/bogus filter for commits.

A commit is noise for *evolution* purposes when it does not reflect a human
(or agent) decision about the code's structure: bot commits, dependency bumps,
formatting runs, generated files, bulk changes. Noise commits are stored (so
nothing is lost) but excluded from churn, co-change and phase.
"""
from __future__ import annotations

import re

from ..config import BOT_AUTHOR_PATTERNS, NOISE_SUBJECT_PATTERNS, Config
from .git_reader import CommitEvent

_BOTS = [re.compile(p, re.I) for p in BOT_AUTHOR_PATTERNS]
_SUBJ = [re.compile(p, re.I) for p in NOISE_SUBJECT_PATTERNS]


def classify_noise(ev: CommitEvent, cfg: Config, source_files: int) -> str | None:
    """Return a reason string if the commit is noise, else None."""
    who = f"{ev.author} <{ev.email}>"
    if any(rx.search(who) for rx in _BOTS):
        return "bot_author"
    if any(rx.search(ev.subject or "") for rx in _SUBJ):
        return "maintenance_subject"
    if len(ev.files) > cfg.max_files_per_commit:
        return "bulk_changeset"
    if source_files == 0:
        return "no_source_files"
    return None
