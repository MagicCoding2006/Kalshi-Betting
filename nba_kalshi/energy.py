"""Opportunity-adjusted hustle proxy. It is a signal, not a direct measure of effort."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class HustleCounts:
    deflections: int
    loose_balls_recovered: int
    offensive_rebounds: int
    contested_shots: int
    offensive_rebound_opportunities: int

    def __post_init__(self) -> None:
        if not all(isinstance(v, int) for v in (self.deflections, self.loose_balls_recovered, self.offensive_rebounds, self.contested_shots, self.offensive_rebound_opportunities)):
            raise ValueError("Hustle event counts must be whole numbers")
        if min(self.deflections, self.loose_balls_recovered, self.offensive_rebounds, self.contested_shots, self.offensive_rebound_opportunities) < 0:
            raise ValueError("Hustle counts must be non-negative")


@dataclass(frozen=True)
class HustleBaseline:
    """Historical event rates per 100 relevant opportunities and their standard deviations."""

    event_rates: dict[str, float]
    event_rate_sds: dict[str, float]

    def __post_init__(self) -> None:
        names = {"deflections", "loose_balls_recovered", "offensive_rebounds", "contested_shots"}
        if set(self.event_rates) != names or set(self.event_rate_sds) != names:
            raise ValueError("Baseline must contain all four hustle event rates and standard deviations")
        if any(v <= 0 for v in self.event_rate_sds.values()):
            raise ValueError("Each baseline standard deviation must be positive")
        if any(v < 0 for v in self.event_rates.values()):
            raise ValueError("Baseline rates must be non-negative")
        if not all(math.isfinite(v) for v in (*self.event_rates.values(), *self.event_rate_sds.values())):
            raise ValueError("Baseline rates and standard deviations must be finite")


def hustle_index(
    counts: HustleCounts,
    defensive_possessions: float,
    offensive_rebound_opportunities: float,
    opponent_field_goal_attempts: float,
    baseline: HustleBaseline,
    prior_possessions: float = 30.0,
) -> float:
    """Return a shrunk mean z-score versus a team's historical rates.

    Deflections/loose balls use defensive possessions, offensive rebounds use
    rebound opportunities, and contests use opponent field-goal attempts. These
    denominators reduce (but do not eliminate) differences in opportunity.
    Shrinkage pulls noisy early-game readings toward neutral. Equal weights are
    placeholders until fitted.
    """
    exposures = (defensive_possessions, offensive_rebound_opportunities, opponent_field_goal_attempts, prior_possessions)
    if not all(math.isfinite(v) for v in exposures):
        raise ValueError("opportunity counts must be finite")
    if defensive_possessions <= 0 or min(offensive_rebound_opportunities, opponent_field_goal_attempts) < 0 or prior_possessions <= 0:
        raise ValueError("defensive possessions/prior must be positive and other opportunities non-negative")
    observed = {
        "deflections": counts.deflections,
        "loose_balls_recovered": counts.loose_balls_recovered,
        "offensive_rebounds": counts.offensive_rebounds,
        "contested_shots": counts.contested_shots,
    }
    opportunities = {
        "deflections": defensive_possessions,
        "loose_balls_recovered": defensive_possessions,
        "offensive_rebounds": offensive_rebound_opportunities,
        "contested_shots": opponent_field_goal_attempts,
    }
    z_scores = [
        0.0 if opportunities[name] == 0 else
        (observed[name] * 100.0 / opportunities[name] - baseline.event_rates[name]) / baseline.event_rate_sds[name]
        for name in observed
    ]
    clipped_mean = sum(max(-3.0, min(3.0, z)) for z in z_scores) / len(z_scores)
    reliability = defensive_possessions / (defensive_possessions + prior_possessions)
    return reliability * clipped_mean
