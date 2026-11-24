const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let JSDOM;try{({JSDOM}=require('jsdom'));}catch{}
const risk=require('../desk/static/nba-risk.js');
const root=path.resolve(__dirname,'..');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};

function setup(saved=null){
 const now=Date.now(),stamp=()=>new Date().toISOString(),date=new Date().toLocaleDateString('en-CA');
 const game={game_id:'1',home:'BOS',away:'NYK',date,tipoff:new Date(now+3600000).toISOString(),markets:[],prediction:{home_win_probability:.6,away_win_probability:.4}};
 const dom=new JSDOM(fs.readFileSync(path.join(root,'desk/static/nba.html'),'utf8'),{url:'http://127.0.0.1:8787/nba',runScripts:'outside-only'});
 const w=dom.window,errors=[],requests=[],settingsRequests=[];
 w.addEventListener('error',event=>errors.push(event.error));
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
 w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
 w.setInterval=()=>0;
 if(saved)w.localStorage.setItem('signaldesk.nba.risk.v1',JSON.stringify(saved));
 w.fetch=async(url,options={})=>{
  let data;
  if(url==='/api/nba/risk'){
   const body=JSON.parse(options.body);settingsRequests.push(body.settings);data={generated_at:stamp(),model_id:'test',settings:body.settings,
    summary:{proposed_cost:0,proposed_fraction:0,existing_exposure:0,total_exposure_after:0,total_fraction_after:0,allocated_games:0,eligible_games:0},allocations:{},previews:{},reasons:{},
    contracts:[{game_id:'1',ticker:'NBA-1',yes_team:'BOS'}],positions:body.positions.map(p=>({...p,entry_cost:p.quantity*p.entry_price+p.entry_fees,max_loss:p.quantity*p.entry_price+p.entry_fees,status:'watching',quote_at:stamp()}))};
  }else if(url==='/api/nba/stop-plan'){
   const body=JSON.parse(options.body);requests.push(body);const good=body.target_bid>=.7;
   data={...body,generated_at:stamp(),feasible:good,stop_bid:good?.59:null,net_at_stop:good?-5.38:null,net_at_target:good?5.85:.72,
    break_even_bid:.65,net_reward_risk:good?1.087:null,two_wins_one_loss:good?6.32:null,entry_fees:body.entry_fees??1.68,entry_fee_basis:body.entry_fees==null?'estimated':'entered',
    minimum_stop_distance:.01,history:{},gap_bid:good?.57:null,net_at_gap_stop:good?-7.4:null,full_loss:62.18,hold_minutes:body.settings.hold_minutes,
    quote_fresh:true,quote_at:stamp(),current_bid_vwap:.68,reasons:good?[]:['The target is too small after costs.'],warnings:[]};
  }else if(url==='/api/nba/fair-prices')data={generated_at:stamp(),served_at:stamp(),board_today:date,model_id:'test',games:[game]};
  else if(['/api/nba/recordings','/api/nba/paper'].includes(url))data={status:'not_started'};
  else data=null;
  return {ok:true,json:async()=>data};
 };
 w.eval(fs.readFileSync(path.join(root,'desk/static/nba-risk.js'),'utf8')+'\n'+fs.readFileSync(path.join(root,'desk/static/nba.js'),'utf8'));
 const el=id=>w.document.getElementById(id);
 const input=(form,key,value)=>{form.elements[key].value=value;form.elements[key].dispatchEvent(new w.Event('input',{bubbles:true}));};
 const submit=form=>form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 const stored=()=>JSON.parse(w.localStorage.getItem('signaldesk.nba.risk.v1'));
 return {dom,w,el,input,submit,stored,requests,settingsRequests,errors};
}

test('entry calculator survives refresh, rejects a poor scalp, saves without cash changes, and transfers to actual fill review',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();x.w.document.querySelector('[data-stop-game="1"]').click();
  const form=x.el('nba-stop-form');x.input(form,'entry_price','60');x.input(form,'quantity','100');
  assert.equal(form.elements.target_bid.value,'70','Entry updates the default target');
  x.el('nba-refresh').click();await flush();assert.equal(form.elements.entry_price.value,'60');assert.ok(x.el('nba-stop-dialog').open);
  x.input(form,'target_bid','65');x.submit(form);await flush();
  assert.equal(x.el('nba-stop-save').disabled,true);assert.match(x.el('nba-stop-result').textContent,/does not meet/);
  x.input(form,'target_bid','70');assert.equal(x.el('nba-stop-result').textContent,'');
  x.submit(form);await flush();assert.equal(x.el('nba-stop-save').disabled,false);
  assert.equal(x.requests.at(-1).entry_price,.6);assert.equal(x.requests.at(-1).quantity,100);
  x.el('nba-stop-save').click();await flush();
  assert.equal(x.stored().settings.cash,1000);assert.equal(x.stored().positions.length,0);
  assert.equal(x.stored().stop_plans['1'].result.stop_bid,.59);
  assert.match(x.el('nba-games').textContent,/Saved individual exit plan/);
  x.w.document.querySelector('[data-stop-game="1"]').click();assert.equal(form.elements.entry_price.value,'60');
  x.submit(form);await flush();x.el('nba-stop-fill').click();
  assert.ok(x.el('nba-position-dialog').open);assert.equal(x.el('nba-position-form').elements.stop_bid.value,'59');
  assert.equal(x.el('nba-position-form').elements.route.value,'NBA-1|yes');
  assert.equal(x.stored().positions.length,0,'Opening actual-fill review never records a fill');
  assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('an actual position can explicitly raise its local stop to fee-aware break-even without recording another fill',{skip:!JSDOM},async()=>{
 const opened=new Date(Date.now()-60000).toISOString();
 const x=setup({version:2,settings:{...risk.defaults,cash:900},positions:[{id:'p',game_id:'1',ticker:'NBA-1',side:'yes',quantity:100,entry_price:.6,entry_fees:1.68,stop_bid:.55,target_bid:.7,hold_minutes:20,opened_at:opened,state:'open'}]});
 try{
  await flush();x.w.document.querySelector('[data-stop-position="p"]').click();
  const form=x.el('nba-stop-form');assert.equal(form.elements.entry_kind.value,'filled');assert.equal(form.elements.entry_price.disabled,true);
  x.submit(form);await flush();assert.equal(x.el('nba-stop-breakeven').hidden,false);
  x.el('nba-stop-breakeven').click();await flush();
  assert.equal(x.stored().positions[0].stop_bid,.65);assert.equal(x.stored().positions.length,1);assert.equal(x.stored().settings.cash,900);
  assert.equal(x.stored().positions[0].opened_at,opened,'The original holding deadline does not restart');
  assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('a calculated loss stop cannot silently loosen an existing tighter stop',{skip:!JSDOM},async()=>{
 const x=setup({version:2,settings:risk.defaults,positions:[{id:'p',game_id:'1',ticker:'NBA-1',side:'yes',quantity:100,entry_price:.6,entry_fees:1.68,stop_bid:.62,target_bid:.7,hold_minutes:20,opened_at:new Date().toISOString(),state:'open'}]});
 try{
  await flush();x.w.document.querySelector('[data-stop-position="p"]').click();x.submit(x.el('nba-stop-form'));await flush();
  assert.equal(x.el('nba-stop-save').disabled,true);assert.match(x.el('nba-stop-result').textContent,/loosen your existing stop/);
  assert.equal(x.el('nba-stop-breakeven').hidden,false,'An explicit raise can remain available');
  assert.equal(x.stored().positions[0].stop_bid,.62);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('an expired calculation cannot be saved as a newly reviewed plan',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();const originalFetch=x.w.fetch;
  x.w.fetch=async(url,options)=>{
   const response=await originalFetch(url,options);
   if(url!=='/api/nba/stop-plan')return response;
   const result=await response.json();return {ok:true,json:async()=>({...result,generated_at:new Date(Date.now()-21000).toISOString()})};
  };
  x.w.document.querySelector('[data-stop-game="1"]').click();const form=x.el('nba-stop-form');
  x.input(form,'entry_price','60');x.input(form,'quantity','100');x.submit(form);await flush();
  x.el('nba-stop-save').click();await flush();
  assert.match(x.el('nba-stop-error').textContent,/Recalculate/);assert.equal(x.stored(),null);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});
test('loss ceilings serialize as portfolio fractions and the full-loss choice remains a string',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();const form=x.el('nba-risk-form');form.elements.max_loss_fraction.value='2';form.elements.scalp_hit_rate.value='66.67';form.elements.risk_loss_basis.value='full_loss';
  x.submit(form);await flush();
  assert.equal(x.settingsRequests.at(-1).max_loss_fraction,.02);assert.equal(x.settingsRequests.at(-1).risk_loss_basis,'full_loss');
  assert.equal(x.stored().settings.max_model_market_gap,.15);assert.equal(x.stored().settings.max_loss_fraction,.02);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});
test('show all today restores games hidden by search and edge filters',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();x.el('nba-search').value='not-a-team';x.el('nba-search').dispatchEvent(new x.w.Event('input',{bubbles:true}));
  assert.equal(x.w.document.querySelectorAll('.nba-game').length,0);assert.match(x.el('nba-coverage').textContent,/1 hidden/);
  x.el('nba-show-today').click();assert.equal(x.w.document.querySelectorAll('.nba-game').length,1);assert.equal(x.el('nba-search').value,'');
  x.w.document.querySelector('[data-nba-filter="edges"]').click();assert.equal(x.w.document.querySelectorAll('.nba-game').length,0);
  x.el('nba-show-today').click();assert.equal(x.w.document.querySelectorAll('.nba-game').length,1);assert.match(x.el('nba-visible-count').textContent,/1 shown \/ 1/);
  assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});
test('two-number purchase entry accepts dollar prices, estimates fees, and saves only a local tracker',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();x.el('nba-trade-game').value='1|NBA-1|no';x.el('nba-add-trade').click();
  const f=x.el('nba-position-form');assert.ok(!x.el('nba-purchase-advanced').open);assert.match(x.el('nba-purchase-route').textContent,/NO on BOS/);
  x.input(f,'entry_price','0.43');x.input(f,'purchase_amount','5');
  assert.equal(f.elements.quantity.value,'11');assert.equal(f.elements.entry_fees.value,'0.19');
  assert.match(x.el('nba-purchase-summary').textContent,/cost \$4.92/);
  assert.equal(x.stored(),null);assert.ok(!x.el('nba-sizing-details').open);
  x.submit(f);await flush();
  const p=x.stored().positions[0];assert.equal(p.quantity,11);assert.equal(p.entry_price,.43);assert.equal(p.side,'no');assert.equal(p.quantity_estimated,true);
  assert.equal(p.reported_amount,5);assert.equal(p.entry_fees,.19);assert.equal(p.stop_bid,.38);assert.equal(p.target_bid,.53);
  assert.match(p.exit_plan_warning,/needs review/);assert.equal(x.stored().settings.cash,1000,'Estimated fills do not silently alter account cash');
  assert.equal(x.requests.at(-1).entry_price,.43);assert.equal(x.requests.at(-1).side,'no');
  assert.match(x.el('nba-risk-held').textContent,/estimated cost/);assert.match(x.el('nba-risk-held').textContent,/needs review/);
  assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('route buttons select the purchased side and percent prices get automatic fee-aware exits',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();x.w.document.querySelector('[data-fill-route="NBA-1|yes"]').click();
  const f=x.el('nba-position-form');x.input(f,'purchase_amount','5');x.input(f,'entry_price','60%');
  x.submit(f);await flush();
  assert.equal(x.stored().positions[0].entry_price,.6);assert.equal(x.stored().positions[0].stop_bid,.59);
  assert.equal(x.stored().positions[0].quantity,8);assert.equal(x.stored().positions[0].exit_plan_warning,'');
  assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('advanced actual fills preserve supplied contracts, fees and exits without a spend estimate',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();x.w.document.querySelector('[data-fill-route="NBA-1|yes"]').click();const f=x.el('nba-position-form');
  x.input(f,'entry_price','43¢');x.input(f,'quantity','12');x.input(f,'entry_fees','0');x.input(f,'stop_bid','38');x.input(f,'target_bid','53');
  x.submit(f);await flush();const p=x.stored().positions[0];assert.equal(p.quantity,12);assert.equal(p.entry_fees,0);assert.equal(p.quantity_estimated,false);
  assert.equal(x.requests.length,0);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('an unavailable stop calculator still records a real purchase and labels default exit rules',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();const original=x.w.fetch;x.w.fetch=(url,options)=>url==='/api/nba/stop-plan'?Promise.reject(Error('offline')):original(url,options);
  x.w.document.querySelector('[data-fill-route="NBA-1|yes"]').click();const f=x.el('nba-position-form');x.input(f,'entry_price','43%');x.input(f,'purchase_amount','5');
  x.submit(f);await flush();assert.match(x.stored().positions[0].exit_plan_warning,/unavailable/);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});

test('edits during an exit calculation cannot save a mismatched position',{skip:!JSDOM},async()=>{
 const x=setup();try{
  await flush();const original=x.w.fetch;let release;
  x.w.fetch=(url,options)=>url==='/api/nba/stop-plan'?new Promise(resolve=>{release=()=>resolve(original(url,options));}):original(url,options);
  x.w.document.querySelector('[data-fill-route="NBA-1|yes"]').click();const f=x.el('nba-position-form');x.input(f,'entry_price','43%');x.input(f,'purchase_amount','5');
  x.submit(f);await flush();x.input(f,'entry_price','44%');release();await flush();
  assert.equal(x.stored(),null);assert.match(x.el('nba-position-error').textContent,/details changed/);assert.deepEqual(x.errors,[]);
 }finally{x.dom.window.close();}
});
