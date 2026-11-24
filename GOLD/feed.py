"""Gold's public Kalshi/Pyth reference feed and completed minute closes.

No tokenized-gold or BTC proxy is substituted for the contract's gold index.
"""
import math
import time
from urllib.parse import quote

KALSHI = 'https://external-api.kalshi.com/trade-api/v2'


def minute_candles(points, now):
    """Close completed UTC-minute buckets only when source coverage is adequate."""
    buckets = {}
    for point in points:
        stamp, price = point['time'], point['price']
        minute = int(stamp // 60) * 60
        if minute + 60 <= now:
            buckets.setdefault(minute, {})[int(stamp)] = price
    candles = []
    for minute, ticks in sorted(buckets.items()):
        if len(ticks) >= 50 and min(ticks) <= minute + 5 and max(ticks) >= minute + 55:
            candles.append({'time': minute, 'close': ticks[max(ticks)], 'high': max(ticks.values()), 'low': min(ticks.values())})
    return candles


def fetch(engine, market, get_json):
    data = get_json(KALSHI + '/live_data/events/' + quote(market['event_ticker'], safe='') + '?range=1h')['live_data']
    details = data.get('details', {})
    if data.get('is_historical') or data.get('type') != 'commodity' or details.get('asset') != 'GOLD':
        raise ValueError('Expected a live GOLD commodity reference series')
    points = []
    for tick in details.get('timeseries', []):
        stamp, price = float(tick['t']) / 1000, float(tick['v'])
        if math.isfinite(stamp) and math.isfinite(price) and price > 0:
            points.append({'time': stamp, 'price': price})
    points = sorted({p['time']: p for p in points}.values(), key=lambda p: p['time'])
    if not points:
        raise ValueError('Gold reference series is empty')
    now = time.time()
    if now - points[-1]['time'] > 15 or points[-1]['time'] > now + 5:
        raise ValueError('Gold reference series is stale or future-dated')
    candles = minute_candles(points, now)
    if engine.config['volatility_mode'] != 'manual' and (not candles or now - candles[-1]['time'] > 180):
        raise ValueError('Gold minute closes are unavailable or stale')
    # Minute closes are calculated before chart downsampling.
    chart = points[::5]
    if chart[-1]['time'] != points[-1]['time']:
        chart.append(points[-1])
    return {'price': points[-1]['price'], 'time': points[-1]['time'],
            'source': 'Kalshi Gold reference (Pyth)', 'points': chart, 'candles': candles}
