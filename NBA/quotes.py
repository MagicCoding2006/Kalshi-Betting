"""Read-only local market quotes shared by the dashboard and risk planner."""
import json
import sqlite3
import zlib
from pathlib import Path

from .data import instant
from .markets import fee_terms,market_view,match_market
from desk.model import normalize_book


def load_quotes(directory,now):
    path=Path(directory)/'recordings.sqlite3'
    if not path.exists():return {},{}
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN')
        contracts={r['ticker']:dict(r) for r in db.execute('SELECT * FROM markets WHERE game_id IS NOT NULL')}
        quotes={}
        for row in db.execute('''SELECT b.*,r.payload_hash,r.error,m.yes_team,m.status,s.state score_state,
            sr.payload_hash series_hash,sr.received_at series_observed_at FROM markets m
            JOIN books b ON b.id=(SELECT max(id) FROM books WHERE ticker=m.ticker) JOIN requests r ON b.request_id=r.id
            LEFT JOIN scores s ON b.score_id=s.id LEFT JOIN requests sr ON b.series_request_id=sr.id'''):
            value=dict(row);age=(now-instant(row['observed_at'])).total_seconds()
            if row['payload_hash'] and not row['error'] and row['quality'] in ('ok','score_stale_or_missing') and -5<=age<=90:
                raw=json.loads(zlib.decompress(db.execute('SELECT body FROM payloads WHERE hash=?',(row['payload_hash'],)).fetchone()[0]))
                value['book']=normalize_book(raw);value['payload']=raw
            value['multiplier']=None
            if row['series_hash'] and 0<=(now-instant(row['series_observed_at'])).total_seconds()<=3600:
                raw=json.loads(zlib.decompress(db.execute('SELECT body FROM payloads WHERE hash=?',(row['series_hash'],)).fetchone()[0]))
                value['multiplier']=fee_terms(raw.get('series',{}))
            quotes[row['ticker']]=value
        return quotes,contracts
    finally:db.close()


def contract_matches_game(ticker,yes_team,game):
    matched=match_market({'ticker':ticker,'event_ticker':ticker.rsplit('-',1)[0],
        'market_type':'binary','status':'active'},[game])
    return bool(matched and matched[1]==yes_team)


def overlay_quotes(result,now,quotes,contracts):
    games={g['game_id']:g for g in result['games'] if g.get('game_id')}
    for ticker,quote in quotes.items():
        game=games.get(quote['game_id']);contract=contracts.get(ticker,{})
        if not game or not contract_matches_game(ticker,quote['yes_team'],game):continue
        existing=next((m for m in game['markets'] if m['ticker']==ticker),None)
        stamp=quote['observed_at'];old_stamp=existing.get('quote_received_at') if existing else None
        if old_stamp and instant(stamp)<instant(old_stamp):continue
        market=existing or {'ticker':ticker,'yes_team':quote['yes_team'],'candidates':[]}
        market.update(quote_source='local_recording',market_status=contract.get('status'),
                      settlement_result=contract.get('result'),settlement_value=contract.get('settlement_value'))
        if quote.get('book'):
            series={'fee_type':'quadratic','fee_multiplier':quote['multiplier']} if quote['multiplier'] is not None else {}
            market.update(market_view(game,quote['yes_team'],quote['payload'],series,stamp,result.get('quantity',10),
                result.get('slippage_buffer',.005),result.get('min_edge',.03),contract.get('status')))
        else:
            # Never recycle a model edge or mark an old/failed book as fresh.
            market.update(quote_received_at=stamp,error=quote.get('error') or ('Book expired' if quote.get('payload_hash') else 'Order book unavailable'),
                          quotes={key:quote.get(key) for key in ('yes_bid','yes_ask','no_bid','no_ask')},candidates=[])
        market['quote_age_seconds']=(now-instant(stamp)).total_seconds()
        if existing is None:game['markets'].append(market)


def coverage(result,rows,now,source=None):
    from zoneinfo import ZoneInfo
    day=now.astimezone(ZoneInfo('America/Indiana/Indianapolis')).date().isoformat()
    source_rows=source['games'] if source and source.get('games') is not None else [r for r in rows if r['date']==day]
    expected={r['game_id'] for r in source_rows if r['date']==day}
    today=[g for g in result['games'] if g.get('date')==day]
    actual={g['game_id']:g for g in today if g.get('game_id')}
    mismatched={r['game_id'] for r in source_rows if r['game_id'] in actual and (r['home'],r['away'])!=(actual[r['game_id']]['home'],actual[r['game_id']]['away'])}
    current=lambda m: bool(m.get('quote_received_at') and not m.get('error') and m.get('market_status','active') in ('active','open')
        and -5<=(now-instant(m['quote_received_at'])).total_seconds()<=result.get('quote_max_age_seconds',90)
        and any((m.get('quotes') or {}).get(key) is not None for key in ('yes_bid','yes_ask','no_bid','no_ask')))
    received=source['received_at'] if source else max((r['score_observed_at'] for r in rows if r['date']==day),default=None)
    verified=bool(received and 0<=(now-instant(received)).total_seconds()<=120 and not (source or {}).get('error'))
    latest=max((m['quote_received_at'] for g in today for m in g['markets'] if current(m)),default=None)
    latest_any=max((m['quote_received_at'] for g in result['games'] for m in g['markets'] if current(m)),default=None)
    return {'day':day,'source_games':len(expected) if received else None,'board_games':len(today),
        'matched_source_games':len((expected&set(actual))-mismatched),'mismatched_source_game_ids':sorted(mismatched),
        'unexpected_board_game_ids':sorted(set(actual)-expected) if source and source.get('games') is not None else [],
        'missing_source_game_ids':sorted(expected-{g['game_id'] for g in today}),
        'games_with_markets':sum(bool(g['markets']) for g in today),'games_with_fresh_quotes':sum(any(current(m) for m in g['markets']) for g in today),
        'source_received_at':received,'source_current':verified,'source_error':(source or {}).get('error'),
        'source_exclusions':(source or {}).get('excluded',[]),'latest_quote_received_at':latest,'latest_any_quote_received_at':latest_any}

