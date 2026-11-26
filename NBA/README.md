# NBA Market Edge

Pregame estimates of NBA full-game win probabilities, expressed as fair YES/NO prices, alongside public Kalshi order books. This is a working research service in the existing Signal Desk workspace, with manual risk planning and a local fill journal. It never places exchange orders.

The [research specification](RESEARCH.md) maps your World Cup project to basketball. [FACTOR_STUDY.md](FACTOR_STUDY.md) records the current context model, factor experiments and evidence limits. [VALIDATION.md](VALIDATION.md) preserves the earlier 35-input model's first evaluation.

## Run

From the workspace root, using Python 3.10+:

```sh
python3 -m venv NBA/.venv
source NBA/.venv/bin/activate
python3 -m pip install -r NBA/requirements.txt
python3 -m NBA refresh --start 2017 --end 2027
python3 -m NBA context
python3 -m NBA optimize
python3 -m NBA scan
python3 server.py
```

An isolated NBA environment is already prepared locally at `NBA/.venv`. Activate it for ML commands. The desk API itself does not need it. [requirements-lock.txt](requirements-lock.txt) records the exact dependency versions used for the checked model; install that file when reproducing this run. Serialized scikit-learn models require the same scikit-learn version; otherwise retrain in the new environment.

Years are **season end years**: `2027` means 2026–27. Fetches use the maintained SportsDataverse releases, not the archived hoopR-data repository. Downloads are cached in `NBA/.data/raw/`; every source has its URL, retrieval time, size and SHA-256. Past seasons reuse verified cached files. To redownload a historical season, remove its `.source.json` file and run `refresh` again. The default range is 2017 through the current season.

For continuing snapshots, run this in a separate terminal alongside Signal Desk:

```sh
python3 -m NBA watch --interval 30 --refresh-hours 6
```

The updater checks availability independently every 30 seconds and targets a prediction/market scan every 30 seconds (actual scans can take longer). Team/player-history ingestion runs separately when the dataset is older than six hours. Source failures, including incomplete chunked HTTP responses, are isolated and retried; worker health is written to `watcher-status.json`. The promoted model is reloaded on every scan, but no weights are automatically retrained or promoted. Rerun `context` and `optimize` explicitly after inspecting coverage and validation, at least within 30 days. `train` remains available for the original baseline; `optimize --no-promote` leaves the serving model unchanged.

On this Mac the NBA updater is installed as a user-level launch agent, starts at login, and restarts after a crash. To install or update that configuration after preparing the NBA environment/model:

```sh
NBA/.venv/bin/python -m NBA service-install
```

The agent is `~/Library/LaunchAgents/com.signaldesk.nba.watch.plist`, with output in `~/Library/Logs/SignalDesk/nba-updater.log`. It uses the prepared NBA venv, a home working directory and an explicit module path; it does not move source files or data. `launchctl print gui/$(id -u)/com.signaldesk.nba.watch` shows its state. A process lock prevents duplicate updaters. The browser and server do not need to remain open for this worker, but the Mac must be awake and connected. The recorder has its own supervised service described below. The paper worker is launched separately. These are local services and require an awake, connected Mac.

The dashboard's **Automatic updates & availability** panel shows background-worker health, scan/availability times, source failures and recent participation-status changes. Each game shows important listed absences and the source report. Known out players receive zero projected minutes. Official NBA injury PDFs are parsed by game date, matchup, team and player; generic ESPN fantasy flags cannot override a current official game report. A newer ESPN comment explicitly referring to that game's day can flag a pending change before the next PDF: it is treated as unresolved and blocks entries while awaiting confirmation. A fresh timestamp on a comment about yesterday's game does not establish today's status. Injury reasons/severity are not inferred from wrapped PDF text. Original PDFs, their hashes, timestamps and parsed observations are retained locally.

New live entries are blocked when official verification is missing/not submitted, the source check is older than five minutes (two minutes within 90 minutes of tipoff), the official publication is older than an hour, important-player status is uncertain (24+ baseline minutes), sources conflict, baseline absences total 60+ minutes for a team, rotation/impact coverage is incomplete, or lineups remain unconfirmed within 30 minutes of tipoff. A changed participation/lineup fingerprint invalidates old entry signals immediately, before the next scan completes. Prediction snapshots expire after three minutes and quotes after 90 seconds. Stale or rebuilding workers/context cannot fund new entries. These are conservative operating policies, not fitted confidence thresholds or proof of profitability.

Model/market midpoint disagreement over 15 percentage points triggers review instead of becoming an automatically attractive edge. The sizing limit can be tightened further. Fair estimates remain visible for inspection, but blocked edges and allocations are suppressed. Availability changes observed after a manually recorded fill appear in its tracker; this does not place or close orders, and pregame reporting does not guarantee instant in-game injury detection. See [the November 22 audit](research/AVAILABILITY_AUDIT_2026-11-22.md) for the SGA timeline and evidence limits.

Open the [NBA dashboard](http://127.0.0.1:8787/nba) for game cards, fair prices, Kalshi purchase costs, net edges, team profiles and model evidence. The default **Today · all games** retains started games. Choose tomorrow, the next seven days, any season date, or the full season schedule. Full-season results load forty cards at a time with an explicit Show more button. Search and filters apply to the entire selected slate. The page reloads the saved scan every five seconds; keep the watcher running to fetch new quotes.

Started games show an archived pregame forecast. When no forecast was issued, a replay is permitted only if the model's labels predate tipoff, and is labeled **Pregame replay**. Completed historical games can use the saved frozen test checkpoint, labeled as a historical replay. These values never become in-play probabilities or live edges. Future games awaiting intervening results, and games beyond the 48-hour pricing horizon, get **provisional** latest-history estimates with scheduled rest; unknown future performance is not invented. Executable edges remain gated on complete current inputs.

The shared server also exposes:

| Endpoint | Response |
| --- | --- |
| `GET http://127.0.0.1:8787/api/nba/fair-prices` | Upcoming fixtures, estimated home/away probabilities, fair contract prices, market costs and research edges |
| `GET http://127.0.0.1:8787/api/nba` | Same response |
| `GET http://127.0.0.1:8787/api/nba/model` | Full evaluation report, dates, calibration bins, baselines, dataset identity and limitations |
| `GET http://127.0.0.1:8787/api/nba/features` | Exact implemented feature names, priors, windows and planned inputs |
| `GET http://127.0.0.1:8787/api/nba/backtest` | Current-season probability buckets and real game results from the frozen historical test checkpoint |
| `GET http://127.0.0.1:8787/api/nba/optimization` | Factor weights, validation trials, later test results, coefficients and evidence coverage |
| `GET http://127.0.0.1:8787/api/nba/recordings` | Local collector health, storage counts, current scores and last recorded books |
| `GET http://127.0.0.1:8787/api/nba/paper` | Fixed paper exit-policy comparison and matched-cohort results |
| `POST http://127.0.0.1:8787/api/nba/risk` | Read-only sizing calculation from manual settings/positions and recorded public books; no orders or account mutations |

These GETs and the risk calculation read local data; they never fetch, train, trade or open a portfolio. Start the scanner/watcher to update prices. Restart an already-running Signal Desk process once to load new routes. A worker can use `python3 -m NBA --data-dir /path/to/research ...` for isolated experiments; the shared API always reads the default `NBA/.data` directory.

## Scalp Kelly and manual risk management

The top **Scalp Kelly & exit plans** panel now defaults to scalp target/stop sizing, with full Kelly shown as a theoretical calculation. Earlier quarter-Kelly / 2%-per-game / 5%-total defaults are migrated out of scalp mode while retaining manual bankroll values and tracked fills. Capital caps default to 100% and remain editable; available cash, existing exposure, whole-contract depth and the selected quantity ceiling still constrain funded allocations. No leverage or exchange orders are used. The initial $1,000 bankroll is an example.

**Supply a scalp hit-rate assumption.** The full-game winner model does not estimate the probability of hitting a price target before a stop. The input starts unset; cards show an explicitly labeled ten-contract payoff example rather than an invented stake. Enter a global rate, set a rate on an individual game, or deliberately try the **66.7% assumption** example. These are user what-if assumptions, never calibrated model probabilities. Changing stop, target or holding time changes the event whose probability must be estimated. The paper study's matched sample count is exposed as evidence context; its early results are not automatically used as a hit-rate model.

For per-contract target gain `g`, loss at the assumed stop fill `l`, entry cost `c`, hit probability `q` and bankroll `B`:

```text
full-Kelly contracts = B × [q/l − (1−q)/g]
full-Kelly planned-loss fraction = q − (1−q) × l/g
full-Kelly capital fraction = planned-loss fraction × c/l
```

Negative values mean zero Kelly; fees/slippage must be included in both exit payoffs. Capital fraction can theoretically exceed 100% when the assumed loss is much smaller than entry cost. The cash-funded planner cannot borrow. Full scalp sizing maximizes the two-exit expected log-growth objective over available, fundable whole-contract quantities, accounting for depth and quantity-specific conservative fees. Fractional choices additionally apply the local payoff-based fractional-Kelly target. Thus tighter stops can increase scalp size under the stated assumptions; a high game-win probability alone cannot.

The two-exit calculation values every non-target/time-limit outcome at the assumed stop. This is a simplifying scenario, not a validated model of forced exits or price jumps. Cards separately display capital committed, loss if the planned stop fills, profit at target, an extra-stop-gap stress case, full contract-loss exposure, and the Kelly sensitivity if losing exits occur at that stressed bid. The gap scenario is not an assigned probability or an exhaustive worst-case execution model.

The model's current fair-value gap still selects qualifying pregame direction. Its probability buffer applies to that entry filter, not to the supplied scalp hit rate. Opponent YES/NO routes for the same outcome can compete on costs. Started/stale/provisional games do not reuse old pregame probabilities as live entry signals. **Hold to settlement** remains available as an explicitly separate sizing basis using `(p−c)/(1−c)`.

**A cash-funded allocation now requires an explicit maximum loss per game**, independently of Kelly fraction and capital caps. No risk percentage is silently selected. The default loss basis is **full contract loss**: entry cost, estimated fees and the entry slippage allowance must fit `equity × max_loss_fraction`. Tightening a stop cannot expand that full-loss ceiling. A deliberate **stop-gap stress only** selection instead caps the modeled gap loss; full contract exposure can then exceed that ceiling. This is not guaranteed loss protection, account enforcement, or a joint portfolio/daily drawdown limit. The individual stop calculator also rejects quantities that breach a configured portfolio loss ceiling. Existing journal positions are preserved.

Each game's standalone full Kelly is shown before portfolio funding. The basket shares cash proportionally across eligible games and is **not** a joint Kelly optimization with game correlations. Existing tracked cost and other manually entered NBA exposure consume cash/capital limits. A tracked game receives no additional proposed entry. New-entry books expire after 90 seconds, exit marks require a receipt within 30 seconds, and plans expire after 20 seconds without refresh. The legacy quote comparison uses the scanner's fixed quantity; risk panels reprice their own quantities.

Manual settings, game-specific assumptions and fills stay in browser local storage. **Export local journal** saves a JSON backup. Clearing browser storage removes this manual journal, not the market-recording database. Treat open positions at entry cost and reconcile equity/cash with the actual account; BTC/Gold portfolios and the unfunded paper experiment remain separate.

Use **I bought YES / NO on [team]** under a game, or select the exact contract above **My positions**. The purchase form needs only **amount spent including fees** and **entry price** (`0.43`, `43%`, `43¢`, or `43`). It estimates the largest affordable whole-contract quantity including conservative standard taker fees; $5 at 43¢ estimates 11 contracts, $0.19 fees and $4.92 cost, leaving $0.08 unallocated. These are inferred quantities and fees, not an exchange fill import. Entry time defaults to now. **Advanced** lets users correct exact quantity, fees, purchase time, exit levels and holding limit; planner cash is changed only if explicitly selected. Automatic exits use the existing fee-aware stop endpoint and current default target/holding settings. Failed feasibility checks or unavailable calculations visibly label fallback default-distance rules. Stop calculations are refreshed after 20 seconds when saving. Fresh bids, full-position depth and fees drive in-page stop/target/time alerts. **Adjust exit levels** edits local thresholds, with time measured from the recorded fill. **Record an exit / resolution** accepts actual quantity, price and fees; partial exits retain the remaining quantity and its share of entry fees. Local alerts send no exchange orders and do not guarantee stop fills.

## Individual entry-price stop calculator

Every game has **Enter my price · calculate stop loss**, independent of Kelly eligibility. Enter a planned or actual entry price, quantity, target sell bid, and an optional maximum net dollar loss. The default minimum net reward/risk is a user-editable **1:1 planning policy**, not an optimized threshold. Blank entry fees are estimated; explicit zero means zero. Planned entries include the configured entry slippage buffer; actual fills use the entered price and fees without adding entry slippage again. Both include estimated exit fees and slippage.

`POST /api/nba/stop-plan` returns this calculation without opening a trading engine or fetching remote data. It chooses the widest whole-cent stop below entry that satisfies the target's **net profit / net loss** requirement, dollar loss budget, and maximum configured price drop. The minimum distance is the greater of the selected minimum, fresh bid/ask spread, and (when available) the 95th percentile of recent observed adverse bid changes. The latter is a descriptive heuristic over 30 minutes, requiring at least ten adverse changes between adjacent observations no more than 30 seconds apart; it is not a calibrated volatility model, future loss quantile, or stop-hit probability. Live book checks require a receipt within 30 seconds and sufficient full-position entry/exit depth. Missing quotes allow explicitly labeled payoff arithmetic only. Missing fee metadata uses an explicitly labeled multiplier-1 assumption.

The result includes suggested SL/target bids, net loss/profit, fee-adjusted break-even sell bid, two-target-wins/one-stop-loss net P&L, required hit-rate threshold, gap stress and full-loss exposure. If costs or distance/depth constraints make a stop infeasible, it rejects the setup rather than inventing an above-entry loss stop. Changing a stop also changes its hit probability; this calculator does not estimate that probability or establish positive expected returns. Deadlines and partial/no fills can produce other outcomes.

**Save individual exit plan** stores a calculation under that game, not a position or a new live quote. It leaves Kelly settings unchanged. **Record fill with these exits** transfers fields into the existing actual-fill form for review; saving a plan alone never deducts cash. Existing tracked positions have **Calculate SL / protect profit**: applying a calculated plan updates local alerts. When sufficiently higher fresh full-position bids support it, **Protect at net break-even** explicitly raises the local stop to the modeled fee-adjusted break-even bid. Neither action sends an order; gaps, changing costs and execution failure can still cause losses. Plans require recalculation after 20 seconds before applying, with fresh quoted plans also requiring a quote no older than 30 seconds. Saved plans are included in local journal exports.

## Local live recording and paper exits

The dashboard's **Live scores & local recording** panel is separate from pregame fair prices. It records games through live play and final status, even without a matching Kalshi market. All observed NBA-team matchups in the current daily score feed are covered, not only games with a valid prediction. Cached current-season schedules seed game identities. All-Star/TBD/noncanonical teams and invalid score rows are preserved in raw responses and listed as exclusions.

```sh
python3 -m NBA record
```

This separate public-data worker uses the standard library. It targets **10-second polling whenever matching winner markets are open**, while games are live, or within 90 minutes of tipoff. Otherwise it uses the idle interval (15 seconds in the installed service). Network/provider delays and rate-limit backoff can lengthen these intervals. It also checks adjacent dates and keeps polling after midnight while a previous-day game is live. Open market discovery runs every minute; recent closed/settled contracts are refreshed separately. Winner order books cover the preceding/following 48 hours, with up to 100 levels per side. This is sampled polling, not every exchange tick. No account keys or orders are used.

The live store is **`NBA/.data/recordings.sqlite3`**, using WAL mode. It contains game identities, score/clock/period observations, YES/NO bids/asks and depth, contract status/settlement, issued forecasts, source URLs, request/receipt times and failures. Fuller game summaries are also captured every minute during live play and periodically after finals, including box scores and play records when supplied. Full source responses are compressed and deduplicated by SHA-256. Their actual receipt times remain authoritative: a final-game summary is not evidence that all its fields were available before tipoff. Missing possession/foul/bonus/timeout fields remain null in the normalized scoreboard; the last scorer is never assumed to have possession. A quote links only to a score received earlier, with stale/missing score links flagged. HTTP receipt time does not prove upstream freshness.

The collector is installed as the user-level macOS agent `com.signaldesk.nba.record`, starts at login and restarts after a crash. It continues when the dashboard closes while the Mac is awake and connected. Install/update it with `NBA/.venv/bin/python -m NBA record-service-install`; logs are in `~/Library/Logs/SignalDesk/nba-recorder.log`. Duplicate collectors for the same data directory are rejected by a process lock. Ctrl+C stops a foreground worker; `collector.pid` and `collector.log` identify the detached worker. Storage is not automatically deleted, and the dashboard shows database plus journal size.

Export a consistent compressed JSONL snapshot while recording continues:

```sh
python3 -m NBA record-export --output NBA/.data/exports/nba-recording.jsonl.gz
# Optional: --since 2026-11-22T00:00:00+00:00
```

Exports refuse to overwrite an existing file. They preserve normalized observations, errors and original responses, with export-time metadata labeled separately from historical market states. Copying just the SQLite main file while WAL writes are active is not a consistent backup; use the export or SQLite's backup API.

The separate paper study reads this database and never sends orders:

```sh
NBA/.venv/bin/python -m NBA paper-prepare
NBA/.venv/bin/python -m NBA paper --watch
```

`NBA/.data/paper/registration.json` fixes the study start, main model ID and entry parameters. Changing it cannot silently rewrite an existing study. A new data directory needs a registration with the same schema before starting the paper worker. The current study uses one qualifying entry per game, ten contracts, at least 3 percentage points of estimated net edge, entry between 60 and 2 minutes before tipoff, a 1-second minimum execution delay and a 30-second maximum entry-data gap. Candidate routes are compared using only quotes already observed. A later ask must still qualify before a simulated entry occurs.

Six alternatives share each entry: hold to settlement; +10¢ target/−5¢ stop; +5¢ target/−5¢ stop; exit after 20 minutes or before tipoff; updated fair-value exit; and sell half after +10¢ then hold the remainder. Targets/stops are price movement before fees. Non-hold policies have a 90-minute deadline except the partial-exit remainder. Sales execute against a later observed bid/depth with conservative fees and 0.5¢ per-contract slippage, not at an assumed stop or candle extreme. Partial depth can leave a position open. These are simulated market exits after a trigger, not guaranteed resting limit fills.

The quarter model is a **separate research artifact**, fitted on earlier 2024–25 games and calibrated on later 2024–25 games. It applies only at actually observed end-Q1 and halftime breaks with clock zero. It is unavailable after play resumes; pregame estimates never become in-game probabilities. Main model weights are not changed. Actual Kalshi settlement, including a supplied nonbinary settlement value, closes remaining contracts; an ESPN final alone does not establish settlement.

Paper state/fills are durable in `NBA/.data/paper/study.sqlite3`; `status.json` drives the dashboard and the worker log/PID are in the same folder. The matched P&L table waits until **every policy has finished the same entry**. Open positions are not silently dropped to improve results. Empty/early samples do not establish profitability, and polling gaps, latency, actual fill uncertainty and unmodeled live context remain limitations.

## What is implemented

- Real historical regular-season team box scores and schedules, normalized to the same 30 NBA team codes across seasons and providers.
- The initial source has missing `total_turnovers` for the entire 2017–18 season. Those 1,230 games are excluded and the gap is recorded in the model report; no zero-filled turnovers enter training.
- Validation of paired teams, reciprocal opponent IDs, dates, final scores, shooting counts and points reconciliation. Invalid games and unresolved/non-NBA schedule entries are recorded as exclusions. All-Star teams and TBD NBA Cup placeholders are not guessed.
- Rolling 10/30-game offensive/defensive efficiency, offensive and opponent four factors, three-point attempt rate, estimated possessions, Elo, home/neutral court, rest, back-to-backs and three games in four nights. Five prior pseudo-games shrink small samples; at least ten actual games per team are required.
- The original baseline combines regularized logistic regression and histogram gradient boosting. The current 63-column context model searches regularized logistic models, group emphasis and group exclusions on three past validation seasons. A later season evaluates the selected configuration; sigmoid calibration uses a disjoint earlier season. The highest validation-accuracy candidate is reported separately from the fair-price candidate selected by log loss.
- Last-five-game projected rotations, regularized prior-season player-impact proxies, rotation continuity, unknown-player flags, and scheduled venue-city travel/timezone changes.
- Timestamped ESPN injury reports and confirmed starters when actually supplied before tipoff. Reported-out players are removed and minutes redistributed; questionable players retain their baseline projection and receive explicit unavailable scenarios. No final-game participation is backfilled as a historical injury report or confirmed lineup.
- Official live referee assignment collection and past crew tendencies. The historical final-crew reconstruction is an ineligible research sensitivity; referees have zero weight in the promoted selection.
- An explicitly separate deployment refit using more recent data. Its calibration labels are disjoint from its estimator-fitting labels. Reported held-out performance belongs to the earlier frozen evaluation checkpoint, not this refit.
- Public `KXNBAGAME` discovery with pagination, strict date/both-teams/YES-side joins to schedules, and YES/NO executable asks derived from the order book.
- Depth-weighted costs for the requested quantity, the series fee type/multiplier, conservative fee rounding and an explicit slippage allowance.
- Versioned local prediction/quote snapshots, model IDs and source fingerprints. Data under `.data/` remain local and are ignored by version control.

Dedicated injury and confirmed-lineup coefficients have **zero historical report coverage** in this run; their independent predictive value has not been established. Current observations can still change the projected rotation features learned from past player history. Confirmed starters require a genuine pregame source response with five distinct starters. Otherwise the dashboard says projected. Unknown availability, impact and source coverage remain explicit in `missing_inputs`.

Still outside the model: possession-based player impact, shot-location matchups, sportsbook consensus, Polymarket, player props, spreads, totals, arbitrage and convergence trading.

## Meaning of the output

For a contract that pays $1 if the home team wins, a home win estimate of `0.62` implies a fair YES price of **62¢** before costs. The opposite full-game outcome has probability `0.38`. For an away-team YES contract, that mapping reverses. These values are estimates, not an observable “true price.”

The core outcome model uses no market price. Every matched market includes the actual YES team and its primary/secondary settlement rules. Cancellation and fair-price-resolution clauses remain outside the $1/$0 EV calculation.

For each YES/NO side:

```text
all_in_cost = depth_weighted_purchase_cost + conservative_fee_estimate + quantity × slippage_buffer
net_edge = outcome_probability − all_in_cost / quantity
expected_profit_dollars = quantity × outcome_probability − all_in_cost
```

Where uncertain-player scenarios exist, edge and profit use the least favorable supplied probability for that purchase: the YES lower bound or one minus the YES upper bound for NO. The fair-price column remains the baseline estimate and shows the conditional scenario range. These scenarios are sensitivity checks, not confidence intervals, exhaustive roster combinations, or assigned probabilities of a player appearing.

`net_edge = 0.03` means **3 probability points / 3¢ per contract**, not a 3% return on capital. `research_edge` means the model estimate exceeds the requested cost by at least the configured threshold. It does not mean the model has demonstrated an advantage over the market.

Each team's winning outcome has two routes: **YES on that team** and **NO on its opponent**. The dashboard shows the quoted purchase price and net edge for each route separately and marks the lower all-in cost as “Best route.” The two teams' outcomes are evaluated independently, so an edge on either team's YES or NO can appear. Equivalent outcomes share their estimated win probability; their book prices, available depth and fees can differ. Cancellation and settlement-rule qualifications still apply.

## Game coverage and price display

The API reconciles the daily recorded score feed with the season schedule. Newly observed games appear even before the next model scan; they show quotes and a pending prediction rather than disappearing. Source date/tipoff/status changes update the visible board and invalidate an old forecast when its fixture changes. Known team identities and regular-season modeling scope are checked before a new fixture receives a model entry signal. This overlay does not rewrite the historical dataset or import future game results into pregame features.

The coverage line compares today's raw daily scoreboard identities with the board, reports missing/mismatched/unexpected IDs and fresh-quote coverage, and distinguishes a verified zero-game day from an unavailable source. The visible count separately reports the selected slate, filtered-out games, and pagination. **Show all today** and **Show all yesterday** clear search and prediction/edge filters.

Market prices are independent of model eligibility. Each route shows the **best buy ask**, **sell bid**, and local receipt age. The requested-quantity average and estimated all-in cost are separate; a best ask is not a guarantee of that price for the entire quantity. Kalshi supplies YES/NO bids, so opposite-side asks are their price complements, preserving quantity. Missing depth, unknown fees, blocked injuries, stale forecasts and live games cannot create a model edge, but do not hide available quotes. Closed markets are identified separately.

`/api/nba/fair-prices` reads newer stored books between scans and recalculates eligible edge/cost values against the unchanged current pregame probability. A newer failed/expired book cannot inherit a fresh old edge; an older recorder observation cannot overwrite a newer scanner book. An event date, both team identities and YES-side identity must match before attaching a contract. Open in-game books remain visible; archived pregame forecasts do not become in-game probabilities or edges. The API remains read-only: no remote fetches, model fitting or exchange orders occur on page reads.

## Current-season probability check

At the bottom of the dashboard, the season table groups estimated probabilities into ten-percentage-point ranges and shows forecasts, wins, losses, mean forecast, actual win rate and calibration gap. **Model picks** counts the higher-probability team once per game, with five ranges from 50% to 100%. **Every team** includes both teams across all ten ranges; those two outcomes are paired, so 470 team forecasts from 235 games still represent 235 games. Samples below twenty are labeled small. Recent evaluated games can be expanded for inspection.

This is a historical replay of past-only pregame features using the earlier frozen test checkpoint, with fitting, model selection and calibration finished before the scored games. It is separate from the later serving refit used by the live board. Only saved, untouched test predictions with matching real final results are counted; missing predictions and changed results are reported as unscored. It is an outcome/calibration check. Historical market prices and trading returns are not evaluated.

The checkpoint selector also tests whether that **same full-game pick** was leading **after Q1** or **at halftime** (Q1 + Q2 cumulative scores). The export’s period scores must be complete, consecutive, nonnegative and reconcile to the final score, including overtime. Missing or inconsistent quarter data are excluded. Ahead, behind and tied are separate; lead rate uses all evaluated games in its denominator. The original probability buckets remain final-winner forecasts, so quarter lead rates are not probability calibration for a quarter-specific model. These checks also do not establish profitable exits without historical exchange prices.

The bottom of the page lists the exact 35 model inputs, grouped by definition and window, plus the separate quote/fee inputs to the edge calculation. The same catalog is exposed by `/api/nba/features`.

The report refreshes in the page every minute and can also be printed with `python3 -m NBA backtest`. New results need eligible saved out-of-sample predictions before joining the scored sample; the UI shows evaluated versus available completed games and the through date.

Default comparison: ten contracts, 0.5¢ slippage per contract, minimum 3¢ net edge. These are research policies. They are not learned optimal thresholds or account-specific fill/fee guarantees. The quadratic taker fee is rounded up to a whole cent at every price level as a conservative allowance; exchange/account rounding can be finer.

The response contains `games[].prediction.home_win_probability`, `away_win_probability`, raw estimator components, profiles, feature values and `markets[].candidates[]`. Each candidate contains `side`, `probability`, `fair_price_cents`, `vwap`, `fee_upper_estimate`, `all_in_cost`, `net_edge`, and `expected_profit_dollars` when available. Component disagreement is a diagnostic, not a confidence interval.

## Missing data and time rules

- Current executable comparisons cover **regular-season games starting within 48 hours**. The full season remains visible with archived, replayed or provisional forecasts as labeled. Playoffs require separate validation and are excluded.
- Source box scores lack historical publication timestamps. Both training replay and serving assume availability at **12:00 UTC on the day after the local game date**. Source corrections and delays remain a limitation, clearly recorded in reports.
- Every feature uses games available before its prediction cutoff. The predicted game's result and later games cannot enter its features. Provisional future forecasts hold performance history at the latest known values and use the published schedule for rest; unknown intervening results block executable edges.
- Missing preceding results within fourteen days, fewer than ten prior games, data older than 36 hours, or a model older than 30 days produce `WAIT` with reasons.
- Quote ages measure **local HTTP receipt**, not exchange event time. After 90 seconds, the API removes edge/EV values. After three minutes, it removes current/provisional estimates. At tipoff it removes the executable estimate and preserves the timestamped pregame record, regardless of Kalshi's later expiration time. A newly trained model invalidates old executable scans until rescanned.
- Crossed books, unknown fee schedules and insufficient visible depth produce `WAIT`. A one-sided book can price only the side with sufficient actual ask liquidity.
- Failed scans preserve the previous snapshot, expose the failure separately, and let the same freshness rules expire it. No synthetic fallback exists.

## Model artifacts and verification

```text
NBA/data.py             Source downloads, normalization and provenance
NBA/features.py         Shared historical/live past-only feature engine
NBA/model.py            Fitting, calibration, scoring, local model artifact
NBA/sources.py          Player/referee Parquet and venue geocoding manifests
NBA/context.py          Past-only player impact, rotation, travel and referee history
NBA/live_context.py     Timestamped injury, lineup and official assignment collection
NBA/experiments.py      Seasonal factor search, ablations and importance diagnostics
NBA/markets.py          Kalshi discovery, joining, book costs and scan snapshots
NBA/service.py          Read-only snapshot views with freshness gates
NBA/__main__.py         refresh / train / scan / watch / report CLI
NBA/.data/dataset.json  Canonical historical games, schedules and source manifest
NBA/.data/model.pkl     Locally generated serving model; never load untrusted pickles
NBA/.data/model-report.json
NBA/.data/optimization-report.json
NBA/.data/context/      Reconstructed context and original timestamped observations
NBA/.data/holdout-predictions.json
NBA/.data/fair-prices.json
NBA/.data/snapshots/    Raw public quotes/rules plus their predictions per scan
```

Local snapshots accumulate so that later market evaluation has original, timestamped evidence. There is no retention deletion; archive them when needed. Historical book depth cannot be reconstructed from final scores or closing prices.

```sh
NBA/.venv/bin/python -m unittest discover -s tests -p 'test_nba*.py' -v
python3 -m unittest discover -s BTC/tests -v
python3 -m unittest discover -s tests -v
node --test tests/test_nba_ui.cjs
```

NBA ML dependencies are optional and imported only during training/scanning. The existing BTC/Gold server still starts with the standard library.

## Position-first interface

The default page starts with **My positions**. Pick the purchased contract using the route buttons or top selector, then supply only amount spent and entry price. The selected route is displayed prominently; no model-recommended side is silently substituted. Contracts, fees, entry time and exit rules are populated automatically, with exact corrections under **Advanced**. Inferred amounts stay labeled as estimates on tracked cards. Saving waits for the automatic exit calculation and rejects input changes that would mismatch an in-flight plan; calculator failure still allows recording an existing purchase with a visible fallback warning.

Tracked purchases show rule-based actions: exit on stop, take profit on target, exit on the holding deadline, review changed availability, or request fresh/full-depth prices. The card separates sell-bid VWAP, estimated net proceeds and P&L from stop/target payoffs, and shows the original deadline and fee-adjusted break-even price. A within-plan message is explicitly not a recovery forecast. Stops and suggested actions remain local/manual; recorded actual exits alone update journal balances.

Automatic current-state target-hit/recovery probabilities remain uncalibrated and unavailable. The UI does not substitute final-winner probability or a break-even hit-rate threshold. Conditional Kelly stays available in the collapsed new-trade planner with explicitly supplied assumptions; there is no automatically fitted target-hit probability driving allocations. Existing fills/settings remain in the same local journal. Research sections and per-game entry details are collapsed, and expanded game plans survive price refreshes.


## Recorded-data risk trial and per-game paper checks

Every game now shows **Risk checks** above its purchase buttons. Expand **View route checks & recorded-data trial** for executable asks/bids, spread, top-level buy/sell depth, paper rejection reasons, full-loss ceilings and historical team coverage. This integrates `nba_kalshi.signals` with existing current pregame probabilities, adverse availability scenarios, probability buffer, injury/staleness guards and model/market disagreement limits. A fixed 5¢ spread ceiling is a trial policy, not an optimized value. Each route is an alternative paper screen, not another allocation. The separate live-performance/hustle winner model still lacks a trained artifact and live input integration. No live entries are authorized using pregame probabilities.

Run `NBA/.venv/bin/python -m NBA scalp-trial` to refresh the offline experiment. It reads a consistent snapshot of `recordings.sqlite3` without interrupting the recorder and writes `NBA/.data/research/scalp-trial.json` plus auditable `scalp-trial-episodes.jsonl`. `GET /api/nba/scalp-trial` only reads the saved report; API requests never train. Reruns are manual and the dashboard shows the run timestamp.

The predefined experiment uses the home-YES contract's YES/NO routes to avoid duplicating equivalent opponent contracts. It tests 10-contract examples at +10¢ target / −5¢ stop / 20-minute maximum holding time. Entry uses recorded full-depth ask VWAP; trigger prices use full-depth bid VWAP and execute at the next observed quote. Both legs include the historical fee multiplier and 0.5¢ slippage. Missing depth, unknown fees, crossed books, stale scores and observation gaps over 30 seconds censor examples. There is at most one entry per side per 20-minute window. These are hypothetical fills, not actual execution results or a chosen profitable strategy. Raw count exclusions and partial-game coverage matter.

A regularized logistic trial predicts **target reached before stop/time exit**, using entry price, spread, oriented score margin, regulation time remaining, past-only 60-second bid change and score age. Earlier game dates train and later dates evaluate; no game crosses partitions. Each game has equal total training/evaluation weight, though entries within and between opposing sides remain correlated. Brier error is compared against the training-only constant rate. Exploratory fitting requires at least three independent training games and three test games; that is a computation minimum, not evidence of adequate support. No live risk probabilities, Kelly inputs, serving artifacts or team-specific fitted models are promoted. Team rows are descriptive historical counts, not current matchup forecasts.

The first strict replay retained roughly 80 windows from 17 games. The unfiltered fixed scalp policy lost money after costs, and the exploratory model did not beat its constant baseline on later games. This is useful negative evidence, with substantial censoring and only four training games; it is not a validated trading conclusion. Collect more complete games before evaluating live promotion.


## Smoothed moving exits

Tracked purchases now keep **Original TP · reference** separate from **Live TP · active** and **Live stop · active**. New purchases enable moving monitoring automatically. Existing local positions are enabled on the next dashboard load, with monitoring starting then; this does not claim the policy existed when an older purchase was made. Pause/resume is available per position. Pausing retains the tighter active stop and current live target, with the original TP preserved. Nothing sends or changes an exchange order.

The current heuristic uses 90-second median full-position bid VWAP, recent adverse/absolute quote moves, spread, bounded score-margin change and late-Q4 clock context. After at least 90 seconds of continuous observations, it updates at most once per 60 seconds. A 2¢ target deadband and 1¢ maximum step suppress tick-to-tick chasing; the target may move up or down. The stop is volatility-aware and ratchets upward by at most 1¢ per update; it never automatically widens the initial allowed loss. TP movement is limited around the initial plan and considers fee-adjusted break-even. Rules are checked against recorded executable bids before moving, and reached rules latch rather than disappearing after a rebound. Manual changes can deliberately reset levels; applying calculator results cannot silently loosen the current active stop.

Missing full-position bid/ask depth, stale scores, unsupported/overtime clocks, stale quotes or gaps over 30 seconds freeze movement. At least 90 seconds of continuous history is required again after a gap. Levels/state persist in the local browser journal; after partial exits the quantity-dependent observation state restarts. Updates require the dashboard to be open and successfully polling. Closing/sleeping the laptop is not continuous exit supervision, and stops are alerts rather than guaranteed fills. Existing manual holding deadlines remain in force.

`NBA/dynamic_exits.py` shares the transition rule with an offline moving-policy replay. `scalp-trial` reports fixed and moving outcomes on common entry windows as well as exclusions. The winner/target logistic trial still predicts the fixed-baseline event; it is not a probability for this moving policy. The moving rules are experimental planning heuristics, not an optimized strategy, a calibrated success probability or demonstrated risk reduction.


Each game card now has an always-visible **Risk & live exits** panel directly below its market section. A tracked purchase is rendered under its matching game as well as in the global positions list, including original TP, moving TP, moving stop, P&L and controls. Without a purchase, placeholders explain the two-input entry flow; no personalized exit price is invented. Purchase and risk-preview buttons remain visible, while Kelly calculations and trial details remain expandable.
