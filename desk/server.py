#!/usr/bin/env python3
"""Shared local research desk. Public GETs only; all fills are SQLite paper fills."""
import argparse
import csv
import errno
import io
import json
import logging
import math
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path

from .model import DEFAULTS, evaluate, fee, normalize_book, realized_volatility, return_statistics, timestamp, validate_config

from .assets import ASSETS, metadata

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
KALSHI = 'https://external-api.kalshi.com/trade-api/v2'
LOG = logging.getLogger('signal')


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Signal-Desk/3.0', 'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def liquidation(levels, quantity):
    """Full-position executable VWAP proceeds. None when visible depth is insufficient."""
    remaining, proceeds = quantity, 0.0
    for level in levels:
        count = min(remaining, level['quantity'])
        proceeds += count * level['price']
        remaining -= count
        if remaining <= 1e-8:
            return proceeds
    return None


def market_quote(book):
    """YES implied probability from this book snapshot, never from a later quote."""
    def best(key):
        levels = book.get(key, [])
        price = levels[0].get('price') if levels else None
        return price if isinstance(price, (int, float)) and math.isfinite(price) and 0 <= price <= 1 else None
    bid, ask = best('yes_bids'), best('yes_asks')
    midpoint = (bid + ask) / 2 if bid is not None and ask is not None and bid <= ask + 1e-8 else None
    return {'market_probability': midpoint, 'yes_bid': bid, 'yes_ask': ask}


class Engine:
    def __init__(self, db_path, asset='btc'):
        if asset not in ASSETS:
            raise ValueError('Unknown strategy: ' + str(asset))
        self.asset, self.spec = asset, ASSETS[asset]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                ticker TEXT, action TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
                side TEXT NOT NULL, quantity INTEGER NOT NULL, entry REAL NOT NULL, cost REAL NOT NULL,
                entry_fee REAL NOT NULL, opened REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                closed REAL, proceeds REAL, pnl REAL, close_reason TEXT);
            CREATE TABLE IF NOT EXISTS paper_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                archived REAL NOT NULL, cash REAL NOT NULL, config TEXT NOT NULL, positions TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS forecasts (ticker TEXT PRIMARY KEY, close_time REAL NOT NULL,
                probability REAL NOT NULL, created REAL NOT NULL, result TEXT, settled REAL);
        ''')
        bound = self.db.execute("SELECT value FROM settings WHERE key='asset'").fetchone()
        prior = self.db.execute('SELECT ticker FROM decisions WHERE ticker IS NOT NULL LIMIT 1').fetchone()
        if (bound and bound[0] != asset) or (not bound and prior and not prior[0].startswith(self.spec['series'] + '-')):
            self.db.close()
            raise ValueError('Database belongs to a different strategy; use separate database paths')
        row = self.db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        self.config = {**DEFAULTS, **self.spec['defaults'], **(json.loads(row['value']) if row else {})}
        row = self.db.execute("SELECT value FROM settings WHERE key='cash'").fetchone()
        self.cash = float(row['value']) if row else self.config['bankroll']
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.auto = False  # Explicit opt-in after every server restart.
        self.market, self.book, self.reference, self.settlement_reference = {}, {}, {}, {}
        self.candle_cache, self.candle_cache_time = [], 0
        self.spot_history = []
        self.return_stats = {'drift': None, 'drift_standard_error': None, 'count': 0}
        self.quote_samples = {'yes': [], 'no': []}
        self.previous_quote = {}
        self.quote_ticker = None
        self.book_time = 0
        self.book_error = None
        self._research = None
        self._outcome_system = None
        self._research_last_candle = 0
        self.research_path = str(db_path) + ".research.sqlite3"
        self.series, self.event = {}, {}
        self.fee_supported = False
        self.fee_multiplier = 1.0
        self.volatility, self.return_count = None, 0
        self.last_update, self.error, self.last_settle = 0, None, 0
        self.decision = {'action': 'WAIT', 'reason': 'Connecting to public market data', 'blockers': [], 'probability': None, 'sizing': None, 'candidates': []}
        self.pool = ThreadPoolExecutor(max_workers=5)
        self.persist()

    def persist(self):
        self.db.executemany('INSERT OR REPLACE INTO settings VALUES (?, ?)',
                            [('asset', self.asset), ('config', json.dumps(self.config)), ('cash', str(self.cash))])
        self.db.commit()

    def positions(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM positions WHERE status='open' OR id IN (SELECT id FROM positions WHERE status='closed' ORDER BY id DESC LIMIT 200) ORDER BY id DESC")]

    def portfolio(self):
        positions = self.positions()
        exposed, marked, realized = 0.0, 0.0, 0.0
        for p in positions:
            if p['status'] == 'open':
                exposed += p['cost']
                proceeds = None
                if p['ticker'] == self.market.get('ticker') and time.time() - self.book_time <= 10:
                    proceeds = liquidation(self.book.get(p['side'] + '_bids', []), p['quantity'])
                # Unavailable book marks at zero: never invent profitable marks.
                exit_fee = fee(p['quantity'], proceeds / p['quantity'], self.fee_multiplier) if proceeds is not None else 0
                p['mark_value'] = max(0, proceeds - exit_fee) if proceeds is not None else 0
                p['mark_available'] = proceeds is not None
                p['unrealized_pnl'] = p['mark_value'] - p['cost'] if proceeds is not None else None
                marked += p['mark_value']
        # All closed rows count toward P&L, even beyond the UI's 200-position window.
        realized = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM positions WHERE status='closed'").fetchone()[0]
        return {'initial_balance': self.config['bankroll'], 'cash': self.cash, 'equity': self.cash + marked,
                'exposure': exposed, 'realized_pnl': realized, 'positions': positions,
                'open_count': sum(p['status'] == 'open' for p in positions)}

    def compute(self):
        portfolio = self.portfolio()
        sigma = self.config['manual_volatility'] if self.config['volatility_mode'] == 'manual' else self.volatility
        effective = dict(self.config)
        effective['effective_drift'] = (self.return_stats.get('drift') if self.config['drift_mode'] == 'historical'
                                        else 0.0 if self.config['drift_mode'] == 'neutral' else self.config['drift'])
        if self.config['slippage_mode'] == 'observed':
            effective['slippage_by_side'] = {side: self.quote_buffer(side)['value'] for side in ('yes', 'no')}
        self.decision = evaluate(self.market, self.book, self.reference.get('price'), self.reference.get('time', 0),
                                 self.book_time, sigma, effective, time.time(), portfolio['equity'],
                                 self.cash, portfolio['exposure'], self.fee_multiplier, self.fee_supported)
        if self.error:
            self.decision['blockers'].append('Market feed error: ' + self.error)
        if self.market.get('ticker') and self.db.execute('SELECT 1 FROM positions WHERE ticker=? LIMIT 1', (self.market['ticker'],)).fetchone():
            self.decision['blockers'].append('This market was already traded; one paper entry per window')
        if self.decision['blockers']:
            self.decision['action'] = 'WAIT'
            self.decision['reason'] = '; '.join(self.decision['blockers'])
        return self.decision

    def observe_book(self, ticker, book, observed):
        if ticker != self.quote_ticker:
            self.quote_samples, self.previous_quote = {'yes': [], 'no': []}, {}
            self.quote_ticker = ticker
        for side in ('yes', 'no'):
            asks = book.get(side + '_asks', [])
            previous = self.previous_quote.get(side)
            if asks and previous and .2 <= observed - previous['time'] <= 10:
                self.quote_samples[side].append({'time': observed, 'change': max(0, asks[0]['price'] - previous['price'])})
            self.quote_samples[side] = [s for s in self.quote_samples[side] if observed - s['time'] <= 600][-1200:]
            self.previous_quote[side] = {'time': observed, 'price': asks[0]['price']} if asks else None

    def quote_buffer(self, side):
        samples = [s['change'] for s in self.quote_samples[side] if time.time() - s['time'] <= 600]
        return {'value': sorted(samples)[math.ceil(.95 * len(samples)) - 1] if len(samples) >= 10 else None,
                'samples': len(samples), 'source': '95th percentile of adverse ask changes between REST polls'}

    def input_status(self):
        settled = self.db.execute('SELECT probability,result FROM forecasts WHERE result IS NOT NULL').fetchall()
        return {'volatility': {'value': self.decision.get('volatility'), 'mode': self.config['volatility_mode'],
                               'samples': self.return_count, 'source': self.spec['returns_source'],
                               'start': self.return_stats.get('start'), 'end': self.return_stats.get('end')},
                'drift': {'value': self.decision.get('drift'), 'mode': self.config['drift_mode'], **self.return_stats},
                'execution_buffer': {side: self.quote_buffer(side) for side in ('yes', 'no')},
                'calibration': {'resolved_markets': len(settled),
                                'pending_markets': self.db.execute('SELECT COUNT(*) FROM forecasts WHERE result IS NULL').fetchone()[0],
                                'brier_score': sum((r['probability'] - (r['result'] == 'yes')) ** 2 for r in settled) / len(settled) if settled else None,
                                'status': 'Unvalidated: collecting official outcomes' if len(settled) < 30 else 'Diagnostic only; haircut and entry threshold remain explicit policy choices'},
                'policies': {'min_edge': self.config['min_edge'], 'probability_haircut': self.config['probability_haircut'],
                             'min_seconds': self.config['min_seconds'], 'label': 'User risk policies, not measured exchange data'}}

    def log(self, action=None, reason=None):
        d = self.decision
        payload = {'asset': self.asset, **market_quote(self.book), 'time': time.time(), 'ticker': self.market.get('ticker'), 'action': action or d['action'],
                   'reason': reason or d['reason'], 'probability': d.get('probability'),
                   'spot': self.reference.get('price'), 'source': self.reference.get('source'),
                   'source_time': self.reference.get('time'), 'book_time': self.book_time,
                   'strike': self.market.get('floor_strike'), 'seconds_left': d.get('seconds_left'),
                   'volatility': d.get('volatility'), 'sizing': d.get('sizing'), 'candidates': d.get('candidates'),
                   'blockers': d.get('blockers'), 'book': self.book, 'config': dict(self.config),
                   'fee_multiplier': self.fee_multiplier, 'cash': self.cash, 'auto_paper': self.auto,
                   'model_version': 'digital-gbm-observed-v2', 'drift': d.get('drift'),
                   'input_status': self.input_status(),
                   'settlement_reference': {k: v for k, v in self.settlement_reference.items() if k != 'points'}}
        self.db.execute('INSERT INTO decisions(time,ticker,action,payload) VALUES(?,?,?,?)',
                        (payload['time'], payload['ticker'], payload['action'], json.dumps(payload, allow_nan=False)))
        self.db.execute('DELETE FROM decisions WHERE id <= (SELECT COALESCE(MAX(id),0)-5000 FROM decisions)')
        self.db.commit()

    def open_paper(self):
        with self.lock:
            self.compute()
            if not self.decision['action'].startswith('BUY '):
                raise ValueError(self.decision['reason'])
            s = self.decision['sizing']
            self.cash -= s['cost']
            self.db.execute('INSERT INTO positions(ticker,side,quantity,entry,cost,entry_fee,opened) VALUES(?,?,?,?,?,?,?)',
                            (self.market['ticker'], s['side'], s['contracts'], s['ask'], s['cost'], s['fee'], time.time()))
            self.persist()
            self.log('PAPER BUY ' + s['side'].upper(), f"Simulated {s['contracts']} contracts at {s['ask']:.4f}; fee ${s['fee']:.2f}. Top-of-book fill assumption.")
            self.compute()

    def close_paper(self, position_id, reason='Manual paper exit'):
        with self.lock:
            p = self.db.execute("SELECT * FROM positions WHERE id=? AND status='open'", (position_id,)).fetchone()
            if not p:
                raise ValueError('Open position not found')
            if p['ticker'] != self.market.get('ticker') or time.time() - self.book_time > 10:
                raise ValueError('No fresh order book for this position; await official settlement')
            if self.market.get('status') not in ('active', 'open') or timestamp(self.market.get('close_time')) <= time.time():
                raise ValueError('Market is closed; await official settlement')
            proceeds = liquidation(self.book.get(p['side'] + '_bids', []), p['quantity'])
            if proceeds is None:
                raise ValueError('Insufficient visible bid depth for a complete exit')
            # VWAP quadratic fee upper-bounds sum of level fees; cent-round conservatively.
            proceeds -= fee(p['quantity'], proceeds / p['quantity'], self.fee_multiplier)
            self.finish_position(p, proceeds, reason)
            self.log('PAPER SELL ' + p['side'].upper(), reason + f"; net P&L ${proceeds - p['cost']:.2f}")
            self.compute()

    def finish_position(self, position, proceeds, reason):
        self.cash += proceeds
        self.db.execute("UPDATE positions SET status='closed',closed=?,proceeds=?,pnl=?,close_reason=? WHERE id=?",
                        (time.time(), proceeds, proceeds - position['cost'], reason, position['id']))
        self.persist()

    def settle(self):
        with self.lock:
            open_positions = [p for p in self.positions() if p['status'] == 'open']
        for p in open_positions:
            try:
                market = get_json(KALSHI + '/markets/' + urllib.parse.quote(p['ticker'], safe=''))['market']
                if market.get('status') in ('settled', 'finalized') and market.get('result') in ('yes', 'no'):
                    with self.lock:
                        current = self.db.execute("SELECT * FROM positions WHERE id=? AND status='open'", (p['id'],)).fetchone()
                        if current:
                            self.finish_position(current, p['quantity'] if p['side'] == market['result'] else 0, 'Official Kalshi result: ' + market['result'].upper())
                            self.log('SETTLED', f"Position #{p['id']} ({p['ticker']}) settled from official result: {market['result'].upper()}")
            except Exception as exc:
                LOG.warning('Settlement check unavailable: %s', exc)

    def settle_forecasts(self):
        with self.lock:
            pending = self.db.execute('SELECT ticker FROM forecasts WHERE result IS NULL AND close_time<? ORDER BY close_time LIMIT 10', (time.time() - 30,)).fetchall()
        for row in pending:
            try:
                market = get_json(KALSHI + '/markets/' + urllib.parse.quote(row['ticker'], safe=''))['market']
                if market.get('status') in ('settled', 'finalized') and market.get('result') in ('yes', 'no'):
                    with self.lock:
                        self.db.execute('UPDATE forecasts SET result=?,settled=? WHERE ticker=? AND result IS NULL',
                                        (market['result'], time.time(), row['ticker']))
                        self.db.commit()
            except Exception as exc:
                LOG.warning('Forecast result unavailable: %s', exc)

    def fetch_settlement_reference(self, market):
        try:
            data = get_json(KALSHI + '/live_data/events/' + urllib.parse.quote(market['event_ticker'], safe='') + '?range=1h')['live_data']
            details = data['details']
            points = sorted(({'time': p['t'] / 1000, 'price': float(p['v'])} for p in details['timeseries']), key=lambda p: p['time'])
            if data.get('is_historical') or not points:
                raise ValueError('Reference chart is historical or empty')
            return {'price': points[-1]['price'], 'time': points[-1]['time'], 'source': 'Kalshi reference chart',
                    'points': points[-3600::10]}
        except Exception as primary_error:
            return {'error': str(primary_error), 'points': [], 'source': 'Kalshi reference chart'}

    def fetch_reference(self, market):
        return self.spec['feed'](self, market, get_json)

    def tick(self):
        now = time.time()
        result = get_json(KALSHI + '/markets?' + urllib.parse.urlencode({'series_ticker': self.spec['series'], 'status': 'open', 'min_close_ts': int(now), 'limit': 100}))
        markets = [m for m in result['markets'] if m.get('status') in ('active', 'open') and timestamp(m.get('open_time')) <= now < timestamp(m.get('close_time'))]
        if not markets:
            raise ValueError('No active ' + self.spec['series'] + ' contract returned; waiting for the next window')
        market = min(markets, key=lambda m: timestamp(m['close_time']))
        changed = market['ticker'] != self.market.get('ticker')
        book_future = self.pool.submit(get_json, KALSHI + '/markets/' + urllib.parse.quote(market['ticker'], safe='') + '/orderbook?depth=100')
        reference_future = self.pool.submit(self.fetch_reference, market)
        settlement_future = reference_future if self.asset == 'gold' else self.pool.submit(self.fetch_settlement_reference, market)
        event_future = self.pool.submit(get_json, KALSHI + '/events/' + urllib.parse.quote(market['event_ticker'], safe='')) if changed else None
        series_future = self.pool.submit(get_json, KALSHI + '/series/' + self.spec['series']) if changed or not self.series else None
        book = normalize_book(book_future.result())
        book_time = time.time()
        reference = reference_future.result()
        settlement_reference = {k: v for k, v in settlement_future.result().items() if k != 'candles'}
        # All futures are awaited before publishing one coherent state.
        event = event_future.result()['event'] if event_future else self.event
        series = series_future.result()['series'] if series_future else self.series
        multiplier = event.get('fee_multiplier_override')
        multiplier = series.get('fee_multiplier', 1) if multiplier is None else multiplier
        kind = event.get('fee_type_override') or series.get('fee_type')
        volatility, count = realized_volatility(reference['candles'], time.time())
        with self.lock:
            if market['ticker'] == self.market.get('ticker') and self.book_time > book_time:
                book, book_time = self.book, self.book_time
            self.market, self.book, self.reference, self.event, self.series = market, book, reference, event, series
            self.settlement_reference = settlement_reference
            self.book_time, self.last_update, self.error = book_time, time.time(), None
            self.volatility, self.return_count = volatility, count
            self.return_stats = return_statistics(reference['candles'], time.time())
            self.observe_book(market['ticker'], book, book_time)
            self.fee_multiplier = float(multiplier)
            self.fee_supported = kind == 'quadratic' and math.isfinite(self.fee_multiplier) and self.fee_multiplier >= 0
            self.compute()
            if (not self.error and 600 <= self.decision['seconds_left'] <= 900
                    and self.decision['probability'] is not None and time.time() - reference['time'] <= 15
                    and not self.decision['blockers']):
                self.db.execute('INSERT OR IGNORE INTO forecasts(ticker,close_time,probability,created) VALUES(?,?,?,?)',
                                (market['ticker'], timestamp(market['close_time']), self.decision['probability'], time.time()))
            if self.auto:
                self.manage_positions()
                self.compute()
                if self.decision['action'].startswith('BUY '):
                    self.open_paper()
            self.log()
        if self._research is not None:
            completed = [{'ts': c['time'] + 60, 'close': c['close'], 'high': c.get('high', c['close']), 'low': c.get('low', c['close'])}
                         for c in reference['candles'] if self._research_last_candle < c['time'] + 60 <= time.time()]
            if completed:
                self.research.store.save_candles(completed)
                self._research_last_candle = max(c['ts'] for c in completed)
        if time.time() - self.last_settle > 30:
            self.last_settle = time.time()
            self.settle()
            self.settle_forecasts()

    def manage_positions(self):
        p_yes = self.decision.get('probability')
        if p_yes is None or self.decision['blockers'] and any('stale' in b or 'feed error' in b for b in self.decision['blockers']):
            return
        for p in self.portfolio()['positions']:
            if p['status'] != 'open' or p['ticker'] != self.market.get('ticker') or p['unrealized_pnl'] is None:
                continue
            ret = p['unrealized_pnl'] / p['cost']
            fair = p_yes if p['side'] == 'yes' else 1 - p_yes
            asks = self.book.get(p['side'] + '_bids', [])
            bid = asks[0]['price'] if asks else 0
            reason = ('Take-profit threshold reached' if ret >= self.config['take_profit'] else
                      'Stop-loss threshold reached' if ret <= -self.config['stop_loss'] else
                      'Market bid has caught up with model fair value' if bid >= fair and self.decision.get('seconds_left', 0) >= self.config['min_seconds'] else None)
            if reason:
                try:
                    self.close_paper(p['id'], reason)
                except ValueError:
                    pass

    @property
    def research(self):
        with self.lock:
            if self._research is None:
                from .research.lab import Lab
                self._research = Lab(self.research_path, self.asset)
            return self._research

    @property
    def outcome_system(self):
        if self.asset != 'btc':
            raise ValueError('BTC outcome research is BTC-specific')
        with self.lock:
            if self._outcome_system is None:
                from BTC.system.service import System
                self._outcome_system = System(self)
            return self._outcome_system

    def run_outcome_system(self):
        self.outcome_system.run(get_json)

    def run_research(self):
        self.research.run(get_json)

    def run_book(self):
        failures = 0
        while not self.stop.is_set():
            started = time.monotonic()
            with self.lock:
                ticker = self.market.get('ticker')
                active = ticker and timestamp(self.market.get('close_time')) > time.time()
            if not active:
                self.stop.wait(.5)
                continue
            try:
                book = normalize_book(get_json(KALSHI + '/markets/' + urllib.parse.quote(ticker, safe='') + '/orderbook?depth=100'))
                observed = time.time()
                with self.lock:
                    if ticker != self.market.get('ticker'):
                        continue  # Ignore an in-flight response for a previous window.
                    self.book, self.book_time, self.book_error = book, observed, None
                    self.last_update = observed
                    self.observe_book(ticker, book, observed)
                    self.compute()
                    if self.auto:
                        self.manage_positions()
                        self.compute()
                        if self.decision['action'].startswith('BUY '):
                            self.open_paper()
                    args = (book, ticker, observed, max(0, timestamp(self.market['close_time']) - observed),
                            dict(self.reference), self.market.get('floor_strike'),
                            self.fee_multiplier if self.fee_supported else None)
                self.research.store.record(*args)
                failures = 0
            except Exception as exc:
                failures += 1
                self.book_error = str(exc)
                LOG.warning('[%s] Fast book feed: %s', self.asset, exc)
            interval = min(8, .5 * 2**min(failures, 4))
            self.stop.wait(max(.01, interval - (time.monotonic() - started)))

    def close(self):
        if self._outcome_system is not None:
            self._outcome_system.close()
        if self._research is not None:
            self._research.close()
        self.db.close()

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                LOG.warning('[%s] Feed refresh: %s', self.asset, exc)
                with self.lock:
                    self.error = str(exc)
                    self.compute()
                    self.log()
            self.stop.wait(max(0.2, 3 - (time.monotonic() - started)))

    def state(self):
        with self.lock:
            self.compute()  # Freshness guards run on reads too; reads never create fills.
            rows = self.db.execute('SELECT id,payload FROM decisions ORDER BY id DESC LIMIT 100').fetchall()
            decisions = []
            for row in rows:
                d = json.loads(row['payload'])
                decisions.append({k: d.get(k) for k in ('time', 'ticker', 'action', 'reason', 'probability', 'spot', 'strike', 'volatility', 'sizing')}
                                 | market_quote(d.get('book', {})) | {'id': row['id']})
            history = self.db.execute('SELECT time,payload FROM decisions WHERE ticker=? ORDER BY id DESC LIMIT 300', (self.market.get('ticker', ''),)).fetchall()
            probability_points = []
            for row in reversed(history):
                d = json.loads(row['payload'])
                if d.get('probability') is not None:
                    probability_points.append({'time': row['time'], 'model': d['probability'],
                                               'market': market_quote(d.get('book', {}))['market_probability']})
            return {'asset': metadata(self.asset), 'market': self.market, 'book': self.book, 'book_time': self.book_time,
                    'reference': {k: v for k, v in self.reference.items() if k != 'candles'},
                    'settlement_reference': self.settlement_reference,
                    'last_update': self.last_update, 'server_time': time.time(), 'error': self.error,
                    'decision': self.decision, 'portfolio': self.portfolio(), 'config': self.config,
                    'auto_paper': self.auto, 'decisions': decisions, 'probability_history': probability_points,
                    'inputs': self.input_status(),
                    'paper_runs': [dict(r) for r in self.db.execute('SELECT id,archived,cash FROM paper_runs ORDER BY id DESC LIMIT 20')],
                    'volatility_returns': self.return_count, 'fee_multiplier': self.fee_multiplier,
                    'feed_mode': 'Public REST · 500 ms book target · 3 s reference/model refresh · exchange caches may lag',
                    'book_error': self.book_error}

    def update_config(self, update):
        with self.lock:
            config = validate_config(update, self.config)
            if config['bankroll'] != self.config['bankroll']:
                if self.db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]:
                    raise ValueError('Starting bankroll can only change before the first paper trade')
                self.cash = config['bankroll']
            self.config = config
            self.persist()
            self.compute()
            self.log('CONFIG', 'Strategy settings updated')

    def reset_paper(self):
        """Archive the current simulation atomically; keep model evidence and audit logs."""
        with self.lock:
            positions = [dict(r) for r in self.db.execute('SELECT * FROM positions ORDER BY id')]
            archived = time.time()
            config = {**self.config, 'bankroll': 100.0}
            with self.db:
                self.db.execute('INSERT INTO paper_runs(archived,cash,config,positions) VALUES(?,?,?,?)',
                                (archived, self.cash, json.dumps(self.config), json.dumps(positions)))
                self.db.execute('DELETE FROM positions')
                self.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                                    [('config', json.dumps(config)), ('cash', '100.0')])
            self.config, self.cash, self.auto = config, 100.0, False
            self.compute()
            self.log('RESET', f'Paper run archived with {len(positions)} positions. New run: $100 cash; bot paused. Decision history retained.')


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer's reverse DNS lookup can block startup on local macOS networks.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if args and '200' not in str(args):
            LOG.info(fmt, *args)

    def send_json(self, payload, status=200):
        data = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

    def engines(self):
        return getattr(self.server, 'engines', {'btc': getattr(self.server, 'engine', None)})

    def select_engine(self, url):
        values = urllib.parse.parse_qs(url.query, keep_blank_values=True).get('asset', ['btc'])
        if len(values) != 1 or values[0] not in self.engines():
            raise ValueError('Unknown or ambiguous strategy; choose btc or gold')
        return self.engines()[values[0]]

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({'error': 'Local access only'}, 403)
        url = urllib.parse.urlparse(self.path)
        if url.path == '/api/nba' or url.path.startswith('/api/nba/'):
            # Separate read-only research service, outside the BTC/Gold execution
            # registry. Optional ML dependencies never load during desk startup.
            from NBA.service import feature_catalog, model_report, public_snapshot, season_backtest, optimization_report, recording_status, paper_status
            routes = {'/api/nba': public_snapshot, '/api/nba/fair-prices': public_snapshot,
                      '/api/nba/model': model_report, '/api/nba/features': feature_catalog,
                      '/api/nba/backtest': season_backtest, '/api/nba/optimization': optimization_report,
                      '/api/nba/recordings': recording_status, '/api/nba/paper': paper_status}
            reader = routes.get(url.path)
            if reader is None:
                return self.send_json({'error': 'Not found'}, 404)
            try:
                return self.send_json(reader())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return self.send_json({'status': 'unavailable', 'error': str(exc)}, 503)
        if url.path == '/api/btc-system/swing':
            engine=self.engines().get('btc')
            if engine is None:return self.send_json({'error':'BTC strategy unavailable'},503)
            try:
                values=urllib.parse.parse_qs(url.query,keep_blank_values=True).get('target',[])
                if len(values)>1:raise ValueError('Choose one BTC target')
                target=float(values[0]) if values and values[0] else None
                return self.send_json(engine.outcome_system.swing_status(target))
            except (ValueError,TypeError,KeyError) as exc:return self.send_json({'error':str(exc)},400)
        if url.path == '/api/btc-system':
            engine = self.engines().get('btc')
            if engine is None:return self.send_json({'error':'BTC strategy unavailable'},503)
            return self.send_json(engine.outcome_system.status())
        if url.path == '/api/assets':
            assets = []
            for key, item in self.engines().items():
                with item.lock:
                    assets.append({**metadata(key), 'auto_paper': item.auto, 'cash': item.cash,
                                   'last_update': item.last_update, 'error': item.error,
                                   'action': item.compute()['action']})
            return self.send_json({'assets': assets})
        try:
            engine = self.select_engine(url)
        except ValueError as exc:
            return self.send_json({'error': str(exc)}, 400)
        if url.path == '/api/research':
            return self.send_json(engine.research.status())
        if url.path == '/api/research/export.csv':
            rows = engine.research.store.rows(100000)
            stream = io.StringIO()
            fields = ['id','ts','ticker','mid','bid','ask','spread','bid_qty','ask_qty','imbalance','microprice','momentum','seconds_left','spot','spot_time','strike','fee_multiplier']
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader();writer.writerows(rows)
            data = stream.getvalue().encode()
            self.send_response(200)
            self.send_header('Content-Type','text/csv')
            self.send_header('Content-Disposition','attachment; filename="'+engine.asset+'-research.csv"')
            self.send_header('Content-Length',str(len(data)));self.end_headers()
            return self.wfile.write(data)
        if url.path == '/api/state':
            return self.send_json(engine.state())
        if url.path == '/api/health':
            return self.send_json({'app': 'signal-desk', 'running': True, 'feed_ok': not engine.error and bool(engine.last_update), 'paper_only': True})
        if url.path == '/api/paper/run':
            try:
                run_id = int(urllib.parse.parse_qs(url.query)['id'][0])
            except (KeyError, ValueError):
                return self.send_json({'error': 'Paper run id required'}, 400)
            with engine.lock:
                row = engine.db.execute('SELECT * FROM paper_runs WHERE id=?', (run_id,)).fetchone()
                result = dict(row) if row else None
                if result:
                    result['config'], result['positions'] = json.loads(result['config']), json.loads(result['positions'])
            return self.send_json(result or {'error': 'Archived run not found'}, 200 if result else 404)
        if url.path == '/api/decision':
            try:
                decision_id = int(urllib.parse.parse_qs(url.query)['id'][0])
            except (KeyError, ValueError):
                return self.send_json({'error': 'Decision id required'}, 400)
            with engine.lock:
                row = engine.db.execute('SELECT payload FROM decisions WHERE id=?', (decision_id,)).fetchone()
            if row:
                payload = json.loads(row[0])
                return self.send_json(payload | market_quote(payload.get('book', {})))
            return self.send_json({'error': 'Decision not found'}, 404)
        if url.path == '/api/export.csv':
            with engine.lock:
                rows = engine.db.execute('SELECT id,payload FROM decisions ORDER BY id DESC LIMIT 5000').fetchall()
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(['id', 'time_utc', 'ticker', 'action', 'reason', 'spot', 'strike', 'p_yes', 'kalshi_p_yes_midpoint', 'kalshi_yes_bid', 'kalshi_yes_ask', 'volatility', 'contracts', 'net_edge'])
            from datetime import datetime, timezone
            for row in rows:
                d = json.loads(row['payload'])
                s = d.get('sizing') or {}
                quote = market_quote(d.get('book', {}))
                writer.writerow([row['id'], datetime.fromtimestamp(d['time'], timezone.utc).isoformat(), d.get('ticker'), d['action'], d['reason'], d.get('spot'), d.get('strike'), d.get('probability'), quote['market_probability'], quote['yes_bid'], quote['yes_ask'], d.get('volatility'), s.get('contracts'), s.get('edge')])
            data = stream.getvalue().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="' + engine.asset + '-decisions.csv"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css'), '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                 '/btc-system': ('btc-system.html', 'text/html'), '/btc-system.js': ('btc-system.js', 'text/javascript'), '/btc-system.css': ('btc-system.css', 'text/css'),
                 '/nba': ('nba.html', 'text/html'), '/nba/': ('nba.html', 'text/html'),
                 '/nba.js': ('nba.js', 'text/javascript'), '/nba-risk.js': ('nba-risk.js', 'text/javascript'), '/nba.css': ('nba.css', 'text/css')}
        if url.path not in files:
            return self.send_json({'error': 'Not found'}, 404)
        name, content_type = files[url.path]
        data = (ROOT / 'static' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.valid_host():
            return self.send_json({'error': 'Local access only'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.send_json({'error': 'Cross-origin mutations rejected'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send_json({'error': 'application/json required'}, 415)
        try:
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > (100000 if url.path=='/api/nba/risk' else 10000):
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            if url.path=='/api/nba/risk':
                from NBA.risk import risk_plan
                try:return self.send_json(risk_plan(body))
                except (OSError,sqlite3.Error) as exc:return self.send_json({'error':str(exc)},503)
            if url.path=='/api/nba/stop-plan':
                from NBA.stops import stop_plan
                try:return self.send_json(stop_plan(body))
                except (OSError,sqlite3.Error) as exc:return self.send_json({'error':str(exc)},503)
            if url.path == '/api/btc-system/job':
                engine=self.engines().get('btc')
                if engine is None:raise ValueError('BTC strategy unavailable')
                engine.outcome_system.start_job(body.get('kind'),get_json)
                return self.send_json({'ok':True})
            engine = self.select_engine(url)
            if url.path == '/api/research/settings':
                engine.research.configure(body)
            elif url.path == '/api/research/job':
                engine.research.start_job(body.get('kind'), get_json)
            elif url.path == '/api/config':
                engine.update_config(body)
            elif url.path == '/api/auto':
                if type(body.get('enabled')) is not bool:
                    raise ValueError('enabled must be a boolean')
                with engine.lock:
                    engine.auto = body['enabled']
                    engine.log('ENGINE', 'Automatic paper trading ' + ('started' if engine.auto else 'paused'))
            elif url.path == '/api/paper/buy':
                engine.open_paper()
            elif url.path == '/api/paper/close':
                engine.close_paper(int(body['id']))
            elif url.path == '/api/paper/reset':
                engine.reset_paper()
            else:
                return self.send_json({'error': 'Not found'}, 404)
            return self.send_json({'ok': True})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json({'error': str(exc)}, 400)


def running_signal_desk(port):
    """Recognize this app, including existing servers predating the app marker."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f'http://127.0.0.1:{port}'
    try:
        with opener.open(base + '/api/health', timeout=1) as response:
            health = json.loads(response.read(8192))
        if not isinstance(health, dict) or health.get('running') is not True or health.get('paper_only') is not True:
            return False
        if health.get('app') == 'signal-desk':
            return True
        with opener.open(base + '/api/assets', timeout=1) as response:
            assets = json.loads(response.read(65536))
        return isinstance(assets, dict) and {item['id'] for item in assets.get('assets', [])} == {'btc', 'gold'}
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description='Signal Desk — BTC and Gold, one local paper-trading app')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--db', default=str(WORKSPACE / 'BTC' / '.data' / 'btc.sqlite3'), help='BTC database (legacy flag preserved)')
    parser.add_argument('--gold-db', default=str(WORKSPACE / 'GOLD' / '.data' / 'gold.sqlite3'))
    parser.add_argument('--no-btc-system', action='store_true', help='Disable the optional BTC exchange/outcome collectors')
    args = parser.parse_args()
    if Path(args.db).resolve() == Path(args.gold_db).resolve():
        parser.error('BTC and Gold require separate database paths')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Reserve the port before opening portfolios or starting another feed engine.
    try:
        server = LocalServer(('127.0.0.1', args.port), Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        if running_signal_desk(args.port):
            print(f'Signal Desk is already running at http://127.0.0.1:{args.port}', flush=True)
            print('Open that link in your browser. Only one server is needed for BTC and Gold.', flush=True)
            return
        print(f'Port {args.port} is being used by another program. Stop that program or choose another port with --port.', file=sys.stderr)
        raise SystemExit(1) from None
    engines, threads = {}, []
    try:
        engines['btc'] = Engine(args.db, 'btc')
        engines['gold'] = Engine(args.gold_db, 'gold')
        server.engines = engines
        for key, engine in engines.items():
            workers = [('reference', engine.run), ('book', engine.run_book), ('research', engine.run_research)]
            if key == 'btc' and not args.no_btc_system:workers.append(('btc-outcomes',engine.run_outcome_system))
            for label, target in workers:
                thread = threading.Thread(target=target, daemon=True, name=key + '-' + label)
                thread.start()
                threads.append(thread)
        print(f'Signal Desk — BTC + Gold: http://127.0.0.1:{server.server_address[1]}', flush=True)
        print('Independent paper portfolios, one server. Optional read-only market-data streams. Ctrl+C to stop.', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for engine in engines.values():
            engine.stop.set()
            if engine._research is not None:
                engine._research.stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=20)
        for engine in engines.values():
            engine.pool.shutdown(wait=True, cancel_futures=True)
            engine.close()


if __name__ == '__main__':
    main()
