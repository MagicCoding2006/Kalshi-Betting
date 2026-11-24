"""Settlement rules, causal flow, grouped outcome validation and replay accounting."""
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from decimal import Decimal

from BTC.system.settlement import averaging_probability,reconstruct,verify,YEAR
from BTC.system.feeds import Venue,IndexFeed,crc
from BTC.system.store import Store
from BTC.system.features import CandleFeatures,build
from BTC.system.evaluation import split_contracts,quality,tree_value,predict
from BTC.system.execution import replay


class Journal:
    def __init__(self):self.gaps=[];self.ticks=[]
    def gap(self,*args):self.gaps.append(args)
    def tick(self,*args):self.ticks.append(args)
    def event(self,*args):pass


def market():
    return {'ticker':'KXBTC15M-TEST','strike_type':'greater_or_equal','rules_primary':'The BRTI average is at least the opening average',
            'open_ts':1000,'close_ts':1900,'floor_strike':100.,'result':'yes'}


class SettlementTests(unittest.TestCase):
    def test_average_probability_uses_final_minute_and_no_partial_proxy(self):
        self.assertIsNone(averaging_probability(100,100,59,.6))
        p=averaging_probability(100,100,900,.6)
        times=[(900-60+i+1)/YEAR for i in range(60)]
        variance=math.log1p(sum(math.expm1(.6*.6*min(a,b)) for a in times for b in times)/3600)
        expected=.5*(1+math.erf(-math.sqrt(variance)/2/math.sqrt(2)))
        self.assertAlmostEqual(p,expected,places=12)
        self.assertGreater(averaging_probability(101,100,900,.6),p)

    def test_complete_raw_windows_verify_tie_as_yes_but_gaps_do_not(self):
        a=[{'exchange_ts':t,'price':100.} for t in range(941,1001)]
        b=[{'exchange_ts':t,'price':100.} for t in range(1841,1901)]
        self.assertEqual(reconstruct(a,1000)['coverage'],60)
        self.assertEqual(verify(market(),a,b)['status'],'match')
        self.assertTrue(verify(market(),a,b[:-1])['status'].startswith('unverified'))
        bad={**market(),'result':'no'}
        self.assertEqual(verify(bad,a,b)['status'],'mismatch')

    def test_half_cent_rounding_is_not_claimed_as_verified(self):
        ticks=[{'exchange_ts':t,'price':100.005} for t in range(941,1001)]
        result=reconstruct(ticks,1000)
        self.assertEqual(result['coverage'],60);self.assertIsNone(result['value'])
        self.assertIn('rounding',result['reason'])


class FlowTests(unittest.TestCase):
    def setUp(self):self.j=Journal()
    def test_coinbase_maker_side_is_inverted_for_aggressor_flow(self):
        v=Venue('coinbase',self.j);v.since=0
        v.consume({'type':'snapshot','bids':[['100','5']],'asks':[['100.01','1']]},100)
        v.since=0
        v.consume({'type':'match','price':'100.01','size':'2','side':'sell','time':100,'trade_id':1},100)
        health,f=v.features(101)
        self.assertTrue(health['ready']);self.assertEqual(f['tfi_5'],1.)
        self.assertGreater(f['depth_5'],0);self.assertGreater(f['microprice_gap_bps'],0)

    def test_l2_requires_snapshot_and_cancellation_changes_ofi(self):
        v=Venue('coinbase',self.j)
        with self.assertRaises(ValueError):v.consume({'type':'l2update','changes':[['buy','100','1']]},100)
        v.consume({'type':'snapshot','bids':[['100','5']],'asks':[['101','2']]},100)
        v.consume({'type':'l2update','changes':[['buy','100','1']]},101)
        self.assertLess(v.flows[-1][1],0)
        with self.assertRaises(ValueError):v.consume({'type':'l2update','changes':[['sell','100','1']]},102)
    
    def test_kraken_checksum_failure_invalidates_book_immediately(self):
        v=Venue('kraken',self.j)
        v.update([('100.00','5.000')],[('101.00','2.000')],100,True)
        expected=crc({'bids':v.bids,'asks':v.asks})
        v.update([],[],101,checksum=expected)
        with self.assertRaises(ValueError):v.update([],[],102,checksum=expected+1)
        self.assertFalse(v.ready)
        self.assertEqual(v.features(102)[1],{})

    def test_trade_gap_resets_flow_instead_of_fabricating_volume(self):
        v=Venue('coinbase',self.j);v.since=0
        v.trade(100,1,'buy',100,100,1);v.trade(101,2,'sell',101,101,3)
        self.assertEqual(len(v.trades),1);self.assertEqual(v.since,101);self.assertTrue(self.j.gaps)

    def test_cf_feed_preserves_source_and_receipt_and_rejects_sequence_gap(self):
        index=IndexFeed(self.j)
        index.consume({'type':'cfbenchmarks_value','sid':1,'seq':1,'msg':{'index_id':'BRTI','data':json.dumps({'time':100,'value':'100.25'})}},101)
        self.assertEqual(index.latest['price'],100.25);self.assertEqual(self.j.ticks[0],(100.,101,100.25))
        with self.assertRaises(ValueError):index.consume({'type':'heartbeat','sid':1,'seq':3},102)

    def test_fresh_streamed_kalshi_books_are_scoped_and_apply_deltas(self):
        index=IndexFeed(self.j)
        index.consume({'type':'orderbook_snapshot','msg':{'market_ticker':'T','yes_dollars_fp':[['.40','10']], 'no_dollars_fp':[['.50','20']]}},100)
        self.assertEqual(index.snapshot('T',101)[0]['yes_asks'][0]['quantity'],20)
        index.consume({'type':'orderbook_delta','msg':{'market_ticker':'T','side':'no','price_dollars':'.50','delta_fp':'-5'}},101)
        self.assertEqual(index.snapshot('T',102)[0]['yes_asks'][0]['quantity'],15)
        self.assertIsNone(index.snapshot('OTHER',102));self.assertIsNone(index.snapshot('T',104))
        with self.assertRaises(ValueError):index.consume({'type':'orderbook_delta','msg':{'market_ticker':'OTHER','side':'yes','price_dollars':'.40','delta_fp':'1'}},102)

    def test_trade_heartbeats_cannot_keep_a_stale_book_eligible(self):
        v=Venue('coinbase',self.j)
        v.consume({'type':'snapshot','bids':[['100','5']],'asks':[['101','2']]},100)
        v.consume({'type':'heartbeat'},108)
        self.assertFalse(v.features(108)[0]['ready']);self.assertEqual(v.features(108)[1],{})


class DatasetTests(unittest.TestCase):
    def test_candle_volatility_cannot_see_future_closes(self):
        candles=[{'ts':i*60,'close':100+math.sin(i)} for i in range(40)]
        before=CandleFeatures(candles).at(2000)
        candles[-1]['close']=1e9
        self.assertEqual(before,CandleFeatures(candles).at(2000))

    def test_whole_contract_split_purges_unavailable_labels(self):
        rows=[]
        for i in range(80):
            for j in range(4):rows.append({'ticker':str(i),'ts':i*900+j*5,'available':(i+1)*900+300})
        split=split_contracts(rows)
        self.assertFalse(set(split['train']) & set(split['calibration']))
        self.assertFalse(set(split['test']) & set(split['calibration']))
        first_cal=min(r['ts'] for r in rows if r['ticker'] in split['calibration'])
        self.assertTrue(all(r['available']<first_cal for r in rows if r['ticker'] in split['train']))

    def test_contract_equal_weight_does_not_reward_duplicate_snapshots(self):
        a={'ticker':'a','y':1,'x':{'seconds_left':300}};b={'ticker':'b','y':0,'x':{'seconds_left':300}}
        one=quality([a,b],[.8,.8]);many=quality([a]*100+[b],[.8]*101)
        self.assertAlmostEqual(one['brier'],many['brier']);self.assertEqual(many['windows'],2)

    def test_tree_json_uses_named_features_and_missing_defaults(self):
        tree={'split_feature_name':'a','threshold':1,'default_left':True,'left_child':{'leaf_value':2},'right_child':{'leaf_value':-2}}
        self.assertEqual(tree_value(tree,{'a':0}),2);self.assertEqual(tree_value(tree,{'a':2}),-2);self.assertEqual(tree_value(tree,{}),2)

    def test_no_exchange_flow_is_invented_for_quote_only_records(self):
        x=build(100,100,500,.5,.5,{'yes_bids':[{'price':.4,'quantity':10}],'yes_asks':[{'price':.5,'quantity':10}]})
        self.assertIsNone(x['cb_tfi_60']);self.assertFalse(x['flow_ready']);self.assertEqual(x['reference_source'],'proxy')


class ExecutionAndStoreTests(unittest.TestCase):
    def test_live_and_imported_features_share_an_immutable_five_second_bucket(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d)
            try:
                self.assertTrue(store.observation('T',101,{'baseline':.5},'live_join'))
                self.assertFalse(store.observation('T',102,{'baseline':.9},'recorded_quote_import'))
                self.assertTrue(store.observation('T',105,{'baseline':.6},'live_join'))
                self.assertEqual(store.stats()['observations'],2)
                self.assertEqual(json.loads(store.db.execute('SELECT features FROM observations WHERE ts=101').fetchone()[0])['baseline'],.5)
            finally:store.close()

    def test_depth_partial_fill_latency_and_single_entry_per_contract(self):
        x={'baseline':.9,'yes_ask':.5,'no_ask':.5}
        row={'ticker':'T','ts':10.,'available':100.,'y':1,'x':x,'market':{'close_ts':90.}}
        artifact={'kind':'baseline','calibration':[1,0]}
        calls=[]
        def quote(t,ts):
            calls.append(ts)
            return {'ts':ts,'fee':1,'book':{'yes_asks':[{'price':.5,'quantity':1}]}}
        result=replay([row,{**row,'ts':15}],artifact,quote)
        self.assertEqual(result['trades'],1);self.assertEqual(result['partial_fills'],1)
        self.assertAlmostEqual(result['ending_cash'],100.48);self.assertEqual(calls,[10.25])
        self.assertEqual(result['maximum_realized_drawdown'],0)

    def test_missing_latency_quote_does_not_fill_at_the_decision_quote(self):
        row={'ticker':'T','ts':10.,'available':100.,'y':1,'x':{'baseline':.9,'yes_ask':.5,'no_ask':.5},'market':{'close_ts':90.}}
        result=replay([row],{'kind':'baseline','calibration':[1,0]},lambda *_:None)
        self.assertEqual(result['ending_cash'],100);self.assertEqual(result['trades'],0)

    def test_export_report_keeps_study_and_tracks_repeated_holdout_inspection(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d)
            try:
                report={'holdout_id':'one','experiments':[]};store.report(report);store.report({'holdout_id':'one','experiments':[]})
                store.report({'exported_rows':10})
                self.assertEqual(store.latest()['holdout_exposures'],2)
                self.assertEqual(store.stats()['observations'],0)
            finally:store.close()

if __name__=='__main__':unittest.main()
