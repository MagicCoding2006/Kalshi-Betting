"""NBA win-probability research and paper-signal engine."""

from .energy import HustleBaseline, HustleCounts, hustle_index
from .engine import PaperAssessment, PaperTradingEngine
from .model import InGameSnapshot, LogisticModel, WinEstimate
from .signals import MarketQuote, PaperSignal, SignalRules, evaluate_paper_signal

__all__ = [
    "HustleBaseline", "HustleCounts", "hustle_index", "InGameSnapshot",
    "LogisticModel", "WinEstimate", "PaperAssessment", "PaperTradingEngine", "MarketQuote", "PaperSignal",
    "SignalRules", "evaluate_paper_signal",
]
