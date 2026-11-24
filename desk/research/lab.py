"""Background research jobs, online quote forecasts and cached dashboard state."""
import copy
import logging
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from .store import Store
from .history import backfill,RangeContext
from .markov import Markov,pairs,backtest,HORIZONS

LOG=logging.getLogger('research')


class Lab:
    def __init__(self,path,asset):
        self.asset=asset;self.store=Store(path);self.lock=threading.RLock();self.stop=threading.Event()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix=asset+'-research')
        self.job={'status':'idle','kind':None};self.settings={'horizon':5,'interior':True,'range_filter':True,**self.store.settings()}
        self.cached={'stats':self.store.stats(),'live':None,'lstm_live':None,'range':None,'reports':self.store.reports()}
        self.bundle=None;self.error=None

    def configure(self,update):
        if set(update)-set(self.settings):raise ValueError('Unknown research setting')
        if 'horizon' in update and (type(update['horizon']) is not int or update['horizon'] not in HORIZONS):raise ValueError('Choose 2, 5 or 10 seconds')
        for key in ('interior','range_filter'):
            if key in update and type(update[key]) is not bool:raise ValueError(key+' must be boolean')
        with self.lock:
            self.settings.update(update);self.cached['live']=None;self.cached['matrix']=[[0,0,0] for _ in range(3)]
            self.store.save_settings(self.settings)

    def status(self):
        with self.lock:
            return copy.deepcopy({**self.cached,'job':self.job,'settings':self.settings,'error':self.error,
                                  'feed_note':'Public REST snapshots; 500 ms target, receipt times rather than exchange-event times. Cached or unchanged quotes are retained.',
                                  'history_note':'Book history starts at collection. Three-day reference candles are a separate dataset, not reconstructed past books.'})

    def start_job(self,kind,request):
        if kind not in ('history','markov','lstm'):raise ValueError('Unknown experiment')
        with self.lock:
            if self.job['status']=='running':raise ValueError('A research job is already running; wait for it to finish')
            self.job={'status':'running','kind':kind,'progress':0.,'message':'Starting','started':time.time()}
            settings=dict(self.settings)
        self.pool.submit(self._job,kind,request,settings)

    def progress(self,value,message):
        with self.lock:self.job.update(progress=value,message=message)

    def _request(self,request,url):
        for attempt in range(4):
            if self.stop.is_set():raise RuntimeError('Research stopped')
            try:return request(url)
            except urllib.error.HTTPError as exc:
                if exc.code!=429 or attempt==3:raise
                self.stop.wait(2**attempt)

    def _job(self,kind,request,settings):
        try:
            if kind=='history':
                backfill(self.asset,lambda u:self._request(request,u),self.store.save_candles,self.progress,self.stop.is_set)
                result={'status':'complete','model':'history','message':'Reference history downloaded; see actual coverage below'}
            elif kind=='markov':
                result=backtest(self.store.rows(50000),settings['horizon'],settings['interior'])
                result.setdefault('model','markov')
            else:
                from .lstm import train
                result,bundle=train(self.store.rows(20000),self.store.candles(),self.store.path.with_suffix('.lstm.pt'),
                                    settings['horizon'],settings['interior'],settings['range_filter'],self.progress,self.stop.is_set)
                if bundle:
                    with self.lock:self.bundle=bundle
            result['created']=time.time()
            self.store.save_report(result)
            with self.lock:self.job.update(status=result['status'],progress=1.,message=result.get('reason',result.get('message','Experiment complete')))
        except Exception as exc:
            LOG.exception('%s research job failed',kind)
            with self.lock:self.job.update(status='error',message=str(exc))
        self.refresh()

    def refresh(self):
        rows=self.store.rows(6000);candles=self.store.candles()
        with self.lock:settings=dict(self.settings);bundle=self.bundle
        samples=pairs(rows,settings['horizon'],settings['interior'])
        model=Markov()
        # Pair construction includes only actually observed future quotes.
        for sample in samples:model.add(sample)
        latest=rows[-1] if rows else None
        live=None
        if latest and latest['mid'] is not None and time.time()-latest['ts']<=2 and (not settings['interior'] or .05<=latest['mid']<=.95):
            live=model.predict(latest)
            if live:live.update(as_of=latest['ts'],target_time=latest['ts']+settings['horizon'],horizon=settings['horizon'],matrix=model.matrix)
        span=RangeContext(candles).at(latest['ts'],latest['spot']) if latest else None
        lstm_live=None
        if bundle and latest and time.time()-latest['ts']<=2:
            from .lstm import predict
            lstm_live=predict(bundle,rows[-20:],candles)
        cached={'stats':self.store.stats(),'live':live,'lstm_live':lstm_live,'range':span,
                'reports':self.store.reports(),'labeled_examples':len(samples),'matrix':model.matrix}
        with self.lock:self.cached=cached

    def run(self,request):
        model_path=self.store.path.with_suffix('.lstm.pt')
        if model_path.exists():
            try:
                from .lstm import load
                self.bundle=load(model_path)
            except Exception as exc:self.error='Saved LSTM could not load: '+str(exc)
        # Automatically start the authorized three-day reference backfill once.
        if self.store.stats()['history_hours']<48:
            self.start_job('history',request)
        while not self.stop.is_set():
            try:self.refresh();self.error=None
            except Exception as exc:
                LOG.warning('Research analysis: %s',exc);self.error=str(exc)
            self.stop.wait(1)

    def close(self):
        self.stop.set();self.pool.shutdown(wait=True,cancel_futures=True);self.store.close()
