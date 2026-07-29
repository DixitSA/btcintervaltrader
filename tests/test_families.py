"""Multi-family (BTC/ETH/SOL) configuration and routing.

These cover the seam that had no tests at all: config loaded from YAML. The
suite previously built MarketsConfig directly with FamilyConfig objects, so it
never saw that a `families:` block from YAML arrives as plain nested dicts --
which took out market discovery and the spot feeds for every family at once.
"""

from __future__ import annotations

import textwrap

import pytest

from btcbot.config import (
    FamilyConfig,
    MarketsConfig,
    coerce_families,
    load_config,
    strike_bounds_for_prefix,
)
from btcbot.spot import SpotFeedManager


def write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body))
    return path


# -- coercion ----------------------------------------------------------------


def test_yaml_families_become_family_configs(tmp_path):
    """The regression: YAML nested dicts must not reach the dataclass raw.

    Without coercion every `fam.prefix` / `fam.spot_symbol` access downstream
    raises AttributeError, so discovery and spot both die on any real config.
    """
    path = write_config(
        tmp_path,
        """
        markets:
          families:
            btc:
              prefix: KXBTC15M
              spot_symbol: BTCUSDT
            sol:
              prefix: KXSOL15M
              strike_min: 1
              strike_max: 10_000
        """,
    )
    cfg = load_config(path)
    assert all(isinstance(f, FamilyConfig) for f in cfg.markets.families.values())
    # The two properties that crashed before.
    assert cfg.markets.slug_prefixes == ["KXBTC15M", "KXSOL15M"]
    assert cfg.markets.strike_bounds["KXSOL15M"] == (1, 10_000)


def test_shipped_config_loads_and_routes():
    """The real config.yaml, not a synthetic one."""
    cfg = load_config()
    assert cfg.markets.families, "shipped config defines no families"
    for key, fam in cfg.markets.families.items():
        assert isinstance(fam, FamilyConfig)
        assert fam.prefix and fam.spot_symbol and fam.display_name
        assert cfg.markets.family_for(f"{fam.prefix}-26JUL291730-30") == key


def test_coerce_accepts_objects_lists_and_strings():
    assert coerce_families({"btc": FamilyConfig(prefix="KXBTC15M")})["btc"].prefix == "KXBTC15M"
    assert coerce_families(["KXBTC15M", "KXETH15M"])["eth"].prefix == "KXETH15M"
    assert coerce_families("KXSOL15M")["sol"].prefix == "KXSOL15M"
    assert coerce_families({"eth": "KXETH15M"})["eth"].spot_symbol == "ETHUSDT"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        None,
        {"eth": {"spot_symbol": "ETHUSDT"}},          # no prefix
        {"eth": {"prefix": "KXETH15M", "typo": 1}},   # unknown key
        {"eth": 7},                                   # wrong type
    ],
)
def test_bad_families_fail_loudly(bad):
    """A misconfigured family must not silently fall back to BTC."""
    with pytest.raises(ValueError):
        coerce_families(bad)


def test_unknown_family_key_names_the_family(tmp_path):
    path = write_config(
        tmp_path,
        """
        markets:
          families:
            eth:
              prefix: KXETH15M
              strike_mim: 100
        """,
    )
    with pytest.raises(ValueError, match="markets.families.eth"):
        load_config(path)


# -- strike bounds -----------------------------------------------------------


def test_bounds_default_per_asset_not_per_btc():
    """A family declared without bounds must get ITS OWN asset's range."""
    sol = FamilyConfig(prefix="KXSOL15M")
    eth = FamilyConfig(prefix="KXETH15M")
    btc = FamilyConfig(prefix="KXBTC15M")
    assert sol.strike_min == 1 and sol.strike_max == 10_000
    assert eth.strike_min == 100 and eth.strike_max == 100_000
    assert btc.strike_min == 1_000 and btc.strike_max == 10_000_000
    # A real SOL strike must sit inside SOL's range and under BTC's floor.
    assert sol.strike_min <= 72.65 <= sol.strike_max
    assert 72.65 < btc.strike_min


def test_explicit_bounds_win_over_defaults():
    fam = FamilyConfig(prefix="KXSOL15M", strike_min=5, strike_max=500)
    assert (fam.strike_min, fam.strike_max) == (5, 500)


def test_bounds_lookup_is_case_insensitive_and_falls_back():
    assert strike_bounds_for_prefix("kxeth15m") == (100, 100_000)
    assert strike_bounds_for_prefix("KXDOGE15M") == (1_000, 10_000_000)


# -- routing -----------------------------------------------------------------


def test_family_for_routes_each_asset():
    markets = MarketsConfig(
        families={
            "btc": {"prefix": "KXBTC15M"},
            "eth": {"prefix": "KXETH15M"},
            "sol": {"prefix": "KXSOL15M"},
        }
    )
    assert markets.family_for("KXETH15M-26JUL291730-30") == "eth"
    assert markets.family_for("KXSOL15M-26JUL291730-30") == "sol"
    assert markets.family_for("KXBTC15M-26JUL291730-30") == "btc"
    assert markets.family_for("KXDOGE15M-26JUL291730-30") is None


def test_direct_construction_coerces_too():
    """MarketsConfig(families={...dicts...}) must work, not just load_config."""
    markets = MarketsConfig(families={"sol": {"prefix": "KXSOL15M"}})
    assert isinstance(markets.families["sol"], FamilyConfig)
    assert markets.strike_bounds == {"KXSOL15M": (1, 10_000)}


# -- spot feeds --------------------------------------------------------------


def test_spot_manager_is_lazy_and_per_symbol(monkeypatch):
    """One feed per symbol, created on demand, each polling its own symbol."""
    families = coerce_families(
        {"btc": {"prefix": "KXBTC15M"}, "eth": {"prefix": "KXETH15M"}}
    )
    mgr = SpotFeedManager(families, "https://example.invalid")
    assert mgr._feeds == {}, "feeds should not be created until first use"

    asked: list[str] = []

    def fake_price(self):
        asked.append(self.symbol)
        return {"BTCUSDT": 63_000.0, "ETHUSDT": 1_882.0}[self.symbol]

    monkeypatch.setattr("btcbot.spot.SpotFeed.price", fake_price)

    assert mgr.price("btc") == 63_000.0
    assert mgr.price("eth") == 1_882.0
    assert asked == ["BTCUSDT", "ETHUSDT"]
    assert set(mgr._feeds) == {"BTCUSDT", "ETHUSDT"}
    assert mgr.all_prices() == {"btc": 63_000.0, "eth": 1_882.0}
    assert mgr.price("nope") is None


def test_discovery_applies_per_asset_strike_bounds():
    """End-to-end wiring: real SOL/ETH/BTC strikes must survive discovery.

    parse_strike defaults to BTC's range, so if discover_markets stops passing
    per-asset bounds the SOL strike silently becomes None again. Values are the
    real ones observed on Kalshi (BTC $63,413.47 / ETH $1,882.46 / SOL $72.65).
    """
    from btcbot.venues.kalshi import KalshiVenue

    payload = {
        "markets": [
            {
                "ticker": "KXBTC15M-26JUL291730-30",
                "close_time": "2026-07-29T21:30:00Z",
                "floor_strike": 63_413.47,
            },
            {
                "ticker": "KXETH15M-26JUL291730-30",
                "close_time": "2026-07-29T21:30:00Z",
                "floor_strike": 1_882.46,
            },
            {
                "ticker": "KXSOL15M-26JUL291730-30",
                "close_time": "2026-07-29T21:30:00Z",
                "floor_strike": 72.6547,
            },
        ]
    }
    venue = KalshiVenue()
    venue._http = type("H", (), {"request": lambda self, *a, **k: _Resp(payload)})()

    bounds = MarketsConfig(
        families={
            "btc": {"prefix": "KXBTC15M"},
            "eth": {"prefix": "KXETH15M"},
            "sol": {"prefix": "KXSOL15M"},
        }
    ).strike_bounds

    found = venue.discover_markets(
        ["KXBTC15M", "KXETH15M", "KXSOL15M"], strike_bounds=bounds
    )
    strikes = {m.slug.split("-")[0]: m.strike for m in found}
    assert strikes["KXBTC15M"] == pytest.approx(63_413.47)
    assert strikes["KXETH15M"] == pytest.approx(1_882.46)
    assert strikes["KXSOL15M"] == pytest.approx(72.6547), (
        "SOL strike was dropped -- discovery is not passing per-asset bounds"
    )


def test_discovery_falls_back_to_asset_defaults_without_config_bounds():
    """Omitting strike_bounds must still not parse SOL against BTC's range."""
    from btcbot.venues.kalshi import KalshiVenue

    payload = {
        "markets": [
            {
                "ticker": "KXSOL15M-26JUL291730-30",
                "close_time": "2026-07-29T21:30:00Z",
                "floor_strike": 72.6547,
            }
        ]
    }
    venue = KalshiVenue()
    venue._http = type("H", (), {"request": lambda self, *a, **k: _Resp(payload)})()

    found = venue.discover_markets(["KXSOL15M"])
    assert found[0].strike == pytest.approx(72.6547)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_seeding_keeps_each_asset_in_its_own_feed(tmp_path):
    """The regression: multi-family rows share a ts, one per market.

    Seeding every feed from every row fed BTC's ~$63k prices into the ETH and
    SOL feeds, so their realized_vol was really BTC's volatility.
    """
    import json as _json

    from btcbot.server import seed_spot_manager

    rows = []
    for i, ts in enumerate([1000.0, 1002.0, 1004.0]):
        for prefix, spot in (
            ("KXBTC15M", 63_000.0 + i),
            ("KXETH15M", 1_880.0 + i),
            ("KXSOL15M", 72.0 + i),
        ):
            rows.append(
                {"ts": ts, "spot": spot, "market": {"slug": f"{prefix}-26JUL291730-30"}}
            )
    (tmp_path / "snapshots-2026-07-29.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n"
    )

    families = coerce_families(
        {
            "btc": {"prefix": "KXBTC15M"},
            "eth": {"prefix": "KXETH15M"},
            "sol": {"prefix": "KXSOL15M"},
        }
    )
    mgr = SpotFeedManager(families, "https://example.invalid")
    total = seed_spot_manager(mgr, str(tmp_path))

    assert total == 9, "every row should be seeded exactly once"
    got = {sym: [px for _ts, px in feed._history] for sym, feed in mgr._feeds.items()}
    assert got["BTCUSDT"] == [63_000.0, 63_001.0, 63_002.0]
    assert got["ETHUSDT"] == [1_880.0, 1_881.0, 1_882.0], "ETH feed got another asset"
    assert got["SOLUSDT"] == [72.0, 73.0, 74.0], "SOL feed got another asset"


def test_seeding_skips_rows_from_unconfigured_families(tmp_path):
    """A slug matching no family is dropped, not attributed to some other asset."""
    import json as _json

    from btcbot.server import seed_spot_manager

    rows = [
        {"ts": 1.0, "spot": 63_000.0, "market": {"slug": "KXBTC15M-x"}},
        {"ts": 1.0, "spot": 0.51, "market": {"slug": "KXDOGE15M-x"}},
        {"ts": 2.0, "spot": 63_001.0, "market": {"slug": "KXBTC15M-x"}},
    ]
    (tmp_path / "snapshots-2026-07-29.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n"
    )
    mgr = SpotFeedManager(coerce_families({"btc": {"prefix": "KXBTC15M"}}), "https://x.invalid")
    assert seed_spot_manager(mgr, str(tmp_path)) == 2
    assert [px for _ts, px in mgr._feeds["BTCUSDT"]._history] == [63_000.0, 63_001.0]


def test_seeding_tolerates_missing_dir_and_bad_lines(tmp_path):
    from btcbot.server import seed_spot_manager

    families = coerce_families({"btc": {"prefix": "KXBTC15M"}})
    mgr = SpotFeedManager(families, "https://example.invalid")
    assert seed_spot_manager(mgr, str(tmp_path / "nope")) == 0

    (tmp_path / "snapshots-2026-07-29.jsonl").write_text(
        'not json\n\n{"ts": 1.0}\n{"spot": 5.0}\n'
        '{"ts": 2.0, "spot": 63000.0, "market": {"slug": "KXBTC15M-x"}}\n'
    )
    assert seed_spot_manager(mgr, str(tmp_path)) == 1


def test_spot_manager_shares_one_feed_per_symbol():
    """Two families on the same symbol must not open two feeds."""
    families = coerce_families(
        {"a": {"prefix": "KXBTC15M"}, "b": {"prefix": "KXBTC15M", "window_seconds": 300}}
    )
    mgr = SpotFeedManager(families, "https://example.invalid")
    assert mgr._feed("BTCUSDT") is mgr._feed("BTCUSDT")
