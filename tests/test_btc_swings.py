"""Touch probabilities are distinct from settlement and executable resale gains."""
import math
import unittest
from BTC.system.swings import SETTINGS,touch_probability,purchase,sale,exit_bid,eligible,path_trade,predict,state,live
from BTC.system.settlement import YEAR
from desk.model import digital_probability


def row(t=100,bid=.49,ask=.5):
    return {'ts':t,'ticker':'BTC','spot':100.,'spot_time':t,'strike':100.,'mid':(bid+ask)/2,'bid':bid,'ask':ask,
            'bid_qty':100,'ask_qty':100,'fee_multiplier':1.,'baseline':.7,'imbalance':.4,'momentum':0.,'seconds_left':1000-t}


class TouchTests(unittest.TestCase):
    def test_zero_log_drift_matches_reflection_principle(self):
        sigma=.6;seconds=900;z=math.log(101/100)/(sigma*math.sqrt(seconds/YEAR))
        expected=math.erfc(z/math.sqrt(2))
        self.assertAlmostEqual(touch_probability(100,101,seconds,sigma,drift=.5*sigma*sigma),expected,places=12)
        self.assertAlmostEqual(touch_probability(100,100/1.01,seconds,sigma,drift=.5*sigma*sigma),expected,places=12)

    def test_touch_can_be_more_likely_than_finishing_beyond_target(self):
        touch=touch_probability(100,100.1,600,.6)
        terminal=digital_probability(100,100.1,600,.6,0)
        self.assertGreater(touch,terminal);self.assertLessEqual(touch,1)
        self.assertGreater(touch_probability(100,100.1,900,.6),touch)

    def test_boundaries_deterministic_paths_and_invalid_inputs(self):
        self.assertEqual(touch_probability(100,100,0,0),1)
        self.assertEqual(touch_probability(100,101,0,.5),0)
        self.assertEqual(touch_probability(100,101,900,0),0)
        self.assertEqual(touch_probability(100,101,YEAR,0,.02),1)
        self.assertEqual(touch_probability(100,99,YEAR,0,-.02),1)
        for args in [(100,0,1,.5),(100,101,-1,.5),(100,101,1,-.5),(100,float('nan'),1,.5)]:
            with self.assertRaises(ValueError):touch_probability(*args)


class SwingExecutionTests(unittest.TestCase):
    def run_path(self,group,settings=None,asof=None):
        return path_trade(group,[r['ts'] for r in group],0,'yes',settings or SETTINGS,asof=asof)

    def test_required_bid_includes_both_fees_and_account_rounding(self):
        cost=purchase(.5,1);self.assertEqual(cost,.52)
        bid=exit_bid(cost,.05,1)
        self.assertGreater(bid,.55)
        self.assertGreaterEqual(sale(bid,1),cost*1.05)
        self.assertLess(sale(round(bid-.0001,4),1),cost*1.05)
        self.assertIsNone(exit_bid(.98,.05,1))

    def test_take_profit_uses_actual_bid_after_exit_latency(self):
        group=[row(),row(100.5),row(101,.58,.59),row(101.5,.58,.59)]
        result=self.run_path(group)
        self.assertEqual(result['reason'],'target');self.assertTrue(result['success'])
        self.assertEqual(result['entry_ts'],100.5);self.assertEqual(result['exit_ts'],101.5)
        self.assertAlmostEqual(result['pnl'],.04)

    def test_target_trigger_does_not_guarantee_target_fill(self):
        group=[row(),row(100.5),row(101,.58,.59),row(101.5,.54,.55)]
        result=self.run_path(group)
        self.assertEqual(result['reason'],'target');self.assertFalse(result['success'])
        self.assertTrue(result['target_trigger_lost_to_latency']);self.assertAlmostEqual(result['roi'],0)

    def test_midpoint_or_ask_rally_does_not_count_as_a_sellable_gain(self):
        group=[row(),row(100.5),row(101,.51,.60),row(101.5,.51,.60),row(102,.51,.60)]
        result=self.run_path(group,{**SETTINGS,'max_hold_seconds':1})
        self.assertEqual(result['reason'],'time');self.assertFalse(result['success']);self.assertLess(result['pnl'],0)

    def test_stop_is_not_a_guaranteed_loss_limit(self):
        result=self.run_path([row(),row(100.5),row(101,.48,.49),row(101.5,.45,.46)])
        self.assertEqual(result['reason'],'stop');self.assertLess(result['roi'],-SETTINGS['stop_loss'])

    def test_outage_is_censored_and_not_a_fabricated_stop_fill(self):
        result=self.run_path([row(),row(100.5),row(104,.60,.61)],asof=200)
        self.assertEqual(result['status'],'censored');self.assertIsNone(result['roi']);self.assertEqual(result['roi_floor'],-1)

    def test_unmatured_live_path_is_pending_not_a_full_loss(self):
        self.assertEqual(self.run_path([row(),row(100.5),row(101)],asof=101)['status'],'pending')

    def test_entry_rejects_adverse_limit_price_and_already_breached_stop(self):
        self.assertEqual(self.run_path([row(),row(100.5,.51,.52)])['status'],'entry_rejected')
        self.assertFalse(eligible(row(bid=.45),'yes'))

    def test_missing_exit_depth_does_not_produce_a_profitable_fill(self):
        group=[row(),row(100.5),row(101,.58,.59),{**row(101.5,.58,.59),'bid_qty':.5}]
        self.assertEqual(self.run_path(group)['status'],'censored')

    def test_sparse_state_is_not_assigned_a_probability(self):
        self.assertIsNone(predict({'state':{},'coarse':{}},row(),'yes'))

    def test_resale_success_probabilities_are_not_forced_to_be_complements(self):
        r=row();stats={'contracts':30,'lower_mean_return_95':-.1,'full_kelly_fraction':0,'censored':0}
        model={'state':{state(r,'yes')[0]:{**stats,'target_success_probability':.2},state(r,'no')[0]:{**stats,'target_success_probability':.3}},'coarse':{}}
        self.assertEqual(predict(model,r,'yes')['target_success_probability'],.2)
        self.assertEqual(predict(model,r,'no')['target_success_probability'],.3)

    def test_fee_changes_and_price_regimes_do_not_reuse_an_unmatched_state(self):
        r=row();self.assertNotEqual(state(r,'yes'),state({**r,'fee_multiplier':0.},'yes'))
        self.assertNotEqual(state(r,'yes'),state({**r,'mid':.85},'yes'))

    def test_unknown_fees_withhold_entry_and_exit_cost_estimates(self):
        r={**row(),'fee_multiplier':None};result=live(r,.6,{'model':{'state':{},'coarse':{}}})
        for candidate in result['sides']:
            self.assertIsNone(candidate['one_contract_cost']);self.assertIsNone(candidate['required_exit_bid'])
            self.assertFalse(candidate['research_candidate'])


if __name__=='__main__':unittest.main()
