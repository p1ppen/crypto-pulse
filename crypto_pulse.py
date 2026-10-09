"""crypto-pulse: a tiny, key-free crypto market terminal report.

Aggregates public exchange data (Binance, Bybit, CoinGecko) and renders a
terminal market report. No API keys required.

Dependencies: stdlib + aiohttp only.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import math
import statistics
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

# --------------------------------------------------------------------------- #
# Public endpoints (all free, no keys)
# --------------------------------------------------------------------------- #
BINANCE_FAPI_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_V5_URL = "https://api.bybit.com/v5/market/tickers?category=linear"
GATE_API_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
BITGET_API_URL = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
MEXC_API_URL = "https://contract.mexc.com/api/v1/contract/funding_rate"
KRAKEN_API_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
BINANCE_KLINES_URL = (
    "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30"
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)

# Binance funds 3x/day, Bybit 3x/day -> 3 * 365 funding events per year.
FUNDING_PERIODS_PER_YEAR = 3 * 365


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FundingRate:
    symbol: str
    venue: str
    rate: float  # raw per-period rate, e.g. 0.00001000


@dataclass(frozen=True)
class VolStats:
    realized_vol_30d: float  # annualized, in percent
    momentum_7d: float       # percent
    momentum_30d: float      # percent
    high: float              # highest price in the window
    low: float               # lowest price in the window
    current: float           # latest close


@dataclass(frozen=True)
class GlobalStats:
    total_market_cap: float
    btc_dominance: float


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
) -> Any:
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.json()


@asynccontextmanager
async def _session(
    session: Optional[aiohttp.ClientSession],
):
    if session is not None:
        yield session
    else:
        s = aiohttp.ClientSession()
        try:
            yield s
        finally:
            await s.close()


# --------------------------------------------------------------------------- #
# Funding-rate parsing (pure)
# --------------------------------------------------------------------------- #
def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_binance_funding(data: Any) -> list[FundingRate]:
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out: list[FundingRate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        rate = _safe_float(item.get("lastFundingRate"))
        if symbol and rate is not None:
            out.append(FundingRate(symbol=symbol, venue="Binance", rate=rate))
    return out


def parse_bybit_funding(data: Any) -> list[FundingRate]:
    out: list[FundingRate] = []
    lst = ((data or {}).get("result", {}) or {}).get("list", [])
    if not isinstance(lst, list):
        return out
    for item in lst:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        rate = _safe_float(item.get("fundingRate"))
        if symbol and rate is not None:
            out.append(FundingRate(symbol=symbol, venue="Bybit", rate=rate))
    return out


def parse_gate_funding(data: Any) -> list[FundingRate]:
    out: list[FundingRate] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = item.get("name")
        rate = _safe_float(item.get("funding_rate"))
        if symbol and rate is not None:
            out.append(FundingRate(symbol=symbol, venue="Gate", rate=rate))
    return out


def parse_bitget_funding(data: Any) -> list[FundingRate]:
    out: list[FundingRate] = []
    payload = (data or {}).get("data", [])
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        rate = _safe_float(item.get("fundingRate"))
        if symbol and rate is not None:
            out.append(FundingRate(symbol=symbol, venue="Bitget", rate=rate))
    return out


def parse_mexc_funding(data: Any) -> list[FundingRate]:
    out: list[FundingRate] = []
    payload = (data or {}).get("data", [])
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        rate = _safe_float(item.get("fundingRate"))
        if symbol and rate is not None:
            out.append(FundingRate(symbol=symbol, venue="MEXC", rate=rate))
    return out


def parse_kraken_funding(data: Any) -> list[FundingRate]:
    out: list[FundingRate] = []
    tickers = (data or {}).get("tickers", [])
    if not isinstance(tickers, list):
        return out
    for item in tickers:
        if not isinstance(item, dict):
            continue
        if item.get("tag") != "perpetual":
            continue
        symbol = item.get("symbol")
        # Kraken's "fundingRate" is an ABSOLUTE dollar amount per contract per hour.
        # "relativeFundingRate" is the fractional rate per hour; multiply by 8 to
        # normalize to the 8h funding interval used by all other venues.
        rel = _safe_float(item.get("relativeFundingRate"))
        if symbol and rel is not None:
            out.append(FundingRate(symbol=symbol, venue="Kraken", rate=rel * 8))
    return out


async def fetch_binance_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, BINANCE_FAPI_URL, timeout)
    except Exception:
        return None
    try:
        return parse_binance_funding(data)
    except Exception:
        return None


async def fetch_bybit_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, BYBIT_V5_URL, timeout)
    except Exception:
        return None
    try:
        return parse_bybit_funding(data)
    except Exception:
        return None


async def fetch_gate_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, GATE_API_URL, timeout)
    except Exception:
        return None
    try:
        return parse_gate_funding(data)
    except Exception:
        return None


async def fetch_bitget_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, BITGET_API_URL, timeout)
    except Exception:
        return None
    try:
        return parse_bitget_funding(data)
    except Exception:
        return None


async def fetch_mexc_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, MEXC_API_URL, timeout)
    except Exception:
        return None
    try:
        return parse_mexc_funding(data)
    except Exception:
        return None


async def fetch_kraken_funding(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    try:
        data = await fetch_json(session, KRAKEN_API_URL, timeout)
    except Exception:
        return None
    try:
        return parse_kraken_funding(data)
    except Exception:
        return None


async def gather_funding_rates(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[list[FundingRate]]:
    results = await asyncio.gather(
        fetch_binance_funding(session, timeout),
        fetch_bybit_funding(session, timeout),
        fetch_gate_funding(session, timeout),
        fetch_bitget_funding(session, timeout),
        fetch_mexc_funding(session, timeout),
        fetch_kraken_funding(session, timeout),
    )
    if all(r is None for r in results):
        return None
    combined: list[FundingRate] = []
    for r in results:
        if r is not None:
            combined.extend(r)
    return combined


def annualized_rate(rate: float) -> float:
    return rate * FUNDING_PERIODS_PER_YEAR * 100


# --------------------------------------------------------------------------- #
# Volatility / momentum (pure)
# --------------------------------------------------------------------------- #
def compute_vol_stats(klines: Any) -> Optional[VolStats]:
    if not isinstance(klines, list) or not klines:
        return None
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for k in klines:
        if not isinstance(k, (list, tuple)) or len(k) < 5:
            continue
        try:
            closes.append(float(k[4]))
            highs.append(float(k[2]))
            lows.append(float(k[3]))
        except (TypeError, ValueError, IndexError):
            continue
    if len(closes) < 2:
        return None

    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    std = statistics.pstdev(log_returns) if len(log_returns) > 1 else 0.0
    vol = std * math.sqrt(365) * 100

    current = closes[-1]
    mom_7d = (current - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 else 0.0
    mom_30d = (current - closes[0]) / closes[0] * 100

    return VolStats(
        realized_vol_30d=vol,
        momentum_7d=mom_7d,
        momentum_30d=mom_30d,
        high=max(highs) if highs else 0.0,
        low=min(lows) if lows else 0.0,
        current=current,
    )


async def fetch_vol_stats(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[VolStats]:
    try:
        klines = await fetch_json(session, BINANCE_KLINES_URL, timeout)
    except Exception:
        return None
    try:
        return compute_vol_stats(klines)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Global market stats (pure)
# --------------------------------------------------------------------------- #
def parse_global(data: Any) -> Optional[GlobalStats]:
    try:
        d = (data or {}).get("data", {})
        btc_dom = float(d.get("market_cap_percentage", {}).get("btc", 0))
        mcap = d.get("total_market_cap", {})
        total = float(mcap.get("usd", 0)) if isinstance(mcap, dict) else float(mcap or 0)
    except (TypeError, ValueError):
        return None
    return GlobalStats(total_market_cap=total, btc_dominance=btc_dom)


async def fetch_global_stats(
    session: aiohttp.ClientSession, timeout: aiohttp.ClientTimeout
) -> Optional[GlobalStats]:
    try:
        data = await fetch_json(session, COINGECKO_GLOBAL_URL, timeout)
    except Exception:
        return None
    return parse_global(data)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_money(n: float) -> str:
    if abs(n) >= 1e12:
        return f"${n / 1e12:.2f}T"
    if abs(n) >= 1e9:
        return f"${n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"${n / 1e6:.2f}M"
    if abs(n) >= 1e3:
        return f"${n / 1e3:.2f}K"
    return f"${n:.2f}"


def render_table(
    headers: list[str], rows: list[list[str]], aligns: Optional[list[str]] = None
) -> str:
    aligns = aligns or ["left"] * len(headers)
    col = "  "
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def line(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            s = str(cell)
            parts.append(s.rjust(widths[i]) if aligns[i] == "right" else s.ljust(widths[i]))
        return col.join(parts)

    bar = col.join("─" * w for w in widths)
    out = [bar, line(headers), bar]
    for row in rows:
        out.append(line(row))
    return "\n".join(out)


def render_funding_table(rates: list[FundingRate], top_n: int = 15) -> str:
    ranked = sorted(rates, key=lambda r: abs(r.rate), reverse=True)[:top_n]
    headers = ["SYMBOL", "VENUE", "RATE%", "ANNUALIZED%"]
    rows: list[list[str]] = []
    for r in ranked:
        rows.append(
            [
                r.symbol,
                r.venue,
                f"{r.rate * 100:.4f}%",
                f"{annualized_rate(r.rate):.2f}%",
            ]
        )
    return render_table(headers, rows, aligns=["left", "left", "right", "right"])


def format_vol(v: VolStats) -> str:
    return (
        f"  30d Realized Vol:  {v.realized_vol_30d:.2f}%\n"
        f"  7d Momentum:       {v.momentum_7d:+.2f}%\n"
        f"  30d Momentum:      {v.momentum_30d:+.2f}%\n"
        f"  High (window):     ${v.high:,.2f}\n"
        f"  Low (window):      ${v.low:,.2f}\n"
        f"  Current:           ${v.current:,.2f}"
    )


def format_global(g: GlobalStats) -> str:
    return (
        f"  Total Market Cap:  {fmt_money(g.total_market_cap)}\n"
        f"  BTC Dominance:     {g.btc_dominance:.2f}%"
    )


def render_report(
    funding_rates: Optional[list[FundingRate]],
    vol_stats: Optional[VolStats],
    global_stats: Optional[GlobalStats],
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(" crypto-pulse :: terminal market report")
    lines.append("=" * 62)
    lines.append(f" generated: {now:%Y-%m-%d %H:%M:%S UTC}")
    lines.append("")

    lines.append("Funding Rates (Top 10 by |rate|)")
    lines.append("-" * 62)
    if not funding_rates:
        lines.append("  UNAVAILABLE")
    else:
        lines.append(render_funding_table(funding_rates, 10))
    lines.append("")

    lines.append("BTC Volatility & Momentum (30 x 1d klines)")
    lines.append("-" * 62)
    if vol_stats is None:
        lines.append("  UNAVAILABLE")
    else:
        lines.append(format_vol(vol_stats))
    lines.append("")

    lines.append("Global Market Stats")
    lines.append("-" * 62)
    if global_stats is None:
        lines.append("  UNAVAILABLE")
    else:
        lines.append(format_global(global_stats))
    lines.append("")
    lines.append("=" * 62)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def run_funding(
    session: Optional[aiohttp.ClientSession] = None,
    venue: Optional[str] = None,
) -> str:
    async with _session(session) as s:
        rates = await gather_funding_rates(s, REQUEST_TIMEOUT)
    if rates is None:
        return "UNAVAILABLE"
    if venue is not None:
        want = venue.strip().lower()
        rates = [r for r in rates if r.venue.lower() == want]
    if not rates:
        return "UNAVAILABLE"
    return render_funding_table(rates, 15)


async def run_vol(session: Optional[aiohttp.ClientSession] = None) -> str:
    async with _session(session) as s:
        vol = await fetch_vol_stats(s, REQUEST_TIMEOUT)
    if vol is None:
        return "UNAVAILABLE"
    return format_vol(vol)


async def run_report(session: Optional[aiohttp.ClientSession] = None) -> str:
    async with _session(session) as s:
        rates, vol, glob = await asyncio.gather(
            gather_funding_rates(s, REQUEST_TIMEOUT),
            fetch_vol_stats(s, REQUEST_TIMEOUT),
            fetch_global_stats(s, REQUEST_TIMEOUT),
        )
    return render_report(rates, vol, glob)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-pulse",
        description="Terminal crypto market report (no API keys required).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    funding_p = sub.add_parser(
        "funding",
        help="Top 15 most extreme funding rates across Binance, Bybit, Gate, "
        "Bitget, MEXC, Kraken.",
    )
    funding_p.add_argument(
        "--venue",
        metavar="VENUE",
        default=None,
        help="Filter rows by venue label (case-insensitive), e.g. --venue bitget.",
    )
    sub.add_parser("vol", help="BTC 30d realized volatility, momentum & price range.")
    sub.add_parser("report", help="Full terminal market report.")
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "funding":
        out = asyncio.run(run_funding(venue=args.venue))
    elif args.command == "vol":
        out = asyncio.run(run_vol())
    elif args.command == "report":
        out = asyncio.run(run_report())
    else:  # pragma: no cover - argparse guards this
        raise SystemExit("unknown command")
    print(out)


if __name__ == "__main__":
    main()
