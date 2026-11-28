"""Consistent read-only research snapshot of observed books, scores and past priors."""
import json
import sqlite3
import zlib
from collections import Counter,defaultdict
from pathlib import Path

from .data import instant
from .markets import fee_terms,sweep
from desk.model import normalize_book


def load_paths(directory):
    db=sqlite3.connect((Path(directory)/'recordings.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row;paths=defaultdict(list);excluded=Counter();fees={}
    try:
        db.execute('BEGIN')
        games={r['game_id']:dict(r) for r in db.execute('SELECT * FROM games')}
        finals={}
        for r in db.execute("SELECT * FROM scores WHERE state='post' AND status_name='STATUS_FINAL' ORDER BY observed_at,id"):
            if r['home_score']!=r['away_score']:
                outcome=int(r['home_score']>r['away_score'])
                if r['game_id'] not in finals or finals[r['game_id']]['home_win']!=outcome:
                    finals[r['game_id']]={'home_win':outcome,'at':r['observed_at']}
        priors={}
        for r in db.execute("SELECT f.*,p.body FROM forecasts f JOIN payloads p ON p.hash=f.payload_hash WHERE kind='issued_pregame' ORDER BY recorded_at DESC,id DESC"):
            gid=r['game_id'];game=games.get(gid)
            if gid in priors or not game or instant(r['recorded_at'])>instant(game['tipoff']) or instant(r['as_of'])>instant(game['tipoff']):continue
            raw=json.loads(zlib.decompress(r['body']))
            if raw.get('home')!=game['home'] or raw.get('away')!=game['away']:continue
            probability=(raw.get('prediction') or {}).get('home_win_probability')
            if isinstance(probability,(int,float)) and 0<probability<1:priors[gid]={'probability':probability,'at':r['recorded_at'],'features':raw.get('features') or {}}
        query='''SELECT b.*,s.state,s.period,s.clock_seconds,s.home_score,s.away_score,s.possession,
            g.home,g.away,g.date,p.body,sp.body series_body,sr.received_at series_at FROM books b
            JOIN scores s ON s.id=b.score_id JOIN games g ON g.game_id=b.game_id JOIN markets m ON m.ticker=b.ticker
            JOIN requests r ON r.id=b.request_id JOIN payloads p ON p.hash=r.payload_hash
            LEFT JOIN requests sr ON sr.id=b.series_request_id LEFT JOIN payloads sp ON sp.hash=sr.payload_hash
            WHERE m.yes_team=g.home AND b.quality='ok' AND s.state='in' ORDER BY b.game_id,b.observed_at,b.id'''
        for r in db.execute(query):
            try:
                if r['period'] not in range(1,5) or r['clock_seconds'] is None:excluded['unsupported_clock']+=1;continue
                if r['score_age_seconds'] is None or not 0<=r['score_age_seconds']<=30:excluded['stale_score']+=1;continue
                if not r['series_at'] or instant(r['series_at'])>instant(r['observed_at']):excluded['future_or_missing_fee_metadata']+=1;continue
                if r['series_request_id'] not in fees:fees[r['series_request_id']]=fee_terms(json.loads(zlib.decompress(r['series_body'])).get('series',{})) if r['series_body'] else None
                multiplier=fees[r['series_request_id']]
                if multiplier is None:excluded['unknown_fees']+=1;continue
                book=normalize_book(json.loads(zlib.decompress(r['body'])))
                if any(book[s+'_bids'] and book[s+'_asks'] and book[s+'_bids'][0]['price']>book[s+'_asks'][0]['price'] for s in ('yes','no')):excluded['crossed_book']+=1;continue
                liquidity={}
                for side in ('yes','no'):
                    bid_q=sum(level['quantity'] for level in book[side+'_bids'][:3]);ask_q=sum(level['quantity'] for level in book[side+'_asks'][:3])
                    liquidity[side]=(bid_q-ask_q)/(bid_q+ask_q) if bid_q+ask_q else 0.
                fills={side:{str(q):{'buy':sweep(book[side+'_asks'],q,multiplier),'sell':sweep(book[side+'_bids'],q,multiplier)} for q in (5,10)} for side in ('yes','no')}
                paths[r['game_id']].append({'at':r['observed_at'],'time':instant(r['observed_at']).timestamp(),'day':r['date'],
                    'state':r['state'],'period':r['period'],'clock':r['clock_seconds'],'home_score':r['home_score'],'away_score':r['away_score'],
                    'home':r['home'],'away':r['away'],'possession':r['possession'],'score_age':r['score_age_seconds'],'multiplier':multiplier,'liquidity':liquidity,'fills':fills})
            except (ValueError,TypeError,KeyError,zlib.error):excluded['invalid_observation']+=1
        return dict(paths),priors,finals,dict(excluded)
    finally:db.close()
