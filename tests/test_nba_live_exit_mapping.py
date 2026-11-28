"""Home-contract live routes must map to the actual held winning outcome."""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from NBA.risk import risk_plan

NOW=datetime(2026,11,25,23,tzinfo=timezone.utc)

class LiveExitMappingTests(unittest.TestCase):
    def report(self,yes_team,side):
        ticker='KXNBAGAME-26NOV25NYKBOS-'+yes_team
        other='NYK' if yes_team=='BOS' else 'BOS'
        yes_price=.45 if yes_team=='BOS' else .53
        quote={'ticker':ticker,'game_id':'g','yes_team':yes_team,'status':'active','score_state':'in','observed_at':NOW.isoformat(),'multiplier':0,
            'book':{'yes_bids':[{'price':yes_price,'quantity':100}],'yes_asks':[{'price':yes_price+.02,'quantity':100}],
                    'no_bids':[{'price':.53 if other=='NYK' else .45,'quantity':100}],'no_asks':[{'price':.55 if other=='NYK' else .47,'quantity':100}]}}
        game={'game_id':'g','home':'BOS','away':'NYK','date':'2026-11-25','tipoff':(NOW-timedelta(minutes=5)).isoformat(),
            'prediction':None,'live_model':{'home_probability':.55,'model_id':'test','ticker':'KXNBAGAME-26NOV25NYKBOS-BOS',
              'routes':[{'side':'yes','outcome_team':'BOS','fair_probability':.55},{'side':'no','outcome_team':'NYK','fair_probability':.45}]}}
        body={'positions':[{'id':'p','game_id':'g','ticker':ticker,'side':side,'quantity':10,'entry_price':.5,'entry_fees':0,
            'opened_at':(NOW-timedelta(minutes=1)).isoformat(),'stop_bid':.1,'target_bid':.9}]}
        with tempfile.TemporaryDirectory() as directory,patch('NBA.risk.public_snapshot',return_value={'games':[game]}),patch('NBA.risk.load_quotes',return_value=({ticker:quote},{ticker:{'ticker':ticker,'game_id':'g','yes_team':yes_team,'status':'active'}})):
            return risk_plan(body,directory=directory,now=NOW)['positions'][0]['live_model_exit']

    def test_all_four_routes_use_their_actual_winning_team(self):
        for team,side,expected,action in [('BOS','yes',.55,'HOLD_CANDIDATE'),('BOS','no',.45,'EXIT_CANDIDATE'),
                                          ('NYK','yes',.45,'EXIT_CANDIDATE'),('NYK','no',.55,'HOLD_CANDIDATE')]:
            with self.subTest(team=team,side=side):
                result=self.report(team,side)
                self.assertEqual(result['fair_probability'],expected);self.assertEqual(result['action'],action)
