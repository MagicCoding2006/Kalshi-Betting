"""Causality, contract boundaries, replay costs and optional LSTM temporal splits."""
import importlib.util
import math
import tempfile
import time
import unittest
import zlib
import json
from pathlib import Path

from desk.research.markov import Markov,pairs,backtest,execution_trade,state_key
from desk.research.history import RangeContext
from desk.research.store import Store
from desk.research.lab import Lab
from desk.research import lstm


def rows(n=500,start=1700000000):
    result=[]
    for i in range(n):
        mid=.5+.025*math.sin(i/15)
        previous=.5+.025*math.sin((i-1)/15) if i else mid
        result.append({'id':i+1,'ts':start+i*.5,'ticker':'CONTRACT-A','mid':mid,'bid':mid-.005,'ask':mid+.005,
                       'spread':.01,'bid_qty':100.,'ask_qty':100.,'imbalance':math.sin(i/15),
                       'microprice':mid+.002*math.sin(i/15),'momentum':mid-previous,
                       'seconds_left':850-i*.5,'spot':100.,'spot_time':start+i*.5-1,'strike':100.,'fee_multiplier':1.})
    return result


def candles(start,end):
    return [{'ts':t,'close':100.,'low':90.,'high':110.} for t in range(start-3*86400,end+60,60)]


class CausalMarkovTests(unittest.TestCase):
    def test_pairs_use_observed_horizon_without_crossing_windows_or_outages(self):
        data=rows(40)
        for row in data[20:]:row['ticker']='CONTRACT-B'
        samples=pairs(data,5)
        self.assertTrue(samples)
        self.assertTrue(all(s['row']['ticker']==s['future']['ticker'] for s in samples))
        self.assertTrue(all(5<=s['future']['ts']-s['row']['ts']<=6 for s in samples))
        gap=rows(30)
        for row in gap[10:]:row['ts']+=3
        self.assertFalse(any(s['row']['id']<=10<s['future']['id'] for s in pairs(gap,5)))

    def test_future_extreme_is_not_removed_to_improve_results(self):
        data=rows(20)
        data[0]['mid']=.9
        data[10]['mid']=.999
        first=next(s for s in pairs(data,5,True) if s['row']['id']==1)
        self.assertEqual(first['future']['mid'],.999)
        self.assertFalse(any(s['row']['id']==11 for s in pairs(data,2,True)))

    def test_markov_support_and_explicit_sparse_state_fallback(self):
        model=Markov();row=rows(1)[0]
        self.assertIsNone(model.predict(row))
        for _ in range(100):model.add({'key':state_key(row),'label':2,'delta':.01})
        p=model.predict(row)
        self.assertAlmostEqual(p['price'],.51)
        self.assertGreater(p['probabilities'][2],.95)
        other={**row,'imbalance':-.9}
        self.assertIn('fallback',model.predict(other)['basis'])

    def test_first_holdout_prediction_can_only_use_already_matured_labels(self):
        data=rows(500)
        report=backtest(data,5)
        self.assertEqual(report['status'],'complete')
        first=report['replay'][0]
        model=Markov()
        for sample in pairs(data,5):
            if sample['future']['ts']<=first['time']:model.add(sample)
        origin=next(r for r in data if r['ts']==first['time'])
        self.assertAlmostEqual(first['predicted'],model.predict(origin)['price'])
        self.assertGreater(first['target_time'],first['time'])

    def test_execution_uses_real_bid_ask_fees_and_nonoverlap(self):
        origin=rows(1)[0];origin.update(mid=.5,bid=.49,ask=.51,spread=.02)
        future={**origin,'ts':origin['ts']+5,'bid':.58,'ask':.60}
        trade=execution_trade(origin,future,.60,0)
        self.assertEqual(trade['side'],'yes')
        self.assertAlmostEqual(trade['pnl'],.03)
        self.assertIsNone(execution_trade(origin,future,.60,origin['ts']+1))
        self.assertIsNone(execution_trade({**origin,'fee_multiplier':None},future,.60,0))


class RangeAndSequenceTests(unittest.TestCase):
    def test_future_highs_cannot_change_past_range_and_breakouts_are_allowed(self):
        t=1700000000
        history=[{'ts':t-72*3600,'close':100,'low':90,'high':110},
                 {'ts':t,'close':100,'low':95,'high':105},
                 {'ts':t+60,'close':1000,'low':1,'high':2000}]
        past=RangeContext(history).at(t,120)
        self.assertEqual(past['low'],90)
        self.assertEqual(past['high'],110)
        self.assertGreater(past['position'],1)
        self.assertEqual(past,RangeContext(history[:-1]).at(t,120))

    def test_old_extremes_expire_and_stale_history_is_not_used(self):
        t=1700000000
        context=RangeContext([{'ts':t-73*3600,'close':100,'low':1,'high':1000},
                              {'ts':t-60,'close':100,'low':90,'high':110},
                              {'ts':t,'close':100,'low':95,'high':105}])
        self.assertEqual(context.at(t,100)['high'],110)
        self.assertIsNone(context.at(t+181,100))

    def test_lstm_features_are_past_only_and_sequences_do_not_cross_contracts(self):
        data=rows(100)
        history=candles(int(data[0]['ts']),int(data[-1]['ts'])+60)
        poison=history+[{'ts':int(data[-1]['ts'])+100,'close':1,'low':.1,'high':1e6}]
        self.assertEqual(lstm.features(data,history),lstm.features(data,poison))
        for row in data[50:]:row['ticker']='CONTRACT-B'
        sequences=lstm.examples(data,history,2,True,True)
        self.assertTrue(sequences)
        self.assertFalse(any(50<=e['sample']['row']['id']<70 for e in sequences))

    def test_lstm_reports_pending_instead_of_fabricating_small_sample_results(self):
        data=rows(100)
        report,bundle=lstm.train(data,candles(int(data[0]['ts']),int(data[-1]['ts'])+60),Path('/unused'),5,True,True,lambda *_:None,lambda:False)
        self.assertEqual(report['status'],'collecting');self.assertIsNone(bundle)

    @unittest.skipUnless(importlib.util.find_spec('torch') and importlib.util.find_spec('numpy'),'Optional PyTorch runtime not installed')
    def test_actual_lstm_training_has_purged_splits_and_reloadable_weights(self):
        data=rows(900)
        history=candles(int(data[0]['ts']),int(data[-1]['ts'])+60)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.pt'
            report,bundle=lstm.train(data,history,path,2,True,True,lambda *_:None,lambda:False)
            self.assertEqual(report['status'],'complete')
            self.assertLess(report['train_target_end'],report['validation_start'])
            self.assertLess(report['validation_target_end'],report['test_start'])
            self.assertTrue(math.isfinite(report['metrics']['mae_cents']))
            loaded=lstm.load(path)
            a=lstm.predict(bundle,data,history);b=lstm.predict(loaded,data,history)
            self.assertAlmostEqual(a['price'],b['price'],places=7)


class StoreAndLabTests(unittest.TestCase):
    def test_research_storage_preserves_raw_books_and_is_separate_from_cash(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'research.sqlite3')
            try:
                book={'yes_bids':[{'price':.4,'quantity':10}], 'yes_asks':[{'price':.6,'quantity':5}]}
                store.record(book,'ONE',time.time(),850,{'price':100,'time':time.time()-1},100,1)
                row=store.rows()[0]
                self.assertAlmostEqual(row['mid'],.5)
                self.assertGreater(row['imbalance'],0)
                raw=store.db.execute('SELECT raw FROM snapshots').fetchone()[0]
                self.assertEqual(json.loads(zlib.decompress(raw)),book)
                self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='positions'").fetchone())
            finally:store.close()

    def test_research_settings_validate_and_persist_without_trading_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'research.sqlite3';lab=Lab(path,'btc')
            try:
                for config in ({'horizon':3},{'horizon':True},{'interior':'false'},{'bankroll':500}):
                    with self.assertRaises(ValueError):lab.configure(config)
                lab.configure({'horizon':10,'range_filter':False})
            finally:lab.close()
            fresh=Lab(path,'btc')
            try:self.assertEqual(fresh.settings['horizon'],10);self.assertFalse(fresh.settings['range_filter'])
            finally:fresh.close()
