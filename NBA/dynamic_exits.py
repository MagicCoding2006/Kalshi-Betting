"""Smoothed, stateful exit monitoring. Heuristic policy, not a fitted forecast or order."""
import hashlib
import json
import math
import sqlite3
import statistics
import zlib
from datetime import timedelta
from pathlib import Path

from .data import instant
from .markets import sweep
from desk.model import normalize_book

POLICY={'version':1,'update_seconds':60,'max_target_step':.01,'target_deadband':.02,
        'smoothing_seconds':90,'minimum_history_seconds':90,'maximum_gap_seconds':30}


def load_samples(directory,position,now,multiplier):
    path=Path(directory)/'recordings.sqlite3'
    if not path.exists() or multiplier is None:return []
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2);db.row_factory=sqlite3.Row
    try:
        rows=db.execute('''SELECT b.observed_at,b.score_age_seconds,s.state,s.period,s.clock_seconds,s.home_score,s.away_score,
          g.home,m.yes_team,p.body FROM books b JOIN scores s ON s.id=b.score_id JOIN games g ON g.game_id=b.game_id
          JOIN markets m ON m.ticker=b.ticker JOIN requests r ON r.id=b.request_id JOIN payloads p ON p.hash=r.payload_hash
          WHERE b.ticker=? AND b.observed_at>=? AND b.observed_at<=? AND b.quality='ok'
          ORDER BY b.observed_at,b.id''',(position['ticker'],(now-timedelta(minutes=5)).isoformat(),now.isoformat())).fetchall()
    finally:db.close()
    samples=[];side=position['side'];q=position['quantity']
    for row in rows:
        book=normalize_book(json.loads(zlib.decompress(row['body'])))
        bid=sweep(book[side+'_bids'],q,multiplier);ask=sweep(book[side+'_asks'],q,multiplier)
        if bid is None or ask is None or ask['vwap']<bid['vwap']:continue
        sign=1 if ((row['yes_team']==row['home'])==(side=='yes')) else -1
        samples.append({'at':row['observed_at'],'bid':bid['vwap'],'spread':ask['vwap']-bid['vwap'],
            'state':row['state'],'period':row['period'],'clock':row['clock_seconds'],
            'margin':sign*(row['home_score']-row['away_score']),'score_age':row['score_age_seconds']})
    return samples


def advance(position,config,now,samples,break_even=None):
    """Pure transition; retain original TP, limit live TP speed, never loosen a stop."""
    p=position;target=p['target_bid'];stop=p['stop_bid'];started=p.get('dynamic_started_at',p['opened_at'])
    signature=hashlib.sha256(json.dumps([p['ticker'],p['side'],p['quantity'],p['entry_price'],p['entry_fees'],
        p['opened_at'],started,stop,target,config['slippage'],POLICY['version']]).encode()).hexdigest()
    candidate=p.get('dynamic_exit_state')
    state={'signature':signature,'target':target,'stop':stop,'updated_at':started,'trigger':None}
    if isinstance(candidate,dict) and candidate.get('signature')==signature:
        try:
            if (isinstance(candidate.get('updated_at'),str) and all(isinstance(candidate.get(k),(float,int)) and not isinstance(candidate[k],bool) and math.isfinite(candidate[k]) and .001<=candidate[k]<1 for k in ('target','stop'))
                and candidate['stop']>=stop and candidate['target']>candidate['stop']
                and instant(started)<=instant(candidate['updated_at'])<=now
                and candidate.get('trigger') in (None,'stop','target')):state=dict(candidate)
        except (KeyError,TypeError,ValueError):pass
    data=sorted([s for s in samples if max(instant(started),now-timedelta(minutes=5))<=instant(s['at'])<=now],key=lambda s:instant(s['at']))
    result={'original_target':p.get('initial_target_bid',target),'original_stop':stop,'active_target':state['target'],'active_stop':state['stop'],
        'state':state,'status':'warming_up','reason':'Collecting at least 90 seconds of continuous full-position quotes.',
        'policy':POLICY,'probability':None,'basis':'Smoothed market and score heuristic; not a calibrated target-hit forecast.'}
    if not data or not -5<=(now-instant(data[-1]['at'])).total_seconds()<=30:
        result.update(status='frozen',reason='Fresh full-position quotes are unavailable. Moving levels are frozen.');return result
    current=data[-1];at=instant(current['at']);bid=current['bid']
    if at<instant(state['updated_at']):
        result.update(status='frozen',reason='An older quote cannot update the moving levels.');return result
    # Test existing levels BEFORE updating, so a target cannot move away from a hit.
    if not state['trigger']:
        for observed in data:
            if instant(observed['at'])<instant(state['updated_at']):continue
            if observed['bid']<=state['stop']+1e-9:state['trigger']='stop'
            elif observed['bid']>=state['target']-1e-9:state['trigger']='target'
            if state['trigger']:
                state['triggered_at']=observed['at'];break
    if state['trigger']:
        result.update(status='triggered',reason='Previously active '+state['trigger']+' was reached; exit rule remains triggered.');return result
    if current['state']!='in' or current['period'] not in range(1,5) or current.get('clock') is None or current.get('score_age') is None or current['score_age']>30:
        result.update(status='frozen',reason='Waiting for a fresh regulation-game score and clock.');return result
    # A gap resets the usable history; it must not masquerade as calm trading.
    segment=[current]
    for sample in reversed(data[:-1]):
        gap=(instant(segment[-1]['at'])-instant(sample['at'])).total_seconds()
        if gap>30:break
        if gap>0:segment.append(sample)
    segment.reverse()
    if len(segment)<6 or (at-instant(segment[0]['at'])).total_seconds()<90:return result
    if (at-instant(state['updated_at'])).total_seconds()<60:
        result.update(status='holding',reason='Holding the live levels until the next one-minute update.');return result
    recent=[s for s in segment if (at-instant(s['at'])).total_seconds()<=90]
    prior=[s for s in segment if 90<(at-instant(s['at'])).total_seconds()<=180]
    center=statistics.median(s['bid'] for s in recent)
    momentum=center-statistics.median(s['bid'] for s in prior) if prior else 0.
    moves=[abs(b['bid']-a['bid']) for a,b in zip(segment,segment[1:])]
    volatility=sorted(moves)[max(0,math.ceil(.9*len(moves))-1)]
    spread=statistics.median(s['spread'] for s in recent)
    margin_change=current['margin']-segment[0]['margin']
    late=current['period']==4 and current['clock']<=180
    cushion=min(.15,max(.02,2*volatility,1.5*spread))
    # Score trend is bounded to a 1-cent adjustment; price remains the main signal.
    trend=max(-.01,min(.01,.002*margin_change))
    desired=center+cushion+(max(-.02,min(.02,momentum))*.5)+trend
    if late:desired-=.01
    floor=max(.01,(break_even if break_even is not None else p['entry_price']+.04))
    desired=max(floor,min(.99,desired))
    desired=max(target-.10,min(target+.10,desired))
    desired=max(floor,desired)
    live_target=state['target']
    if abs(desired-live_target)>=.02-1e-9:
        live_target+=.01 if desired>live_target else -.01
    # Do not publish a lowered target below an already executable bid. Keep the prior rule.
    if live_target<=bid+1e-9:live_target=state['target']
    rounded=round(live_target,2)
    live_target=min(.99,max(state['stop']+.01,.01,max(state['target']-.01,min(state['target']+.01,rounded))))
    # Volatility-aware trailing stop: ratchet upward only, below executable bid and TP.
    distance=cushion*(.75 if late else 1.)
    desired_stop=max(stop,min(bid-.01,center-distance,live_target-.01))
    live_stop=max(state['stop'],min(state['stop']+.01,math.floor((desired_stop+1e-9)*100)/100))
    if live_stop>=bid or live_stop>=live_target:live_stop=state['stop']
    state.update(target=round(live_target,3),stop=round(live_stop,3),updated_at=current['at'])
    result.update(active_target=state['target'],active_stop=state['stop'],status='active',
        reason='One-minute update from median bids, observed volatility, spread, score trend and late-game clock.',
        inputs={'median_bid':center,'observed_move_p90':volatility,'median_spread':spread,'momentum':momentum,'score_margin_change':margin_change,'late_game':late})
    return result
