"""Protect market symmetry, capital rounding and the prospective research lockbox."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from BTC.system.evaluation import predict
from BTC.system.execution import replay,cash_cost
from BTC.system.refinement import forecast,folds,freeze_protocol,prospective,VERSION,CANDIDATES
from BTC.system.store import Store


def iso(ts):return datetime.fromtimestamp(ts,timezone.utc).isoformat()


def features():
    return {'baseline':.7,'kalshi_mid':.5,'kalshi_spread':.02,'kalshi_imbalance':.2,'kalshi_micro_gap':.002,
            'seconds_left':600,'vol_5m':.6,'sigma_30m':.5,'yes_ask':.51,'no_ask':.51,'reference_source':'proxy'}


def contract(store,ticker,opened,origin='live_join'):
    close=opened+900;ts=opened+100;available=close+2
    m=store.contract({'ticker':ticker,'open_time':iso(opened),'close_time':iso(close),'settlement_ts':available,'result':'yes'})
    x=features();x['spot_received']=ts
    store.observation(ticker,ts,x,origin)
    return {'ticker':ticker,'ts':ts,'available':available,'market':m,'x':x,'y':1}


class SymmetryAndReplayTests(unittest.TestCase):
    def test_market_anchor_has_no_hidden_yes_prior_and_is_side_symmetric(self):
        x=features();opposite={**x,'baseline':1-x['baseline'],'kalshi_mid':1-x['kalshi_mid'],
                              'kalshi_imbalance':-x['kalshi_imbalance'],'kalshi_micro_gap':-x['kalshi_micro_gap']}
        for model in [{'kind':'market'},{'kind':'market_blend','alpha':.25},
                      {'kind':'market_residual','coefficients':[.1,.2,.1,-.1,.2]}]:
            self.assertAlmostEqual(forecast(model,x)+forecast(model,opposite),1.,places=12)
            self.assertEqual(predict(model,x),forecast(model,x))
        neutral={**x,'baseline':.5,'kalshi_imbalance':0,'kalshi_micro_gap':0}
        self.assertEqual(forecast({'kind':'market_residual','coefficients':[.5]*5},neutral),.5)

    def test_corrections_are_bounded_even_for_extreme_baseline(self):
        x={**features(),'baseline':.999999,'kalshi_mid':.1}
        p=forecast({'kind':'market_blend','alpha':.25},x)
        self.assertLess(p,.16);self.assertGreater(p,.1)

    def test_subcent_principal_is_included_in_account_rounding(self):
        self.assertEqual(cash_cost(.505,.017499),.53)
        row={'ticker':'T','ts':10,'available':100,'y':1,'x':{**features(),'baseline':.9},'market':{'close_ts':90}}
        def quote(*args):return {'ts':10.25,'fee':1,'book':{'yes_asks':[{'price':.505,'quantity':1}]}}
        a={'kind':'baseline','calibration':[1,0]}
        result=replay([row],a,quote)
        self.assertAlmostEqual(result['ending_cash'],100.47)
        self.assertAlmostEqual(result['fees_and_rounding'],.025)
        # Approximate per-unit sizing must not allow the rounded cost to exceed a cap.
        blocked=replay([row],a,quote,bankroll=26.49)
        self.assertEqual(blocked['trades'],0)

    def test_walkforward_replay_uses_received_predictions_not_later_refit(self):
        row={'ticker':'T','ts':10,'available':100,'y':1,'x':features(),'market':{'close_ts':90}}
        a={'kind':'received_predictions','predictions':{('T',10):.9}}
        def quote(*args):return {'ts':10.25,'fee':1,'book':{'yes_asks':[{'price':.51,'quantity':1}]}}
        self.assertEqual(replay([row],a,quote)['trades'],1)
        self.assertEqual(replay([row],a,quote,policy={'enabled':False})['trades'],0)

    def test_each_walkforward_fit_has_only_already_available_labels(self):
        rows=[{'ticker':str(i),'ts':i*900+10,'available':(i+1)*900+40,'y':i%2} for i in range(120)]
        blocks=folds(rows)
        self.assertEqual(len(blocks),4)
        for train,validation in blocks:
            self.assertTrue(all(r['available']<validation[0]['ts'] for r in train))
            self.assertFalse({r['ticker'] for r in train}&{r['ticker'] for r in validation})


class LockboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
    def tearDown(self):self.store.close();self.temp.cleanup()

    def lockbox(self,needed=2):
        p={'id':'study','frozen_at':900,'prospective_contracts_required':needed}
        f={'protocol_id':'study','published_at':1000,'frozen_at':900,'candidate':{'kind':'market','reference_mode':['proxy']},
           'champion':{'kind':'market','reference_mode':['proxy']},'development_gate':False}
        f['artifact_hash']=hashlib.sha256(json.dumps(f,sort_keys=True).encode()).hexdigest()
        (self.store.directory/'refinement-protocol.json').write_text(json.dumps(p))
        (self.store.directory/'refined-model.json').write_text(json.dumps(f))
        return f

    def test_published_tests_are_quarantined_and_refit_cannot_move_boundary(self):
        rows=[contract(self.store,str(i),i*900,origin='recorded_quote_import') for i in range(110)]
        self.store.report({'contract_membership':{'test':[str(i) for i in range(100,110)]}})
        with patch('BTC.system.refinement.time.time',return_value=200000):p=freeze_protocol(self.store,rows)
        self.assertEqual(len(p['development_tickers']),100)
        self.assertFalse(set(p['development_tickers'])&set(p['quarantined_tests']))
        rows.append(contract(self.store,'new',200000))
        with patch('BTC.system.refinement.time.time',return_value=300000):again=freeze_protocol(self.store,rows)
        self.assertEqual(p,again)

    def test_historical_imports_cannot_populate_a_prospective_test(self):
        self.lockbox();contract(self.store,'imported',1100,origin='recorded_quote_import')
        result=prospective(self.store,now=10000)
        self.assertEqual(result['reserved_contracts'],0);self.assertNotIn('quality',result)

    def test_scores_hidden_until_all_reserved_labels_available_then_persist_once(self):
        f=self.lockbox();a=contract(self.store,'a',1100);b=contract(self.store,'b',2100)
        for r in [a,b]:self.store.shadow('study',f['artifact_hash'],r['ticker'],r['ts'],r['ts']+.01,.5)
        early=prospective(self.store,now=b['available']-1)
        self.assertEqual(early['completed_contracts'],1);self.assertNotIn('execution',early);self.assertNotIn('quality',early)
        final=prospective(self.store,now=b['available']+1)
        self.assertEqual(final['status'],'complete');self.assertEqual(final['tickers'],['a','b']);self.assertFalse(final['passed'])
        c=contract(self.store,'c',3100);self.store.shadow('study',f['artifact_hash'],'c',c['ts'],c['ts']+.01,.9)
        self.assertEqual(prospective(self.store,now=10000),final)

    def test_model_tampering_invalidates_prospective_scores(self):
        f=self.lockbox();f['candidate']['kind']='market_blend';f['candidate']['alpha']=.25
        (self.store.directory/'refined-model.json').write_text(json.dumps(f))
        self.assertEqual(prospective(self.store,now=10000)['status'],'error')


if __name__=='__main__':unittest.main()
