"""Point-in-time outcome features, with missing source data left missing."""
import bisect
import math
import statistics
from .settlement import averaging_probability,YEAR

BASE=['baseline','z_distance','seconds_left','sigma_30m','vol_5m','distance_bps']
FLOW=['cb_depth_5','cb_depth_10','cb_depth_25','cb_microprice_gap_bps','cb_tfi_5','cb_tfi_30','cb_tfi_60',
      'cb_ofi_1','cb_ofi_10','cb_depletion_5','cb_replenishment_10','cb_absorption_10','cb_absorption_60','kr_depth_10','kr_tfi_30','cross_venue_bps']
KALSHI=['kalshi_mid','kalshi_spread','kalshi_imbalance','kalshi_micro_gap','kalshi_bid_qty','kalshi_ask_qty']


class CandleFeatures:
    def __init__(self,candles):
        self.candles=sorted(candles,key=lambda c:c['ts']);self.times=[c['ts'] for c in self.candles]
    def at(self,received):
        i=bisect.bisect_right(self.times,received)
        selected=self.candles[max(0,i-32):i]
        if not selected or received-selected[-1]['ts']>180:return None,None
        returns=[math.log(b['close']/a['close']) for a,b in zip(selected,selected[1:]) if b['ts']-a['ts']==60]
        def vol(n):return statistics.stdev(returns[-n:])*math.sqrt(YEAR/60) if len(returns)>=n else None
        return vol(30),vol(5)


def build(spot,strike,remaining,sigma,vol5,book,venues=None,reference_source='proxy'):
    if not sigma or sigma<=0 or not spot or not strike or remaining<60:return None
    bids,asks=book.get('yes_bids',[]),book.get('yes_asks',[])
    if not bids or not asks:return None
    b,a=bids[0]['price'],asks[0]['price'];bq,aq=bids[0]['quantity'],asks[0]['quantity']
    if not 0<b<=a<1:return None
    depth_b=sum(l['quantity'] for l in bids[:5]);depth_a=sum(l['quantity'] for l in asks[:5])
    features={'baseline':averaging_probability(spot,strike,remaining,sigma),'seconds_left':remaining,'sigma_30m':sigma,'vol_5m':vol5,
              'z_distance':math.log(spot/strike)/(sigma*math.sqrt(remaining/YEAR)),
              'distance_bps':(spot/strike-1)*10000,'kalshi_mid':(a+b)/2,'kalshi_spread':a-b,
              'kalshi_imbalance':(depth_b-depth_a)/(depth_b+depth_a) if depth_b+depth_a else 0,
              'kalshi_micro_gap':(a*bq+b*aq)/(bq+aq)-(a+b)/2 if bq+aq else 0,
              'kalshi_bid_qty':bq,'kalshi_ask_qty':aq,'yes_ask':a,'no_ask':1-b,'reference_source':reference_source}
    for name in FLOW:features[name]=None
    venues=venues or {};cb=venues.get('coinbase',{});kr=venues.get('kraken',{})
    for prefix,source in [('cb',cb),('kr',kr)]:
        for key,value in source.items():
            name=prefix+'_'+key
            if name in FLOW:features[name]=value
    if cb.get('mid') and kr.get('mid'):features['cross_venue_bps']=(cb['mid']/kr['mid']-1)*10000
    features['flow_ready']=cb.get('tfi_60') is not None and cb.get('depth_10') is not None
    return features
