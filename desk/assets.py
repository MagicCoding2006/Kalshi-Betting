"""Asset-specific feeds and contract facts; the UI and execution engine are shared."""
from BTC.feed import fetch as btc_feed
from GOLD.feed import fetch as gold_feed

ASSETS = {
    'btc': {
        'id': 'btc', 'symbol': 'BTC', 'name': 'Bitcoin', 'icon': '₿', 'series': 'KXBTC15M',
        'price_label': 'BTC spot price', 'unit': 'USD / BTC', 'feed': btc_feed,
        'source_label': 'Coinbase BTC/USD spot proxy', 'source_short': 'COINBASE',
        'returns_source': 'Coinbase completed 1-minute log returns',
        'settlement': 'Final 60-second average of CF Benchmarks BRTI, compared with the opening average.',
        'model_note': 'Coinbase spot is a proxy for BRTI. Terminal-price probability approximates averaged settlement.',
        'feed_notice': 'Model input: Coinbase BTC/USD spot proxy. Kalshi settles on the final-minute BRTI average.',
        'defaults': {},
    },
    'gold': {
        'id': 'gold', 'symbol': 'GOLD', 'name': 'Gold', 'icon': 'Au', 'series': 'KXGOLD15M',
        'price_label': 'Gold reference price', 'unit': 'USD / troy oz', 'feed': gold_feed,
        'source_label': 'Kalshi Gold reference (Pyth)', 'source_short': 'PYTH / KALSHI',
        'returns_source': 'Completed minute closes from Kalshi Gold reference ticks (Pyth)',
        'settlement': 'Closing price of the one-minute Pyth Gold candle at the window end, compared with the opening candle close; values rounded to two decimals.',
        'model_note': 'The model uses Kalshi’s published Gold reference series. Lognormal dynamics, constant volatility, and rounding remain approximations.',
        'feed_notice': 'Model input: Kalshi’s live Gold reference series. Settlement uses the Pyth Gold one-minute candle close, in USD per troy ounce.',
        # Gold is a candle-close contract, not BTC's final-minute average.
        'defaults': {'min_seconds': 60},
    },
}


def metadata(asset):
    return {key: value for key, value in ASSETS[asset].items() if key not in ('feed', 'defaults')}
