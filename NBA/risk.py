"""Read-only portfolio planner and manual-position alerts. Never places orders."""
import json
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from .data import DATA,instant,utcnow
from .markets import fee_terms,sweep
from .service import public_snapshot
from .quotes import load_quotes,contract_matches_game

DEFAULTS={'equity':1000.,'cash':1000.,'other_exposure':0.,'sizing_mode':'scalp','scalp_hit_rate':None,
          'kelly_fraction':1.,'max_game_fraction':1.,'max_total_fraction':1.,'probability_buffer':.02,
          'minimum_edge':.03,'stop_move':.05,'target_move':.10,'gap_move':.02,'max_contracts':10000,
          'hold_minutes':20,'slippage':.005,'max_loss_fraction':None,'risk_loss_basis':'full_loss','max_model_market_gap':.15}
BOUNDS={'equity':(0,1e9),'cash':(0,1e9),'other_exposure':(0,1e9),'kelly_fraction':(0,1),
        'max_game_fraction':(0,1),'max_total_fraction':(0,1),'probability_buffer':(0,.25),
        'minimum_edge':(0,.5),'stop_move':(.01,.5),'target_move':(.01,.9),'hold_minutes':(1,240),'slippage':(0,.1)}
BOUNDS.update(gap_move=(0,.5),max_contracts=(1,20000))
BOUNDS['max_model_market_gap']=(0,.15)


def settings(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('Unknown risk settings')
    result={**DEFAULTS,**value}
    if result['sizing_mode'] not in ('scalp','settlement'):raise ValueError('Unknown sizing mode')
    if result['risk_loss_basis'] not in ('full_loss','gap_stop'):raise ValueError('Unknown loss-budget basis')
    limit=result['max_loss_fraction']
    if limit is not None and (isinstance(limit,bool) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or not 0<=limit<=1):raise ValueError('Loss budget must be a portfolio fraction or left unset')
    rate=result['scalp_hit_rate']
    if rate is not None and (isinstance(rate,bool) or not isinstance(rate,(int,float)) or not math.isfinite(rate) or not 0<=rate<=1):raise ValueError('Scalp hit rate must be a probability or left unset')
    if type(result['max_contracts']) is not int:raise ValueError('Maximum contracts must be an integer')
    for key,(lo,hi) in BOUNDS.items():
        n=result[key]
        if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not lo<=n<=hi:raise ValueError('Invalid '+key.replace('_',' '))
    if result['max_game_fraction']>result['max_total_fraction']:raise ValueError('Per-game limit must not exceed the total NBA limit')
    return result


def positions(value):
    if not isinstance(value,list) or len(value)>100:raise ValueError('At most 100 tracked positions are supported')
    result=[];seen=set()
    for item in value:
        if not isinstance(item,dict):raise ValueError('Invalid tracked position')
        p=dict(item)
        for key in ['id','game_id','ticker']:
            if not isinstance(p.get(key),str) or not 1<=len(p[key])<=100:raise ValueError('Invalid position '+key)
        if p['id'] in seen:raise ValueError('Duplicate position ID')
        seen.add(p['id'])
        if p.get('side') not in ('yes','no'):raise ValueError('Invalid contract side')
        q=p.get('quantity');price=p.get('entry_price');fees=p.get('entry_fees',0)
        if type(q) is not int or not 1<=q<=100000:raise ValueError('Invalid position quantity')
        if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or not 0<price<1:raise ValueError('Invalid entry price')
        if isinstance(fees,bool) or not isinstance(fees,(int,float)) or not math.isfinite(fees) or not 0<=fees<=q:raise ValueError('Invalid entry fees')
        p['entry_fees']=fees;p['entry_cost']=q*price+fees;p['opened_at']=instant(p['opened_at']).isoformat()
        if p.get('state','open') not in ('open','closed'):raise ValueError('Invalid position state')
        for key in ['stop_bid','target_bid','initial_target_bid']:
            n=p.get(key)
            if n is not None and (isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not .001<=n<1):raise ValueError('Invalid '+key)
        if 'dynamic_exits' in p and type(p['dynamic_exits']) is not bool:raise ValueError('Invalid dynamic exit setting')
        if p.get('dynamic_started_at') is not None:
            if not isinstance(p['dynamic_started_at'],str):raise ValueError('Invalid dynamic monitoring timestamp')
            p['dynamic_started_at']=instant(p['dynamic_started_at']).isoformat()
        result.append(p)
    return result


def kelly_fraction(probability,cost):
    return max(0.,min(1.,(probability-cost)/(1-cost))) if 0<cost<1 else 0.


def fee_at(price,quantity,multiplier):
    return math.ceil((.07*multiplier*quantity*price*(1-price)-1e-10)*100)/100


def scalp_kelly(hit_rate,gain,loss,cost):
    """Capital and planned-loss fractions for a hypothetical two-exit scalp.

    Maximize q log(1+n*g/B)+(1-q) log(1-n*l/B). Unlike settlement,
    the losing outcome is the assumed stop fill, not the entire entry cost.
    Capital can exceed 100% theoretically; cash constraints apply separately.
    """
    if hit_rate is None or gain<=0 or loss<=0 or cost<=0:return {'capital_fraction':0.,'risk_fraction':0.}
    risk=max(0.,hit_rate-(1-hit_rate)*loss/gain)
    return {'capital_fraction':risk*cost/loss,'risk_fraction':risk}


def exits(quantity,entry_cost,entry_price,multiplier,config,stop=None,target=None):
    stop=stop if stop is not None else max(.01,entry_price-config['stop_move'])
    target=target if target is not None else min(.99,entry_price+config['target_move'])
    stop_net=quantity*stop-fee_at(stop,quantity,multiplier)-quantity*config['slippage']
    target_net=quantity*target-fee_at(target,quantity,multiplier)-quantity*config['slippage']
    gain=target_net-entry_cost;loss=entry_cost-stop_net
    gap_bid=max(.001,stop-config['gap_move'])
    gap_net=quantity*gap_bid-fee_at(gap_bid,quantity,multiplier)-quantity*config['slippage']
    return {'stop_bid':stop,'target_bid':target,'net_at_stop':stop_net-entry_cost,'net_at_target':gain,
            'settlement_max_loss':entry_cost,'break_even_scalp_hit_rate':loss/(loss+gain) if loss>0 and gain>0 else None,
            'gap_bid':gap_bid,'net_at_gap_stop':gap_net-entry_cost,
            'target_net_profitable':gain>0,'assumption':'Hypothetical two-exit scalp. All non-target/time-limit outcomes are valued at the assumed stop; actual exits can differ.'}


def preview_route(route,config):
    levels=route['book'][route['side']+'_asks'];q=min(10,config['max_contracts'],int(sum(v['quantity'] for v in levels)))
    if q<1:return None
    fill=sweep(levels,q,route['multiplier']);cost=fill['price_cost']+fill['fee_upper_estimate']+q*config['slippage']
    if route['sizing_probability']-cost/q<config['minimum_edge']:return None
    item={k:v for k,v in route.items() if k not in ('book','multiplier')}
    item.update(quantity=q,entry_vwap=fill['vwap'],entry_fees=fill['fee_upper_estimate'],all_in_cost=cost,
                exit_plan=exits(q,cost,fill['vwap'],route['multiplier'],config),preview_only=True)
    e=item['exit_plan'];item['preview_kelly']=scalp_kelly(item.get('scalp_hit_rate',config['scalp_hit_rate']),e['net_at_target'],-e['net_at_stop'],cost)
    return item


def size_route(route,budget,config):
    """Evaluate whole contracts using actual ask depth and quantity-specific fees."""
    B=config['equity'];p=route['sizing_probability'];alpha=config['kelly_fraction'];best=None
    scalp=config['sizing_mode']=='scalp';hit_rate=route.get('scalp_hit_rate',config['scalp_hit_rate'])
    if scalp and hit_rate is None:return None
    if config['max_loss_fraction'] is None:return None
    if B<=0 or budget<=0 or alpha<=0:return None
    levels=route['book'][route['side']+'_asks']
    max_q=min(config['max_contracts'],int(sum(level['quantity'] for level in levels)))
    for q in range(1,max_q+1):
        fill=sweep(levels,q,route['multiplier'])
        if not fill:break
        cost=fill['price_cost']+fill['fee_upper_estimate']+q*config['slippage']
        if cost>budget+1e-9:break
        c=cost/q;edge=p-c;exit_plan=exits(q,cost,fill['vwap'],route['multiplier'],config)
        risk_amount=cost if config['risk_loss_basis']=='full_loss' else max(0.,-exit_plan['net_at_gap_stop'])
        if risk_amount>B*config['max_loss_fraction']+1e-9:break
        metrics=scalp_kelly(hit_rate,exit_plan['net_at_target'],-exit_plan['net_at_stop'],cost) if scalp else None
        k=metrics['capital_fraction'] if scalp else kelly_fraction(p,c)
        if edge<config['minimum_edge'] or k<=0:continue
        # Full scalp Kelly directly maximizes the stated two-exit log-growth
        # objective over fundable whole quantities. Fractional sizing also
        # limits exposure to the local payoff-based fractional Kelly target.
        if (not scalp or alpha<1) and cost>B*alpha*k+1e-9:continue
        if scalp:
            if exit_plan['net_at_target']<=0 or exit_plan['net_at_stop']>=0 or -exit_plan['net_at_stop']>=B:continue
            growth=hit_rate*math.log1p(exit_plan['net_at_target']/B)+(1-hit_rate)*math.log1p(exit_plan['net_at_stop']/B)
        else:growth=p*math.log1p((q-cost)/B)+(1-p)*math.log1p(-cost/B)
        item={k:v for k,v in route.items() if k not in ('book','multiplier')}
        item.update(quantity=q,all_in_cost=cost,stake_fraction=cost/B,entry_vwap=fill['vwap'],entry_fees=fill['fee_upper_estimate'],
                    all_in_per_contract=c,net_edge=edge,full_kelly_fraction=k,fractional_kelly_target=k*alpha,
                    expected_profit=hit_rate*exit_plan['net_at_target']+(1-hit_rate)*exit_plan['net_at_stop'] if scalp else q*p-cost,
                    net_if_win=q-cost,expected_log_growth=growth,exit_plan=exit_plan,sizing_mode=config['sizing_mode'],
                    scalp_hit_rate=hit_rate if scalp else None,planned_stop_risk_fraction=-exit_plan['net_at_stop']/B,
                    gap_stop_risk_fraction=-exit_plan['net_at_gap_stop']/B,
                    loss_budget_amount=B*config['max_loss_fraction'],loss_budget_measure=risk_amount,loss_budget_basis=config['risk_loss_basis'],
                    full_kelly_risk_fraction=metrics['risk_fraction'] if scalp else k,
                    gap_kelly=scalp_kelly(hit_rate,exit_plan['net_at_target'],-exit_plan['net_at_gap_stop'],cost) if scalp else None)
        if best is None or growth>best['expected_log_growth']:best=item
    return best


def allocate(routes,config,open_positions):
    occupied=sum(p['entry_cost'] for p in open_positions)+config['other_exposure']
    remaining=min(max(0.,config['equity']*config['max_total_fraction']-occupied),config['cash'],max(0.,config['equity']-occupied))
    tracked_games={p['game_id'] for p in open_positions};desired={}
    for game_id,options in routes.items():
        if game_id in tracked_games:continue
        choices=[s for option in options if (s:=size_route(option,config['equity']*config['max_game_fraction'],config))]
        if choices:desired[game_id]=max(choices,key=lambda r:r['expected_log_growth'])
    total=sum(p['all_in_cost'] for p in desired.values());scale=min(1.,remaining/total) if total else 0.
    plans={}
    for game_id,desired_plan in desired.items():
        budget=desired_plan['all_in_cost']*scale
        choices=[s for option in routes[game_id] if (s:=size_route(option,budget,config))]
        if choices:
            plan=max(choices,key=lambda r:r['expected_log_growth']);plan['unscaled_stake_fraction']=desired_plan['stake_fraction']
            plan['individual_cost']=desired_plan['all_in_cost'];plan['individual_quantity']=desired_plan['quantity']
            plan['portfolio_scaled']=scale<1;plans[game_id]=plan
    total_new=sum(p['all_in_cost'] for p in plans.values())
    return plans,{'tracked_exposure':occupied-config['other_exposure'],'existing_exposure':occupied,'new_entry_budget':remaining,
                  'existing_exceeds_cap':occupied>config['equity']*config['max_total_fraction']+1e-9,
                  'proposed_cost':total_new,'proposed_fraction':total_new/config['equity'] if config['equity'] else 0,
                  'total_exposure_after':occupied+total_new,'total_fraction_after':(occupied+total_new)/config['equity'] if config['equity'] else None,
                  'scale':scale,'eligible_games':len(desired),'allocated_games':len(plans)}




def tracked_alert(position,quote,contract,config,now,directory=DATA):
    p=position;q=p['quantity'];cost=p['entry_cost'];stop=p.get('stop_bid') if p.get('stop_bid') is not None else max(.01,p['entry_price']-config['stop_move'])
    target=p.get('target_bid') if p.get('target_bid') is not None else min(.99,p['entry_price']+config['target_move'])
    minutes=p.get('hold_minutes',config['hold_minutes'])
    if not isinstance(minutes,(int,float)) or isinstance(minutes,bool) or not math.isfinite(minutes) or not 1<=minutes<=240:raise ValueError('Invalid position holding time')
    elapsed=(now-instant(p['opened_at'])).total_seconds()/60;due=elapsed>=minutes
    result={'id':p['id'],'game_id':p['game_id'],'ticker':p['ticker'],'side':p['side'],'quantity':q,'entry_price':p['entry_price'],
            'entry_cost':cost,'stop_bid':stop,'target_bid':target,'time_due':due,'held_minutes':max(0,elapsed),
            'max_loss':cost,'status':'quote_unavailable','net_liquidation':None,'unrealized_pnl':None,'hold_minutes':minutes}
    result['initial_target_bid']=p.get('initial_target_bid',target)
    result['opened_at']=p['opened_at']
    result['deadline_at']=(instant(p['opened_at'])+timedelta(minutes=minutes)).isoformat()
    result['target_probability']={'status':'not_calibrated','probability':None,'reason':'No validated model estimates target-before-stop success from the current game and order book.'}
    multiplier=(quote or {}).get('multiplier')
    if multiplier is not None:
        result['exit_plan']=exits(q,cost,p['entry_price'],multiplier,config,stop,target)
        result['break_even_bid']=next((i/100 for i in range(1,100) if q*i/100-fee_at(i/100,q,multiplier)-q*config['slippage']>=cost-1e-9),None)
    if p.get('dynamic_exits'):
        from .dynamic_exits import load_samples,advance
        import sqlite3
        dynamic_position={**p,'stop_bid':stop,'target_bid':target}
        try:
            fresh_book=bool(quote and quote.get('book') and quote.get('status') in ('active','open') and multiplier is not None
                and -5<=(now-instant(quote['observed_at'])).total_seconds()<=30
                and sweep(quote['book'][p['side']+'_bids'],q,multiplier)
                and sweep(quote['book'][p['side']+'_asks'],q,multiplier))
            samples=load_samples(directory,p,min(now,instant(quote['observed_at'])),multiplier) if fresh_book else []
            result['dynamic_exits']=advance(dynamic_position,config,now,samples,result.get('break_even_bid'))
        except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
            result['dynamic_exits']=advance(dynamic_position,config,now,[],result.get('break_even_bid'))
    if result.get('dynamic_exits'):
        stop=result['dynamic_exits']['active_stop'];target=result['dynamic_exits']['active_target']
        result.update(stop_bid=stop,target_bid=target)
        if multiplier is not None:result['exit_plan']=exits(q,cost,p['entry_price'],multiplier,config,stop,target)
    if contract and contract.get('status') in ('settled','finalized'):
        value=contract.get('settlement_value')
        if value is None and contract.get('result') in ('yes','no'):value=float(contract['result']=='yes')
        if value is not None:
            value=value if p['side']=='yes' else 1-value
            result.update(status='settled',settlement_price=value,net_liquidation=q*value,unrealized_pnl=q*value-cost)
        return result
    if not quote or not quote.get('book') or not -5<=(now-instant(quote['observed_at'])).total_seconds()<=30 or quote.get('status') not in ('active','open'):
        if due:result['status']='time_due_quote_unavailable'
        return result
    bids=quote['book'][p['side']+'_bids'];result['quote_at']=quote['observed_at']
    available=int(sum(level['quantity'] for level in bids));result['available_contracts']=available
    multiplier=quote.get('multiplier');fill=sweep(bids,q,multiplier) if multiplier is not None else None
    trigger=fill['vwap'] if fill else (bids[0]['price'] if bids else None)
    result['bid_vwap']=fill['vwap'] if fill else None
    if fill:
        net=fill['price_cost']-fill['fee_upper_estimate']-q*config['slippage']
        result.update(net_liquidation=net,unrealized_pnl=net-cost,exit_fee_estimate=fill['fee_upper_estimate'])
    dynamic=result.get('dynamic_exits')
    result['status']='watching' if fill else 'insufficient_depth_or_fees'
    if trigger is not None and trigger<=stop+1e-9:result['status']='stop_reached' if fill else 'stop_reached_insufficient_depth'
    elif trigger is not None and trigger>=target-1e-9:result['status']='target_reached' if fill else 'target_touched_insufficient_depth'
    elif due:result['status']='time_due'
    if dynamic and fill and dynamic['state'].get('trigger'):
        result['status']=dynamic['state']['trigger']+'_reached'
        result['exit_rule_previously_reached']=True
    return result


def risk_plan(body,directory=DATA,now=None):
    now=now or utcnow();config=settings(body.get('settings',{}));ledger=positions(body.get('positions',[]))
    hit_rates=body.get('hit_rates',{})
    if not isinstance(hit_rates,dict) or len(hit_rates)>1500:raise ValueError('Invalid per-game scalp hit rates')
    for key,value in hit_rates.items():
        if not isinstance(key,str) or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:raise ValueError('Invalid per-game scalp hit rate')
    open_positions=[p for p in ledger if p.get('state','open')=='open']
    snapshot=public_snapshot(directory,now);quotes,contracts=load_quotes(directory,now);routes=defaultdict(list);reasons={}
    for p in open_positions:
        if p.get('dynamic_started_at') and instant(p['dynamic_started_at'])>now+timedelta(seconds=5):raise ValueError('Dynamic monitoring start cannot be in the future')
        if instant(p['opened_at'])>now+timedelta(seconds=5):raise ValueError('Position entry time cannot be in the future')
        if p['ticker'] in contracts and p['game_id']!=contracts[p['ticker']]['game_id']:raise ValueError('Position contract does not match its game')
    for game in snapshot.get('games',[]):
        game_id=game['game_id'];prediction=game.get('prediction')
        if instant(game['tipoff'])<=now:
            reasons[game_id]='Game started: a pregame probability cannot size an in-game entry.';continue
        if prediction is None or game.get('status')!='research_estimate' or game.get('blockers'):
            reasons[game_id]='Waiting for a current executable pregame estimate.';continue
        if game.get('entry_safety',{}).get('blocked',True):
            reasons[game_id]=' '.join(game.get('entry_safety',{}).get('messages',[])) or 'Availability verification is required before sizing a new entry.';continue
        home_p=prediction['home_win_probability'];bounds=game.get('availability_probability_range') or {'home_min':home_p,'home_max':home_p}
        reasons[game_id]='No fresh qualifying route after costs, probability buffer and limits.'
        for quote in quotes.values():
            if quote['game_id']!=game_id or quote['yes_team'] not in (game['home'],game['away']) or not quote.get('book') or quote.get('multiplier') is None or quote['status'] not in ('active','open') or quote['score_state'] in ('in','post'):continue
            if not contract_matches_game(quote['ticker'],quote['yes_team'],game):continue
            for side in ('yes','no'):
                outcome=quote['yes_team'] if side=='yes' else (game['away'] if quote['yes_team']==game['home'] else game['home'])
                baseline=home_p if outcome==game['home'] else 1-home_p
                adverse=bounds['home_min'] if outcome==game['home'] else 1-bounds['home_max']
                bids=quote['book'].get(side+'_bids',[]);asks=quote['book'].get(side+'_asks',[])
                if not bids or not asks:
                    reasons[game_id]='Both sides of a fresh market are needed to check model disagreement.';continue
                market_mid=(bids[0]['price']+asks[0]['price'])/2
                if abs(baseline-market_mid)>config['max_model_market_gap']+1e-9:
                    reasons[game_id]=f'Model/market disagreement exceeds {config["max_model_market_gap"]*100:g} percentage points. Review the model and injury context before entry.';continue
                routes[game_id].append({'game_id':game_id,'ticker':quote['ticker'],'side':side,'yes_team':quote['yes_team'],
                    'outcome_team':outcome,'model_probability':baseline,'scenario_probability':adverse,
                    'sizing_probability':max(0.,adverse-config['probability_buffer']),'scalp_hit_rate':hit_rates.get(game_id,config['scalp_hit_rate']),'quote_at':quote['observed_at'],
                    'book':quote['book'],'multiplier':quote['multiplier']})
    previews={}
    for game_id,options in routes.items():
        values=[p for option in options if (p:=preview_route(option,config))]
        if values:
            previews[game_id]=max(values,key=lambda p:p['sizing_probability']-p['all_in_cost']/p['quantity'])
            if config['sizing_mode']=='scalp':
                chosen=previews[game_id]
                routes[game_id]=[r for r in options if r['outcome_team']==chosen['outcome_team']]
                if hit_rates.get(game_id,config['scalp_hit_rate']) is None:reasons[game_id]='Enter your target-before-stop hit-rate assumption to calculate scalp Kelly.'
                elif chosen['preview_kelly']['capital_fraction']==0:reasons[game_id]='The assumed scalp hit rate does not beat the target/stop payoff after costs.'
            if config['max_loss_fraction'] is None:reasons[game_id]='Set your maximum per-game loss budget before a cash-funded allocation. A stop does not cap full contract loss.'
    allocations,summary=allocate(routes,config,open_positions)
    summary['entry_signal_games']=len(previews)
    evidence_path=Path(directory)/'paper'/'status.json'
    evidence=json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
    for p in open_positions:reasons[p['game_id']]='Existing tracked exposure: review that position before adding another bet.'
    alerts=[tracked_alert(p,quotes.get(p['ticker']),contracts.get(p['ticker']),config,now,directory) for p in open_positions]
    for position,alert in zip(open_positions,alerts):
        alert['availability_alerts']=[c for c in snapshot.get('availability_updates',{}).get('changes',[])
            if c['game_id']==position['game_id'] and instant(c['observed_at'])>=instant(position['opened_at'])]
    from .dashboard_risk import dashboard_assessments
    from .scalp_trial import trial_status
    trial=trial_status(directory)
    checks=dashboard_assessments(snapshot,quotes,config,open_positions,directory,now,trial)
    return {'risk_checks':checks,'risk_trial':trial,'generated_at':now.isoformat(),'model_id':snapshot.get('model_id'),'settings':config,'summary':summary,
            'allocations':allocations,'previews':previews,'reasons':reasons,'positions':alerts,
            'scalp_evidence':{'matched_paper_entries':evidence.get('matched_closed_entries'),'hit_rate_model_fitted':False},
            'contracts':[{'ticker':m['ticker'],'game_id':m['game_id'],'yes_team':m['yes_team'],'status':m['status']} for m in contracts.values()],
            'quote_max_age_seconds':90,'exit_quote_max_age_seconds':30,'plan_max_age_seconds':20,'max_sized_contracts':config['max_contracts'],
            'basis':'Scalp target/stop payoff Kelly with a user-supplied hit rate; theoretical size and cash-funded basket shown separately.' if config['sizing_mode']=='scalp' else 'Settlement Kelly.',
            'limitations':['A user-selected probability buffer is not a statistical confidence interval.',
                'Scalp hit rates are user assumptions; the current final-winner model does not estimate barrier-hit probability.',
                'Stops/targets are manual monitoring levels, not exchange orders or guaranteed fills.',
                'Scalp Kelly assumes stop execution; gap stress and full contract loss are separate risks. Cash cannot be levered.',
                'Portfolio/cash inputs and fills are manually maintained; no account is connected.']}
