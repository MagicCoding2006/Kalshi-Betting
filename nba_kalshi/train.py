"""Train and time-split evaluate the win model from a prepared snapshot CSV.

The CSV may contain multiple snapshots per game. All rows from the same calendar
date are held out together, and sample weights prevent games with denser snapshots
from dominating the fit.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date
import json
import math
from pathlib import Path
from typing import Any

from .model import FEATURE_NAMES


def _read_rows(path: Path) -> list[dict[str, Any]]:
    required = {"snapshot_id", "game_id", "game_date", "home_win", *FEATURE_NAMES}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        rows = []
        seen = set()
        game_dates: dict[str, date] = {}
        for line, raw in enumerate(reader, start=2):
            game_id = raw["game_id"].strip()
            snapshot_id = raw["snapshot_id"].strip()
            if not game_id:
                raise ValueError(f"line {line}: game_id is empty")
            if not snapshot_id or snapshot_id in seen:
                raise ValueError(f"line {line}: snapshot_id must be non-empty and unique")
            try:
                game_date = date.fromisoformat(raw["game_date"][:10])
                target = int(raw["home_win"])
                features = {name: float(raw[name]) for name in FEATURE_NAMES}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"line {line}: invalid date, target, or feature value") from exc
            if target not in (0, 1) or any(not math.isfinite(v) for v in features.values()):
                raise ValueError(f"line {line}: target must be 0/1 and features must be finite")
            if game_id in game_dates and game_dates[game_id] != game_date:
                raise ValueError(f"line {line}: game_id {game_id!r} has inconsistent dates")
            game_dates[game_id] = game_date
            seen.add(snapshot_id)
            rows.append({"snapshot_id": snapshot_id, "game_id": game_id, "game_date": game_date, "target": target, "features": features})
    if len(rows) < 100 or len({r["game_id"] for r in rows}) < 40:
        raise ValueError("Need at least 100 snapshots across 40 games for a minimally useful research split")
    return sorted(rows, key=lambda r: (r["game_date"], r["game_id"]))


def _brier(targets: list[int], probs: list[float]) -> float:
    return sum((p - y) ** 2 for y, p in zip(targets, probs)) / len(targets)


def _log_loss(targets: list[int], probs: list[float]) -> float:
    eps = 1e-15
    return -sum(y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p)) for y, p in zip(targets, probs)) / len(targets)


def train(input_csv: Path, model_path: Path, metrics_path: Path, holdout_fraction: float = 0.2) -> dict[str, Any]:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before training (scikit-learn is required)") from exc

    if not 0.1 <= holdout_fraction <= 0.4:
        raise ValueError("holdout_fraction must be between 0.1 and 0.4")
    rows = _read_rows(input_csv)
    dates = sorted({r["game_date"] for r in rows})
    if len(dates) < 10:
        raise ValueError("Need at least 10 distinct game dates for chronological evaluation")
    first_test = max(1, min(len(dates) - 1, int(len(dates) * (1 - holdout_fraction))))
    train_dates, test_dates = set(dates[:first_test]), set(dates[first_test:])
    train_rows = [r for r in rows if r["game_date"] in train_dates]
    test_rows = [r for r in rows if r["game_date"] in test_dates]
    x_train = [[r["features"][name] for name in FEATURE_NAMES] for r in train_rows]
    x_test = [[r["features"][name] for name in FEATURE_NAMES] for r in test_rows]
    y_train = [r["target"] for r in train_rows]
    y_test = [r["target"] for r in test_rows]
    if len(set(y_train)) != 2 or len(set(y_test)) != 2:
        raise ValueError("Both chronological train and holdout partitions must contain wins and losses")

    games_per_train_game: dict[str, int] = {}
    for row in train_rows:
        games_per_train_game[row["game_id"]] = games_per_train_game.get(row["game_id"], 0) + 1
    sample_weights = [1.0 / games_per_train_game[row["game_id"]] for row in train_rows]
    scaler = StandardScaler()
    scaler.fit(x_train, sample_weight=sample_weights)
    x_train_scaled = scaler.transform(x_train)
    x_test_scaled = scaler.transform(x_test)
    classifier = LogisticRegression(C=1.0, max_iter=2000, solver="lbfgs")
    classifier.fit(x_train_scaled, y_train, sample_weight=sample_weights)
    probs = [float(row[1]) for row in classifier.predict_proba(x_test_scaled)]
    predictions = [int(p >= 0.5) for p in probs]
    brier = _brier(y_test, probs)
    train_base_rate = sum(y * w for y, w in zip(y_train, sample_weights)) / sum(sample_weights)

    # Once the untouched chronological evaluation is recorded, fit the artifact
    # on all available history so paper inference uses the newest completed data.
    all_x = [[r["features"][name] for name in FEATURE_NAMES] for r in rows]
    all_y = [r["target"] for r in rows]
    games_per_game: dict[str, int] = {}
    for row in rows:
        games_per_game[row["game_id"]] = games_per_game.get(row["game_id"], 0) + 1
    all_weights = [1.0 / games_per_game[row["game_id"]] for row in rows]
    final_scaler = StandardScaler()
    final_scaler.fit(all_x, sample_weight=all_weights)
    final_x_scaled = final_scaler.transform(all_x)
    final_classifier = LogisticRegression(C=1.0, max_iter=2000, solver="lbfgs")
    final_classifier.fit(final_x_scaled, all_y, sample_weight=all_weights)

    artifact = {
        "format_version": 1,
        "means": {name: float(value) for name, value in zip(FEATURE_NAMES, final_scaler.mean_)},
        "scales": {name: float(value) for name, value in zip(FEATURE_NAMES, final_scaler.scale_)},
        "coefficients": {name: float(value) for name, value in zip(FEATURE_NAMES, final_classifier.coef_[0])},
        "intercept": float(final_classifier.intercept_[0]),
        "trained_through": max(dates).isoformat(),
        "holdout_brier_score": brier,
    }
    metrics = {
        "warning": "The temporal holdout evaluates the pre-refit model; the exported artifact is then refit on all supplied dates. This is not evidence of future profitability.",
        "train_snapshots": len(train_rows),
        "holdout_snapshots": len(test_rows),
        "train_games": len({r["game_id"] for r in train_rows}),
        "holdout_games": len({r["game_id"] for r in test_rows}),
        "train_date_range": [min(train_dates).isoformat(), max(train_dates).isoformat()],
        "holdout_date_range": [min(test_dates).isoformat(), max(test_dates).isoformat()],
        "holdout_brier_score": brier,
        "holdout_log_loss": _log_loss(y_test, probs),
        "holdout_accuracy": sum(int(a == b) for a, b in zip(y_test, predictions)) / len(y_test),
        "constant_train_rate_brier": _brier(y_test, [train_base_rate] * len(y_test)),
        "holdout_win_rate": sum(y_test) / len(y_test),
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a chronological NBA win-probability research model")
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--model", type=Path, default=Path("artifacts/nba_win_model.json"))
    parser.add_argument("--metrics", type=Path, default=Path("artifacts/holdout_metrics.json"))
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    args = parser.parse_args()
    print(json.dumps(train(args.input_csv, args.model, args.metrics, args.holdout_fraction), indent=2))


if __name__ == "__main__":
    main()
