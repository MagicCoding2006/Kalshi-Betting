# Signal Desk — BTC + Gold

One local dashboard and one server for both strategies. Switch between **Bitcoin** and **Gold** using the sidebar or the strategy selector. Both feeds and paper engines continue independently while you view either market.

```sh
python3 server.py
```

Open **http://127.0.0.1:8787**. On macOS, double-click **Start Signal Desk.command** instead. Python 3.10+ is sufficient; the dashboard, recorder, and Markov model use the standard library and need no frontend build. The optional LSTM experiment uses NumPy and PyTorch (`python3 -m pip install -r requirements-research.txt` if missing). The older `python3 BTC/server.py` and `BTC/Start BTC.command` also launch this same shared app.

If you run the command while the dashboard is already running, it prints the existing dashboard URL and exits successfully. It does not open the portfolios or start another bot. If another program occupies the port, startup reports that conflict without a traceback.

Direct views:

- [Bitcoin](http://127.0.0.1:8787/?asset=btc)
- [Gold](http://127.0.0.1:8787/?asset=gold)
- [BTC probability & swing research](http://127.0.0.1:8787/btc-system)

The [BTC outcome system](BTC/system/README.md) adds live Coinbase/Kraken book and trade collection, optional raw BRTI and Kalshi streams, five-second features, calibrated baseline/LightGBM experiments, whole-contract chronological validation, a separate $100 execution replay, and Parquet/DuckDB export. Run `sh BTC/system/setup.sh` for its optional dependencies. It shares this site and is independent of portfolio trading. The page also includes a BTC first-passage calculator and an adaptive swing experiment: stochastic bid targets/stops, variable holding times, and whole-contract fractional Kelly optimize modeled log growth after costs and tail risk. The prior fixed 5%/10% experiment is archived for comparison. The existing Research Lab below continues to forecast seconds-ahead quote movement; settlement and touch probabilities remain distinct from profitable-resale probabilities.

## Shared experience, separate strategies

Each strategy has its own:

- Live contract discovery, order book, reference-price chart, model probabilities, and YES/NO edge comparison.
- Automatically measured volatility, historical drift, and quote-movement execution buffers.
- Strategy settings, bot start/pause switch, decision audit log, CSV export, and calibration evidence.
- Paper cash, positions, P&L, official settlement, archived runs, and reset-to-$100 button.

Decision activity and the full log place **Model P(YES)** next to **Kalshi YES (mid)**. The Kalshi value is the midpoint of the saved YES bid and ask at that decision, not today's quote applied to a historical record. Hover for the bid/ask, or open the record for details. A one-sided or crossed book has no midpoint and shows a dash. CSV exports include both probabilities and the YES bid/ask. Actual entries continue to use executable asks and estimated costs.

With a bot enabled, exit checks run on each feed update (on each book update (500 ms target)). There is no minimum hold. A position closes when its net resale return reaches the configured take-profit (+15% by default) or stop-loss (-20% by default), or when its side's best bid reaches model fair value outside the entry cutoff. Return is measured against the position's entry cost including fees, with estimated exit fees deducted. The spread and fees alone can therefore trigger an exit at the next check. Positions otherwise remain open; after market close, they await the official result. Full visible bid depth is required for an exit. Pausing a bot pauses automatic exits as well as entries; official settlement continues.

New portfolios start with **$100 each**. These are separate simulation accounts, not one shared $100 balance. Existing BTC cash, positions, settings, forecasts, and archives are retained. Switching views never moves cash, resets a portfolio, or pauses the other bot. A Gold reset affects Gold only.

All trading is **paper trading only**. Both bots start paused when the server restarts. Market monitoring starts immediately; a closed market or unavailable source produces WAIT for that strategy. No real order endpoint or account connection is implemented.

## Price sources and contract rules

| Strategy | Series | Model input | Settlement |
| --- | --- | --- | --- |
| Bitcoin | `KXBTC15M` | Timestamped Coinbase BTC/USD spot proxy; minute candles for returns | Final 60-second CF Benchmarks BRTI average versus the opening average |
| Gold | `KXGOLD15M` | Kalshi's public Gold reference series, sourced from Pyth; minute closes computed from its one-second ticks | Closing value of the one-minute Pyth Gold candle at window end versus opening close; two-decimal rounding |

Gold uses the series' published Pyth Gold index reference, **not BTC, a tokenized-gold substitute, or Coinbase candles**. The API's one-hour series supplies enough history to estimate realized volatility without inventing an input. A completed minute needs at least 50 distinct seconds, source coverage within five seconds of both boundaries, and a source timestamp before the minute boundary. Incomplete and poorly covered minutes are excluded; at least 30 contiguous minute returns are required for automatic volatility/drift.

Both strategies use the same lognormal digital Black–Scholes/GBM baseline and fractional Kelly sizing. BTC's averaged settlement and spot/index basis are approximations. Gold's candle-close rule fits the terminal-price interpretation more directly, but constant volatility, lognormal dynamics, source latency, and settlement rounding remain approximations. Historical drift is noisy; policy haircuts and entry thresholds are explicitly labeled, not claimed as learned optimal settings.

The default entry cutoff is 75 seconds for BTC and 60 seconds for Gold. These are risk policies. Gold does not inherit the claim that it settles on a final-minute average. The UI shows the exact current contract's primary and secondary rules in Model details.

## Layout

```text
server.py                  One app entry point
Start Signal Desk.command  macOS launcher
 desk/
   server.py               Shared HTTP app, strategy registry routing, paper engine
   model.py                Shared probability and sizing calculations
   assets.py               Asset metadata, settlement descriptions, feed selection
   static/                 Shared frontend
   research/               Recorder, reference backfill, Markov replay, optional LSTM
 BTC/
   feed.py                 Coinbase spot/candle adapter
   server.py, model.py     Compatibility launcher/imports
   .data/btc.sqlite3       Existing BTC portfolio and history
   tests/                  Original model/accounting/API regression suite
 GOLD/
   feed.py                 Kalshi/Pyth Gold adapter and minute aggregation
   .data/gold.sqlite3      Independent Gold portfolio and history
 tests/
   test_multi_asset.py     Gold data and cross-strategy isolation tests
```

`NBA/` now contains a separate [NBA pregame fair-value research service](NBA/README.md): real historical data, an ML training/calibration pipeline, public Kalshi winner-market comparisons, a [visual dashboard](http://127.0.0.1:8787/nba), and read-only `/api/nba/*` endpoints on this server. The sidebar links to the NBA board, with fair prices, executable-cost comparisons, filters and game details. Start its CLI scanner/watcher separately to keep snapshots current. The [NBA research specification](NBA/RESEARCH.md) lists basketball metrics, soccer-to-NBA translations and public data sources. It has no trading engine or portfolio. `CFB/` remains reserved for a future strategy.

## Research Lab

Select **Research lab** in the same sidebar for BTC or Gold. The experiments are independent of the paper-trading strategy: their predictions and replay P&L never open positions, change sizing, or change portfolio cash.

### Sampling and storage

- A dedicated order-book loop targets **500 ms** per asset, including network time. The UI refreshes at a 500 ms target; live experiment summaries update about once per second. Actual median receipt intervals are displayed.
- The existing reference/model worker runs every three seconds; completed volatility/history candles update as they become available. Half-second book sampling does not imply half-second underlying-source updates.
- Public REST requests are used. Quotes may be cached and are not exchange-event timestamps. On network errors or HTTP 429 responses, collection backs off rather than pretending the target cadence was met. Authenticated WebSocket snapshots/deltas would be the next feed upgrade; no credentials are required by this implementation.
- Research files are separate from portfolios: `BTC/.data/btc.sqlite3.research.sqlite3` and `GOLD/.data/gold.sqlite3.research.sqlite3`. They contain compressed top-ten-level books, derived features, reference candles, and experiment reports.
- Raw snapshots retain three days, reference candles seven days, and the latest twenty reports. Storage size is shown in the lab. The CSV exports up to 100,000 recent feature rows; compressed raw books remain in the local research database.
- Historical order books cannot be reconstructed from minute candles. Full book data start when this recorder starts. Repeated/unchanged quotes are retained; outages are not filled with fabricated ticks.

### Markov experiment

The first-order state uses depth imbalance (top five bid/ask levels), the previous quote direction, spread, and time-to-close buckets. It estimates the next price direction (down/flat/up) and mean price change over **2, 5, or 10 seconds**. Flat means within ±0.1¢. At least 100 matured horizon pairs are required; states with fewer than twenty examples explicitly use the global fallback. Direction probabilities use Laplace smoothing.

Targets use the first actually observed midpoint at or after the horizon, at most one second late. Pairs never cross a contract window or a sampling gap over two seconds. The optional Kalshi interior filter admits starting midpoints in 5–95%; it does not remove future outcomes that reach an extreme.

The backtest starts from past data and expands the model only when each new label has actually matured. With three or more contract windows, whole contracts are separated chronologically. Otherwise the report explicitly identifies a within-contract chronological split. Training labels that cross the initial split are purged. The report compares held-out price MAE and direction accuracy with no-change/flat baselines. Overlapping forecast errors are correlated; the sample count is not a count of independent market events.

The one-contract P&L replay chooses entries using the forecast and current spread, fills at observed asks, and exits at observed horizon bids. It includes recorded fee multipliers and conservative estimated taker fees. Entries cannot overlap, and both quotes need a visible whole contract. This is an execution approximation: no queue position, order latency, future slippage, or guaranteed fills are simulated. Its P&L is separate from the paper portfolio.

### Historical ranges and LSTM

Three days of reference history are backfilled automatically. BTC uses Coinbase minute OHLC candles in bounded API windows. Gold uses frozen historical Kalshi/Pyth event reference windows and completed minute OHLC values. The actual counts, date span, and missing coverage are displayed; source gaps are not invented. New completed candles continue to append after backfill.

LSTM inputs are twenty consecutive book snapshots, including quote/depth features, remaining time, spot-versus-strike distance, and the **past-only trailing three-day reference range**. Historical highs/lows are context, not support/resistance guarantees. New highs/lows can put the position outside 0–100%; those values are preserved. The optional 5–95% range filter applies to the prediction-time range position, not future outcomes.

The experiment requires 600 usable sequences and at least 48 hours of prior reference context. It uses a small CPU LSTM (16 hidden units), training-only normalization, chronological train/validation/test splits, purged horizon labels, and validation-only early stopping over at most twenty epochs. With four or more contract windows, splits use whole windows; smaller datasets explicitly report within-contract splits. Missing data or insufficient filtered sequences produce **collecting**, not an invented trained result.

Only NumPy and PyTorch are optional dependencies. Weights are saved locally next to the research database as `.lstm.pt`, alongside the feature/scaler metadata, and loaded on the next start. Live predictions use the last trained model; changing the horizon requires retraining that model. The UI distinguishes this frozen LSTM from the continuously updated Markov forecast. Short initial holdouts are diagnostic, not proof of an edge.

Use **Run Markov backtest** or **Train LSTM experiment**, inspect the held-out metrics, and use **Play replay** or the slider to see forecasts against their later observed prices. Replay includes the actual horizon observation time and quote context.

## Local API

All strategy-specific APIs accept `?asset=btc` or `?asset=gold`. Omitting it selects BTC for compatibility. Unknown, empty, and duplicate asset keys are rejected instead of silently routing a mutation to BTC.

```text
GET  /api/assets
GET  /api/state?asset=gold
GET  /api/decision?id=1&asset=gold
GET  /api/export.csv?asset=gold
GET  /api/paper/run?id=1&asset=gold
GET  /api/research?asset=gold
GET  /api/research/export.csv?asset=gold
POST /api/research/settings?asset=gold  {"horizon":5,"interior":true,"range_filter":true}
POST /api/research/job?asset=gold       {"kind":"markov"}  # history / markov / lstm
POST /api/config?asset=gold
POST /api/auto?asset=gold          {"enabled": true}
POST /api/paper/buy?asset=gold     {}
POST /api/paper/close?asset=gold   {"id": 1}
POST /api/paper/reset?asset=gold   {}
```

The browser includes the selected asset in every request and guards against responses from a previously selected view. Each database is bound to its strategy; accidental reuse for the other asset is rejected. The server listens only on loopback and rejects foreign Host headers and cross-origin mutations.

To run isolated test portfolios, pass both paths to the same server:

```sh
python3 server.py --db /tmp/test-btc.sqlite3 --gold-db /tmp/test-gold.sqlite3 --port 8788
```

## Verification

```sh
python3 -m unittest discover -s BTC/tests -v
python3 -m unittest discover -s tests -v
python3 -m py_compile desk/*.py BTC/*.py GOLD/*.py server.py
node --check desk/static/app.js
```

Tests cover the original BTC behavior, Gold's series selection and minute closes, stale/wrong/historical Gold data, independent settings and cash, position-ID collisions, resets, exports, bot controls, rejected asset keys, database binding, and failure isolation.

## Sources

- [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Order-book mechanics](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [Public event live data](https://docs.kalshi.com/api-reference/live-data/get-event-live-data)
- [Gold series and settlement source](https://external-api.kalshi.com/trade-api/v2/series/KXGOLD15M)
- [Original referenced Gold contract and its rules](https://external-api.kalshi.com/trade-api/v2/markets/KXGOLD15M-26NOV191930-30)
- [Detailed baseline, fees, sizing, and paper-execution assumptions](BTC/README.md)

Research references: [Kalshi order-book feed and authentication](https://docs.kalshi.com/websockets/orderbook-updates), [rate limits](https://docs.kalshi.com/getting_started/rate_limits), [historical contract candles](https://docs.kalshi.com/api-reference/market/get-market-candlesticks), [queue-imbalance study on equities](https://arxiv.org/abs/1512.03492), and [PyTorch LSTM](https://docs.pytorch.org/docs/stable/generated/torch.nn.LSTM.html). The equity research motivates an experiment; it does not establish predictability on Kalshi.
