"""Pure paper decision engine; callers provide timestamp-aligned data and quotes."""

from dataclasses import dataclass

from .model import InGameSnapshot, LogisticModel
from .signals import MarketQuote, PaperSignal, SignalRules, evaluate_paper_signal


@dataclass(frozen=True)
class PaperAssessment:
    home_probability: float
    away_probability: float
    signal: PaperSignal


class PaperTradingEngine:
    """Calculate an estimate and paper signal without making network calls."""

    def __init__(self, model: LogisticModel, rules: SignalRules) -> None:
        self.model = model
        self.rules = rules

    def assess(self, snapshot: InGameSnapshot, quote: MarketQuote) -> PaperAssessment:
        estimate = self.model.estimate(snapshot)
        signal = evaluate_paper_signal(estimate.home_win_probability, quote, self.rules)
        return PaperAssessment(
            home_probability=estimate.home_win_probability,
            away_probability=estimate.away_win_probability,
            signal=signal,
        )
