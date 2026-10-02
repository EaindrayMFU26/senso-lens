"""Connectedness: load-bearing vs isolated — the second axis, never folded into phase.

Phase measures trajectory; connectedness measures importance. A module that is
constantly modified but that nothing depends on is growth by trajectory and
isolated by connectedness, and the two are reported side by side.

Sources: declared fan-in from the import graph (tier 3) and co-change partners
(tier 1). Git cannot see runtime usage, so "isolated" always means isolated as
far as history and imports can tell.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Config
from ..store.db import Store
from .cochange import partners_of


@dataclass
class Connectedness:
    status: str              # load-bearing | isolated
    fan_in: int
    fan_out: int
    partners: int
    imports_available: bool

    def to_dict(self) -> dict:
        return {"status": self.status, "fan_in": self.fan_in, "fan_out": self.fan_out,
                "cochange_partners": self.partners, "imports_available": self.imports_available}


def connectedness_of(store: Store, module: str, cfg: Config, now: datetime | None = None) -> Connectedness:
    fan_in, fan_out = store.fan_in(module), store.fan_out(module)
    partners = partners_of(store, module, cfg, now, top_k=0)
    n_partners = len(partners)
    any_imports = store.conn.execute("SELECT 1 FROM imports LIMIT 1").fetchone() is not None
    load_bearing = fan_in >= cfg.load_bearing_fan_in or n_partners >= cfg.load_bearing_partners
    return Connectedness("load-bearing" if load_bearing else "isolated", fan_in, fan_out, n_partners, any_imports)
