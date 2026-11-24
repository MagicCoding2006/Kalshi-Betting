"""Adaptive stochastic barriers and distributional, whole-contract Kelly sizing.

The objective is expected log wealth after costs, not a fixed percentage win.
Conditional logit diffusion is an approximation, gated on later calibration.
"""
import bisect
import hashlib
import json
import math
import statistics
import time
from collections import Counter,defaultdict
from pathlib import Path

from .evaluation import logit,sigmoid
from .refinement import atomic_json
from .swings import load_rows,quote,touch_probability

VERSION='adaptive-logit-v1'
SETTINGS={'objective':'expected_log_growth','kelly_fraction':.25,'capital_cap':.02,'tail_loss_budget':.01,
          'tail_probability':.05,'max_forecast_seconds':120,'latency_seconds':.25,'entry_slippage_limit':.005,
          'close_buffer_seconds':15,'max_quote_gap':2,'decision_interval_seconds':15,'process_interval_seconds':5,
          'simulation_step_seconds':1,'parameter_groups':32,'paths_per_group':8,'starting_cash':100.,
          'min_state_contracts':20,'min_coarse_contracts':30,'min_calibration_contracts':12,
          'target_basis':'trade_cost','min_entry_price':.05,'max_entry_price':.95}


def purchase(q,p,m):
    return math.ceil((q*p+math.ceil((.07*m*q*p*(1-p)-1e-12)*1e6)/1e6-1e-10)*100)/100


def sale(q,p,m):
    return math.floor((q*p-math.ceil((.07*m*q*p*(1-p)-1e-12)*1e6)/1e6+1e-10)*100)/100


def weighted_kelly(returns,weights):
    """Solve max_f sum(w log(1+fR)), including the full-cost loss outcome."""
    if not returns or len(returns)!=len(weights):raise ValueError('Matching nonempty outcomes and weights required')
    if any(not math.isfinite(r) or r<-1 for r in returns) or any(not math.isfinite(w) or w<0 for w in weights):raise ValueError('Invalid return distribution')
    total=sum(weights)
    if total<=0:raise ValueError('Positive probability mass required')
    mean=sum(w*r for w,r in zip(weights,returns))/total
    if mean<=0:return 0.
    lo,hi=0.,.999
    for _ in range(42):
        f=(lo+hi)/2
        slope=sum(w*r/(1+f*r) for w,r in zip(weights,returns))
        if slope>0:lo=f
        else:hi=f
    return (lo+hi)/2


def tail_mean(returns,weights,tail=.05):
    """Weighted expected shortfall, accounting for fractional boundary mass."""
    if len(returns)!=len(weights) or not returns or not 0<tail<=1 or sum(weights)<=0 or any(not math.isfinite(w) or w<0 for w in weights):raise ValueError('Valid tail probability and matching weights required')
    needed=tail*sum(weights);left=needed;value=0.
    for r,w in sorted(zip(returns,weights)):
        used=min(left,w);value+=used*r;left-=used
        if left<=1e-12:break
    return value/needed


def keys(row):
    gap=row['baseline']-row['mid'];g=0 if gap<-.03 else 2 if gap>.03 else 1
    p=0 if row['mid']<.3 else 2 if row['mid']>.7 else 1
    t=0 if row['seconds_left']<=300 else 1
    flow=0 if row['imbalance']<-.2 else 2 if row['imbalance']>.2 else 1
    coarse=f'{round(row["fee_multiplier"],6)}:{p}:{g}:{t}'
    return f'{flow}:{coarse}',coarse


def valid(row):
    return (row.get('baseline') is not None and row.get('fee_multiplier') is not None and row.get('spot_time') is not None
            and 0<=row['ts']-row['spot_time']<=5 and row['seconds_left']>=60
            and 0<row['bid']<=row['ask']<1 and min(row['bid_qty'],row['ask_qty'])>=1)


def transitions(groups,names,before=float('inf')):
    result=[];dt=SETTINGS['process_interval_seconds']
    for ticker in names:
        group=groups[ticker];times=[r['ts'] for r in group];bucket=None
        for i,row in enumerate(group):
            b=int(row['ts']//SETTINGS['decision_interval_seconds'])
            if b==bucket or not valid(row):continue
            bucket=b;j=bisect.bisect_left(times,row['ts']+dt,i+1)
            if j>=len(group) or times[j]>row['ts']+dt+1 or times[j]>=before:continue
            if any(y-x>SETTINGS['max_quote_gap'] for x,y in zip(times[i:j],times[i+1:j+1])):continue
            if not 0<group[j]['mid']<1:continue
            elapsed=times[j]-times[i]
            result.append({'row':row,'future_ts':times[j],'dt':elapsed,'delta':logit(group[j]['mid'])-logit(row['mid']),
                           'flow_next':group[j]['imbalance']})
    return result


def process_stats(samples):
    by_contract=defaultdict(list)
    for s in samples:by_contract[s['row']['ticker']].append(s)
    rates=[statistics.mean(s['delta']/s['dt'] for s in values) for values in by_contract.values()]
    mu=statistics.mean(rates);se=statistics.stdev(rates)/math.sqrt(len(rates)) if len(rates)>1 else abs(mu)
    shrunk=math.copysign(max(0,abs(mu)-1.96*se),mu)
    variances=[statistics.mean((s['delta']-mu*s['dt'])**2/s['dt'] for s in values) for values in by_contract.values()]
    sigma=math.sqrt(max(0,statistics.mean(variances)))
    innovations=[(s['delta']-mu*s['dt'])/(max(sigma,1e-12)*math.sqrt(s['dt'])) for s in samples]
    # Preserve observed tails rather than substituting Gaussian price shocks.
    center=statistics.mean(innovations);scale=math.sqrt(statistics.mean((x-center)**2 for x in innovations))
    innovations=sorted((x-center)/scale for x in innovations) if scale>0 else [0.]
    if len(innovations)>1024:innovations=[innovations[int(i*(len(innovations)-1)/1023)] for i in range(1024)]
    xys=[statistics.mean((logit(s['row']['baseline'])-logit(s['row']['mid']))*s['delta']/s['dt'] for s in values) for values in by_contract.values()]
    xxs=[statistics.mean((logit(s['row']['baseline'])-logit(s['row']['mid']))**2 for s in values) for values in by_contract.values()]
    kappa=max(0,min(.2,sum(xys)/sum(xxs))) if sum(xxs)>0 else 0.
    flow_xy=sum(s['row']['imbalance']*s['flow_next'] for s in samples);flow_xx=sum(s['row']['imbalance']**2 for s in samples)
    rho=max(0,min(.99,flow_xy/flow_xx)) if flow_xx else 0.
    tau=max(5,min(120,-5/math.log(rho))) if rho>0 else 5.
    return {'contracts':len(by_contract),'mu_raw':mu,'mu':shrunk,'mu_standard_error':se,'sigma':sigma,
            'kappa':kappa,'flow_decay_seconds':tau,'spread_75':sorted(s['row']['spread'] for s in samples)[int(.75*(len(samples)-1))],
            'innovations':innovations}


def fit(samples):
    bins={'state':defaultdict(list),'coarse':defaultdict(list)}
    for s in samples:
        for kind,key in zip(('state','coarse'),keys(s['row'])):bins[kind][key].append(s)
    return {kind:{key:process_stats(values) for key,values in entries.items() if len({s['row']['ticker'] for s in values})>=5} for kind,entries in bins.items()}


def lookup(model,row):
    if not valid(row):return None
    for kind,key,minimum in [('state',keys(row)[0],SETTINGS['min_state_contracts']),('coarse',keys(row)[1],SETTINGS['min_coarse_contracts'])]:
        p=model.get(kind,{}).get(key)
        if p and p['contracts']>=minimum and p['sigma']>0:return {**p,'basis':kind,'state_key':key}
    return None


def calibrate(model,samples):
    import numpy as np
    rows=[]
    for s in samples:
        p=lookup(model,s['row'])
        if p:rows.append((s,p))
    names=sorted({s['row']['ticker'] for s,p in rows})
    if len(names)<SETTINGS['min_calibration_contracts']:return {'passed':False,'drift_trust':0.,'reason':'Insufficient independent calibration contracts','contracts':len(names),'variance_scale':1.}
    candidates=[]
    for alpha in (0.,.25,.5,1.):
        losses=defaultdict(list)
        for s,p in rows:losses[s['row']['ticker']].append(((s['delta']-alpha*p['mu']*s['dt'])**2-s['delta']**2)/s['dt'])
        values=[statistics.mean(v) for v in losses.values()]
        candidates.append({'alpha':alpha,'mean_mse_difference':statistics.mean(values),'values':values})
    winner=min(candidates,key=lambda c:c['mean_mse_difference']);rng=np.random.default_rng(439)
    v=np.array(winner['values']);boot=rng.choice(v,size=(2000,len(v)),replace=True).mean(axis=1)
    interval=np.quantile(boot,[.05/6,1-.05/6]).tolist()
    passed=winner['alpha']>0 and interval[1]<0
    ratios=[(s['delta']-winner['alpha']*p['mu']*s['dt'])**2/(s['dt']*p['sigma']**2) for s,p in rows]
    inflation=max(1.,math.sqrt(statistics.mean(ratios)))
    return {'passed':bool(passed),'drift_trust':winner['alpha'],'contracts':len(names),'variance_scale':inflation,
            'mse_improvement_interval':interval,'interval_confidence':1-.05/3,
            'candidates':[{k:v for k,v in c.items() if k!='values'} for c in candidates],
            'reason':'Conditional drift improves later forecasts' if passed else 'Conditional drift has not demonstrated improvement over no-change forecasts'}


def gap_hazard(groups,names):
    events=0;observed=0.
    for ticker in names:
        g=groups[ticker]
        for a,b in zip(g,g[1:]):
            dt=b['ts']-a['ts']
            if dt>SETTINGS['max_quote_gap']:events+=1
            elif dt>0:observed+=dt
    # An approximate upper Poisson rate; outages are modeled as full-cost losses.
    upper=(events+1.645*math.sqrt(events+1)+1)/observed if observed else float('inf')
    return {'events':events,'observed_seconds':observed,'upper_rate_per_second':upper}


def horizon_choices(row,p):
    limit=min(SETTINGS['max_forecast_seconds'],row['seconds_left']-SETTINGS['close_buffer_seconds']-2*SETTINGS['latency_seconds'])
    if limit<5:return []
    tau=p['flow_decay_seconds']
    return sorted({max(5,min(int(limit),int(round(tau*f/5))*5)) for f in (.5,1.,2.)})


def scenario_paths(row,p,calibration,max_seconds):
    import numpy as np
    groups=SETTINGS['parameter_groups'];per=SETTINGS['paths_per_group'];n=groups*per
    material=f'{row["ticker"]}:{int(row["ts"]//15)}:{row["mid"]:.4f}'
    seed=int(hashlib.sha256(material.encode()).hexdigest()[:8],16);rng=np.random.default_rng(seed)
    alpha=calibration['drift_trust'];sigma=p['sigma']*calibration.get('variance_scale',1.)
    mus=np.repeat(rng.normal(p['mu'],p['mu_standard_error'],groups),per)*alpha
    # A single parameter draw per path persists; it is not independent noise at each step.
    kappa=p['kappa']*alpha;theta=logit(row['baseline']);initial=logit(row['mid'])
    residual=mus-kappa*(theta-initial);tau=p['flow_decay_seconds'];level=np.full(n,initial)
    paths=np.empty((n,max_seconds));latency=np.empty_like(paths);innov=np.array(p['innovations'])
    for i in range(max_seconds):
        mu=kappa*(theta-level)+residual*np.exp(-i/tau)
        level=np.clip(level+mu+sigma*rng.choice(innov,n),-9.21,9.21)
        paths[:,i]=1/(1+np.exp(-level))
        delayed=level+mu*SETTINGS['latency_seconds']+sigma*math.sqrt(SETTINGS['latency_seconds'])*rng.choice(innov,n)
        latency[:,i]=1/(1+np.exp(-np.clip(delayed,-9.21,9.21)))
    return paths,latency


def plan(model_bundle,row,side,equity=100.):
    """Select stochastic barriers, horizon and integer quantity jointly.

    Bounds are quantiles of simulated extrema, not fixed ROI percentages. The
    return distribution includes parameter uncertainty, fees and a gap-loss atom.
    """
    import numpy as np
    if not model_bundle or model_bundle.get('version')!=VERSION:return {'quantity':0,'reason':'Train the adaptive stochastic model'}
    p=lookup(model_bundle['process'],row)
    if not p:return {'quantity':0,'reason':'Insufficient independent contracts for this process state'}
    cal=model_bundle['calibration']
    if not cal['passed']:
        return {'quantity':0,'reason':'WAIT: conditional drift has not passed later-period calibration','state_contracts':p['contracts'],
                'calibration_passed':False,'process':{k:p[k] for k in ('mu_raw','mu','mu_standard_error','sigma','kappa','flow_decay_seconds')}}
    ask,aq=quote(row,side,True);bid,bq=quote(row,side)
    if not SETTINGS['min_entry_price']<=ask<=SETTINGS['max_entry_price']:return {'quantity':0,'reason':'Outside the supported entry-price range'}
    horizons=horizon_choices(row,p)
    if not horizons:return {'quantity':0,'reason':'Not enough time for an exit before market close'}
    limit=min(.9999,ask+SETTINGS['entry_slippage_limit']);m=row['fee_multiplier']
    max_q=min(int(aq),int(bq),int(equity*SETTINGS['capital_cap']/max(limit,1e-9)))
    if max_q<1:return {'quantity':0,'reason':'Whole-contract liquidity or capital cap too small'}
    paths,delayed=scenario_paths(row,p,cal,max(horizons));spread=max(row['spread'],p['spread_75'])
    mid=paths if side=='yes' else 1-paths;late=delayed if side=='yes' else 1-delayed
    bids=np.clip(mid-spread/2,.0001,.9999);exit_bids=np.clip(late-spread/2,.0001,.9999)
    best=None;best_illustration=None;groups=SETTINGS['parameter_groups'];per=SETTINGS['paths_per_group'];n=groups*per
    rate=model_bundle['gap_hazard']['upper_rate_per_second']
    for horizon in horizons:
        window=bids[:,:horizon]
        targets=np.unique(np.quantile(window.max(axis=1),[.5,.75,.9]))
        latency_noise=p['sigma']*cal.get('variance_scale',1.)*row['mid']*(1-row['mid'])*math.sqrt(SETTINGS['latency_seconds'])
        stops=np.unique(np.maximum(.0001,np.minimum(np.quantile(window.min(axis=1),[.1,.25,.5]),bid-max(.0001,latency_noise))))
        for target in targets:
            for stop in stops:
                if target<=bid+.0001 or stop>=bid-.0001 or stop>=target:continue
                hit=(window>=target)|(window<=stop);has=hit.any(axis=1);first=hit.argmax(axis=1)
                first=np.where(has,first,horizon-1);elapsed=first+1+SETTINGS['latency_seconds']
                price=exit_bids[np.arange(n),first];gap=float(np.mean(1-np.exp(-rate*elapsed)))
                for q in range(1,max_q+1):
                    cost=purchase(q,limit,m)
                    if cost>equity*SETTINGS['capital_cap']+1e-10:continue
                    net_target=sale(q,float(target),m)/cost-1
                    if net_target<=0:continue
                    fees=np.ceil((.07*m*q*price*(1-price)-1e-12)*1e6)/1e6
                    proceeds=np.floor((q*price-fees+1e-10)*100)/100;returns=proceeds/cost-1
                    outcomes=np.append(returns,-1.);weights=np.append(np.full(n,(1-gap)/n),gap)
                    lo,hi=0.,.999
                    if float(np.dot(outcomes,weights))<=0:hi=0.
                    else:
                        for _ in range(32):
                            f=(lo+hi)/2
                            if float(np.dot(weights,outcomes/(1+f*outcomes)))>0:lo=f
                            else:hi=f
                    full=(lo+hi)/2;fraction=cost/equity
                    expected=float(np.dot(outcomes,weights));lower=float(np.quantile((1-gap)*returns.reshape(groups,per).mean(axis=1)-gap,.05))
                    cvar=max(0.,-tail_mean(np.minimum(outcomes,0).tolist(),weights.tolist(),SETTINGS['tail_probability']))
                    growth=float(np.dot(np.log1p(fraction*outcomes),weights));duration=float(np.mean(elapsed));score=growth/duration
                    sizing_ok=fraction<=SETTINGS['kelly_fraction']*full+1e-10 and fraction*cvar<=SETTINGS['tail_loss_budget']+1e-10
                    accepted=bool(cal['passed'] and expected>0 and lower>0 and growth>0 and sizing_ok)
                    candidate={'quantity':q if accepted else 0,'evaluated_quantity':q,'cost':cost,'capital_fraction':fraction if accepted else 0.,
                        'target_bid':float(target),'stop_bid':float(stop),'max_hold_seconds':horizon,
                        'target_net_return':net_target,'stop_net_return':sale(q,float(stop),m)/cost-1,
                        'expected_net_return':expected,'conservative_mean_return':lower,'probability_profit':float((1-gap)*np.mean(returns>0)),
                        'full_kelly_fraction':full,'fractional_kelly_fraction':SETTINGS['kelly_fraction']*full,
                        'tail_expected_loss_fraction':cvar,'portfolio_tail_loss_fraction':fraction*cvar,
                        'expected_log_growth':growth,'log_growth_per_second':score,'expected_hold_seconds':duration,
                        'estimated_gap_loss_probability':gap,'state_contracts':p['contracts'],'basis':p['basis'],
                        'process':{k:p[k] for k in ('mu_raw','mu','mu_standard_error','sigma','kappa','flow_decay_seconds')},
                        'calibration_passed':cal['passed'],'sizing_passed':sizing_ok,
                        'reason':'Paper research candidate only' if accepted else 'WAIT: calibration, conservative net return or fractional-Kelly/tail-risk budget failed'}
                    if best_illustration is None or score>best_illustration['log_growth_per_second']:best_illustration=candidate
                    if accepted and (best is None or score>best['log_growth_per_second']):best=candidate
    return best or best_illustration or {'quantity':0,'reason':'No fee-covering stochastic exit plan in the supported range','state_contracts':p['contracts']}


def replay_trade(group,times,i,side,policy,asof):
    q=policy['quantity'];start=group[i];lag=SETTINGS['latency_seconds'];j=bisect.bisect_left(times,start['ts']+lag,i+1)
    if j>=len(group) or times[j]>start['ts']+lag+1:return {'status':'pending' if asof<start['ts']+lag+1 else 'rejected'}
    fill=group[j];ask,qty=quote(fill,side,True);original,_=quote(start,side,True)
    if qty<q or ask>original+SETTINGS['entry_slippage_limit']+1e-10 or fill['fee_multiplier'] is None:return {'status':'rejected'}
    cost=purchase(q,ask,fill['fee_multiplier'])
    deadline=min(fill['ts']+policy['max_hold_seconds'],start['ts']+start['seconds_left']-SETTINGS['close_buffer_seconds'])
    base={'ticker':start['ticker'],'side':side,'quantity':q,'cost':cost,'entry_ts':times[j],'decision_ts':start['ts'],
          'target_bid':policy['target_bid'],'stop_bid':policy['stop_bid'],'max_hold_seconds':policy['max_hold_seconds'],
          'expected_net_return':policy['expected_net_return'],'expected_log_growth':policy['expected_log_growth']}
    for k in range(j,len(group)):
        if k>j and times[k]-times[k-1]>SETTINGS['max_quote_gap']:
            return {**base,'status':'censored','pnl':-cost,'roi':None,'exit_ts':deadline+lag+1,'reason':'quote gap; full-cost stress'}
        bid,bq=quote(group[k],side)
        trigger='model target' if bid>=policy['target_bid'] else 'model stop' if bid<=policy['stop_bid'] else 'model time limit' if times[k]>=deadline else None
        if trigger is None:continue
        leave=bisect.bisect_left(times,times[k]+lag,k+1)
        if leave>=len(group) or times[leave]>times[k]+lag+1:
            if asof<times[k]+lag+1:return {'status':'pending'}
            return {**base,'status':'censored','pnl':-cost,'roi':None,'exit_ts':deadline+lag+1,'reason':'missing exit; full-cost stress'}
        price,depth=quote(group[leave],side);m=group[leave].get('fee_multiplier')
        if depth<q or m is None or group[leave]['seconds_left']<=0:
            return {**base,'status':'censored','pnl':-cost,'roi':None,'exit_ts':times[leave],'reason':'unexecutable exit; full-cost stress'}
        proceeds=sale(q,price,m)
        return {**base,'status':'closed','pnl':proceeds-cost,'roi':proceeds/cost-1,'exit_ts':times[leave],'reason':trigger,'exit_bid':price}
    if asof<deadline+lag+1:return {'status':'pending'}
    return {**base,'status':'censored','pnl':-cost,'roi':None,'exit_ts':deadline+lag+1,'reason':'recording ended; full-cost stress'}


def study(source,directory):
    groups=load_rows(source);names=sorted(groups,key=lambda t:groups[t][0]['ts']);n=len(names)
    if n<90:raise ValueError('Adaptive study needs 90 recorded contract windows for train/calibration/test')
    a=int(.5*n);b=int(.7*n);cal_names=names[a:b];test_names=names[b:]
    cal_start=groups[cal_names[0]][0]['ts'];test_start=groups[test_names[0]][0]['ts']
    train=transitions(groups,names[:a],cal_start);cal=transitions(groups,cal_names,test_start)
    if len({s['row']['ticker'] for s in train})<30:raise ValueError('Need 30 training contracts with continuous five-second transitions')
    process=fit(train);calibration=calibrate(process,cal);hazard=gap_hazard(groups,names[:b])
    if not math.isfinite(hazard['upper_rate_per_second']):raise ValueError('Insufficient continuous quote coverage for gap-risk estimation')
    bundle={'version':VERSION,'created':time.time(),'settings':SETTINGS,'process':process,'calibration':calibration,'gap_hazard':hazard}
    cash=100.;available=0.;fills=[];reasons=Counter();evaluated=0;asof=max(g[-1]['ts'] for g in groups.values())
    for ticker in test_names:
        group=groups[ticker];times=[r['ts'] for r in group];bucket=None
        for i,row in enumerate(group):
            new=int(row['ts']//SETTINGS['decision_interval_seconds'])
            if new==bucket or row['ts']<available or not valid(row):continue
            bucket=new
            if not calibration['passed']:reasons['calibration has not demonstrated predictive drift']+=1;continue
            choices=[]
            for side in ('yes','no'):
                p=plan(bundle,row,side,cash);evaluated+=1
                if p['quantity']>0:choices.append((p['log_growth_per_second'],side,p))
                else:reasons[p['reason']]+=1
            if not choices:continue
            _,side,p=max(choices,key=lambda v:v[0]);trade=replay_trade(group,times,i,side,p,asof)
            if trade['status'] in ('pending','rejected'):reasons[trade['status']]+=1;continue
            cash+=trade['pnl'];available=trade['exit_ts'];fills.append({**trade,'cash_after':cash})
        atomic_json(Path(directory)/'adaptive-progress.json',{'contract':ticker,'completed_contracts':test_names.index(ticker)+1,'total_contracts':len(test_names)})
    high=100.;drawdown=0.
    for f in fills:high=max(high,f['cash_after']);drawdown=max(drawdown,(high-f['cash_after'])/high)
    total_cost=sum(f['cost'] for f in fills);returns=[f['pnl']/f['cost'] for f in fills]
    report={'kind':'adaptive_swing','version':VERSION,'status':'complete','created':time.time(),'settings':SETTINGS,
            'split':{'training_contracts':a,'calibration_contracts':b-a,'test_contracts':n-b,'calibration_start':cal_start,'test_start':test_start},
            'calibration':calibration,'gap_hazard':hazard,'process_states':{k:len(v) for k,v in process.items()},
            'training_transition_count':len(train),'calibration_transition_count':len(cal),'evaluated_plans':evaluated,
            'execution':{'starting_cash':100.,'ending_cash':cash,'net_pnl':cash-100,'trades':len(fills),'censored_trades':sum(f['status']=='censored' for f in fills),
                         'return_on_deployed_capital':sum(f['pnl'] for f in fills)/total_cost if total_cost else None,
                         'positive_trade_rate':sum(r>0 for r in returns)/len(returns) if returns else None,
                         'maximum_replay_drawdown':drawdown,'realized_log_growth':math.log(cash/100) if cash>0 else None,'skips':dict(reasons),'fills':fills[-100:]},
            'selection_rule':'Process fit on first 50% of contracts; drift trust/variance calibrated on next 20%; frozen evaluation on last 30%. Training/calibration labels are purged at the next boundary. No realized test-profit parameter search.',
            'objective':'Maximize predicted expected log wealth growth per holding second, conditional on positive conservative net return, fractional Kelly and a portfolio expected-shortfall budget.',
            'limits':'Research approximation, not a guarantee of positive long-run ROI. Conditional logit diffusion, fixed within-trade process parameters/spread and empirical innovation resampling can misprice regime changes. Barriers are model-selected quantile candidates, not a global optimal-stopping solution. The conservative mean is a parameter-scenario quantile, not an empirical confidence interval. Missing exits are full-cost stress losses. This historical test is exploratory, not a new untouched lockbox. No portfolio order connection.'}
    by_contract=defaultdict(float)
    for f in fills:by_contract[f['ticker']]+=f['pnl']
    growth_gate={'passed':False,'reason':'Need at least 30 closed trades across 20 contract windows with a positive held-out P&L interval','contracts':len(by_contract)}
    if len(fills)>=30 and len(by_contract)>=20:
        import numpy as np
        values=np.array(list(by_contract.values()));rng=np.random.default_rng(72)
        boot=rng.choice(values,size=(2000,len(values)),replace=True).mean(axis=1);interval=np.quantile(boot,[.025,.975]).tolist()
        passed=interval[0]>0 and cash>100
        growth_gate={'passed':bool(passed),'reason':'Positive held-out growth evidence' if passed else 'Held-out net growth is not established','contracts':len(values),'mean_pnl_interval_95':interval}
    report['growth_gate']=growth_gate;bundle['growth_gate']=growth_gate
    atomic_json(Path(directory)/'adaptive-swing-model.json',bundle);atomic_json(Path(directory)/'adaptive-swing-report.json',report)
    return report


def live(row,sigma,bundle,target=None):
    target=row.get('strike') if target is None else target
    touch=touch_probability(row['spot'],target,max(0,row['seconds_left']),sigma) if sigma and target and row.get('spot') else None
    result={'ts':row['ts'],'ticker':row['ticker'],'spot':row.get('spot'),'target':target,'seconds_left':row['seconds_left'],
            'p_btc_touch_before_close':touch,'p_settlement_yes':row.get('baseline'),'sigma':sigma,'target_basis':'trade_cost',
            'settings':SETTINGS,'sides':[],'note':'Adaptive exits maximize modeled log growth after costs; Kelly sizes the return distribution. BTC touch and settlement probabilities are separate benchmarks. No fixed profit target or stop percentage.'}
    for side in ('yes','no'):
        ask,_=quote(row,side,True);bid,bq=quote(row,side);m=row.get('fee_multiplier')
        p=plan(bundle,row,side) if bundle and valid(row) else {'quantity':0,'reason':'Train adaptive model or wait for fresh, supported inputs'}
        if p['quantity']>0 and not bundle.get('growth_gate',{}).get('passed'):
            p={**p,'quantity':0,'capital_fraction':0.,'reason':'WAIT: positive held-out net growth has not been established'}
        one=purchase(1,ask,m) if m is not None else None
        result['sides'].append({'side':side,'ask':ask,'bid':bid,'one_contract_cost':one,
            'net_if_sold_now':sale(1,bid,m)-one if one and bq>=1 else None,'plan':p,
            'quantity':p['quantity'],'research_candidate':p['quantity']>0,'bankroll_fraction':p.get('capital_fraction',0),'reason':p['reason']})
    return result
