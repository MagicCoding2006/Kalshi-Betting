"""Distributional Kelly, dynamic barriers, chronology and execution invariants."""
import copy
import unittest

from BTC.system.adaptive_swings import (VERSION,SETTINGS,weighted_kelly,tail_mean,keys,plan,live,
    horizon_choices,purchase,sale,replay_trade,transitions,process_stats,calibrate)


def row(ts=100.,bid=.5,ask=.51):
    return {'ticker':'TEST','ts':ts,'mid':(bid+ask)/2,'bid':bid,'ask':ask,'bid_qty':100,'ask_qty':100,
            'spread':ask-bid,'baseline':.7,'fee_multiplier':1.,'spot_time':ts,'seconds_left':1000-ts,
            'imbalance':.5,'spot':100.,'strike':101.}


def bundle(r):
    p={'contracts':100,'mu_raw':.06,'mu':.06,'mu_standard_error':.001,'sigma':.02,'kappa':.001,
       'flow_decay_seconds':40.,'spread_75':.01,'innovations':[-1.4,-.7,0,.7,1.4]}
    return {'version':VERSION,'process':{'state':{keys(r)[0]:p},'coarse':{}},
            'calibration':{'passed':True,'drift_trust':1.,'variance_scale':1.},
            'gap_hazard':{'upper_rate_per_second':.0001},'growth_gate':{'passed':True}}


class KellyTests(unittest.TestCase):
    def test_classic_binary_result_and_nonbinary_payoffs(self):
        self.assertAlmostEqual(weighted_kelly([1,-1],[.6,.4]),.2,places=9)
        self.assertAlmostEqual(weighted_kelly([2,-1],[.5,.5]),.25,places=9)
        self.assertGreater(weighted_kelly([.5,.2,-.3,-1],[.4,.4,.19,.01]),0)

    def test_negative_edge_and_invalid_distributions(self):
        self.assertEqual(weighted_kelly([1,-1],[.4,.6]),0)
        self.assertEqual(weighted_kelly([1,-1],[.5,.5]),0)
        for r,w in [([],[]),([-1.1,1],[.5,.5]),([1],[0]),([1],[-1]),([1,2],[1])]:
            with self.assertRaises(ValueError):weighted_kelly(r,w)

    def test_tail_risk_preserves_small_full_loss_probability(self):
        self.assertAlmostEqual(tail_mean([-1,0],[.01,.99],.05),-.2)
        self.assertAlmostEqual(tail_mean([-1,-.1,0],[.02,.1,.88],.05),-.46)


class AdaptivePlanTests(unittest.TestCase):
    def test_positive_synthetic_edge_respects_fractional_kelly_and_tail_budget(self):
        r=row();p=plan(bundle(r),r,'yes')
        self.assertGreater(p['quantity'],0)
        self.assertLessEqual(p['capital_fraction'],SETTINGS['capital_cap'])
        self.assertLessEqual(p['capital_fraction'],p['fractional_kelly_fraction']+1e-9)
        self.assertLessEqual(p['portfolio_tail_loss_fraction'],SETTINGS['tail_loss_budget']+1e-9)
        self.assertGreater(p['expected_log_growth'],0);self.assertGreater(p['conservative_mean_return'],0)
        self.assertEqual(p['cost'],purchase(p['quantity'],r['ask']+SETTINGS['entry_slippage_limit'],1))

    def test_barriers_change_with_measured_volatility_instead_of_fixed_roi(self):
        r=row();a=bundle(r);b=copy.deepcopy(a);b['process']['state'][keys(r)[0]]['sigma']=.06
        p=plan(a,r,'yes');q=plan(b,r,'yes')
        self.assertIsNotNone(p.get('target_bid'));self.assertIsNotNone(q.get('target_bid'))
        self.assertNotEqual((p['target_bid'],p['stop_bid']),(q['target_bid'],q['stop_bid']))
        self.assertNotAlmostEqual(p['target_net_return'],.05)
        self.assertNotIn('profit_target',SETTINGS);self.assertNotIn('stop_loss',SETTINGS)

    def test_horizon_depends_on_decay_and_time_to_close(self):
        p={'flow_decay_seconds':40.}
        self.assertNotEqual(horizon_choices(row(),p),horizon_choices(row(),{'flow_decay_seconds':10.}))
        r={**row(),'seconds_left':30}
        self.assertTrue(all(h+SETTINGS['close_buffer_seconds']+2*SETTINGS['latency_seconds']<=30 for h in horizon_choices(r,p)))

    def test_uncertainty_and_gap_loss_can_remove_a_positive_point_estimate(self):
        r=row();b=bundle(r);b['process']['state'][keys(r)[0]]['mu_standard_error']=.15
        self.assertEqual(plan(b,r,'yes')['quantity'],0)
        b=bundle(r);b['gap_hazard']['upper_rate_per_second']=.5
        self.assertEqual(plan(b,r,'yes')['quantity'],0)

    def test_missing_calibration_and_failed_test_do_not_approve_entries(self):
        r=row();b=bundle(r);b['calibration']['passed']=False
        self.assertEqual(plan(b,r,'yes')['quantity'],0)
        b=bundle(r);b['growth_gate']['passed']=False
        s=live(r,.6,b)
        self.assertTrue(all(x['quantity']==0 for x in s['sides']))

    def test_sparse_or_unknown_fee_state_is_not_invented(self):
        r=row();b=bundle(r);b['process']['state'][keys(r)[0]]['contracts']=5
        self.assertEqual(plan(b,r,'yes')['quantity'],0)
        self.assertEqual(plan(bundle(row()),{**r,'fee_multiplier':None},'yes')['quantity'],0)


class ChronologyAndExecutionTests(unittest.TestCase):
    def test_process_fit_counts_contracts_not_duplicate_ticks(self):
        samples=[]
        for i in range(10):
            for j in range(20):samples.append({'row':{**row(),'ticker':str(i)},'delta':.02+(-1)**j*.01,'dt':5.,'flow_next':.4})
        p=process_stats(samples)
        self.assertEqual(p['contracts'],10);self.assertGreater(p['sigma'],0)

    def test_transition_labels_cannot_cross_calibration_cutoff_or_quote_gap(self):
        g=[row(100+i,bid=.5+i*.0001,ask=.51+i*.0001) for i in range(21)]
        s=transitions({'TEST':g},['TEST'],before=108)
        self.assertTrue(s);self.assertTrue(all(x['future_ts']<108 for x in s))
        self.assertEqual(transitions({'TEST':[g[0],g[6]]},['TEST']),[])

    def test_calibration_requires_actual_later_forecast_improvement(self):
        r=row();b=bundle(r);p=b['process']['state'][keys(r)[0]];p['mu']=.02
        samples=[{'row':{**r,'ticker':str(i)},'delta':.1,'dt':5.} for i in range(20)]
        self.assertTrue(calibrate(b['process'],samples)['passed'])
        p['mu']=0.
        self.assertFalse(calibrate(b['process'],samples)['passed'])

    def trade(self,depth=100):
        group=[row(100,.495,.505),row(100.5,.495,.505),row(101,.61,.62),{**row(101.5,.60,.61),'bid_qty':depth}]
        p={'quantity':3,'target_bid':.60,'stop_bid':.45,'max_hold_seconds':5,'expected_net_return':.1,'expected_log_growth':.001}
        return replay_trade(group,[r['ts'] for r in group],0,'yes',p,asof=200)

    def test_actual_quantity_and_delayed_bid_determine_pnl(self):
        t=self.trade();self.assertEqual(t['status'],'closed');self.assertEqual(t['quantity'],3)
        self.assertEqual(t['cost'],1.57);self.assertEqual(t['exit_ts'],101.5)
        self.assertAlmostEqual(t['pnl'],sale(3,.6,1)-1.57)

    def test_insufficient_full_quantity_exit_is_not_a_success(self):
        t=self.trade(depth=2)
        self.assertEqual(t['status'],'censored');self.assertEqual(t['pnl'],-t['cost'])


if __name__=='__main__':unittest.main()
