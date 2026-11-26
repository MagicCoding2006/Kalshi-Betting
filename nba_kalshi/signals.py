"""Fee-aware, conservative paper-trading signal and position sizing rules."""

from dataclasses import dataclass
import math
from typing import Literal

Outcome = Literal["home", "away"]


@dataclass(frozen=True)
class MarketQuote:
    outcome: Outcome
    ask: float
    bid: float
    estimated_entry_fee_per_contract: float
    estimated_exit_fee_per_contract: float
    max_spread: float
    contracts_available_at_best_ask: int
    contracts_available_at_best_bid: int

    def __post_init__(self) -> None:
        if self.outcome not in ("home", "away"):
            raise ValueError("outcome must be 'home' or 'away'")
        if not all(math.isfinite(v) for v in (self.ask, self.bid, self.estimated_entry_fee_per_contract, self.estimated_exit_fee_per_contract, self.max_spread)):
            raise ValueError("quote prices and fees must be finite")
        if not 0.0 <= self.bid <= self.ask <= 1.0:
            raise ValueError("quote must satisfy 0 <= bid <= ask <= 1")
        if min(self.estimated_entry_fee_per_contract, self.estimated_exit_fee_per_contract) < 0:
            raise ValueError("fees cannot be negative")
        if not 0 <= self.max_spread <= 1:
            raise ValueError("max_spread must be in [0, 1]")
        if not isinstance(self.contracts_available_at_best_ask, int) or not isinstance(self.contracts_available_at_best_bid, int):
            raise ValueError("top-of-book quantities must be whole contracts")
        if self.contracts_available_at_best_ask < 0 or self.contracts_available_at_best_bid < 0:
            raise ValueError("top-of-book quantities cannot be negative")


@dataclass(frozen=True)
class SignalRules:
    minimum_net_edge: float
    maximum_loss_dollars_per_trade: float
    maximum_contracts: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_net_edge) or not 0 < self.minimum_net_edge < 1:
            raise ValueError("minimum_net_edge must be in (0, 1)")
        if not isinstance(self.maximum_contracts, int) or not math.isfinite(self.maximum_loss_dollars_per_trade) or self.maximum_loss_dollars_per_trade <= 0 or self.maximum_contracts <= 0:
            raise ValueError("risk and contract limits must be positive")


@dataclass(frozen=True)
class PaperSignal:
    action: Literal["paper_buy", "no_trade"]
    outcome: Outcome | None
    model_probability: float | None
    net_edge: float | None
    contracts: int
    reason: str


def evaluate_paper_signal(
    home_probability: float,
    quote: MarketQuote,
    rules: SignalRules,
) -> PaperSignal:
    """Compare model probability to the executable ask after estimated round-trip fees.

    This returns a paper signal only. It does not create, send, or cancel orders.
    """
    if not math.isfinite(home_probability) or not 0.0 <= home_probability <= 1.0:
        raise ValueError("home_probability must be in [0, 1]")
    if quote.ask - quote.bid > quote.max_spread:
        return PaperSignal("no_trade", None, None, None, 0, "spread exceeds configured limit")
    if quote.contracts_available_at_best_ask < 1 or quote.contracts_available_at_best_bid < 1:
        return PaperSignal("no_trade", None, None, None, 0, "insufficient top-of-book liquidity")
    probability = home_probability if quote.outcome == "home" else 1.0 - home_probability
    round_trip_cost = quote.estimated_entry_fee_per_contract + quote.estimated_exit_fee_per_contract
    net_edge = probability - quote.ask - round_trip_cost
    if net_edge + 1e-12 < rules.minimum_net_edge:
        return PaperSignal("no_trade", quote.outcome, probability, net_edge, 0, "net edge below threshold")

    loss_per_contract = quote.ask + quote.estimated_entry_fee_per_contract
    if loss_per_contract <= 0:
        return PaperSignal("no_trade", quote.outcome, probability, net_edge, 0, "cannot size a zero-cost contract safely")
    contracts = min(
        rules.maximum_contracts,
        quote.contracts_available_at_best_ask,
        quote.contracts_available_at_best_bid,
        int(rules.maximum_loss_dollars_per_trade // loss_per_contract),
    )
    if contracts < 1:
        return PaperSignal("no_trade", quote.outcome, probability, net_edge, 0, "risk cap is smaller than one contract")
    return PaperSignal("paper_buy", quote.outcome, probability, net_edge, contracts, "paper signal passed configured filters")
