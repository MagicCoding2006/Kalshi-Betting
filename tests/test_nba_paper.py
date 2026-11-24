"""Paper exits use future observations causally and keep unfinished cohorts visible."""
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from NBA.data import atomic_json
from NBA.paper import PaperStudy,checkpoint_probability,partial_sweep
from NBA.recording import Recording

NOW=datetime(2026,11,22,2,tzinfo=timezone.utc)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name);self.store=Recording(self.directory)
        self.reg={'study_id':'test','registered_at':(NOW-timedelta(minutes=1)).isoformat(),'model_id':'test','quantity':10,
            'minimum_net_edge':.03,'slippage_per_contract':.005,'max_book_score_age_seconds':30,'max_forecast_age_seconds':900,
            'minimum_entry_lead_seconds':120,'maximum_entry_lead_seconds':3600,'execution_latency_seconds':1,
            'max_execution_gap_seconds':30,'maximum_policy_hold_seconds':5400,'policies':['settlement_hold','target10_stop5']}
        atomic_json(self.directory/'paper'/'registration.json',self.reg)
        self.game={'game_id':'1','date':'2026-11-21','tipoff':(NOW+timedelta(minutes=10)).isoformat(),'season':2027,
            'home':'BOS','away':'NYK','state':'pre','period':0,'clock_seconds':0,'home_score':0,'away_score':0}
        req=self.request({'events':[]},NOW-timedelta(seconds=10),'scoreboard');self.store.score(self.game,req,(NOW-timedelta(seconds=10)).isoformat())
        self.market={'ticker':'KXNBAGAME-26NOV21NYBOS-BOS','event_ticker':'KXNBAGAME-26NOV21NYBOS','status':'active','market_type':'binary'}
        req=self.request({'markets':[self.market]},NOW-timedelta(seconds=10),'market_discovery')
        self.store.market(self.market,req,(NOW-timedelta(seconds=10)).isoformat(),self.store.fixtures())
        self.series=self.request({'series':{'fee_type':'quadratic','fee_multiplier':1}},NOW-timedelta(seconds=10),'series')
        payload={'prediction':{'home_win_probability':.8,'away_win_probability':.2},'features':{'elo_diff':0,'home_court':1},'blockers':[]}
        digest=self.store.payload(payload)
        self.store.db.execute('INSERT INTO forecasts(game_id,model_id,as_of,recorded_at,kind,payload_hash) VALUES(?,?,?,?,?,?)',
            ('1','test',(NOW-timedelta(seconds=10)).isoformat(),(NOW-timedelta(seconds=5)).isoformat(),'issued_pregame',digest))
        self.store.db.commit();self.study=None

    def tearDown(self):
        if self.study:self.study.close()
        self.store.close();self.temp.cleanup()

    def request(self,payload,at,kind):
        return self.store.request(None,kind,{'payload':payload,'url':'https://example.test','requested_at':at.isoformat(),
            'received_at':at.isoformat(),'error':None,'http_status':200})

    def book(self,seconds,bid=.59,ask=.60):
        at=NOW+timedelta(seconds=seconds);payload={'orderbook_fp':{'yes_dollars':[[str(bid),'20']],'no_dollars':[[str(1-ask),'20']]}}
        req=self.request(payload,at,'orderbook')
        self.store.book(self.market['ticker'],{'payload':payload,'received_at':at.isoformat(),'error':None},req,self.series);self.store.db.commit()

    def start(self):self.study=PaperStudy(self.directory)

    def test_entry_waits_for_later_quote_and_restart_does_not_duplicate(self):
        self.book(0);self.start();r=self.study.step(NOW)
        self.assertEqual(r['entries'],0);self.assertEqual(r['pending_entries'],1)
        self.book(2);r=self.study.step(NOW+timedelta(seconds=2));self.assertEqual(r['entries'],1)
        self.study.close();self.study=PaperStudy(self.directory)
        self.assertEqual(self.study.step(NOW+timedelta(seconds=3))['entries'],1)

    def test_stop_uses_later_gap_price_and_cohorts_wait_for_settlement(self):
        self.book(0);self.book(2);self.book(4,.54,.55);self.book(6,.40,.41);self.start()
        r=self.study.step(NOW+timedelta(seconds=6))
        stopped=next(p for p in r['policies'] if p['policy']=='target10_stop5')
        self.assertAlmostEqual(stopped['net_pnl'],-2.44)
        self.assertEqual(stopped['closed_trades'],1);self.assertEqual(r['matched_closed_entries'],0)
        self.assertEqual(stopped['matched_net_pnl'],0,'An early exit alone cannot win the policy comparison')
        self.market.update(status='settled',result='no',settlement_value_dollars='0')
        at=NOW+timedelta(seconds=8);req=self.request({'market':self.market},at,'market_status')
        self.store.market(self.market,req,at.isoformat(),self.store.fixtures());self.store.db.commit()
        r=self.study.step(at);self.assertEqual(r['matched_closed_entries'],1)
        self.assertEqual([p['closed_trades'] for p in r['policies']],[1,1])
        self.assertAlmostEqual(r['policies'][0]['net_pnl'],-6.22)

    def test_forecast_first_seen_after_the_quote_cannot_trigger_entry(self):
        self.store.db.execute('UPDATE forecasts SET recorded_at=?',((NOW+timedelta(seconds=10)).isoformat(),));self.store.db.commit()
        self.book(0);self.book(2);self.start();r=self.study.step(NOW+timedelta(seconds=2))
        self.assertEqual(r['entries'],0);self.assertEqual(r['pending_entries'],0)

    def test_changed_registration_cannot_silently_rewrite_the_study(self):
        self.start();self.study.step(NOW);self.study.close();self.study=None
        atomic_json(self.directory/'paper'/'registration.json',{**self.reg,'quantity':20})
        with self.assertRaisesRegex(ValueError,'registration changed'):PaperStudy(self.directory)

    def test_partial_fills_do_not_invent_depth(self):
        fill=partial_sweep([{'price':.6,'quantity':3}],10,1)
        self.assertEqual(fill['quantity'],3)

    def test_quarter_probability_is_unavailable_after_play_resumes(self):
        self.assertIsNone(checkpoint_probability({'models':{}},{'state':'in','period':2,'clock_seconds':719},{}))
        self.assertIsNone(checkpoint_probability({'models':{}},{'state':'in','period':3,'clock_seconds':0},{}))


if __name__=='__main__':unittest.main()
