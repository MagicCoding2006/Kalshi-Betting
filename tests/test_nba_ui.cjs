// Pricing and time boundaries in the browser view, using Node's built-in runner.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../desk/static/nba.js');
const now = Date.parse('2026-11-22T00:30:00Z');
const snapshot = {generated_at:new Date(now).toISOString(),estimate_max_age_seconds:900,quote_max_age_seconds:90,min_edge:.03,quantity:10};
function game() {
  return {game_id:'1',tipoff:new Date(now+3600000).toISOString(),status:'research_estimate',home:'DAL',away:'LAC',venue:'American Airlines Center',blockers:[],prediction:{home_win_probability:.6,away_win_probability:.4},markets:[
    {ticker:'DAL',yes_team:'DAL',quote_received_at:new Date(now).toISOString(),candidates:[{side:'yes',quantity:10,vwap:.52,all_in_cost:5.4,net_edge:.06,status:'research_edge',blockers:[]}]},
    {ticker:'LAC',yes_team:'LAC',quote_received_at:new Date(now).toISOString(),candidates:[{side:'no',quantity:10,vwap:.49,all_in_cost:5.1,net_edge:.09,status:'research_edge',blockers:[]}]}
  ]};
}
test('team prices compare direct YES with the opposite team’s NO after costs',()=>{
  const buy=ui.bestPurchase(game(),'DAL',snapshot,now);
  assert.equal(buy.market.yes_team,'LAC');assert.equal(buy.candidate.side,'no');assert.equal(buy.cost,.51);
  assert.equal(ui.bestEdge(game(),snapshot,now),.09);
  const html=ui.card(game(),snapshot,now);
  assert.ok(html.includes('YES on DAL'));assert.ok(html.includes('NO on LAC'));
  assert.ok(html.includes('+6.0 pp'));assert.ok(html.includes('+9.0 pp'));
  assert.ok(html.includes('Best route'));
});
test('an old cheaper quote cannot outrank a fresh executable quote',()=>{
  const g=game();g.markets[1].quote_received_at=new Date(now-91000).toISOString();
  const buy=ui.bestPurchase(g,'DAL',snapshot,now);
  assert.equal(buy.market.yes_team,'DAL');assert.equal(buy.valid,true);
});
test('expired quotes lose edge displays even when stored costs remain present',()=>{
  const g=game(),later=now+91000;
  assert.equal(ui.bestEdge(g,snapshot,later),null);
  assert.equal(ui.estimateReady(g,snapshot,later),true);
  const html=ui.card(g,snapshot,later);
  assert.ok(html.includes('Last quote · unavailable now'));assert.ok(!html.includes('+9.0 pp'));
});
test('estimate expiry, offline state and game start remove fair prices',()=>{
  assert.equal(ui.estimateReady(game(),snapshot,now+901000),false);
  assert.equal(ui.estimateReady(game(),{...snapshot,offline:true},now),false);
  const g=game();g.tipoff=new Date(now).toISOString();
  assert.equal(ui.estimateReady(g,snapshot,now),false);
  assert.equal(ui.filtered([g],snapshot,now).length,1,'Started matchups remain visible');
});
test('pending games do not become edges and search accepts team names',()=>{
  const g=game();
  assert.equal(ui.filtered([g],snapshot,now,{search:'clippers',filter:'edges'}).length,1);
  g.status='WAIT';g.prediction=null;g.blockers=['preceding_game_result_unavailable:0'];
  assert.equal(ui.filtered([g],snapshot,now,{filter:'edges'}).length,0);
  assert.ok(ui.card(g,snapshot,now).includes('Waiting for a preceding game result'));
});
test('provider text is escaped before rendering',()=>{
  const g=game();g.venue='<img src=x onerror=alert(1)>';
  const html=ui.card(g,snapshot,now);
  assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
});
test('a NO contract has value when the modeled YES team is unlikely to win',()=>{
  const g=game();g.markets[1].candidates[0].net_edge=.18;g.markets[1].candidates[0].all_in_cost=4.2;
  assert.equal(ui.bestPurchase(g,'DAL',snapshot,now).candidate.side,'no');
  assert.ok(ui.card(g,snapshot,now).includes('+18.0 pp'));
  g.markets[0].candidates.push({side:'no',quantity:10,vwap:.2,all_in_cost:2.2,net_edge:.18,status:'research_edge',blockers:[]});
  assert.equal(ui.bestPurchase(g,'LAC',snapshot,now).candidate.side,'no');
  assert.equal(ui.bestPurchase(g,'LAC',snapshot,now).market.yes_team,'DAL');
});
test('season table keeps per-game picks separate from paired team outcomes',()=>{
  const bucket={lower:.6,upper:.7,n:10,wins:7,losses:3,mean_probability:.65,win_rate:.7,calibration_gap:.05,small_sample:true};
  const r={status:'ready',evaluated_games:10,completed_games:12,unscored_games:2,through:'2026-11-20',model_cutoff:'2026-04-14',summary:{correct_picks:7,pick_accuracy:.7,mean_pick_probability:.65},pick_buckets:[bucket],team_buckets:[bucket],results:[]};
  assert.ok(ui.seasonMarkup(r).includes('One pick per game'));
  assert.ok(ui.seasonMarkup(r).includes('60–70%'));
  assert.ok(ui.seasonMarkup(r,'teams').includes('20 team forecasts from 10 games'));
  assert.ok(ui.seasonMarkup(r).includes('Small sample'));
});
test('archived and provisional forecasts display without executable edges',()=>{
  const g=game();g.tipoff=new Date(now-1000).toISOString();g.prediction=null;
  g.pregame_prediction={home_win_probability:.6,away_win_probability:.4};g.prediction_kind='saved_pregame';
  assert.equal(ui.displayPrediction(g,snapshot,now).kind,'saved_pregame');
  assert.equal(ui.bestEdge(g,snapshot,now),null);
  assert.ok(ui.card(g,snapshot,now).includes('60.0%'));
  g.tipoff=new Date(now+86400000).toISOString();g.pregame_prediction=null;g.preview_prediction={home_win_probability:.6,away_win_probability:.4};g.status='provisional';
  assert.equal(ui.displayPrediction(g,snapshot,now).kind,'provisional');
  assert.equal(ui.bestEdge(g,snapshot,now),null);
});
test('date slates retain every matchup including already-started games',()=>{
  const g=game();g.date='2026-11-21';g.tipoff=new Date(now-1000).toISOString();
  assert.equal(ui.filtered([g],{...snapshot,board_today:'2026-11-21'},now,{range:'today'}).length,1);
  assert.equal(ui.filtered([g],{...snapshot,board_today:'2026-11-21'},now,{range:'tomorrow'}).length,0);
});
test('availability scenarios show both team ranges and conservative edge labels',()=>{
  const g=game();g.availability_probability_range={home_min:.5,home_max:.7};
  const html=ui.card(g,snapshot,now);
  assert.ok(html.includes('50.0%–70.0% scenarios'));
  assert.ok(html.includes('30.0%–50.0% scenarios'));
  assert.ok(html.includes('scenario floor'));
});
test('projected lineups are not presented as confirmed and provider names are escaped',()=>{
  const g=game(),rotation={confirmed:false,injury_missing:false,players:[{name:'<img onerror=x>',availability:'questionable',baseline_minutes:25,projected_minutes:25,impact:null,starter:true}]};
  g.context={rotations:{home:rotation,away:rotation},referees:[],availability_observed_at:new Date(now).toISOString()};
  let html=ui.contextMarkup(g);
  assert.ok(html.includes('Projected lineup; not confirmed'));
  assert.ok(!html.includes('Confirmed starter'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('Unknown'));
  rotation.confirmed=true;html=ui.contextMarkup(g);
  assert.ok(html.includes('Confirmed starting five'));
  assert.ok(html.includes('Confirmed starter'));
});
test('factor report distinguishes validation selection, later evaluation and missing history',()=>{
  const trial={config:{name:'test',eligible:true},validation:{accuracy:.66,log_loss:.6,brier:.2}};
  const score={accuracy:.68,log_loss:.59};
  const report={status:'complete',completed_trials:43,probability_winner:trial,accuracy_winner:trial,validation_seasons:[2023,2024,2025],
    test:{season:2026,selected:score,baseline:{...score,log_loss:.61},accuracy_winner:score},trials:[trial],coefficients:[],
    groups:[{group:'availability',status:'no_historical_variation',selected_emphasis:1,coefficient_share:0,test_permutation_log_loss_increase:0,validation_removal_accuracy_change:0}],
    context_coverage:{player_games:10980,injury_report_games:0,confirmed_lineup_games:0}};
  const html=ui.factorMarkup(report);
  assert.ok(html.includes('43'));
  assert.ok(html.includes('test never selects the weights'));
  assert.ok(html.includes('not causal contributions'));
  assert.ok(html.includes('Dedicated coefficient needs history'));
  assert.ok(html.includes('Archived injury-report games: 0'));
});
test('recording view hides old quotes and escapes scoreboard text',()=>{
  const report={status:'recording',next_interval_seconds:10,counts:{scores:100,in_game_books:50},database_bytes:1048576,
    games:[{away:'NYK',home:'BOS',away_score:45,home_score:50,state:'in',period:2,display_clock:'<img>',score_observed_at:new Date(now).toISOString(),
      quotes:[{yes_team:'BOS',status:'active',quality:'ok',observed_at:new Date(now-31000).toISOString(),yes_bid:.6,yes_ask:.61}]}]};
  const html=ui.recordingMarkup(report,now);
  assert.ok(html.includes('Unavailable now'));assert.ok(!html.includes('60.0¢'));assert.ok(!html.includes('<img>'));assert.ok(html.includes('&lt;img&gt;'));
});
test('paper comparison does not score profitable exits before matching cohorts finish',()=>{
  const report={status:'collecting',worker_current:true,matched_closed_entries:0,entries:1,pending_entries:0,unfilled_entry_attempts:0,
    policies:[{policy:'target10_stop5',open_positions:0,closed_trades:1,matched_net_pnl:0,matched_win_rate:null,net_pnl:1,flagged_trades:0}]};
  const html=ui.paperMarkup(report);assert.ok(html.includes('Awaiting complete cohort'));assert.ok(!html.includes('$1.00'));
});
test('availability review blocks client-side edges even if a racing quote response still contains one',()=>{
 const g=game();g.entry_safety={blocked:true,messages:['Star <player> questionable'],official_checked_at:new Date(now).toISOString()};
 assert.equal(ui.bestEdge(g,snapshot,now),null);
 const html=ui.card(g,snapshot,now);assert.match(html,/Availability review/);assert.match(html,/New entry blocked/);
 assert.ok(!html.includes('Star <player>'));assert.match(html,/Received 0s ago · entry blocked/);
});
test('update health surfaces worker failures and recent status changes without HTML injection',()=>{
 const html=ui.healthMarkup({update_worker:{current:false,scan_error:'<img>'},availability_updates:{changes:[{observed_at:new Date(now).toISOString(),team:'OKC',name:'Player',before:'questionable',after:'out'}]}},now);
 assert.match(html,/Stopped \/ stale/);assert.match(html,/questionable → out/);assert.ok(!html.includes('<img>'));
});
test('live games and games without forecasts still show current buy asks and sell bids',()=>{
 for(const live of [false,true]){
  const g=game();g.prediction=null;g.status='WAIT';g.markets=[{ticker:'DAL',yes_team:'DAL',quote_received_at:new Date(now-3000).toISOString(),market_status:'active',quotes:{yes_ask:.56,yes_bid:.54,no_ask:.46,no_bid:.44},candidates:[]}];
  if(live){g.tipoff=new Date(now-1000).toISOString();g.game_phase='started';g.pregame_prediction={home_win_probability:.6,away_win_probability:.4};}
  const html=ui.card(g,snapshot,now);
  assert.match(html,/56.0¢/);assert.match(html,/Sell bid 54.0¢/);assert.match(html,/Received 3s ago/);assert.equal(ui.bestEdge(g,snapshot,now),null);
 }
});
test('a buy quote is the best ask rather than the quantity-weighted average',()=>{
 const g=game();g.markets[0].quotes={yes_ask:.5,yes_bid:.49};g.markets[0].candidates[0].vwap=.55;
 const buy=ui.bestPurchase(g,'DAL',snapshot,now);
 assert.equal(ui.card(g,snapshot,now).includes('10-contract avg 55.0¢'),true);
 assert.equal(ui.bestEdge(g,snapshot,now),.09);
 const direct=ui.card(g,snapshot,now);assert.match(direct,/50.0¢<small class="nba-buy-label"> buy ask/);
});
test('daily views retain games across an Eastern-calendar date and allow yesterday',()=>{
 const g=game();g.date='2026-11-21';const today={...game(),game_id:'2',date:'2026-11-22'};
 assert.deepEqual(ui.filtered([g,today],{...snapshot,board_today:'2026-11-22'},now,{range:'yesterday'}).map(g=>g.game_id),['1']);
});
test('finalized contracts display the actual settlement payout rather than a stale buy ask',()=>{
 const g=game();g.markets=[{...g.markets[0],market_status:'finalized',settlement_result:'no',quotes:{yes_bid:.01,yes_ask:.02,no_bid:.98,no_ask:.99}}];
 g.tipoff=new Date(now-1000).toISOString();g.prediction=null;g.game_phase='final';
 const html=ui.card(g,snapshot,now);assert.match(html,/Settled 0.0¢/);assert.match(html,/Settled 100.0¢/);assert.match(html,/Final settlement · market closed/);assert.equal(ui.bestEdge(g,snapshot,now),null);
});
test('red flags render a skip verdict with key absences, and the filter hides skipped games',()=>{
  const g=game();g.red_flags={verdict:'skip',model_team:'DAL',model_gap:.21,model_ask:.39,
    flags:[{level:'skip',code:'model_side_missing_key_players',team:'DAL',title:'Model likes DAL, but DAL is missing key players',detail:'Luka <b>out</b>'}],
    teams:{DAL:{out_minutes:35,out:[{name:'Luka',status:'out',minutes:35,role:'#1 impact on team',key:true,star:true}],uncertain:[]},LAC:{out_minutes:0,out:[],uncertain:[]}}};
  const html=ui.card(g,snapshot,now);
  assert.ok(html.includes('Skip this game'));
  assert.ok(html.includes('Model side: <strong>DAL</strong> · +21.0 pp vs Kalshi 39.0¢'));
  assert.ok(html.includes('nba-absence key out'));assert.ok(html.includes('★ Luka'));
  assert.ok(html.includes('Luka &lt;b&gt;out&lt;/b&gt;'),'flag text is escaped');
  const ok=game();ok.game_id='2';ok.red_flags={verdict:'clear',model_team:null,flags:[],teams:{}};
  assert.ok(ui.card(ok,snapshot,now).includes('No red flags'));
  assert.deepEqual(ui.filtered([g,ok,game()],snapshot,now,{filter:'unflagged'}).map(x=>x.game_id),['2']);
  assert.equal(ui.redFlagsMarkup(game()),'');
});
