"""Recording preserves observations, missing data and receipt-time joins."""
import copy
import json
import tempfile
import unittest
import gzip
from datetime import datetime,timezone,timedelta
from pathlib import Path

from NBA.recording import Recording,scoreboard_games,Collector,export_recordings
from NBA.service import recording_status

NOW=datetime(2026,11,22,2,tzinfo=timezone.utc)


def event(identifier='1',state='in'):
    return {'id':identifier,'date':'2026-11-22T01:00:00Z','season':{'year':2027,'type':2},
        'status':{'clock':100,'displayClock':'1:40','period':2,'type':{'state':state,'name':'STATUS_IN_PROGRESS'}},
        'competitions':[{'competitors':[{'homeAway':'away','score':'40','team':{'abbreviation':'NY'},'linescores':[]},
            {'homeAway':'home','score':'45','team':{'abbreviation':'BOS'},'linescores':[]}],
            'situation':{'lastPlay':{'team':{'abbreviation':'BOS'}}}}]}


def response(payload,at=NOW):
    return {'payload':payload,'url':'https://example.test/data','requested_at':(at-timedelta(seconds=1)).isoformat(),
            'received_at':at.isoformat(),'http_status':200,'error':None}


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Recording(self.temp.name)
    def tearDown(self):self.store.close();self.temp.cleanup()

    def add_game(self,at=NOW):
        r=response({'events':[event()]},at);req=self.store.request(None,'scoreboard',r)
        row=scoreboard_games(r['payload'])[0][0]
        return self.store.score(row,req,r['received_at'])

    def add_market(self):
        market={'ticker':'KXNBAGAME-26NOV21NYBOS-BOS','event_ticker':'KXNBAGAME-26NOV21NYBOS',
                'market_type':'binary','status':'active'}
        req=self.store.request(None,'market_discovery',response({'markets':[market]}))
        self.store.market(market,req,NOW.isoformat(),self.store.fixtures());return market

    def test_score_parser_preserves_absent_possession_and_fouls(self):
        rows,rejected=scoreboard_games({'events':[event()]})
        self.assertEqual(rejected,[]);r=rows[0]
        self.assertEqual((r['home'],r['away'],r['home_score'],r['away_score']),('BOS','NYK',45,40))
        self.assertEqual(r['date'],'2026-11-21')
        self.assertIsNone(r['possession']);self.assertIsNone(r['home_fouls']);self.assertIsNone(r['home_bonus'])

    def test_corrupt_score_is_excluded_and_payload_can_still_be_saved(self):
        bad=event();bad['competitions'][0]['competitors'][0]['score']='nan'
        rows,rejected=scoreboard_games({'events':[bad]})
        self.assertFalse(rows);self.assertEqual(len(rejected),1)
        self.store.request(None,'scoreboard',response({'events':[bad]}))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone()[0],1)

    def test_raw_payloads_deduplicate_without_losing_repeated_receipts(self):
        for _ in range(2):self.store.request(None,'scoreboard',response({'events':[event()]}))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM requests').fetchone()[0],2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM payloads').fetchone()[0],1)

    def test_book_joins_only_score_received_before_quote(self):
        earlier=self.add_game(NOW-timedelta(seconds=2));self.add_game(NOW+timedelta(seconds=1));market=self.add_market()
        r=response({'orderbook_fp':{'yes_dollars':[['.6','12']],'no_dollars':[['.39','12']]}})
        req=self.store.request(None,'orderbook',r);self.store.book(market['ticker'],r,req,None)
        b=self.store.db.execute('SELECT * FROM books').fetchone()
        self.assertEqual(b['score_id'],earlier);self.assertEqual(b['score_age_seconds'],2)
        self.assertAlmostEqual(b['yes_ask'],.61);self.assertEqual(b['quality'],'ok')

    def test_missing_depth_and_failed_requests_do_not_reuse_prior_quotes(self):
        self.add_game();market=self.add_market()
        for payload,error,quality in [({'orderbook_fp':{}},None,'empty'),(None,'timeout','request_failed')]:
            r=response(payload);r['error']=error
            req=self.store.request(None,'orderbook',r);self.store.book(market['ticker'],r,req,None)
            b=self.store.db.execute('SELECT * FROM books ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(b['quality'],quality);self.assertIsNone(b['yes_bid'])

    def test_stale_score_is_linked_but_flagged_for_replay(self):
        self.add_game(NOW-timedelta(minutes=2));market=self.add_market()
        r=response({'orderbook_fp':{'yes_dollars':[['.6','12']],'no_dollars':[['.39','12']]}})
        req=self.store.request(None,'orderbook',r);self.store.book(market['ticker'],r,req,None)
        self.assertEqual(self.store.db.execute('SELECT quality FROM books').fetchone()[0],'score_stale_or_missing')

    def test_settlement_is_explicit_not_inferred_from_a_final_score(self):
        self.add_game();market=self.add_market();market.update(status='settled',result='no',settlement_value_dollars='0.0000')
        req=self.store.request(None,'market_status',response({'market':market}))
        self.store.market(market,req,NOW.isoformat(),self.store.fixtures())
        row=self.store.db.execute('SELECT * FROM markets').fetchone()
        self.assertEqual(row['game_id'],'1');self.assertEqual(row['result'],'no');self.assertEqual(row['settlement_value'],0)

    def test_closed_market_can_be_joined_when_first_seen_after_the_game(self):
        self.add_game();market={'ticker':'KXNBAGAME-26NOV21NYBOS-BOS','event_ticker':'KXNBAGAME-26NOV21NYBOS','market_type':'binary','status':'settled','result':'yes'}
        req=self.store.request(None,'recent_settled',response({'markets':[market]}))
        self.store.market(market,req,NOW.isoformat(),self.store.fixtures())
        self.assertEqual(self.store.db.execute('SELECT game_id FROM markets').fetchone()[0],'1')

    def test_consistent_export_is_read_only_and_includes_source_payload(self):
        self.add_game();self.store.db.commit();p=Path(self.temp.name)/'export.jsonl.gz'
        export_recordings(self.temp.name,p)
        with gzip.open(p,'rt') as stream:rows=[json.loads(line) for line in stream]
        self.assertTrue(any(r['type']=='request' and r['payload'].get('events') for r in rows))
        with self.assertRaises(FileExistsError):export_recordings(self.temp.name,p)

    def test_midnight_live_game_keeps_fast_polling_and_uses_compatible_filters(self):
        from urllib.parse import urlparse,parse_qs
        now=datetime(2026,11,22,6,tzinfo=timezone.utc) # 1 AM ET, previous day's game still live.
        urls=[]
        def fetch(url):
            urls.append(url);q=parse_qs(urlparse(url).query)
            if 'scoreboard' in url:return response({'events':[event()] if q['dates']==['20261121'] else []},now)
            if '/series/' in url:return response({'series':{'fee_type':'quadratic','fee_multiplier':1}},now)
            if q.get('status')==['settled']:
                self.assertIn('min_settled_ts',q);self.assertNotIn('min_close_ts',q)
            return response({'markets':[]},now)
        collector=Collector(self.temp.name,fetch=fetch)
        try:
            result=collector.cycle(now)
            self.assertEqual(result['next_interval_seconds'],10)
            self.assertEqual(result['today_states'],{})
            self.assertFalse(result['errors'])
        finally:collector.store.close()

    def test_stopped_heartbeat_remains_stopped_even_if_recent(self):
        from NBA.data import atomic_json
        self.add_game();self.store.db.commit()
        atomic_json(Path(self.temp.name)/'collector-status.json',{'last_cycle_at':NOW.isoformat(),'next_interval_seconds':10,'running':False,'counts':{}})
        self.assertEqual(recording_status(self.temp.name,NOW)['status'],'stopped_or_stale')


if __name__=='__main__':unittest.main()
