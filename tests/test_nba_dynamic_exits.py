import copy
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from NBA.dynamic_exits import advance
from NBA.risk import tracked_alert,settings

NOW=datetime(2026,11,25,23,tzinfo=timezone.utc)

def position():
    return {'id':'p','ticker':'NBA-BOS','side':'yes','quantity':10,'game_id':'g','entry_price':.43,'entry_fees':.19,
        'entry_cost':4.49,'opened_at':NOW.isoformat(),'dynamic_started_at':NOW.isoformat(),
        'stop_bid':.38,'target_bid':.53,'initial_target_bid':.53,'dynamic_exits':True}

def history(seconds=180,bid=.48,margin=2):
    return [{'at':(NOW+timedelta(seconds=t)).isoformat(),'bid':bid,'spread':.02,'state':'in','period':3,
             'clock':500-t,'margin':margin,'score_age':2} for t in range(0,seconds+1,10)]

class DynamicTests(unittest.TestCase):
    def test_original_tp_survives_while_secondary_moves_by_one_cent(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48)
        self.assertEqual(r['original_target'],.53);self.assertEqual(r['active_target'],.52)
        self.assertEqual(r['active_stop'],.39);self.assertEqual(p['target_bid'],.53)
        self.assertLess(r['active_stop'],r['active_target'])

    def test_no_twitch_inside_minute_and_state_survives_round_trip(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48)
        p['dynamic_exit_state']=copy.deepcopy(r['state'])
        again=advance(p,settings({}),NOW+timedelta(seconds=190),history(190,bid=.50),.48)
        self.assertEqual(again['status'],'holding');self.assertEqual(again['active_target'],r['active_target']);self.assertEqual(again['active_stop'],r['active_stop'])

    def test_stale_or_gapped_history_freezes_levels(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48);p['dynamic_exit_state']=r['state']
        stale=advance(p,settings({}),NOW+timedelta(seconds=300),history(),.48)
        self.assertEqual(stale['status'],'frozen');self.assertEqual(stale['active_stop'],.39)
        data=history(300);data=[s for s in data if s['at']<(NOW+timedelta(seconds=180)).isoformat() or s['at']>(NOW+timedelta(seconds=250)).isoformat()]
        gap=advance(p,settings({}),NOW+timedelta(seconds=300),data,.48)
        self.assertEqual(gap['status'],'warming_up');self.assertEqual(gap['active_target'],.52)

    def test_targets_are_checked_before_recalculation_and_latched(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(bid=.54),.48)
        self.assertEqual(r['status'],'triggered');self.assertEqual(r['active_target'],.53);self.assertEqual(r['state']['trigger'],'target')
        p['dynamic_exit_state']=r['state'];r=advance(p,settings({}),NOW+timedelta(seconds=240),history(240,bid=.48),.48)
        self.assertEqual(r['state']['trigger'],'target')

    def test_stop_never_moves_down_as_price_or_volatility_worsens(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48);p['dynamic_exit_state']=r['state']
        data=history(240,bid=.44);r=advance(p,settings({}),NOW+timedelta(seconds=240),data,.48)
        self.assertGreaterEqual(r['active_stop'],.39);self.assertLessEqual(abs(r['active_target']-.52),.01000001)

    def test_warmup_and_score_validation_do_not_invent_confidence(self):
        r=advance(position(),settings({}),NOW+timedelta(seconds=30),history(30),.48)
        self.assertEqual(r['status'],'warming_up');self.assertIsNone(r['probability'])
        data=history();data[-1]['score_age']=100
        r=advance(position(),settings({}),NOW+timedelta(seconds=180),data,.48);self.assertEqual(r['status'],'frozen')

    def test_future_observations_and_future_cached_state_are_rejected(self):
        p=position();data=history();data.append({**data[-1],'at':(NOW+timedelta(seconds=900)).isoformat(),'bid':.99})
        r=advance(p,settings({}),NOW+timedelta(seconds=180),data,.48);self.assertEqual(r['state']['trigger'],None)
        p['dynamic_exit_state']={**r['state'],'updated_at':(NOW+timedelta(seconds=900)).isoformat(),'stop':.49}
        r=advance(p,settings({}),NOW+timedelta(seconds=190),history(190),.48);self.assertLess(r['active_stop'],.49)

    def test_stop_hit_has_priority_and_cannot_be_relaxed(self):
        r=advance(position(),settings({}),NOW+timedelta(seconds=180),history(bid=.35),.48)
        self.assertEqual(r['state']['trigger'],'stop');self.assertEqual(r['active_stop'],.38)

    def test_tracker_uses_active_exit_values_and_never_replays_future_books(self):
        p=position();at=NOW+timedelta(seconds=180)
        quote={'observed_at':at.isoformat(),'status':'active','multiplier':1,
               'book':{'yes_bids':[{'price':.48,'quantity':100}],'yes_asks':[{'price':.50,'quantity':100}]}}
        with patch('NBA.dynamic_exits.load_samples',return_value=history()) as load:
            r=tracked_alert(p,quote,None,settings({}),at)
        self.assertEqual(r['target_bid'],.52);self.assertEqual(r['stop_bid'],.39)
        self.assertEqual(load.call_args.args[2],at)
        self.assertEqual(r['dynamic_exits']['original_target'],.53)

    def test_missing_full_exit_depth_freezes_persisted_levels(self):
        p=position();r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48);p['dynamic_exit_state']=r['state']
        at=NOW+timedelta(seconds=190);quote={'observed_at':at.isoformat(),'status':'active','multiplier':1,
               'book':{'yes_bids':[{'price':.48,'quantity':1}],'yes_asks':[{'price':.50,'quantity':100}]}}
        with patch('NBA.dynamic_exits.load_samples') as load:r=tracked_alert(p,quote,None,settings({}),at)
        load.assert_not_called();self.assertEqual(r['dynamic_exits']['status'],'frozen');self.assertEqual(r['stop_bid'],.39)

    def test_a_recorded_stop_touch_cannot_disappear_after_a_rebound(self):
        data=history();data[5]['bid']=.37
        r=advance(position(),settings({}),NOW+timedelta(seconds=180),data,.48)
        self.assertEqual(r['state']['trigger'],'stop');self.assertEqual(r['state']['triggered_at'],data[5]['at'])

    def test_fractional_cent_reference_does_not_exceed_the_one_cent_step(self):
        p=position();p['target_bid']=.531
        r=advance(p,settings({}),NOW+timedelta(seconds=180),history(),.48)
        self.assertLessEqual(abs(r['active_target']-.531),.010000001)
