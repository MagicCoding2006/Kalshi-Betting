const {test}=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../desk/static/nba-risk.js');
const now=Date.parse('2026-11-22T03:00:00Z');
function fixture(){
 const game={game_id:'1',home:'BOS',away:'NYK',tipoff:new Date(now+3600000).toISOString(),prediction:{home_win_probability:.7}};
 const a={game_id:'1',side:'no',yes_team:'NYK',outcome_team:'BOS',quantity:10,stake_fraction:.02,all_in_cost:20,net_if_win:5,
  quote_at:new Date(now).toISOString(),model_probability:.7,sizing_probability:.68,full_kelly_fraction:.3,expected_profit:2,
  exit_plan:{settlement_max_loss:20,stop_bid:.5,target_bid:.7,net_at_stop:-2,net_at_target:1,break_even_scalp_hit_rate:2/3}};
 const plan={generated_at:new Date(now).toISOString(),model_id:'test',settings:{kelly_fraction:.25},allocations:{1:a},contracts:[],positions:[]};
 return {game,a,plan};
}
test('a game shows portfolio percent, whole contracts, YES/NO route and both exit scenarios',()=>{
 const {game,plan}=fixture();const html=ui.gameMarkup(game,plan,{model_id:'test'},now);
 for(const text of ['2.00% of portfolio','NO on NYK','10 contracts','Manual stop bid','Manual target bid','Maximum settlement loss','not the model’s winner probability'])assert.ok(html.includes(text),text);
});
test('stale plans, stale quotes, model changes and game start remove actionable sizing',()=>{
 for(const kind of ['plan','quote','model','started']){
  const {game,a,plan}=fixture();let n=now;
  if(kind==='plan')n+=21000;if(kind==='quote')a.quote_at=new Date(now-91000).toISOString();if(kind==='started')game.tipoff=new Date(now-1000).toISOString();
  const html=ui.gameMarkup(game,plan,{model_id:kind==='model'?'other':'test'},n);
  assert.ok(!html.includes('2.00% of portfolio'));assert.ok(html.includes('0% new allocation'));
 }
});
test('partial journal exits allocate entry fees and preserve the remaining quantity',()=>{
 const ledger=[{id:'p',quantity:10,entry_price:.6,entry_fees:1,state:'open'}];
 const result=ui.closePosition(ledger,'p',4,.8,.2,new Date(now).toISOString());
 assert.equal(result.positions[0].quantity,6);assert.ok(Math.abs(result.positions[0].entry_fees-.6)<1e-9);
 assert.equal(result.positions[1].state,'closed');assert.ok(Math.abs(result.realizedPnl-.2)<1e-9);
 assert.equal(ledger[0].quantity,10,'Source ledger is not mutated');
});
test('provider contract names cannot inject HTML into a tracker alert',()=>{
 const p={id:'p',game_id:'1',ticker:'<img onerror=x>',side:'yes',quantity:1,entry_cost:1,max_loss:1,stop_bid:.4,target_bid:.7,status:'quote_unavailable'};
 const html=ui.positionMarkup(p,null,now);assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
});
test('scalp view separates assumed barrier-hit probability, capital and planned-stop risk',()=>{
 const {game,a,plan}=fixture();plan.settings={equity:1000,kelly_fraction:1,sizing_mode:'scalp'};
 Object.assign(a,{stake_fraction:.6,full_kelly_fraction:1.2,full_kelly_risk_fraction:.2,planned_stop_risk_fraction:.1,
  gap_stop_risk_fraction:.15,scalp_hit_rate:2/3,gap_kelly:{capital_fraction:.4},exit_plan:{...a.exit_plan,gap_bid:.48,net_at_gap_stop:-150}});
 const html=ui.gameMarkup(game,plan,{model_id:'test'},now);
 for(const t of ['60.00% capital committed','120.00% capital','Your assumed target hit rate','Gap stress','Full contract-loss exposure','Two-exit assumption'])assert.ok(html.includes(t),t);
 assert.ok(!html.includes('settlement basis'));
});
test('an unset scalp hit rate shows a payoff example without inventing a probability or stake',()=>{
 const {game,a,plan}=fixture();plan.settings={equity:1000,kelly_fraction:1,sizing_mode:'scalp'};plan.allocations={};plan.previews={1:{...a,scalp_hit_rate:null,preview_kelly:{capital_fraction:0}}};
 const html=ui.gameMarkup(game,plan,{model_id:'test'},now);
 assert.ok(html.includes('Scalp payoff preview'));assert.ok(html.includes('not a stake'));assert.ok(html.includes('not set'));
 assert.ok(!html.includes('capital committed'));
});
test('the individual stop calculator is available even without Kelly or a market',()=>{
 const {game}=fixture();const html=ui.gameMarkup(game,null,{},now);
 assert.ok(html.includes('data-stop-game="1"'));assert.ok(html.includes('Enter my price'));
});
test('individual stop results show net payoff checks and escape provider messages',()=>{
 const html=ui.stopResultMarkup({feasible:false,entry_price:.6,quantity:100,entry_fees:1.68,net_at_target:.72,reasons:['<img onerror=x>'],warnings:[]});
 for(const text of ['Setup does not meet','Two wins + one loss','Break-even sell bid','not a prediction of success','&lt;img'])assert.ok(html.includes(text),text);
 assert.ok(!html.includes('<img'));
});
test('saved individual plans stay separate from live sizing and tracked fills',()=>{
 const {game,plan}=fixture();const html=ui.gameMarkup(game,plan,{model_id:'test'},now,{result:{side:'no',ticker:'NBA-1',quantity:100,entry_price:.6,stop_bid:.59,target_bid:.7,net_at_stop:-5.38,net_at_target:5.85}});
 assert.ok(html.includes('Saved individual exit plan'));assert.ok(html.includes('Recalculate before use'));
 assert.ok(html.includes('Kelly allocations above use their own global settings'));
});
test('an availability change after the fill is visible even before a price stop fires',()=>{
 const html=ui.positionMarkup({id:'p',game_id:'1',ticker:'NBA-1',side:'yes',quantity:1,entry_cost:.6,max_loss:.6,stop_bid:.5,target_bid:.7,status:'watching',quote_at:new Date(now).toISOString(),availability_alerts:[{name:'Player <1>',before:'questionable',after:'out'}]},{generated_at:new Date(now).toISOString()},now);
 assert.match(html,/Availability changed since entry/);assert.match(html,/questionable → out/);assert.ok(!html.includes('Player <1>'));
});
test('a racing old allocation cannot be shown once the game has an availability block',()=>{
 const {game,plan}=fixture();game.entry_safety={blocked:true};
 const html=ui.gameMarkup(game,plan,{model_id:'test'},now);
 assert.ok(!html.includes('2.00% of portfolio'));assert.match(html,/0% new allocation/);
});
test('position action prioritizes exit rules and never promises recovery',()=>{
 const plan={generated_at:new Date(now).toISOString()},p={status:'watching',quote_at:new Date(now).toISOString(),stop_bid:.38,target_bid:.53};
 assert.match(ui.positionDecision({...p,status:'stop_reached'},plan,now).title,/Exit: stop reached at 38.0¢/);
 assert.match(ui.positionDecision({...p,time_due:true},plan,now).title,/deadline passed/);
 assert.match(ui.positionDecision({...p,status:'target_reached'},plan,now).title,/Take profit at 53.0¢/);
 assert.match(ui.positionDecision(p,plan,now).reason,/not a prediction/);
 assert.match(ui.positionDecision(p,plan,now+31000).title,/unavailable/);
});


test('entry prices accept decimal dollars, bare cents and percentage notation',()=>{
 for(const value of ['0.43','.43','43','43%','43¢','$0.43',' 43% '])assert.equal(ui.parseEntry(value),.43,value);
 for(const value of ['', '0','100%','100','abc','43xyz','-43','Infinity','0.43.1'])assert.throws(()=>ui.parseEntry(value),undefined,value);
 assert.equal(ui.parseEntry('0.5%'),.005);
});
test('quick purchase budgets fit whole contracts plus conservative estimated fees',()=>{
 const p=ui.estimatePurchase(5,.43);assert.equal(p.quantity,11);assert.equal(p.fees,.19);assert.ok(Math.abs(p.cost-4.92)<1e-9);assert.ok(p.cost<=5);
 for(const price of [.01,.1,.43,.5,.99])for(const amount of [1,5,43,1000]){
  const p=ui.estimatePurchase(amount,price);assert.ok(p.quantity*price+p.fees<=amount+1e-9);const next=p.quantity+1;
  assert.ok(next*price+Math.ceil((.07*next*price*(1-price)-1e-10)*100)/100>amount-1e-9);
 }
 for(const amount of [0,-1,NaN,.1])assert.throws(()=>ui.estimatePurchase(amount,.43));
});

test('per-game paper checks show trial evidence without treating it as a live probability',()=>{
 const {game,plan}=fixture();plan.risk_checks={'1':{status:'blocked',blockers:['In-game model not connected'],max_spread:.05,loss_cap:10,
  routes:[{side:'yes',yes_team:'<script>',ask:.5,bid:.47,spread:.03,ask_depth:20,bid_depth:15,status:'blocked',reason:'<unsafe>'}],
  trial:{status:'research_only',summary:{games:17,episodes:80,target_rate:.25,mean_net_pnl:-.54},teams:{BOS:{games:1,episodes:4,target_rate:.25}},
   evaluation:{status:'experimental_evaluation',test:{games:13,episodes:66},brier_score:.303,baseline_brier_score:.2,improves_baseline:false}}}};
 const html=ui.checksMarkup(game,plan,now);
 for(const text of ['new entry blocked','research only','80 replayed entries','did not beat','not this game’s probability','no team-specific model','&lt;script&gt;','&lt;unsafe&gt;'])assert.ok(html.includes(text),text);
 assert.ok(!html.includes('<script>'));assert.ok(!ui.checksMarkup(game,plan,now+21000).includes('80 replayed entries'));
});

test('moving exits keep original TP separate from active TP and SL without an invented probability',()=>{
 const html=ui.dynamicMarkup({dynamic_exits:{status:'active',original_target:.53,active_target:.52,active_stop:.4,reason:'<moving>',basis:'Heuristic only'}});
 for(const text of ['Original TP','53.0¢','Live TP','52.0¢','Live stop','40.0¢','1¢ per minute','&lt;moving&gt;'])assert.ok(html.includes(text),text);
 assert.ok(!html.includes('<moving>'));
});

test('every game has a visible risk panel and tracked exits only appear on the matching game',()=>{
 const {game,plan}=fixture();plan.positions=[{id:'p',game_id:'1',ticker:'NBA-1',side:'yes',quantity:10,entry_price:.43,entry_cost:4.49,
  status:'watching',quote_at:new Date(now).toISOString(),stop_bid:.4,target_bid:.52,initial_target_bid:.53,
  dynamic_exits:{status:'active',original_target:.53,active_target:.52,active_stop:.4,reason:'Smoothed',basis:'Heuristic'}}];
 const html=ui.gameMarkup(game,plan,{model_id:'test'},now);
 assert.ok(html.includes('Risk &amp; live exits'));assert.ok(html.includes('data-game-risk="1"'));assert.ok(html.includes('Original TP'));assert.ok(html.includes('Live TP'));assert.ok(html.includes('data-risk-close="p"'));
 assert.ok(html.indexOf('data-risk-close="p"')<html.indexOf('<details data-game-plan='),'Tracked exits are outside the collapsed sizing section');
 const other=ui.gameMarkup({...game,game_id:'2'},plan,{model_id:'test'},now);
 assert.ok(other.includes('Risk &amp; live exits'));assert.ok(other.includes('just your entry price and amount spent'));assert.ok(!other.includes('data-risk-close="p"'));
});

test('exit trial shows bid horizons, validation failures and partial exits without changing the active plan',()=>{
 const {game,plan}=fixture();plan.exit_trial={range_models:{180:{status:'experimental',split:{test_games:5},mean_absolute_error:.06,unchanged_bid_mae:.05,interval_coverage:.45,improves_baseline:false}},
  game_state:{status:'insufficient_games'},partial_exits:{later_games:{samples:20,games:5,policies:{sell_half_now:{mean_net_pnl:-.5,worst_decile_mean:-1}}}}};
 plan.exit_forecasts={'1':{status:'experimental',routes:{yes:{outcome_team:'<BOS>',side:'yes',yes_team:'BOS',bands:{180:{status:'experimental',low:.4,center:.5,high:.6}}}}}};
 const html=ui.exitResearchMarkup(game,plan,now);
 for(const text of ['3 min','40.0¢','60.0¢','Did not beat','Sell half now','Existing TP, SL and Kelly sizing are unchanged','&lt;BOS&gt;'])assert.ok(html.includes(text),text);
 assert.ok(!html.includes('<BOS>'));assert.ok(!ui.exitResearchMarkup(game,plan,now+21000).includes('40.0¢'));
});

test('position exit comparisons hide stale scenarios and escape source explanations',()=>{
 const {plan}=fixture();const p={id:'p',exit_options:{reason:'<unsafe>',options:[{action:'sell_half_now',contracts_now:5,remaining:5,net_proceeds_now:2,net_pnl_scenario:{low:-1,center:0,high:1}}]}};
 const html=ui.exitOptionsMarkup(p,plan,now);assert.ok(html.includes('Sell half now'));assert.ok(html.includes('&lt;unsafe&gt;'));
 assert.ok(!ui.exitOptionsMarkup(p,plan,now+21000).includes('$2.00'));
});

test('supplied video listing is matched by event, date and both teams',()=>{
 const game={game_id:'401909324',date:'2026-11-25',home:'GSW',away:'SAC'};
 assert.match(ui.videoSource(game).url,/sacramento-kings-vs-golden-state-warriors-3/);
 assert.equal(ui.videoSource({...game,date:'2026-11-26'}),null);
 assert.equal(ui.videoSource({...game,away:'BOS'}),null);assert.equal(ui.videoSource({...game,game_id:'other'}),null);
});

test('full-game winner value cannot override scalp exit status or availability review',()=>{
 const plan={generated_at:new Date(now).toISOString()},p={id:'p',status:'watching',quote_at:new Date(now).toISOString(),stop_bid:.38,target_bid:.53,
  live_model_exit:{action:'HOLD_CANDIDATE',fair_probability:.8,reason:'High winner probability'}};
 assert.match(ui.positionDecision(p,plan,now).title,/Within plan/);assert.ok(!ui.positionDecision(p,plan,now).title.includes('hold candidate'));
 assert.match(ui.positionDecision({...p,availability_alerts:[{}]},plan,now).title,/availability changed/);
});

test('opening research reports target execution, independent games and negative comparisons explicitly',()=>{
 const html=ui.openingMarkup({game_id:'g'},{opening_trial:{status:'research_only',coverage:{games:26,first_30_seconds_games:13},summary:{plans:100,unique_entry_observations:10,games:6},
  evaluation:{split:{test_games:4,calibration_games:3},families:{price_and_plan:{brier:.1,log_loss:.3},plus_score_momentum:{brier:.11,log_loss:.4,improves_price_baseline:false}},test_plan_summaries:{'moving_levels:0.05:180':{plans:20,games:4,successful_exit_rate:.1,mean_net_pnl:-.5}}}}});
 for(const value of ['not whether the team wins','only 13','4 games','Did not beat price baseline','moving TP/SL','touched bid alone does not count','Kelly/your original target are unchanged'])assert.ok(html.includes(value),value);
});
