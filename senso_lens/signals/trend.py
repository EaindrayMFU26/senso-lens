"""Mann-Kendall trend test and Sen's slope, with tie correction.

Non-parametric, so it does not assume normality of churn counts, and it is the
same test the parent SENSO program uses as the independent reference for phase
labels — which is what makes convergent validity checkable.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass


@dataclass
class MKResult:
    trend: str          # "increasing" | "decreasing" | "no_trend"
    p: float
    z: float
    s: int
    slope: float        # Sen's slope (units per period)
    n: int

    @property
    def direction(self) -> str:
        return {"increasing": "rising", "decreasing": "falling"}.get(self.trend, "flat")


def mann_kendall(x: list[float], alpha: float = 0.10) -> MKResult:
    n = len(x)
    if n < 3:
        return MKResult("no_trend", 1.0, 0.0, 0, 0.0, n)
    s = 0
    slopes: list[float] = []
    for i in range(n - 1):
        xi = x[i]
        for j in range(i + 1, n):
            d = x[j] - xi
            s += (d > 0) - (d < 0)
            slopes.append(d / (j - i))
    ties = Counter(x)
    tie_term = sum(t * (t - 1) * (2 * t + 5) for t in ties.values() if t > 1)
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0
    if var_s <= 0:
        return MKResult("no_trend", 1.0, 0.0, s, 0.0, n)
    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    p = math.erfc(abs(z) / math.sqrt(2.0))  # two-sided normal tail, == 2*(1-Phi(|z|))
    slopes.sort()
    m = len(slopes)
    slope = slopes[m // 2] if m % 2 else 0.5 * (slopes[m // 2 - 1] + slopes[m // 2])
    if p <= alpha and z > 0:
        trend = "increasing"
    elif p <= alpha and z < 0:
        trend = "decreasing"
    else:
        trend = "no_trend"
    return MKResult(trend, float(p), float(z), int(s), float(slope), n)
