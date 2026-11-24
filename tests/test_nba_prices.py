"""Prices and daily game visibility must not depend on model entry approval."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from NBA.data import atomic_json
from NBA.live_board import load_live_games,merge_schedule
from NBA.markets import compare,market_view
from NBA.quotes import overlay_quotes,contract_matches_game
from NBA.recording import Recording,scoreboard_games
from NBA.service import public_snapshot

NOW=datetime(2026,11,23,20,tzinfo=timezone.utc)
TICKER='KXNBAGAME-26NOV23NYKBOS-BOS'
SERIES={'fee_type':'quadratic','fee_multiplier':1}


def fixture():
    return {'game_id':'1','home':'BOS','away':'NYK','date':'2026-11-23','tipoff':(NOW+timedelta(hours=3)).isoformat(),'season':2027,'neutral':False,'state':'pre','venue':'Arena'}


def book(yes=.55,no=.4):
    return {'orderbook_fp':{'yes_dollars':[[str(yes),'1000']],'no_dollars':[[str(no),'1000']]}}


def response(payload,at=NOW):
    return {'payload':payload,'url':'https://example.test','requested_at':(at-timedelta(seconds=1)).isoformat(),'received_at':at.isoformat(),'error':None,'http_status':200}


def game():
    return {**fixture(),'prediction':{'home_win_probability':.7,'away_win_probability':.3},'preview_prediction':None,'pregame_prediction':None,
            'blockers':[],'status':'research_estimate','markets':[],'entry_safety':{'blocked':False,'reasons':[]}}


class PriceTests(unittest.TestCase):
    def test_quotes_and_costs_exist_without_a_win_forecast(self):
        yes,no=compare(None,book(),SERIES)
        self.assertEqual(yes['best_ask'],.6);self.assertEqual(yes['best_bid'],.55)
        self.assertEqual(no['best_ask'],.45);self.assertEqual(no['best_bid'],.4)
        self.assertIsNone(yes['net_edge']);self.assertIsNone(yes['probability'])
        self.assertGreater(yes['all_in_cost'],6);self.assertEqual(yes['status'],'quotes_only')

    def test_approval_blocks_an_edge_without_hiding_its_prices(self):
        g=game();g['entry_safety']={'blocked':True,'reasons':['key_player_uncertain']}
        m=market_view(g,'BOS',book(),SERIES,NOW.isoformat())
        self.assertEqual(m['quotes']['yes_ask'],.6)
        self.assertIsNone(m['candidates'][0]['net_edge'])

    def test_newer_books_recompute_edges_instead_of_relabeling_an_old_edge(self):
        g=game();g['markets']=[{'ticker':TICKER,'yes_team':'BOS','quote_received_at':(NOW-timedelta(seconds=40)).isoformat(),'candidates':[{'side':'yes','net_edge':.2}]}]
        q={'game_id':'1','yes_team':'BOS','observed_at':(NOW-timedelta(seconds=2)).isoformat(),'book':True,'payload':book(),'multiplier':1}
        s={'games':[g],'quantity':10,'slippage_buffer':.005,'min_edge':.03}
        overlay_quotes(s,NOW,{TICKER:q},{TICKER:{'status':'active'}})
        m=g['markets'][0]
        self.assertEqual(m['quote_source'],'local_recording');self.assertAlmostEqual(m['candidates'][0]['net_edge'],.078)
        self.assertEqual(m['quotes']['yes_bid'],.55)

    def test_an_older_recorder_quote_cannot_replace_a_newer_scanner_quote(self):
        g=game();g['markets']=[{'ticker':TICKER,'yes_team':'BOS','quote_received_at':NOW.isoformat(),'candidates':[{'net_edge':.123}]}]
        q={'game_id':'1','yes_team':'BOS','observed_at':(NOW-timedelta(seconds=2)).isoformat(),'book':True,'payload':book(),'multiplier':1}
        overlay_quotes({'games':[g]},NOW,{TICKER:q},{TICKER:{'status':'active'}})
        self.assertEqual(g['markets'][0]['candidates'][0]['net_edge'],.123)

    def test_latest_failed_book_cannot_leave_a_fresh_edge_behind(self):
        g=game();g['markets']=[{'ticker':TICKER,'yes_team':'BOS','quote_received_at':(NOW-timedelta(seconds=30)).isoformat(),'candidates':[{'net_edge':.2}]}]
        overlay_quotes({'games':[g]},NOW,{TICKER:{'game_id':'1','yes_team':'BOS','observed_at':NOW.isoformat(),'error':'Timeout'}},{TICKER:{'status':'active'}})
        self.assertFalse(g['markets'][0]['candidates']);self.assertEqual(g['markets'][0]['error'],'Timeout')

    def test_contract_date_and_yes_team_identity_are_verified(self):
        self.assertTrue(contract_matches_game(TICKER,'BOS',game()))
        self.assertFalse(contract_matches_game(TICKER,'NYK',game()))
        self.assertFalse(contract_matches_game(TICKER,'BOS',{**game(),'date':'2026-11-24'}))


class LiveBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name);self.store=Recording(self.directory)

    def tearDown(self):self.store.close();self.temp.cleanup()

    def record(self,state='pre',tip=None):
        event={'id':'1','date':tip or fixture()['tipoff'],'season':{'year':2027,'type':2},
            'status':{'period':1,'clock':600,'displayClock':'10:00','type':{'state':state,'name':'STATUS_IN_PROGRESS' if state=='in' else 'STATUS_SCHEDULED'}},
            'competitions':[{'neutralSite':False,'venue':{'fullName':'Arena'},'competitors':[
                {'homeAway':'home','team':{'abbreviation':'BOS'},'score':'42'},
                {'homeAway':'away','team':{'abbreviation':'NYK'},'score':'40'}]}]}
        payload={'events':[event]};req=self.store.request(None,'scoreboard',response(payload))
        row=scoreboard_games(payload)[0][0];self.store.score(row,req,NOW.isoformat())
        market={'ticker':TICKER,'event_ticker':TICKER.rsplit('-',1)[0],'market_type':'binary','status':'active'}
        req=self.store.request(None,'markets',response({'markets':[market]}));self.store.market(market,req,NOW.isoformat(),self.store.fixtures())
        series=self.store.request(None,'series',response({'series':SERIES}))
        r=response(book(),NOW+timedelta(seconds=1));req=self.store.request(None,'orderbook',r)
        self.store.book(TICKER,r,req,series);self.store.db.commit()

    def snapshot(self,generated=None):
        atomic_json(self.directory/'model-report.json',{'model_id':'test'})
        g=game();g['markets']=[]
        atomic_json(self.directory/'fair-prices.json',{'model_id':'test','generated_at':(generated or NOW).isoformat(),'games':[g],
            'quote_max_age_seconds':90,'estimate_max_age_seconds':180,'quantity':10,'slippage_buffer':.005,'min_edge':.03})

    def test_recorded_game_appears_with_prices_before_the_model_first_runs(self):
        self.record();s=public_snapshot(self.directory,NOW+timedelta(seconds=2))
        self.assertEqual(s['status'],'quotes_only');self.assertEqual(len(s['games']),1)
        self.assertEqual(s['coverage']['matched_source_games'],1);self.assertEqual(s['coverage']['games_with_fresh_quotes'],1)
        self.assertEqual(s['games'][0]['markets'][0]['quotes']['yes_ask'],.6)
        self.assertIsNone(s['games'][0]['prediction'])

    def test_live_score_and_prices_do_not_turn_a_pregame_estimate_into_live_edge(self):
        self.record('in',(NOW-timedelta(hours=1)).isoformat());self.snapshot()
        s=public_snapshot(self.directory,NOW+timedelta(seconds=2));g=s['games'][0]
        self.assertEqual(g['game_phase'],'started');self.assertEqual(g['live_score']['home_score'],42)
        self.assertEqual(g['markets'][0]['quotes']['yes_ask'],.6)
        self.assertIsNone(g['prediction']);self.assertTrue(all(c['net_edge'] is None for c in g['markets'][0]['candidates']))

    def test_current_market_prices_survive_an_expired_model_snapshot(self):
        self.record();self.snapshot(NOW-timedelta(minutes=10))
        g=public_snapshot(self.directory,NOW+timedelta(seconds=2))['games'][0]
        self.assertIsNone(g['prediction']);self.assertEqual(g['markets'][0]['quotes']['yes_ask'],.6)

    def test_new_fixture_is_added_to_model_schedule_without_mutating_historical_data(self):
        self.record();rows=load_live_games(self.directory,NOW+timedelta(seconds=2));dataset={'schedule':[],'games':[],'fingerprint':'frozen'}
        result=merge_schedule(dataset,rows,NOW+timedelta(seconds=2))
        self.assertEqual(len(result['schedule']),1);self.assertFalse(dataset['schedule']);self.assertEqual(result['fingerprint'],'frozen')
        self.assertTrue(result['schedule'][0]['live_model_scope_verified'])

    def test_empty_daily_feed_can_confirm_an_actual_zero_game_day(self):
        r=response({'events':[]});r['url']='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261123&limit=1000'
        self.store.request(None,'scoreboard',r);self.store.db.commit()
        s=public_snapshot(self.directory,NOW)
        self.assertEqual(s['coverage']['source_games'],0);self.assertTrue(s['coverage']['source_current'])

    def test_unreadable_recording_store_does_not_remove_the_saved_game_board(self):
        self.snapshot();bad=self.directory/'other';bad.mkdir()
        (bad/'fair-prices.json').write_bytes((self.directory/'fair-prices.json').read_bytes())
        (bad/'recordings.sqlite3').write_bytes(b'not a database')
        s=public_snapshot(bad,NOW)
        self.assertEqual(len(s['games']),1);self.assertTrue(s['quote_feed_errors'])

    def test_reschedule_updates_recorded_date_and_blocks_reuse_of_original_price_forecast(self):
        self.record();self.snapshot();row=dict(self.store.db.execute('SELECT * FROM games WHERE game_id=?',('1',)).fetchone())
        row.update(date='2026-11-24',tipoff=(NOW+timedelta(days=1)).isoformat(),season=None)
        self.store.game(row,NOW.isoformat());self.store.db.commit()
        recorded=self.store.db.execute('SELECT * FROM games WHERE game_id=?',('1',)).fetchone()
        self.assertEqual(recorded['date'],'2026-11-24');self.assertEqual(recorded['season'],2027)
        g=public_snapshot(self.directory,NOW+timedelta(seconds=2))['games'][0]
        self.assertIsNone(g['prediction']);self.assertIn('schedule_changed_since_prediction',g['blockers'])
        self.assertFalse(g['markets'],'An old-date contract cannot be relabeled as a rescheduled game')


if __name__=='__main__':unittest.main()
