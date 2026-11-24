"""Digital Black–Scholes baseline and conservative, whole-contract sizing."""
import math
from datetime import datetime

YEAR_SECONDS = 365 * 24 * 60 * 60
DEFAULTS = {
    'bankroll': 100.0, 'kelly_fraction': 0.25, 'max_position_pct': 0.02,
    'max_exposure_pct': 0.05, 'min_edge': 0.03, 'probability_haircut': 0.02,
    'slippage': 0.005, 'volatility_mode': 'realized', 'manual_volatility': 0.60,
    'drift': 0.0, 'min_seconds': 75, 'take_profit': 0.15, 'stop_loss': 0.20,
    'drift_mode': 'historical', 'slippage_mode': 'observed',
}


def timestamp(value):
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def digital_probability(spot, strike, seconds, volatility, drift=0.0):
    """N(d2): risk-neutral digital probability if drift=r, GBM physical if drift=mu.

    This estimates terminal exceedance, NOT first passage / touch probability.
    It does not price the final-minute arithmetic-average settlement exactly.
    """
    if not all(finite(x) for x in (spot, strike, seconds, volatility, drift)):
        raise ValueError('Model inputs must be finite')
    if spot <= 0 or strike <= 0 or volatility < 0:
        raise ValueError('Positive spot/strike and nonnegative volatility required')
    years = max(0.0, seconds) / YEAR_SECONDS
    if years == 0 or volatility == 0:
        terminal = spot * math.exp(drift * years)
        return float(terminal >= strike)
    d2 = (math.log(spot / strike) + (drift - volatility ** 2 / 2) * years) / (volatility * math.sqrt(years))
    return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))


def realized_volatility(candles, now):
    """Annualized sample stdev of contiguous, completed 1-minute log returns."""
    closes = sorted({int(c['time']): float(c['close']) for c in candles
                     if c['time'] + 60 <= now and finite(c['close']) and c['close'] > 0}.items())
    returns = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:]) if b[0] - a[0] == 60]
    if len(returns) < 30:
        return None, len(returns)
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    vol = math.sqrt(variance * YEAR_SECONDS / 60)
    return (vol if vol > 0 else None), len(returns)


def return_statistics(candles, now):
    """Historical GBM drift and estimation error; not a forward-return guarantee."""
    closes = sorted({int(c['time']): float(c['close']) for c in candles
                     if c['time'] + 60 <= now and finite(c['close']) and c['close'] > 0}.items())
    returns = [math.log(b[1] / a[1]) for a, b in zip(closes, closes[1:]) if b[0] - a[0] == 60]
    if len(returns) < 30:
        return {'drift': None, 'drift_standard_error': None, 'count': len(returns)}
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    scale = YEAR_SECONDS / 60
    return {'drift': mean * scale + variance * scale / 2,
            'drift_standard_error': math.sqrt(variance / len(returns)) * scale,
            'count': len(returns), 'start': closes[0][0], 'end': closes[-1][0] + 60}


def normalize_book(payload):
    """Prefer fixed-point dollar schema, retain legacy cent schema support."""
    fixed = payload.get('orderbook_fp')
    book = fixed if fixed is not None else payload.get('orderbook', {})
    def levels(side):
        key = side + '_dollars' if fixed is not None or side + '_dollars' in book else side
        divisor = 1 if key.endswith('_dollars') else 100
        clean = []
        for price, quantity in book.get(key, []) or []:
            p, q = float(price) / divisor, float(quantity)
            if finite(p) and finite(q) and 0 < p < 1 and q > 0:
                clean.append({'price': p, 'quantity': q})
        return sorted(clean, key=lambda l: l['price'], reverse=True)
    yes, no = levels('yes'), levels('no')
    def asks(opposite):
        return [{'price': round(1 - l['price'], 6), 'quantity': l['quantity']} for l in opposite]
    return {'yes_bids': yes, 'no_bids': no, 'yes_asks': asks(no), 'no_asks': asks(yes)}


def fee(quantity, price, multiplier=1.0):
    """Conservative non-direct account cent rounding for a single paper fill."""
    return math.ceil((0.07 * multiplier * quantity * price * (1 - price) - 1e-10) * 100) / 100


def kelly(probability, all_in_cost):
    if not 0 < all_in_cost < 1:
        return 0.0
    return max(0.0, (probability - all_in_cost) / (1 - all_in_cost))


def propose(side, probability, book, config, equity, cash, exposure, multiplier):
    levels = book.get(side + '_asks', [])
    if not levels:
        return {'side': side, 'contracts': 0, 'edge': None, 'reason': 'No executable ask liquidity'}
    ask = levels[0]['price']
    slippage = config.get('slippage_by_side', {}).get(side, config['slippage'])
    if not finite(slippage):
        return {'side': side, 'probability': probability, 'ask': ask, 'contracts': 0,
                'edge': None, 'reason': 'Collecting 10 fresh quote intervals for the execution buffer'}
    conservative_p = max(0, probability - config['probability_haircut'])
    unit_fee = 0.07 * multiplier * ask * (1 - ask)
    cost = ask + unit_fee + slippage
    full_kelly = kelly(conservative_p, cost)
    budget = max(0, min(equity * full_kelly * config['kelly_fraction'],
                        equity * config['max_position_pct'],
                        equity * config['max_exposure_pct'] - exposure, cash))
    contracts = max(0, min(math.floor(budget / cost) if cost > 0 else 0,
                           math.floor(levels[0]['quantity'])))
    while contracts and contracts * (ask + slippage) + fee(contracts, ask, multiplier) > budget + 1e-9:
        contracts -= 1
    total_fee = fee(contracts or 1, ask, multiplier)
    edge = conservative_p - ask - total_fee / (contracts or 1) - slippage
    reason = 'Positive edge after fees and buffers'
    if edge < config['min_edge']:
        contracts, reason = 0, 'Net edge below entry threshold'
    elif contracts == 0:
        reason = 'Kelly budget, exposure cap, or whole-contract liquidity too small'
    return {'side': side, 'probability': probability, 'adjusted_probability': conservative_p,
            'ask': ask, 'edge': edge, 'slippage': slippage, 'full_kelly': full_kelly,
            'fractional_kelly': full_kelly * config['kelly_fraction'], 'budget': budget,
            'contracts': contracts, 'fee': fee(contracts, ask, multiplier),
            'cost': contracts * ask + fee(contracts, ask, multiplier),
            'portfolio_pct': (contracts * ask + fee(contracts, ask, multiplier)) / equity if equity else 0,
            'reason': reason}


def evaluate(market, book, spot, source_time, book_time, volatility, config,
             now, equity, cash, exposure, fee_multiplier=1.0, fee_supported=True):
    seconds = max(0, timestamp(market.get('close_time')) - now)
    blockers = []
    if market.get('status') not in ('active', 'open') or not timestamp(market.get('open_time')) <= now < timestamp(market.get('close_time')):
        blockers.append('Market is not open for trading')
    if seconds < config['min_seconds']:
        blockers.append('Entry cutoff: too close to settlement')
    if now - source_time > 15 or source_time > now + 5:
        blockers.append('Reference price is stale or its timestamp is invalid')
    if now - book_time > 10:
        blockers.append('Order book is stale')
    strike = market.get('floor_strike')
    if not finite(strike) or strike <= 0:
        blockers.append('Strike is not published yet')
    if market.get('strike_type') not in ('greater', 'greater_or_equal'):
        blockers.append('Unsupported contract payoff; expected terminal above-strike market')
    if not finite(spot) or spot <= 0:
        blockers.append('No valid underlying reference price')
    if not finite(volatility) or volatility <= 0:
        blockers.append('Need at least 30 completed minute returns or manual volatility')
    if not fee_supported:
        blockers.append('Fee model is unknown; cannot estimate executable edge')
    for side in ('yes', 'no'):
        bids, asks = book.get(side + '_bids', []), book.get(side + '_asks', [])
        if bids and asks and bids[0]['price'] > asks[0]['price'] + 1e-8:
            blockers.append('Crossed order book snapshot')
            break
    probability = None
    drift = config.get('effective_drift', config['drift'])
    if not finite(drift):
        blockers.append('Historical drift needs at least 30 completed minute returns')
    if finite(spot) and spot > 0 and finite(strike) and strike > 0 and finite(volatility) and volatility > 0:
        if finite(drift):
            probability = digital_probability(spot, strike, seconds, volatility, drift)
    candidates = []
    if probability is not None and fee_supported:
        candidates = [propose('yes', probability, book, config, equity, cash, exposure, fee_multiplier),
                      propose('no', 1 - probability, book, config, equity, cash, exposure, fee_multiplier)]
    actionable = [c for c in candidates if c['contracts'] > 0]
    best = max(actionable or candidates, key=lambda c: c.get('edge') if c.get('edge') is not None else -10, default=None)
    action = 'WAIT' if blockers or not actionable else 'BUY ' + best['side'].upper()
    return {'action': action, 'probability': probability, 'seconds_left': seconds,
            'volatility': volatility, 'drift': drift, 'candidates': candidates, 'sizing': best,
            'blockers': blockers, 'reason': '; '.join(blockers) if blockers else (best['reason'] if best else 'Waiting for market data')}


def validate_config(update, existing):
    numeric = {'bankroll': (1, 1e9), 'kelly_fraction': (0, 1), 'max_position_pct': (0, 0.25),
               'max_exposure_pct': (0, 0.5), 'min_edge': (0, 0.5), 'probability_haircut': (0, 0.5),
               'slippage': (0, 0.1), 'manual_volatility': (0.01, 5), 'drift': (-1, 1),
               'min_seconds': (60, 600), 'take_profit': (0.01, 2), 'stop_loss': (0.01, 0.99)}
    result = dict(existing)
    for key, value in update.items():
        if key == 'volatility_mode':
            if value not in ('realized', 'manual'):
                raise ValueError('Invalid volatility mode')
        elif key in ('drift_mode', 'slippage_mode'):
            allowed = ('historical', 'neutral', 'manual') if key == 'drift_mode' else ('observed', 'manual')
            if value not in allowed:
                raise ValueError('Invalid ' + key)
        elif key in numeric:
            if isinstance(value, bool) or not finite(value) or not numeric[key][0] <= value <= numeric[key][1]:
                raise ValueError('Invalid value for ' + key)
        else:
            raise ValueError('Unknown setting: ' + key)
        result[key] = value
    if result['max_position_pct'] > result['max_exposure_pct']:
        raise ValueError('Position cap cannot exceed total exposure cap')
    return result
