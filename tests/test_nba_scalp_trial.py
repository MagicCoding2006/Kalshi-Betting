import copy
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from NBA.scalp_trial import replay,evaluate,trial_status
from NBA.dashboard_risk import dashboard_assessments
from NBA.risk import settings


def point(t,bid=.49,ask=.5):
    def fill(p):return {'vwap':p,'price_cost':p*10,'fee_upper_estimate':.18}
    return {'time':t,'at':str(t),'state':'in','period':2,'clock':500,'home_score':40,'away_score':38,'score_age':2,
            'fills':{side:{'buy':fill(ask),'sell':fill(bid)} for side in ('yes','no')}}


class TrialTests(unittest.TestCase):
    def test_trigger_uses_later_executable_quote_and_actual_gap_loss(self):
        rows,missing=replay([point(0),point(10,.44),point(20,.30)],'yes','BOS','g','2026-11-22')
        self.assertEqual(len(rows),1);r=rows[0]
        self.assertEqual(r['outcome'],'stop');self.assertEqual(r['exited_at'],'20')
        self.assertAlmostEqual(r['net_pnl'],-2.46);self.assertAlmostEqual(r['adverse_bid_move'],.2)
        self.assertEqual(r['features'][2],2)

    def test_missing_future_or_gap_is_censored_not_a_loss_or_win(self):
        for points in ([point(0)],[point(0),point(40,.7)],[point(0),point(10,.65),point(50,.65)]):
            rows,missing=replay(points,'yes','BOS','g','d');self.assertEqual(rows,[]);self.assertTrue(missing)

    def test_timeouts_count_as_non_target_and_use_later_quote(self):
        points=[point(t) for t in range(0,1230,10)]
        rows,missing=replay(points,'no','NYK','g','d')
        self.assertEqual(rows[0]['outcome'],'time');self.assertEqual(rows[0]['exited_at'],'1210');self.assertEqual(rows[0]['target_hit'],0)
        self.assertEqual(rows[0]['features'][2],-2)

    def test_insufficient_depth_censors_window(self):
        points=[point(0),point(10)];points[1]['fills']['yes']['sell']=None
        rows,missing=replay(points,'yes','BOS','g','d');self.assertEqual(rows,[]);self.assertEqual(missing['insufficient_exit_depth'],1)

    def test_no_report_does_not_train_or_guess(self):
        with tempfile.TemporaryDirectory() as d:self.assertEqual(trial_status(d)['status'],'not_run')
        self.assertEqual(evaluate([])['status'],'insufficient_dates')

    def test_chronological_split_keeps_games_together_before_fit(self):
        rows=[{'day':f'2026-11-{day}','game_id':str(day),'target_hit':day%2,'outcome':'stop','net_pnl':0,'adverse_bid_move':0} for day in (21,22,23,24)]
        report=evaluate(rows)
        self.assertEqual(report['status'],'insufficient_games')
        self.assertFalse(set(report['train_game_ids'])&set(report['test_game_ids']))


class DashboardRiskTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,25,18,tzinfo=timezone.utc)
        self.game={'game_id':'g','date':'2026-11-25','home':'BOS','away':'NYK','tipoff':(self.now+timedelta(hours=2)).isoformat(),
                   'status':'research_estimate','blockers':[],'entry_safety':{'blocked':False},'prediction':{'home_win_probability':.65}}
        ticker='KXNBAGAME-26NOV25NYKBOS-BOS'
        self.quote={'ticker':ticker,'game_id':'g','yes_team':'BOS','status':'active','score_state':'pre','observed_at':self.now.isoformat(),'multiplier':1,
            'book':{side+'_'+kind:[{'price':price,'quantity':100}] for side,kind,price in [('yes','asks',.55),('yes','bids',.52),('no','asks',.48),('no','bids',.45)]}}
        self.quotes={ticker:self.quote};self.cfg=settings({'max_loss_fraction':.01})

    def assess(self,ledger=None):
        with tempfile.TemporaryDirectory() as directory:
            return dashboard_assessments({'games':[self.game]},self.quotes,self.cfg,ledger or [],Path(directory),self.now)['g']

    def test_new_paper_filters_apply_full_loss_cap_not_stop_assumption(self):
        report=self.assess();route=report['routes'][0]
        self.assertEqual(route['status'],'paper_buy');self.assertLessEqual(route['contracts']*(.55+.02+.005),10)
        self.assertFalse(report['trial']['promoted'])

    def test_missing_budget_injuries_and_live_game_cannot_pass(self):
        self.cfg['max_loss_fraction']=None;self.assertEqual(self.assess()['status'],'blocked')
        self.cfg['max_loss_fraction']=.01;self.game['entry_safety']['blocked']=True;self.assertEqual(self.assess()['status'],'blocked')
        self.game['entry_safety']['blocked']=False;self.quote['score_state']='in';self.assertEqual(self.assess()['status'],'blocked')

    def test_stale_prices_and_large_disagreement_block_paper_entries(self):
        self.quote['observed_at']=(self.now-timedelta(seconds=31)).isoformat();self.assertEqual(self.assess()['status'],'blocked')
        self.quote['observed_at']=self.now.isoformat();self.game['prediction']['home_win_probability']=.99
        report=self.assess();self.assertEqual(report['status'],'blocked');self.assertIn('disagreement',report['routes'][0]['reason'])

    def test_existing_exposure_consumes_portfolio_cap(self):
        self.cfg['max_total_fraction']=.01
        ledger=[{'game_id':'other','entry_cost':10}]
        self.assertEqual(self.assess(ledger)['loss_cap'],0);self.assertEqual(self.assess(ledger)['status'],'blocked')
