# First NBA validation run

Run date: November 21, 2026 (America/Indiana/Indianapolis). This records the initial implementation, before adding injury or rotation inputs.

Model ID: `64deb8092bfa2aab`. Dataset fingerprint: `7dec15e51a930c07b01cc637eff124c46769075ed04765172bfa04a9ffd003de`.

## Data and chronology

Accepted 10,984 completed games from publicly downloadable [SportsDataverse team box scores](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_team_boxscores) with corresponding [schedules](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_schedules). After the ten-game/team history minimum, 10,672 games were eligible for features.

The source contains 1,230 games in 2017–18 with missing total turnover counts. The entire season was excluded. Other recorded exclusions include non-NBA All-Star team rows and one box score whose points did not reconcile. Exclusion records can refer to a team row or a game; they are not all distinct games. Source season-type 2 defines this dataset; Cup scheduling conventions can differ from official regular-season standings counts.

| Block | Games | From | Through |
| --- | ---: | --- | --- |
| fit | 6,360 | 2016-11-14 | 2023-02-10 |
| selection | 1,629 | 2023-02-11 | 2024-10-23 |
| calibration | 1,061 | 2024-10-24 | 2025-03-24 |
| test | 1,622 | 2025-03-25 | 2026-11-20 |

Whole calendar dates stay in one block. Boundary label-availability purging is implemented; this particular split required zero purges. Every historical feature uses only earlier results available under the next-day-noon UTC convention. Historical publication timestamps are assumed, and upstream stat revisions remain a limitation.

## Frozen held-out results

| Predictor | Log loss ↓ | Brier ↓ | Winner accuracy | ECE ↓ |
| --- | ---: | ---: | ---: | ---: |
| Calibrated selected ML model | 0.603782 | 0.208346 | 68.00% | 0.0255 |
| Past-only Elo | 0.604813 | 0.208434 | 68.00% | 0.0396 |
| Constant training home-win rate | 0.691151 | 0.248995 | 54.07% | 0.0255 |

The ensemble selection chose logistic weight 1.00: the calibrated logistic model, with gradient boosting retained as a comparison component. The held-out sample contains 1,622 games.

The mean log-loss improvement versus Elo was 0.001032, with a paired day-block bootstrap 95% interval of [-0.006279, 0.008308]. That interval includes zero. This run does **not** establish superiority over Elo. It does beat the constant home-win baseline on this sample. Day blocks do not eliminate longer-term team/time dependence.

No historical market-quote replay was available. Betting profitability, closing-line value and an advantage over Kalshi/sportsbook prices have not been established. The test is one chronological holdout; further model development must reserve a new untouched test period.

## Serving refit and live wiring

The deployment estimator was refit on 10,204 games through 2026-03-13; a disjoint 468-game block through 2026-11-20 calibrated it. The held-out numbers above describe the earlier frozen evaluation model, not this serving refit.

A public Kalshi scan matched 28 winner contracts across 14 fixtures in the next 48 hours. 9 fixtures had sufficient available history to produce a research estimate; the rest waited for preceding game results. All three NBA HTTP endpoints returned 200 in an isolated local server smoke test. These counts describe a dated scan, not persistent live coverage.

Snapshot staleness, game-start cutoffs, changed-model invalidation, missing book depth, crossed books, unsupported fees, exact date/team mapping, corrupt input rejection and future-result leakage have focused tests. Existing BTC/Gold suites also pass. No running portfolio server was restarted for this verification.

## Reproduce and inspect

Use `NBA/.venv/bin/python -m NBA train` with the cached sources. Exact package versions are in [requirements-lock.txt](requirements-lock.txt). Full precision scores, bin sample sizes, frequency intervals, source hashes and dependency versions are saved locally in `.data/model-report.json`; once-held-out probabilities are in `.data/holdout-predictions.json`. The raw quote/rule snapshots are in `.data/snapshots/`.

The next high-value extension is timestamped injury/rotation data, followed by repeated seasonal and prospective market evaluation. See [RESEARCH.md](RESEARCH.md).
