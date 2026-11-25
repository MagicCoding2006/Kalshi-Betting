"""Feature engineering and inference for a calibrated NBA win model.

This module does not fetch data or place trades. Model parameters must be trained
on time-ordered historical snapshots before the probability is considered usable.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Mapping

FEATURE_NAMES = (
    "score_margin",
    "pregame_expected_margin",
    "seconds_remaining",
    "home_offense_residual_per_100",
    "away_offense_residual_per_100",
    "hustle_index_difference",
)


@dataclass(frozen=True)
class InGameSnapshot:
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    period: int
    seconds_remaining: int
    pregame_expected_home_margin: float
    home_offense_residual_per_100: float
    away_offense_residual_per_100: float
    home_hustle_index: float
    away_hustle_index: float

    def __post_init__(self) -> None:
        if self.home_score < 0 or self.away_score < 0:
            raise ValueError("Scores must be non-negative")
        if not 1 <= self.period <= 4:
            raise ValueError("This baseline model only supports regulation periods 1..4")
        if not 0 <= self.seconds_remaining <= 720:
            raise ValueError("seconds_remaining must be between 0 and 720")
        numeric_values = (
            self.pregame_expected_home_margin,
            self.home_offense_residual_per_100,
            self.away_offense_residual_per_100,
            self.home_hustle_index,
            self.away_hustle_index,
        )
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("Model inputs must be finite numbers")

    @property
    def seconds_left_in_game(self) -> int:
        return (4 - self.period) * 720 + self.seconds_remaining

    def features(self) -> dict[str, float]:
        return {
            "score_margin": float(self.home_score - self.away_score),
            "pregame_expected_margin": self.pregame_expected_home_margin,
            "seconds_remaining": float(self.seconds_left_in_game),
            "home_offense_residual_per_100": self.home_offense_residual_per_100,
            "away_offense_residual_per_100": self.away_offense_residual_per_100,
            "hustle_index_difference": self.home_hustle_index - self.away_hustle_index,
        }


@dataclass(frozen=True)
class WinEstimate:
    home_win_probability: float
    away_win_probability: float


@dataclass(frozen=True)
class LogisticModel:
    """Standardized logistic regression parameters exported by training.py."""

    means: Mapping[str, float]
    scales: Mapping[str, float]
    coefficients: Mapping[str, float]
    intercept: float
    trained_through: str
    holdout_brier_score: float
    format_version: int = 1

    @classmethod
    def load(cls, path: str | Path) -> "LogisticModel":
        with Path(path).open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if raw.get("format_version") != 1:
            raise ValueError("Unsupported model format")
        model = cls(**raw)
        for name in FEATURE_NAMES:
            if name not in model.means or name not in model.scales or name not in model.coefficients:
                raise ValueError(f"Model artifact is missing feature {name}")
            if model.scales[name] <= 0:
                raise ValueError(f"Model feature scale must be positive: {name}")
            if not all(math.isfinite(v) for v in (model.means[name], model.scales[name], model.coefficients[name])):
                raise ValueError(f"Model parameters must be finite: {name}")
        if not math.isfinite(model.intercept):
            raise ValueError("Model intercept must be finite")
        return model

    def predict_home_probability(self, snapshot: InGameSnapshot) -> float:
        features = snapshot.features()
        logit = self.intercept
        for name in FEATURE_NAMES:
            standardized = (features[name] - self.means[name]) / self.scales[name]
            logit += self.coefficients[name] * standardized
        # Numerically stable sigmoid.
        if logit >= 0:
            z = math.exp(-logit)
            return 1.0 / (1.0 + z)
        z = math.exp(logit)
        return z / (1.0 + z)

    def estimate(self, snapshot: InGameSnapshot) -> WinEstimate:
        home = self.predict_home_probability(snapshot)
        return WinEstimate(home_win_probability=home, away_win_probability=1.0 - home)
