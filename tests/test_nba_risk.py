"""Kelly must respect prices, actual depth, existing exposure and slate-wide limits."""
import copy
import math
import tempfile
import unittest
import json
import threading
import urllib.request
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from NBA.risk import settings as risk_settings,kelly_fraction,size_route,allocate,positions,tracked_alert,exits,risk_plan,scalp_kelly


def settings(update):
    return risk_settings({'max_loss_fraction':1,'sizing_mode':'settlement','kelly_fraction':.25,'max_game_fraction':.02,'max_total_fraction':.05,**update})

NOW=datetime(2026,11,22,3,tzinfo=timezone.utc)


def route(game='1',p=.7,price=.5,side='yes',quantity=1000):
    return {'game_id':game,'ticker':'NBA-'+game,'side':side,'yes_team':'BOS','outcome_team':'BOS',
        'model_probability':p,'scenario_probability':p,'sizing_probability':p,'quote_at':NOW.isoformat(),
        'multiplier':0,'book':{side+'_asks':[{'price':price,'quantity':quantity}]}}


class RiskTests(unittest.TestCase):
    def test_scalp_kelly_uses_partial_stop_loss_and_can_exceed_cash_theoretically(self):
        r=scalp_kelly(2/3,.1,.05,.6)
        self.assertAlmostEqual(r['risk_fraction'],.5)
        self.assertAlmostEqual(r['capital_fraction'],6)
        r=scalp_kelly(2/3,.058,.095,.622)
        self.assertAlmostEqual(r['capital_fraction'],.7901996370235929)
        self.assertAlmostEqual(r['risk_fraction'],.1206896551724137)

    def test_scalp_kelly_needs_a_separate_hit_rate_and_zero_edge_means_zero(self):
        cfg=risk_settings({'slippage':0})
        self.assertIsNone(size_route(route(p=.99),1000,cfg))
        self.assertEqual(scalp_kelly(.5,.05,.10,.6)['capital_fraction'],0)

    def test_tighter_stop_can_increase_scalp_size_without_any_hidden_two_percent_cap(self):
        cfg=risk_settings({'max_loss_fraction':1,'slippage':0,'scalp_hit_rate':.55,'stop_move':.10,'target_move':.10})
        narrow=size_route(route(p=.9),1000,cfg)
        wide=size_route(route(p=.9),1000,{**cfg,'stop_move':.12})
        self.assertGreater(narrow['stake_fraction'],.4)
        self.assertGreater(narrow['quantity'],wide['quantity'])
        self.assertLessEqual(narrow['all_in_cost'],1000)

    def test_scalp_size_is_not_derived_from_game_winner_probability(self):
        cfg=risk_settings({'max_loss_fraction':1,'slippage':0,'scalp_hit_rate':.55,'stop_move':.1})
        a=size_route(route(p=.6),1000,cfg);b=size_route(route(p=.95),1000,cfg)
        self.assertEqual(a['quantity'],b['quantity'])

    def test_scalp_allocation_still_cannot_spend_more_than_available_cash(self):
        cfg=risk_settings({'max_loss_fraction':1,'slippage':0,'scalp_hit_rate':.7})
        plans,summary=allocate({str(i):[route(str(i),.9)] for i in range(3)},cfg,[])
        self.assertLessEqual(summary['proposed_cost'],1000)
        self.assertGreater(summary['proposed_cost'],900)
        self.assertTrue(all(p['stake_fraction']>.05 for p in plans.values()))

    def test_scap_gap_stress_does_not_masquerade_as_the_planned_stop(self):
        cfg=risk_settings({'max_loss_fraction':1,'scalp_hit_rate':2/3})
        p=size_route(route(p=.9),1000,cfg)
        self.assertLess(p['exit_plan']['net_at_gap_stop'],p['exit_plan']['net_at_stop'])
        self.assertGreater(p['gap_stop_risk_fraction'],p['planned_stop_risk_fraction'])
        self.assertLessEqual(p['gap_kelly']['capital_fraction'],p['full_kelly_fraction'])
    def test_kelly_is_fraction_of_bankroll_paid_not_confidence(self):
        self.assertAlmostEqual(kelly_fraction(.6,.5),.2)
        self.assertEqual(kelly_fraction(.9,.95),0,'A confident favorite can still be overpriced')
        self.assertEqual(kelly_fraction(.5,1),0)

    def test_fractional_kelly_and_per_game_limits(self):
        cfg=settings({'slippage':0,'max_game_fraction':.25,'max_total_fraction':.5})
        p=size_route(route(p=.6),250,cfg)
        self.assertEqual(p['quantity'],100)
        self.assertAlmostEqual(p['all_in_cost'],50)
        capped=size_route(route(p=.9),20,cfg)
        self.assertLessEqual(capped['all_in_cost'],20)

    def test_more_expensive_deeper_liquidity_is_not_scaled_as_the_best_ask(self):
        r=route();r['book']['yes_asks']=[{'price':.5,'quantity':10},{'price':.9,'quantity':1000}]
        cfg=settings({'slippage':0,'max_game_fraction':.25,'max_total_fraction':.5})
        p=size_route(r,250,cfg)
        self.assertEqual(p['quantity'],10)
        self.assertEqual(p['all_in_cost'],5)

    def test_fee_rounding_and_slippage_are_part_of_the_quantity_budget(self):
        r=route();r['multiplier']=1
        p=size_route(r,20,settings({}))
        self.assertLess(p['quantity'],40)
        self.assertLessEqual(p['all_in_cost'],20)
        self.assertAlmostEqual(p['all_in_cost'],p['quantity']*p['entry_vwap']+p['entry_fees']+.005*p['quantity'])

    def test_allocations_share_one_total_budget_even_across_hidden_games(self):
        cfg=settings({'slippage':0});routes={str(i):[route(str(i),.9)] for i in range(8)}
        plans,summary=allocate(routes,cfg,[])
        self.assertLessEqual(sum(p['all_in_cost'] for p in plans.values()),50+1e-8)
        self.assertEqual(len(plans),8);self.assertTrue(all(p['portfolio_scaled'] for p in plans.values()))

    def test_recorded_and_other_exposure_reduce_budget_and_block_reentry(self):
        cfg=settings({'other_exposure':20,'slippage':0})
        held=[{'game_id':'1','entry_cost':20}]
        plans,summary=allocate({'1':[route('1',.9)],'2':[route('2',.9)]},cfg,held)
        self.assertNotIn('1',plans);self.assertLessEqual(summary['proposed_cost'],10)
        self.assertLessEqual(summary['total_exposure_after'],50)

    def test_existing_overexposure_is_reported_without_allocating_more(self):
        plans,summary=allocate({'1':[route()]},settings({'other_exposure':80}),[])
        self.assertFalse(plans);self.assertTrue(summary['existing_exceeds_cap'])

    def test_no_cash_zero_equity_or_no_fraction_means_no_allocation(self):
        for update in [{'cash':0},{'equity':0},{'kelly_fraction':0}]:
            cfg=settings(update);plans,summary=allocate({'1':[route()]},cfg,[])
            self.assertFalse(plans)

    def test_settlement_stop_distance_never_increases_kelly_size(self):
        a=size_route(route(),20,settings({'stop_move':.01}))
        b=size_route(route(),20,settings({'stop_move':.2}))
        self.assertEqual(a['quantity'],b['quantity']);self.assertEqual(a['all_in_cost'],b['all_in_cost'])

    def test_invalid_or_nonfinite_settings_fail_closed(self):
        for update in [{'equity':float('nan')},{'cash':True},{'max_game_fraction':.1,'max_total_fraction':.05},{'unknown':1}]:
            with self.assertRaises(ValueError):settings(update)

    def test_stop_gap_uses_actual_bid_and_full_position_fees(self):
        p={'id':'p','game_id':'1','ticker':'NBA-1','side':'yes','quantity':10,'entry_price':.6,'entry_cost':6.18,
            'stop_bid':.55,'target_bid':.7,'opened_at':NOW.isoformat()}
        quote={'observed_at':NOW.isoformat(),'status':'active','multiplier':1,'book':{'yes_bids':[{'price':.5,'quantity':10}]}}
        result=tracked_alert(p,quote,None,settings({}),NOW)
        self.assertEqual(result['status'],'stop_reached')
        self.assertAlmostEqual(result['unrealized_pnl'],-1.41)
        self.assertEqual(result['max_loss'],6.18)
        quote['book']['yes_bids'][0]['quantity']=3
        result=tracked_alert(p,quote,None,settings({}),NOW)
        self.assertEqual(result['status'],'stop_reached_insufficient_depth');self.assertIsNone(result['unrealized_pnl'])

    def test_stale_bid_cannot_trigger_an_exit_or_mark_profit(self):
        p={'id':'p','game_id':'1','ticker':'NBA-1','side':'no','quantity':10,'entry_price':.6,'entry_cost':6,
            'opened_at':NOW.isoformat()}
        quote={'observed_at':(NOW-timedelta(seconds=31)).isoformat(),'status':'active','multiplier':1,'book':{'no_bids':[{'price':.8,'quantity':10}]}}
        r=tracked_alert(p,quote,None,settings({}),NOW)
        self.assertEqual(r['status'],'quote_unavailable');self.assertIsNone(r['unrealized_pnl'])

    def test_no_contract_settlement_is_complement_of_yes_value(self):
        p={'id':'p','game_id':'1','ticker':'NBA-1','side':'no','quantity':10,'entry_price':.6,'entry_cost':6,'opened_at':NOW.isoformat()}
        r=tracked_alert(p,None,{'status':'finalized','settlement_value':0},settings({}),NOW)
        self.assertEqual(r['net_liquidation'],10);self.assertEqual(r['unrealized_pnl'],4)

    def test_live_game_never_uses_saved_pregame_probability_for_new_kelly(self):
        g={'game_id':'1','home':'BOS','away':'NYK','tipoff':(NOW-timedelta(minutes=1)).isoformat(),
            'prediction':{'home_win_probability':.9},'status':'research_estimate','blockers':[]}
        with patch('NBA.risk.public_snapshot',return_value={'model_id':'test','games':[g]}),patch('NBA.risk.load_quotes',return_value=({},{})):
            report=risk_plan({},now=NOW)
        self.assertFalse(report['allocations']);self.assertIn('Game started',report['reasons']['1'])

    def test_risk_http_calculation_does_not_require_or_open_a_trading_engine(self):
        from desk.server import Handler,LocalServer
        server=LocalServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True)
        try:
            with patch('NBA.risk.risk_plan',return_value={'allocations':{},'read_only':True}) as calculate:
                thread.start();body={'settings':{'equity':1000}}
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/nba/risk',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
                with urllib.request.urlopen(req,timeout=3) as response:self.assertTrue(json.load(response)['read_only'])
                calculate.assert_called_once_with(body)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
