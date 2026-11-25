# NBA live-performance research bot

This repository is a research and **paper-trading** foundation for estimating NBA game outcomes. It is not a live trading bot yet. There is no NBA data connection, Kalshi connection, calibrated model artifact, or backtest result in this repository. It cannot place trades.

## What it measures

The model uses a few different kinds of evidence:

1. **Game state:** score margin and time remaining.
2. **Pregame expectation:** expected home-team point margin from a point-in-time strength model.
3. **Live offensive performance:** each team's points per possession compared with its pregame expected points per possession, expressed per 100 possessions.
4. **Hustle proxy:** deflections and loose balls per defensive possession, offensive rebounds per rebound opportunity, and contested shots per opponent field-goal attempt. The helper compares each rate with that team's historical baseline, clips extreme early readings, and shrinks small samples toward neutral.

“Hustle index” is only a proxy for observable activity. It does not measure motivation or energy directly. Its equal feature weights and shrinkage prior are starting choices, not proven optimal weights. Baselines must be calculated from games available **before** the current snapshot. The win model learns whether these features help predict outcomes; do not assume hustle improves predictions until the held-out results show it.

## What it does today

- `energy.py`: calculates an opportunity-adjusted, shrunk hustle proxy.
- `model.py`: builds a fixed set of live features and loads an exported logistic-regression model.
- `train.py`: validates a prepared CSV, trains on earlier dates, and reports metrics on later dates. All snapshots on a date remain in one partition, and training weights prevent games with more snapshots from dominating the fit.
- `signals.py`: compares the model probability with a supplied executable ask and includes estimated entry and exit fees, spread limits, a minimum edge, and a maximum paper loss.
- `engine.py`: combines the probability model and signal rules. It makes no network calls and places no orders.

The training output is a win-probability evaluation, **not** proof of trading profit. A trading backtest also needs historical Kalshi quotes/order-book depth, fills, fees, and realistic exits. No live NBA data provider is included. Live NBA stats may be delayed, unavailable for some fields, or licensed; verify that the chosen source supplies each feature at the timestamp the bot would have acted.

## Setup

Python 3.10 or newer is recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Training data

Prepare a CSV with these columns. Use one row per game snapshot and a stable unique `snapshot_id`; a game can have many snapshots. Only include data that would have been available at that snapshot time.

| Column | Meaning |
| --- | --- |
| `snapshot_id` | Unique ID for this observation |
| `game_id` | Stable NBA game ID; repeated for snapshots from the same game |
| `game_date` | Game date as `YYYY-MM-DD` |
| `home_win` | Final result, `1` for home win, `0` for home loss |
| `score_margin` | Home score minus away score at the snapshot |
| `pregame_expected_margin` | Pregame expected home point margin, frozen before tipoff |
| `seconds_remaining` | Regulation game seconds remaining |
| `home_offense_residual_per_100` | Home live points per possession minus its pregame expected points per possession, times 100 |
| `away_offense_residual_per_100` | Away live points per possession minus its pregame expected points per possession, times 100 |
| `hustle_index_difference` | Home hustle index minus away hustle index, using prior-game baselines only |

Use consistent decision times or include a representative range of live snapshots. Do not feed the final score, postgame stats, or any feature updated after the snapshot timestamp. Model quality depends on the quality and timestamp accuracy of this dataset.

Train and create a chronological holdout report:

```powershell
python -m nba_kalshi.train path\to\snapshots.csv
```

By default this writes `artifacts/nba_win_model.json` and `artifacts/holdout_metrics.json`. The latest portion of the date range is held out for evaluation; after reporting those metrics, the exported model is refit on all supplied dates for current paper inference. Review Brier score and log loss against the constant-rate baseline before using the artifact even for paper signals. The current script does not tune thresholds, estimate Kalshi profitability, or certify calibration.

## Producing a paper signal

Call `PaperTradingEngine.assess(snapshot, quote)` from a data adapter. The caller must align the NBA snapshot and Kalshi quote to the same time. `MarketQuote` prices and fee estimates are dollars per contract in `[0, 1]`; it also needs best bid/ask quantities. `SignalRules` requires you to explicitly set a minimum edge, maximum loss, and contract limit. The signal compares the outcome probability with the ask, subtracts estimated round-trip fees, rejects wide spreads or empty top-of-book levels, and sizes a paper position under the configured loss cap. A positive paper signal is not an order or a recommendation.

Kalshi fees vary by market and order type; use the current market-specific schedule in data collection and backtesting. A limit order may not fill, and a market order may execute at multiple prices. See [Kalshi fee details](https://help.kalshi.com/en/articles/13823805-fees) and [order details](https://help.kalshi.com/en/articles/13823810-quick-orders).

## Before live trading

This repo is not ready to trade real money. The next required work is to obtain timestamped historical NBA inputs and Kalshi market quotes, build the point-in-time feature pipeline, evaluate predictions and net fills out of sample, then paper trade against live feeds while logging every input and decision. Only after that would a separately reviewed execution adapter be appropriate, with explicit user approval, API key security, order reconciliation, position limits, loss limits, stale-feed checks, and a kill switch. Stop-loss and take-profit thresholds should come from validated strategy behavior and executable prices, not arbitrary percentages.
