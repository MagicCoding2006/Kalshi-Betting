"""Past-only reference history. Historical candles are not historical order books."""
import bisect
import math
import time
from collections import deque
from urllib.parse import urlencode, quote

KALSHI='https://external-api.kalshi.com/trade-api/v2'


def gold_candles(ticks, end):
    buckets={}
    for tick in ticks:
        ts,price=float(tick['t'])/1000,float(tick['v'])
        if not math.isfinite(price) or price<=0:continue
        minute=int(ts//60)*60
        if minute+60<=end:buckets.setdefault(minute,{})[int(ts)]=price
    result=[]
    for minute,points in sorted(buckets.items()):
        if len(points)>=50 and min(points)<=minute+5 and max(points)>=minute+55:
            result.append({'ts':minute+60,'close':points[max(points)],'high':max(points.values()),'low':min(points.values())})
    return result


def backfill(asset, request, save, progress, stopped, days=3):
    end=int(time.time()//60)*60
    start=end-days*86400
    if asset=='btc':
        windows=list(range(start,end,240*60))
        for i,left in enumerate(windows):
            if stopped():return
            rows=request('https://api.exchange.coinbase.com/products/BTC-USD/candles?'+urlencode({'granularity':60,'start':left,'end':min(end,left+240*60)}))
            save([{'ts':r[0]+60,'low':float(r[1]),'high':float(r[2]),'close':float(r[4])} for r in rows if start<=r[0] and r[0]+60<=end])
            progress((i+1)/len(windows),'Loading Coinbase minute history')
    else:
        markets=[];cursor=''
        for _ in range(6):
            if stopped():return
            params={'series_ticker':'KXGOLD15M','status':'settled','limit':100,'min_close_ts':start,'max_close_ts':end}
            if cursor:params['cursor']=cursor
            page=request(KALSHI+'/markets?'+urlencode(params))
            markets.extend(page.get('markets',[]));cursor=page.get('cursor','')
            if not cursor:break
        from desk.model import timestamp
        chosen=[];last=float('inf')
        for market in sorted(markets,key=lambda m:timestamp(m['close_time']),reverse=True):
            close=timestamp(market['close_time'])
            if last-close>=3600 or not chosen:
                chosen.append(market);last=close
        if not chosen:raise ValueError('No settled Gold windows available for reference-history backfill')
        for i,market in enumerate(chosen):
            if stopped():return
            data=request(KALSHI+'/live_data/events/'+quote(market['event_ticker'],safe='')+'?range=1h')['live_data']
            details=data.get('details',{})
            if data.get('type')!='commodity' or details.get('asset')!='GOLD':raise ValueError('Unexpected Gold history source')
            # Frozen event charts are intentionally permitted for historical data.
            save([c for c in gold_candles(details.get('timeseries',[]),timestamp(market['close_time'])) if c['ts']>=start])
            progress((i+1)/len(chosen),'Loading frozen Gold reference windows')


class RangeContext:
    """Rolling three-day extrema computed from completed candles only."""
    def __init__(self,candles):
        self.rows=sorted(candles,key=lambda c:c['ts']);self.times=[c['ts'] for c in self.rows]
        self.ranges=[];highs=deque();lows=deque();left=0
        for i,row in enumerate(self.rows):
            while left<i and self.rows[left]['ts']<row['ts']-3*86400:left+=1
            while highs and highs[0]<left:highs.popleft()
            while lows and lows[0]<left:lows.popleft()
            while highs and self.rows[highs[-1]]['high']<=row['high']:highs.pop()
            while lows and self.rows[lows[-1]]['low']>=row['low']:lows.pop()
            highs.append(i);lows.append(i)
            self.ranges.append((self.rows[lows[0]]['low'],self.rows[highs[0]]['high'],row['ts']-self.rows[left]['ts']))

    def at(self,ts,price):
        i=bisect.bisect_right(self.times,ts)-1
        if i<0 or ts-self.times[i]>180 or price is None:return None
        low,high,span=self.ranges[i]
        if high<=low:return None
        return {'low':low,'high':high,'position':(price-low)/(high-low),'coverage_hours':span/3600,'as_of':self.times[i]}
