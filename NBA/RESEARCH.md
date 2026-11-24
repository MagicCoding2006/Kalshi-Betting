# NBA pregame pricing specification

The objective is to estimate `P(team wins the full game | information available before tipoff)`, calibrate that estimate, express it as the value of a $1 binary contract, and compare it with the cost actually available in the market. “Fair” always means a model estimate with a stated information cutoff.

This design follows the [World Cup Market Edge project](https://github.com/MagicCoding2006/World-Cup-Market-Edge), especially its outcome model, real-data backtest, independent sentiment overlay, and public market scanner. The NBA implementation starts with full-game winner contracts. NBA games have no final draw after overtime; soccer's draw class and “not to win captures the draw” logic therefore disappear.

## Translation from soccer

| World Cup input or component | NBA equivalent | Treatment |
| --- | --- | --- |
| FIFA/international Elo strength | NBA team Elo, opponent-adjusted net rating, roster strength | Elo implemented; adjustment/rosters next |
| Goals, xG and shot quality | Points per 100 possessions, shot-location quality, expected eFG | Efficiency implemented; shot quality next |
| Attacking/defensive form | Rolling offensive/defensive rating and four factors | Implemented |
| Player/squad value | Projected minutes × player impact and replacement quality | High-priority extension; salary is not impact |
| Injuries and suspensions | Injury status, rest decisions, minutes limits and starter availability | Highest-priority extension |
| Venue/home advantage | Actual home/neutral court, home familiarity and altitude | Home/neutral implemented; altitude/travel next |
| Rest days and fixture congestion | Back-to-backs, three-in-four, four-in-six, road trips | Rest/B2B/three-in-four implemented |
| International travel/base camps | Previous arena → current arena distance, direction and time-zone change | Requires actual itinerary/venue history |
| Temperature/humidity | Mostly indoor; prioritize altitude, travel and sleep disruption | Outdoor weather omitted from outcome model |
| Tournament/group/elimination state | Regular season, playoffs, series state, seeding incentives and rotations | V1 regular season only |
| Win/draw/loss probabilities | Home/away win including overtime | Two probabilities sum to one |
| Monte Carlo of soccer results | Correlated possessions/score simulation with overtime | Needed for coherent spreads/totals later |
| Kalshi/Polymarket comparison | Exact NBA outcome/settlement mapping, executable asks and fees | Kalshi winner markets implemented |
| Sentiment/hype gap | Public-team/star attention, news intensity and possible crowd bias | Keep outside independent fair model initially |
| Convergence/OU price path | NBA pregame quote dynamics with injury/news jumps | Separate experiment requiring timestamped books |

The original repository can fall back to synthetic development data. This implementation instead returns unavailable/WAIT if actual data or a trained artifact is missing. Synthetic data are useful for software tests, never evidence of predictive performance.

## Metrics to collect

“Implemented” below refers to actual model inputs. Other rows are an explicit backlog, not claims that those signals have already been measured or proven useful. Use only values whose `published_at` and `observed_at` precede the prediction cutoff. Keep player/team identifiers stable across trades and name changes.

| Category | Important metrics | Why it may matter | Status / source |
| --- | --- | --- | --- |
| Team strength | Elo, opponent-adjusted offensive rating, defensive rating, net rating, schedule strength | Persistent ability and quality of opposition | Elo implemented; adjusted ratings from historical possessions/results next |
| Offense/defense | ORtg/DRtg per 100 possessions, half-court efficiency, transition efficiency, garbage-time-filtered ratings | Separates scoring ability from tempo and blowouts | Box-score ORtg/DRtg implemented; possession versions via pbpstats |
| Shooting factor | eFG%, opponent eFG%, rim/paint/midrange/corner-three/above-break-three frequency and accuracy | Shot quality and defensive fit | eFG and opponent eFG implemented; locations via NBA/PBP data |
| Three-point variance | 3PA/FGA, 3P%, opponent 3PA share, assisted/unassisted threes, catch-and-shoot/pull-up mix | High-volume threes change variance as well as mean | 3PA share implemented; regress noisy accuracy toward longer history |
| Turnover factor | TOV/possessions, opponent TOV/possessions, live-ball turnovers, ball-handler turnover rate | Lost possessions and transition chances | Team/opponent TOV rates implemented |
| Rebounding factor | OREB/(OREB+opponent DREB), opponent OREB%, contested rebound chances, lineup size | Extra possessions, second chances and matchup size | Team/opponent OREB share implemented |
| Free-throw factor | FTA/FGA, opponent FTA/FGA, FT%, foul-drawing rates, foul-prone defenders | Efficient scoring and late-game behavior | FTA/FGA on both sides implemented; FT accuracy/player roles next |
| Tempo | Possessions per 48 minutes, offensive possession length, transition frequency, pace under each rotation | Especially important for totals and variance | Estimated possessions per game implemented, explicitly not pace per 48 |
| Availability | Out/doubtful/questionable/probable, official update time, rest designation, illness, minutes restriction, return from injury | A missing creator/defender can change team ability sharply | Official injury reports and timestamped team announcements; not yet ingested |
| Projected rotation | Expected minutes per player, starting five, bench allocation, active roster, starter confirmation | Impact depends on who actually plays and who replaces them | Need a pregame minutes model and archived lineup snapshots |
| Player value | Offensive/defensive RAPM or another validated impact estimate, uncertainty, usage, efficiency, creation burden | Converts availability and roster changes into basketball strength | Fit shrinkage impact from PBP; do not add multiple correlated impact scores blindly |
| Lineup interaction | Regularized lineup net rating, continuity, shared minutes, spacing, rim protection, primary matchup defenders | Five-player units can differ from the sum of individuals | PBP lineup reconstruction; heavily shrink small samples |
| Rest/workload | Days off, B2B, three-in-four, four-in-six, recent minutes, overtime burden, road-trip game number | Fatigue and rotation decisions | Days off/B2B/three-in-four implemented; player workloads next |
| Travel/circadian | Actual arena-to-arena distance, time-zone jumps, east/west direction, local tip time, home/road transition, altitude exposure | Schedule-related effects | Derive from versioned schedules and arena coordinates; no invented itinerary |
| Recent form | Last 5/10/20/30 efficiency, exponentially weighted trends, season/prior-season strength, quality of recent opponents | Detects changing ability while limiting small-sample noise | Shrunk 10/30-game windows implemented; controlled alternatives to test |
| Venue | Home/neutral, actual arena, altitude, international/temporary venue, unusual start time | Home advantage varies by setting | Home/neutral implemented; broader venue features next |
| Matchup style | Rim pressure vs rim protection, offensive boards vs defensive boards, transition vs transition defense, P&R coverages, switchability | Conditional advantages beyond generic team strength | Test interactions after lineup/shot data exist |
| Coaching/roster changes | Trades, new coach, rotation changes, continuity, new-player minutes | Historical averages can become stale overnight | Versioned transactions and lineup history; not v1 |
| Officials | Crew assignment time, foul/FTA tendencies adjusted for teams, sample size | Possible small context signal | Low priority; assignments must be known before prediction; avoid raw referee W/L narratives |
| Competition context | Regular season/playoffs, elimination/series state, Cup games, end-of-season availability | Changes effort and rotation distribution | Separate validation regimes; do not hand-code “must win” boosts |
| Market benchmark | Timestamped moneylines/spreads/totals, book dispersion, opening/closing lines and overround | Strong benchmark; useful separate market-informed model | Store at the same forecast horizon; no sportsbook feed in v1 |
| Execution | YES/NO bid/ask, depth, spread, quote age, fees, market status, contract wording | Determines whether an apparent gap can be purchased | Kalshi implemented |
| Attention/news | News publication time, injury-news novelty, search interest, social attention | May explain crowd pricing or impending updates | Independent overlay until incremental predictive value is demonstrated |

NBA's [official glossary](https://www.nba.com/stats/help/glossary) defines the advanced-stat vocabulary. The formulas used by v1 are explicit and may differ from provider possession-count conventions:

```text
estimated game possessions = 0.5 × Σteams(FGA + 0.44 × FTA − OREB + TOV)
ORtg = 100 × points / estimated possessions
DRtg = 100 × opponent points / estimated possessions
eFG% = (FGM + 0.5 × 3PM) / FGA
TOV rate = TOV / estimated possessions
OREB% = OREB / (OREB + opponent DREB)
free-throw rate = FTA / FGA
three-point attempt rate = 3PA / FGA
```

V1 averages these per-game metrics over the last 10 and 30 available games, with five fixed neutral prior pseudo-games. Windows may carry previous-season games within 365 days; Elo regresses 25% toward 1500 at a season transition. Elo uses K=20 and a 65-point home allowance. These are documented baseline choices to challenge in validation, not optimized facts. The model sees home-minus-away features, both B2B/three-in-four indicators, home/neutral status, sample sizes and mean recent possessions. It does **not** claim that rolling ratings are schedule-adjusted.

## Open-source and public data

| Source | Useful contents | Access and practical limits |
| --- | --- | --- |
| [SportsDataverse NBA pipeline](https://github.com/sportsdataverse/hoopR-nba-data) and [release assets](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_team_boxscores) | Historical team/player box scores, schedules, rosters, shots, officials and crosswalks | Public files generated from ESPN; implemented team/schedule adapter. Check each season's coverage and release freshness; missing games are possible |
| [SportsDataverse schedules](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_schedules) | Start time, teams, venue, home/away, neutral flag, final/pregame state | Implemented. Schedule records also contain current records and scores; those fields never enter pregame features |
| [nba_api](https://github.com/swar/nba_api), especially [LeagueGameLog](https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/leaguegamelog.md) | Official NBA team/player game logs, advanced box scores, lineups and shot data | Open-source client, NBA-hosted data. Some NBA endpoints block requests/time out; client maintenance does not guarantee endpoint access. An alternative adapter is future work |
| [NBA official injury report](https://www.nba.com/players/injury-report) | Official player status and reasons | Required next input. Preserve each published revision and local receipt time; final game participation is not a valid pregame injury label |
| [pbpstats](https://pbpstats.readthedocs.io/en/latest/) | Possessions, on-floor lineups, score context, shot zones and event-order corrections | Open-source parser; upstream PBP availability/quality still matters. Use for garbage-time filtering, rotations and regularized player/lineup impact |
| [Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data) and [order books](https://docs.kalshi.com/api-reference/market/get-market-orderbook) | Contract rules, executable bids/asks, depth, timestamps where supplied and market metadata | Implemented without account keys. Receipt times do not establish exchange freshness. Historical candles do not recreate executable book depth |
| [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) and series metadata | Taker/maker formulas and series multipliers | V1 supports the recognized quadratic taker schedule and exposes conservative rounding assumptions |

Open-source software and publicly downloadable sports statistics are different things. Keep original source attribution and inspect dataset terms before redistributing raw records. The API exposes derived research estimates; no private account data is needed.

## Injury and rotation model: the most important next extension

1. Archive official injury revisions and lineup announcements with source time and receipt time. Fix forecast horizons such as the prior evening, T−60 minutes and T−10 minutes. Late news is usable only for later forecasts.
2. Predict a minutes distribution for every possible active player using past rotation, role, return-from-injury workload and known status. “Questionable” is not automatically a 50% play probability; estimate availability from archived statuses if enough examples exist.
3. Estimate offensive/defensive player impact with regularization and uncertainty. Project substitutions and replacement minutes, not just the absent player's points per game. Avoid double-counting an absence already present throughout the rolling team baseline.
4. Create coherent joint lineup scenarios. Correlated rest decisions and replacement rotations cannot be treated as independent coin flips. Compute a weighted mixture of scenario win probabilities.
5. Retrain and compare with the team-only baseline at identical horizons. Check performance separately with confirmed/uncertain lineups and after trades. Until these data exist, player absence stays a missing input rather than an arbitrary adjustment.

The mixture is `P(home wins) = Σscenario P(scenario | pregame information) × P(home wins | scenario)`. Its uncertainty comes from lineup probabilities, player impact and game variance; raw disagreement between two classifiers is not a calibrated probability interval.

## Model and evaluation ladder

1. **Baselines:** constant historical home win rate and past-only Elo with home advantage.
2. **Implemented team model:** regularized logistic regression and shallow histogram gradient boosting; ensemble weight chosen on later validation data; sigmoid calibration fitted on a separate subsequent block. Early stopping with a random internal split is disabled.
3. **Player-aware model:** add projected rotation/impact distributions, then matchup, workload and travel features. Add a family only if it improves repeated out-of-time tests and calibration at the target forecast horizon.
4. **Margin/total model:** predict a joint distribution of scoring margin and total, with a shared pace/possession process, correlated scoring, three-point variance, late-game fouling and overtime. Validate tails and discrete pushes before mapping to spreads/totals. An independent Poisson goal model is not an appropriate automatic port from soccer.
5. **Optional market-informed estimate:** separately benchmark a no-vig sportsbook consensus or market-anchored model. Label it as market-informed. It must not be presented as an independent signal that then “discovers” the same market's mispricing.

V1 partitions unique calendar dates approximately 60/15/10/15 into fit, ensemble selection, calibration and test. Entire dates stay together. Features may update with earlier outcomes during the test, as they would in live operation, while estimator weights and calibration stay frozen. No test results select the winner or change parameters. A subsequent serving refit is reported separately.

Evaluate **log loss and Brier score** first; also report reliability bins, bin sample sizes/Wilson frequency intervals, ECE and accuracy. Compare paired losses with baselines; day-block bootstrap intervals retain same-day groups, although longer team/time dependence can remain. Evaluate multiple rolling seasons and horizons before model promotion. Keep a final untouched period for any subsequent model development; repeated tuning against this first holdout would consume it.

For a market benchmark, remove bookmaker overround consistently: for a two-way decimal-odds pair, normalize `(1/odds_home, 1/odds_away)` to sum to one. Record the original prices and normalization method. A Kalshi midpoint is a descriptive quote, not an executable price or guaranteed unbiased probability.

Outcome accuracy alone does not establish profitable mispricing. A later market replay must use the **quote actually observed at each forecast**, depth/size, fees, spread, latency/slippage, contract settlement/cancellation rules, and no selection on future price movement. Measure calibration by edge bucket, net EV realized, drawdown, turnover and closing-line value at a fixed horizon. Log all forecasts and no-trade decisions, not just favorable examples. This implementation starts collecting the required quote evidence now.

## Exposure contract and production sequence

V1 exposes fixture identity, tipoff, information cutoff, model/source identity, input coverage, probabilities, estimated fair prices, executable-cost comparisons and explicit WAIT reasons. Read-only endpoints are in [README.md](README.md). No position-sizing logic is needed for this scope.

The immediate next priorities are injury/minutes snapshots, recurring source coverage checks, and prospective paper evaluation at fixed pregame horizons. After those pass, add player-aware models and repeated seasonal tests; extend to spreads/totals once the score distribution is validated. Cross-venue arbitrage and stochastic convergence are independent later projects: identical settlement, available liquidity, actual fees and news-driven jumps must be modeled before making either claim.
