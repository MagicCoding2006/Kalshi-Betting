'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (n, digits=2) => Number.isFinite(n) ? n.toLocaleString('en-US', {style:'currency',currency:'USD',minimumFractionDigits:digits,maximumFractionDigits:digits}) : '—';
const pct = (n, digits=1) => Number.isFinite(n) ? (n*100).toFixed(digits)+'%' : '—';
const cents = n => Number.isFinite(n) ? (n*100).toFixed(n*100 % 1 > .0001 ? 1 : 0)+'¢' : '—';
const number = n => Number.isFinite(n) ? n.toLocaleString('en-US', {maximumFractionDigits:2}) : '—';
const clockTime = t => t ? new Date(t*1000).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
let state = null, page = 'overview', chartMode = 'price', bookSide = 'yes', requestBusy = false, failed = false, lastReceived = 0;
let selectedAsset = new URLSearchParams(location.search).get('asset') === 'gold' ? 'gold' : 'btc';
let assetRequestVersion = 0;
const assetLabels = {btc:{name:'Bitcoin',symbol:'BTC'},gold:{name:'Gold',symbol:'GOLD'}};
function apiUrl(path, asset=selectedAsset){return path+(path.includes('?')?'&':'?')+'asset='+encodeURIComponent(asset);}
let toastTimer;
const pageInfo = {overview:['Market overview','From market data to a decision. Every step, in view.'],decisions:['Decision log','Every evaluation, with the inputs and reasoning behind it.'],research:['Research lab','Record the book. Test the forecast. Inspect what the data supports.'],positions:['Paper portfolio','Track simulated positions as the market moves.']};
function navigate(next) {
  if(next!=='research')stopReplay();
  page = next;
  for (const key of Object.keys(pageInfo)) $(key+'-page').hidden = key !== page;
  document.querySelectorAll('[data-page]').forEach(el => el.classList.toggle('active',el.dataset.page===page));
  $('page-title').textContent = pageInfo[page][0];
  if (page === 'overview') { const tag = document.createElement('span'); tag.className='live-label'; tag.textContent='LIVE DATA'; $('page-title').append(tag); }
  $('page-description').textContent = pageInfo[page][1];
  if (state) render();
}
function toast(message, error=false) {
  clearTimeout(toastTimer); $('toast').textContent=message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  toastTimer=setTimeout(() => {$('toast').hidden=true;},6000);
}
async function mutate(path, body={}) {
  if (requestBusy) return false;
  requestBusy=true;
  updateAssetControls();
  try {
    const response=await fetch(apiUrl(path),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    await refresh(); return true;
  } catch(error) { if(path==='/api/config') $('settings-error').textContent=error.message; else if(path==='/api/paper/reset') $('reset-error').textContent=error.message; else toast(error.message,true); return false; }
  finally { requestBusy=false; updateAssetControls(); }
}
async function refresh() {
  const requestedAsset=selectedAsset,version=++assetRequestVersion;
  try {
    const [response,assetsResponse]=await Promise.all([fetch(apiUrl('/api/state',requestedAsset),{cache:'no-store'}),fetch('/api/assets',{cache:'no-store'})]);
    if (!response.ok) throw new Error('Server returned '+response.status);
    const next=await response.json();
    if(requestedAsset!==selectedAsset||version!==assetRequestVersion)return;
    if(next.asset.id!==requestedAsset)throw new Error('Strategy response mismatch');
    state=next;failed=false;lastReceived=Date.now();$('asset-content').hidden=false;render();
    if(page==='research')refreshResearch(requestedAsset);
    if(assetsResponse.ok){const summary=await assetsResponse.json();if(requestedAsset!==selectedAsset||version!==assetRequestVersion)return;for(const asset of summary.assets){const label=$(asset.id+'-engine-status');if(label)label.textContent=(asset.auto_paper?'Bot running':'Bot paused')+' · '+money(asset.cash);}}
  } catch(error) { if(requestedAsset===selectedAsset&&version===assetRequestVersion){failed=true;renderConnection();} }
}
function updateAssetControls(){
  $('asset-select').value=selectedAsset;$('asset-select').disabled=requestBusy;
  document.querySelectorAll('[data-asset]').forEach(el=>{el.classList.toggle('selected',el.dataset.asset===selectedAsset);el.disabled=requestBusy;el.setAttribute('aria-pressed',String(el.dataset.asset===selectedAsset));});
}
function selectAsset(asset, push=true){
  if(requestBusy||!assetLabels[asset]||asset===selectedAsset&&state)return;
  stopReplay();researchState=null;
  selectedAsset=asset;state=null;failed=false;assetRequestVersion++;
  $('asset-content').hidden=true;
  document.querySelectorAll('dialog[open]').forEach(el=>el.close());
  $('asset-breadcrumb').textContent=assetLabels[asset].name;
  $('asset-heading').textContent=assetLabels[asset].symbol+' / 15 MINUTE';
  $('connection-banner').hidden=false;$('connection-banner').textContent='Loading '+assetLabels[asset].name+' strategy…';
  chartMode='price';document.querySelectorAll('[data-chart]').forEach(el=>el.classList.toggle('selected',el.dataset.chart==='price'));
  if(push){const url=new URL(location.href);url.searchParams.set('asset',asset);history.pushState({asset},'',url);}
  updateAssetControls();renderConnection();refresh();
}

function serverNow(){return state ? state.server_time+(Date.now()-lastReceived)/1000 : Date.now()/1000;}
function renderConnection() {
  const referenceAge=state ? serverNow()-(state.reference.time||0) : Infinity;
  const bookAge=state ? serverNow()-state.book_time : Infinity;
  const healthy=state && !state.error && !failed && referenceAge<=15 && bookAge<=10 && Date.now()-lastReceived<10000;
  $('feed-status').classList.toggle('connected',Boolean(healthy));
  $('feed-status').innerHTML='<i></i>'+(!state?'Connecting':failed?'Disconnected':healthy?'Feed connected':'Feed delayed');
  $('book-live').textContent=bookAge<=10 && !failed?'LIVE':'STALE';
  let message=!state?'Connecting to Kalshi’s public market feed. No sample data is shown.':
    failed?'Lost connection to the local server. Displayed data are frozen; actions are disabled.':
    state.error?'Feed refresh paused: '+state.error:
    referenceAge>15?'Reference price is delayed. New paper entries are blocked until fresh data arrive.':
    bookAge>10?'Order book is delayed. New paper entries are blocked until fresh data arrive.':
    state.asset.feed_notice;
  $('connection-banner').hidden=!message; $('connection-banner').classList.toggle('informational',Boolean(healthy && !state?.error));
  if(message) $('connection-banner').textContent=message;
  $('paper-buy').disabled=!healthy || !state?.decision.action.startsWith('BUY ');
  $('auto-button').disabled=failed || !state;
  if(!healthy && state && state.decision.action.startsWith('BUY ')) {
    $('decision-chip').textContent='WAIT'; $('decision-label').textContent='Waiting for fresh data';
  }
  return healthy;
}
function updateTimer() {
  $('clock').textContent=new Date().toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})+' LOCAL';
  if(!state?.market.close_time) return;
  const remaining=Math.max(0,new Date(state.market.close_time).getTime()/1000-serverNow());
  $('countdown').textContent=String(Math.floor(remaining/60)).padStart(2,'0')+':'+String(Math.floor(remaining%60)).padStart(2,'0');
  $('timer-progress').style.width=Math.min(100,remaining/900*100)+'%';
  renderConnection();
}
function render() {
  if(!state)return;
  const {market:m,reference:r,decision:d,portfolio:p,config:c,asset:a}=state;
  updateAssetControls();
  $('asset-breadcrumb').textContent=a.name;$('asset-heading').textContent=a.symbol+' / 15 MINUTE';
  $('spot-label').textContent=a.price_label;$('price-unit').textContent=a.unit;$('chart-title').textContent=a.name+' price';
  document.querySelector('[name="volatility_mode"] option[value="realized"]').textContent=a.id==='gold'?'Automatic · Gold reference minute returns':'Automatic · Coinbase 1-minute returns';
  $('settings-asset').textContent=a.symbol+' STRATEGY';$('settings-title').textContent=a.name+' strategy settings';
  $('footer-asset').textContent='BTC + Gold research desk';
  $('reset-paper').textContent='Reset '+a.name+' to $100';$('reset-title').textContent='Start a fresh $100 '+a.name+' run';$('reset-scope').textContent='This resets '+a.name+' only.';
  $('cutoff-explanation').textContent=a.id==='btc'?'60-second averaging rule + chosen safety margin.':'User safety cutoff; Gold settles on a candle close.';
  $('model-summary').textContent='N(d₂) estimates terminal exceedance under a lognormal model. '+a.model_note+' The baseline is uncalibrated; paper fills assume displayed liquidity.';
  $('model-input-description').textContent='S is '+a.source_label+'. K is the published '+a.symbol+' strike, and T is the actual time to close in years. Volatility and historical drift come from completed minute returns; their sources and sampling error are shown in settings.';
  $('settlement-description').textContent=a.settlement;$('model-limitations').textContent=a.model_note;
  $('official-rules').textContent=(m.rules_primary||'Rules unavailable until market discovery.')+' '+(m.rules_secondary||'');
  document.querySelector('[data-chart="reference"]').hidden=a.id==='gold';
  document.querySelector('[data-chart="price"]').textContent=a.id==='gold'?'Reference':'Spot';
  $('export-decisions').href=apiUrl('/api/export.csv');
  document.title='Signal Desk · '+a.name;

  $('spot').textContent=money(r.price);
  $('strike').textContent=money(m.floor_strike);
  $('probability').textContent=pct(d.probability); $('probability-no').textContent='P(NO): '+pct(d.probability===null?null:1-d.probability);
  const points=r.points||[];
  const old=points.find(point => point.time>=serverNow()-900) || points[0];
  const change=old && r.price ? r.price/old.price-1 : null;
  $('spot-change').textContent=change===null?'Waiting for reference data':(change>=0?'+':'')+pct(change,2)+' over chart window';
  $('spot-change').classList.toggle('mint',change>0); $('spot-change').classList.toggle('coral',change<0);
  const distance=r.price && m.floor_strike?r.price-m.floor_strike:null;
  $('strike-distance').textContent=distance===null?'Strike not published':money(Math.abs(distance))+' '+(distance>=0?'above':'below')+' target';
  $('close-time').textContent=m.close_time?'Closes '+new Date(m.close_time).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZoneName:'short'}):'Discovering active market';
  $('market-ticker').textContent=m.ticker||a.series+' · Discovering market';
  $('chart-source').textContent=chartMode==='reference'?'KALSHI':chartMode==='price'?a.source_short:'MODEL';
  $('chart-subtitle').textContent=chartMode==='reference'?'Kalshi reference chart · settlement context':chartMode==='price'?a.source_label:'Model probability vs. YES bid/ask midpoint';
  $('model-edge').textContent='σ '+pct(d.volatility,0)+' annualized · '+(c.volatility_mode==='realized'?state.volatility_returns+' returns':'manual');
  const actionable=d.action.startsWith('BUY '), s=d.sizing||{};
  $('decision-chip').textContent=d.action;
  $('decision-chip').classList.toggle('buy',actionable);
  $('decision-glyph').textContent=actionable?'+':'Ⅱ'; $('decision-glyph').classList.toggle('buy',actionable);
  $('decision-label').textContent=actionable?'Buy '+s.side.toUpperCase()+' · model sees an edge':d.blockers.length?'Entry paused':'No qualifying edge';
  $('decision-reason').textContent=d.reason;
  $('fair-label').textContent='Fair value'+(s.side?' ('+s.side.toUpperCase()+')':''); $('ask-label').textContent='Ask'+(s.side?' ('+s.side.toUpperCase()+')':'');
  $('fair-value').textContent=s.probability!==undefined?cents(s.probability):'—';
  $('entry-ask').textContent=cents(s.ask);
  $('net-edge').textContent=Number.isFinite(s.edge)?(s.edge>=0?'+':'')+(s.edge*100).toFixed(2)+' pp':'—';
  $('net-edge').className=s.edge>=c.min_edge?'mint':s.edge<0?'coral':'';
  $('decision-footnote').textContent='Entry requires '+(c.min_edge*100).toFixed(1)+' pp net edge.';
  const size=actionable?s.portfolio_pct||0:0;
  $('size-percent').innerHTML=(size*100).toFixed(2)+'<span>%</span>';
  $('size-cost').textContent=money(actionable?s.cost:0);
  $('size-contracts').textContent=number(actionable?s.contracts:0)+' contracts';
  $('size-bar').style.width=(c.max_position_pct?Math.min(100,size/c.max_position_pct*100):0)+'%';
  $('position-cap').textContent=pct(c.max_position_pct);
  $('equity').textContent=money(p.equity);
  $('kelly-tag').textContent=({'0.25':'¼','0.5':'½','0.1':'⅒','1':'FULL','0':'0'}[String(c.kelly_fraction)]||number(c.kelly_fraction))+' KELLY';
  $('sizing-note').textContent=d.blockers.length?'New entries blocked. '+d.blockers[0]:actionable?'Best-ask liquidity cap · fees included in stake.':'No stake while net edge is below the threshold.';
  $('auto-button').textContent=state.auto_paper?'Pause '+a.name+' bot':'Start '+a.name+' bot';
  $('auto-button').classList.toggle('running',state.auto_paper);
  $('recent-count').textContent=state.decisions.length;
  $('log-count').textContent=state.decisions.length;
  $('recent-table').innerHTML=decisionTable(state.decisions.slice(0,5));
  renderLog(); renderBook(); renderChart(); renderPositions(); renderInputs(); renderSideComparison(); updateTimer();
}
function badge(action){let type=action.includes('BUY')?'buy':action.includes('SELL')?'sell':action==='WAIT'?'wait':'';return '<span class="action-badge '+type+'">'+esc(action)+'</span>';}
function decisionTable(rows){
  if(!rows.length)return '<div class="empty-text">No decisions yet. Evaluations will appear as live data arrive.</div>';
  return '<div class="table-wrap"><table><thead><tr><th>TIME (LOCAL)</th><th>DECISION</th><th>'+esc(state.asset.symbol)+' PRICE</th><th>MODEL P(YES)</th><th title="YES bid/ask midpoint saved at decision time; an implied probability, not an executable fill price">KALSHI YES (MID)</th><th>NET EDGE</th><th>REASON</th></tr></thead><tbody>'+rows.map(d=>'<tr class="audit-row" data-decision="'+d.id+'" tabindex="0" role="button" aria-label="Inspect decision at '+clockTime(d.time)+'"><td class="mono">'+clockTime(d.time)+'</td><td>'+badge(d.action)+'</td><td class="mono">'+money(d.spot)+'</td><td class="mono">'+pct(d.probability)+'</td><td class="mono" title="Saved YES bid: '+pct(d.yes_bid)+'; ask: '+pct(d.yes_ask)+'. Midpoint is not an executable entry price.">'+pct(d.market_probability)+'</td><td class="mono '+((d.sizing?.edge||0)>=state.config.min_edge?'mint':'')+'">'+(Number.isFinite(d.sizing?.edge)?(d.sizing.edge>=0?'+':'')+(d.sizing.edge*100).toFixed(2)+' pp':'—')+'</td><td class="reason-cell" title="'+esc(d.reason)+'">'+esc(d.reason)+'</td></tr>').join('')+'</tbody></table></div>';
}
function renderLog(){
  const filter=$('decision-filter').value;
  const rows=state.decisions.filter(d=>filter==='all'||filter==='signals'&&d.action.startsWith('BUY')||filter==='trades'&&(d.action.startsWith('PAPER')||d.action==='SETTLED')||filter==='wait'&&d.action==='WAIT');
  $('full-log').innerHTML=decisionTable(rows);
}
function renderBook(){
  const bids=state.book[bookSide+'_bids']||[],asks=state.book[bookSide+'_asks']||[];
  const bid=bids[0]?.price,ask=asks[0]?.price;
  $('best-bid').textContent=cents(bid); $('best-ask').textContent=cents(ask);
  $('spread').textContent=Number.isFinite(bid)&&Number.isFinite(ask)?cents(ask-bid):'—';
  const topAsks=asks.slice(0,3),topBids=bids.slice(0,3),max=Math.max(1,...topAsks.map(l=>l.quantity),...topBids.map(l=>l.quantity));
  function row(l,type,total){return '<div class="book-row '+type+'"><b class="depth-fill" data-depth="'+(l.quantity/max*100)+'"></b><span>'+(l.price*100).toFixed(l.price*100%1>.0001?1:0)+'</span><span>'+number(l.quantity)+'</span><span>'+number(total)+'</span></div>';}
  let total=0;const askRows=topAsks.map(l=>{total+=l.quantity;return row(l,'ask',total);}).reverse();total=0;
  const bidRows=topBids.map(l=>{total+=l.quantity;return row(l,'bid',total);});
  $('book-levels').innerHTML=askRows.join('')+(topAsks.length?'':'<div class="empty-text">No asks available</div>')+'<div class="book-divider">'+bookSide.toUpperCase()+' · BID / ASK</div>'+bidRows.join('');
  document.querySelectorAll('[data-depth]').forEach(el=>el.style.width=el.dataset.depth+'%');
}
function renderChart(){
  if(!state)return;
  const probability=chartMode==='probability',settlement=chartMode==='reference',now=serverNow(); const reference=settlement?state.settlement_reference:state.reference;
  let points=probability?(state.probability_history||[]).filter(p=>p.time>=now-900):(reference?.points||[]).filter(p=>p.time>=now-900);
  if(!probability && reference?.price && reference?.time) points=points.concat([{time:reference.time,price:reference.price}]);
  points=points.filter(p=>Number.isFinite(p.time)&&(probability?Number.isFinite(p.model):Number.isFinite(p.price)));
  $('chart-empty').hidden=points.length>1; $('chart-empty').textContent=settlement&&reference?.error?'Kalshi reference chart unavailable':'Collecting chart data';
  $('chart').setAttribute('aria-label',probability?'Model YES probability and Kalshi midpoint over time':state.asset.name+' price and target over the last 15 minutes');
  $('chart-legend').innerHTML=probability?'<span><i class="legend-line mint-line"></i>Model P(YES)</span><span><i class="legend-line coral-line"></i>Kalshi midpoint</span><span class="chart-range">CURRENT CONTRACT</span>':'<span><i class="legend-line mint-line"></i>'+ (settlement?'Kalshi reference':state.asset.source_label) +'</span><span><i class="legend-line dashed"></i>Target price</span><span class="chart-range">LAST 15 MIN</span>';
  if(points.length<2){$('chart').innerHTML='';return;}
  const w=800,h=280,left=12,right=82,top=16,bottom=32,pw=w-left-right,ph=h-top-bottom;
  const minT=probability?Math.min(...points.map(p=>p.time)):now-900,maxT=Math.max(now,minT+30);
  let values=probability?points.flatMap(p=>[p.model,p.market]).filter(Number.isFinite):points.map(p=>p.price);
  if(!probability&&state.market.floor_strike)values.push(state.market.floor_strike);
  let min=probability?0:Math.min(...values),max=probability?1:Math.max(...values);
  const pad=probability?0:Math.max((state.reference.price||1)*.00015,(max-min)*.16);min-=pad;max+=pad;
  const x=t=>left+(t-minT)/(maxT-minT)*pw,y=v=>top+(max-v)/(max-min)*ph;
  let svg='<defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#a7edc7" stop-opacity=".13"/><stop offset="100%" stop-color="#a7edc7" stop-opacity="0"/></linearGradient><clipPath id="plot-clip"><rect x="'+left+'" y="'+top+'" width="'+pw+'" height="'+ph+'"/></clipPath></defs>';
  for(let i=0;i<5;i++){const value=min+(max-min)*i/4,yy=y(value);svg+='<line class="gridline" x1="'+left+'" x2="'+(w-right)+'" y1="'+yy+'" y2="'+yy+'"/><text x="'+(w-right+13)+'" y="'+(yy+4)+'">'+(probability?pct(value,0):money(value,state.asset.id==='gold'?2:0))+'</text>';}
  for(let i=0;i<5;i++){const t=minT+(maxT-minT)*i/4;svg+='<text x="'+x(t)+'" y="'+(h-8)+'" text-anchor="'+(i===0?'start':i===4?'end':'middle')+'">'+new Date(t*1000).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})+'</text>';}
  const coords=(key)=>points.filter(p=>Number.isFinite(p[key])).map(p=>x(p.time).toFixed(2)+','+y(p[key]).toFixed(2)).join(' ');
  const key=probability?'model':'price',line=coords(key);
  const first=points[0],last=points[points.length-1];
  svg+='<g clip-path="url(#plot-clip)"><polygon points="'+x(first.time)+','+(top+ph)+' '+line+' '+x(last.time)+','+(top+ph)+'" fill="url(#chart-fill)"/>';
  if(!probability&&state.market.floor_strike){const yy=y(state.market.floor_strike);svg+='<line class="target-line" x1="'+left+'" x2="'+(w-right)+'" y1="'+yy+'" y2="'+yy+'"/><text x="'+(left+7)+'" y="'+(yy-8)+'" fill="#c2aa7e">TARGET '+money(state.market.floor_strike)+'</text>';}
  if(probability)svg+='<polyline points="'+coords('market')+'" fill="none" stroke="#a8b4dc" stroke-width="1.5" stroke-linejoin="round"/>';
  svg+='<polyline points="'+line+'" fill="none" stroke="#b3f2cf" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/></g><circle cx="'+x(last.time)+'" cy="'+y(last[key])+'" r="4" fill="#b3f2cf" stroke="#15251d" stroke-width="2"/>';
  $('chart').innerHTML=svg;
}
function renderPositions(){
  const p=state.portfolio;
  $('archived-runs').innerHTML=(state.paper_runs||[]).length?'<div class="table-wrap"><table><thead><tr><th>RUN</th><th>ARCHIVED (LOCAL)</th><th>ENDING CASH</th><th></th></tr></thead><tbody>'+state.paper_runs.map(r=>'<tr><td>#'+r.id+'</td><td>'+esc(new Date(r.archived*1000).toLocaleString())+'</td><td>'+money(r.cash)+'</td><td><button class="text-button" data-run="'+r.id+'">Inspect archived run</button></td></tr>').join('')+'</tbody></table></div>':'<div class="empty-text">Runs are archived when you reset the paper portfolio.</div>';
  $('portfolio-equity').textContent=money(p.equity); $('portfolio-cash').textContent=money(p.cash);
  $('portfolio-pnl').textContent=(p.realized_pnl>=0?'+':'−')+money(Math.abs(p.realized_pnl));
  $('portfolio-pnl').className=p.realized_pnl>=0?'mint':'coral'; $('portfolio-exposure').textContent=money(p.exposure);
  if(!p.positions.length){$('positions-table').innerHTML='<div class="empty-text">No paper positions yet. Start the paper bot or simulate a qualifying signal from the overview.</div>';return;}
  $('positions-table').innerHTML='<div class="table-wrap"><table><thead><tr><th>MARKET / OPENED</th><th>SIDE</th><th>CONTRACTS</th><th>ENTRY</th><th>COST + FEES</th><th>NET P&amp;L</th><th>STATUS</th><th></th></tr></thead><tbody>'+p.positions.map(pos=>{const pnl=pos.status==='closed'?pos.pnl:pos.unrealized_pnl;return '<tr><td class="mono">'+esc(pos.ticker)+'<br>'+clockTime(pos.opened)+'</td><td>'+badge(pos.side.toUpperCase())+'</td><td class="mono">'+number(pos.quantity)+'</td><td class="mono">'+cents(pos.entry)+'</td><td class="mono">'+money(pos.cost)+'</td><td class="mono '+(pnl>=0?'mint':'coral')+'">'+(pnl===null?'Unquoted':(pnl>=0?'+':'−')+money(Math.abs(pnl)))+'</td><td title="'+esc(pos.close_reason||'Awaiting exit or official result')+'">'+(pos.status==='open'?'Open':'Closed')+'</td><td>'+(pos.status==='open'?'<button class="button secondary manual-close" data-close-position="'+pos.id+'" '+(!pos.mark_available?'disabled':'')+'>Paper close</button>':'')+'</td></tr>';}).join('')+'</tbody></table></div>';
}
function renderSideComparison(){
  const rows=state.decision.candidates||[];
  $('side-comparison').innerHTML='<div class="table-wrap"><table><thead><tr><th>SIDE</th><th>MODEL PROBABILITY</th><th>BUY AT</th><th>NET EDGE</th><th>ELIGIBLE CONTRACTS</th></tr></thead><tbody>'+rows.map(c=>'<tr><td>'+esc(c.side.toUpperCase())+'</td><td>'+pct(c.probability)+'</td><td>'+cents(c.ask)+'</td><td title="'+esc(c.reason)+'">'+(Number.isFinite(c.edge)?(c.edge*100).toFixed(2)+' pp':esc(c.reason))+'</td><td>'+number(c.contracts)+'</td></tr>').join('')+'</tbody></table></div>';
}
function renderInputs(){
  const i=state.inputs;if(!i)return;
  const drift=i.drift.value,driftError=i.drift.drift_standard_error;
  $('live-inputs').innerHTML=[['Annualized volatility',pct(i.volatility.value),i.volatility.samples+' completed minute returns · '+i.volatility.mode],['Historical drift over 15m',Number.isFinite(drift)?(drift*900/(365*86400)*10000).toFixed(2)+' bps':'Pending',Number.isFinite(driftError)?'Sampling SE: '+(driftError*900/(365*86400)*10000).toFixed(2)+' bps · historical, not a forecast':'Need at least 30 completed returns'],...['yes','no'].map(side=>[side.toUpperCase()+' execution buffer',cents(i.execution_buffer[side].value),i.execution_buffer[side].samples+' quote intervals · need 10 · 95th percentile'])].map(([label,value,note])=>'<div><span>'+label+'</span><strong>'+value+'</strong><small>'+note+'</small></div>').join('');
  const cal=i.calibration;
  $('calibration-status').textContent=cal.status+'. '+cal.resolved_markets+' resolved forecasts; '+cal.pending_markets+' awaiting results.'+(Number.isFinite(cal.brier_score)?' Brier score: '+cal.brier_score.toFixed(4)+' (lower is better).':' No outcome-based error estimate yet.');
}
function syncOverrideFields(){
  const form=$('settings-form');
  for(const [mode,field,id,active] of [['volatility_mode','manual_volatility','manual-volatility-field','manual'],['drift_mode','drift','manual-drift-field','manual'],['slippage_mode','slippage','manual-slippage-field','manual']]){
    const manual=form.elements.namedItem(mode).value===active;
    $(id).hidden=!manual; form.elements.namedItem(field).disabled=!manual;
  }
}
const percentFields=new Set(['kelly_fraction','max_position_pct','max_exposure_pct','min_edge','probability_haircut','manual_volatility','drift','take_profit','stop_loss']);
function openSettings(){
  if(!state)return;
  const form=$('settings-form');
  for(const [key,value]of Object.entries(state.config)){const field=form.elements.namedItem(key);if(field)field.value=percentFields.has(key)&&key!=='kelly_fraction'?+(value*100).toFixed(4):key==='slippage'?value*100:value;}
  form.elements.bankroll.disabled=state.portfolio.positions.length>0;
  syncOverrideFields();renderInputs();$('settings-error').textContent='';$('settings-dialog').showModal();
}
async function inspectDecision(id){
  const requestedAsset=selectedAsset;
  try{const response=await fetch(apiUrl('/api/decision?id='+encodeURIComponent(id)));const d=await response.json();if(requestedAsset!==selectedAsset)return;if(!response.ok)throw new Error(d.error);
    const s=d.sizing||{};
    $('detail-content').innerHTML='<p class="detail-reason">'+badge(d.action)+' · '+clockTime(d.time)+'<br><br>'+esc(d.reason)+'</p><div class="detail-grid">'+[[state.asset.symbol+' reference',money(d.spot)],['Target strike',money(d.strike)],['Model P(YES)',pct(d.probability)],['Kalshi YES midpoint',pct(d.market_probability)],['Kalshi YES bid / ask',pct(d.yes_bid)+' / '+pct(d.yes_ask)],['Annualized volatility',pct(d.volatility)],['Executable ask',cents(s.ask)],['Net edge',Number.isFinite(s.edge)?(s.edge*100).toFixed(2)+' pp':'—'],['Suggested contracts',number(s.contracts)],['All-in paper stake',money(s.cost)]].map(([key,value])=>'<div><span>'+key+'</span><strong>'+value+'</strong></div>').join('')+'</div><p class="detail-reason">Market: '+esc(d.ticker)+'<br>Source: '+esc(d.source)+' · '+clockTime(d.source_time)+'</p><details><summary>Full saved inputs and settings</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>';
    $('detail-dialog').showModal();
  }catch(error){toast(error.message,true);}
}
document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.page)));
$('view-all').addEventListener('click',()=>navigate('decisions'));
$('settings-open').addEventListener('click',openSettings);$('settings-side').addEventListener('click',openSettings);
$('method-open').addEventListener('click',()=>$('method-dialog').showModal());
document.querySelectorAll('[data-close]').forEach(el=>el.addEventListener('click',()=>$(el.dataset.close).close()));
document.querySelectorAll('dialog').forEach(el=>el.addEventListener('click',event=>{if(event.target===el){const box=el.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)el.close();}}));
document.querySelectorAll('[data-chart]').forEach(el=>el.addEventListener('click',()=>{chartMode=el.dataset.chart;document.querySelectorAll('[data-chart]').forEach(button=>button.classList.toggle('selected',button===el));if(state)render();}));
document.querySelectorAll('[data-book]').forEach(el=>el.addEventListener('click',()=>{bookSide=el.dataset.book;document.querySelectorAll('[data-book]').forEach(button=>button.classList.toggle('selected',button===el));if(state)renderBook();}));
$('auto-button').addEventListener('click',async()=>{if(state&&await mutate('/api/auto',{enabled:!state.auto_paper}))toast(state.auto_paper?'Paper bot started. Signals will create simulated positions.':'Paper bot paused. Market monitoring continues.');});
$('paper-buy').addEventListener('click',async()=>{if(await mutate('/api/paper/buy'))toast('Paper position opened. No real order was sent.');});
$('decision-filter').addEventListener('change',renderLog);
$('settings-form').addEventListener('change',syncOverrideFields);
$('reset-paper').addEventListener('click',()=>{$('reset-error').textContent='';$('reset-dialog').showModal();});
$('reset-confirm').addEventListener('click',async()=>{if(await mutate('/api/paper/reset')){$('reset-dialog').close();toast('New paper run: $100 cash. Previous run archived; bot paused.');}});
$('settings-form').addEventListener('submit',async event=>{event.preventDefault();const update={};for(const [key,value]of new FormData(event.target)){if(['volatility_mode','drift_mode','slippage_mode'].includes(key))update[key]=value;else update[key]=percentFields.has(key)&&key!=='kelly_fraction'?Number(value)/100:key==='slippage'?Number(value)/100:Number(value);}
  if(await mutate('/api/config',update)){$('settings-dialog').close();toast('Strategy saved. New settings are active.');}
});
document.addEventListener('click',async event=>{const run=event.target.closest('[data-run]');if(run){const requestedAsset=selectedAsset;try{const response=await fetch(apiUrl('/api/paper/run?id='+run.dataset.run));const data=await response.json();if(requestedAsset!==selectedAsset)return;if(!response.ok)throw new Error(data.error);$('detail-content').innerHTML='<p class="detail-reason">Archived paper run #'+data.id+' · cash '+money(data.cash)+'</p><pre>'+esc(JSON.stringify(data,null,2))+'</pre>';$('detail-dialog').showModal();}catch(error){toast(error.message,true);}}const row=event.target.closest('[data-decision]');if(row)inspectDecision(row.dataset.decision);const close=event.target.closest('[data-close-position]');if(close&&await mutate('/api/paper/close',{id:Number(close.dataset.closePosition)}))toast('Paper position closed at visible bid depth.');});
document.addEventListener('keydown',event=>{const row=event.target.closest('[data-decision]');if(row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();inspectDecision(row.dataset.decision);}});
async function poll(){await refresh();setTimeout(poll,500);}

document.querySelectorAll('[data-asset]').forEach(el=>el.addEventListener('click',()=>selectAsset(el.dataset.asset)));
$('asset-select').addEventListener('change',event=>selectAsset(event.target.value));
window.addEventListener('popstate',()=>selectAsset(new URLSearchParams(location.search).get('asset')==='gold'?'gold':'btc',false));
updateAssetControls();

let researchState=null,researchBusy=false,replayTimer=null,replayIndex=0,replayKey='';
async function refreshResearch(asset=selectedAsset){
  if(researchBusy)return;researchBusy=true;
  try{const response=await fetch(apiUrl('/api/research',asset),{cache:'no-store'});const data=await response.json();if(asset!==selectedAsset)return;if(!response.ok)throw new Error(data.error);researchState=data;renderResearch();}
  catch(error){$('research-job-status').textContent=error.message;}
  finally{researchBusy=false;}
}
function stopReplay(){if(replayTimer)clearInterval(replayTimer);replayTimer=null;if($('replay-play'))$('replay-play').textContent='Play replay';}
function selectedReport(){return researchState?.reports?.find(r=>r.model===$('research-report').value);}
function renderResearch(){
  if(!researchState)return;const r=researchState,s=r.stats,live=r.live;
  $('research-feed-note').textContent=r.feed_note+(s.last?' Last receipt '+Math.max(0,serverNow()-s.last).toFixed(1)+'s ago.':' Waiting for the first quote.');$('research-history-note').textContent=r.history_note+' Raw snapshots retain the top 10 levels per side for '+s.retention_days+' days.';
  $('research-count').textContent=number(s.snapshots);$('research-cadence').textContent=Number.isFinite(s.actual_interval_ms)?s.actual_interval_ms.toFixed(0)+' ms':'Collecting';$('research-pairs').textContent=number(r.labeled_examples||0);$('research-storage').textContent=s.storage_mb.toFixed(1)+' MB';
  $('research-export').href=apiUrl('/api/research/export.csv');
  $('research-horizon').value=r.settings.horizon;$('research-interior').checked=r.settings.interior;$('research-range-filter').checked=r.settings.range_filter;
  const fresh=live && serverNow()<live.target_time;
  $('markov-price').textContent=fresh?cents(live.price):'Collecting';$('markov-basis').textContent=fresh?(live.basis.startsWith('global')?'GLOBAL FALLBACK':'STATE MODEL'):'COLLECTING';
  $('markov-support').textContent=fresh?'Forecast for '+clockTime(live.target_time)+' · '+live.support+' observations in this state; '+live.total_examples+' total · '+live.basis:'Need 100 matured, valid pairs and a current quote inside the selected price band.';
  for(const [i,label]of ['down','flat','up'].entries())$('markov-'+label).textContent=fresh?pct(live.probabilities[i]):'—';
  const matrix=r.matrix||[[0,0,0],[0,0,0],[0,0,0]],labels=['Down','Flat','Up'];
  $('transition-matrix').innerHTML='<div class="table-wrap"><table><thead><tr><th>FROM / TO</th>'+labels.map(l=>'<th>'+l+'</th>').join('')+'</tr></thead><tbody>'+matrix.map((row,i)=>{const total=row.reduce((a,b)=>a+b,0);return '<tr><td>'+labels[i]+'</td>'+row.map(n=>'<td title="'+n+' observed transitions">'+(total?pct(n/total):'—')+'</td>').join('')+'</tr>';}).join('')+'</tbody></table></div>';
  $('lstm-price').textContent=r.lstm_live&&serverNow()<r.lstm_live.target_time?cents(r.lstm_live.price):'Not trained';
  $('lstm-live-note').textContent=r.lstm_live?'Last trained horizon '+r.lstm_live.horizon+'s; forecast for '+clockTime(r.lstm_live.target_time):'Needs 600 valid 20-snapshot sequences plus 48+ hours of prior reference context. Missing data stay pending.';
  $('research-low').textContent=money(r.range?.low);$('research-high').textContent=money(r.range?.high);$('research-range-position').textContent=pct(r.range?.position);
  $('reference-history-status').textContent=number(s.history_candles)+' completed reference candles · '+s.history_hours.toFixed(1)+' hours between first and last observations. '+(s.history_start?'From '+new Date(s.history_start*1000).toLocaleString()+'.':'No backfill yet.');
  $('research-job-status').textContent=(r.job.kind||'Research')+': '+r.job.status+' · '+(r.job.message||'Ready')+(r.job.status==='running'?' ('+pct(r.job.progress,0)+')':'')+(r.error?' · '+r.error:'');
  document.querySelectorAll('.research-job').forEach(el=>el.disabled=r.job.status==='running');
  const report=selectedReport(),complete=report?.status==='complete'&&report.metrics;
  $('backtest-empty').hidden=Boolean(complete);$('backtest-results').hidden=!complete;
  if(!complete){$('backtest-empty').textContent=report?.reason||'Collect observations, then run an experiment. No synthetic performance is shown.';return;}
  const m=report.metrics;$('backtest-mae').textContent=m.mae_cents.toFixed(3)+'¢';$('backtest-baseline').textContent=m.no_change_mae_cents.toFixed(3)+'¢';$('backtest-accuracy').textContent=pct(m.direction_accuracy);$('backtest-pnl').textContent=money(report.net_pnl);
  $('backtest-split').textContent=report.split+' · '+report.train_examples+' train / '+report.test_examples+' test observations · '+report.trades+' non-overlapping simulated trades · flat-direction baseline '+pct(m.flat_baseline_accuracy)+'.';
  $('backtest-description').textContent=report.model.toUpperCase()+' · '+report.horizon+' second target · forecast error and execution replay';$('backtest-note').textContent=report.note;
  const key=selectedAsset+':'+report.model+':'+report.created;if(key!==replayKey){stopReplay();replayKey=key;replayIndex=Math.max(0,report.replay.length-1);}
  $('replay-slider').max=Math.max(0,report.replay.length-1);$('replay-slider').value=replayIndex;renderReplay();
  $('backtest-sample-table').innerHTML='<div class="table-wrap"><table><thead><tr><th>FORECAST TIME</th><th>CURRENT YES MID</th><th>FORECAST</th><th>OBSERVED AT HORIZON</th><th>ERROR</th></tr></thead><tbody>'+report.replay.slice(-8).map(p=>'<tr><td>'+clockTime(p.time)+'</td><td>'+cents(p.current)+'</td><td>'+cents(p.predicted)+'</td><td>'+cents(p.actual)+'</td><td>'+((p.predicted-p.actual)*100).toFixed(3)+'¢</td></tr>').join('')+'</tbody></table></div>';
}
function renderReplay(){
  const report=selectedReport();if(!report?.replay?.length)return;
  replayIndex=Math.min(replayIndex,report.replay.length-1);const points=report.replay.slice(0,replayIndex+1),p=points[points.length-1];
  $('replay-slider').value=replayIndex;$('replay-time').textContent=(replayIndex+1)+' / '+report.replay.length;
  $('replay-detail').textContent='Made '+clockTime(p.time)+'; observed '+clockTime(p.target_time)+'. Current '+cents(p.current)+', forecast '+cents(p.predicted)+', actual '+cents(p.actual)+'.'+(Number.isFinite(p.imbalance)?' Queue imbalance '+pct(p.imbalance)+'; spread '+cents(p.spread)+'.':'');
  const all=report.replay,values=all.flatMap(p=>[p.predicted,p.actual]);let min=Math.min(...values),max=Math.max(...values);const pad=Math.max(.002,(max-min)*.1);min-=pad;max+=pad;
  const x=i=>18+i/Math.max(1,all.length-1)*700,y=v=>15+(max-v)/(max-min)*190;
  let svg='';for(let i=0;i<5;i++){const v=min+(max-min)*i/4;svg+='<line class="gridline" x1="18" x2="718" y1="'+y(v)+'" y2="'+y(v)+'"/><text x="730" y="'+(y(v)+4)+'">'+cents(v)+'</text>';}
  for(const [key,color]of [['predicted','#b3f2cf'],['actual','#a8b4dc']])svg+='<polyline fill="none" stroke="'+color+'" stroke-width="2" points="'+points.map((p,i)=>x(i)+','+y(p[key])).join(' ')+'"/>';
  $('research-replay-chart').innerHTML=svg;
}
for(const id of ['research-horizon','research-interior','research-range-filter'])$(id).addEventListener('change',async()=>{stopReplay();if(await mutate('/api/research/settings',{horizon:Number($('research-horizon').value),interior:$('research-interior').checked,range_filter:$('research-range-filter').checked}))refreshResearch();});
document.querySelectorAll('[data-job]').forEach(el=>el.addEventListener('click',async()=>{if(await mutate('/api/research/job',{kind:el.dataset.job})){if(['markov','lstm'].includes(el.dataset.job)){$('research-report').value=el.dataset.job;replayKey='';}refreshResearch();}}));
$('research-report').addEventListener('change',()=>{replayKey='';renderResearch();});
$('replay-slider').addEventListener('input',event=>{stopReplay();replayIndex=Number(event.target.value);renderReplay();});
$('replay-play').addEventListener('click',()=>{if(replayTimer){stopReplay();return;}const report=selectedReport();if(!report?.replay?.length)return;if(replayIndex>=report.replay.length-1)replayIndex=0;$('replay-play').textContent='Pause replay';replayTimer=setInterval(()=>{if(page!=='research'){stopReplay();return;}replayIndex++;if(replayIndex>=report.replay.length){replayIndex=report.replay.length-1;stopReplay();}renderReplay();},500);});

$('asset-content').hidden=true;
$('asset-breadcrumb').textContent=assetLabels[selectedAsset].name;
$('asset-heading').textContent=assetLabels[selectedAsset].symbol+' / 15 MINUTE';
renderConnection();poll();setInterval(updateTimer,1000);
