'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=(v,n=1)=>Number.isFinite(v)?(v*100).toFixed(n)+'%':'—';
const num=(v,n=3)=>Number.isFinite(v)?v.toFixed(n):'—';
const cents=v=>Number.isFinite(v)?(v*100).toFixed(1)+'¢':'—';
const money=v=>Number.isFinite(v)?v.toLocaleString('en-US',{style:'currency',currency:'USD'}):'—';
const local=t=>t?new Date(t*1000).toLocaleString():'—';
let busy=false;
function table(head,rows){return '<div class="table-wrap"><table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';}
async function refresh(){
 try{
  const response=await fetch('/api/btc-system',{cache:'no-store'});const s=await response.json();if(!response.ok)throw new Error(s.error||'Research unavailable');
  $('connection').textContent='Research connected · '+new Date().toLocaleTimeString();
  const live=s.live,stale=!live||Date.now()/1000-live.ts>12,f=stale?{}:live.features,models=stale?{}:live.models;
  if(stale)$('connection').textContent='Connected · awaiting fresh eligible observation';
  $('baseline').textContent=pct(models.A?.p_yes??f.baseline);$('residual').textContent=pct(models.C?.p_yes);
  $('baseline-note').textContent=models.A?'Calibrated final-minute-average baseline':'Uncalibrated final-minute-average benchmark';
  $('residual-note').textContent=models.C?'YES edge '+num(Number.isFinite(models.C.yes_edge)?models.C.yes_edge*100:null,2)+' pp · NO edge '+num(Number.isFinite(models.C.no_edge)?models.C.no_edge*100:null,2)+' pp':'Train a study with enough settled contracts';
  $('asks').textContent=cents(f.yes_ask)+' / '+cents(f.no_ask);
  $('prediction-table').innerHTML=table(['MODEL','MODEL P(YES)','KALSHI YES MID','YES ASK','NO ASK','YES NET EDGE','NO NET EDGE'],['A','B','C','D'].map(name=>[name==='D'?'D · gated selector':name,pct(models[name]?.p_yes),pct(f.kalshi_mid),cents(f.yes_ask),cents(f.no_ask),num(Number.isFinite(models[name]?.yes_edge)?models[name].yes_edge*100:null,2)+' pp',num(Number.isFinite(models[name]?.no_edge)?models[name].no_edge*100:null,2)+' pp']));
  const remaining=Number.isFinite(f.seconds_left)?Math.max(0,f.seconds_left-(Date.now()/1000-live.ts)):null;
  $('remaining').textContent=Number.isFinite(remaining)?Math.floor(remaining/60)+':'+String(Math.floor(remaining%60)).padStart(2,'0'):'—';$('ticker').textContent=stale?'Awaiting fresh eligible BTC inputs':live.ticker;
  $('source-notice').textContent='Settlement-outcome research only; no portfolio order connection. '+(s.brti.raw_available?'Raw BRTI available. Verify opening/closing coverage below.':'Raw BRTI unavailable: Coinbase proxy inputs are labeled; exact settlement reconstruction is unverified.')+(live?' Last feature record '+local(live.ts)+'.':'');
  const feeds=Object.values(s.venues).map(v=>[esc(v.venue),'<span class="status-badge '+(v.ready?'':'pending')+'">'+(v.ready?'LIVE':'PENDING')+'</span>',esc(v.error||'Fresh snapshot; rolling flows warm for 60s'),esc(v.reconnects),local(v.last_receipt)]);
  feeds.push(['BRTI','<span class="status-badge '+(s.brti.raw_available?'':'pending')+'">'+(s.brti.raw_available?'RAW INDEX':'MISSING')+'</span>',esc(s.brti.status),'—','—']);
  $('feed-table').innerHTML=table(['SOURCE','STATE','DETAIL','RECONNECTS','RECEIVED'],feeds);
  const fields=[['Distance to strike',f.distance_bps,' bps'],['Standardized distance',f.z_distance,' σ'],['30-minute annualized volatility',Number.isFinite(f.sigma_30m)?f.sigma_30m*100:null,'%'],['Coinbase depth · 5 bps',f.cb_depth_5,''],['Coinbase depth · 10 bps',f.cb_depth_10,''],['Coinbase depth · 25 bps',f.cb_depth_25,''],['Aggressor flow · 5s',f.cb_tfi_5,''],['Aggressor flow · 30s',f.cb_tfi_30,''],['Quote OFI · 10s',f.cb_ofi_10,''],['Absorption · 10s',f.cb_absorption_10,' BTC / $'],['Cross-venue divergence',f.cross_venue_bps,' bps'],['Kalshi depth imbalance',f.kalshi_imbalance,'']];
  $('feature-table').innerHTML=table(['FEATURE','MEASURED VALUE'],fields.map(([label,value,unit])=>[esc(label),Number.isFinite(value)?num(value)+esc(unit):'<span class="muted">Missing / warming up</span>']));
  $('official-rule-text').textContent=(live?.rules?.primary||'Waiting for current contract rules.')+' '+(live?.rules?.secondary||'');
  const verified=s.settlement_verification.filter(v=>v.status==='match').length,mismatch=s.settlement_verification.filter(v=>v.status==='mismatch').length;
  $('verification-state').textContent=mismatch?'MISMATCH':verified?'VERIFIED WINDOWS':'UNVERIFIED';
  $('verification-note').textContent=verified+' reconstructed windows match; '+mismatch+' mismatches. '+(s.settlement_verification.length-verified-mismatch)+' recent outcomes lack complete raw-index coverage. Official labels are used for research even when index reconstruction is unavailable.';
  $('observations').textContent=s.stats.observations.toLocaleString();$('windows').textContent=s.stats.windows.toLocaleString();$('outcomes').textContent=s.stats.settled_windows.toLocaleString();
  $('job-status').textContent=s.job.status;$('job-message').textContent=(s.job.message||'Ready. Import recordings, then run the study.')+' '+s.stats.raw_retention;
  document.querySelectorAll('[data-kind]').forEach(el=>el.disabled=busy||s.job.status==='running');
  const report=s.report;
  renderRefinement(s,stale);
  renderSwingReport(s.swing_report,s.fixed_swing_report);
  if(report?.experiments){
   $('cohort').textContent=report.cohort;
   $('model-table').innerHTML=table(['EXPERIMENT','TEST CONTRACTS','BRIER ↓','LOG LOSS ↓','CALIBRATION ERROR','BRIER VS MARKET'],report.experiments.map(e=>e.status==='complete'?['<span class="model-'+e.name+'">'+e.name+' · '+esc(e.description)+'</span>',e.quality.windows,num(e.quality.brier,4),num(e.quality.log_loss,4),pct(e.quality.ece),num(e.vs_market.mean_brier_difference,4)+' ['+e.vs_market.contract_bootstrap_95.map(v=>num(v,4)).join(', ')+']']:[esc(e.name),'—','—','—','—',esc(e.reason)]));
   const marketQuality=report.experiments.find(e=>e.market_quality)?.market_quality;
   if(marketQuality)$('model-table').querySelector('tbody').insertAdjacentHTML('beforeend','<tr><td>Kalshi midpoint benchmark</td><td>'+marketQuality.windows+'</td><td>'+num(marketQuality.brier,4)+'</td><td>'+num(marketQuality.log_loss,4)+'</td><td>'+pct(marketQuality.ece)+'</td><td>0 · reference</td></tr>');
   $('execution-table').innerHTML=table(['MODEL','NET P&L','FILLS / ATTEMPTS','PARTIAL','MAX REALIZED DD'],report.experiments.filter(e=>e.execution).map(e=>[e.name,money(e.execution.net_pnl),e.execution.trades+' / '+e.execution.attempts,e.execution.partial_fills,pct(e.execution.maximum_realized_drawdown)]));
   $('execution-note').textContent=report.experiments.find(e=>e.execution)?.execution.assumptions||'';
   $('split-note').textContent='Holdout '+report.holdout_id+' · '+report.split.train+' train / '+report.split.calibration+' calibration / '+report.split.test+' test contracts. '+report.model_selection;
   $('sequence-gate').textContent=report.sequence_gate;
   $('protocol').textContent=JSON.stringify({holdout_id:report.holdout_id,holdout_exposures:report.holdout_exposures,contract_membership:report.contract_membership,label_timing:report.label_timing,by_time_remaining:Object.fromEntries(report.experiments.filter(e=>e.quality).map(e=>[e.name,e.quality.by_time_remaining])),limits:report.limits},null,2);
   $('importance').innerHTML=table(['MODEL','GLOBAL GAIN IMPORTANCE'],report.experiments.filter(e=>e.feature_importance?.length).map(e=>[e.name,e.feature_importance.slice(0,8).map(i=>esc(i.feature)+' ('+num(i.gain,2)+')').join('<br>')]));
   calibration(report.experiments);
  }else{$('model-table').innerHTML='<p class="empty-text">Run the study once at least 40 independent, officially settled contracts are available. No illustrative scores are shown.</p>';}
  $('gap-table').innerHTML=s.stats.gaps.length?table(['VENUE','RECEIVED','EVENT'],s.stats.gaps.map(g=>[esc(g.venue),local(g.ts),esc(g.reason)])):'<p class="empty-text">No collector gaps recorded yet.</p>';
 }catch(error){$('connection').textContent='Unavailable';$('source-notice').textContent=error.message;for(const id of ['baseline','residual','asks','remaining'])$(id).textContent='—';$('prediction-table').textContent='Fresh observation unavailable';$('residual-note').textContent='Fresh observation unavailable';}
}
function calibration(experiments){
 const x=v=>40+v*510,y=v=>260-v*230;let svg='';
 for(let i=0;i<=5;i++){const v=i/5;svg+='<line class="gridline" x1="40" x2="550" y1="'+y(v)+'" y2="'+y(v)+'"/><text x="10" y="'+(y(v)+4)+'">'+pct(v,0)+'</text><text x="'+x(v)+'" y="286" text-anchor="middle">'+pct(v,0)+'</text>';}
 svg+='<line x1="40" x2="550" y1="260" y2="30" stroke="#819588" stroke-dasharray="4 5"/>';
 for(const e of experiments.filter(e=>e.quality)){const color={A:'#b3f2cf',B:'#e9bf81',C:'#a8b8ef'}[e.name];const pts=e.quality.reliability;
  svg+='<polyline fill="none" stroke="'+color+'" stroke-width="2" points="'+pts.map(p=>x(p.predicted)+','+y(p.actual)).join(' ')+'"/>';
  for(const p of pts)svg+='<circle cx="'+x(p.predicted)+'" cy="'+y(p.actual)+'" r="4" fill="'+color+'"><title>'+e.name+': '+pct(p.predicted)+' predicted, '+pct(p.actual)+' observed; '+p.windows+' windows</title></circle>';
 }
 $('calibration-chart').innerHTML=svg;
}
document.querySelectorAll('[data-kind]').forEach(el=>el.addEventListener('click',async()=>{if(busy)return;busy=true;try{const response=await fetch('/api/btc-system/job',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:el.dataset.kind})});const data=await response.json();if(!response.ok)throw new Error(data.error);await refresh();}catch(error){$('job-message').textContent=error.message;}finally{busy=false;}}));
async function poll(){await refresh();setTimeout(poll,2000);}poll();

function renderRefinement(s,stale){
 const r=s.refinement,p=s.prospective,live=stale?null:s.live?.challenger;
 $('refinement-live').textContent=live?'Frozen challenger '+live.name+' · P(YES) '+pct(live.p_yes)+'. '+live.message:'Awaiting a fresh frozen-model shadow forecast.';
 if(r){
  $('refinement-state').textContent=r.development_gate?'PROSPECTIVE TEST PENDING':'NO DEMONSTRATED EDGE';
  $('refinement-summary').textContent=r.recommendation+'. '+r.protocol.development_tickers.length+' development contracts; '+r.protocol.quarantined_tests.length+' previously published test contracts quarantined. Selected benchmark: '+r.selected+'.';
  $('refinement-table').innerHTML=table(['CANDIDATE','VALIDATION CONTRACTS','BRIER ↓','LOG LOSS ↓','BRIER Δ VS MARKET · 98.75% INTERVAL','$100 REPLAY NET','FILLS'],r.scores.map(c=>[esc(c.name)+(c.name===r.challenger?' · challenger':''),c.quality.windows,num(c.quality.brier,4),num(c.quality.log_loss,4),num(c.vs_market.mean_brier_difference,4)+' ['+c.vs_market.interval.map(v=>num(v,4)).join(', ')+']',money(c.execution.net_pnl),c.execution.trades]));
  $('refinement-selection').textContent=r.selection_rule+' '+r.limits+' Replay P&L here uses development walk-forward contracts and is not directly comparable with the earlier study’s different test period.';
  const labels={enough_contracts:'At least 60 validation contracts',at_least_three_folds:'At least three chronological folds',familywise_brier_interval_below_zero:'Brier improvement survives candidate comparison',improves_at_least_75pct_folds:'Improves in at least 75% of folds',log_loss_no_worse_than_market:'Log loss no worse than Kalshi',positive_net_walkforward_replay:'Positive net walk-forward replay'};
  $('refinement-gates').innerHTML=table(['REQUIREMENT','RESULT'],Object.entries(r.gates).map(([k,v])=>[esc(labels[k]||k),v?'PASS':'FAIL']));
  $('refinement-stress').innerHTML=table(['LATENCY','NET P&L','FILLS','FEES / ROUNDING','REALIZED DD'],r.stress.map(t=>[t.latency_ms+' ms',money(t.result.net_pnl),t.result.trades,money(t.result.fees_and_rounding),pct(t.result.maximum_realized_drawdown)]));
  const audit=r.diagnostics;
  $('refinement-audit').innerHTML=(audit.periods?table(['ORIGINAL PERIOD','CONTRACTS','YES OUTCOMES'],Object.entries(audit.periods).map(([k,v])=>[esc(k),v.contracts,pct(v.yes_rate)])):'')+(audit.models?table(['ORIGINAL MODEL','YES / NO FILLS','CALIBRATED 50% SIGNAL','CENT-ALIGNED REPLAY NET'],Object.entries(audit.models).map(([k,v])=>[esc(k),(v.sides.yes||0)+' / '+(v.sides.no||0),pct(v.calibration_at_50),money(v.cent_aligned_replay?.net_pnl)])):'');
  $('refinement-protocol').textContent=JSON.stringify({id:r.protocol.id,frozen_at:local(r.protocol.frozen_at),development_fingerprint:r.protocol.development_fingerprint,folds:r.folds,entry_policy:r.protocol.entry_policy,selection:r.selection_rule},null,2);
 }
 if(p){
  $('prospective-state').textContent=p.status==='complete'?'Completed once · '+p.required_contracts+' contracts':p.status==='collecting'?(p.completed_contracts||0)+' / '+p.required_contracts+' settled · '+p.reserved_contracts+' reserved':p.status;
  $('prospective-result').textContent=p.status==='complete'?'Candidate Brier '+num(p.quality.brier,4)+' vs Kalshi '+num(p.market_quality.brier,4)+' · net replay '+money(p.execution.net_pnl)+'. '+p.message:(p.message||p.reason||'Run refinement to freeze the candidate and start recording new forecasts.');
  if(p.status==='complete')$('refinement-state').textContent=p.passed?'PROSPECTIVE PASSED · REVIEW':'NO VALIDATED EDGE';
 }
 const b=[['Spot L2 + actual trades','Implemented','Coinbase and Kraken; source health and gaps above'],['Raw BRTI settlement verification',s.brti.raw_available?'Recording':'Credentials required','Complete raw opening and closing windows are required'],['Kalshi order books and trades','Implemented','Public REST; authenticated streaming supported when configured'],['Five-second features / baseline / LightGBM','Implemented','Spot-flow training waits for enough independent contracts'],['Walk-forward selection and future lockbox','Implemented','Published tests quarantined; first 30 later forecasted contracts evaluated once'],['Fees, latency, depth, partial fills, Kelly','Implemented','Separate research replay; no portfolio order connection'],['Binance / perpetual-futures features','Deferred','Add after spot-flow evidence; not supplied by the current models'],['New outcome sequence model','Deferred','Conditional on demonstrated order-flow improvement; existing quote LSTM is a different target'],['Twelve-week multi-regime validation','Collecting','Current local history is shorter; no long-run profitability claim'],['Queue priority / guaranteed fills','Not simulated','Visible depth remains an execution approximation'],['Live trading execution','Not implemented','Research and existing paper portfolios only']];
 $('blueprint-table').innerHTML=table(['BLUEPRINT COMPONENT','STATE','DETAIL'],b.map(row=>row.map(esc)));
}

function renderSwingReport(r,fixed){
 if(fixed){const e=fixed.execution,b=fixed.settlement_signal_benchmark;$('fixed-swing-benchmark').innerHTML=table(['ARCHIVED FIXED RULES','NET P&L','TRADES'],[['Fixed 5% target / 10% stop',money(e.net_pnl),e.trades],...(b?[[esc(b.name),money(b.net_pnl),b.trades]]:[])]);}
 if(!r||r.kind!=='adaptive_swing')return;
 const e=r.execution,c=r.calibration;
 $('swing-report-table').innerHTML=table(['STRATEGY','NET P&L','TRADES','RETURN ON DEPLOYED CAPITAL','LOG WEALTH GROWTH'],[['Adaptive stochastic exits + fractional Kelly',money(e.net_pnl),e.trades,pct(e.return_on_deployed_capital),num(e.realized_log_growth,4)],['No-trade benchmark',money(0),0,'—',num(0,4)]]);
 $('swing-coverage').innerHTML=table(['CHECK','VALUE'],[['Train / calibration / test contracts',r.split.training_contracts+' / '+r.split.calibration_contracts+' / '+r.split.test_contracts],['Later drift calibration',c.passed?'PASS':esc(c.reason)],['Calibrated drift trust',num(c.drift_trust,2)],['Positive held-out growth',r.growth_gate.passed?'PASS':esc(r.growth_gate.reason)],['Replay drawdown · gap stress included',pct(e.maximum_replay_drawdown)],['Censored trades',e.censored_trades],...Object.entries(e.skips).map(([k,v])=>[esc(k),v])]);
 $('swing-report-note').textContent=r.objective+' '+r.selection_rule+' '+r.limits;
 $('swing-fills').innerHTML=e.fills.length?table(['CONTRACT','SIDE / QUANTITY','MODEL TARGET / STOP','TIME CAP','EXIT','NET ROI','P&L'],e.fills.map(f=>[esc(f.ticker),esc(f.side)+' / '+f.quantity,cents(f.target_bid)+' / '+cents(f.stop_bid),f.max_hold_seconds+'s',esc(f.reason),pct(f.roi),money(f.pnl)])):'<p class="research-note">No qualified trades. Keeping cash is the model’s result, not evidence of a profitable strategy.</p>';
 $('swing-protocol').textContent=JSON.stringify({version:r.version,settings:r.settings,split:r.split,calibration:r.calibration,gap_hazard:r.gap_hazard,growth_gate:r.growth_gate,limits:r.limits},null,2);
}

async function refreshSwing(){
 try{
  const target=$('touch-target').value;
  const response=await fetch('/api/btc-system/swing'+(target?'?target='+encodeURIComponent(target):''),{cache:'no-store'}),s=await response.json();
  if(!response.ok)throw new Error(s.error||'Swing research unavailable');
  if(s.status!=='live')throw new Error(s.reason||'Waiting for fresh market data');
  $('swing-status').textContent='LIVE RESEARCH';
  const price=v=>Number.isFinite(v)?v.toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}):'—';
  $('touch-prices').textContent=price(s.spot)+' → '+price(s.target);
  $('touch-probability').textContent=pct(s.p_btc_touch_before_close);
  $('swing-settlement').textContent=pct(s.p_settlement_yes);
  $('touch-time').textContent=Math.floor(s.seconds_left/60)+':'+String(Math.floor(s.seconds_left%60)).padStart(2,'0')+' remaining · '+s.ticker;
  $('swing-live-note').textContent=s.source+' · measured annual volatility '+pct(s.sigma)+'. '+s.note+' Live quote view targets 500 ms; underlying source updates have their own cadence.';
  const precise=v=>Number.isFinite(v)?(v*100).toFixed(2)+'¢':'—';
  $('swing-live-table').innerHTML=table(['SIDE','CURRENT ASK / SELL BID','PLAN QUANTITY / LIMIT COST','MODEL TARGET · NET RETURN','MODEL STOP · NET RETURN','MAX HOLD'],s.sides.map(x=>{const p=x.plan||{};return [esc(x.side.toUpperCase()),cents(x.ask)+' / '+cents(x.bid),(p.quantity>0?p.quantity+' contracts':p.evaluated_quantity?'WAIT · '+p.evaluated_quantity+'-contract reference':'WAIT')+' / '+money(p.cost),precise(p.target_bid)+' · '+pct(p.target_net_return),precise(p.stop_bid)+' · '+pct(p.stop_net_return),Number.isFinite(p.max_hold_seconds)?p.max_hold_seconds+'s':'—'];}));
  $('swing-risk-table').innerHTML=table(['SIDE','EXPECTED NET ROI','CONSERVATIVE MODEL ROI','P(NET PROFIT)','FULL / FRACTIONAL KELLY','PORTFOLIO TAIL LOSS','RESEARCH STATUS'],s.sides.map(x=>{const p=x.plan||{};return [esc(x.side.toUpperCase()),pct(p.expected_net_return),pct(p.conservative_mean_return),pct(p.probability_profit),pct(p.full_kelly_fraction)+' / '+pct(p.fractional_kelly_fraction),pct(p.portfolio_tail_loss_fraction),esc(p.quantity>0?'PAPER CANDIDATE · '+pct(x.bankroll_fraction)+' of $100':x.reason)];}));
 }catch(error){$('swing-status').textContent='WAIT';$('swing-live-note').textContent=error.message;for(const id of ['touch-prices','touch-probability','swing-settlement'])$(id).textContent='—';$('swing-live-table').textContent='Fresh inputs unavailable';$('swing-risk-table').textContent='';}
}
$('touch-use-strike').addEventListener('click',()=>{$('touch-target').value='';refreshSwing();});
async function pollSwing(){await refreshSwing();setTimeout(pollSwing,500);}pollSwing();
