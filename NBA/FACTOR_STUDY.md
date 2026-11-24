# NBA factor study — November 21, 2026

Current model: `9b9bab79a9b0cc5f`. Original 35-input artifacts are preserved as `model-v1.pkl` and `model-v1-report.json`. Full machine-readable results: `NBA/.data/optimization-report.json` and `GET /api/nba/optimization`. The dashboard includes **Factor experiments**, the input catalog and per-game rotation details.

## Added context

The model schema now has 63 columns: 35 team/schedule columns plus 28 context columns across travel, rotation/impact, availability and referee families. Twelve total families are tested. Having a column in the schema does not imply a nonzero learned coefficient.

| Factor | Construction | Practical limit |
| --- | --- | --- |
| Injuries | Timestamped public ESPN injury reports. Out players are removed, with minutes redistributed. Questionable players keep baseline minutes and receive conditional out scenarios. | No archived historical report games in the evaluation set. Dedicated coefficients are unvalidated; availability can change learned rotation inputs. Unreported does not mean healthy. |
| Confirmed starters | ESPN pregame summary, five unique players explicitly marked as starters, received before tipoff. | The source may not announce starters. Actual final starters are never treated as advance knowledge. No historical confirmed-lineup report games in this run. |
| Projected minutes | Last five available team boxes within 90 days; zero minutes for absences, trade-owner filtering, total 240, individual cap 48. Announced starters can use prior starter-role minutes. | A history-based projection; no coach/private rotation knowledge. Missing minutes/history are flagged. |
| Player impact | Ridge game-margin regression on player minute shares, prior three seasons only, home advantage removed. Projected minutes combine these coefficients into rotation/starter/bench/top-player features. | A regularized box-score proxy, not possession RAPM or a causal estimate. Unseen players have unknown impact and an explicit unknown-minute share. |
| Travel | Haversine distance between successive scheduled venue cities and DST-aware timezone change. | Schedule proxy, not actual flights, routing, hotel stays or internal travel plans. |
| Referees | Official current assignments; earlier crew home-margin/foul/FTA tendencies with shrinkage. | Historical final crews do not prove the assignment was known before tipoff. That reconstruction is a separate research trial, ineligible for promotion. Active referee emphasis is zero. |

Data: [SportsDataverse player box scores](https://github.com/sportsdataverse/hoopR-nba-data/blob/main/docs/datasets/player_box.md), [SportsDataverse releases](https://github.com/sportsdataverse/hoopR-nba-data/releases), [ESPN injury feed](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries), [NBA referee assignments](https://official.nba.com/referee-assignments/) and [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api), using GeoNames location data. Download manifests retain URLs, receipt times and hashes. Player history reconciled for **10,980 of 10,984** accepted team games. All 40 used venue cities were geocoded in this run.

## Search and evaluation

43 configurations were tested, including the 35-column baseline, expanded context, regularization choices, family exclusions, half/double emphasis, combined emphasis, and one ineligible referee sensitivity. Emphasis is applied **after** standardization; applying it before standardization would cancel the weighting. Regression learns individual coefficients within the allowed families.

Each validation fold fits on older seasons, calibrates on the immediately preceding season and scores the next season. Labels crossing block boundaries are purged. Validation seasons are 2022–23, 2023–24 and 2024–25, totaling 3,691 scored games. Selection uses only these validation results. The 2025–26 season is a later evaluation; the 2026–27 current-season view is another frozen-checkpoint diagnostic. Earlier broad historical/current-season results were already reviewed during development, so neither is presented as a new untouched prospective test. New timestamped prospective evidence starts now.

The fair-price candidate minimizes validation log loss, which evaluates the actual probabilities. The accuracy candidate maximizes correct winner picks, with log loss breaking ties. A bounded search does not prove a globally optimal set of weights.

| 2025–26 later test, 1,230 games | Correct picks | Accuracy | Log loss ↓ | Brier ↓ |
| --- | ---: | ---: | ---: | ---: |
| Matched 35-input baseline, C=0.1 | 836 | 67.97% | 0.60480 | 0.20869 |
| Selected fair-price configuration | 836 | 67.97% | 0.59794 | 0.20568 |
| Highest validation-accuracy configuration | 837 | 68.05% | 0.60159 | 0.20748 |

The fair-price model improved probability quality; its winner accuracy did not improve on this later season. The accuracy-selected model got one additional game right. The paired day-block bootstrap estimates log-loss improvement 0.00687, with a 95% interval [0.00080, 0.01249]. Same-day dependence is grouped; cross-day/team dependence and repeated research choices remain limitations.

The already reviewed current 2026–27 season has 235 scored games: selected model **158/235 (67.23%)**, matched baseline **155/235 (65.96%)**. These frozen historical forecasts differ from the later deployment refit, whose fit/calibration dates are recorded in the model report. They do not measure betting profit or a price advantage.

## Selected families and interpretation

Fair-price winner: **combined emphasis, C=1.0**. Active family emphasis: strength 1, efficiency 1, turnovers 1, schedule 1, travel 1, rotation/impact 1, availability 1. Direct shooting, rebounding, free-throw and pace/history columns are excluded; those signals may still overlap with efficiency. Referee emphasis is zero. Availability's dedicated coefficients are zero because no historical observations were available.

The largest absolute standardized-coefficient shares are rotation/impact (53.6%), efficiency (19.4%), schedule (11.2%) and strength (10.8%). These describe this model's reliance, not causal percentages or transferable manual weights. Monthly grouped permutation checks also show efficiency, rotation and strength matter most. Travel's test shuffle result is negative: its contribution was not uniformly beneficial. That later test result did not reselect the model.

The UI shows all family emphasis values, coefficient shares, grouped permutation effects, validation removal effects, all trial scores and the largest coefficients. Family-removal effects compare with expanded C=0.1, not with the final combined configuration. Correlated features prevent interpreting one score as an isolated causal contribution.

## Reproduction

From the workspace root:

```sh
NBA/.venv/bin/python -m pip install -r NBA/requirements-lock.txt
NBA/.venv/bin/python -m NBA context
NBA/.venv/bin/python -m NBA optimize --no-promote
```

Omit `--no-promote` to save the selected model and its evaluation forecasts. The worker reloads models each scan. New source releases and accumulating snapshots can change a future run; consult the saved source hashes and dataset fingerprint. Repeating the search on an inspected test set does not create a fresh holdout.
