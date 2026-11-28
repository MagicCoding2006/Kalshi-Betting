'use strict';
const NBAView = (() => {
  const names = {ATL:'Atlanta Hawks',BOS:'Boston Celtics',BKN:'Brooklyn Nets',CHA:'Charlotte Hornets',CHI:'Chicago Bulls',CLE:'Cleveland Cavaliers',DAL:'Dallas Mavericks',DEN:'Denver Nuggets',DET:'Detroit Pistons',GSW:'Golden State Warriors',HOU:'Houston Rockets',IND:'Indiana Pacers',LAC:'LA Clippers',LAL:'Los Angeles Lakers',MEM:'Memphis Grizzlies',MIA:'Miami Heat',MIL:'Milwaukee Bucks',MIN:'Minnesota Timberwolves',NOP:'New Orleans Pelicans',NYK:'New York Knicks',OKC:'Oklahoma City Thunder',ORL:'Orlando Magic',PHI:'Philadelphia 76ers',PHX:'Phoenix Suns',POR:'Portland Trail Blazers',SAC:'Sacramento Kings',SAS:'San Antonio Spurs',TOR:'Toronto Raptors',UTA:'Utah Jazz',WAS:'Washington Wizards'};
  const finite = Number.isFinite;
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const name = code => names[code] || code || 'Unknown team';
  const pct = value => finite(value) ? (value*100).toFixed(1)+'%' : '—';
  const cents = value => finite(value) ? (value*100).toFixed(1)+'¢' : '—';
  const pp = value => finite(value) ? (value>=0?'+':'')+(value*100).toFixed(1)+' pp' : '—';
  const money = value => finite(value) ? value.toLocaleString('en-US',{style:'currency',currency:'USD'}) : '—';
  const epoch = value => value ? Date.parse(value) : NaN;
  const age = (value,now) => (now-epoch(value))/1000;
  const future = (game,now) => epoch(game.tipoff)>now&&!['in','post'].includes(game.state)&&!['started','final','postponed','canceled'].includes(game.game_phase);
  function estimateReady(game,snapshot,now) {
    const seconds=age(snapshot.generated_at,now),p=game.prediction;
    return !snapshot.offline && future(game,now) && game.status!=='WAIT' && finite(p?.home_win_probability)
      && finite(p?.away_win_probability) && seconds>=-5 && seconds<=(snapshot.estimate_max_age_seconds??900);
  }
  function displayPrediction(game,snapshot,now) {
    if(estimateReady(game,snapshot,now))return {prediction:game.prediction,kind:'current_pregame'};
    if(!future(game,now)&&game.pregame_prediction)return {prediction:game.pregame_prediction,kind:game.prediction_kind||'saved_pregame'};
    const seconds=age(snapshot.generated_at,now);
    if(!snapshot.offline&&future(game,now)&&game.preview_prediction&&seconds>=-5&&seconds<=(snapshot.estimate_max_age_seconds??900))return {prediction:game.preview_prediction,kind:'provisional'};
    return null;
  }
  function localDay(value) {const d=new Date(value);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
  function purchases(game,code,snapshot,now) {
    const estimates=estimateReady(game,snapshot,now)&&!game.entry_safety?.blocked,options=[];
    for(const market of game.markets||[]){
      if(![game.home,game.away].includes(market.yes_team))continue;
      const seconds=age(market.quote_received_at,now),q=market.quotes||{};
      const candidates=market.quotes?['yes','no'].map(side=>(market.candidates||[]).find(c=>c.side===side)||{side,blockers:[]}):(market.candidates||[]);
      for(const candidate of candidates){
        const side=candidate.side;if(!['yes','no'].includes(side))continue;
        const outcome=side==='yes'?market.yes_team:(market.yes_team===game.home?game.away:game.home);if(outcome!==code)continue;
        const cost=finite(candidate.all_in_cost)&&candidate.quantity>0?candidate.all_in_cost/candidate.quantity:null;
        const price=finite(q[side+'_ask'])?q[side+'_ask']:finite(candidate.best_ask)?candidate.best_ask:candidate.vwap;
        const bid=finite(q[side+'_bid'])?q[side+'_bid']:candidate.best_bid;
        const quoteFresh=!snapshot.offline&&!market.error&&!q.crossed&&!(finite(bid)&&finite(price)&&bid>price)
          &&['active','open'].includes(market.market_status||'active')&&seconds>=-5&&seconds<=(snapshot.quote_max_age_seconds??90);
        const valid=estimates&&quoteFresh&&!(market.model_market_gap>(snapshot.model_market_review_threshold??.15))
          &&candidate.status!=='WAIT'&&!(candidate.blockers||[]).length&&finite(candidate.net_edge)&&finite(cost);
        options.push({candidate,market,valid,quoteFresh,cost,price,bid,seconds,askQuantity:q[side+'_ask_quantity'],label:`${side.toUpperCase()} on ${market.yes_team}`});
      }
    }
    return options.sort((a,b)=>Number(b.valid)-Number(a.valid)||Number(b.quoteFresh)-Number(a.quoteFresh)
      ||((a.valid?a.cost:(finite(a.price)?a.price:Infinity))-(b.valid?b.cost:(finite(b.price)?b.price:Infinity))));
  }
  function bestPurchase(game,code,snapshot,now) {return purchases(game,code,snapshot,now)[0]||null;}
  function bestEdge(game,snapshot,now) {
    const edges=[game.home,game.away].map(code=>bestPurchase(game,code,snapshot,now)).filter(x=>x?.valid).map(x=>x.candidate.net_edge);
    return edges.length?Math.max(...edges):null;
  }
  function reason(code) {
    if(String(code).startsWith('preceding_game_result_unavailable:'))return 'Waiting for a preceding game result';
    return ({game_started:'Game has started',game_started_or_not_scheduled:'Game has started or is not scheduled',estimate_snapshot_expired:'Fair estimate expired; waiting for a new scan',quote_expired:'Market quote expired',model_changed_or_unavailable:'Waiting for a scan from the current model',model_older_than_30_days:'Model needs retraining',dataset_older_than_36_hours:'Historical data needs an update',fewer_than_10_prior_games:'Not enough prior games',model_contains_future_labels:'Model cutoff is inconsistent',insufficient_executable_depth:'Insufficient quoted depth',unsupported_fee_schedule:'Fee schedule is unavailable',crossed_book:'Crossed order book',cost_unavailable:'Purchase cost unavailable'})[code]||String(code).replace(/_/g,' ');
  }
  function waitingReason(game,snapshot,now) {
    if(snapshot.offline)return 'Connection to the local server was lost';
    if(!future(game,now))return 'Game has started';
    if(age(snapshot.generated_at,now)>(snapshot.estimate_max_age_seconds??900))return 'Fair estimate expired; waiting for a new scan';
    return [...new Set((game.blockers||[]).map(reason))].join(' · ')||'Waiting for sufficient pregame data';
  }
  function gameBadge(game,snapshot,now) {
    const shown=displayPrediction(game,snapshot,now);
    if(['postponed','canceled'].includes(game.game_phase))return {label:game.game_phase==='postponed'?'Postponed':'Canceled',wait:true};
    if(!future(game,now))return {label:(game.game_phase==='final'?'Final':'Started')+(shown?' · pregame pick':''),wait:true};
    if(shown?.kind==='provisional')return {label:'Provisional estimate',wait:true};
    if(game.entry_safety?.blocked&&shown)return {label:'Availability review · entry blocked',wait:true};
    if((game.markets||[]).some(m=>m.model_market_gap>.15))return {label:'Model / market review',wait:true};
    if(!estimateReady(game,snapshot,now))return {label:[game.home,game.away].some(code=>purchases(game,code,snapshot,now).some(q=>q.quoteFresh))?'Quotes ready · model pending':future(game,now)?'Waiting for data':'Started',wait:true};
    const edge=bestEdge(game,snapshot,now);
    if(finite(edge))return {label:edge>=(snapshot.min_edge??.03)?'Research edge':'Estimate ready',wait:false};
    return {label:(game.markets||[]).length?'Quotes unavailable':'No market quote',wait:true};
  }
  function teamRow(game,code,snapshot,now){
    const shown=displayPrediction(game,snapshot,now),p=shown?shown.prediction[code===game.home?'home_win_probability':'away_win_probability']:null;
    const options=purchases(game,code,snapshot,now),best=options.find(option=>option.valid),opponent=code===game.home?game.away:game.home;
    const interval=game.availability_probability_range,range=interval&&shown?(code===game.home?[interval.home_min,interval.home_max]:[1-interval.home_max,1-interval.home_min]):null;
    return ['yes','no'].map((side,index)=>{
      const buy=options.find(option=>option.candidate.side===side),valid=buy?.valid,fresh=buy?.quoteFresh,isBest=valid&&buy===best;
      const price=finite(buy?.price)?cents(buy.price):'—',label=side==='yes'?`YES on ${code}`:`NO on ${opponent}`;
      const settled=buy&&['settled','finalized'].includes(buy.market.market_status),result=buy?.market?.settlement_result;
      const settlement=finite(buy?.market?.settlement_value)?buy.market.settlement_value:result==='yes'?1:result==='no'?0:null;
      const settledPrice=settled&&settlement!=null?cents(side==='yes'?settlement:1-settlement):null;
      const note=settled?'Final settlement · market closed':fresh?`Received ${Math.max(0,Math.round(buy.seconds))}s ago${game.entry_safety?.blocked?' · entry blocked':''}`:finite(buy?.price)?'Last quote · unavailable now':'No current buy ask';
      const sell=finite(buy?.bid)?cents(buy.bid):'—';
      const costs=fresh&&finite(buy?.cost)?`${buy.candidate.quantity}-contract avg ${cents(buy.candidate.vwap)} · ${cents(buy.cost)} incl. costs`:'';
      const fairLabel=shown?.kind==='provisional'?' provisional':shown&&shown.kind!=='current_pregame'?' pregame':' fair';
      const outcome=index===0?`<th scope="rowgroup" rowspan="2" class="nba-outcome">${esc(name(code))}<small>${code===game.home?'Home':'Away'}</small></th><td rowspan="2" class="nba-outcome"><span class="nba-price fair">${pct(p)}</span><span class="nba-price-note">${finite(p)?cents(p)+fairLabel:'Estimate pending'}</span>${range?`<span class="nba-price-note">${pct(range[0])}–${pct(range[1])} scenarios</span>`:''}</td>`:'';
      return `<tr class="nba-route-row ${isBest?'nba-best-route':''}">${outcome}<td><span class="nba-route-label">${esc(label)}</span><span class="nba-price ${fresh?'':'stale'}">${settledPrice?'Settled '+settledPrice:price+'<small class="nba-buy-label"> buy ask</small>'}</span><span class="nba-price-note ${fresh?'':'stale'}">${settled?'No active order book':'Sell bid '+sell}</span><span class="nba-price-note ${fresh?'':'stale'}">${note}</span>${costs?`<span class="nba-price-note">${costs}</span>`:''}</td><td><span class="nba-price edge ${valid&&buy.candidate.net_edge<0?'negative':''}">${valid?pp(buy.candidate.net_edge):'—'}</span><span class="nba-price-note ${isBest?'mint':''}">${isBest?'Best route':valid?'After costs':game.entry_safety?.blocked?'Entry blocked':'No current edge'}${valid&&range?' · scenario floor':''}</span></td></tr>`;
    }).join('');
  }
  function priceTable(game,snapshot,now) {
    const modelLabel=future(game,now)?'Pregame model / fair':'Saved pregame model / fair';
    return `<table class="nba-prices"><caption class="sr-only">${esc(name(game.away))} at ${esc(name(game.home))}: YES and NO routes for each team's win</caption><thead><tr><th scope="col">Team</th><th scope="col">${modelLabel}</th><th scope="col">Buy ask / sell bid</th><th scope="col">Net edge</th></tr></thead><tbody>${teamRow(game,game.away,snapshot,now)}</tbody><tbody>${teamRow(game,game.home,snapshot,now)}</tbody></table>`;
  }
  function liveModelMarkup(game,snapshot) {
    if(game.game_phase!=='started')return '';
    const live=game.live_model||{},report=snapshot.live_model_report||{},routes=live.routes||[];
    const probability=Number.isFinite(live.home_probability)?`<strong>${esc(game.home)} ${pct(live.home_probability)} · ${esc(game.away)} ${pct(1-live.home_probability)}</strong>`:'<strong>Probability unavailable</strong>';
    const action=live.action==='ENTRY CANDIDATE'?`WINNER-VALUE GAP · ${esc(live.outcome_team)} · ${pp(live.net_edge)} after costs`:'WAIT · '+esc(live.reason||'Collecting live inputs');
    const rows=routes.map(r=>`<tr><td>${esc(r.side.toUpperCase()+' on '+r.outcome_team)}</td><td>${pct(r.fair_probability)} fair · ${cents(r.best_ask)} ask / ${cents(r.best_bid)} bid</td><td>${pp(r.net_edge)}</td><td><strong>${esc(r.action==='ENTRY_CANDIDATE'?'WINNER-VALUE GAP':'WAIT')}</strong><small>${esc(r.reason)}</small></td></tr>`).join('');
    const validation=report.status==='evaluated'?`Out-of-time probability check: ${report.test_games} games in season ${report.test_season}; Brier ${Number(report.brier).toFixed(3)} vs ${Number(report.score_time_brier).toFixed(3)} score-only. This tests winner probabilities, not trading profit.`:'No completed chronological live-model evaluation is available.';
    return `<details data-game-winner="${esc(game.game_id)}" class="nba-availability-card nba-live-model-card ${live.action==='ENTRY CANDIDATE'?'has-edge':''}" aria-label="Full-game winner reference"><summary>Full-game winner reference · not scalp exit odds</summary><p>${probability}</p><p><strong>${action}</strong></p>${rows?`<div class="nba-check-table"><table><thead><tr><th>Route</th><th>Fair / executable quote</th><th>Net edge</th><th>Decision</th></tr></thead><tbody>${rows}</tbody></table></div>`:''}<small>Score ${Number.isFinite(live.score_age_seconds)?Math.max(0,Math.round(live.score_age_seconds))+'s':'—'} old · Kalshi quote ${Number.isFinite(live.quote_age_seconds)?Math.max(0,Math.round(live.quote_age_seconds))+'s':'—'} old · minimum +5 pp edge after costs. ${esc(validation)}</small><p class="nba-risk-caption">This estimates final-game winners, not the chance of hitting your exit. Winner-value gaps are not scalp entry or hold instructions. There is no automatic order placement. The displayed table below remains the saved pregame estimate.</p></details>`;
  }
  function card(game,snapshot,now,riskMarkup='') {
    const ready=estimateReady(game,snapshot,now),shown=displayPrediction(game,snapshot,now),badge=gameBadge(game,snapshot,now),edge=bestEdge(game,snapshot,now);
    const time=new Date(game.tipoff).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
    const live=game.live_score,scoreline=live&&['started','final'].includes(game.game_phase)?`<p class="nba-live-score">${esc(game.away)} ${live.away_score??'—'} – ${esc(game.home)} ${live.home_score??'—'} · ${game.game_phase==='final'?'Final':(live.period>4?'OT'+(live.period-4):'Q'+(live.period||'?'))+' '+esc(live.display_clock||'')} · ${age(live.score_observed_at,now)<=30?'recent score':'last recorded score'}</p>`:'';
    const context=`Book comparison: ${snapshot.quantity??10} contracts · Fees + ${cents(snapshot.slippage_buffer??.005)} buffer`;
    const bar=shown?`<div class="nba-model-bars"><span>${esc(game.away)}</span><meter min="0" max="1" value="${shown.prediction.away_win_probability}" aria-label="${esc(name(game.away))} pregame win probability">${pct(shown.prediction.away_win_probability)}</meter><span>${esc(game.home)}</span></div>`:'';
    const explanation=shown?.kind==='provisional'?'Latest-history preview with scheduled rest. Intervening results are unknown; executable edges remain unavailable.':shown&&!future(game,now)?(shown.kind==='pregame_replay'?'Pregame replay using only results known before tipoff.':shown.kind==='historical_test'?'Historical pregame test replay from the frozen checkpoint.':'Saved pregame forecast, retained after tipoff.')+' This is not an in-play estimate.':waitingReason(game,snapshot,now);
    return `<article data-game-id="${esc(game.game_id)}" class="nba-game ${finite(edge)&&edge>=(snapshot.min_edge??.03)?'has-edge':''}"><div class="nba-game-head"><span class="nba-tipoff">${esc(time)}${game.neutral?' · NEUTRAL':''}</span><span class="nba-badge ${badge.wait?'wait':''}">${esc(badge.label)}</span></div><div class="nba-matchup"><span class="nba-team-symbol" aria-hidden="true">${esc(game.away)}</span><div><h3 class="nba-matchup-title">${esc(game.away)} <span>at</span> ${esc(game.home)}</h3><p class="nba-venue">${esc(game.venue||'Venue not supplied')}</p></div><span class="nba-team-symbol home" aria-hidden="true">${esc(game.home)}</span></div>${scoreline}${liveModelMarkup(game,snapshot)}${priceTable(game,snapshot,now)}${availabilityMarkup(game)}${riskMarkup}<div class="nba-game-foot"><span class="nba-game-context">${context}</span><button class="nba-inspect" data-inspect="${esc(game.game_id)}" aria-label="Details for ${esc(name(game.away))} at ${esc(name(game.home))}">Details</button></div>${ready?'':`<p class="nba-availability">${esc(explanation)}</p>`}${bar}</article>`;
  }
  function availabilityMarkup(game){
    const safety=game.entry_safety,rotations=game.context?.rotations||{};
    if(!safety&&!Object.keys(rotations).length)return '';
    const lines=['away','home'].map(side=>{
      const players=(rotations[side]?.players||[]).filter(p=>p.baseline_minutes>0&&!['unreported','available','active','probable'].includes(p.availability)).slice(0,8);
      return players.length?`<p><strong>${esc(game[side])}</strong> · ${players.map(p=>esc(p.name)+' <strong>'+esc(p.availability)+'</strong>'+(p.availability==='out'?' · 0 projected minutes':'')).join('; ')}</p>`:'';
    }).join('');
    const link=safety?.official_url||game.context?.availability?.url;
    const source=link&&/^https:\/\/ak-static\.cms\.nba\.com\/referee\/injury\//.test(link)?`<a href="${esc(link)}" target="_blank" rel="noopener">Official report</a>`:'Official report unavailable';
    return `<div class="nba-availability-card">${safety?.blocked?'<strong class="nba-availability-block">New entry blocked</strong><p>'+safety.messages.slice(0,3).map(esc).join(' ')+'</p>':''}${lines}<small>${source}${safety?.official_checked_at?' · checked '+esc(new Date(safety.official_checked_at).toLocaleTimeString([],{hour:'numeric',minute:'2-digit',second:'2-digit'})):''}</small></div>`;
  }
  function healthMarkup(snapshot,now){
    if(!snapshot)return '<p class="nba-explanation">Checking the update worker…</p>';
    const w=snapshot.update_worker||{},u=snapshot.availability_updates||{},changes=(u.changes||[]).slice(-5).reverse();
    const time=value=>value?new Date(value).toLocaleTimeString([],{hour:'numeric',minute:'2-digit',second:'2-digit'}):'Unavailable';
    const notes=[w.scan_error,w.availability_error,...(u.errors||[])].filter(Boolean);
    return `<div class="nba-model-stats"><div><span>Background updater</span><strong>${w.current&&w.running!==false?'Running':'Stopped / stale'}</strong></div><div><span>Last prediction scan</span><strong>${esc(time(w.last_scan_at||snapshot.generated_at))}</strong></div><div><span>Last availability collection</span><strong>${esc(time(u.observed_at))}</strong></div><div><span>Target update cadence</span><strong>${w.availability_interval_seconds||30}s availability · ${w.scan_interval_seconds||30}s scans</strong></div></div><p class="nba-explanation">Injuries update independently of market scans and history downloads. Game cards show official report coverage, key absences and entry blocks. Local collection requires the computer to stay on and connected; checks cannot guarantee instant upstream reports. ${notes.length?'<strong>Update issue:</strong> '+notes.map(esc).join(' · '):''}</p>${changes.length?'<div class="nba-update-changes">'+changes.map(c=>'<p>'+esc(time(c.observed_at))+' · '+esc(c.team)+' '+esc(c.name)+': <strong>'+esc(c.before)+' → '+esc(c.after)+'</strong></p>').join('')+'</div>':''}`;
  }
  function filtered(games,snapshot,now,{filter='all',search='',sort='tipoff',range='all',date=''}={}) {
    const query=search.trim().toLowerCase();
    const today=snapshot.board_today||localDay(now),tomorrow=new Date(today+'T12:00:00'),yesterday=new Date(today+'T12:00:00');tomorrow.setDate(tomorrow.getDate()+1);yesterday.setDate(yesterday.getDate()-1);
    const week=new Date(today+'T12:00:00');week.setDate(week.getDate()+7);
    return games.filter(g=>{const day=g.date||localDay(g.tipoff);return range==='yesterday'?day===localDay(yesterday):range==='today'?day===today:range==='tomorrow'?day===localDay(tomorrow):range==='week'?day>=today&&day<localDay(week):range==='date'?day===date:true;})
      .filter(g=>!query||[g.home,g.away,name(g.home),name(g.away),g.venue||''].join(' ').toLowerCase().includes(query))
      .filter(g=>filter==='all'||filter==='estimates'&&!!displayPrediction(g,snapshot,now)||filter==='edges'&&finite(bestEdge(g,snapshot,now))&&bestEdge(g,snapshot,now)>=(snapshot.min_edge??.03))
      .sort((a,b)=>sort==='edge'?((bestEdge(b,snapshot,now)??-Infinity)-(bestEdge(a,snapshot,now)??-Infinity)||epoch(a.tipoff)-epoch(b.tipoff)):epoch(a.tipoff)-epoch(b.tipoff));
  }
  function contextMarkup(game) {
    const context=game.context;
    if(!context)return '<p class="nba-explanation">Availability context was not captured for this archived forecast.</p>';
    const sections=['away','home'].map(side=>{
      const rotation=context.rotations[side],code=game[side];
      const rows=rotation.players.filter(p=>p.baseline_minutes>0||p.availability!=='unreported').slice(0,18).map(p=>`<tr><th scope="row">${esc(p.name)}${p.starter?'<small>'+ (rotation.confirmed?'Confirmed starter':'Projected starter')+'</small>':''}</th><td>${esc(p.availability)}</td><td>${Number(p.projected_minutes).toFixed(1)}</td><td>${finite(p.impact)?p.impact.toFixed(2):'Unknown'}</td></tr>`).join('');
      const distance=game.features?.[side+'_travel_missing']?'Unavailable':Number(game.features?.[side+'_travel_km']||0).toFixed(0)+' km';
      return `<h3>${esc(name(code))} · ${rotation.confirmed?'Confirmed starting five':'Projected lineup; not confirmed'}</h3><p>Scheduled venue-city travel: ${distance} · Timezone shift: ${Number(game.features?.[side+'_timezone_shift']||0).toFixed(0)} hours. Injury report: ${rotation.injury_missing?'unavailable':'observed'}.</p><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Player</th><th scope="col">Reported status</th><th scope="col">Projected min</th><th scope="col">Impact proxy</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }).join('');
    const range=game.availability_probability_range;
    const uncertainty=range?`<p>Home win estimate across availability scenarios: <strong>${pct(range.home_min)}–${pct(range.home_max)}</strong>. This is a conditional scenario range, not a confidence interval. The baseline retains projected minutes for uncertain players; net edge uses the least favorable supplied scenario.</p>`:'';
    return `<h3>Availability, rotation and travel</h3><p>Observed ${context.availability_observed_at?esc(new Date(context.availability_observed_at).toLocaleString()):'without a current injury/lineup report'}. Projected minutes total 240 per team when sufficient history is available. Impact is a regularized prior-season box-score proxy; new players can have unknown impact.</p>${uncertainty}${sections}<h3>Referee assignment</h3><p>${context.referees.length?context.referees.map(esc).join(', '):'Not announced or unavailable for this game.'} Referee-history reconstruction is shown in the factor study as a separate research sensitivity, with no active weight in the trusted selection.</p>`;
  }
  function detail(game,snapshot,now,riskMarkup='') {
    const rows=[['Days off','rest',v=>v],['Offensive rating','ortg_10',v=>v.toFixed(1)],['Defensive rating','drtg_10',v=>v.toFixed(1)],['Effective FG','efg_10',pct],['Turnover rate','tov_rate_10',pct],['Offensive rebound share','orb_rate_10',pct],['Free-throw rate','ftr_10',pct],['3-point attempt share','three_rate_10',pct]];
    const profiles=rows.map(([label,key,format])=>`<tr><th scope="row">${label}</th>${['away','home'].map(side=>{const v=game.profiles?.[side]?.[key];return `<td>${finite(v)?esc(format(v)):'—'}</td>`;}).join('')}</tr>`).join('');
    const contracts=(game.markets||[]).map(m=>`<details class="nba-contract" data-contract="${esc(m.ticker)}"><summary>${esc(m.yes_team)} wins · Contract rules & costs</summary><p>${esc(m.ticker)}</p>${(m.candidates||[]).map(c=>{const option=purchases(game,c.side==='yes'?m.yes_team:(m.yes_team===game.home?game.away:game.home),snapshot,now).find(x=>x.market.ticker===m.ticker&&x.candidate.side===c.side);return `<p class="nba-contract-cost">${esc(c.side.toUpperCase())}: ${cents(c.vwap)} quoted average · ${money(c.fee_upper_estimate)} fee allowance for ${esc(c.quantity)} contracts${option?.valid?' · '+pp(c.net_edge)+' net edge':' · comparison unavailable'}</p>`;}).join('')}<p>${esc(m.rules_primary||'Rules not supplied.')}</p><p>${esc(m.rules_secondary||'')}</p>${m.error?`<p>Quote error: ${esc(m.error)}</p>`:''}</details>`).join('');
    return `<p>${esc(new Date(game.tipoff).toLocaleString([],{weekday:'long',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}))} · ${esc(game.venue||'Venue not supplied')}</p>${priceTable(game,snapshot,now)}${riskMarkup}<p>${estimateReady(game,snapshot,now)?'Fair prices use team/player history and the context available before tipoff. Unconfirmed lineups remain projected.':esc(waitingReason(game,snapshot,now))}</p><h3>Recent team profile</h3><p>Ten-game averages, shrunk toward a baseline. Ratings are points per 100 estimated possessions; days off refer to the scheduled matchup.</p><table class="nba-profile-table"><thead><tr><th scope="col">Metric</th><th scope="col">${esc(game.away)}</th><th scope="col">${esc(game.home)}</th></tr></thead><tbody>${profiles}</tbody></table>${contextMarkup(game)}<h3>Market contracts</h3>${contracts||'<p>No matching Kalshi contracts in this scan.</p>'}<p>Data scan: ${esc(new Date(snapshot.generated_at).toLocaleTimeString())}. Quotes shown as unavailable must be refreshed by the scanner before comparing an edge.</p>`;
  }
  function seasonMarkup(report,mode='picks',checkpoint='final') {
    if(!report||!['ready','empty'].includes(report.status))return `<p class="nba-explanation">${esc(report?.reason||'The season report could not be loaded. Refresh this view to retry.')}</p>`;
    const early=checkpoint!=='final',point=early?report.checkpoints?.[checkpoint]:null;
    const n=early?point?.evaluated_games:report.evaluated_games;
    if(!n)return '<p class="nba-explanation">No eligible historical results are available for this checkpoint yet. Missing quarter scores are excluded.</p>';
    const all=mode==='teams',data=early?point:report,rows=all?data.team_buckets:data.pick_buckets,s=report.summary;
    const label=checkpoint==='q1'?'after Q1':'at halftime';
    const stats=early?`<div><span>Games with quarter scores</span><strong>${n}</strong></div><div><span>Pick leading ${label}</span><strong>${point.leading} / ${n}</strong></div><div><span>Lead rate · all games</span><strong>${pct(point.lead_rate)}</strong></div><div><span>Tied at checkpoint</span><strong>${point.tied}</strong></div>`:`<div><span>Games checked</span><strong>${n}</strong></div><div><span>Correct winner picks</span><strong>${s.correct_picks} / ${n}</strong></div><div><span>Pick win rate</span><strong>${pct(s.pick_accuracy)}</strong></div><div><span>Mean pick probability</span><strong>${pct(s.mean_pick_probability)}</strong></div>`;
    const bucketRows=rows.map(b=>`<tr><th scope="row">${(b.lower*100).toFixed(0)}–${(b.upper*100).toFixed(0)}%${b.small_sample&&b.n?'<small>Small sample</small>':''}</th><td>${b.n}</td><td>${b.wins}</td><td>${b.losses}</td><td>${pct(b.mean_probability)}</td><td><strong>${pct(b.win_rate)}</strong></td><td class="${!early&&finite(b.calibration_gap)&&b.calibration_gap<0?'coral':''}">${early?b.ties:pp(b.calibration_gap)}</td></tr>`).join('');
    const recent=report.results.filter(r=>!early||r.checkpoints?.[checkpoint]).slice(-20).reverse().map(r=>{
      const at=r.checkpoints?.[checkpoint],status=early?at.pick_result:r.correct?'Correct':'Miss';
      const outcome=early?`${r.away} ${at.away_score} – ${r.home} ${at.home_score}`:r.winner;
      return `<tr><td>${esc(r.date)}</td><th scope="row">${esc(r.away)} at ${esc(r.home)}</th><td>${esc(r.pick)}<small>${pct(r.pick_probability)}</small></td><td>${esc(outcome)}</td><td class="${status==='Correct'||status==='leading'?'mint':status==='tied'?'':'coral'}">${esc(status)}</td></tr>`;
    }).join('');
    const countNote=all?`${n*2} team forecasts from ${n} games. Both teams are included; their outcomes are paired.`:'One pick per game: the team with the higher pregame win probability.';
    const interpretation=early?`Checking whether the pregame full-game pick was leading ${label}. Halftime sums Q1 and Q2. Ties are separate and remain in the lead-rate denominator. The probability bins still describe the final-winner forecast.`:'“Gap” is actual win rate minus the mean forecast.';
    return `<div class="nba-model-stats">${stats}</div><p class="nba-explanation nba-season-description">${countNote} ${interpretation} Bucket ranges include the lower bound; only the final bucket includes 100%.</p><div class="nba-calibration-wrap" role="region" aria-label="Season probability bucket results" tabindex="0"><table class="nba-calibration-table"><thead><tr><th scope="col">Pregame probability</th><th scope="col">Forecasts</th><th scope="col">${early?'Ahead':'Wins'}</th><th scope="col">${early?'Behind':'Losses'}</th><th scope="col">Mean forecast</th><th scope="col">${early?'Lead rate':'Actual win rate'}</th><th scope="col">${early?'Ties':'Gap'}</th></tr></thead><tbody>${bucketRows}</tbody></table></div><p class="nba-explanation nba-season-method">${n} of ${report.completed_games} available completed games scored, through ${esc(report.through)}. Historical replay from the earlier frozen test model; fitting, selection and calibration ended ${esc(report.model_cutoff)}. Live prices use a later refit. Historical market prices are unavailable, so leading at a checkpoint does not establish an exit profit.${early&&point.missing_quarter_scores?' '+point.missing_quarter_scores+' games have no verified quarter scores.':''}</p><details class="nba-season-results" id="nba-season-results"><summary>Recent evaluated games</summary><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Date</th><th scope="col">Game</th><th scope="col">Model pick</th><th scope="col">${early?'Checkpoint score':'Winner'}</th><th scope="col">Result</th></tr></thead><tbody>${recent}</tbody></table></div></details>`;
  }
  function inputsMarkup(catalog) {
    if(!catalog?.groups)return '<p class="nba-explanation">The feature catalog is unavailable.</p>';
    const rows=catalog.groups.map(g=>`<tr><th scope="row">${esc(g.label)}<small>${g.features.map(esc).join(', ')}</small></th><td>${esc(g.window)}</td><td>${esc(g.description)}</td></tr>`).join('');
    return `<p class="nba-explanation"><strong>${catalog.feature_count} candidate input columns.</strong> The factor study shows which families have active weight in the selected model. Rolling efficiency features use separate 10-game and 30-game windows, shrunk with five neutral prior games. Future provisional previews use the known schedule for rest and the latest available performance history.</p><div class="nba-calibration-wrap"><table class="nba-calibration-table nba-input-table"><thead><tr><th scope="col">Variable</th><th scope="col">Window</th><th scope="col">Definition</th></tr></thead><tbody>${rows}</tbody></table></div><p class="nba-explanation nba-season-method"><strong>Edge calculation:</strong> ${catalog.edge_inputs.map(esc).join('; ')}. <strong>Coverage:</strong> injury reports, announced lineups and referee assignments are collected when available. Their dedicated weights require timestamped historical evidence. Rotation, impact and scheduled travel are reconstructed past-only. Shot-location matchups and possession-based player impact remain future work. Quarter scores evaluate the historical pick; they are not pregame inputs.</p>`;
  }
  function factorMarkup(report) {
    if(!report||report.status==='not_run')return '<p class="nba-explanation">Run the factor study with <code>NBA/.venv/bin/python -m NBA optimize</code>. It compares prior validation seasons before evaluating the later test.</p>';
    if(report.status==='running')return `<p class="nba-explanation">Testing factor configurations: ${report.completed_trials} completed. Latest: ${esc(report.latest_trial||'preparing data')}. The active model remains available during the search.</p>`;
    if(report.status!=='complete')return `<p class="nba-explanation">${esc(report.error||'The factor study is unavailable.')}</p>`;
    const probability=report.probability_winner,accuracy=report.accuracy_winner,test=report.test;
    const picked=score=>finite(score.n)?Math.round(score.accuracy*score.n)+' / '+score.n+' ('+pct(score.accuracy)+')':pct(score.accuracy);
    const groups=report.groups.map(g=>`<tr><th scope="row">${esc(g.group.replace(/_/g,' '))}<small>${g.status==='no_historical_variation'?'Dedicated coefficient needs history':g.status==='requires_timestamped_assignment_history'?'Research-only assignment reconstruction':g.selected_emphasis===0?'Direct columns excluded':'Tested'}</small></th><td>${g.selected_emphasis.toFixed(1)}×</td><td>${pct(g.coefficient_share)}</td><td>${g.test_permutation_log_loss_increase.toFixed(4)}</td><td>${g.validation_removal_accuracy_change==null?'—':pp(g.validation_removal_accuracy_change)}</td></tr>`).join('');
    const trials=[...report.trials].sort((a,b)=>b.validation.accuracy-a.validation.accuracy||a.validation.log_loss-b.validation.log_loss).map(t=>`<tr><th scope="row">${esc(t.config.name)}${!t.config.eligible?'<small>Research-only; excluded from promotion</small>':''}</th><td>${pct(t.validation.accuracy)}</td><td>${t.validation.log_loss.toFixed(4)}</td><td>${t.validation.brier.toFixed(4)}</td></tr>`).join('');
    const coefficients=report.coefficients.slice(0,20).map(c=>`<tr><th scope="row">${esc(c.feature)}</th><td>${c.standardized_coefficient.toFixed(3)}</td><td>${pct(c.absolute_share)}</td></tr>`).join('');
    return `<div class="nba-model-stats"><div><span>Configurations tested</span><strong>${report.completed_trials}</strong></div><div><span>Best validation accuracy</span><strong>${pct(accuracy.validation.accuracy)}</strong></div><div><span>Best validation log loss</span><strong>${probability.validation.log_loss.toFixed(4)}</strong></div><div><span>Later test accuracy</span><strong>${pct(test.selected.accuracy)}</strong></div></div><p class="nba-explanation"><strong>Fair-price selection:</strong> ${esc(probability.config.name)}. <strong>Accuracy selection:</strong> ${esc(accuracy.config.name)}. Selection uses ${report.validation_seasons.map(s=>String(s-1)+'–'+String(s).slice(-2)).join(', ')}; the later ${test.season-1}–${String(test.season).slice(-2)} test never selects the weights. Baseline test log loss ${test.baseline.log_loss.toFixed(4)} → selected ${test.selected.log_loss.toFixed(4)}. Correct picks on that test: baseline ${picked(test.baseline)}, fair-price selection ${picked(test.selected)}, accuracy selection ${picked(test.accuracy_winner)}. These are historical diagnostics; current-season summaries were already reviewed.</p><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Factor family</th><th scope="col">Selected emphasis</th><th scope="col">Coefficient share</th><th scope="col">Test loss increase when shuffled</th><th scope="col">Validation accuracy change when removed</th></tr></thead><tbody>${groups}</tbody></table></div><p class="nba-explanation nba-season-method">Larger positive shuffle loss means this fitted model relied more on that family; a negative value means shuffling helped on this test. Coefficient shares are model diagnostics, not causal contributions. Emphasis changes the regularization after standardization. Removal trials compare with the expanded C=0.1 reference; a positive accuracy change means removal helped. Correlated information can remain in other families when a direct group is excluded.</p><p class="nba-explanation"><strong>Coverage:</strong> ${report.context_coverage.player_games.toLocaleString()} historical games with player rotations. Archived injury-report games: ${report.context_coverage.injury_report_games}; confirmed-lineup games: ${report.context_coverage.confirmed_lineup_games}. New availability changes the projected rotation, but dedicated injury/lineup coefficients need more pregame history. Referees are collected live; the final-record history assumption is isolated from the promoted model.</p><details class="nba-season-results"><summary>All configuration results</summary><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Configuration</th><th scope="col">Validation accuracy</th><th scope="col">Log loss</th><th scope="col">Brier</th></tr></thead><tbody>${trials}</tbody></table></div></details><details class="nba-season-results"><summary>Largest learned standardized coefficients</summary><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Feature</th><th scope="col">Coefficient</th><th scope="col">Absolute share</th></tr></thead><tbody>${coefficients}</tbody></table></div></details>`;
  }
  function recordingMarkup(report,now) {
    if(!report||report.status==='not_started')return '<p class="nba-explanation">Start the local recorder with <code>python3 -m NBA record</code>.</p>';
    const counts=report.counts||{},current=report.status==='recording';
    const rows=(report.games||[]).map(g=>{
      const scoreAge=age(g.score_observed_at,now),fresh=current&&scoreAge>=-5&&scoreAge<=30;
      const phase=g.state==='post'?'Final':g.state==='pre'?'Pregame':`${g.period>4?'OT'+(g.period-4):g.period?'Q'+g.period:'Period unavailable'} · ${g.display_clock||'Clock unavailable'}`;
      const quotes=(g.quotes||[]).map(q=>{
        const seconds=age(q.observed_at,now),ok=current&&q.quality==='ok'&&['active','open'].includes(q.status)&&seconds>=-5&&seconds<=30;
        return `<small>${esc(q.yes_team)} YES · ${ok?cents(q.yes_bid)+' bid / '+cents(q.yes_ask)+' ask':'Unavailable now'}</small>`;
      }).join('')||'<small>No recorded order book</small>';
      return `<tr><th scope="row">${esc(g.away)} at ${esc(g.home)}</th><td>${g.away_score??'—'} – ${g.home_score??'—'}</td><td>${esc(phase)}<small>${fresh?'Recently received':'Last recorded'} · ${finite(scoreAge)?Math.max(0,Math.round(scoreAge))+'s ago':'time unavailable'}</small></td><td>${quotes}</td></tr>`;
    }).join('');
    return `<div class="nba-model-stats"><div><span>Collector</span><strong>${current?'Recording':'Stopped / stale'}</strong></div><div><span>Score observations</span><strong>${Number(counts.scores||0).toLocaleString()}</strong></div><div><span>In-game books saved</span><strong>${Number(counts.in_game_books||0).toLocaleString()}</strong></div><div><span>Local database + journal</span><strong>${(Number(report.database_bytes||0)/1048576).toFixed(1)} MB</strong></div></div><p class="nba-explanation">Active polling every ${esc(report.next_interval_seconds)} seconds. Games remain recorded without a matching market. Source responses, receipt times, model forecasts, book depth and failures are preserved locally. Collection pauses while the computer sleeps.</p><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Game</th><th scope="col">Away – home</th><th scope="col">Last score state</th><th scope="col">Recorded market bid / ask</th></tr></thead><tbody>${rows}</tbody></table></div><p class="nba-explanation nba-season-method">These are sampled public observations, not guaranteed executable quotes or live model estimates. Receipt time does not establish exchange/feed freshness. Latest source errors: ${(report.errors||[]).length}.</p>`;
  }
  function paperMarkup(report) {
    if(!report||report.status==='not_started')return '<p class="nba-explanation">The paper study has not started.</p>';
    const labels={settlement_hold:'Hold to settlement',target10_stop5:'10¢ target / 5¢ stop',target5_stop5:'5¢ target / 5¢ stop',time20m_before_tipoff:'20-minute / before-tipoff exit',checkpoint_fair_value:'Updated fair value at checkpoints',partial_target10:'Sell half at +10¢; hold rest'};
    const matched=report.matched_closed_entries||0;
    const rows=(report.policies||[]).map(p=>`<tr><th scope="row">${esc(labels[p.policy]||p.policy)}</th><td>${p.open_positions}</td><td>${p.closed_trades}</td><td>${matched?money(p.matched_net_pnl):'Awaiting complete cohort'}</td><td>${pct(p.matched_win_rate)}</td><td>${p.flagged_trades}</td></tr>`).join('');
    return `<div class="nba-model-stats"><div><span>Qualifying shared entries</span><strong>${report.entries}</strong></div><div><span>Fully completed comparisons</span><strong>${matched}</strong></div><div><span>Pending / unfilled entries</span><strong>${report.pending_entries} / ${report.unfilled_entry_attempts}</strong></div><div><span>Paper worker</span><strong>${report.worker_current?'Running':'Stopped / stale'}</strong></div></div><p class="nba-explanation">Unfunded replay of recorded observations; no orders are sent. Entries use ten contracts and at least three percentage points of estimated edge after costs. Exits use later observed bids, fees and slippage; price gaps can exceed the stop. Net results below compare only entries for which every policy has finished.</p><div class="nba-calibration-wrap"><table class="nba-calibration-table"><thead><tr><th scope="col">Exit policy</th><th scope="col">Open</th><th scope="col">Closed</th><th scope="col">Matched net P&amp;L</th><th scope="col">Matched win rate</th><th scope="col">Flagged closes</th></tr></thead><tbody>${rows}</tbody></table></div><p class="nba-explanation nba-season-method">Targets/stops refer to contract-price movement before fees. Non-hold exit policies have a 90-minute deadline; the partial-exit remainder holds to settlement. Quarter fair value is experimental and available only during observed end-Q1 and halftime breaks. This early study does not establish profitability.</p>`;
  }
    return {names,name,esc,pct,cents,pp,money,age,future,localDay,estimateReady,displayPrediction,bestPurchase,bestEdge,reason,card,filtered,detail,seasonMarkup,inputsMarkup,contextMarkup,factorMarkup,recordingMarkup,paperMarkup,availabilityMarkup,healthMarkup,liveModelMarkup};
})();

if(typeof module!=='undefined'&&module.exports)module.exports=NBAView;
if(typeof document!=='undefined') {
  const $=id=>document.getElementById(id);
  const state={risk:null,riskSequence:0,riskSettings:{...NBARisk.defaults},riskPositions:[],riskHitRates:{},stopPlans:{},stopDraft:null,stopSequence:0,snapshot:null,model:null,catalog:null,optimization:null,backtest:null,backtestRead:0,seasonMode:'picks',checkpoint:'final',range:'today',date:NBAView.localDay(Date.now()),visibleLimit:40,received:0,filter:'all',search:'',sort:'tipoff',busy:false,error:null,selected:null,key:''};
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-game-video]');if(!button)return;
    const game=state.snapshot?.games?.find(g=>g.game_id===button.dataset.gameVideo),source=game&&NBARisk.videoSource(game);
    if(!source)return;
    const panel=$('nba-live-video'),mount=$('nba-video-mount');
    panel.hidden=false;$('nba-video-game').textContent=game.away+' at '+game.home+' · '+game.date;
    $('nba-video-source').href=source.url;
    if(mount.dataset.gameId!==game.game_id){
      mount.replaceChildren();mount.dataset.gameId=game.game_id;
      const frame=document.createElement('iframe');frame.className='nba-video-frame';frame.title=game.away+' at '+game.home+' external webpage preview';
      frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-presentation');
      frame.setAttribute('allow','autoplay; fullscreen; picture-in-picture');frame.referrerPolicy='no-referrer';frame.allowFullscreen=true;
      $('nba-video-status').textContent='Trying to load the supplied game page. Video playback is not verified; Cloudflare or the provider may block this embed.';
      frame.addEventListener('load',()=>{$('nba-video-status').textContent='External document loaded. This does not confirm video playback; a challenge or embedding restriction may still be displayed.';});
      frame.src=source.url;mount.append(frame);
    }
    panel.scrollIntoView?.({behavior:'smooth',block:'start'});
  });
  $('nba-video-close').addEventListener('click',()=>{$('nba-video-mount').replaceChildren();delete $('nba-video-mount').dataset.gameId;$('nba-live-video').hidden=true;$('nba-video-source').removeAttribute('href');});
  function now() {return state.snapshot?.served_at?Date.parse(state.snapshot.served_at)+(Date.now()-state.received):Date.now();}
  function view() {return {...state.snapshot,offline:!!state.error};}
  function elapsed(seconds) {if(!Number.isFinite(seconds))return '—';if(seconds<60)return Math.max(0,Math.floor(seconds))+'s ago';if(seconds<3600)return Math.floor(seconds/60)+'m ago';return Math.floor(seconds/3600)+'h ago';}
  function updateSummary() {
    const current=now(),s=view(),games=NBAView.filtered(s.games||[],s,current,{range:state.range,date:state.date});
    const estimates=games.filter(g=>NBAView.displayPrediction(g,s,current)).length;
    const edges=games.filter(g=>{const edge=NBAView.bestEdge(g,s,current);return Number.isFinite(edge)&&edge>=(s.min_edge??.03);}).length;
    $('nba-clock').textContent=new Date(current).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    if(!state.snapshot)return;
    const seconds=NBAView.age(s.generated_at,current);
    $('nba-count').textContent=games.length;$('nba-estimates').textContent=estimates;$('nba-edges').textContent=edges;
    $('nba-edge-policy').textContent='At least '+((s.min_edge??.03)*100).toFixed(0)+' pp net · hypotheses to review';
    $('nba-age').textContent=elapsed(seconds);
    $('nba-scan-time').textContent=s.generated_at?new Date(s.generated_at).toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'}):'No scan saved';
    const quoteStamp=s.coverage?(s.coverage.latest_any_quote_received_at||s.coverage.latest_quote_received_at):s.generated_at;
    const fresh=!!quoteStamp&&NBAView.age(quoteStamp,current)>=-5&&NBAView.age(quoteStamp,current)<=(s.quote_max_age_seconds??90)&&!state.error;
    $('nba-feed').classList.toggle('connected',fresh);
    $('nba-feed').innerHTML='<i></i>'+(state.error?'Connection lost':fresh?'Market feed current':'Waiting for fresh quotes');
    let message='Quotes reload from local recordings every five seconds. Buy asks, sell bids and receipt ages remain visible independently of pregame entry checks.';
    if(state.error)message='Cannot reach the NBA API: '+state.error+'. Showing saved context; price comparisons are unavailable.';
    else if(s.status==='not_ready')message=s.reason||'No scan is saved yet. Start the NBA watcher using the command below.';
    else if(seconds>(s.estimate_max_age_seconds??900))message='The pregame forecast has expired. Fresh recorded market quotes remain visible while the automatic model update is pending.';
    else if(!fresh)message='Market quotes have expired. Fair estimates remain available; net edges return with a new scanner update.';
    else if(s.quote_feed_errors?.length)message='The recorder is temporarily unavailable. Showing timestamped scan quotes where available.';
    else if(s.last_scan_error?.error)message='The latest scan failed: '+s.last_scan_error.error+'. The last saved scan is shown until it expires.';
    $('nba-notice').textContent=message;$('nba-notice').classList.toggle('informational',fresh&&!state.error);
  }
  function renderBoard() {
    const s=view(),current=now();updateSummary();
    if(!state.snapshot)return;
    const games=NBAView.filtered(s.games||[],s,current,state);
    const slate=NBAView.filtered(s.games||[],s,current,{range:state.range,date:state.date});
    const shown=games.slice(0,state.visibleLimit);
    const c=s.coverage||{},hidden=slate.length-games.length,paged=games.length-shown.length,discrepancies=(c.missing_source_game_ids?.length||0)+(c.mismatched_source_game_ids?.length||0)+(c.unexpected_board_game_ids?.length||0);
    $('nba-coverage').textContent=(c.source_games!=null?`Today: ${c.matched_source_games}/${c.source_games} feed games on the board${c.source_current?'':' · source check stale'}. ${c.games_with_fresh_quotes}/${c.board_games} have fresh Kalshi quotes. `:'Daily feed coverage is not verified yet. ')+`${discrepancies?'Source slate discrepancy detected. ':''}Selected slate: ${slate.length} games; ${hidden} hidden by search/prediction/edge filters${paged?'; '+paged+' more below':''}.`; 
    $('nba-visible-count').textContent=shown.length+' shown / '+slate.length+' in selected slate';
    $('nba-more').hidden=shown.length>=games.length;
    const expandedPlans=[...$('nba-games').querySelectorAll('[data-game-plan][open]')].map(e=>e.dataset.gamePlan);
    const expandedChecks=[...$('nba-games').querySelectorAll('[data-game-checks][open]')].map(e=>e.dataset.gameChecks);
    const expandedExitTrials=[...$('nba-games').querySelectorAll('[data-game-exit-trial][open]')].map(e=>e.dataset.gameExitTrial);
    const expandedPositionOptions=[...document.querySelectorAll('[data-position-options][open]')].map(e=>e.dataset.positionOptions);
    const expandedOpening=[...$('nba-games').querySelectorAll('[data-game-opening][open]')].map(e=>e.dataset.gameOpening);
    const expandedWinner=[...$('nba-games').querySelectorAll('[data-game-winner][open]')].map(e=>e.dataset.gameWinner);
    const focused=document.activeElement?.dataset?.inspect;
    if(!games.length) {
      const filtered=state.filter!=='all'||state.search.trim();
      $('nba-games').innerHTML=`<div class="nba-empty"><h3>${filtered?'No games match this view':s.status==='not_ready'?'The first scan is still needed':'No upcoming games in this snapshot'}</h3><p>${filtered?'Try all games or clear the team search.':'Keep the NBA watcher running to load the next slate.'}</p></div>`;
    } else if(state.sort==='edge') {
      $('nba-games').innerHTML='<div class="nba-game-grid">'+shown.map(g=>NBAView.card(g,s,current,NBARisk.gameMarkup(g,state.risk,s,current,state.stopPlans[g.game_id]))).join('')+'</div>';
    } else {
      const today=new Date(current).toDateString(),tomorrow=new Date(current);tomorrow.setDate(tomorrow.getDate()+1);
      const groups=new Map();for(const g of shown){const date=new Date(g.tipoff),key=date.toDateString();if(!groups.has(key))groups.set(key,{date,games:[]});groups.get(key).games.push(g);}
      $('nba-games').innerHTML=[...groups.entries()].map(([key,group])=>`<h3 class="nba-day-title">${key===today?'TODAY · ':key===tomorrow.toDateString()?'TOMORROW · ':''}${NBAView.esc(group.date.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}).toUpperCase())}</h3><div class="nba-game-grid">${group.games.map(g=>NBAView.card(g,s,current,NBARisk.gameMarkup(g,state.risk,s,current,state.stopPlans[g.game_id]))).join('')}</div>`).join('');
    }
    $('nba-games').querySelectorAll('[data-game-plan]').forEach(e=>e.open=expandedPlans.includes(e.dataset.gamePlan));
    $('nba-games').querySelectorAll('[data-game-checks]').forEach(e=>e.open=expandedChecks.includes(e.dataset.gameChecks));
    $('nba-games').querySelectorAll('[data-game-exit-trial]').forEach(e=>e.open=expandedExitTrials.includes(e.dataset.gameExitTrial));
    document.querySelectorAll('[data-position-options]').forEach(e=>e.open=expandedPositionOptions.includes(e.dataset.positionOptions));
    $('nba-games').querySelectorAll('[data-game-opening]').forEach(e=>e.open=expandedOpening.includes(e.dataset.gameOpening));
    $('nba-games').querySelectorAll('[data-game-winner]').forEach(e=>e.open=expandedWinner.includes(e.dataset.gameWinner));
    if(focused)[...document.querySelectorAll('[data-inspect]')].find(el=>el.dataset.inspect===focused)?.focus({preventScroll:true});
    renderDetail();renderRisk();state.key=freshnessKey();
  }
  function renderDetail() {
    if(!$('nba-detail').open||!state.selected)return;
    const game=state.snapshot?.games?.find(g=>g.game_id===state.selected);if(!game)return;
    const expanded=[...$('nba-detail-content').querySelectorAll('details[open]')].map(el=>el.dataset.contract),scroll=$('nba-detail').scrollTop;
    const focusedContract=document.activeElement?.closest('[data-contract]')?.dataset.contract;
    $('nba-detail-title').textContent=game.away+' at '+game.home;
    $('nba-detail-content').innerHTML=NBAView.detail(game,view(),now(),NBARisk.gameMarkup(game,state.risk,view(),now(),state.stopPlans[game.game_id]));
    $('nba-detail-content').querySelectorAll('[data-contract]').forEach(el=>el.open=expanded.includes(el.dataset.contract));
    if(focusedContract)[...$('nba-detail-content').querySelectorAll('[data-contract]')].find(el=>el.dataset.contract===focusedContract)?.querySelector('summary')?.focus({preventScroll:true});
    $('nba-detail').scrollTop=scroll;
  }
  function renderModel() {
    const r=state.model,m=r?.held_out?.model;
    if(!m){$('nba-model-content').innerHTML='<p class="nba-explanation">Model evidence is unavailable. The board will retry loading it.</p>';return;}
    const span=r.split?.test;
    $('nba-model-content').innerHTML=`<div class="nba-model-stats"><div><span>Held-out games</span><strong>${Number(m.n).toLocaleString()}</strong></div><div><span>Winner accuracy</span><strong>${NBAView.pct(m.accuracy)}</strong></div><div><span>Log loss · lower is better</span><strong>${Number(m.log_loss).toFixed(4)}</strong></div><div><span>Brier · lower is better</span><strong>${Number(m.brier).toFixed(4)}</strong></div></div><p class="nba-explanation">${span?`Test period: <strong>${NBAView.esc(span.from)} to ${NBAView.esc(span.through)}</strong>. `:''}${r.feature_names?.length||35} team, player and context features. The serving model is a later refit; these scores describe the frozen historical test. <strong>An advantage over market prices has not been established.</strong> Availability coverage and factor-study limits are shown below.</p>`;
  }
  function freshnessKey() {
    const s=view(),current=now();return JSON.stringify([NBARisk.current(state.risk,current),...(state.risk?.positions||[]).map(p=>!!p.quote_at&&current-Date.parse(p.quote_at)<=30000),...(s.games||[]).map(g=>[NBAView.future(g,current),NBAView.estimateReady(g,s,current),...(g.markets||[]).map(m=>{const seconds=NBAView.age(m.quote_received_at,current);return seconds>=-5&&seconds<=(s.quote_max_age_seconds??90);}),NBARisk.usable(state.risk?.allocations?.[g.game_id],g,state.risk,current)] )]);
  }
  function renderBacktest() {
    if(state.renderedBacktest===state.backtest&&state.renderedSeasonMode===state.seasonMode&&state.renderedCheckpoint===state.checkpoint)return;
    const expanded=$('nba-season-results')?.open;
    $('nba-season-content').innerHTML=NBAView.seasonMarkup(state.backtest,state.seasonMode,state.checkpoint);
    if($('nba-season-results'))$('nba-season-results').open=!!expanded;
    if(state.backtest?.season_label)$('nba-season-title').textContent=state.backtest.season_label+' season: probabilities vs results';
    state.renderedBacktest=state.backtest;state.renderedSeasonMode=state.seasonMode;state.renderedCheckpoint=state.checkpoint;
  }
  async function read(path) {const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),7000);try{const response=await fetch(path,{cache:'no-store',signal:controller.signal});const data=await response.json();if(!response.ok)throw new Error(data.error||'HTTP '+response.status);return data;}finally{clearTimeout(timer);}}
  const riskStorageKey='signaldesk.nba.risk.v1';
  const riskPercentFields=new Set(['max_game_fraction','max_total_fraction','probability_buffer','minimum_edge','stop_move','target_move','slippage','scalp_hit_rate','gap_move','max_loss_fraction','max_model_market_gap']);
  function riskFormValues() {
    const result={};for(const [key,value] of new FormData($('nba-risk-form')))result[key]=['sizing_mode','risk_loss_basis'].includes(key)?value:['scalp_hit_rate','max_loss_fraction'].includes(key)&&value===''?null:Number(value)/(riskPercentFields.has(key)?100:1);
    return result;
  }
  function fillRiskForm() {
    for(const [key,value] of Object.entries(state.riskSettings)){const field=$('nba-risk-form').elements.namedItem(key);if(field)field.value=typeof value==='string'?value:value==null?'':Number((value*(riskPercentFields.has(key)?100:1)).toFixed(4));}
  }
  function saveRisk() {
    try{localStorage.setItem(riskStorageKey,JSON.stringify({version:2,settings:state.riskSettings,positions:state.riskPositions,hit_rates:state.riskHitRates,stop_plans:state.stopPlans}));}
    catch(error){$('nba-risk-message').textContent='Browser storage is unavailable. Keep a separate record of your fills.';}
  }
  function restoreRisk() {
    try{
      const saved=JSON.parse(localStorage.getItem(riskStorageKey)||'null');
      if(saved){state.riskSettings={...NBARisk.defaults,...Object.fromEntries(Object.entries(saved.settings||{}).filter(([k])=>k in NBARisk.defaults))};state.riskPositions=Array.isArray(saved.positions)?saved.positions.filter(p=>p&&typeof p==='object'):[];state.riskHitRates=saved.hit_rates||{};state.stopPlans=saved.stop_plans&&typeof saved.stop_plans==='object'?saved.stop_plans:{};if(saved.version!==2){state.riskSettings={...state.riskSettings,sizing_mode:'scalp',scalp_hit_rate:null,kelly_fraction:1,max_game_fraction:1,max_total_fraction:1};saveRisk();}$('nba-risk-message').textContent='Planner restored. Choose your maximum loss per game and verify the scalp hit-rate assumption. Tracked positions are preserved.';}
    }catch(error){$('nba-risk-message').textContent='Saved planner values could not be read. Enter balances before using the example settings.';}
    for(const p of state.riskPositions){if(p.state!=='closed'&&p.dynamic_exits===undefined){p.dynamic_exits=true;p.dynamic_started_at=new Date().toISOString();p.initial_target_bid=p.target_bid;}}
    if(state.riskPositions.length)saveRisk();
    fillRiskForm();
  }
  async function refreshRisk() {
    const sequence=++state.riskSequence,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
    try{
      const response=await fetch('/api/nba/risk',{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',signal:controller.signal,
        body:JSON.stringify({settings:state.riskSettings,positions:state.riskPositions.filter(p=>p.state!=='closed'),hit_rates:state.riskHitRates})});
      const data=await response.json();if(!response.ok)throw Error(data.error||'Risk planner unavailable');
      if(sequence===state.riskSequence){
        state.risk=data;let changed=false;
        for(const alert of data.positions||[]){const p=state.riskPositions.find(p=>p.id===alert.id&&p.state!=='closed');const d=alert.dynamic_exits;
          if(p?.dynamic_exits&&d?.state&&JSON.stringify(p.dynamic_exit_state)!==JSON.stringify(d.state)){p.dynamic_exit_state=d.state;changed=true;}
        }
        if(changed)saveRisk();
      }
    }catch(error){if(sequence===state.riskSequence)state.risk={...state.risk,error:error.message||'Risk planner unavailable'};}
    finally{clearTimeout(timer);renderRisk();}
  }
  function renderRisk() {
    const changed=Object.keys(state.risk?.allocations||{}).some(id=>state.snapshot?.games?.find(g=>g.game_id===id)?.entry_safety?.blocked);
    $('nba-risk-summary').innerHTML=NBARisk.summaryMarkup(changed?{error:'Availability changed; refreshing the portfolio allocation.'}:state.risk,now());
    $('nba-update-health').innerHTML=NBAView.healthMarkup(state.snapshot,now());
    const openOptions=[...$('nba-risk-held').querySelectorAll('[data-position-options][open]')].map(e=>e.dataset.positionOptions);
    const held=state.risk?.positions||[];
    $('nba-risk-held').innerHTML=held.length?held.map(p=>NBARisk.positionMarkup({...p,...Object.fromEntries(Object.entries(state.riskPositions.find(saved=>saved.id===p.id)||{}).filter(([key])=>['quantity_estimated','reported_amount','exit_plan_warning'].includes(key)))},state.risk,now())).join(''):'<p class="nba-explanation">No purchases added yet. Choose the contract you bought, then enter just your amount spent and entry price.</p>';
    $('nba-risk-held').querySelectorAll('[data-position-options]').forEach(e=>e.open=openOptions.includes(e.dataset.positionOptions));
    const picker=$('nba-trade-game'),selected=picker.value;
    picker.innerHTML='<option value="">Choose the team and YES / NO side…</option>'+(state.snapshot?.games||[]).filter(g=>(state.risk?.contracts||[]).some(m=>m.game_id===g.game_id)).sort((a,b)=>Math.abs(Date.parse(a.tipoff)-now())-Math.abs(Date.parse(b.tipoff)-now())).flatMap(g=>(state.risk?.contracts||[]).filter(m=>m.game_id===g.game_id).flatMap(m=>['yes','no'].map(side=>`<option value="${NBARisk.esc(g.game_id+'|'+m.ticker+'|'+side)}">${NBARisk.esc(g.away+' at '+g.home)} · ${side.toUpperCase()} on ${NBARisk.esc(m.yes_team)} · ${NBARisk.esc(new Date(g.tipoff).toLocaleDateString())}</option>`))).join('');picker.value=selected;
  }
  function localDateTime(date) {return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);}
  function invalidateStop(){
    ++state.stopSequence;state.stopDraft=null;$('nba-stop-result').innerHTML='';
    $('nba-stop-save').disabled=true;$('nba-stop-fill').disabled=true;$('nba-stop-breakeven').hidden=true;
    $('nba-stop-calculate').disabled=false;$('nba-stop-calculate').textContent='Calculate SL';
  }
  function openStop(gameId,position=null){
    const game=state.snapshot?.games.find(g=>g.game_id===gameId);if(!game)return;
    const form=$('nba-stop-form'),saved=position?null:state.stopPlans[gameId],request=saved?.request;
    const a=state.risk?.allocations?.[gameId]||state.risk?.previews?.[gameId];
    const contracts=[...new Map([...(state.risk?.contracts||[]),...(game.markets||[]).map(m=>({...m,game_id:gameId}))].filter(m=>m.game_id===gameId).map(m=>[m.ticker,m])).values()];
    if(position&&!contracts.some(m=>m.ticker===position.ticker))contracts.push({ticker:position.ticker,yes_team:position.ticker});
    form.reset();invalidateStop();form.elements.game_id.value=gameId;form.elements.position_id.value=position?.id||'';
    form.elements.route.innerHTML=contracts.length?contracts.flatMap(m=>['yes','no'].map(side=>`<option value="${NBARisk.esc(m.ticker+'|'+side)}">${side.toUpperCase()} on ${NBARisk.esc(m.yes_team)} · ${NBARisk.esc(m.ticker)}</option>`)).join(''):`<option value="|yes">YES on ${NBARisk.esc(game.home)} · no linked market</option><option value="|no">NO on ${NBARisk.esc(game.home)} · no linked market</option>`;
    const source=position||request||a;
    if(source?.ticker)form.elements.route.value=source.ticker+'|'+source.side;
    if(!form.elements.route.value)form.elements.route.selectedIndex=0;
    form.elements.route.disabled=!!position;
    for(const key of ['entry_price','quantity','entry_kind','entry_fees'])form.elements[key].disabled=!!position;
    const price=position?.entry_price??request?.entry_price??a?.entry_vwap;
    form.elements.entry_price.value=Number.isFinite(price)?Number((price*100).toFixed(2)):'';
    form.elements.quantity.value=position?.quantity??request?.quantity??10;
    form.elements.target_bid.value=Number(((position?.target_bid??request?.target_bid??Math.min(.99,(price||.6)+state.riskSettings.target_move))*100).toFixed(2));
    form.elements.max_loss.value=request?.max_loss??'';form.elements.entry_fees.value=position?.entry_fees??request?.entry_fees??'';
    form.elements.entry_kind.value=position?'filled':request?.entry_kind||'planned';
    form.elements.min_reward_risk.value=request?.min_reward_risk??1;
    form.elements.min_stop_move.value=Number(((request?.min_stop_move??.01)*100).toFixed(2));
    form.elements.max_stop_move.value=Number(((request?.settings?.stop_move??state.riskSettings.stop_move)*100).toFixed(2));
    form.elements.hold_minutes.value=position?.hold_minutes??request?.settings?.hold_minutes??state.riskSettings.hold_minutes;
    $('nba-stop-save').textContent=position?'Apply to tracked position':'Save individual exit plan';
    $('nba-stop-fill').hidden=!!position;$('nba-stop-error').textContent='';
    $('nba-stop-game').textContent=game.away+' at '+game.home+(position?' · existing fill':' · individual payoff calculator');
    $('nba-stop-dialog').showModal();
  }
  $('nba-stop-form').addEventListener('input',event=>{
    if(event.target.name==='entry_price'){
      const form=event.currentTarget,entry=Number(event.target.value)/100;
      if(entry>0&&entry<1)form.elements.target_bid.value=Number((Math.min(.99,entry+state.riskSettings.target_move)*100).toFixed(2));
    }
    invalidateStop();$('nba-stop-error').textContent='';
  });
  $('nba-stop-form').addEventListener('change',()=>{invalidateStop();$('nba-stop-error').textContent='';});
  $('nba-stop-form').addEventListener('submit',async event=>{
    event.preventDefault();invalidateStop();const sequence=state.stopSequence,form=event.currentTarget,v=key=>form.elements[key].value;
    const [ticker,side]=v('route').split('|');
    const request={game_id:v('game_id'),ticker:ticker||null,side,entry_price:Number(v('entry_price'))/100,quantity:Number(v('quantity')),
      target_bid:Number(v('target_bid'))/100,max_loss:v('max_loss')===''?null:Number(v('max_loss')),entry_fees:v('entry_fees')===''?null:Number(v('entry_fees')),
      entry_kind:v('entry_kind'),min_reward_risk:Number(v('min_reward_risk')),min_stop_move:Number(v('min_stop_move'))/100,
      settings:{...state.riskSettings,stop_move:Number(v('max_stop_move'))/100,hold_minutes:Number(v('hold_minutes'))}};
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
    $('nba-stop-calculate').disabled=true;$('nba-stop-calculate').textContent='Calculating…';
    try{
      const response=await fetch('/api/nba/stop-plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),signal:controller.signal,cache:'no-store'});
      const result=await response.json();if(!response.ok)throw Error(result.error||'Stop calculator unavailable');
      if(sequence!==state.stopSequence)return;
      state.stopDraft={request,result,position_id:v('position_id')};$('nba-stop-result').innerHTML=NBARisk.stopResultMarkup(result);
      $('nba-stop-save').disabled=!result.feasible;$('nba-stop-fill').disabled=!result.feasible||!ticker;
      const p=state.riskPositions.find(p=>p.id===v('position_id')&&p.state!=='closed');
      if(p&&result.stop_bid!=null&&result.stop_bid<(state.risk?.positions?.find(row=>row.id===p.id)?.stop_bid??p.stop_bid)-1e-9){
        $('nba-stop-save').disabled=true;
        $('nba-stop-result').insertAdjacentHTML('beforeend','<p class="nba-risk-disclaimer">This calculated SL would loosen your existing stop. Keep the current protection or use “Adjust exit levels” for a deliberate change.</p>');
      }
      $('nba-stop-breakeven').hidden=!(p&&result.quote_fresh&&result.break_even_bid!=null&&result.break_even_bid<p.target_bid&&result.current_bid_vwap>=result.break_even_bid+result.minimum_stop_distance&&result.break_even_bid>(state.risk?.positions?.find(row=>row.id===p.id)?.stop_bid??p.stop_bid));
    }catch(error){if(sequence===state.stopSequence)$('nba-stop-error').textContent=error.message||'Stop calculator unavailable';}
    finally{clearTimeout(timer);if(sequence===state.stopSequence){$('nba-stop-calculate').disabled=false;$('nba-stop-calculate').textContent='Recalculate SL';}}
  });
  function reviewedStop(requireFeasible=true){
    const d=state.stopDraft;
    if(!d||requireFeasible&&!d.result.feasible)throw Error('Calculate a feasible exit plan first.');
    if(now()-Date.parse(d.result.generated_at)>20000||d.result.quote_fresh&&now()-Date.parse(d.result.quote_at)>30000)throw Error('Recalculate to refresh the exit plan before saving or applying it.');
    return d;
  }
  $('nba-stop-save').addEventListener('click',async()=>{
    try{
      const d=reviewedStop(),p=state.riskPositions.find(p=>p.id===d.position_id&&p.state!=='closed');
      if(d.position_id&&!p)throw Error('This position is no longer open.');
      if(p&&d.result.stop_bid<(state.risk?.positions?.find(row=>row.id===p.id)?.stop_bid??p.stop_bid)-1e-9)throw Error('This plan would loosen the existing stop. Use manual exit adjustment if that is your intended change.');
      if(p){p.stop_bid=d.result.stop_bid;p.target_bid=d.result.target_bid;p.hold_minutes=d.result.hold_minutes;p.stop_planner_snapshot=d.result;p.dynamic_exit_state=null;p.dynamic_started_at=new Date().toISOString();}
      else state.stopPlans[d.result.game_id]={request:d.request,result:d.result};
      saveRisk();$('nba-stop-dialog').close();state.risk=null;await refreshRisk();renderBoard();
      $('nba-risk-message').textContent=p?'Entry-specific exit levels applied to the local position tracker.':'Individual exit plan saved under the game. It does not change Kelly settings or create a position.';
    }catch(error){$('nba-stop-error').textContent=error.message;}
  });
  $('nba-stop-fill').addEventListener('click',()=>{
    try{
      const d=reviewedStop();if(!d.result.ticker)throw Error('Choose a linked contract to record an actual fill.');
      $('nba-stop-dialog').close();openFill(d.result.game_id,d.result.ticker+'|'+d.result.side);
      const form=$('nba-position-form');form.elements.manual_details.checked=true;form.elements.purchase_amount.required=false;$('nba-purchase-advanced').open=true;
      for(const [key,value] of Object.entries({quantity:d.result.quantity,entry_price:d.result.entry_price*100,entry_fees:d.result.entry_fees,stop_bid:d.result.stop_bid*100,target_bid:d.result.target_bid*100,hold_minutes:d.result.hold_minutes}))form.elements[key].value=Number(value.toFixed(4));
    }catch(error){$('nba-stop-error').textContent=error.message;}
  });
  $('nba-stop-breakeven').addEventListener('click',async()=>{
    try{
      const d=reviewedStop(false),p=state.riskPositions.find(p=>p.id===d.position_id&&p.state!=='closed'),r=d.result;
      if(!p||!r.quote_fresh||r.break_even_bid==null||r.current_bid_vwap<r.break_even_bid+r.minimum_stop_distance||r.break_even_bid>=p.target_bid||r.break_even_bid<=(state.risk?.positions?.find(row=>row.id===p.id)?.stop_bid??p.stop_bid))throw Error('Fresh bids do not support raising this stop to net break-even.');
      p.stop_bid=r.break_even_bid;p.stop_planner_snapshot=r;p.dynamic_exit_state=null;p.dynamic_started_at=new Date().toISOString();saveRisk();$('nba-stop-dialog').close();state.risk=null;await refreshRisk();renderBoard();
      $('nba-risk-message').textContent='Local stop raised to modeled net break-even. A price gap, changing fees or insufficient bids can still cause a loss.';
    }catch(error){$('nba-stop-error').textContent=error.message;}
  });
  $('nba-add-trade').addEventListener('click',()=>{const [id,ticker,side]=$('nba-trade-game').value.split('|');if(id&&ticker&&side)openFill(id,ticker+'|'+side);else $('nba-trade-game').focus();});
  $('nba-open-sizing').addEventListener('click',()=>{$('nba-sizing-details').open=true;});
  const purchaseForm=$('nba-position-form');
  let purchaseSession=0,purchaseSequence=0,purchaseTimer=null,purchaseReadyKey=null,purchaseReadyAt=0,purchaseWarning='';
  function purchaseKey(){return JSON.stringify(Array.from(purchaseForm.elements).filter(e=>e.name).map(e=>[e.name,e.type==='checkbox'?e.checked:e.value]));}
  function purchaseSummary(message=''){
    const f=purchaseForm;
    try{
      const price=NBARisk.parseEntry(f.elements.entry_price.value),q=Number(f.elements.quantity.value),fees=Number(f.elements.entry_fees.value);
      const amount=Number(f.elements.purchase_amount.value),cost=q*price+fees;
      $('nba-purchase-summary').textContent=(f.elements.manual_details.checked?'Exact details: ':'Estimated: ')+q+' contracts · fees '+NBARisk.money(fees)+' · cost '+NBARisk.money(cost)+(amount>0&&!f.elements.manual_details.checked?' · '+NBARisk.money(Math.max(0,amount-cost))+' unallocated from your amount':'')+'. Exit at '+f.elements.stop_bid.value+'¢ or lower; take profit at '+f.elements.target_bid.value+'¢ or higher; '+f.elements.hold_minutes.value+'-minute limit. '+(message||purchaseWarning||'Entry time defaults to now; change it in Advanced for an older purchase. Estimates can be corrected there.');
    }catch{$('nba-purchase-summary').textContent='Enter your amount spent and entry price. Everything else fills automatically.';}
  }
  function updatePurchaseQuantity(){
    purchaseReadyKey=null;purchaseWarning='';++purchaseSequence;clearTimeout(purchaseTimer);
    const f=purchaseForm;
    try{
      const price=NBARisk.parseEntry(f.elements.entry_price.value);
      if(!f.elements.manual_details.checked){
        const estimate=NBARisk.estimatePurchase(Number(f.elements.purchase_amount.value),price);
        f.elements.quantity.value=estimate.quantity;f.elements.entry_fees.value=estimate.fees;
        f.elements.stop_bid.value=Number((Math.max(.001,Math.min(.98,price-state.riskSettings.stop_move))*100).toFixed(1));
        f.elements.target_bid.value=Number((Math.min(.999,price+state.riskSettings.target_move)*100).toFixed(1));
        purchaseSummary('Calculating fee-aware exits…');purchaseTimer=setTimeout(()=>automaticPurchasePlan(),300);
      }else purchaseSummary();
      $('nba-position-error').textContent='';
    }catch(error){purchaseSummary();if(f.elements.entry_price.value&&f.elements.purchase_amount.value)$('nba-position-error').textContent=error.message;}
  }
  async function automaticPurchasePlan(){
    const f=purchaseForm;if(f.elements.manual_details.checked){purchaseSummary();return;}
    const price=NBARisk.parseEntry(f.elements.entry_price.value),estimate=NBARisk.estimatePurchase(Number(f.elements.purchase_amount.value),price);
    const key=purchaseKey();if(key===purchaseReadyKey&&Date.now()-purchaseReadyAt<20000)return;
    const sequence=++purchaseSequence,[ticker,side]=f.elements.route.value.split('|');
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
    let warning='';
    try{
      const response=await fetch('/api/nba/stop-plan',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({game_id:f.elements.game_id.value,ticker,side,entry_kind:'filled',entry_price:price,quantity:estimate.quantity,entry_fees:estimate.fees,target_bid:Number(f.elements.target_bid.value)/100,settings:state.riskSettings})});
      const plan=await response.json();if(!response.ok)throw Error(plan.error||'Exit calculation unavailable');
      if(sequence!==purchaseSequence||key!==purchaseKey())return;
      if(plan.feasible&&Number.isFinite(plan.stop_bid))f.elements.stop_bid.value=Number((plan.stop_bid*100).toFixed(1));
      else warning='Exit plan needs review: '+((plan.reasons||[]).join(' ')||'No fee-valid stop was found.')+' Your default stop distance is used; it is not a validated scalp.';
    }catch(error){if(sequence!==purchaseSequence||key!==purchaseKey())return;warning='Automatic exit calculation unavailable. Your default stop and target distances are used; review the plan.';}
    finally{clearTimeout(timeout);}
    if(sequence!==purchaseSequence)return;
    purchaseWarning=warning;purchaseReadyKey=purchaseKey();purchaseReadyAt=Date.now();purchaseSummary(warning);
  }
  purchaseForm.elements.purchase_amount.addEventListener('input',updatePurchaseQuantity);
  purchaseForm.elements.entry_price.addEventListener('input',updatePurchaseQuantity);
  purchaseForm.elements.manual_details.addEventListener('change',()=>{purchaseForm.elements.purchase_amount.required=!purchaseForm.elements.manual_details.checked;updatePurchaseQuantity();});
  for(const key of ['quantity','entry_fees','stop_bid','target_bid','hold_minutes'])purchaseForm.elements[key].addEventListener('input',()=>{purchaseForm.elements.manual_details.checked=true;purchaseForm.elements.purchase_amount.required=false;updatePurchaseQuantity();});
  $('nba-position-suggest').addEventListener('click',async()=>{purchaseForm.elements.manual_details.checked=false;purchaseForm.elements.purchase_amount.required=true;updatePurchaseQuantity();clearTimeout(purchaseTimer);try{await automaticPurchasePlan();}catch(error){$('nba-position-error').textContent=error.message;}});
  function openFill(gameId,route=null) {
    const game=state.snapshot?.games.find(g=>g.game_id===gameId);if(!game)return;
    const form=purchaseForm,contracts=(state.risk?.contracts||[]).filter(m=>m.game_id===gameId);
    ++purchaseSession;++purchaseSequence;clearTimeout(purchaseTimer);purchaseReadyKey=null;purchaseWarning='';
    form.reset();form.elements.game_id.value=gameId;form.elements.purchase_amount.required=true;$('nba-purchase-advanced').open=false;
    form.elements.route.innerHTML=contracts.flatMap(m=>['yes','no'].map(side=>`<option value="${NBARisk.esc(m.ticker+'|'+side)}">${side.toUpperCase()} on ${NBARisk.esc(m.yes_team)} · ${NBARisk.esc(m.ticker)}</option>`)).join('');
    if(route)form.elements.route.value=route;
    if(!form.elements.route.value){$('nba-purchase-advanced').open=true;}
    form.elements.quantity.value=1;form.elements.entry_price.value='';
    form.elements.entry_fees.value=0;form.elements.opened_at.value=localDateTime(new Date());
    form.elements.stop_bid.value=45;form.elements.target_bid.value=60;
    form.elements.hold_minutes.value=state.riskSettings.hold_minutes;$('nba-position-error').textContent='';
    $('nba-purchase-route').textContent=game.away+' at '+game.home+' · '+(form.elements.route.selectedOptions[0]?.textContent||'Choose your contract in Advanced');
    purchaseSummary();$('nba-position-save').disabled=false;$('nba-position-dialog').showModal();form.elements.purchase_amount.focus();
  }
  function openExit(id) {
    const p=state.riskPositions.find(p=>p.id===id&&p.state!=='closed');if(!p)return;
    const alert=state.risk?.positions?.find(p=>p.id===id),form=$('nba-exit-form');form.reset();form.elements.id.value=id;
    form.elements.quantity.value=p.quantity;form.elements.quantity.max=p.quantity;
    const price=alert?.status==='settled'?alert.settlement_price:alert?.bid_vwap;
    form.elements.exit_price.value=Number.isFinite(price)?Number((price*100).toFixed(2)):'';
    form.elements.exit_fees.value=alert?.status==='settled'?0:alert?.exit_fee_estimate??0;
    $('nba-exit-error').textContent='';$('nba-exit-dialog').showModal();
  }
  document.addEventListener('click',event=>{
    const track=event.target.closest('[data-risk-track]'),close=event.target.closest('[data-risk-close]'),dismiss=event.target.closest('[data-risk-dismiss]'),adjust=event.target.closest('[data-risk-adjust]'),hit=event.target.closest('[data-risk-hit]');
    const stopGame=event.target.closest('[data-stop-game]'),stopPosition=event.target.closest('[data-stop-position]');if(stopGame)openStop(stopGame.dataset.stopGame);if(stopPosition){const p=state.riskPositions.find(p=>p.id===stopPosition.dataset.stopPosition&&p.state!=='closed');if(p)openStop(p.game_id,p);}
    if(track)openFill(track.dataset.riskTrack,track.dataset.fillRoute);if(close)openExit(close.dataset.riskClose);if(dismiss)$(dismiss.dataset.riskDismiss).close();if(adjust)openAdjust(adjust.dataset.riskAdjust);if(hit)openHit(hit.dataset.riskHit);
  });
  document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-dynamic-toggle]');if(!button)return;
    const p=state.riskPositions.find(p=>p.id===button.dataset.dynamicToggle&&p.state!=='closed');if(!p)return;
    const latest=state.risk?.positions?.find(row=>row.id===p.id)?.dynamic_exits;
    if(p.dynamic_exits&&latest){p.stop_bid=Math.max(p.stop_bid,latest.active_stop);p.target_bid=latest.active_target;}
    p.dynamic_exits=!p.dynamic_exits;p.dynamic_started_at=new Date().toISOString();delete p.dynamic_exit_state;
    p.initial_target_bid??=p.target_bid;saveRisk();state.risk=null;await refreshRisk();renderBoard();
  });
  function openAdjust(id) {
    const p=state.riskPositions.find(p=>p.id===id&&p.state!=='closed');if(!p)return;
    const current=state.risk?.positions?.find(row=>row.id===id)||p,form=$('nba-adjust-form');form.elements.id.value=id;form.elements.stop_bid.value=Number((current.stop_bid*100).toFixed(1));
    form.elements.target_bid.value=Number((current.target_bid*100).toFixed(1));form.elements.hold_minutes.value=p.hold_minutes;
    $('nba-adjust-error').textContent='';$('nba-adjust-dialog').showModal();
  }
  $('nba-adjust-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target;
    try{
      const stop=Number(form.elements.stop_bid.value)/100,target=Number(form.elements.target_bid.value)/100,minutes=Number(form.elements.hold_minutes.value);
      if(!Number.isFinite(stop)||!Number.isFinite(target)||stop<=0||target>=1||stop>=target||!Number.isFinite(minutes)||minutes<1||minutes>240)throw Error('Check stop, target and holding time.');
      state.riskPositions=state.riskPositions.map(p=>p.id===form.elements.id.value?{...p,stop_bid:stop,target_bid:target,hold_minutes:minutes,dynamic_exit_state:null,dynamic_started_at:new Date().toISOString()}:p);
      saveRisk();$('nba-adjust-dialog').close();state.risk=null;await refreshRisk();renderBoard();
      $('nba-risk-message').textContent='Manual exit levels updated locally. No exchange order was changed.';
    }catch(error){$('nba-adjust-error').textContent=error.message;}
  });
  function openHit(gameId) {
    const game=state.snapshot?.games.find(g=>g.game_id===gameId),form=$('nba-hit-form');form.elements.game_id.value=gameId;
    const rate=state.riskHitRates[gameId]??state.riskSettings.scalp_hit_rate;form.elements.hit_rate.value=rate==null?'':Number((rate*100).toFixed(2));
    $('nba-hit-game').textContent=game?game.away+' at '+game.home:gameId;$('nba-hit-error').textContent='';$('nba-hit-dialog').showModal();
  }
  $('nba-hit-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target,rate=Number(form.elements.hit_rate.value)/100;
    if(form.elements.hit_rate.value===''||!Number.isFinite(rate)||rate<0||rate>1){$('nba-hit-error').textContent='Enter a probability from 0 to 100%.';return;}
    state.riskHitRates[form.elements.game_id.value]=rate;saveRisk();$('nba-hit-dialog').close();state.risk=null;await refreshRisk();renderBoard();
    $('nba-risk-message').textContent='Game-specific scalp assumption saved. It is your estimate, not a fitted model probability.';
  });
  $('nba-hit-global').addEventListener('click',async()=>{delete state.riskHitRates[$('nba-hit-form').elements.game_id.value];saveRisk();$('nba-hit-dialog').close();state.risk=null;await refreshRisk();renderBoard();});
  $('nba-scalp-example').addEventListener('click',()=>{$('nba-risk-form').elements.scalp_hit_rate.value=66.67;$('nba-risk-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});
  $('nba-risk-form').addEventListener('submit',async event=>{
    event.preventDefault();state.riskSettings=riskFormValues();state.risk=null;saveRisk();renderBoard();
    $('nba-risk-message').textContent='Recalculating allocations across every eligible upcoming game…';await refreshRisk();renderBoard();
    $('nba-risk-message').textContent=state.risk?.error||'Kelly calculated from your selected assumptions. Standalone Kelly and shared cash allocation are separate; verify the stop-fill and hit-rate assumptions before your final call.';
  });
  $('nba-upcoming').addEventListener('click',()=>{state.range='week';state.visibleLimit=40;$('nba-slate').value='week';renderBoard();$('nba-board-title').scrollIntoView?.({behavior:'smooth',block:'start'});});
  $('nba-position-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target,v=key=>form.elements.namedItem(key).value;
    if($('nba-position-save').disabled)return;
    $('nba-position-save').disabled=true;clearTimeout(purchaseTimer);
    try{
      const session=purchaseSession,before=purchaseKey();await automaticPurchasePlan();
      if(session!==purchaseSession||!$('nba-position-dialog').open)throw Error('Purchase dialog changed. Review and save the current purchase.');
      if(before!==purchaseKey()&&purchaseReadyKey!==purchaseKey())throw Error('Purchase details changed. Check the preview and save again.');
      const [ticker,side]=v('route').split('|'),quantity=Number(v('quantity')),price=NBARisk.parseEntry(v('entry_price')),fees=Number(v('entry_fees'));
      const stop=Number(v('stop_bid'))/100,target=Number(v('target_bid'))/100,opened=new Date(v('opened_at')),minutes=Number(v('hold_minutes'));
      if(!ticker||!['yes','no'].includes(side)||!Number.isInteger(quantity)||quantity<1||quantity>100000||!Number.isFinite(price)||price<=0||price>=1||!Number.isFinite(fees)||fees<0||fees>quantity||!Number.isFinite(stop)||!Number.isFinite(target)||stop<=0||target>=1||stop>=target||!Number.isFinite(minutes)||minutes<1||minutes>240||!Number.isFinite(opened.getTime())||opened.getTime()>Date.now()+5000)throw Error('Check the actual fill, timestamp, stop and target values.');
      if(state.riskPositions.filter(p=>p.state!=='closed').length>=100)throw Error('Close or export older positions before adding more.');
      const p={id:globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+Math.random().toString(36).slice(2),game_id:v('game_id'),ticker,side,quantity,entry_price:price,entry_fees:fees,opened_at:opened.toISOString(),stop_bid:stop,target_bid:target,hold_minutes:Number(v('hold_minutes')),quantity_estimated:!form.elements.manual_details.checked,reported_amount:Number(v('purchase_amount'))||null,dynamic_exits:true,dynamic_started_at:new Date().toISOString(),initial_target_bid:target,exit_plan_warning:purchaseWarning,state:'open',planner_snapshot:{recorded_at:new Date().toISOString(),sizing_mode:state.riskSettings.sizing_mode,scalp_hit_rate:state.riskHitRates[v('game_id')]??state.riskSettings.scalp_hit_rate,kelly_fraction:state.riskSettings.kelly_fraction,model_id:state.risk?.model_id,note:'Planner assumptions at manual recording time, not proof of information available at the original fill.'}};
      state.riskPositions.push(p);
      if(form.elements.update_cash.checked)state.riskSettings.cash=Math.max(0,state.riskSettings.cash-quantity*price-fees);
      fillRiskForm();saveRisk();$('nba-position-dialog').close();state.risk=null;await refreshRisk();renderBoard();
      $('nba-risk-message').textContent='Existing fill saved locally; no order was placed. Verify the remaining cash and equity. Stop, target and time alerts now follow recorded bids.';
    }catch(error){$('nba-position-error').textContent=error.message;}finally{$('nba-position-save').disabled=false;}
  });
  $('nba-exit-form').addEventListener('submit',async event=>{
    event.preventDefault();const form=event.target;
    try{
      const result=NBARisk.closePosition(state.riskPositions,form.elements.id.value,Number(form.elements.quantity.value),Number(form.elements.exit_price.value)/100,Number(form.elements.exit_fees.value),new Date().toISOString());
      state.riskPositions=result.positions;
      if(form.elements.update_balances.checked){state.riskSettings.equity=Math.max(0,state.riskSettings.equity+result.realizedPnl);state.riskSettings.cash=Math.max(0,state.riskSettings.cash+result.netProceeds);}
      fillRiskForm();saveRisk();$('nba-exit-dialog').close();state.risk=null;await refreshRisk();renderBoard();
      $('nba-risk-message').textContent='Exit recorded locally. Realized P&L: '+NBARisk.money(result.realizedPnl)+'. Verify your planner balances; no exchange action was taken.';
    }catch(error){$('nba-exit-error').textContent=error.message;}
  });
  purchaseForm.elements.route.addEventListener('change',()=>{const game=state.snapshot?.games.find(g=>g.game_id===purchaseForm.elements.game_id.value);$('nba-purchase-route').textContent=(game?game.away+' at '+game.home+' · ':'')+purchaseForm.elements.route.selectedOptions[0].textContent;updatePurchaseQuantity();});
  $('nba-risk-export').addEventListener('click',()=>{
    const blob=new Blob([JSON.stringify({exported_at:new Date().toISOString(),version:2,settings:state.riskSettings,positions:state.riskPositions,hit_rates:state.riskHitRates,stop_plans:state.stopPlans},null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='nba-manual-risk-journal.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  restoreRisk();
  async function refresh() {
    if(state.busy)return;state.busy=true;$('nba-refresh').disabled=true;$('nba-refresh').textContent='Refreshing…';
    try {
      const needsBacktest=Date.now()-state.backtestRead>60000;
      const tasks=await Promise.allSettled([read('/api/nba/fair-prices'),state.model?Promise.resolve(state.model):read('/api/nba/model'),needsBacktest?read('/api/nba/backtest'):Promise.resolve(state.backtest),state.catalog?Promise.resolve(state.catalog):read('/api/nba/features'),needsBacktest?read('/api/nba/optimization'):Promise.resolve(state.optimization),read('/api/nba/recordings'),read('/api/nba/paper'),refreshRisk()]);
      if(tasks[0].status==='rejected')throw tasks[0].reason;
      if(!Array.isArray(tasks[0].value.games))throw new Error('Invalid NBA snapshot');
      state.snapshot=tasks[0].value;state.received=Date.now();state.error=null;
      if(tasks[1].status==='fulfilled')state.model=tasks[1].value;
      if(tasks[2].status==='fulfilled'){state.backtest=tasks[2].value;if(needsBacktest)state.backtestRead=Date.now();}
      if(tasks[3].status==='fulfilled'&&state.catalog!==tasks[3].value){state.catalog=tasks[3].value;$('nba-inputs-content').innerHTML=NBAView.inputsMarkup(state.catalog);}
      if(tasks[4].status==='fulfilled'&&state.optimization!==tasks[4].value){state.optimization=tasks[4].value;$('nba-factor-content').innerHTML=NBAView.factorMarkup(state.optimization);}
      if(tasks[5].status==='fulfilled')$('nba-recording-content').innerHTML=NBAView.recordingMarkup(tasks[5].value,Date.now());
      if(tasks[6].status==='fulfilled')$('nba-paper-content').innerHTML=NBAView.paperMarkup(tasks[6].value);
      renderBoard();renderModel();renderBacktest();
    } catch(error) {
      state.error=error.message||'Request failed';
      if(state.snapshot)renderBoard();else{$('nba-notice').textContent='The NBA API could not be loaded: '+state.error;$('nba-games').innerHTML='<div class="nba-empty"><h3>Waiting for the local server</h3><p>Check that Signal Desk is running, then refresh this view.</p></div>';}
    } finally {state.busy=false;$('nba-refresh').disabled=false;$('nba-refresh').textContent='Refresh view';}
  }
  $('nba-refresh').addEventListener('click',()=>{state.model=null;state.backtestRead=0;refresh();});
  function showWholeDay(range){
    state.range=range;state.filter='all';state.search='';state.sort='tipoff';state.visibleLimit=40;
    $('nba-slate').value=range;$('nba-search').value='';$('nba-sort').value='tipoff';
    document.querySelectorAll('[data-nba-filter]').forEach(button=>{const selected=button.dataset.nbaFilter==='all';button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});renderBoard();
  }
  $('nba-show-today').addEventListener('click',()=>showWholeDay('today'));
  $('nba-show-yesterday').addEventListener('click',()=>showWholeDay('yesterday'));
  $('nba-slate').addEventListener('change',event=>{state.range=event.target.value;state.visibleLimit=40;renderBoard();});
  $('nba-date').value=state.date;
  $('nba-date').addEventListener('change',event=>{state.date=event.target.value;state.range='date';state.visibleLimit=40;$('nba-slate').value='date';renderBoard();});
  $('nba-more').addEventListener('click',()=>{state.visibleLimit+=40;renderBoard();});
  $('nba-checkpoint').addEventListener('change',event=>{state.checkpoint=event.target.value;renderBacktest();});
  document.querySelectorAll('[data-season-mode]').forEach(el=>el.addEventListener('click',()=>{state.seasonMode=el.dataset.seasonMode;document.querySelectorAll('[data-season-mode]').forEach(button=>{const selected=button===el;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});renderBacktest();}));
  $('nba-search').addEventListener('input',event=>{state.search=event.target.value;state.visibleLimit=40;renderBoard();});
  $('nba-sort').addEventListener('change',event=>{state.sort=event.target.value;renderBoard();});
  document.querySelectorAll('[data-nba-filter]').forEach(el=>el.addEventListener('click',()=>{state.filter=el.dataset.nbaFilter;document.querySelectorAll('[data-nba-filter]').forEach(button=>{const selected=button===el;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});renderBoard();}));
  $('nba-games').addEventListener('click',event=>{const button=event.target.closest('[data-inspect]');if(button){state.selected=button.dataset.inspect;$('nba-detail').showModal();renderDetail();}});
  $('nba-detail-close').addEventListener('click',()=>$('nba-detail').close());
  $('nba-detail').addEventListener('click',event=>{if(event.target!==$('nba-detail'))return;const box=event.target.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)event.target.close();});
  const zone=Intl.DateTimeFormat().resolvedOptions().timeZone;
  $('nba-board-note').textContent='Full game including overtime · Times in '+zone.replace(/_/g,' ');
  refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
  setInterval(()=>{updateSummary();if(state.snapshot&&freshnessKey()!==state.key)renderBoard();},1000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
}
