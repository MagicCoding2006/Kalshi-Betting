import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from desk.server import Engine, Handler, LocalServer, liquidation
from desk.model import normalize_book


def iso(ts):return datetime.fromtimestamp(ts,timezone.utc).isoformat()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=Engine(Path(self.temp.name)/'test.db')
        self.engine.update_config({'bankroll':10000,'drift_mode':'manual','slippage_mode':'manual'})
        now=time.time()
        self.engine.market={'ticker':'BTC-TEST','event_ticker':'BTC-EVENT','status':'active','open_time':iso(now-30),'close_time':iso(now+800),'floor_strike':100.,'strike_type':'greater_or_equal'}
        self.engine.book=normalize_book({'orderbook_fp':{'yes_dollars':[['.49','10000']],'no_dollars':[['.49','10000']]}})
        self.engine.book_time=now
        self.engine.reference={'price':100.1,'time':now,'source':'Coinbase spot proxy'}
        self.engine.volatility=.6
        self.engine.fee_supported=True

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.engine.close()
        self.temp.cleanup()

    def test_paper_accounting_no_repeated_entries_and_realized_pnl(self):
        e=self.engine
        e.open_paper()
        pos=e.positions()[0]
        self.assertAlmostEqual(e.cash+pos['cost'],10000)
        self.assertEqual(e.compute()['action'],'WAIT')
        with self.assertRaises(ValueError):e.open_paper()
        e.close_paper(pos['id'])
        pos=e.positions()[0]
        self.assertEqual(pos['status'],'closed')
        self.assertLess(pos['pnl'],0) # Spread and fees are charged on both legs.
        self.assertAlmostEqual(e.cash,10000+pos['pnl'])
        self.assertEqual(e.compute()['action'],'WAIT')
        with self.assertRaises(ValueError):e.close_paper(pos['id'])
        with self.assertRaises(ValueError):e.update_config({'bankroll':20000})

    def test_official_settlement_only(self):
        e=self.engine
        e.open_paper()
        pos=e.positions()[0]
        with patch('desk.server.get_json',return_value={'market':{'status':'closed','result':''}}):e.settle()
        self.assertEqual(e.positions()[0]['status'],'open')
        with patch('desk.server.get_json',return_value={'market':{'status':'settled','result':'yes'}}):e.settle()
        self.assertEqual(e.positions()[0]['status'],'closed')
        self.assertAlmostEqual(e.cash,10000-pos['cost']+pos['quantity'])
        with patch('desk.server.get_json',return_value={'market':{'status':'settled','result':'yes'}}):e.settle()
        self.assertAlmostEqual(e.cash,10000-pos['cost']+pos['quantity'])

    def test_stale_reads_never_fill_and_spot_proxy_is_explicit(self):
        e=self.engine
        e.auto=True
        e.state();e.state()
        self.assertEqual(e.positions(),[])
        e.reference['time']-=100
        self.assertEqual(e.state()['decision']['action'],'WAIT')
        with self.assertRaises(ValueError):e.open_paper()
        e.reference['time']=time.time()
        self.assertEqual(e.state()['decision']['action'],'BUY YES')
        self.assertEqual(e.state()['reference']['source'],'Coinbase spot proxy')

    def test_manual_bankroll_and_zero_exposure(self):
        e=self.engine
        e.update_config({'bankroll':5000})
        self.assertEqual(e.cash,5000)
        e.update_config({'max_exposure_pct':0,'max_position_pct':0})
        self.assertEqual(e.compute()['action'],'WAIT')

    def test_illiquid_and_closed_exits_rejected(self):
        e=self.engine;e.open_paper();pos=e.positions()[0]
        e.book['yes_bids'][0]['quantity']=.5
        with self.assertRaises(ValueError):e.close_paper(pos['id'])
        e.book['yes_bids'][0]['quantity']=10000
        e.market['close_time']=iso(time.time()-1)
        with self.assertRaises(ValueError):e.close_paper(pos['id'])

    def test_reset_archives_run_and_keeps_audit_and_forecasts(self):
        e=self.engine;e.open_paper();p=e.positions()[0]
        e.auto=True
        e.db.execute("INSERT INTO forecasts VALUES('forecast-test',0,.6,0,'yes',1)");e.db.commit()
        before=e.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]
        cash=e.cash
        e.reset_paper()
        self.assertEqual(e.cash,100)
        self.assertEqual(e.config['bankroll'],100)
        self.assertFalse(e.auto)
        self.assertEqual(e.positions(),[])
        self.assertEqual(e.portfolio()['realized_pnl'],0)
        self.assertEqual(e.portfolio()['equity'],100)
        archive=e.db.execute('SELECT * FROM paper_runs').fetchone()
        self.assertEqual(archive['cash'],cash)
        self.assertEqual(json.loads(archive['positions'])[0]['id'],p['id'])
        self.assertEqual(e.db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0],1)
        self.assertGreater(e.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],before)
        e.open_paper()
        self.assertGreater(e.positions()[0]['id'],p['id'])
        self.assertLessEqual(e.positions()[0]['cost'],2)
        # Reset again; delayed settlement for an old id must never touch the new run.
        e.reset_paper()
        with patch('desk.server.get_json',return_value={'market':{'status':'settled','result':'yes'}}):e.settle()
        self.assertEqual(e.cash,100)

    def test_buy_no_when_no_has_positive_edge_and_reject_expensive_no(self):
        e=self.engine
        e.reference['price']=99.9
        self.assertEqual(e.compute()['action'],'BUY NO')
        yes,no=e.decision['candidates']
        self.assertAlmostEqual(yes['probability']+no['probability'],1)
        e.open_paper()
        self.assertEqual(e.positions()[0]['side'],'no')
        e.close_paper(e.positions()[0]['id'])
        e.reset_paper()
        # Very high P(NO) alone is insufficient when the NO ask is even higher.
        e.reference['price']=99.999
        e.book=normalize_book({'orderbook_fp':{'yes_dollars':[['.01','10000']],'no_dollars':[['.99','10000']]}})
        result=e.compute()
        no=next(c for c in result['candidates'] if c['side']=='no')
        self.assertEqual(no['contracts'],0)
        self.assertNotEqual(result['action'],'BUY NO')

    def test_liquidation_consumes_depth(self):
        self.assertEqual(liquidation([{'price':.6,'quantity':2},{'price':.5,'quantity':3}],4),2.2)
        self.assertIsNone(liquidation([{'price':.6,'quantity':2}],4))


class HttpTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.http=LocalServer(('127.0.0.1',0),Handler)
        self.http.engine=self.engine
        self.port=self.http.server_address[1]
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join()
        super().tearDown()

    def request(self,path,data=None,headers=None):
        return urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,data=json.dumps(data).encode() if data is not None else None,headers=headers or {'Content-Type':'application/json'}),timeout=3)

    def test_reset_api_and_archived_run(self):
        self.engine.open_paper()
        with self.request('/api/paper/reset',{}) as response:self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.engine.cash,100)
        with self.request('/api/paper/run?id=1') as response:
            run=json.load(response)
            self.assertEqual(len(run['positions']),1)
            self.assertEqual(run['config']['bankroll'],10000)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/paper/reset',{}, {'Content-Type':'application/json','Origin':'https://example.com'})
        self.assertEqual(cm.exception.code,403)
        self.assertEqual(self.engine.db.execute('SELECT COUNT(*) FROM paper_runs').fetchone()[0],1)

    def test_api_assets_export_and_cross_origin(self):
        for path in ['/','/app.js','/styles.css','/favicon.svg','/api/health','/api/state']:
            with self.request(path) as response:self.assertEqual(response.status,200)
        with self.request('/api/auto',{'enabled':True}) as response:self.assertTrue(json.load(response)['ok'])
        with self.request('/api/export.csv') as response:self.assertIn('ENGINE',response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/auto',{'enabled':False},{'Content-Type':'application/json','Origin':'https://example.com'})
        self.assertEqual(cm.exception.code,403)
        self.assertTrue(self.engine.auto)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/auto',{'enabled':'true'})
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/config',{'drift':float('nan')})
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/api/decision?id=garbage')
        self.assertEqual(cm.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError) as cm:self.request('/../../server.py')
        self.assertEqual(cm.exception.code,404)

class DataPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=Engine(Path(self.temp.name)/'pipeline.db')

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.engine.close()
        self.temp.cleanup()

    def test_tick_keeps_spot_and_averaged_reference_separate(self):
        e=self.engine;now=time.time()
        market={'ticker':'BTC-NEXT','event_ticker':'BTC-EVENT','status':'active','open_time':iso(now-30),'close_time':iso(now+800),'floor_strike':100.,'strike_type':'greater_or_equal'}
        candles=[{'time':int(now//60)*60-i*60,'close':100+(i%2)*.05} for i in reversed(range(90))]
        spot={'price':100.1,'time':now,'source':'Coinbase spot proxy','points':[],'candles':candles}
        reference={'price':99.,'time':now,'source':'Kalshi reference chart','points':[]}
        def public(url):
            if '/markets?' in url:return {'markets':[market]}
            if '/orderbook?' in url:return {'orderbook_fp':{'yes_dollars':[['.49','10000']],'no_dollars':[['.49','10000']]}}
            if '/events/' in url:return {'event':{'fee_multiplier_override':2,'fee_type_override':None}}
            if '/series/' in url:return {'series':{'fee_multiplier':1,'fee_type':'quadratic'}}
            raise AssertionError('Unexpected URL '+url)
        with patch('desk.server.get_json',side_effect=public),patch.object(e,'fetch_reference',return_value=spot),patch.object(e,'fetch_settlement_reference',return_value=reference):e.tick()
        self.assertEqual(e.market['ticker'],'BTC-NEXT')
        self.assertEqual(e.reference['price'],100.1)
        self.assertEqual(e.settlement_reference['price'],99.)
        self.assertEqual(e.fee_multiplier,2)
        self.assertGreater(e.return_count,30)
        self.assertGreater(e.decision['probability'],.5)
        self.assertEqual(e.positions(),[])
        payload=json.loads(e.db.execute('SELECT payload FROM decisions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(payload['model_version'],'digital-gbm-observed-v2')
        self.assertEqual(payload['source'],'Coinbase spot proxy')

    def test_manual_volatility_does_not_depend_on_candle_service(self):
        e=self.engine;e.config['volatility_mode']='manual'
        now=time.time()
        def public(url):
            if url.endswith('/ticker'):return {'price':'100.1','time':iso(now)}
            raise OSError('Candle API unavailable')
        with patch('desk.server.get_json',side_effect=public):result=e.fetch_reference({})
        self.assertEqual(result['price'],100.1)
        self.assertEqual(result['candles'],[])
        e.config['volatility_mode']='realized'
        with patch('desk.server.get_json',side_effect=public),self.assertRaises(OSError):e.fetch_reference({})

    def test_observed_buffer_requires_history_and_resets_on_rollover(self):
        e=self.engine;now=time.time()
        for i in range(11):
            book=normalize_book({'orderbook_fp':{'yes_dollars':[['.4','10000']],'no_dollars':[[str(.5-i*.001),'10000']]}})
            e.observe_book('BTC-A',book,now-30+i*3)
        self.assertEqual(e.quote_buffer('yes')['samples'],10)
        self.assertAlmostEqual(e.quote_buffer('yes')['value'],.001)
        self.assertEqual(e.quote_buffer('no')['value'],0)
        e.observe_book('BTC-B',book,now+3)
        self.assertIsNone(e.quote_buffer('yes')['value'])

    def test_forecast_settlement_records_official_outcome_and_brier(self):
        e=self.engine
        e.db.execute("INSERT INTO forecasts VALUES('BTC-OLD',0,.8,0,NULL,NULL)");e.db.commit()
        with patch('desk.server.get_json',return_value={'market':{'status':'closed','result':''}}):e.settle_forecasts()
        self.assertEqual(e.input_status()['calibration']['resolved_markets'],0)
        with patch('desk.server.get_json',return_value={'market':{'status':'settled','result':'yes'}}):e.settle_forecasts()
        calibration=e.input_status()['calibration']
        self.assertEqual(calibration['resolved_markets'],1)
        self.assertAlmostEqual(calibration['brier_score'],.04)
        self.assertTrue(calibration['status'].startswith('Unvalidated'))

    def test_restart_preserves_capital_and_positions_but_pauses_bot(self):
        e=self.engine
        e.update_config({'bankroll':12345,'kelly_fraction':.5})
        e.auto=True
        fresh=Engine(Path(self.temp.name)/'pipeline.db')
        try:
            self.assertEqual(fresh.cash,12345)
            self.assertEqual(fresh.config['kelly_fraction'],.5)
            self.assertFalse(fresh.auto)
        finally:
            fresh.pool.shutdown(wait=True);fresh.close()


if __name__=='__main__':unittest.main()
