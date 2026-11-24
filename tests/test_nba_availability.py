"""Official availability, stale-signal invalidation and worker recovery regressions."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, date
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from NBA.availability import entry_safety, fingerprint, report_metadata
from NBA.context import ContextState
from NBA.data import atomic_json
from NBA.live_context import collect
from NBA.official_injuries import latest_url, parse_rows, game_reports, player_key
from NBA.risk import settings, size_route, risk_plan
from NBA.service import public_snapshot
from NBA.watch import UpdateWorker

NOW=datetime(2026,11,23,21,tzinfo=timezone.utc)


def fixture():
    return {'game_id':'1','home':'IND','away':'OKC','date':'2026-11-23','season':2027,'tipoff':(NOW+timedelta(hours=2)).isoformat()}


def observation():
    return {'schema':2,'observed_at':NOW.isoformat(),'injuries':{},'lineups':{},'game_injuries':{'1':{
        'teams':{'IND':[],'OKC':[{'id':'sga','name':'Shai Gilgeous-Alexander','status':'out'}]},
        'coverage':{'IND':'reported','OKC':'reported'},'published_at':(NOW-timedelta(minutes=10)).isoformat(),
        'checked_at':NOW.isoformat(),'observed_at':NOW.isoformat(),'unmatched':[],
        'url':'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-23_03_45PM.pdf'}}}


def game():
    f=fixture();obs=observation()
    return {**f,'prediction':{'home_win_probability':.7},'status':'research_estimate','blockers':[],
        'markets':[],'context':{'availability':report_metadata(obs,f),'rotations':{
        'home':{'players':[],'out_minutes':0,'confirmed':False},
        'away':{'players':[{'id':'sga','name':'Shai Gilgeous-Alexander','baseline_minutes':36,'projected_minutes':0,'availability':'out'}],'out_minutes':36,'confirmed':False}}},
        'availability_fingerprint':fingerprint(obs,f)}


class AvailabilityTests(unittest.TestCase):
    def test_official_out_wins_over_espn_generic_questionable_for_this_game_only(self):
        state=ContextState({}, {}, {}, {2027:{}})
        state.history['OKC'].append({'date':'2026-11-22','game_id':'past','players':[{'id':'sga','name':'Shai Gilgeous-Alexander','minutes':36,'starter':True}]})
        obs=observation();obs['injuries']['OKC']=[{'id':'sga','name':'Shai Gilgeous-Alexander','status':'questionable'}]
        _,detail=state.features(fixture(),{},obs)
        p=detail['rotations']['away']['players'][0]
        self.assertEqual(p['projected_minutes'],0);self.assertEqual(p['availability'],'out')
        other={**fixture(),'game_id':'other','date':'2026-11-24'}
        _,detail=state.features(other,{},obs)
        self.assertEqual(detail['rotations']['away']['players'][0]['availability'],'questionable')

    def test_recent_official_report_allows_a_known_absence_without_guessing_play_probability(self):
        self.assertFalse(entry_safety(game(),NOW,observation())['blocked'])

    def test_stale_missing_and_unsubmitted_reports_block_entries(self):
        for kind in ['stale_check','stale_publication','unsubmitted','missing']:
            obs=observation();g=game()
            if kind=='stale_check':obs['game_injuries']['1']['checked_at']=(NOW-timedelta(seconds=301)).isoformat()
            elif kind=='stale_publication':obs['game_injuries']['1']['published_at']=(NOW-timedelta(hours=2)).isoformat()
            elif kind=='unsubmitted':obs['game_injuries']['1']['coverage']['OKC']='not_submitted'
            else:obs['game_injuries']={}
            with self.subTest(kind=kind):self.assertTrue(entry_safety(g,NOW,obs)['blocked'])

    def test_status_change_invalidates_a_forecast_before_the_next_scan_finishes(self):
        g=game();obs=observation();obs['game_injuries']['1']['teams']['OKC'][0]['status']='questionable'
        safety=entry_safety(g,NOW,obs)
        self.assertIn('availability_changed_since_prediction',safety['reasons'])
        unchanged=observation();unchanged['game_injuries']['1']['checked_at']=(NOW+timedelta(seconds=10)).isoformat()
        self.assertEqual(fingerprint(observation(),g),fingerprint(unchanged,g),'Polling timestamps do not constitute a status change')

    def test_major_uncertainty_rotation_disruption_and_late_lineups_are_separate_blocks(self):
        g=game();g['context']['rotations']['away']['players'][0]['availability']='questionable'
        self.assertIn('key_player_uncertain',entry_safety(g,NOW)['reasons'])
        g=game();g['context']['rotations']['away']['out_minutes']=70
        self.assertIn('large_rotation_disruption',entry_safety(g,NOW)['reasons'])
        g=game();g['tipoff']=(NOW+timedelta(minutes=20)).isoformat()
        self.assertIn('lineups_unconfirmed_near_tipoff',entry_safety(g,NOW)['reasons'])
        for rotation in g['context']['rotations'].values():rotation['confirmed']=True
        self.assertNotIn('lineups_unconfirmed_near_tipoff',entry_safety(g,NOW)['reasons'])

    def test_api_immediately_removes_edges_when_live_availability_has_changed(self):
        with tempfile.TemporaryDirectory() as d:
            g=game();g['markets']=[{'quote_received_at':NOW.isoformat(),'candidates':[{'status':'research_edge','net_edge':.1,'expected_profit_dollars':1,'blockers':[]}]}]
            atomic_json(Path(d)/'fair-prices.json',{'generated_at':NOW.isoformat(),'model_id':'test','games':[g],'quote_max_age_seconds':90,'estimate_max_age_seconds':180})
            atomic_json(Path(d)/'model-report.json',{'model_id':'test'})
            obs=observation();obs['game_injuries']['1']['teams']['OKC'][0]['status']='available'
            atomic_json(Path(d)/'context/live.json',obs)
            result=public_snapshot(d,NOW)
            self.assertIsNotNone(result['games'][0]['prediction'],'Fair estimate remains visible for review')
            self.assertIsNone(result['games'][0]['markets'][0]['candidates'][0]['net_edge'])
            self.assertIn('availability_changed_since_prediction',result['games'][0]['entry_safety']['reasons'])

    def test_referee_chunk_failure_cannot_kill_injury_collection(self):
        with tempfile.TemporaryDirectory() as d,patch('NBA.live_context.get_json',return_value={'injuries':[]}),patch('NBA.official_injuries.collect_official',return_value={'games':{}}),patch('NBA.live_context.urllib.request.urlopen',side_effect=IncompleteRead(b'partial')):
            result=collect({'schedule':[]},d,NOW)
        self.assertTrue(any(s['kind']=='injuries' for s in result['sources']))
        self.assertTrue(any('referees:' in error for error in result['errors']))

    def test_official_names_and_report_game_context_are_not_flattened_across_dates(self):
        rows=[['11/23/2026','07:00 (ET)','OKC@IND','Oklahoma City Thunder','Gilgeous-Alexander, Shai','Out',''],
              ['','','','','','', 'Neck Strain'],['','','','Indiana Pacers','','NOT YET','SUBMITTED']]
        parsed=parse_rows(rows)
        self.assertEqual(parsed['2026-11-23|OKC@IND']['OKC']['players'][0]['name'],'Shai Gilgeous-Alexander')
        self.assertEqual(parsed['2026-11-23|OKC@IND']['IND']['status'],'not_submitted')
        source={'games':parsed,'url':'official','published_at':NOW.isoformat(),'observed_at':NOW.isoformat(),'checked_at':NOW.isoformat()}
        reports=game_reports({'schedule':[fixture(),{**fixture(),'game_id':'2','date':'2026-11-24'}]},source,{}, {'OKC':{player_key('Shai Gilgeous-Alexander'):'sga'}})
        self.assertIn('1',reports);self.assertNotIn('2',reports)

    def test_official_link_selection_rejects_future_reports(self):
        a='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-23_03_45PM.pdf'
        b='https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-23_04_15PM.pdf'
        self.assertEqual(latest_url(a+' '+b,NOW),a)

    def test_transport_failure_is_retried_and_does_not_permanently_stop_worker(self):
        with tempfile.TemporaryDirectory() as d:
            args=SimpleNamespace(data_dir=Path(d),interval=30,refresh_hours=6,quantity=10,slippage=.005,min_edge=.03)
            worker=UpdateWorker(args)
            valid={'generated_at':NOW.isoformat(),'model_id':'test','games':[]}
            with patch('NBA.watch.load_dataset',return_value={'generated_at':NOW.isoformat()}),patch('NBA.watch.utcnow',return_value=NOW),patch('NBA.watch.scan_once',side_effect=[IncompleteRead(b'partial'),valid]),patch('NBA.watch.traceback.print_exc'):
                self.assertIsNone(worker.step());self.assertIn('IncompleteRead',worker.status['scan_error'])
                self.assertEqual(worker.step(),valid);self.assertIsNone(worker.status['scan_error'])
            with patch('NBA.live_context.collect',side_effect=IncompleteRead(b'partial')),patch('NBA.watch.load_dataset',return_value={'schedule':[]}),patch('NBA.watch.traceback.print_exc'):
                self.assertIsNone(worker.availability_once());self.assertIn('IncompleteRead',worker.status['availability_error'])

    def test_newer_game_specific_news_pauses_before_the_next_official_pdf(self):
        rows=[['11/23/2026','07:00 (ET)','OKC@IND','Oklahoma City Thunder','Caruso, Alex','Out',''],
              ['','','','Indiana Pacers','Player, Other','Available','']]
        official={'games':parse_rows(rows),'url':'official','published_at':(NOW-timedelta(minutes=15)).isoformat(),'observed_at':NOW.isoformat(),'checked_at':NOW.isoformat()}
        espn={'OKC':[{'id':'sga','name':'Shai Gilgeous-Alexander','status':'out','source_updated_at':NOW.isoformat(),
                      'detail':"Gilgeous-Alexander is ruled out for Monday's game against Indiana."}]}
        reports=game_reports({'schedule':[fixture()]},official,espn,{})
        row=reports['1']['teams']['OKC'][0]
        self.assertEqual(row['id'],'sga');self.assertEqual(row['status'],'uncertain');self.assertTrue(row['source_conflict'])
        self.assertTrue(reports['1']['pending_secondary'])
        espn['OKC'][0]['detail']="Gilgeous-Alexander is listed as out for Sunday's game against Miami."
        reports=game_reports({'schedule':[fixture()]},official,espn,{})
        self.assertFalse(reports['1']['pending_secondary'],'A fresh timestamp on an old-game comment cannot update tonight’s status')

    def test_source_conflicts_and_unreliable_rotations_block_new_entries(self):
        g=game();g['context']['rotations']['away']['players'][0]['source_conflict']=True
        self.assertIn('injury_sources_disagree',entry_safety(g,NOW)['reasons'])
        g=game();g['context']['rotations']['away']['unknown_share']=.3
        self.assertIn('rotation_projection_unreliable',entry_safety(g,NOW)['reasons'])


class LossBudgetTests(unittest.TestCase):
    def route(self):
        return {'game_id':'1','ticker':'NBA-1','side':'yes','yes_team':'IND','outcome_team':'IND','sizing_probability':.9,
                'multiplier':0,'book':{'yes_asks':[{'price':.5,'quantity':10000}]}}

    def test_missing_loss_budget_means_no_funded_size(self):
        self.assertIsNone(size_route(self.route(),1000,settings({'scalp_hit_rate':.7})))

    def test_full_loss_budget_limits_contract_cost_even_when_kelly_wants_much_more(self):
        cfg=settings({'scalp_hit_rate':.7,'slippage':0,'max_loss_fraction':.02})
        p=size_route(self.route(),1000,cfg)
        self.assertLessEqual(p['all_in_cost'],20);self.assertEqual(p['loss_budget_basis'],'full_loss')
        tighter=size_route(self.route(),1000,{**cfg,'stop_move':.01})
        self.assertEqual(tighter['quantity'],p['quantity'],'A tighter stop cannot override full-loss protection')

    def test_opted_in_gap_basis_is_distinct_from_full_contract_exposure(self):
        p=size_route(self.route(),1000,settings({'scalp_hit_rate':.7,'slippage':0,'max_loss_fraction':.02,'risk_loss_basis':'gap_stop'}))
        self.assertLessEqual(p['loss_budget_measure'],20)
        self.assertGreater(p['all_in_cost'],20)

    def test_availability_and_large_market_disagreement_block_api_sizing(self):
        q={'ticker':'KXNBAGAME-26NOV23OKCIND-IND','game_id':'1','yes_team':'IND','status':'active','score_state':'pre','multiplier':0,'observed_at':NOW.isoformat(),
           'book':{'yes_bids':[{'price':.3,'quantity':1000}],'yes_asks':[{'price':.31,'quantity':1000}],
                   'no_bids':[{'price':.69,'quantity':1000}],'no_asks':[{'price':.7,'quantity':1000}]}}
        for safety in [{'blocked':True,'messages':['Injury report stale']},{'blocked':False}]:
            g=game();g['entry_safety']=safety
            with patch('NBA.risk.public_snapshot',return_value={'games':[g]}),patch('NBA.risk.load_quotes',return_value=({'NBA-1':q},{})):
                result=risk_plan({'settings':{'scalp_hit_rate':.9,'max_loss_fraction':1}},now=NOW)
            self.assertFalse(result['allocations'])
            self.assertIn('Injury report stale' if safety['blocked'] else 'disagreement',result['reasons']['1'])


if __name__=='__main__':unittest.main()
