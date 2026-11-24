"""Contract rules and explicit approximation to the final-minute arithmetic average."""
import math
from decimal import Decimal,ROUND_HALF_UP

YEAR=365*86400


def rules(market):
    text=market.get('rules_primary','')
    supported=(market.get('ticker','').startswith('KXBTC15M-') and market.get('strike_type')=='greater_or_equal'
               and 'BRTI' in text and 'average' in text.lower() and 'at least' in text.lower())
    return {'supported':supported,'source':'CF Benchmarks BRTI','window_seconds':60,'tie':'YES (greater than or equal)',
            'precision':2,'primary':text,'secondary':market.get('rules_secondary','')}


def averaging_probability(spot,strike,seconds,sigma,steps=60):
    """Moment-matched lognormal approximation to 60 future GBM index observations.

    Zero physical drift is an explicit benchmark. Final-minute partial averages
    need raw index observations; proxy-only forecasts stop before that window.
    """
    if not all(math.isfinite(x) for x in [spot,strike,seconds,sigma]) or min(spot,strike,sigma)<=0 or seconds<60:
        return None
    times=[(seconds-60+i+1)/YEAR for i in range(steps)]
    second=sum((2*(steps-i)-1)*math.expm1(sigma*sigma*t) for i,t in enumerate(times))/(steps*steps)
    variance=math.log1p(second)
    if variance<=0:return float(spot>=strike)
    z=(math.log(spot/strike)-variance/2)/math.sqrt(variance)
    return .5*(1+math.erf(z/math.sqrt(2)))


def rounded(value):
    return Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)


def reconstruct(ticks,boundary):
    # CF's documented quarter-hour accumulation is (close - 60s, close].
    slots={}
    for tick in ticks:
        ts=tick['exchange_ts']
        if boundary-60<ts<=boundary:
            slot=math.ceil(ts-boundary+60)
            if slot not in slots or ts>slots[slot]['exchange_ts']:slots[slot]=tick
    if len(slots)!=60:return {'coverage':len(slots),'value':None}
    value=sum(Decimal(str(t['price'])) for t in slots.values())/60
    if value*100%1==Decimal('.5'):
        return {'coverage':60,'value':None,'reason':'Half-cent rounding convention requires confirmation'}
    return {'coverage':60,'value':str(rounded(value))}


def verify(market,opening,closing):
    if not rules(market)['supported']:return {'status':'unsupported rules'}
    a=reconstruct(opening,market['open_ts']);b=reconstruct(closing,market['close_ts'])
    if a['value'] is None or b['value'] is None:
        reason=a.get('reason') or b.get('reason') or 'raw BRTI coverage missing'
        return {'status':'unverified: '+reason,'opening_seconds':a['coverage'],'closing_seconds':b['coverage']}
    result='yes' if Decimal(b['value'])>=Decimal(a['value']) else 'no'
    return {'status':'match' if result==market.get('result') and rounded(market['floor_strike'])==Decimal(a['value']) else 'mismatch',
            'opening':a['value'],'closing':b['value'],'reconstructed':result,'official':market.get('result')}
