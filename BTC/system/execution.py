"""Latency/depth replay held to official settlement; independent $100 bankroll."""
import math
from .evaluation import predict


def fee(q,p,m):return math.ceil((.07*m*q*p*(1-p)-1e-10)*100)/100


def cash_cost(notional,model_fee):
    """Non-direct member purchase: align the whole balance change to cents."""
    return math.ceil((notional+model_fee-1e-10)*100)/100


def replay(rows,artifact,quote_after,bankroll=100.,latency=.25,kelly=.25,cap=.02,min_edge=.03,policy=None):
    policy=policy or {};reasons={}
    def reject(reason):reasons[reason]=reasons.get(reason,0)+1
    cash=bankroll;pending=[];trades=[];equity=[{'ts':rows[0]['ts'] if rows else 0,'equity':bankroll}];attempts=0;rejected=0;partial=0;seen=set()
    def settle(now):
        nonlocal cash,pending
        for pos in sorted(list(pending),key=lambda p:p['available']):
            if pos['available']<=now:
                payout=pos['quantity'] if pos['won'] else 0;cash+=payout
                pos['pnl']=payout-pos['cost'];pending.remove(pos)
                equity.append({'ts':pos['available'],'equity':cash+sum(p['cost'] for p in pending)})

    for row in sorted(rows,key=lambda r:r['ts']):
        settle(row['ts'])
        if row['ticker'] in seen:continue
        if policy.get('enabled') is False:continue
        if row['x'].get('seconds_left',900)<policy.get('min_seconds',0):reject('entry cutoff');continue
        if row['x'].get('kalshi_spread',0)>policy.get('max_spread',1):reject('wide spread');continue
        if row['ts']-row['x'].get('spot_received',row['ts'])>policy.get('max_reference_age',float('inf')):reject('stale reference');continue
        p=artifact['predictions'][(row['ticker'],row['ts'])] if artifact['kind']=='received_predictions' else predict(artifact,row['x'])
        source=row['x'];choices=[('yes',p,source['yes_ask']),('no',1-p,source['no_ask'])]
        side,probability,ask=max(choices,key=lambda c:c[1]-c[2])
        if probability-ask<=min_edge:continue
        attempts+=1;quote=quote_after(row['ticker'],row['ts']+latency)
        if not quote or quote['fee'] is None or quote['ts']>=row['market']['close_ts']:
            rejected+=1;reject('missing quote or fees');continue
        levels=quote['book'].get(side+'_asks',[])
        if not levels:rejected+=1;reject('missing ask depth');continue
        best=levels[0]['price'];m=quote['fee'];unit=best+.07*m*best*(1-best)
        full=max(0,(probability-unit)/(1-unit)) if unit<1 else 0
        current=cash+sum(p['cost'] for p in pending)
        budget=max(0,min(cash,current*cap,current*full*kelly,current*.05-sum(p['cost'] for p in pending)))
        wanted=math.floor(budget/unit) if unit>0 else 0
        quantity=0;cost=0.;notional=0.;model_fee=0.
        for level in levels:
            price=level['price'];maximum=min(wanted-quantity,math.floor(level['quantity']))
            def estimate(q):
                # Round each fill's fee to microdollars, then apply the order's
                # balance rounding once, including sub-cent principal.
                new_fee=math.ceil((.07*m*q*price*(1-price)-1e-12)*1e6)/1e6
                return cash_cost(notional+q*price,model_fee+new_fee),new_fee
            while maximum>0:
                total,new_fee=estimate(maximum)
                if total<=budget+1e-9 and probability-total/(quantity+maximum)>=min_edge:break
                maximum-=1
            if maximum>0:
                cost,new_fee=estimate(maximum);quantity+=maximum;notional+=maximum*price;model_fee+=new_fee
            if quantity>=wanted:break
        if not quantity:rejected+=1;reject('edge, budget or liquidity');continue
        if quantity<wanted:partial+=1
        cash-=cost;seen.add(row['ticker'])
        pos={'ticker':row['ticker'],'decision_ts':row['ts'],'fill_ts':quote['ts'],'side':side,'quantity':quantity,'cost':cost,
             'notional':notional,'fees_and_rounding':cost-notional,'available':row['available'],'won':bool(row['y'])==(side=='yes')}
        pending.append(pos);trades.append(pos)
    settle(float('inf'))
    high=bankroll;drawdown=0
    for point in equity:high=max(high,point['equity']);drawdown=max(drawdown,(high-point['equity'])/high)
    return {'starting_cash':bankroll,'ending_cash':cash,'net_pnl':cash-bankroll,'trades':len(trades),'attempts':attempts,
            'fill_rate':len(trades)/attempts if attempts else None,'rejected':rejected,'partial_fills':partial,
            'maximum_realized_drawdown':drawdown,'latency_ms':latency*1000,'equity_curve':equity,'fills':trades[-100:],
            'fees_and_rounding':sum(t['fees_and_rounding'] for t in trades),'rejection_reasons':reasons,'entry_policy':policy,
            'assumptions':'Quarter Kelly; 2% per-contract and 5% aggregate cost caps; one entry per market; observed ask ladder after latency (≤1s late); non-direct account cent-aligned principal plus fees; capital released at official or conservatively proxied result availability. No queue position or guaranteed fills.'}
