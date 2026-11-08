"""Statistical testing utilities for variant experimentation.

Provides frequentist Z-tests for proportions with multiple comparison
corrections and minimum sample size calculations.
"""

import math
from typing import Dict, List, Optional, Tuple


def z_test_proportions(
    successes_a: int, trials_a: int,
    successes_b: int, trials_b: int,
) -> Tuple[float, float]:
    """Two-proportion Z-test (two-tailed).

    Returns (z_statistic, p_value).
    """
    if trials_a < 1 or trials_b < 1:
        return 0.0, 1.0

    p_a = successes_a / trials_a
    p_b = successes_b / trials_b

    # Pooled proportion
    p_pool = (successes_a + successes_b) / (trials_a + trials_b)

    # Standard error
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / trials_a + 1 / trials_b))
    if se < 1e-12:
        return 0.0, 1.0

    z = (p_b - p_a) / se

    # Two-tailed p-value using normal approximation
    p_value = 2 * (1 - _norm_cdf(abs(z)))
    return z, p_value


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using error function approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def min_sample_size(
    baseline_rate: float,
    mde: float = 0.02,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Minimum sample size per variant for a two-proportion Z-test.

    Args:
        baseline_rate: Expected proportion under control.
        mde: Minimum detectable effect (absolute difference).
        alpha: Significance level (two-tailed).
        power: Statistical power (1 - beta).

    Returns:
        Required sample size per arm.
    """
    if mde <= 0 or baseline_rate <= 0 or baseline_rate >= 1:
        return 200  # fallback

    p1 = baseline_rate
    p2 = baseline_rate + mde

    z_alpha = _norm_ppf(1 - alpha / 2)
    z_beta = _norm_ppf(power)

    p_bar = (p1 + p2) / 2
    n = ((z_alpha * math.sqrt(2 * p_bar * (1 - p_bar)) +
          z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) / mde) ** 2

    return max(int(math.ceil(n)), 50)


def _norm_ppf(p: float) -> float:
    """Approximate inverse normal CDF (percent-point function).

    Uses rational approximation from Abramowitz and Stegun.
    """
    if p <= 0:
        return -6.0
    if p >= 1:
        return 6.0

    if p < 0.5:
        return -_rational_approx(math.sqrt(-2 * math.log(p)))
    else:
        return _rational_approx(math.sqrt(-2 * math.log(1 - p)))


def _rational_approx(t: float) -> float:
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)


def find_best_variant(
    variant_stats: List[Dict],
    kpi: str,
    min_samples: int = 100,
    alpha: float = 0.05,
) -> Optional[Dict]:
    """Find the statistically significant best variant using pairwise Z-tests.

    Args:
        variant_stats: List of dicts with keys:
            variant, total_dials, total_connected, total_engaged
        kpi: 'pickup_rate' or 'engagement_rate'
        min_samples: Minimum observations per variant.
        alpha: Significance level (Bonferroni-corrected for multiple comparisons).

    Returns:
        Dict with winner info or None if no significant winner.
    """
    # Filter to variants with enough samples
    eligible = []
    for vs in variant_stats:
        if kpi == "pickup_rate":
            n = vs["total_dials"]
            k = vs["total_connected"]
        else:  # engagement_rate
            n = vs["total_connected"]
            k = vs["total_engaged"]

        if n >= min_samples:
            rate = k / n if n > 0 else 0
            eligible.append({**vs, "n": n, "k": k, "rate": rate})

    if len(eligible) < 2:
        return None

    # Sort by rate descending
    eligible.sort(key=lambda x: x["rate"], reverse=True)

    # Bonferroni correction for number of pairwise comparisons
    num_comparisons = len(eligible) * (len(eligible) - 1) // 2
    adjusted_alpha = alpha / max(num_comparisons, 1)

    best = eligible[0]
    second = eligible[1]

    z, p_value = z_test_proportions(second["k"], second["n"], best["k"], best["n"])

    if p_value < adjusted_alpha and z > 0:
        return {
            "winner": best["variant"],
            "winner_rate": best["rate"],
            "winner_n": best["n"],
            "runner_up": second["variant"],
            "runner_up_rate": second["rate"],
            "runner_up_n": second["n"],
            "z_statistic": z,
            "p_value": p_value,
            "adjusted_alpha": adjusted_alpha,
            "significant": True,
        }

    return None


def detect_degradation(
    current_rate: float,
    baseline_rate: float,
    current_n: int,
    baseline_n: int,
    threshold: float = 0.02,
    min_samples: int = 100,
) -> Optional[Dict]:
    """Detect if current rate has degraded significantly from baseline.

    Returns degradation info or None if not significant.
    """
    if current_n < min_samples or baseline_n < min_samples:
        return None

    delta = current_rate - baseline_rate
    if delta >= -threshold:
        return None

    current_k = int(current_rate * current_n)
    baseline_k = int(baseline_rate * baseline_n)
    z, p_value = z_test_proportions(current_k, current_n, baseline_k, baseline_n)

    if p_value < 0.05 and z > 0:  # baseline is better
        return {
            "current_rate": current_rate,
            "baseline_rate": baseline_rate,
            "delta": delta,
            "z_statistic": z,
            "p_value": p_value,
            "significant": True,
        }

    return None


def check_guardrail(
    current_rate: float,
    baseline_rate: float,
    max_drop: float,
    current_n: int,
    min_samples: int = 100,
) -> bool:
    """Check if the guardrail KPI is within acceptable bounds.

    Returns True if OK, False if guardrail violated.
    """
    if current_n < min_samples:
        return True  # not enough data to judge

    return current_rate >= baseline_rate - max_drop


def compute_total_variation_distance(
    old_weights: Dict[str, float],
    new_weights: Dict[str, float],
) -> float:
    """Compute total-variation distance between two allocation distributions.

    TV distance = 0.5 * Σ|old_norm_i - new_norm_i| over all keys in union.
    """
    all_keys = set(old_weights.keys()) | set(new_weights.keys())

    old_total = sum(old_weights.values())
    new_total = sum(new_weights.values())

    if old_total <= 0 or new_total <= 0:
        return 1.0

    tv = 0.0
    for key in all_keys:
        old_p = old_weights.get(key, 0) / old_total
        new_p = new_weights.get(key, 0) / new_total
        tv += abs(old_p - new_p)

    return tv / 2.0


def compute_impact_score(
    current_rate: float,
    projected_rate: float,
    affected_leads: int,
    total_leads: int,
) -> float:
    """Compute an honest impact score in [0, 1].

    Based on the magnitude of expected improvement and the fraction
    of traffic affected.
    """
    if total_leads <= 0:
        return 0.0

    rate_improvement = max(0, projected_rate - current_rate)
    coverage = affected_leads / total_leads

    # Score is geometric mean of improvement significance and coverage
    # Capped at 1.0
    raw = math.sqrt(min(rate_improvement / 0.05, 1.0) * coverage)
    return round(min(raw, 1.0), 3)

