"""Corrections for grid search. The bar rises with the number of things tried."""

import math
import random

import pytest

from btcbot.multiple_testing import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    family_p_value,
    probabilistic_sharpe_ratio,
    sample_moments,
    sharpe_of,
    sidak_alpha,
    sidak_critical_t,
)


def test_one_test_leaves_the_bar_alone():
    """With N=1 the correction must be a no-op, or it is not a correction."""
    assert sidak_alpha(0.05, 1) == pytest.approx(0.05)
    assert sidak_critical_t(0.05, 1) == pytest.approx(1.96, abs=0.01)


def test_the_bar_rises_with_the_number_of_tests():
    bars = [sidak_critical_t(0.05, n) for n in (1, 5, 20, 100)]
    assert bars == sorted(bars)
    # The headline number: the repo's default sweep is 5 thresholds x 4
    # directions, and judging that grid at |t| > 2 is off by more than a point.
    assert sidak_critical_t(0.05, 20) == pytest.approx(3.02, abs=0.02)


def test_sidak_is_never_harsher_than_bonferroni():
    for n in (2, 10, 50):
        assert sidak_alpha(0.05, n) >= 0.05 / n


def test_family_p_value_inflates_a_marginal_single_test_result():
    """t=2.4 reads as p=0.016 alone and as nothing at all across a grid."""
    single = family_p_value(2.4, n_tests=1)
    assert single == pytest.approx(0.016, abs=0.002)
    swept = family_p_value(2.4, n_tests=20)
    assert swept > 0.25
    assert swept > single


def test_family_p_value_is_monotone_in_both_arguments():
    assert family_p_value(3.0, 20) > family_p_value(4.0, 20)
    assert family_p_value(3.0, 40) > family_p_value(3.0, 20)


def test_critical_t_actually_holds_the_family_error_rate():
    """Simulate the null directly: 5% of GRIDS should produce a false positive."""
    rng = random.Random(7)
    n_tests, trials = 20, 4000
    bar = sidak_critical_t(0.05, n_tests)
    naive_hits = corrected_hits = 0
    for _ in range(trials):
        ts = [rng.gauss(0.0, 1.0) for _ in range(n_tests)]
        best = max(ts, key=abs)
        if abs(best) > 2.0:
            naive_hits += 1
        if abs(best) > bar:
            corrected_hits += 1
    # The naive bar is catastrophically wrong on a grid this size...
    assert naive_hits / trials > 0.4
    # ...and the corrected one lands near the 5% it advertises.
    assert 0.03 < corrected_hits / trials < 0.08


def test_expected_max_sharpe_grows_with_trials_and_scatter():
    assert expected_max_sharpe(0.01, 1) == 0.0
    assert expected_max_sharpe(0.01, 50) > expected_max_sharpe(0.01, 5)
    assert expected_max_sharpe(0.04, 20) > expected_max_sharpe(0.01, 20)
    # No scatter across the grid means nothing was gained by searching it.
    assert expected_max_sharpe(0.0, 100) == 0.0


def test_psr_falls_when_returns_are_negatively_skewed():
    """Binary payoffs are skewed, and the plain t-statistic ignores that."""
    normal = probabilistic_sharpe_ratio(0.15, n_obs=200, skew=0.0, kurtosis=3.0)
    skewed = probabilistic_sharpe_ratio(0.15, n_obs=200, skew=-1.5, kurtosis=8.0)
    assert normal > skewed


def test_psr_needs_two_observations():
    assert probabilistic_sharpe_ratio(0.5, n_obs=1) is None


def test_deflating_a_result_never_flatters_it():
    """Same numbers, more trials -> strictly less confidence."""
    kwargs = dict(sharpe=0.18, n_obs=300, sharpe_variance=0.01, skew=-0.4, kurtosis=5.0)
    one = deflated_sharpe_ratio(n_trials=1, **kwargs)
    many = deflated_sharpe_ratio(n_trials=40, **kwargs)
    assert one > many


def test_a_searched_no_edge_grid_does_not_clear_the_deflated_bar():
    """The control experiment, in statistic form.

    Twenty strategies with genuinely zero edge. The best one always looks
    positive -- that is what "best of twenty" means. The deflated Sharpe has to
    refuse it anyway.
    """
    rng = random.Random(11)
    n_obs = 250
    grids = [[rng.gauss(0.0, 1.0) for _ in range(n_obs)] for _ in range(20)]
    sharpes = [sharpe_of(g) for g in grids]
    best_idx = max(range(len(grids)), key=lambda i: sharpes[i])
    best = grids[best_idx]

    assert sharpes[best_idx] > 0  # it does look like a winner
    mean_sh = sum(sharpes) / len(sharpes)
    var_sh = sum((s - mean_sh) ** 2 for s in sharpes) / (len(sharpes) - 1)
    _, _, skew, kurt = sample_moments(best)

    dsr = deflated_sharpe_ratio(
        sharpe=sharpes[best_idx],
        n_obs=n_obs,
        n_trials=len(grids),
        sharpe_variance=var_sh,
        skew=skew,
        kurtosis=kurt,
    )
    assert dsr is not None
    assert dsr < 0.95


def test_sharpe_and_t_stat_agree_up_to_sqrt_n():
    """t = sharpe * sqrt(n) ties this module to the report's existing statistic."""
    rets = [0.02, -0.01, 0.03, 0.00, -0.02, 0.05, 0.01, -0.03]
    sr = sharpe_of(rets)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    t_stat = mean / math.sqrt(var / len(rets))
    assert sr * math.sqrt(len(rets)) == pytest.approx(t_stat)


def test_moments_refuse_degenerate_input():
    assert sample_moments([]) is None
    assert sample_moments([0.5]) is None
    assert sample_moments([0.5, 0.5, 0.5]) is None  # zero variance
    assert sharpe_of([0.5, 0.5]) is None


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError):
        sidak_alpha(0.0, 5)
    with pytest.raises(ValueError):
        sidak_alpha(1.0, 5)
    with pytest.raises(ValueError):
        sidak_alpha(0.05, 0)
    with pytest.raises(ValueError):
        family_p_value(2.0, 0)
    with pytest.raises(ValueError):
        expected_max_sharpe(0.01, 0)
