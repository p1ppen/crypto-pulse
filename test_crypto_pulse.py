"""Tests for crypto_pulse.

Uses pytest + pytest-asyncio. aiohttp is mocked via unittest.mock style fake
session objects so no real network calls are made.
"""
from __future__ import annotations

import math
import statistics
from unittest.mock import MagicMock

import aiohttp
import pytest

import crypto_pulse as cp


# --------------------------------------------------------------------------- #
# Fake aiohttp session / response
# --------------------------------------------------------------------------- #
class MockResponse:
    def __init__(self, data, status: int = 200):
        self._data = data
        self.status = status

    async def __aenter__(self) -> "MockResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def json(self):
        return self._data

    async def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(MagicMock(), MagicMock(), status=self.status)


class FakeSession:
    """Mimics aiohttp.ClientSession.get for testing."""

    def __init__(self, data_by_url=None, exc: Exception | None = None):
        self._data = data_by_url or {}
        self._exc = exc
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        for key, val in self._data.items():
            if url.startswith(key):
                return MockResponse(val)
        # No canned data for this URL -> treat as unexpected.
        raise aiohttp.ClientError(f"unexpected url: {url}")


# --------------------------------------------------------------------------- #
# Sample data
# --------------------------------------------------------------------------- #
BINANCE_FUNDING_JSON = [
    {"symbol": "BTCUSDT", "lastFundingRate": "0.00001000"},
    {"symbol": "ETHUSDT", "lastFundingRate": "-0.00002500"},
]
BYBIT_FUNDING_JSON = {
    "retCode": 0,
    "result": {
        "list": [
            {"symbol": "XRPUSDT", "fundingRate": "0.00005000"},
        ]
    },
}
# Gate.io returns a bare JSON list; field names differ ("name", "funding_rate").
GATE_FUNDING_JSON = [
    {"name": "BTC_USDT", "funding_rate": 0.000019},
    {"name": "ETH_USDT", "funding_rate": -0.000045},
    {"name": "BAD_RATE", "funding_rate": "not-a-number"},  # bad rate skipped
    {"name": "MISSING_RATE", "funding_rate": None},  # missing rate skipped
    {"funding_rate": 0.0001},  # missing name skipped
    "not-a-dict",  # non-dict skipped
]
# Bitget wraps in {"code","data"}; rate is a string.
BITGET_FUNDING_JSON = {
    "code": "00000",
    "data": [
        {"symbol": "BTCUSDT", "fundingRate": "0.000045"},  # string rate
        {"symbol": "ETHUSDT", "fundingRate": "-0.000020"},  # string, negative
    ],
}
# MEXC wraps in {"success","code","data"}; rate is a float.
MEXC_FUNDING_JSON = {
    "success": True,
    "code": 0,
    "data": [
        {"symbol": "BTC_USDT", "fundingRate": 0.000030},  # float rate
        {"symbol": "ETH_USDT", "fundingRate": -0.000050},  # float, negative
    ],
}
# Kraken: only tickers with tag == "perpetual" are kept; rate is a float.
KRAKEN_FUNDING_JSON = {
    "tickers": [
        {"symbol": "PF_RENDERUSD", "tag": "perpetual", "relativeFundingRate": -0.0001, "fundingRate": -1.1958993633166e-05},
        {"symbol": "PF_BTCUSD", "tag": "perpetual", "relativeFundingRate": 0.00005, "fundingRate": 31.8},
        {"symbol": "PF_EXPIRY", "tag": "expiry", "relativeFundingRate": 0.0001},  # not perpetual -> filtered
        {"symbol": "PF_NORATE", "tag": "perpetual"},  # missing rate -> skipped
    ],
}

GLOBAL_JSON = {
    "data": {
        "market_cap_percentage": {"btc": 42.53, "eth": 18.1},
        "total_market_cap": {"usd": 2_840_000_000_000},
    }
}


def make_klines(closes: list[float]) -> list[list]:
    """Build synthetic OHLCV klines with a given close series."""
    return [
        [i, c - 0.5, c + 1.0, c - 1.0, c, 100.0, i]
        for i, c in enumerate(closes)
    ]


CLOSES_30 = [100.0 * (1.001 ** i) for i in range(30)]
KLINES_30 = make_klines(CLOSES_30)


# --------------------------------------------------------------------------- #
# 1) Funding annualization math
# --------------------------------------------------------------------------- #
def test_annualized_rate_formula():
    assert cp.annualized_rate(0.0) == 0.0
    assert cp.annualized_rate(0.00001) == pytest.approx(0.00001 * 3 * 365 * 100)  # 1.095


def test_parse_binance_funding():
    parsed = cp.parse_binance_funding(BINANCE_FUNDING_JSON)
    assert len(parsed) == 2
    btc = next(p for p in parsed if p.symbol == "BTCUSDT")
    assert btc.rate == 0.00001
    assert btc.venue == "Binance"


def test_parse_binance_funding_dict_response():
    # Some endpoints return a single dict; parser should wrap it.
    parsed = cp.parse_binance_funding({"symbol": "SOLUSDT", "lastFundingRate": "0.00000100"})
    assert len(parsed) == 1
    assert parsed[0].symbol == "SOLUSDT"


def test_parse_bybit_funding():
    parsed = cp.parse_bybit_funding(BYBIT_FUNDING_JSON)
    assert len(parsed) == 1
    assert parsed[0].symbol == "XRPUSDT"
    assert parsed[0].venue == "Bybit"
    assert parsed[0].rate == 0.00005


def test_parse_gate_funding():
    parsed = cp.parse_gate_funding(GATE_FUNDING_JSON)
    assert len(parsed) == 2
    btc = next(p for p in parsed if p.symbol == "BTC_USDT")
    assert btc.venue == "Gate"
    assert btc.rate == 0.000019
    eth = next(p for p in parsed if p.symbol == "ETH_USDT")
    assert eth.rate == -0.000045  # negative funding preserved


def test_parse_gate_funding_rejects_bad_input():
    assert cp.parse_gate_funding({"not": "a list"}) == []
    assert cp.parse_gate_funding(None) == []


def test_parse_bitget_funding():
    parsed = cp.parse_bitget_funding(BITGET_FUNDING_JSON)
    assert len(parsed) == 2
    btc = next(p for p in parsed if p.symbol == "BTCUSDT")
    assert btc.venue == "Bitget"
    assert btc.rate == 0.000045  # string rate coerced to float
    eth = next(p for p in parsed if p.symbol == "ETHUSDT")
    assert eth.rate == -0.000020  # negative string rate preserved


def test_parse_bitget_funding_rejects_bad_input():
    assert cp.parse_bitget_funding({"code": "00000", "data": "nope"}) == []
    assert cp.parse_bitget_funding(None) == []


def test_parse_mexc_funding():
    parsed = cp.parse_mexc_funding(MEXC_FUNDING_JSON)
    assert len(parsed) == 2
    btc = next(p for p in parsed if p.symbol == "BTC_USDT")
    assert btc.venue == "MEXC"
    assert btc.rate == 0.000030  # float rate kept as float
    eth = next(p for p in parsed if p.symbol == "ETH_USDT")
    assert eth.rate == -0.000050  # negative float preserved


def test_parse_mexc_funding_rejects_bad_input():
    assert cp.parse_mexc_funding({"success": True, "code": 0, "data": {}}) == []
    assert cp.parse_mexc_funding(None) == []


def test_parse_kraken_funding_filters_non_perpetual():
    parsed = cp.parse_kraken_funding(KRAKEN_FUNDING_JSON)
    assert len(parsed) == 2
    symbols = {p.symbol for p in parsed}
    assert symbols == {"PF_RENDERUSD", "PF_BTCUSD"}
    assert all(p.venue == "Kraken" for p in parsed)
    render = next(p for p in parsed if p.symbol == "PF_RENDERUSD")
    assert render.rate == pytest.approx(-0.0001 * 8)  # relative/hr x8 -> 8h normalized
    btc = next(p for p in parsed if p.symbol == "PF_BTCUSD")
    assert btc.rate == pytest.approx(0.00005 * 8)


def test_parse_kraken_funding_rejects_bad_input():
    assert cp.parse_kraken_funding({"tickers": "nope"}) == []
    assert cp.parse_kraken_funding(None) == []


@pytest.mark.asyncio
async def test_funding_command_annualization():
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
    }
    out = await cp.run_funding(FakeSession(urls))
    assert "UNAVAILABLE" not in out
    # Ranked by abs rate: XRP(5e-5), ETH(2.5e-5), BTC(1e-5)
    assert "XRPUSDT" in out
    assert "5.47%" in out          # 0.00005 * 1095 * 100 = 5.475, float repr 5.4749... -> "5.47%"
    assert "2.74%" in out          # 0.000025 * 1095 * 100 = 2.7375 -> "2.74%"
    assert "1.10%" in out          # 0.00001  * 1095 * 100 = 1.095 -> "1.10%"
    assert "-0.0025%" in out       # ETH rate%: -0.000025 * 100 = -0.0025


@pytest.mark.asyncio
async def test_funding_command_all_sources_down():
    out = await cp.run_funding(FakeSession(exc=aiohttp.ClientError("down")))
    assert out == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_funding_command_all_six_venues():
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
        cp.GATE_API_URL: GATE_FUNDING_JSON,
        cp.BITGET_API_URL: BITGET_FUNDING_JSON,
        cp.MEXC_API_URL: MEXC_FUNDING_JSON,
        cp.KRAKEN_API_URL: KRAKEN_FUNDING_JSON,
    }
    out = await cp.run_funding(FakeSession(urls))
    assert "UNAVAILABLE" not in out
    venues = {"Binance", "Bybit", "Gate", "Bitget", "MEXC", "Kraken"}
    for v in venues:
        assert v in out
    assert "BTCUSDT" in out and "BTC_USDT" in out and "PF_RENDERUSD" in out


@pytest.mark.asyncio
async def test_funding_command_one_source_down_rest_render():
    # Gate URL not canned -> FakeSession raises -> Gate omitted; others render.
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
    }
    out = await cp.run_funding(FakeSession(urls))
    assert "UNAVAILABLE" not in out
    assert "Binance" in out and "Bybit" in out
    assert "Gate" not in out and "Kraken" not in out


@pytest.mark.asyncio
async def test_funding_command_venue_filter_case_insensitive():
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
        cp.GATE_API_URL: GATE_FUNDING_JSON,
        cp.BITGET_API_URL: BITGET_FUNDING_JSON,
        cp.MEXC_API_URL: MEXC_FUNDING_JSON,
        cp.KRAKEN_API_URL: KRAKEN_FUNDING_JSON,
    }
    out = await cp.run_funding(FakeSession(urls), venue="GATE")
    assert "UNAVAILABLE" not in out
    assert "Gate" in out
    assert "Binance" not in out and "Bybit" not in out


@pytest.mark.asyncio
async def test_funding_command_venue_no_match():
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
    }
    out = await cp.run_funding(FakeSession(urls), venue="kraken")
    assert out == "UNAVAILABLE"


def test_render_funding_table_long_symbols_align():
    long_symbol = "SUPER_LONG_PERP_TOKEN"
    rates = [
        cp.FundingRate(symbol=long_symbol, venue="Gate", rate=0.0005),
        cp.FundingRate(symbol="PF_RENDERUSD", venue="Kraken", rate=-0.0003),
    ]
    table = cp.render_funding_table(rates, top_n=15)
    lines = table.splitlines()
    bar = lines[0]
    # The bar is "─"*w0 + "  " + "─"*w1 + ...; the first segment's length is the
    # SYMBOL column width, which must fit the longest symbol.
    sym_width = len(bar.split("  ")[0])
    assert sym_width >= len(long_symbol)
    # Every data row must place the column-0 / column-1 separator ("  ") at the
    # exact same offset => columns are aligned despite differing symbol lengths.
    for row in lines[3:]:
        assert row[sym_width : sym_width + 2] == "  "
        assert row[: len(long_symbol)]  # non-empty symbol field


# --------------------------------------------------------------------------- #
# 2) Vol computation from fixed klines
# --------------------------------------------------------------------------- #
def test_compute_vol_stats_momentum():
    closes = [100.0, 101.0, 99.0, 102.0, 103.0, 100.0, 98.0, 105.0]
    stats = cp.compute_vol_stats(make_klines(closes))
    assert stats is not None
    # 7d: (105 - 100) / 100
    assert stats.momentum_7d == pytest.approx(5.0)
    # 30d: (105 - 100) / 100
    assert stats.momentum_30d == pytest.approx(5.0)
    assert stats.current == 105.0
    # high window uses k[2] = c+1.0 -> max close + 1.0; low = c-1.0
    assert stats.high == pytest.approx(105.0 + 1.0)
    assert stats.low == pytest.approx(98.0 - 1.0)


def test_compute_vol_stats_volatility():
    closes = [100.0 * (1.001 ** i) for i in range(30)]
    stats = cp.compute_vol_stats(make_klines(closes))
    assert stats is not None
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    expected_vol = statistics.pstdev(log_returns) * math.sqrt(365) * 100
    assert stats.realized_vol_30d == pytest.approx(expected_vol)
    expected_m30 = (closes[-1] - closes[0]) / closes[0] * 100
    assert stats.momentum_30d == pytest.approx(expected_m30)
    expected_m7 = (closes[-1] - closes[-8]) / closes[-8] * 100
    assert stats.momentum_7d == pytest.approx(expected_m7)


@pytest.mark.asyncio
async def test_vol_command_with_mock_klines():
    urls = {cp.BINANCE_KLINES_URL: KLINES_30}
    out = await cp.run_vol(FakeSession(urls))
    assert "Realized Vol:" in out
    closes = CLOSES_30
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    expected_vol = statistics.pstdev(log_returns) * math.sqrt(365) * 100
    assert f"{expected_vol:.2f}%" in out


@pytest.mark.asyncio
async def test_vol_command_unavailable():
    out = await cp.run_vol(FakeSession(exc=aiohttp.ClientError("down")))
    assert out == "UNAVAILABLE"


def test_compute_vol_stats_bad_input():
    assert cp.compute_vol_stats([]) is None
    assert cp.compute_vol_stats("nope") is None
    assert cp.compute_vol_stats([["x"]]) is None  # malformed rows skipped -> <2 closes


# --------------------------------------------------------------------------- #
# Global stats parsing
# --------------------------------------------------------------------------- #
def test_parse_global():
    g = cp.parse_global(GLOBAL_JSON)
    assert g is not None
    assert g.btc_dominance == 42.53
    assert g.total_market_cap == 2_840_000_000_000.0
    assert cp.fmt_money(g.total_market_cap) == "$2.84T"


# --------------------------------------------------------------------------- #
# 3) Report renders with all sources down (all UNAVAILABLE)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_report_all_unavailable():
    out = await cp.run_report(FakeSession(exc=aiohttp.ClientError("down")))
    assert "UNAVAILABLE" in out
    # One UNAVAILABLE per section: funding, vol, global.
    assert out.count("UNAVAILABLE") == 3


@pytest.mark.asyncio
async def test_report_full_render():
    urls = {
        cp.BINANCE_FAPI_URL: BINANCE_FUNDING_JSON,
        cp.BYBIT_V5_URL: BYBIT_FUNDING_JSON,
        cp.BINANCE_KLINES_URL: KLINES_30,
        cp.COINGECKO_GLOBAL_URL: GLOBAL_JSON,
    }
    out = await cp.run_report(FakeSession(urls))
    assert "crypto-pulse :: terminal market report" in out
    assert "generated:" in out
    assert "UNAVAILABLE" not in out
    assert "BTC Dominance" in out
    assert "XRPUSDT" in out


# --------------------------------------------------------------------------- #
# Table rendering sanity
# --------------------------------------------------------------------------- #
def test_render_table_aligns_and_bars():
    table = cp.render_table(
        ["A", "B"],
        [["1", "2"]],
        aligns=["left", "right"],
    )
    assert "A" in table and "B" in table
    assert "─" in table


def test_render_funding_table_empty():
    out = cp.render_funding_table(
        [cp.FundingRate(symbol="X", venue="Bybit", rate=0.0)], top_n=5
    )
    assert "X" in out


# --------------------------------------------------------------------------- #
# 4) Open interest scanner
# --------------------------------------------------------------------------- #
# Sample OKI payloads (shapes verified against the live endpoints).
BINANCE_OI_JSON = {"symbol": "BTCUSDT", "openInterest": "9543210.00", "time": 1700000000}
BINANCE_OI_ETH = {"symbol": "ETHUSDT", "openInterest": "5123456.78", "time": 1700000001}
BYBIT_OI_JSON = {
    "retCode": 0,
    "result": {"list": [
        {"openInterest": "19000000.5", "timestamp": "1700000000"},
        {"openInterest": "19100000.9", "timestamp": "17000003600"},
    ]},
    "time": "17000003600",
}
OKX_OI_JSON = {"code": "0", "data": [{"oi": "26345678", "instId": "BTC-USDT-SWAP", "ts": "1700000000"}]}
OKX_OI_ETH = {"code": "0", "data": [{"oi": "8800000", "instId": "ETH-USDT-SWAP"}]}


# --- OKX symbol mapping --------------------------------------------------- #
def test_okx_inst_id_mapping():
    assert cp.okx_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert cp.okx_inst_id("ETHUSDT") == "ETH-USDT-SWAP"
    assert cp.okx_inst_id("SOLUSDT") == "SOL-USDT-SWAP"
    assert cp.okx_inst_id("XRPUSDT") == "XRP-USDT-SWAP"
    assert cp.okx_inst_id("DOGEUSDT") == "DOGE-USDT-SWAP"
    # Non-USDT quote still splits correctly.
    assert cp.okx_inst_id("BTCBUSD") == "BTC-BUSD-SWAP"
    # Unknown quote falls back to a -SWAP id rather than mangling the base.
    assert cp.okx_inst_id("BTCTX") == "BTCTX-SWAP"


def test_okx_oi_url_uses_mapped_instid():
    url = cp.okx_oi_url("BTCUSDT")
    assert "instId=BTC-USDT-SWAP" in url
    assert url.startswith(cp.OKX_OI_BASE)


# --- Parsing each venue's response shape ---------------------------------- #
def test_parse_binance_oi():
    oi = cp.parse_binance_oi(BINANCE_OI_JSON, "BTCUSDT")
    assert oi is not None
    assert oi.symbol == "BTCUSDT"
    assert oi.venue == "Binance"
    assert oi.oi == 9543210.0


def test_parse_binance_oi_rejects_bad_input():
    assert cp.parse_binance_oi("nope", "BTCUSDT") is None
    assert cp.parse_binance_oi({"no_interest": 1}, "BTCUSDT") is None  # missing field
    assert cp.parse_binance_oi({"openInterest": "not-a-number"}, "BTCUSDT") is None


def test_parse_bybit_oi_takes_latest_bucket():
    oi = cp.parse_bybit_oi(BYBIT_OI_JSON, "BTCUSDT")
    assert oi is not None
    assert oi.venue == "Bybit"
    # limit=2 -> two buckets; latest entry is 19100000.9.
    assert oi.oi == 19100000.9


def test_parse_bybit_oi_rejects_bad_input():
    assert cp.parse_bybit_oi({}, "BTCUSDT") is None
    assert cp.parse_bybit_oi({"result": {"list": []}}, "BTCUSDT") is None
    assert cp.parse_bybit_oi({"result": {"list": [{"no_interest": 1}]}}, "BTCUSDT") is None


def test_parse_okx_oi_matches_instid():
    oi = cp.parse_okx_oi(OKX_OI_JSON, "BTCUSDT")
    assert oi is not None
    assert oi.venue == "OKX"
    assert oi.oi == 26345678.0


def test_parse_okx_oi_rejects_instid_mismatch():
    # Payload returned for a different instId -> treated as unavailable.
    data = {"code": "0", "data": [{"oi": "1", "instId": "ETH-USDT-SWAP"}]}
    assert cp.parse_okx_oi(data, "BTCUSDT") is None


def test_parse_okx_oi_rejects_bad_input():
    assert cp.parse_okx_oi({}, "BTCUSDT") is None
    assert cp.parse_okx_oi({"data": [{"instId": "BTC-USDT-SWAP"}]}, "BTCUSDT") is None  # missing oi


# --- Table rendering ------------------------------------------------------- #
def test_render_oi_table_all_venues_present():
    oi_map = {
        "BTCUSDT": {
            "Binance": cp.OpenInterest("BTCUSDT", "Binance", 1_000_000.0),
            "Bybit": cp.OpenInterest("BTCUSDT", "Bybit", 3_000_000.0),
            "OKX": cp.OpenInterest("BTCUSDT", "OKX", 2_000_000.0),
        }
    }
    out = cp.render_oi_table(oi_map)
    assert "SYMBOL" in out and "BINANCE_OI" in out
    assert "BYBIT_OI" in out and "OKX_OI" in out and "TOTAL" in out
    assert "1.00M" in out and "3.00M" in out and "2.00M" in out
    assert "6.00M" in out  # total
    assert "UNAVAILABLE" not in out


def test_render_oi_table_sorted_by_total_desc():
    oi_map = {
        "ETHUSDT": {
            "Binance": cp.OpenInterest("ETHUSDT", "Binance", 100_000.0),
            "Bybit": cp.OpenInterest("ETHUSDT", "Bybit", 100_000.0),
            "OKX": cp.OpenInterest("ETHUSDT", "OKX", 100_000.0),
        },
        "BTCUSDT": {
            "Binance": cp.OpenInterest("BTCUSDT", "Binance", 5_000_000.0),
            "Bybit": cp.OpenInterest("BTCUSDT", "Bybit", 5_000_000.0),
            "OKX": cp.OpenInterest("BTCUSDT", "OKX", 5_000_000.0),
        },
    }
    out = cp.render_oi_table(oi_map)
    # BTCUSDT (15M total) should appear above ETHUSDT (300K total).
    assert out.index("BTCUSDT") < out.index("ETHUSDT")


def test_render_oi_table_one_venue_down():
    oi_map = {
        "BTCUSDT": {
            "Binance": cp.OpenInterest("BTCUSDT", "Binance", 1_000_000.0),
            "Bybit": cp.OpenInterest("BTCUSDT", "Bybit", 2_000_000.0),
            "OKX": None,
        }
    }
    out = cp.render_oi_table(oi_map)
    assert "UNAVAILABLE" in out  # OKX cell
    assert "1.00M" in out and "2.00M" in out
    assert "3.00M" in out  # total of the two available venues


def test_render_oi_table_all_venues_down():
    oi_map = {"DOGEUSDT": {"Binance": None, "Bybit": None, "OKX": None}}
    out = cp.render_oi_table(oi_map)
    # All cells -> UNAVAILABLE, including the TOTAL.
    assert out.count("UNAVAILABLE") == 4


# --- End-to-end (mocked, no network) --------------------------------------- #
@pytest.mark.asyncio
async def test_oi_command_full_render():
    urls = {
        cp.binance_oi_url("BTCUSDT"): BINANCE_OI_JSON,
        cp.bybit_oi_url("BTCUSDT"): BYBIT_OI_JSON,
        cp.okx_oi_url("BTCUSDT"): OKX_OI_JSON,
    }
    out = await cp.run_oi(FakeSession(urls), symbols=["BTCUSDT"])
    assert "open interest scanner" in out
    assert "BTCUSDT" in out
    # Binance 9,543,210 ~= 9.54M, Bybit latest 19,100,000.9 ~= 19.10M, OKX 26,345,678 ~= 26.35M
    assert "9.54M" in out
    assert "19.10M" in out
    assert "26.35M" in out
    # Total ~= 54,988,888.9 -> 54.99M
    assert "54.99M" in out
    # Snapshot note present.
    assert "point-in-time snapshot" in out
    assert "UNAVAILABLE" not in out


@pytest.mark.asyncio
async def test_oi_command_one_venue_down():
    # OKX URL not canned -> FakeSession raises -> OKX cell UNAVAILABLE; rest render.
    urls = {
        cp.binance_oi_url("BTCUSDT"): BINANCE_OI_JSON,
        cp.bybit_oi_url("BTCUSDT"): BYBIT_OI_JSON,
    }
    out = await cp.run_oi(FakeSession(urls), symbols=["BTCUSDT"])
    assert "UNAVAILABLE" in out            # OKX cell
    assert "9.54M" in out and "19.10M" in out


@pytest.mark.asyncio
async def test_oi_command_all_down_renders_table():
    out = await cp.run_oi(FakeSession(exc=aiohttp.ClientError("down")), symbols=["BTCUSDT"])
    # Per-cell availability: every cell UNAVAILABLE, total UNAVAILABLE.
    assert out.count("UNAVAILABLE") == 4
    assert "open interest scanner" in out


@pytest.mark.asyncio
async def test_oi_command_symbols_flag_overrides_default(caplog):
    # Provide ETH only; ensure only ETH columns are queried (no BTC requests).
    urls = {
        cp.binance_oi_url("ETHUSDT"): BINANCE_OI_ETH,
        cp.bybit_oi_url("ETHUSDT"): {"retCode": 0, "result": {"list": [{"openInterest": "8000000", "timestamp": "1"}]}},
        cp.okx_oi_url("ETHUSDT"): OKX_OI_ETH,
    }
    sess = FakeSession(urls)
    out = await cp.run_oi(sess, symbols=["ETHUSDT"])
    assert "ETHUSDT" in out
    # No BTCUSDT should have been requested.
    assert not any("BTCUSDT" in u for u in sess.calls)
    # --symbols comma parsing
    assert cp.parse_symbols("btcusdt, ethusdt") == ["BTCUSDT", "ETHUSDT"]
    assert cp.parse_symbols("") is None
    assert cp.parse_symbols("   ") is None


@pytest.mark.asyncio
async def test_oi_command_default_symbols_used_when_none():
    sess = FakeSession(exc=aiohttp.ClientError("down"))
    await cp.run_oi(sess)  # no symbols -> defaults
    # 5 default symbols * 3 venues = 15 URL calls attempted.
    assert len(sess.calls) == 3 * len(cp.DEFAULT_OI_SYMBOLS)


# --------------------------------------------------------------------------- #
# 5) CLI wiring
# --------------------------------------------------------------------------- #
def test_cli_oi_subcommand_and_symbols():
    ns = cp.build_parser().parse_args(["oi"])
    assert ns.command == "oi"
    assert ns.symbols is None
    ns2 = cp.build_parser().parse_args(["oi", "--symbols", "BTCUSDT,ETHUSDT"])
    assert ns2.symbols == "BTCUSDT,ETHUSDT"
