# crypto-pulse

A tiny, **key-free** terminal market report for crypto. It pulls public data from
exchange APIs (no API keys, no auth) and renders a readable report right in your
shell.

Aggregates funding rates from six venues — Binance Futures, Bybit, Gate.io,
Bitget, MEXC, and Kraken — plus Binance spot OHLCV and CoinGecko global stats.

Built with the Python standard library + `aiohttp` only — typed, async, and
defensive: if any data source is slow or down, it simply prints `UNAVAILABLE`
and the rest of the report still renders.

## Requirements

- Python 3.10+
- `aiohttp`

## Install

```bash
pip install aiohttp
# optional, for tests only:
pip install pytest pytest-asyncio
```

## Usage

```bash
python crypto_pulse.py funding                       # Top 15 most extreme funding rates
python crypto_pulse.py funding --venue bitget        # Top 15 for Bitget only (case-insensitive)
python crypto_pulse.py vol                           # BTC 30d realized vol, momentum, price range
python crypto_pulse.py oi                            # Open interest scanner (Binance / Bybit / OKX)
python crypto_pulse.py oi --symbols BTCUSDT,ETHUSDT  # Override the default symbol list
python crypto_pulse.py report                        # Full terminal market report
```

### Commands

#### `funding`
Aggregates perpetual-swap funding rates from Binance Futures, Bybit, Gate.io,
Bitget, MEXC, and Kraken, ranks them by absolute value, and prints the top 15:

| SYMBOL | VENUE | RATE% | ANNUALIZED% |
|--------|-------|-------|-------------|

`ANNUALIZED% = rate * 3 * 365 * 100` (funding is paid 3× daily). Use
`--venue VENUE` (case-insensitive) to filter rows by venue label, e.g.
`--venue kraken`. If every source is unreachable the command prints `UNAVAILABLE`;
otherwise available venues still render.

#### `vol`
Fetches the last 30 daily candles for BTCUSDT from Binance spot and computes:

- **30d realized volatility** — annualized standard deviation of daily log
  returns (`std * sqrt(365) * 100`).
- **7d / 30d momentum** — percentage price change over the window.
- **Price range / current** — high/low and latest close observed in the window.

#### `report`
Combines everything into one terminal report: funding table (top 10), the BTC
volatility section, and global market stats (total market cap, BTC dominance),
timestamped with a generated-at header.

## Data sources

All free, public, no keys required:

| Source | Endpoint |
|--------|----------|
| Binance Futures | `https://fapi.binance.com/fapi/v1/premiumIndex` (funding rates) |
| Binance Spot | `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=30` (BTC candles) |
| Bybit v5 | `https://api.bybit.com/v5/market/tickers?category=linear` (funding rates) |
| Gate.io | `https://api.gateio.ws/api/v4/futures/usdt/contracts` (funding rates) |
| Bitget | `https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES` (funding rates) |
| MEXC | `https://contract.mexc.com/api/v1/contract/funding_rate` (funding rates) |
| Kraken | `https://futures.kraken.com/derivatives/api/v3/tickers` (funding rates, perpetuals only) |
| CoinGecko | `https://api.coingecko.com/api/v3/global` (global market cap, BTC dominance) |

Each request uses an 8-second timeout and is wrapped in its own `try/except`,
so a single failing source never breaks the whole report.

## Design notes

- `crypto_pulse.py` is the single source of truth; pure parsing/computation
  functions are separated from async fetching so they're trivial to test.
- `stdlib` + `aiohttp` only for the tool itself; tests additionally use
  `pytest` + `pytest-asyncio` and mock `aiohttp` via fake session objects
  (no network calls in tests).

## Run tests

```bash
python -m pytest -q
```

## Disclaimer

- This tool does **not** provide financial advice.
- It reads *public* market data only; nothing is stored or transmitted by this
  project.
- Data freshness/delay depends on the upstream exchanges; treat it as a quick
  pulse, not a trading signal.
- Endpoints are third-party and may change or rate-limit without notice.
