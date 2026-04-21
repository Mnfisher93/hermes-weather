# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project: Hermes Weather Arbitrage Agent

Hermes is an autonomous Polymarket weather-arbitrage bot. It ingests professional meteorological forecast data (Open-Meteo, NOAA/NWS, ECMWF, GFS ensemble), compares model probabilities against Polymarket's implied odds, and places limit orders where edge exceeds a configurable threshold. Position sizing uses fractional Kelly Criterion. The terminal UI mirrors the design in the screenshot: three-panel layout with live agent log, position table, and market scan.

**Competitive edge source:** Professional forecast APIs update every 2–6 hours. Polymarket crowd prices lag. The bot exploits that latency gap, not a better forecast model.

**Reference wallets/proof:** coldmath (`@coldmath`) — weather-only, targets "No" positions at 95–99¢ when the market wildly overprices a bucket. $68K portfolio, global city coverage.

---

## Tech Stack

| Layer | Tool |
|---|---|
| Agent framework | [Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch) — skills system, memory, parallel subagents |
| Intelligence | Claude API (`claude-opus-4-7` default, `claude-sonnet-4-6` for fast scans) |
| Market API | [`py-clob-client`](https://github.com/Polymarket/py-clob-client) — Polymarket CLOB |
| Market discovery | Gamma API (`gamma-api.polymarket.com`) — no auth, market metadata |
| Portfolio tracking | Data API (`data-api.polymarket.com`) |
| Weather primary | Open-Meteo (free, no key, 14-day hourly, GFS/ECMWF/HRRR) |
| Weather verify | NOAA/NWS (`api.weather.gov`) — resolution source match |
| Weather ensemble | 31-member GFS ensemble via Open-Meteo — count members, not point forecasts |
| Terminal UI | `rich` (panels, tables, live updates) |
| Storage | SQLite (trade history, calibration) + JSON per-market snapshots |
| Blockchain | Polygon (chain ID 137), USDC, small MATIC for gas |

---

## Key External APIs

```
Gamma API (market discovery, no auth):
  GET https://gamma-api.polymarket.com/markets?tag=weather

CLOB API (orders, wallet auth required):
  POST https://clob.polymarket.com/order

Open-Meteo (free, no key):
  GET https://api.open-meteo.com/v1/forecast
  GET https://ensemble-api.open-meteo.com/v1/ensemble  ← GFS 31-member

NWS/NOAA (resolution source — must match market):
  GET https://api.weather.gov/points/{lat},{lon}
  GET https://api.weather.gov/gridpoints/{office}/{x},{y}/forecast

METAR/Aviation (real-time obs, maps cities to airport ICAO codes):
  GET https://aviationweather.gov/api/data/metar?ids={ICAO}
```

**Critical**: Markets resolve against **NOAA station data** — always verify the resolution source in the market description and map to the correct airport/station (e.g. Dallas → KDAL, not city center; NYC → KLGA, not JFK).

---

## Architecture

```
hermes/
├── core/
│   ├── scanner.py         # Polls Gamma API, filters weather markets by volume/liquidity
│   ├── weather.py         # Fetches Open-Meteo, NWS, METAR; builds ensemble probability
│   ├── edge.py            # edge = model_prob - market_yes_price; EV calculation
│   ├── kelly.py           # Fractional Kelly (default 0.25×) position sizing
│   ├── executor.py        # py-clob-client wrapper: place, cancel, monitor
│   └── risk.py            # Stop-loss, max exposure, kill switch
├── data/
│   ├── markets/           # Per-market JSON: forecast snapshots, price history, PnL
│   └── calibration.db     # SQLite: predictions vs outcomes, Brier scores
├── ui/
│   └── dashboard.py       # rich-based terminal: 3-panel layout
├── skills/                # Hermes Agent skill files (.md)
├── config.json            # Thresholds, city list, bet caps, API keys
├── .env                   # Wallet private key, API credentials (never commit)
└── main.py                # Entry point: scan loop (~50 min cycle)
```

**Data flow per cycle:**
```
scanner.py → get weather markets (volume > $5K)
  → weather.py → fetch 31-member GFS ensemble per city
    → edge.py → compare ensemble probability vs market YES price
      → if edge > MIN_EDGE (default 0.05):
          kelly.py → size position (fractional Kelly × bankroll)
          executor.py → place limit order via py-clob-client
          data/markets/ → log snapshot
```

---

## Core Concepts

### Ensemble Probability
Count GFS ensemble members exceeding the temperature threshold, divide by 31. If 24/31 members show Dallas > 84°F, model assigns 77% probability. If Polymarket trades that bucket at 0.40, edge = 0.37 — very strong.

### Kelly Criterion (fractional)
```python
kelly = (win_prob * (1/price - 1) - (1 - win_prob)) / (1/price - 1)
position = kelly * KELLY_FRACTION * bankroll  # KELLY_FRACTION default 0.25
position = min(position, MAX_BET, bankroll * 0.05)  # hard caps
```

### EV Gate
Only trade if `EV = win_prob * (1 - price) - (1 - win_prob) * price > MIN_EV` (default 0.03).

### Resolution Source Matching
Before placing any trade: parse the market description for the resolution station. Map cities to ICAO codes. Fetch NWS data for that exact station. Do not use Open-Meteo coordinates for a city center if the market resolves against an airport station.

---

## Commands

```bash
# Install Hermes Agent
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
hermes                      # start agent

# Install Python deps
pip install py-clob-client web3 python-dotenv requests rich aiohttp aiosqlite

# Run the bot (main loop)
python main.py

# Run in simulation mode (no real orders)
python main.py --sim

# Scan markets without trading (read-only dashboard)
python main.py --scan-only

# Run a single city edge check
python -m hermes.core.edge --city "Tokyo" --date "2026-04-20"

# Calibration report (past predictions vs outcomes)
python -m hermes.data.calibration --report

# CLOB client auth test
python -c "from py_clob_client.client import ClobClient; c = ClobClient(...); print(c.get_ok())"
```

---

## Environment Setup

`.env` (never commit):
```
PK=<wallet_private_key>
FUNDER=<funder_address_if_proxy_wallet>
CHAIN_ID=137
CLOB_HOST=https://clob.polymarket.com
ANTHROPIC_API_KEY=<key>
```

`config.json`:
```json
{
  "min_edge": 0.05,
  "min_ev": 0.03,
  "kelly_fraction": 0.25,
  "max_bet_usd": 50,
  "max_exposure_usd": 200,
  "min_market_volume": 5000,
  "scan_interval_seconds": 3000,
  "cities": ["Chicago", "Dallas", "Tokyo", "London", "Singapore", "Seoul", "Ankara"],
  "city_icao_map": {
    "Chicago": "KORD",
    "Dallas": "KDAL",
    "Tokyo": "RJTT",
    "London": "EGLL",
    "Singapore": "WSSS",
    "Seoul": "RKSI",
    "Ankara": "LTAC"
  }
}
```

---

## py-clob-client Key Patterns

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

client = ClobClient(host, key=private_key, chain_id=137, signature_type=0)
client.set_api_creds(client.create_or_derive_api_creds())

# Read-only (no auth needed)
markets = client.get_simplified_markets()
book = client.get_order_book(token_id)
mid = client.get_midpoint(token_id)

# Place limit order
order = client.create_order(OrderArgs(
    token_id=token_id,
    price=price,       # USDC, 2 decimal places
    size=size,         # shares
    side="BUY",
    order_type=OrderType.GTC
))
resp = client.post_order(order, OrderType.GTC)
```

---

## Reference Repos

- [`alteregoeth-ai/weatherbot`](https://github.com/alteregoeth-ai/weatherbot) — Kelly + EV filtering, simulation mode, 20 cities
- [`suislanchez/polymarket-kalshi-weather-bot`](https://github.com/suislanchez/polymarket-kalshi-weather-bot) — GFS 31-member ensemble, Brier calibration, React dashboard
- [`hcharper/polyBot-Weather`](https://github.com/hcharper/polyBot-Weather) — simpler reference implementation
- [`Jon-Becker/prediction-market-analysis`](https://github.com/Jon-Becker/prediction-market-analysis) — 36GB historical dataset, market microstructure research
- [`Polymarket/agents`](https://github.com/Polymarket/agents) — official AI agent framework: GammaMarketClient, Chroma vector DB for news RAG, LangChain integration, CLI pattern
- [`ent0n29/polybot`](https://github.com/ent0n29/polybot) — reverse-engineered Polymarket strategy patterns

---

## Competitive Intelligence

### PolyGun ([polygun.xyz](https://polygun.xyz))
Telegram bot that offers copy trading, limit orders, and portfolio tracking on Polymarket. **Use it to:**
- Watch coldmath (`@coldmath`) positions in real-time via copy-trade feature
- Study how profitable wallets size and time their entries
- Validate your own bot's signals against human top traders
- **Not a developer API** — 1% fee per tx, Telegram-native only

### Wallet Targets to Study
- `@coldmath` — weather-only, $68K portfolio, "No" positions at 95-99¢ entry, global cities
- Use Polymarket Data API to pull trade history: `https://data-api.polymarket.com/activity?user={address}`

### Official Polymarket Agents Framework
`github.com/Polymarket/agents` provides the canonical pattern: `GammaMarketClient` for market metadata, Pydantic models for trades, RAG over news, LangChain for reasoning. Hermes extends this with weather-specific edge calculation and Hermes Agent skill system.

---

## Risk Controls (mandatory before live trading)

1. Hard stop-loss at 20% drawdown (halt all new orders)
2. Max single position: `min(kelly_size, MAX_BET_USD, 5% bankroll)`
3. Max total exposure: `MAX_EXPOSURE_USD` across all open positions
4. Slippage filter: skip markets where spread > $0.03
5. Liquidity filter: skip markets with volume < $5,000
6. Kill switch: `python main.py --halt` cancels all open orders immediately
7. Paper-trade mode: all logic runs but `executor.py` logs instead of submitting

---

## Hermes Agent Integration

Install Hermes Agent and store bot skills under `skills/`. Key skills to create:
- `scan_markets.md` — triggers market scan for weather edges
- `check_position.md` — reports open positions and unrealized PnL
- `place_bet.md` — wraps executor with confirmation prompt
- `calibration_report.md` — surfaces Brier scores for recent predictions

Hermes runs Claude (`claude-opus-4-7` for reasoning, `claude-sonnet-4-6` for fast polling) via its multi-provider system. Switch with `hermes model`.

---

## What You Can Do From Terminal Right Now

```bash
# 1. Bootstrap the agent
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash

# 2. Set up Python env
cd ~/Antigravity/Hermes
python3 -m venv .venv && source .venv/bin/activate
pip install py-clob-client web3 python-dotenv requests rich aiohttp

# 3. Test Polymarket read-only (no wallet needed)
python3 -c "
from py_clob_client.client import ClobClient
c = ClobClient('https://clob.polymarket.com', chain_id=137)
print(c.get_simplified_markets())
"

# 4. Pull weather data for a city (no key needed)
curl 'https://api.open-meteo.com/v1/forecast?latitude=35.68&longitude=139.69&hourly=temperature_2m&forecast_days=3'

# 5. Fetch GFS ensemble (31 members)
curl 'https://ensemble-api.open-meteo.com/v1/ensemble?latitude=35.68&longitude=139.69&hourly=temperature_2m&models=gfs_seamless'

# 6. Clone reference weatherbot for patterns
git clone https://github.com/alteregoeth-ai/weatherbot /tmp/weatherbot-ref
```
