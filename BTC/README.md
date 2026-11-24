# BTC strategy · shared Signal Desk

BTC now shares one dashboard and server with Gold. See [the workspace README](../README.md) for the multi-strategy layout. The old BTC launcher still starts the shared app; existing BTC data are retained.

A local frontend and paper-trading research engine for Kalshi's `KXBTC15M` Bitcoin markets. Python 3.10+; no packages, API keys, Node build, or external database required.

## Run

From the repository root:

```sh
python3 BTC/server.py
```

Open **http://127.0.0.1:8787**. Keep the terminal running; stop with Ctrl+C. Use `--port 8788` to choose another port. On macOS you can also double-click `Start BTC.command`.

The dashboard includes:

- Current contract, published target, live Coinbase BTC/USD spot proxy, and actual time remaining.
- Separate spot, Kalshi reference, and model-versus-market probability charts.
- YES/NO book depth, executable asks, spreads, and available fractional contract quantities.
- Current decision, net edge, whole-contract sizing, and a reason for waiting or buying.
- Saved decision details with exact inputs/settings and CSV export.
- Paper positions, bid-depth marks, cash, realized P&L, and official-result settlement.
- Editable volatility, fractional Kelly, edge buffers, capital caps, and exit rules.

Monitoring starts immediately. **Start paper bot** enables automatic simulated entries and exits. It starts paused after every server restart. **Paper trade this signal** simulates the current qualifying signal. There is no real order-submission code or account connection. No live-money credentials are requested or stored.

## Data

The server targets order-book sampling every 500 ms and reference/model-input refreshes every three seconds. No scraping or authenticated Kalshi Pro session is needed.

| Source | Purpose |
| --- | --- |
| Kalshi `/markets?series_ticker=KXBTC15M&status=open` | Discover an active 15-minute market, strike, rules, and close time |
| Kalshi `/markets/{ticker}/orderbook` | Up to 100 price levels per side; dollar prices and fixed-point quantities |
| Kalshi `/series/KXBTC15M`, `/events/{event_ticker}` | Quadratic fee model and event multiplier overrides |
| Kalshi `/live_data/events/{event_ticker}?range=1h` | Separate Kalshi reference chart for settlement context |
| Coinbase Exchange `/products/BTC-USD/ticker` | Timestamped raw spot-price proxy for the GBM model |
| Coinbase Exchange `/products/BTC-USD/candles?granularity=60` | Up to four hours of minute closes, refreshed every 60 seconds |
| Kalshi `/markets/{ticker}` | Authoritative YES/NO result for pending paper settlement |

Kalshi returns bids on both sides. A NO bid at 40¢ is an executable YES ask at 60¢, with the same size. The app derives asks from the opposite side rather than using last trades or midpoints as entry prices. It supports fractional displayed quantities but sizes whole contracts only.

This is polled REST data, not an authenticated WebSocket stream. Exchange response caches and network latency can delay observations. The displayed source timestamps and local book receipt times support freshness guards; they do not guarantee exchange-time synchronization. Kalshi reference-chart failure is isolated from the spot/book model; Coinbase spot failure blocks new entries; candle failure blocks entries when realized volatility is selected.

## Baseline model

```text
T = seconds until actual market close / (365 × 24 × 3600)
d2 = [ln(S/K) + (mu - sigma²/2) × T] / (sigma × sqrt(T))
P(YES) = normal_cdf(d2)
P(NO) = 1 - P(YES)
```

`S` is the Coinbase BTC/USD spot proxy, `K` is Kalshi's published strike, and `mu` is estimated automatically from the same completed minute returns: `mean(log return) × 525600 + sigma²/2`. Its sampling standard error is displayed in settings. This short-history estimate is noisy and is not a reliable forward drift forecast. Select the explicitly labeled zero-drift benchmark or manual mode if desired. When `mu` is the risk-free rate, `N(d2)` is the Black–Scholes risk-neutral digital probability; with a physical drift assumption, it is a GBM terminal probability. This is **not** a calibrated forecast of real-world outcomes. Returns are not assumed to earn the quoted option edge with certainty.

Realized `sigma` is the sample standard deviation of completed contiguous one-minute log returns, annualized by `sqrt(525600)`. At least 30 returns are required. Missing-minute gaps are excluded; unfinished candles are excluded. Alternatively, supply manual annualized volatility.

These Kalshi contracts resolve using the **final 60-second average of CF Benchmarks' BRTI**, compared with the opening reference average. They do not resolve on touching a target at any point. Coinbase spot differs from BRTI, and Kalshi's chart can itself contain averaged values; the chart is intentionally kept out of the model inputs. This baseline approximates an averaged-settlement contract with a terminal-price model. It neither models the arithmetic average exactly nor corrects spot/index basis. A default 2 percentage point probability haircut is a configurable heuristic, not a statistical confidence interval. The last 75 seconds are blocked for new entries. Calibrating against historical outcomes and adding the entitled BRTI feed would be the next modeling steps.

## Measured inputs and explicit policies

Strategy settings now separate automatically computed values from user risk policies. Automatic volatility, historical drift with sampling error, and observed quote-movement buffers refresh without manual entry. Manual overrides are hidden and disabled unless selected. Missing data remain pending instead of falling back to the displayed old 60% override.

Entry thresholds, probability haircuts, capital caps, and the safety margin before settlement are policy choices; they cannot be looked up as facts from the exchange. The app records one forecast per market in the 10–15-minute-to-close cohort and retrieves official settlement outcomes independently of whether a paper trade occurs. Settings show resolved/pending counts and Brier score when available. This diagnostic is not a validated probability haircut or an optimized entry threshold; the UI explicitly reports missing calibration evidence. No historical P&L or calibration scores are invented.

## Fees, edge, and sizing

Both YES and NO are evaluated separately and displayed side by side, including complementary probabilities, executable asks, net edges, and eligible sizes. The engine buys whichever has the better **positive fee-adjusted edge**, not simply the higher probability. A high P(NO) does not justify buying an even more expensive NO contract:


```text
adjusted_p = max(0, model_p - probability_haircut)
net_edge = adjusted_p - ask - estimated_fee_per_contract - slippage_buffer
full_kelly = max(0, (adjusted_p - all_in_unit_cost) / (1 - all_in_unit_cost))
budget = min(equity × fractional_kelly,
             equity × position_cap,
             equity × exposure_cap - existing_open_cost,
             available_cash)
```

Default settings: $100 paper bankroll, quarter Kelly, 2% position cap, 5% aggregate cost exposure cap, 3 percentage point minimum net edge, 2 percentage point probability haircut, and an automatically observed execution buffer. The 3 pp threshold and 2 pp haircut are explicit risk-policy choices, not values supplied by Kalshi or empirically validated defaults.

Taker fee estimate: `0.07 × fee_multiplier × contracts × price × (1 - price)`, conservatively rounded up to cents per simulated fill. Series/event fee metadata must be available and quadratic; an unknown fee model blocks entries. This estimates non-direct account fees and does not implement the exchange's full fill-level accumulator/rebate mechanics. The execution buffer reduces edge and budget but is not charged as a simulated fill expense. In automatic mode it is the 95th percentile of adverse best-ask changes between consecutive REST polls, separately for YES and NO, using at most 1,200 intervals from the last ten minutes. At least ten valid 0.2–10 second intervals are required per side; new candidates stay pending until enough observations arrive. Rollover clears quote samples. This measures historical quote movement, not actual realized slippage or a guarantee about the next fill. A manual override is optional and clearly labeled.

The final quantity is floored to whole contracts and capped by liquidity **at the best ask**. The engine does not assume the entire position fills at a midpoint, walk the ask ladder, or increase quantity beyond that best-level depth. It rechecks fees after quantity rounding. Exactly one entry is allowed per contract window, including after an earlier exit, to avoid repeated entries and immediate re-entry churn.

## Repricing and paper positions

The current fair value and candidate size update at fresh book evaluations (500 ms target). Research experiments live in the shared Research Lab and do not drive paper orders. With the paper bot running, an existing position exits at visible bid depth if:

- Net liquidation return reaches the take-profit threshold (default +15% on cost).
- Net liquidation return reaches the stop-loss threshold (default -20% on cost).
- The market's best bid reaches model fair value, outside the final-minute guard.

Pausing the bot pauses automatic entries **and exits**; monitoring, manual exits, and official settlement continue. Partial fills, queue priority, order latency, execution races, and real slippage are not simulated. A full manual/automatic exit is rejected if visible bid depth cannot cover the position. Fees are estimated on both entry and exit. Take-profit and stop-loss thresholds are triggers, not guaranteed execution prices.

After closing, an unresolved position awaits Kalshi's official result. It is never settled from the local spot proxy. Official settlement checks run roughly every 30 seconds when the feed is healthy. An open position with unavailable fresh bid depth is marked at zero for conservative equity and shown as unquoted. This can understate equity until quotes or settlement return.

New entries are blocked for stale/missing spot or book data, a closed/unopened market, missing strike, unsupported payoff/fees, crossed books, insufficient volatility history, the final-minute guard, already-traded windows, and insufficient edge/budget/liquidity.

## Storage and API

Everything is local in `BTC/.data/btc.sqlite3` (ignored by Git). Config, cash, fills, and the latest 5,000 decision records persist. Logs include the book, timestamps, probability, fees, model version, settings, and reasons. The UI shows the latest 100 records; export includes up to 5,000. The starting bankroll is editable only before the first trade. **Paper portfolio → Reset to $100** archives every current position, the cash balance, and settings, clears current-run positions/P&L, restores $100 cash, and pauses the bot. Decision records and collected forecast outcomes remain intact. Archived runs can be inspected in the dashboard. Position IDs are never reused, so an in-flight settlement cannot credit a reset portfolio for an old position. `--db /absolute/path.sqlite3` starts a separate paper portfolio.

The HTTP server binds only to `127.0.0.1`, rejects foreign Host headers, and rejects cross-origin mutations.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/state` | Current market, model, book, charts, log, and portfolio |
| `GET /api/decision?id=123` | Full saved audit record |
| `GET /api/export.csv` | Download retained decisions |
| `GET /api/health` | Server and feed status |
| `POST /api/config` | Validated strategy updates |
| `POST /api/auto` | `{"enabled": true}` to enable the paper bot |
| `POST /api/paper/buy` | Simulate a qualifying current signal |
| `GET /api/paper/run?id=1` | Inspect archived paper-run cash, settings, and positions |
| `POST /api/paper/reset` | Archive current run, restart with $100 cash, and pause the bot |
| `POST /api/paper/close` | `{"id": 1}` to exit a paper position |

## Checks

```sh
python3 -m unittest discover -s BTC/tests -v
python3 -m py_compile desk/model.py desk/server.py BTC/feed.py
node --check desk/static/app.js
```

Tests cover known digital probabilities, volatility sampling, reciprocal asks, fractional liquidity, fee rounding, Kelly/capital caps, freshness/expiry guards, simulated accounting, official settlement, duplicate-entry protection, API validation, and local request restrictions. They use isolated temporary databases and mocked external responses; no real trades are placed.

## Primary references

- [Kalshi public market data and REST freshness](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi order-book schema and reciprocal asks](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Kalshi event live data](https://docs.kalshi.com/api-reference/live-data/get-event-live-data)
- [Kalshi fee schedule](https://kalshi.com/fee-schedule) and [rounding mechanics](https://docs.kalshi.com/getting_started/fee_rounding)
- [Authenticated BRTI feed and averaging semantics](https://docs.kalshi.com/websockets/cfbenchmarks-value)
- [CF Benchmarks passthrough access requirements](https://docs.kalshi.com/cfbenchmarks/rest-passthrough)
- [Coinbase Exchange product ticker](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductticker) and [candles](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles)
