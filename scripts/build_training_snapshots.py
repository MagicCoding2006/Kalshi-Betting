"""Build point-in-time features from downloaded NBA play-by-play Parquet files.

The available-event proxy uses rebounds, steals, and blocks. The source does not
include true deflection, loose-ball, or contested-shot counts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "nba"
OUTPUT = ROOT / "data" / "processed" / "nba_training_snapshots.csv"
KEEP = [
    "season", "game_date", "game_id", "team_home", "team_away", "event_num",
    "msg_type", "period", "secs_game", "team_abb", "off_team_abb", "hs", "vs",
    "poss_home", "poss_away", "time_actual", "description",
]
SNAPSHOT_FEATURES = [
    "snapshot_id", "game_id", "game_date", "snapshot_time_utc", "home_win", "score_margin",
    "pregame_expected_margin", "seconds_remaining", "home_offense_residual_per_100",
    "away_offense_residual_per_100", "activity_proxy_difference",
]


def _rate(count: float, chances: float) -> float:
    return 100.0 * count / chances if chances > 0 else float("nan")


def _z(value: float, history: deque[float]) -> float:
    if not np.isfinite(value) or len(history) < 5:
        return 0.0
    sd = float(np.std(history, ddof=1))
    if not np.isfinite(sd) or sd < 1e-6:
        return 0.0
    return float(np.clip((value - float(np.mean(history))) / sd, -3.0, 3.0))


def build(first_season: int, last_season: int, output: Path = OUTPUT) -> pd.DataFrame:
    paths = [RAW / f"pbp_regular_{s}.parquet" for s in range(first_season, last_season + 1)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing raw play-by-play files: {', '.join(missing)}. "
            "Run scripts/collect_public_data.py first."
        )
    parts = [pd.read_parquet(path, columns=KEEP) for path in paths]
    pbp = pd.concat(parts, ignore_index=True)
    pbp["game_date"] = pd.to_datetime(pbp["game_date"], errors="coerce").dt.date
    pbp["time_actual"] = pd.to_datetime(pbp["time_actual"], errors="coerce", utc=True)
    for col in ("event_num", "msg_type", "period", "secs_game", "hs", "vs", "poss_home", "poss_away"):
        pbp[col] = pd.to_numeric(pbp[col], errors="coerce").fillna(0.0)
    for col in ("team_home", "team_away", "team_abb", "off_team_abb"):
        pbp[col] = pbp[col].fillna("").astype(str).str.upper()
    pbp["description"] = pbp["description"].fillna("").astype(str)
    pbp.sort_values(["game_date", "game_id", "secs_game", "event_num"], inplace=True)

    # Public PBP lets us approximate a few active plays, not a full energy measure.
    desc = pbp["description"]
    pbp["orb_event"] = (
        pbp["msg_type"].eq(4) & pbp["team_abb"].eq(pbp["off_team_abb"])
        & desc.str.contains("Rebound", case=False)
        & ~desc.str.contains("Team Rebound", case=False)
    ).astype(int)
    pbp["steal_event"] = desc.str.contains(r"\bSteal:", case=False, regex=True).astype(int)
    pbp["block_event"] = desc.str.contains(r"\bBlock:", case=False, regex=True).astype(int)
    pbp["miss_event"] = pbp["msg_type"].eq(2).astype(int)
    pbp["fga_event"] = pbp["msg_type"].isin([1, 2]).astype(int)

    team_ratings: dict[str, float] = defaultdict(float)
    off_rates: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))
    def_rates: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))
    activity_rates: dict[str, dict[str, deque[float]]] = defaultdict(
        lambda: {k: deque(maxlen=15) for k in ("orb", "steal", "block")}
    )
    league_ppp_history: deque[float] = deque(maxlen=5000)
    previous_season: int | None = None
    output_rows: list[dict[str, Any]] = []

    # Finish each date before updating ratings/baselines, preventing same-day
    # outcomes from leaking into snapshots from another game that date.
    for game_date, date_rows in pbp.groupby("game_date", sort=True):
        date_season = int(date_rows["season"].iloc[0])
        if previous_season is not None and date_season != previous_season:
            for team in team_ratings:
                team_ratings[team] *= 0.75
            off_rates.clear()
            def_rates.clear()
            activity_rates.clear()
        previous_season = date_season
        games: list[dict[str, Any]] = []
        for game_id, source_game in date_rows.groupby("game_id", sort=False):
            full_game = source_game.copy()
            # In this archive poss_home/poss_away are event flags, not totals.
            full_game["cum_home_poss"] = full_game["poss_home"].cumsum()
            full_game["cum_away_poss"] = full_game["poss_away"].cumsum()
            full_final = full_game.iloc[-1]
            game = full_game[full_game["period"].between(1, 4)].copy()
            if game.empty:
                continue
            first, regulation_final = game.iloc[0], game.iloc[-1]
            home, away = first["team_home"], first["team_away"]
            if not home or not away or home == away:
                continue
            final_home, final_away = float(full_final["hs"]), float(full_final["vs"])
            home_poss, away_poss = float(full_final["cum_home_poss"]), float(full_final["cum_away_poss"])
            reg_home_poss = float(regulation_final["cum_home_poss"])
            reg_away_poss = float(regulation_final["cum_away_poss"])
            if home_poss <= 0 or away_poss <= 0 or final_home == final_away:
                continue

            # All pregame values below use prior completed dates only.
            expected_margin = team_ratings[home] - team_ratings[away] + 2.0
            league_ppp = float(np.mean(league_ppp_history)) if league_ppp_history else 1.10
            home_off = float(np.mean(off_rates[home])) if off_rates[home] else league_ppp
            away_off = float(np.mean(off_rates[away])) if off_rates[away] else league_ppp
            home_def = float(np.mean(def_rates[home])) if def_rates[home] else league_ppp
            away_def = float(np.mean(def_rates[away])) if def_rates[away] else league_ppp
            exp_home_ppp = max(0.75, min(1.40, league_ppp + home_off - league_ppp + away_def - league_ppp))
            exp_away_ppp = max(0.75, min(1.40, league_ppp + away_off - league_ppp + home_def - league_ppp))

            # Steals and blocks are credited to the defending team.
            game["orb_home_event"] = (game["orb_event"].eq(1) & game["team_abb"].eq(home)).astype(int)
            game["orb_away_event"] = (game["orb_event"].eq(1) & game["team_abb"].eq(away)).astype(int)
            game["steal_home_event"] = (game["steal_event"].eq(1) & game["team_abb"].eq(away)).astype(int)
            game["steal_away_event"] = (game["steal_event"].eq(1) & game["team_abb"].eq(home)).astype(int)
            game["block_home_event"] = (game["block_event"].eq(1) & game["team_abb"].eq(away)).astype(int)
            game["block_away_event"] = (game["block_event"].eq(1) & game["team_abb"].eq(home)).astype(int)
            game["miss_home_event"] = (game["miss_event"].eq(1) & game["team_abb"].eq(home)).astype(int)
            game["miss_away_event"] = (game["miss_event"].eq(1) & game["team_abb"].eq(away)).astype(int)
            game["fga_home_event"] = (game["fga_event"].eq(1) & game["team_abb"].eq(home)).astype(int)
            game["fga_away_event"] = (game["fga_event"].eq(1) & game["team_abb"].eq(away)).astype(int)
            cumulative_cols = [
                "orb_home_event", "orb_away_event", "steal_home_event", "steal_away_event",
                "block_home_event", "block_away_event", "miss_home_event", "miss_away_event",
                "fga_home_event", "fga_away_event",
            ]
            game[cumulative_cols] = game[cumulative_cols].cumsum()
            home_baselines, away_baselines = activity_rates[home], activity_rates[away]

            home_final_stats = {
                "orb": _rate(float(game["orb_home_event"].iloc[-1]), float(game["miss_home_event"].iloc[-1])),
                "steal": _rate(float(game["steal_home_event"].iloc[-1]), reg_away_poss),
                "block": _rate(float(game["block_home_event"].iloc[-1]), float(game["fga_away_event"].iloc[-1])),
            }
            away_final_stats = {
                "orb": _rate(float(game["orb_away_event"].iloc[-1]), float(game["miss_away_event"].iloc[-1])),
                "steal": _rate(float(game["steal_away_event"].iloc[-1]), reg_home_poss),
                "block": _rate(float(game["block_away_event"].iloc[-1]), float(game["fga_home_event"].iloc[-1])),
            }

            # One observation per minute of regulation, ending before the final horn.
            event_seconds = game["secs_game"].to_numpy(dtype=float)
            for elapsed in range(60, 2880, 60):
                ix = int(np.searchsorted(event_seconds, elapsed, side="right") - 1)
                if ix < 0:
                    continue
                row = game.iloc[ix]
                hp, ap = float(row["cum_home_poss"]), float(row["cum_away_poss"])
                hs, vs = float(row["hs"]), float(row["vs"])
                h_orb, a_orb = float(row["orb_home_event"]), float(row["orb_away_event"])
                h_steal, a_steal = float(row["steal_home_event"]), float(row["steal_away_event"])
                h_block, a_block = float(row["block_home_event"]), float(row["block_away_event"])
                h_miss, a_miss = float(row["miss_home_event"]), float(row["miss_away_event"])
                h_fga, a_fga = float(row["fga_home_event"]), float(row["fga_away_event"])
                h_z = (
                    _z(_rate(h_orb, h_miss), home_baselines["orb"])
                    + _z(_rate(h_steal, ap), home_baselines["steal"])
                    + _z(_rate(h_block, a_fga), home_baselines["block"])
                ) / 3.0
                a_z = (
                    _z(_rate(a_orb, a_miss), away_baselines["orb"])
                    + _z(_rate(a_steal, hp), away_baselines["steal"])
                    + _z(_rate(a_block, h_fga), away_baselines["block"])
                ) / 3.0
                h_exposure = min(float(row["cum_away_poss"]), h_miss, a_fga)
                a_exposure = min(float(row["cum_home_poss"]), a_miss, h_fga)
                h_z *= h_exposure / (h_exposure + 30.0) if h_exposure > 0 else 0.0
                a_z *= a_exposure / (a_exposure + 30.0) if a_exposure > 0 else 0.0
                h_resid = (hs / hp - exp_home_ppp) * 100.0 if hp > 0 else 0.0
                a_resid = (vs / ap - exp_away_ppp) * 100.0 if ap > 0 else 0.0
                output_rows.append({
                    "snapshot_id": f"{game_id}_{elapsed}",
                    "game_id": str(game_id),
                    "game_date": game_date.isoformat(),
                    "snapshot_time_utc": row["time_actual"].isoformat() if pd.notna(row["time_actual"]) else "",
                    "home_win": int(final_home > final_away),
                    "score_margin": hs - vs,
                    "pregame_expected_margin": expected_margin,
                    "seconds_remaining": 2880 - elapsed,
                    "home_offense_residual_per_100": h_resid,
                    "away_offense_residual_per_100": a_resid,
                    "activity_proxy_difference": h_z - a_z,
                })

            games.append({
                "home": home, "away": away, "margin": final_home - final_away,
                "expected_margin": expected_margin, "home_points": final_home, "away_points": final_away,
                "home_poss": home_poss, "away_poss": away_poss,
                "home_ppp": final_home / home_poss, "away_ppp": final_away / away_poss,
                "home_rates": home_final_stats, "away_rates": away_final_stats,
                "season": int(first["season"]),
            })

        if not games:
            continue
        # Simultaneous daily point-rating updates avoid within-date leakage.
        rating_changes: dict[str, float] = defaultdict(float)
        for game in games:
            error = game["margin"] - game["expected_margin"]
            rating_changes[game["home"]] += 0.04 * error
            rating_changes[game["away"]] -= 0.04 * error
        for team, delta in rating_changes.items():
            team_ratings[team] += delta

        for game in games:
            off_rates[game["home"]].append(game["home_ppp"])
            off_rates[game["away"]].append(game["away_ppp"])
            def_rates[game["home"]].append(game["away_points"] / game["away_poss"])
            def_rates[game["away"]].append(game["home_points"] / game["home_poss"])
            for name, value in game["home_rates"].items():
                if np.isfinite(value):
                    activity_rates[game["home"]][name].append(value)
            for name, value in game["away_rates"].items():
                if np.isfinite(value):
                    activity_rates[game["away"]][name].append(value)
            league_ppp_history.extend((game["home_ppp"], game["away_ppp"]))

    result = pd.DataFrame(output_rows, columns=SNAPSHOT_FEATURES)
    if result.empty:
        raise RuntimeError("No regulation snapshots were produced; inspect the source data columns")
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False)
    print(f"Wrote {len(result):,} snapshots from {result.game_id.nunique():,} games to {output}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build point-in-time NBA snapshots from downloaded play-by-play")
    parser.add_argument("--first-season", type=int, default=2023)
    parser.add_argument("--last-season", type=int, default=2027)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    build(args.first_season, args.last_season, args.output)


if __name__ == "__main__":
    main()
