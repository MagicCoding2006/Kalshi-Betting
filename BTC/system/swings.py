"""BTC touch calculator and a distinct, executable-bid swing experiment.

Touch, settlement and profitable-resale events are separate targets. Research
replay uses one whole contract so probabilities and fee rounding match sizing.
"""
import bisect
import json
import math
import random
import sqlite3
import time
from collections import Counter,defaultdict
from pathlib import Path

from .features import CandleFeatures
from .settlement import YEAR,averaging_probability
from .execution import cash_cost
from .refinement import atomic_json

SETTINGS={'profit_target':.05,'stop_loss':.10,'max_hold_seconds':60,'latency_seconds':.25,
          'entry_slippage_limit':.005,'close_buffer_seconds':15,'max_quote_gap':2,
          'min_state_contracts':20,'kelly_fraction':.25,'capital_cap':.02,'target_basis':'trade_cost',
          'quantity':1,'starting_cash':100.}


def touch_probability(spot,target,seconds,sigma,drift=0.):
    """First passage under GBM; annualized arithmetic drift is a benchmark."""
    if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (spot,target,seconds,sigma,drift)):
        raise ValueError('Finite model inputs required')
    if min(spot,target)<=0 or seconds<0 or sigma<0:raise ValueError('Positive prices and nonnegative horizon/volatility required')
    if target==spot:return 1.
    if seconds==0:return 0.
    direction=1 if target>spot else -1;t=seconds/YEAR
    distance=abs(math.log(target)-math.log(spot));mu=direction*(drift-.5*sigma*sigma)
    if sigma==0:return float(mu>0 and mu*t>=distance)
    scale=sigma*math.sqrt(t)
    cdf=lambda z:.5*math.erfc(-z/math.sqrt(2))
    tail=cdf((-mu*t-distance)/scale)
    second=math.exp(min(700,2*mu*distance/(sigma*sigma)))*tail if tail else 0.
    return max(0.,min(1.,cdf((mu*t-distance)/scale)+second))


def purchase(price,m):
    return cash_cost(price,math.ceil((.07*m*price*(1-price)-1e-12)*1e6)/1e6)


def sale(price,m):
    model_fee=math.ceil((.07*m*price*(1-price)-1e-12)*1e6)/1e6
    return math.floor((price-model_fee+1e-10)*100)/100


def exit_bid(cost,target,m):
    """Smallest four-decimal bid producing the desired net return for one share."""
    needed=cost*(1+target)
    if sale(.9999,m)<needed-1e-10:return None
    lo,hi=1,9999
    while lo<hi:
        mid=(lo+hi)//2
        if sale(mid/10000,m)>=needed-1e-10:hi=mid
        else:lo=mid+1
    return lo/10000


def quote(row,side,ask=False):
    if side=='yes':return row['ask' if ask else 'bid'],row['ask_qty' if ask else 'bid_qty']
    return 1-row['bid' if ask else 'ask'],row['bid_qty' if ask else 'ask_qty']


def state(row,side):
    sign=1 if side=='yes' else -1
    bucket=lambda v,cut:0 if v<-cut else 2 if v>cut else 1
    # Mirror YES/NO consistently; no model receives a fitted directional prior.
    gap=bucket(sign*(row['baseline']-row['mid']),.03)
    expiry=0 if row['seconds_left']<=300 else 1
    side_price=row['mid'] if side=='yes' else 1-row['mid']
    price_bucket=0 if side_price<.3 else 2 if side_price>.7 else 1
    fee_schedule=str(round(row['fee_multiplier'],6))
    coarse=f'{side}:{fee_schedule}:{price_bucket}:{gap}:{expiry}'
    touch=row.get('p_touch_hold');touch_bucket='missing' if touch is None else 0 if touch<.3 else 2 if touch>.7 else 1
    full=f'{touch_bucket}:{bucket(sign*row["imbalance"],.2)}:{bucket(sign*row.get("momentum",0),.001)}:{coarse}'
    return full,coarse


def eligible(row,side,settings=SETTINGS):
    if row.get('fee_multiplier') is None or row.get('baseline') is None:return False
    if row['seconds_left']<settings['max_hold_seconds']+settings['close_buffer_seconds']+2*settings['latency_seconds']:return False
    if row.get('spot_time') is None or not 0<=row['ts']-row['spot_time']<=5:return False
    if not 0<row['bid']<=row['ask']<1:return False
    ask,aq=quote(row,side,True);bid,bq=quote(row,side)
    if min(aq,bq)<1 or not .1<=ask<=.9:return False
    cost=purchase(ask,row['fee_multiplier'])
    return sale(bid,row['fee_multiplier'])/cost-1>-settings['stop_loss'] and exit_bid(cost,settings['profit_target'],row['fee_multiplier']) is not None


def path_trade(group,times,i,side,settings=SETTINGS,asof=None):
    """Entry/exit latency, limit entry, executable bids and forced time exits."""
    start=group[i];delay=settings['latency_seconds'];m=start['fee_multiplier'];asof=times[-1] if asof is None else asof
    j=bisect.bisect_left(times,start['ts']+delay,lo=i+1)
    if j>=len(group) or times[j]>start['ts']+delay+1:return {'status':'pending' if asof<start['ts']+delay+1 else 'entry_rejected'}
    fill=group[j];ask,aq=quote(fill,side,True);planned_ask,_=quote(start,side,True)
    if fill.get('fee_multiplier') is None or aq<1 or ask>planned_ask+settings['entry_slippage_limit']+1e-10:return {'status':'entry_rejected'}
    m=fill['fee_multiplier'];cost=purchase(ask,m);bid,bq=quote(fill,side)
    if sale(bid,m)/cost-1<=-settings['stop_loss']:return {'status':'entry_rejected'}
    if exit_bid(cost,settings['profit_target'],m) is None:return {'status':'entry_rejected'}
    deadline=min(fill['ts']+settings['max_hold_seconds'],start['ts']+start['seconds_left']-settings['close_buffer_seconds'])
    base={'ticker':start['ticker'],'side':side,'decision_ts':start['ts'],'entry_ts':fill['ts'],'entry_price':ask,'cost':cost}
    for k in range(j,len(group)):
        if k>j and times[k]-times[k-1]>settings['max_quote_gap']:
            return {**base,'status':'censored','reason':'quote gap','roi':None,'roi_floor':-1.,'exit_ts':deadline+delay+1}
        current=group[k];bid,bq=quote(current,side);mult=current.get('fee_multiplier')
        net=sale(bid,mult)/cost-1 if mult is not None and bq>=1 else None
        reason='target' if net is not None and net>=settings['profit_target']-1e-10 else 'stop' if net is not None and net<=-settings['stop_loss'] else 'time' if times[k]>=deadline else None
        if reason is None:continue
        leave=bisect.bisect_left(times,times[k]+delay,lo=k+1)
        if leave>=len(group) or times[leave]>times[k]+delay+1:
            return {**base,'status':'censored','reason':'missing exit after latency','roi':None,'roi_floor':-1.,'exit_ts':deadline+delay+1}
        closing=group[leave];exit_price,exit_qty=quote(closing,side);em=closing.get('fee_multiplier')
        if exit_qty<1 or em is None or closing['seconds_left']<=0:
            return {**base,'status':'censored','reason':'insufficient exit depth or fees','roi':None,'roi_floor':-1.,'exit_ts':times[leave]}
        proceeds=sale(exit_price,em);roi=proceeds/cost-1
        return {**base,'status':'closed','reason':reason,'exit_ts':times[leave],'exit_price':exit_price,'proceeds':proceeds,
                'pnl':proceeds-cost,'roi':roi,'roi_floor':roi,'success':roi>=settings['profit_target']-1e-10,
                'fees_and_rounding':cost-ask+exit_price-proceeds,
                'target_trigger_lost_to_latency':reason=='target' and roi<settings['profit_target']-1e-10}
    if asof<deadline+delay+1:return {'status':'pending'}
    return {**base,'status':'censored','reason':'recording ended before exit','roi':None,'roi_floor':-1.,'exit_ts':deadline+delay+1}


def summarize(samples):
    returns=[s['roi_floor'] for s in samples];n=len(returns);success=sum(s.get('success',False) for s in samples)
    rng=random.Random(512);boot=sorted(sum(rng.choice(returns) for _ in returns)/n for _ in range(500))
    # One earliest observation per contract in each state avoids counting ticks as independent outcomes.
    mean=sum(returns)/n
    def derivative(f):return sum(r/(1+f*r) for r in returns)/n
    lo,hi=0.,.99
    if mean>0:
        for _ in range(45):
            mid=(lo+hi)/2
            if derivative(mid)>0:lo=mid
            else:hi=mid
    else:hi=0.
    return {'contracts':n,'target_success_probability':success/n,'mean_net_return':mean,'lower_mean_return_95':boot[25],
            'censored':sum(s['status']=='censored' for s in samples),'full_kelly_fraction':(lo+hi)/2,
            'observed_positive_return_rate':sum(r>0 for r in returns)/n}


def fit(samples):
    bins={'state':defaultdict(dict),'coarse':defaultdict(dict)}
    for s in sorted(samples,key=lambda s:s['row']['ts']):
        for kind,key in zip(('state','coarse'),state(s['row'],s['side'])):
            bins[kind][key].setdefault(s['row']['ticker'],s['trade'])
    return {kind:{key:summarize(list(by_contract.values())) for key,by_contract in entries.items() if len(by_contract)>=5} for kind,entries in bins.items()}


def predict(model,row,side,settings=SETTINGS):
    keys=state(row,side)
    for kind,key,minimum in [('state',keys[0],settings['min_state_contracts']),('coarse',keys[1],30)]:
        item=model.get(kind,{}).get(key)
        if item and item['contracts']>=minimum:
            fraction=min(settings['capital_cap'],settings['kelly_fraction']*item['full_kelly_fraction'])
            allowed=item['lower_mean_return_95']>0 and item['censored']/item['contracts']<=.05
            return {**item,'basis':kind,'state_key':key,'budget_fraction':fraction if allowed else 0.,'eligible_signal':allowed}
    return None


def load_rows(path):
    connection=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
    try:
        rows=[dict(r) for r in connection.execute('SELECT id,ts,ticker,mid,bid,ask,spread,bid_qty,ask_qty,imbalance,momentum,seconds_left,spot,spot_time,strike,fee_multiplier FROM snapshots ORDER BY ts')]
        candle=CandleFeatures([dict(r) for r in connection.execute('SELECT ts,close FROM candles ORDER BY ts')])
    finally:connection.close()
    groups=defaultdict(list);sigma_by_minute={}
    for r in rows:
        if r['mid'] is None:continue
        minute=int(r['ts']//60)
        if minute not in sigma_by_minute:sigma_by_minute[minute]=candle.at(r['ts'])[0]
        sigma=sigma_by_minute[minute]
        r['baseline']=averaging_probability(r['spot'],r['strike'],r['seconds_left'],sigma) if r['spot'] and r['strike'] and sigma else None
        r['p_touch_hold']=touch_probability(r['spot'],r['strike'],max(0,min(SETTINGS['max_hold_seconds'],r['seconds_left'])),sigma) if r['spot'] and r['strike'] and sigma else None
        groups[r['ticker']].append(r)
    return groups


def settlement_signal_benchmark(grouped):
    """Expose the error of treating a settlement edge as a short-hold edge."""
    cash=100.;available=0.;fills=[];rejected=0
    for key,possibilities in sorted(grouped.items(),key=lambda kv:kv[0][1]):
        if key[1]<available:continue
        eligible_signals=[]
        for s in possibilities:
            row=s['row'];ask,_=quote(row,s['side'],True);p=row['baseline'] if s['side']=='yes' else 1-row['baseline']
            limit_cost=purchase(min(.9999,ask+SETTINGS['entry_slippage_limit']),row['fee_multiplier'])
            edge=p-purchase(ask,row['fee_multiplier'])
            if edge>=.03 and limit_cost<=cash*SETTINGS['capital_cap']:eligible_signals.append((edge,s))
        if not eligible_signals:continue
        _,selected=max(eligible_signals,key=lambda v:v[0]);trade=selected['trade']
        if trade['status']=='entry_rejected':rejected+=1;continue
        cash+=trade.get('pnl',-trade['cost']);available=trade['exit_ts'];fills.append(trade)
    return {'name':'Settlement signal with swing exits · diagnostic only','starting_cash':100.,'ending_cash':cash,'net_pnl':cash-100,
            'trades':len(fills),'target_successes':sum(t.get('success',False) for t in fills),'censored_trades':sum(t['status']=='censored' for t in fills),
            'entry_rejections':rejected,'note':'Same test cohort, one-share cash, latency and exits. A settlement-probability edge is not a forecast of short-term resale profit.'}


def study(source,directory):
    groups=load_rows(source);names=sorted(groups,key=lambda t:groups[t][0]['ts'])
    if len(names)<40:raise ValueError('Need 40 recorded BTC contract windows for a whole-contract swing split')
    split=int(len(names)*.7);test_names=set(names[split:]);cutoff=min(groups[t][0]['ts'] for t in test_names)
    train=[];test=[];counts=Counter();asof=max(r['ts'] for g in groups.values() for r in g)
    for ticker in names:
        group=groups[ticker];times=[r['ts'] for r in group];last_bucket=None
        for i,row in enumerate(group):
            bucket=int(row['ts']//15)
            if bucket==last_bucket:continue
            last_bucket=bucket
            for side in ('yes','no'):
                if not eligible(row,side):continue
                trade=path_trade(group,times,i,side,asof=asof);counts[trade['status']]+=1
                if trade['status']=='pending':continue
                sample={'row':row,'side':side,'trade':trade}
                if ticker in test_names:test.append(sample)
                elif trade['status']!='entry_rejected' and trade['exit_ts']<cutoff:train.append(sample)
    if len({s['row']['ticker'] for s in train})<30 or not test:raise ValueError('Collecting: insufficient usable training/test paths after fees, freshness and entry checks')
    model=fit(train);cash=100.;active_until=0.;fills=[];skips=Counter();predictions=[]
    grouped=defaultdict(list)
    for s in test:grouped[(s['row']['ticker'],s['row']['ts'])].append(s)
    for key,possibilities in sorted(grouped.items(),key=lambda kv:kv[0][1]):
        if key[1]<active_until:continue
        candidates=[]
        for s in possibilities:
            prediction=predict(model,s['row'],s['side'])
            if not prediction:skips['insufficient independent state support']+=1;continue
            if s['trade']['status']!='entry_rejected':predictions.append({'ticker':key[0],'ts':key[1],'side':s['side'],'p_target':prediction['target_success_probability'],'actual':int(s['trade'].get('success',False))})
            if not prediction['eligible_signal']:skips['no positive lower expected return']+=1;continue
            ask,_=quote(s['row'],s['side'],True)
            cost=purchase(min(.9999,ask+SETTINGS['entry_slippage_limit']),s['row']['fee_multiplier'])
            if cost>cash*prediction['budget_fraction']+1e-9:skips['Kelly or capital budget']+=1;continue
            candidates.append((prediction['lower_mean_return_95'],s,prediction))
        if not candidates:continue
        _,chosen,p=max(candidates,key=lambda c:c[0]);trade=chosen['trade']
        if trade['status']=='entry_rejected':skips['entry IOC rejected after signal']+=1;continue
        cost=trade['cost']
        # Cash remains committed until exit; censored trades debit their entire
        # cost as a worst-case stress, rather than disappearing from the replay.
        cash+=trade.get('pnl',-cost);active_until=trade['exit_ts']
        fills.append({**trade,'cash_after':cash,'state_contracts':p['contracts']})
    high=100.;dd=0.
    for f in fills:high=max(high,f['cash_after']);dd=max(dd,(high-f['cash_after'])/high)
    windows=defaultdict(float);window_start={}
    for f in fills:
        pnl=f.get('pnl',-f['cost']);window_start.setdefault(f['ticker'],f['cash_after']-pnl);windows[f['ticker']]+=pnl
    report={'kind':'swing','status':'complete','created':time.time(),'settings':SETTINGS,'split':{'training_contracts':split,'test_contracts':len(test_names),'cutoff':cutoff},
            'usable_training_contracts':len({s['row']['ticker'] for s in train}),'usable_test_contracts':len({s['row']['ticker'] for s in test}),
            'label_counts':dict(counts),'selection_rule':'Fixed 5% net position-return target / 10% stop / 60-second hold; one share, 15-second decision sampling. No settings were tuned on the historical test.',
            'execution':{'starting_cash':100.,'ending_cash':cash,'net_pnl':cash-100,'trades':len(fills),'censored_trades':sum(f['status']=='censored' for f in fills),
                         'net_5pct_target_successes':sum(f.get('success',False) for f in fills),'maximum_replay_drawdown':dd,'skips':dict(skips),
                         'windows_with_5pct_portfolio_gain':sum(v>=.05*window_start[t] for t,v in windows.items()),'fills':fills[-100:]},
            'settlement_signal_benchmark':settlement_signal_benchmark(grouped),
            'prediction_count':len(predictions),'target_brier':sum((p['p_target']-p['actual'])**2 for p in predictions)/len(predictions) if predictions else None,
            'limits':'Historical diagnostic, not a fresh lockbox. Target Brier uses overlapping observations and is not an independent sample count. Missing paths count as full-cost stress losses for selection and replay. One-share top-of-book fills; no queue guarantees or bot identity inference. Touch probabilities never substitute for observed resale outcomes.'}
    atomic_json(Path(directory)/'swing-model.json',{'created':report['created'],'settings':SETTINGS,'model':model})
    atomic_json(Path(directory)/'swing-report.json',report)
    return report


def live(row,sigma,model_bundle,target=None):
    row=dict(row)
    row['p_touch_hold']=touch_probability(row['spot'],row['strike'],max(0,min(SETTINGS['max_hold_seconds'],row['seconds_left'])),sigma) if row.get('spot') and row.get('strike') and sigma else None
    target=row.get('strike') if target is None else target
    touch=touch_probability(row['spot'],target,max(0,row['seconds_left']),sigma) if target and sigma and row.get('spot') else None
    result={'ts':row['ts'],'ticker':row['ticker'],'spot':row.get('spot'),'target':target,'seconds_left':row['seconds_left'],
            'p_btc_touch_before_close':touch,'p_settlement_yes':row.get('baseline'),'sigma':sigma,'target_basis':'trade_cost',
            'settings':SETTINGS,'sides':[],'note':'BTC touch probability is a GBM proxy benchmark, not Kalshi settlement or the probability of a profitable sale. Model drift is zero; volatility is measured.'}
    for side in ('yes','no'):
        ask,aq=quote(row,side,True);bid,bq=quote(row,side);m=row.get('fee_multiplier');p=predict(model_bundle['model'],row,side) if model_bundle and row.get('baseline') is not None and m is not None else None
        cost=purchase(ask,m) if m is not None else None
        required=exit_bid(cost,SETTINGS['profit_target'],m) if cost else None
        economics=eligible(row,side) if row.get('baseline') is not None else False
        budget=100*p['budget_fraction'] if p else 0.
        allowed=bool(economics and p and p['eligible_signal'] and cost<=budget)
        result['sides'].append({'side':side,'ask':ask,'bid':bid,'one_contract_cost':cost,'required_exit_bid':required,
            'net_if_sold_now':sale(bid,m)-cost if cost and bq>=1 else None,'prediction':p,'research_candidate':allowed,
            'quantity':1 if allowed else 0,'bankroll_fraction':cost/100 if allowed else 0,
            'reason':'Paper research candidate only; no portfolio order connection' if allowed else 'WAIT: costs, state evidence, lower expected return or Kelly budget do not support entry'})
    return result
