"""BTC spot proxy and minute-return data, separate from settlement averaging."""
import math
import time
from desk.model import timestamp


def fetch(engine, market, get_json):
    # A raw spot proxy is appropriate for GBM. Kalshi's potentially smoothed
    # chart stays separate and never feeds the spot model or realized sigma.
    ticker = get_json('https://api.exchange.coinbase.com/products/BTC-USD/ticker')
    now = int(time.time())
    if now - engine.candle_cache_time > 60 or not engine.candle_cache:
        try:
            rows = get_json(f'https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&start={now-14400}&end={now}')
            engine.candle_cache = sorted([{'time': row[0], 'close': float(row[4]), 'high': float(row[2]), 'low': float(row[1])} for row in rows], key=lambda c: c['time'])
            engine.candle_cache_time = now
        except Exception:
            if engine.config['volatility_mode'] != 'manual':
                raise
    candles = engine.candle_cache
    if engine.config['volatility_mode'] != 'manual' and (not candles or now - candles[-1]['time'] > 180):
        raise ValueError('Coinbase minute candles are stale; volatility is unavailable')
    price, source_time = float(ticker['price']), timestamp(ticker['time'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid Coinbase BTC spot price')
    engine.spot_history = [p for p in engine.spot_history if p['time'] >= now - 3600]
    if not engine.spot_history or source_time > engine.spot_history[-1]['time']:
        engine.spot_history.append({'time': source_time, 'price': price})
    history_start = engine.spot_history[0]['time']
    points = [{'time': c['time'] + 60, 'price': c['close']} for c in candles
              if c['time'] + 60 < history_start and c['time'] + 60 <= now]
    return {'price': price, 'time': source_time, 'source': 'Coinbase spot proxy',
            'points': points + engine.spot_history, 'candles': candles}
