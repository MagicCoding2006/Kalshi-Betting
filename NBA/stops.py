"""Entry-specific, fee-aware exit planning. No orders or fitted stop predictions."""
import math
import sqlite3
from datetime import timedelta
from pathlib import Path

from .data import DATA, instant, utcnow
from .markets import sweep
from .risk import fee_at, load_quotes, settings


def number(value, label, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError('Invalid ' + label)
    return value


def adverse_moves(directory, ticker, side, now):
    """Descriptive changes between adjacent received quotes, never bridge gaps."""
    result = {'window_minutes': 30, 'adjacent_pairs': 0, 'adverse_changes': 0, 'adverse_move_p95': None}
    path = Path(directory) / 'recordings.sqlite3'
    if not ticker or not path.exists():
        return result
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        # Side was validated before selecting this fixed SQL column.
        rows = db.execute('SELECT observed_at,' + side + '_bid FROM books WHERE ticker=? '
                          "AND observed_at>=? AND observed_at<=? AND quality='ok' ORDER BY id DESC LIMIT 5000",
                          (ticker, (now-timedelta(minutes=30)).isoformat(), now.isoformat())).fetchall()
    finally:
        db.close()
    previous = None
    moves = []
    for stamp, bid in reversed(rows):
        if bid is None:
            previous = None
            continue
        current = (instant(stamp), bid)
        if previous and 0 < (current[0]-previous[0]).total_seconds() <= 30:
            result['adjacent_pairs'] += 1
            drop = previous[1]-bid
            if drop > 1e-9:
                moves.append(drop)
        previous = current
    result['adverse_changes'] = len(moves)
    if len(moves) >= 10:
        result['adverse_move_p95'] = sorted(moves)[math.ceil(.95*len(moves))-1]
    return result


def calculate_stop(entry, quantity, target, multiplier, config, *, entry_fees=None,
                   entry_kind='planned', max_loss=None, min_reward_risk=1., min_stop_move=.01,
                   quote=None, history=None, now=None):
    """Choose the widest whole-cent loss stop meeting explicit payoff constraints.

    A spread / observed adverse-change floor can reject a stop that is too tight.
    This is a planning policy, not an estimated optimal stop or hit probability.
    """
    now = now or utcnow()
    fees = fee_at(entry, quantity, multiplier) if entry_fees is None else entry_fees
    cost = quantity*entry + fees + (quantity*config['slippage'] if entry_kind == 'planned' else 0)

    def pnl(bid):
        return quantity*bid-fee_at(bid, quantity, multiplier)-quantity*config['slippage']-cost

    gain = pnl(target)
    budget = min(gain/min_reward_risk, max_loss if max_loss is not None else math.inf)
    quote_fresh = bool(quote and quote.get('book') and quote.get('status') in ('active', 'open')
                       and -5 <= (now-instant(quote['observed_at'])).total_seconds() <= 30)
    side = (quote or {}).get('side', 'yes')
    bids = quote['book'].get(side+'_bids', []) if quote_fresh else []
    asks = quote['book'].get(side+'_asks', []) if quote_fresh else []
    best_bid = bids[0]['price'] if bids else None
    spread = max(0., asks[0]['price']-best_bid) if bids and asks else None
    history = history or {'adverse_move_p95': None, 'adjacent_pairs': 0, 'adverse_changes': 0, 'window_minutes': 30}
    move_floor = max(min_stop_move, spread or 0, history.get('adverse_move_p95') or 0)
    lower = max(.01, entry-config['stop_move'])
    upper = entry-move_floor
    candidates = [c/100 for c in range(1, 100) if lower-1e-9 <= c/100 <= upper+1e-9]
    stop = next((bid for bid in candidates if -budget-1e-9 <= pnl(bid) < 0), None) if gain > 0 else None
    # A stop must sit below the entry; a profitable stop above the entry is not
    # offered as protection for a new trade that cannot cover its opening costs.
    break_even = next((c/100 for c in range(1, 100) if pnl(c/100) >= -1e-9), None)
    reasons = []
    warnings = []
    if gain <= 0:
        reasons.append('The target does not cover entry fees, exit fees and slippage.')
    elif stop is None:
        reasons.append('No loss stop below entry meets the net reward/risk, loss budget and minimum-distance constraints. Skip this setup or change its economics; a tighter stop does not remove trading costs.')
    if not quote_fresh:
        warnings.append('No fresh executable book check: this is a payoff calculation, not a live entry signal.')
    else:
        if asks and best_bid is not None and asks[0]['price'] <= best_bid:
            reasons.append('The recorded book is crossed or locked; a usable spread is unavailable.')
        if stop is not None and best_bid is not None and best_bid <= stop+1e-9:
            reasons.append('The current bid is already at or below the proposed stop. This entry cannot start inside that protection level.')
        if not bids or sum(level['quantity'] for level in bids) < quantity:
            reasons.append('The recorded bids cannot cover the whole position. Full exit liquidity is insufficient.')
        if entry_kind == 'planned' and (not asks or sum(level['quantity'] for level in asks if level['price'] <= entry+1e-9) < quantity):
            reasons.append('The requested quantity is not currently offered at or below your entry price.')
    if history.get('adverse_move_p95') is None:
        warnings.append('Too few recent adverse price changes for the observed-move floor; no volatility-calibrated stop is available.')
    fill = sweep(bids, quantity, multiplier) if bids else None
    depth_at_stop = sum(level['quantity'] for level in bids if stop is not None and level['price'] >= stop-1e-9) if bids and stop is not None else None
    if depth_at_stop is not None and depth_at_stop < quantity and sum(level['quantity'] for level in bids) >= quantity:
        reasons.append('Current bids at or above the proposed stop do not cover the full quantity. Deeper fills can exceed the planned loss.')
    if fill and stop is not None and fill['vwap'] <= stop+1e-9 and not any('already at or below' in r for r in reasons):
        reasons.append('Selling this quantity through current depth would already cross the proposed stop.')
    loss = -pnl(stop) if stop is not None else None
    gap_bid = max(.01, stop-config['gap_move']) if stop is not None else None
    portfolio_limit=config.get('max_loss_fraction')
    tail_measure=cost if config.get('risk_loss_basis')=='full_loss' else -pnl(gap_bid) if gap_bid is not None else None
    if portfolio_limit is not None and tail_measure is not None and tail_measure>config['equity']*portfolio_limit+1e-9:
        reasons.append('This quantity exceeds your portfolio loss ceiling under the selected risk basis. Reduce quantity; tightening the planned SL does not cap full contract loss.')
    return {'entry_price': entry, 'quantity': quantity, 'entry_kind': entry_kind, 'entry_fees': fees,
            'entry_fee_basis': 'estimated' if entry_fees is None else 'entered', 'entry_cost': cost,
            'stop_bid': stop, 'target_bid': target, 'break_even_bid': break_even,
            'net_at_stop': -loss if loss is not None else None, 'net_at_target': gain,
            'loss_budget': max_loss, 'effective_loss_limit': max(0., budget), 'min_reward_risk': min_reward_risk,
            'net_reward_risk': gain/loss if loss is not None and loss > 0 else None,
            'break_even_hit_rate': loss/(loss+gain) if loss is not None and gain > 0 else None,
            'two_wins_one_loss': 2*gain-loss if loss is not None else None,
            'gap_bid': gap_bid, 'net_at_gap_stop': pnl(gap_bid) if gap_bid is not None else None,
            'full_loss': cost, 'planned_risk_fraction': loss/config['equity'] if loss is not None and config['equity'] else None,
            'portfolio_loss_limit':config['equity']*portfolio_limit if portfolio_limit is not None else None,
            'portfolio_loss_basis':config.get('risk_loss_basis'),
            'minimum_stop_distance': move_floor, 'spread': spread, 'history': history,
            'quote_fresh': quote_fresh, 'quote_at': quote.get('observed_at') if quote_fresh else None,
            'current_bid': best_bid, 'current_bid_vwap': fill['vwap'] if fill else None,
            'depth_at_stop': depth_at_stop,
            'current_exit_pnl': fill['price_cost']-fill['fee_upper_estimate']-quantity*config['slippage']-cost if fill else None,
            'feasible': stop is not None and not reasons, 'reasons': reasons, 'warnings': warnings,
            'basis': 'Payoff constraints plus spread and observed price-change floors; not a fitted optimal stop.',
            'price_increment': .01, 'hold_minutes': config['hold_minutes']}


def stop_plan(body, directory=DATA, now=None):
    now = now or utcnow()
    config = settings(body.get('settings', {}))
    entry = number(body.get('entry_price'), 'entry price', .001, .999)
    quantity = body.get('quantity')
    if type(quantity) is not int or not 1 <= quantity <= 100000:
        raise ValueError('Quantity must be a whole number from 1 to 100000')
    target = number(body.get('target_bid', min(.99, entry+config['target_move'])), 'target bid', .001, .999)
    if target <= entry:
        raise ValueError('Target bid must exceed entry price')
    entry_kind = body.get('entry_kind', 'planned')
    if entry_kind not in ('planned', 'filled'):
        raise ValueError('Unknown entry type')
    fees = body.get('entry_fees')
    if fees is not None:
        fees = number(fees, 'entry fees', 0, quantity)
    max_loss = body.get('max_loss')
    if max_loss is not None:
        max_loss = number(max_loss, 'loss budget', .001, 1e9)
    rr = number(body.get('min_reward_risk', 1.), 'minimum reward/risk', .1, 20)
    min_move = number(body.get('min_stop_move', .01), 'minimum stop distance', .01, .5)
    side = body.get('side', 'yes')
    if side not in ('yes', 'no'):
        raise ValueError('Invalid contract side')
    ticker = body.get('ticker')
    game_id = body.get('game_id')
    if ticker is not None and (not isinstance(ticker, str) or not 1 <= len(ticker) <= 100):
        raise ValueError('Invalid contract ticker')
    if not isinstance(game_id, str) or not 1 <= len(game_id) <= 100:
        raise ValueError('Invalid game ID')
    quotes, contracts = load_quotes(directory, now) if ticker else ({}, {})
    if ticker and (ticker not in contracts or contracts[ticker]['game_id'] != game_id):
        raise ValueError('The selected contract cannot be verified for this game')
    quote = dict(quotes[ticker], side=side) if ticker in quotes else None
    multiplier = quote.get('multiplier') if quote else None
    fee_assumed = multiplier is None
    multiplier = 1. if fee_assumed else multiplier
    result = calculate_stop(entry, quantity, target, multiplier, config, entry_fees=fees, entry_kind=entry_kind,
                            max_loss=max_loss, min_reward_risk=rr, min_stop_move=min_move,
                            quote=quote, history=adverse_moves(directory, ticker, side, now), now=now)
    if fee_assumed:
        result['warnings'].append('Fee multiplier 1 is assumed; current series fee metadata is unavailable. Verify fees before trading.')
    result.update(generated_at=now.isoformat(), game_id=game_id, ticker=ticker, side=side,
                  fee_multiplier=multiplier, fee_multiplier_assumed=fee_assumed,
                  entry_edge_evaluated=False, stop_hit_probability_fitted=False)
    return result
