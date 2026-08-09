"""Corrections for having tried more than one strategy.

`sweep` runs a grid and prints a t-statistic per cell. Judging the best cell at
|t| > 2 is the single most common way a backtest lies to you: that threshold is
calibrated for ONE pre-registered hypothesis. Across a 20-cell grid, pure noise
clears it about 60% of the time, and the cell that clears it is exactly the one
you will most want to trade.

Two tools here, in increasing order of how much they know about the data:

* `sidak_critical_t` -- the corrected bar. "Given I ran N tests, how large does
  the best t have to be before 5% is still 5%?" Needs nothing but N.
* `deflated_sharpe_ratio` -- Bailey & Lopez de Prado's Deflated Sharpe Ratio.
  Uses the spread of results ACROSS the grid, plus the skew and kurtosis of the
  winning cell's returns. Strictly better here, because binary payoffs are
  violently non-normal and the plain t-statistic assumes they are not.

Both are stdlib-only, in keeping with the rest of the package.

Reference: Bailey & Lopez de Prado, "The Deflated Sharpe Ratio: Correcting for
Selection Bias, Backtest Overfitting and Non-Normality" (2014). Reached via the
"Advances in Financial Machine Learning" entry in awesome-systematic-trading --
see docs/systematic-trading.md.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Optional, Sequence

_NORM = NormalDist()
_EULER_MASCHERONI = 0.5772156649015329


def sidak_alpha(alpha: float, n_tests: int) -> float:
    """Per-test significance level that holds the FAMILY-wide level at `alpha`.

    Sidak rather than Bonferroni: exact when the tests are independent, and
    slightly less punishing than dividing by N.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if n_tests < 1:
        raise ValueError("n_tests must be >= 1")
    return 1.0 - (1.0 - alpha) ** (1.0 / n_tests)


def sidak_critical_t(alpha: float = 0.05, n_tests: int = 1, two_sided: bool = True) -> float:
    """The |t| the BEST of `n_tests` cells must clear to mean anything.

    Normal approximation to Student's t, which is accurate to well under a
    tenth of a point at the trade counts a sweep produces (hundreds). At n < 30
    it is optimistic -- the real bar is a little higher than this returns.

    For a 4-direction x 5-threshold grid it returns ~3.02, not 2.0. That gap is
    the entire reason this module exists.
    """
    per_test = sidak_alpha(alpha, n_tests)
    tail = per_test / 2.0 if two_sided else per_test
    return _NORM.inv_cdf(1.0 - tail)


def family_p_value(t_stat: float, n_tests: int, two_sided: bool = True) -> float:
    """Probability that noise alone produces a |t| this large SOMEWHERE in the grid.

    This is the number to quote for a swept result. A cell at t = 2.4 has a
    single-test p of 0.016, which sounds like a finding; across 20 cells the
    family-wise p is 0.28, which is nothing.
    """
    if n_tests < 1:
        raise ValueError("n_tests must be >= 1")
    single = 1.0 - _NORM.cdf(abs(t_stat))
    if two_sided:
        single *= 2.0
    single = min(max(single, 0.0), 1.0)
    return 1.0 - (1.0 - single) ** n_tests


def expected_max_sharpe(sharpe_variance: float, n_trials: int) -> float:
    """E[max Sharpe] across `n_trials` strategies that ALL have zero true edge.

    The benchmark the winning cell has to beat. Rises with both the number of
    things tried and how widely they scattered, which is the honest way of
    saying that a grid full of wildly differing results is a grid that will
    hand you a big number for free.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if sharpe_variance <= 0:
        return 0.0
    if n_trials == 1:
        return 0.0
    sigma = math.sqrt(sharpe_variance)
    a = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
    b = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sigma * ((1.0 - _EULER_MASCHERONI) * a + _EULER_MASCHERONI * b)


def probabilistic_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    benchmark: float = 0.0,
) -> Optional[float]:
    """P(true Sharpe > `benchmark`), correcting for skew and fat tails.

    `sharpe` and `benchmark` are per-observation (per-TRADE here), not
    annualized. `kurtosis` is raw, not excess -- a normal distribution is 3.0.

    Negative skew and fat tails both make an observed Sharpe less trustworthy,
    and a binary-payout trade book has plenty of both: buying favourites at 74c
    produces many small wins and occasional large losses, which is precisely the
    shape that flatters a t-statistic.
    """
    if n_obs < 2:
        return None
    denom = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe**2
    if denom <= 0:
        # The correction has gone unstable; refuse rather than return a number
        # produced by taking the square root of a negative.
        return None
    z = (sharpe - benchmark) * math.sqrt(n_obs - 1.0) / math.sqrt(denom)
    return _NORM.cdf(z)


def deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    n_trials: int,
    sharpe_variance: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> Optional[float]:
    """P(the winning cell's true Sharpe > 0), given it won a search of `n_trials`.

    Read it as a confidence: > 0.95 is the analogue of clearing |t| > 2 for a
    single pre-chosen hypothesis. Below that, the result is consistent with
    having searched hard enough to find noise.

    `sharpe_variance` should be the sample variance of the Sharpe ratios across
    the grid you actually ran. Grid cells are usually correlated (nested
    thresholds re-trade the same windows), so the EFFECTIVE number of trials is
    below the nominal count -- passing the nominal count therefore makes this
    conservative, which is the direction to err in.
    """
    benchmark = expected_max_sharpe(sharpe_variance, n_trials)
    return probabilistic_sharpe_ratio(
        sharpe=sharpe,
        n_obs=n_obs,
        skew=skew,
        kurtosis=kurtosis,
        benchmark=benchmark,
    )


def sample_moments(values: Sequence[float]) -> Optional[tuple[float, float, float, float]]:
    """(mean, stdev, skew, raw kurtosis) or None when undefined.

    Population skew and kurtosis (the estimators Bailey & Lopez de Prado's
    formulas assume), not the bias-corrected sample versions.
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return None
    sd = math.sqrt(var)
    m3 = sum((v - mean) ** 3 for v in values) / n
    m4 = sum((v - mean) ** 4 for v in values) / n
    pop_sd = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    if pop_sd <= 0:
        return None
    return mean, sd, m3 / pop_sd**3, m4 / pop_sd**4


def sharpe_of(values: Sequence[float]) -> Optional[float]:
    """Per-observation Sharpe: mean / stdev of per-trade returns.

    Related to the report's t-statistic by t = sharpe * sqrt(n), which is why
    the two never disagree about sign and always disagree about scale.
    """
    moments = sample_moments(values)
    if moments is None:
        return None
    mean, sd, _, _ = moments
    return mean / sd
