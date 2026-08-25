"""stats.py — small stdlib-only statistics helpers for report metrics.

No numpy/scipy on this Pi (see requirements.txt) — everything here is
plain ``math`` and ``statistics``. Kept intentionally minimal: enough for
confidence intervals and a chi-square goodness-of-fit test on small
per-node count tables, not a general stats library.
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional, Sequence, Tuple


def median(values: Sequence[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return statistics.median(values)


def iqr(values: Sequence[float]) -> Optional[Tuple[float, float]]:
    """(Q1, Q3) using the exclusive method (statistics.quantiles default n=4)."""
    values = sorted(v for v in values if v is not None)
    if len(values) < 2:
        return None
    q1, _, q3 = statistics.quantiles(values, n=4)
    return (q1, q3)


def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> Tuple[float, float, float]:
    """
    Wilson score interval for a binomial proportion.

    Returns (p_hat, lo, hi). ``z`` defaults to the two-sided 95% z-score.
    Well-behaved at n == 0 (returns (0.0, 0.0, 1.0) — maximally uninformative
    rather than raising) and at k == 0 or k == n (does not clamp to a
    degenerate 0-width interval the way a naive normal approximation would).
    """
    if n <= 0:
        return 0.0, 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lo = max(0.0, (centre - margin) / denom)
    hi = min(1.0, (centre + margin) / denom)
    return p, lo, hi


def binomial_ci(k: int, n: int) -> Tuple[float, float, float]:
    """Alias of wilson_ci — Wilson is the recommended interval for small n."""
    return wilson_ci(k, n)


def _lower_incomplete_gamma_reg(a: float, x: float) -> float:
    """
    Regularized lower incomplete gamma P(a, x), via series (x < a+1) or a
    continued fraction for the complement (x >= a+1) — the standard
    Numerical-Recipes split, using math.lgamma so it stays numerically
    stable for the small integer/half-integer `a` chi-square needs.
    """
    if x < 0 or a <= 0:
        return 0.0
    if x == 0:
        return 0.0
    if x < a + 1.0:
        # Series expansion for P(a, x).
        ap = a
        summ = 1.0 / a
        term = summ
        for _ in range(500):
            ap += 1.0
            term *= x / ap
            summ += term
            if abs(term) < abs(summ) * 1e-14:
                break
        return summ * math.exp(-x + a * math.log(x) - math.lgamma(a))
    # Continued fraction for Q(a, x) = 1 - P(a, x).
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return 1.0 - q


def chi2_sf(x: float, df: int) -> float:
    """
    Survival function (upper tail p-value) of the chi-square distribution:
    P(X > x) for X ~ chi2(df). Used for goodness-of-fit tests against a
    configured per-node delivery distribution.
    """
    if df <= 0:
        return 1.0
    if x <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - _lower_incomplete_gamma_reg(df / 2.0, x / 2.0)))


def chi_square_gof(observed: Sequence[float], expected: Sequence[float]) -> Tuple[float, int, float]:
    """
    Pearson's chi-square goodness-of-fit statistic against expected counts.

    Returns (chi2, df, p_value). Cells with expected == 0 are skipped (and
    should be — a zero-probability cell is a modeling error, not evidence).
    """
    chi2 = 0.0
    df = 0
    for o, e in zip(observed, expected):
        if e <= 0:
            continue
        chi2 += (o - e) ** 2 / e
        df += 1
    df = max(0, df - 1)
    return chi2, df, chi2_sf(chi2, df) if df > 0 else 1.0


def rolling_mean(values: Sequence[Optional[float]], window: int) -> List[Optional[float]]:
    """
    Centred rolling mean over `values`, edge-truncated (the window shrinks
    near the boundaries rather than padding with None). None entries in
    `values` are excluded from the window they fall in.
    """
    n = len(values)
    if n == 0 or window <= 0:
        return []
    half = window // 2
    out: List[Optional[float]] = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        window_vals = [v for v in values[lo:hi] if v is not None]
        out.append(sum(window_vals) / len(window_vals) if window_vals else None)
    return out


def safe_ratio(numer: float, denom: float) -> Optional[float]:
    """numer / denom, or None if denom is zero — never ZeroDivisionError."""
    if not denom:
        return None
    return numer / denom
