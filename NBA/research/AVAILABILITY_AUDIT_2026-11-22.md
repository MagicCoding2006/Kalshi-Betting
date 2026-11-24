# OKC–Miami availability audit — November 22, 2026

Based on original saved forecasts and received public books. Existing snapshots were not rewritten. User account fills and order timestamps are not connected, so this does not attribute any specific user loss.

SGA was present as questionable in earlier observations, then out in the observation received at 21:44:44 UTC (4:44 p.m. Eastern). His projected minutes were zero in subsequent archived pregame forecasts. The forecast remained far above the public market after that update. More frequent updates alone would not establish that valuation as correct.

| Forecast time (UTC) | SGA status | Projected minutes | OKC win estimate | YES bid / ask | Model minus midpoint (absolute) |
| --- | --- | ---: | ---: | --- | ---: |
| 20:00:47 | questionable | 39.1 | 62.56% | 39¢ / 40¢ | 23.06 pp |
| 21:34:34 | uncertain | 39.1 | 62.56% | 30¢ / 31¢ | 32.06 pp |
| 21:44:44 | out | 0.0 | 58.97% | 29¢ / 30¢ | 29.47 pp |
| 22:49:16 | out | 0.0 | 64.02% | 37¢ / 38¢ | 26.52 pp |

All 31 saved pregame scans inspected from 20:00 UTC onward exceeded the new 15 pp disagreement review threshold. This is a retrospective policy check, not an estimate of improved profitability; it does not incorporate unobserved execution or prove when a user entered.

The later updater failure was http.client.IncompleteRead from the referee page. It terminated the old watch process. The new worker isolates availability from scans/history ingestion, catches transport failures, publishes health, and is supervised by a per-user macOS launch agent.

New live-entry policies also require fresh official game-specific coverage, pause important-player uncertainty, pause large roster disruption, and require confirmed lineups for entries within 30 minutes of tipoff. These are conservative policies, not optimized or validated predictors.

The active model weights and model ID were not promoted or retrained during this fix. Prospective market-relative calibration and fee-aware scalp results are still required before claiming an edge.

## Reported 43-cent entry and overnight hold

The user reports entering the day before at approximately 43 cents, then holding. Exact entry time, quantity, fees and contract side were not supplied; the following assumes YES on OKC winning. This is a reconstruction from received public observations, not an account fill reconciliation.

- The first archived November 21 injury observation is 8:36 p.m. Eastern and lists SGA as questionable/day-to-day, with an ESPN source-update timestamp of 5:45 p.m. Eastern. It does not establish when the user entered relative to that injury report.
- The first recorded pregame YES bid at or below 38 cents was 1:12:36 p.m. Eastern on November 22. The observed 38-cent level had substantial displayed quantity, but displayed depth does not prove an actual user fill.
- At 4:29:09 p.m. the recorded bid was 39 cents; by 4:30:11 p.m. it was 31 cents. These are sampled observations about a minute apart, not a complete tick-by-tick path.
- The [official 4:30 p.m. report](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-22_04_30PM.pdf) lists SGA out. The old ESPN-based pipeline first records him out at 4:44:44 p.m. Eastern. Official sourcing was missing from that pipeline.
- At 5:59:57 p.m., just before tipoff, the recorded YES bid was 37 cents. The contract later finalized NO with a zero YES settlement value.

For a 43-cent purchase, a 38-cent sale is an 11.6% gross loss of entry price, a 29-cent sale is a 32.6% gross loss, and zero settlement loses the whole entry price. Fees worsen those figures. These are price-arithmetic examples, not executable return backtests.

An overnight hold also exceeds the planner's default 20-minute scalp deadline. Current time, price and injury-change alerts require an actual fill to be recorded in the local tracker; they do not submit exchange orders, and page-visible alerts do not execute in the background. Background source collection updates predictions independently of those manual position alerts. No software-side account stop was active merely because a suggested level was displayed.

The new key-player uncertainty filter blocks similar new entries while a major player's participation is unresolved. Market-disagreement review, a chosen full-contract-loss sizing ceiling, and manual time/price exits address different parts of this incident. None demonstrates that a particular alternative exit would have filled or that the strategy has positive prospective returns.
