"""Offline target-before-stop trial. APIs read the saved report; never train on requests."""
import bisect
import json
import math
import sqlite3
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from .data import DATA, atomic_json, instant, utcnow
from .markets import fee_terms, sweep
from desk.model import normalize_book

POLICY={'quantity':10,'target_move':.10,'stop_move':.05,'hold_seconds':1200,
        'maximum_quote_gap_seconds':30,'slippage_per_leg':.005}
FEATURES=['entry_price','spread','score_margin','seconds_remaining','bid_change_60s','score_age_seconds']


def replay(points, side, team, game_id, day, dynamic=False):
    """Nonoverlapping entries per side; use the observation AFTER a trigger to sell."""
    rows=[];excluded=Counter();i=0;q=POLICY['quantity'];slip=POLICY['slippage_per_leg']
    times=[p['time'] for p in points]
    while i<len(points):
        p=points[i];buy=p['fills'][side]['buy'];bid=p['fills'][side]['sell']
        if p['state']!='in' or p['period'] not in range(1,5) or buy is None or bid is None or not .06<buy['vwap']<.89:
            i+=1;continue
        start=p['time'];deadline=start+POLICY['hold_seconds'];entry=buy['vwap'];j=i+1;trigger=None;exit_point=None;reason='incomplete_window';adverse=0.
        live_position={'ticker':game_id,'side':side,'quantity':q,'entry_price':entry,'entry_fees':buy['fee_upper_estimate'],
            'opened_at':p['at'],'stop_bid':max(.001,entry-POLICY['stop_move']),'target_bid':min(.999,entry+POLICY['target_move'])}
        direction=1 if side=='yes' else -1
        live_history=[{'at':p['at'],'bid':bid['vwap'],'spread':buy['vwap']-bid['vwap'],'state':p['state'],'period':p['period'],
            'clock':p['clock'],'margin':direction*(p['home_score']-p['away_score']),'score_age':p['score_age']}]
        while j<len(points):
            current=points[j];previous=points[j-1]
            if current['time']-previous['time']>POLICY['maximum_quote_gap_seconds']:
                reason='recording_gap';break
            sell=current['fills'][side]['sell']
            if sell is None:
                reason='insufficient_exit_depth';break
            adverse=max(adverse,entry-sell['vwap'])
            if trigger:
                if current['time']>deadline+POLICY['maximum_quote_gap_seconds']:
                    reason='late_exit';break
                exit_point=current;reason=trigger;break
            if dynamic:
                from .dynamic_exits import advance
                sign=1 if side=='yes' else -1
                current_buy=current['fills'][side]['buy']
                if current_buy is None:reason='insufficient_exit_depth';break
                live_history.append({'at':current['at'],'bid':sell['vwap'],'spread':current_buy['vwap']-sell['vwap'],
                    'state':current['state'],'period':current['period'],'clock':current['clock'],
                    'margin':sign*(current['home_score']-current['away_score']),'score_age':current['score_age']})
                break_even=entry+(buy['fee_upper_estimate']+sell['fee_upper_estimate'])/q+2*slip
                decision=advance(live_position,{'slippage':slip},instant(current['at']),live_history,break_even)
                live_position['dynamic_exit_state']=decision['state']
                trigger=decision['state'].get('trigger')
            else:
                if sell['vwap']<=entry-POLICY['stop_move']+1e-9:trigger='stop'
                elif sell['vwap']>=entry+POLICY['target_move']-1e-9:trigger='target'
            if not trigger and current['time']>=deadline:trigger='time'
            j+=1
        if exit_point:
            sell=exit_point['fills'][side]['sell'];cost=buy['price_cost']+buy['fee_upper_estimate']+q*slip
            net=sell['price_cost']-sell['fee_upper_estimate']-q*slip-cost
            prior=bisect.bisect_right(times,start-60)-1
            prior_bid=points[prior]['fills'][side]['sell'] if prior>=0 and start-points[prior]['time']<=90 else None
            sign=1 if side=='yes' else -1
            features=[entry,buy['vwap']-bid['vwap'],sign*(p['home_score']-p['away_score']),
                      (4-p['period'])*720+p['clock'],bid['vwap']-prior_bid['vwap'] if prior_bid else 0.,p['score_age']]
            rows.append({'game_id':game_id,'day':day,'team':team,'side':side,'entered_at':p['at'],
                         'exited_at':exit_point['at'],'outcome':reason,'target_hit':int(reason=='target'),
                         'net_pnl':net,'entry_cost':cost,'adverse_bid_move':adverse,'features':features})
        else:excluded[reason]+=1
        # Never overlap windows within the same contract side, including censored windows.
        i=max(i+1,bisect.bisect_right(times,deadline))
    return rows,excluded


def summarize(rows):
    games={r['game_id'] for r in rows};n=len(rows)
    return {'episodes':n,'games':len(games),'target_rate':sum(r['target_hit'] for r in rows)/n if n else None,
            'outcomes':dict(Counter(r['outcome'] for r in rows)),
            'mean_net_pnl':sum(r['net_pnl'] for r in rows)/n if n else None,
            'mean_adverse_bid_move':sum(r['adverse_bid_move'] for r in rows)/n if n else None}


def evaluate(rows):
    days=sorted({r['day'] for r in rows})
    if len(days)<3:return {'status':'insufficient_dates','reason':'Need at least three completed recording dates.'}
    cutoff=days[max(1,int(len(days)*.7))]
    train=[r for r in rows if r['day']<cutoff];test=[r for r in rows if r['day']>=cutoff]
    result={'status':'insufficient_games','test_from':cutoff,'train':summarize(train),'test':summarize(test),
            'train_game_ids':sorted({r['game_id'] for r in train}),'test_game_ids':sorted({r['game_id'] for r in test})}
    if len({r['game_id'] for r in train})<3 or len({r['game_id'] for r in test})<3 or len({r['target_hit'] for r in train})<2:return result
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    counts=Counter(r['game_id'] for r in train);weights=np.array([1/counts[r['game_id']] for r in train])
    weights*=len(train)/weights.sum()
    scaler=StandardScaler().fit([r['features'] for r in train],sample_weight=weights)
    model=LogisticRegression(C=.1,max_iter=1000).fit(scaler.transform([r['features'] for r in train]),[r['target_hit'] for r in train],sample_weight=weights)
    probs=model.predict_proba(scaler.transform([r['features'] for r in test]))[:,1]
    base=float(np.average([r['target_hit'] for r in train],weights=weights))
    test_counts=Counter(r['game_id'] for r in test);tw=np.array([1/test_counts[r['game_id']] for r in test]);y=np.array([r['target_hit'] for r in test])
    brier=float(np.average((probs-y)**2,weights=tw));baseline=float(np.average((base-y)**2,weights=tw))
    result.update(status='experimental_evaluation',training_game_warning='Exploratory fit only; very few independent games. Not a calibrated probability model.',brier_score=brier,baseline_brier_score=baseline,
                  improves_baseline=brier<baseline,baseline_target_rate=base,
                  coefficients=dict(zip(FEATURES,map(float,model.coef_[0]))),
                  note='Earlier dates train; later dates test. Each game has equal total weight. No parameters selected on the test set. No serving model or Kelly promotion.')
    return result


def run_trial(directory=DATA):
    directory=Path(directory);path=directory/'recordings.sqlite3'
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5);db.row_factory=sqlite3.Row
    groups=defaultdict(list);excluded=Counter();fee_cache={};book_count=0
    try:
        db.execute('BEGIN')
        # One home-YES market per game; its YES/NO cover both outcomes without duplicate opponent contracts.
        query='''SELECT b.*,s.state,s.period,s.clock_seconds,s.home_score,s.away_score,g.home,g.away,g.date,
          p.body,sp.body series_body FROM books b JOIN scores s ON s.id=b.score_id JOIN games g ON g.game_id=b.game_id
          JOIN markets m ON m.ticker=b.ticker JOIN requests r ON r.id=b.request_id JOIN payloads p ON p.hash=r.payload_hash
          LEFT JOIN requests sr ON sr.id=b.series_request_id LEFT JOIN payloads sp ON sp.hash=sr.payload_hash
          WHERE m.yes_team=g.home AND b.quality='ok' AND s.state IN ('in','post') ORDER BY b.game_id,b.observed_at,b.id'''
        for r in db.execute(query):
            book_count+=1
            try:
                if r['series_request_id'] not in fee_cache:
                    fee_cache[r['series_request_id']]=fee_terms(json.loads(zlib.decompress(r['series_body'])).get('series',{})) if r['series_body'] else None
                multiplier=fee_cache[r['series_request_id']]
                if multiplier is None:excluded['unknown_fees']+=1;continue
                if r['state']=='in' and r['clock_seconds'] is None:excluded['missing_clock']+=1;continue
                book=normalize_book(json.loads(zlib.decompress(r['body'])))
                fills={side:{'buy':sweep(book[side+'_asks'],POLICY['quantity'],multiplier),'sell':sweep(book[side+'_bids'],POLICY['quantity'],multiplier)} for side in ('yes','no')}
                if any(book[side+'_bids'] and book[side+'_asks'] and book[side+'_bids'][0]['price']>book[side+'_asks'][0]['price'] for side in ('yes','no')):excluded['crossed_book']+=1;continue
                if r['score_age_seconds'] is None or not 0<=r['score_age_seconds']<=30:excluded['stale_score']+=1;continue
                groups[r['game_id']].append({'time':instant(r['observed_at']).timestamp(),'at':r['observed_at'],'state':r['state'],
                    'period':r['period'],'clock':r['clock_seconds'] or 0,'home_score':r['home_score'],'away_score':r['away_score'],
                    'home':r['home'],'away':r['away'],'day':r['date'],'score_age':r['score_age_seconds'],'fills':fills})
            except (ValueError,TypeError,KeyError,zlib.error):excluded['invalid_observation']+=1
    finally:db.close()
    rows=[];moving_rows=[];moving_excluded=Counter()
    for game_id,points in groups.items():
        for side in ('yes','no'):
            episodes,missing=replay(points,side,points[0]['home' if side=='yes' else 'away'],game_id,points[0]['day'])
            rows.extend(episodes);excluded.update(missing)
            moving,missing=replay(points,side,points[0]['home' if side=='yes' else 'away'],game_id,points[0]['day'],dynamic=True)
            moving_rows.extend(moving);moving_excluded.update(missing)
    def key(row):return (row['game_id'],row['side'],row['entered_at'])
    common={key(r) for r in rows}&{key(r) for r in moving_rows}
    teams={team:summarize([r for r in rows if r['team']==team]) for team in sorted({r['team'] for r in rows})}
    report={'schema':1,'generated_at':utcnow().isoformat(),'status':'research_only','policy':POLICY,'feature_names':FEATURES,
        'observed_books':book_count,'summary':summarize(rows),'exclusions':dict(excluded),'evaluation':evaluate(rows),'teams':teams,
        'dynamic_comparison':{'summary':summarize(moving_rows),'exclusions':dict(moving_excluded),
            'matched':{'fixed':summarize([r for r in rows if key(r) in common]),'moving':summarize([r for r in moving_rows if key(r) in common])},
            'policy':'Same scheduled entries and initial levels; moving TP/SL use only observations received by each step. Stops never loosen; TP moves at most 1 cent per minute.',
            'note':'Descriptive comparison, not a strategy selected on this sample. The logistic evaluation above predicts the FIXED baseline event, not the moving target.'},
        'promoted':False,'limitations':['Small, partial-game dataset; no calibrated live target-hit probability.',
            'Overlapping/correlated teams remain grouped by game in chronological evaluation.',
            'Quotes are sampled, not actual fills; gaps over 30 seconds censor a window.',
            'Triggers execute at the next observed full-depth quote, with fees and slippage; no guarantee of real execution.',
            'Team summaries are descriptive, not independently trained team models. No live sizing changes.']}
    output=directory/'research';output.mkdir(parents=True,exist_ok=True)
    atomic_json(output/'scalp-trial.json',report)
    (output/'scalp-trial-dynamic-episodes.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in moving_rows))
    (output/'scalp-trial-episodes.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return report


def trial_status(directory=DATA):
    path=Path(directory)/'research'/'scalp-trial.json'
    return json.loads(path.read_text()) if path.exists() else {'status':'not_run','promoted':False,'teams':{},'summary':{}}


if __name__=='__main__':
    print(json.dumps(run_trial(),indent=2))
