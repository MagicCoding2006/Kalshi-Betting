# BTC probability and swing research

Open [BTC outcome research](http://127.0.0.1:8787/btc-system) on the shared Signal Desk server. This module implements the hybrid first stage of the BTC × Kalshi blueprint: reuse recorded Kalshi books while collecting new spot order flow, then compare settlement probabilities on chronological holdouts. It is independent of the existing seconds-ahead Markov/LSTM lab and paper portfolios.

The same page now includes a **BTC touch calculator and Kalshi swing lab** for the separate objective of reselling a contract within its 15-minute window. Settlement, touching a BTC price, and making money on a Kalshi resale are three different events.

## Run

From the project root:

```sh
sh BTC/system/setup.sh
.venv-btc-research/bin/python server.py
```

There is no frontend build. If the shared server is already running, stop that instance before launching updated code. `--no-btc-system` disables the new background collectors. Starting the shared server still pauses the existing trading bots until explicitly enabled.

The setup creates an isolated environment for LightGBM, scikit-learn, DuckDB, Parquet, and WebSocket support. Training runs in a separate process and exports portable JSON trees, so the web app does not import LightGBM. On this Mac, the training launcher can reuse the OpenMP library bundled with an existing PyTorch installation. Other macOS installations may need `libomp` for LightGBM. This setup does not modify the global Python packages.

Optional authenticated **data feeds** use these environment variables:

```sh
export KALSHI_API_KEY_ID="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="/absolute/path/to/private-key.pem"
```

Use a read-only key. The BRTI channel additionally requires the account's CF Benchmarks entitlement. Keys stay in the server environment; neither the browser nor the training worker receives them. This module has no order endpoint or connection to portfolio cash.

## Data and timing

- Coinbase Exchange BTC-USD: public `level2_batch`, `matches`, and heartbeat streams. L2 sizes are absolute quantities; zero removes a level. Coinbase's reported trade side is the maker side and is inverted to obtain aggressor flow. Trade-ID gaps reset flow windows.
- Kraken BTC/USD: public v2 book depth 100 and actual trade updates. Decimal precision is retained for top-ten CRC32 validation; checksum failure invalidates the book and reconnects for a fresh snapshot. Trade snapshots are not counted as new flow.
- Kalshi: the existing shared recorder targets 500 ms REST books. With credentials, research additionally records WebSocket snapshots, deltas, and trades, and prefers a fresh streamed book for observations. Without credentials, public trades are polled every five seconds. Unchanged REST receipts are not claimed to be exchange events.
- Raw BRTI: the authenticated `cfbenchmarks_value` channel, preserving source time and local receipt time. Public Coinbase prices are explicitly tagged `proxy`; no historical index values or spot trade flow are invented.
- Features are joined every five seconds, using inputs already received. Spot books require a recent book update, rolling flow requires warm-up, and invalid/stale inputs remain missing. The dashboard refreshes every two seconds. Clock anomalies, gaps, and reconnects are journaled; there is no operating-system clock synchronization service.
- Volatility is measured from completed, contiguous Coinbase minute candles: trailing 30-return and 5-return estimates. Even when raw BRTI spot is available, volatility remains a Coinbase estimate. Zero drift is an explicit statistical benchmark, not an estimated positive-return assumption.

The UI has **Import recordings & outcomes**, **Run A / B / C study**, and **Export Parquet + DuckDB** actions. Import reads existing local BTC research books, joins official settled-market metadata, and samples one observation per five-second bucket when a fresh reference and usable volatility exist. Old records cannot retrospectively supply exchange trade flow.

## Settlement fidelity

Only supported `KXBTC15M` greater-or-equal contracts with matching BRTI averaging rules are admitted. The official contract text is shown on the page. YES wins on equality. Verification requires all 60 one-second slots in both opening and closing `(boundary − 60s, boundary]` raw-index windows, checks the published opening strike, and compares the reconstructed result to the official result. Missing coverage is unverified. Exact half-cent rounding cases are also unverified until that convention is confirmed.

Forecast A moment-matches a lognormal approximation to the arithmetic average of 60 future GBM samples with zero drift. This is an approximation, not exact settlement replication or a touch probability. Forecasts stop during the final 60 seconds because a partial-average model has not been implemented. Official labels remain usable for research when raw BRTI reconstruction is unavailable; the UI distinguishes those two facts.

## Experiments and validation

- **A:** statistical average-price baseline with later-period logistic calibration.
- **B:** fixed-parameter LightGBM using baseline plus Coinbase/Kraken depth, microprice, actual aggressor flow, quote OFI, depletion, replenishment, absorption, and cross-venue features. It requires at least 40 distinct labeled contracts with warmed spot flow; otherwise it shows **collecting**.
- **C:** LightGBM correction to the Kalshi midpoint logit, followed by logistic calibration. Before B's cohort exists, it is explicitly a **Kalshi-only ablation** using baseline and Kalshi book features. Once enough fresh flow exists, A/B/C share that matched flow cohort.

Whole contracts split chronologically 60% / 20% / 20%. Training labels must be available before calibration begins, and calibration labels before testing begins. Every contract has equal total weight regardless of snapshot count. Metrics include Brier score, log loss, reliability/ECE, time-remaining slices, market comparison, and a 500-resample contract-cluster bootstrap interval. Portable JSON inference is checked against the original LightGBM output during every fit. Global gain importance describes training usage; it is not a causal explanation of a live signal.

Official settlement timestamps control historical label/capital availability when supplied. Otherwise expected expiration (or close + five minutes) is explicitly marked as a proxy, not historical receipt time. The dashboard records contract membership and identical-holdout fingerprint reuse counts. Changing the dataset can still reuse some old test contracts; those reports are exploratory and are not a permanent untouched holdout. No rolling multi-regime study or automatic hyperparameter search is claimed.

The sequence-model gate requires B to beat A with a contract-bootstrap interval below zero across at least 30 independent test contracts. Binance, derivatives, sequence training, automatic strategy promotion, and a twelve-week historical dataset are deferred. An accurate outcome forecast is not automatically a profitable trade.

## Adaptive swing policy: expected log growth

**Train adaptive swing policy** is the current swing experiment. It replaces the universal 5% profit target and 10% stop with market-dependent bid levels, holding times, and whole-contract sizes. The archived fixed-target experiment remains available for comparison.

The objective is positive expected log wealth growth after costs, evaluated per expected holding second. Kelly sizing solves `argmax_f E[log(1 + f R)]` over the complete modeled net-return distribution. It uses quarter Kelly, a 2% capital cap, and a 1% portfolio expected-shortfall budget. These are explicit risk preferences; they are not estimated returns. The loss-budget calculation uses the worst 5% tail of the nonnegative loss part, so rare full-cost losses cannot be offset by gains within the same tail. No fixed ROI percentage is an entry requirement or exit target.

The price process models Kalshi's bounded YES price in **log-odds space**. Five-second transitions are measured from actual received books, conditioned on price, time remaining, model-versus-market disagreement, fee schedule, and book imbalance. Contract-weighted drift, drift uncertainty, volatility, mean reversion, and flow persistence are estimated from the recording. Drift is shrunk toward zero when uncertain. Empirical innovations retain observed tails; a fitted mean-reversion term pulls toward the current underlying-based settlement benchmark. This process is separate from the BTC touch calculator's zero-drift GBM benchmark.

For each supported state, Monte Carlo paths carry a persistent parameter-uncertainty draw plus empirical price shocks. Holding horizons depend on measured flow decay and remaining contract time, subject to an explicit 120-second research cap and 15-second close buffer. Candidate targets are quantiles of simulated maximum bids; candidate stops are quantiles of minimum bids, with a volatility-derived latency-noise floor. The optimizer compares these finite candidate policies and integer quantities by expected log growth per holding second, using cent-rounded fees, entry-price limits, exit latency, and an estimated gap-loss probability. This is a finite policy search, **not a proven global optimal-stopping solution**.

Targets/stops vary between trades and update in the live candidate view. The replay fixes a selected trade's bid barriers and deadline at entry; it does not claim continuous re-optimization of an already open position. Stop fills can be worse than their trigger. Quantity must be available at the best observed ask and bid; replay approximates all-or-none execution, not queue priority or guaranteed depth. Unknown exits are debited as full-cost stress losses, and only one position is open at a time.

The first 50% of whole contracts fit the process; the next 20% calibrate drift trust and variance; the final 30% evaluate the frozen policy. Labels that cross a boundary are purged. Drift trust is selected from `0, 0.25, 0.5, 1` by later forecast error, not profit; nonzero trust needs an improvement interval below zero across independent calibration contracts. Variance estimates are inflated if later forecast errors are larger. Gap risk uses a conservative upper rate estimated from training/calibration recording gaps. The simulation's conservative mean-return statistic is a lower quantile of parameter-scenario mean returns, **not an empirical 95% confidence interval**.

Research entries require positive conservative modeled return, positive expected log growth, calibration support, and fractional-Kelly/tail-risk budgets. The live monitor additionally requires at least 30 held-out trades across 20 windows with a positive contract-bootstrap P&L interval before showing an entry candidate. Failing any gate produces a zero stake. Historical validation is still exploratory; regime changes and correlated trades can invalidate both probabilities and sizing. This is not a guarantee of positive future or long-run ROI.

The page reports expected ROI, conservative modeled ROI, probability of a profitable resale, full/fractional Kelly, portfolio tail loss, variable target/stop bid levels, holding time, and quantity. Reports include realized return on deployed capital, log wealth growth, net P&L, drawdown, and censor stress. Model artifacts are `adaptive-swing-model.json` and `adaptive-swing-report.json`; NumPy is needed for live simulation and is installed by the research setup script. The module remains independent of the active paper portfolios.

## Archived fixed-target swing experiment

The touch calculator estimates the GBM first-passage probability of BTC reaching a chosen price before the current contract closes, starting from the latest fresh reference. Its default target is the current Kalshi strike. It uses measured realized volatility and explicitly uses zero arithmetic drift as a benchmark. It handles upward/downward targets, already-at-target prices, and zero-time/zero-volatility cases. A touch is not the final-minute average or an automatic Kalshi payout. Without raw BRTI, the reference remains a labeled Coinbase proxy.

The swing strategy's target is **5% net gain on the trade's entire entry cost**, after entry and exit fees and cash rounding. It has a fixed 10% net stop, 60-second maximum hold, 15-second close buffer, and 250 ms entry/exit latency. The live table calculates the minimum executable sell bid needed to meet the 5% target; midpoint or ask rallies do not count as sellable profit. A custom BTC target changes the touch calculator only. The cash model uses the active contract strike and Kalshi quote-state history.

The archived `swing` job fits a distinct empirical fixed-target model. Features are the probability of touching the contract strike during the holding period, side-aligned Kalshi depth imbalance, recent quote momentum, volatility-model disagreement with Kalshi, price level, fee schedule, and remaining time. Labels come from actual later quotes: buy at an observed ask, then sell at the first target, stop, or time-triggered bid after exit latency. A target trigger can lose its profit before the exit quote arrives. Entry limits reject adverse moves greater than half a cent. Entries whose spread and fees already breach the planned stop are rejected.

This first experiment uses **one whole contract per entry** so probability labels and cent rounding match the proposed trade exactly. The entry must fit a quarter-Kelly budget and 2% capital cap on a separate $100 research bankroll. Kelly uses the observed return distribution rather than settlement YES probability. No-trade is an explicit outcome. A 5% gain on a $2 position is $0.10, not 5% of a $100 account.

Whole contract windows split chronologically 70% training / 30% historical test, with training labels purged if not mature before testing. Decisions sample every 15 seconds; actual execution uses the recorded approximately 500 ms quotes. Each state uses only its earliest training observation per contract and side. Predictions require 20 independent contracts in the detailed state or 30 in an explicitly coarse fallback. Entries require a positive one-sided 95% bootstrap lower mean net return and at most 5% censored training paths. These are conservative experimental policies, not guarantees of a risk-adjusted 5% expected return. Contract resampling also does not prove independence across regimes.

Missing paths, quote gaps over two seconds, absent fees, or insufficient exit depth are not assigned invented profitable exits. Censored paths use a **full-cost loss stress** for fitting and replay; the UI separately counts them. Live paths that have not matured remain pending. Only one trade can be open at a time, cash stays committed until exit, and both YES and NO are considered from decision-time inputs. Their resale-profit probabilities are not complements. The historical test is diagnostic, not a new untouched lockbox; it does not alter the frozen outcome experiment.

The report also shows a fixed **settlement signal with swing exits** benchmark using the same test quotes and execution rules. This illustrates why a favorable settlement forecast does not automatically produce a favorable short-hold return. Outcomes include net P&L, target hits, censoring, drawdown with gap stress, and the count of windows achieving 5% of portfolio value.

Live swing quotes use a small API polled at a 500 ms target, while underlying references update at their own cadence. `swing-model.json` and `swing-report.json` live beside the other research artifacts. This is a research replay and live candidate monitor; it does not change the existing portfolio bots or their take-profit settings, identify bots behind orders, or place real orders.

## Market-anchored refinement

**Run refinement study** audits the original calibration/side bias and evaluates five fixed alternatives: unchanged Kalshi midpoint, 10% and 25% blends of baseline versus market log-odds, and two regularized residual models with penalties 10 and 50. The residual models have **no fitted intercept**, cap the adjustment at ±0.5 log-odds, and use only direction-sensitive residual, book-imbalance, microprice, time-interaction, and volatility-interaction features. A quote-only history does not receive fabricated spot flow.

The first run freezes the development contracts and excludes **every previously published test contract**. Four expanding chronological folds fit only on labels already available before validation begins; each contract contributes equal total weight. The winner is chosen by walk-forward Brier score, never by largest replay profit. Four challengers use 98.75% contract-bootstrap intervals as an approximate Bonferroni familywise 95% screen. These intervals still assume contract clusters can be resampled independently; they do not establish robustness across long market regimes.

An eligible challenger needs at least 60 validation contracts, at least three folds, improvement in at least 75% of folds, a Brier interval entirely below zero, log loss no worse than the market, and positive net development replay. If any gate fails, **D**, the gated live selector, retains the market benchmark and reports no demonstrated edge. The frozen challenger remains visible for shadow research. None of these models connects to the paper portfolios.

The replay uses the predictions generated before each fold, not the later full-data refit. Every candidate shares the same explicit entry policies: 90-second cutoff, at most 3¢ spread, reference age at most five seconds, 3 pp net edge, quarter Kelly, 2% individual and 5% aggregate cost caps. These are fixed research policies. Latency stress runs at 250 ms, one second, and two seconds use the same policy; they are not searched to select the best profit. Conservative non-direct cash rounding aligns the entire principal-plus-fee balance change to cents, including sub-cent prices.

`refinement-protocol.json`, `refined-model.json`, and their fingerprints keep the selection boundary and model fixed across reruns. The server records the candidate's actual emitted predictions in `shadow_forecasts`; imported history cannot populate the prospective cohort. The first **30 later contracts**, opening after model publication and receiving live shadow predictions, form the prospective test. Its scores/P&L remain hidden until all 30 outcomes are available, then `prospective-final.json` is persisted once. A matching `prospective-status.json` updates after the periodic outcome sync, about every five minutes. The server must stay running to collect these forecasts. No forecast, result, or paper cash is fabricated during outages.

The dashboard distinguishes original exposed-test results, development walk-forward results, and the prospective test. Their contract cohorts differ; their profits are not interchangeable before/after figures. A passing prospective result still does not automatically enable trading. New inference code or a changed candidate must use an explicitly new research protocol rather than silently editing this locked study.

## $100 execution replay

The independent replay chooses the better positive YES/NO edge, waits 250 ms, and uses the first actually recorded quote at or after that time, at most one second late. It traverses the observed ask ladder, handles whole quantities and partial fills, rounds estimated taker fees, and rejects missing quotes or unsupported fees. It uses quarter Kelly, a 2% per-contract cost cap, a 5% aggregate cap, and a 3 percentage point net-edge threshold; these are explicit research policies, not measured market quantities. There is one entry per contract, held to official settlement with capital locked until label availability.

Replay P&L, fill rate, rejects, partial fills, and maximum **realized** drawdown are shown. Remaining positions are valued at cost for that drawdown measure; it is not mark-to-market drawdown. Quote visibility does not guarantee execution, and queue priority is not simulated. No simulated result alters the shared paper portfolios.

## Files and APIs

Production research data live in `BTC/.data/system/`: the WAL SQLite journal, immutable observations, compressed raw events, BRTI ticks, contract metadata, reports, and `models.json`. Raw messages retain the newest 200,000; feature, tick, gap, and quote journals persist. Export periodically if longer raw histories are needed. Export writes feature/raw Parquet partitions by UTC date, venue, and symbol, and `lake/research.duckdb` with the `outcome_features` view. Raw payload blobs remain zlib-compressed JSON. Custom `--db` portfolios receive a separate outcome directory beside their research database.

```text
GET  /api/btc-system
GET  /api/btc-system/swing?target=65000   # optional BTC target; default is current strike
POST /api/btc-system/job   {"kind":"import" | "train" | "refine" | "adaptive" | "swing" | "export"}
```

Jobs run one at a time. Same-origin protections match the shared app. Reported errors and missing data remain visible; no demo values are substituted.

## Primary references

- [Kalshi 15-minute crypto settlement](https://help.kalshi.com/en/articles/13823838-crypto-markets)
- [Kalshi WebSocket order books](https://docs.kalshi.com/websockets/orderbook-updates)
- [Kalshi CF Benchmarks channel](https://docs.kalshi.com/websockets/cfbenchmarks-value)
- [Coinbase Exchange WebSocket channels](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels)
- [Kraken v2 book and checksum](https://docs.kraken.com/api/docs/websocket-v2/book/)
- [LightGBM Python API](https://lightgbm.readthedocs.io/en/stable/Python-API.html)
- [Calibration requirements and limitations](https://scikit-learn.org/stable/modules/calibration.html)
- [Time-series validation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
- [Kalshi balance and fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [University of Chicago: hitting probabilities for Brownian motion with drift](https://galton.uchicago.edu/~yibi/teaching/stat317/2021/Lectures/Lecture25.pdf)
- [Kelly: A New Interpretation of Information Rate](https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf)
- [Busseti, Ryu, Boyd: Risk-Constrained Kelly Gambling](https://web.stanford.edu/~boyd/papers/pdf/kelly.pdf) — theoretical context; this prototype does not implement that paper's formal drawdown guarantee.
