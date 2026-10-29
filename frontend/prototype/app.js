(function(){
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const fmt=(n,d=2)=>Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const sgn=(n,d=2)=>(n>=0?'+':'-')+fmt(Math.abs(n),d);
const cls=n=>n>=0?'up':'dn';
function rng(seed){let s=seed>>>0;return()=>{s=(Math.imul(s,1664525)+1013904223)>>>0;return s/4294967296}}
function hash(str){let h=2166136261;for(const c of str){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
const col=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const two=n=>String(n).padStart(2,'0');
const clock=d=>two(d.getHours())+':'+two(d.getMinutes())+':'+two(d.getSeconds());

/* ---------- demo data ---------- */
const MK=[
 {s:'BTC/USDT',p:64820.4,c:2.31,seed:11,d:2,v:'1.82x'},
 {s:'ETH/USDT',p:3412.65,c:1.72,seed:23,d:2,v:'1.31x'},
 {s:'SOL/USDT',p:168.42,c:4.12,seed:37,d:2,v:'2.05x'},
 {s:'XRP/USDT',p:0.5821,c:-0.64,seed:41,d:4,v:'0.88x'},
 {s:'DOGE/USDT',p:0.1387,c:3.4,seed:53,d:4,v:'1.47x'}];
const mk=s=>MK.find(m=>m.s===s);
function genCandles(m,n){
  const r=rng(m.seed*977),out=[];let p=m.p*0.9,t=Date.now()-n*900000;
  for(let i=0;i<n;i++){const drift=(Math.sin(i/17+m.seed)*0.0011)+0.0004,vol=0.004+r()*0.006;
    const o=p,c=o*(1+drift+(r()-0.5)*vol*2),h=Math.max(o,c)*(1+r()*vol*0.8),l=Math.min(o,c)*(1-r()*vol*0.8);
    out.push({t:t+i*900000,o,h,l,c,v:(0.6+r()*1.4)*(1+Math.abs(c-o)/o*60)});p=c}
  const k=m.p/out[n-1].c;out.forEach(x=>{x.o*=k;x.h*=k;x.l*=k;x.c*=k});return out}
MK.forEach(m=>{m.cs=genCandles(m,160);m.open=m.p/(1+m.c/100)});
const BOTS=[
 {id:'001',ex:'Binance',m:'BTC/USDT',st:'Trend AI',v:'2.1',tf:'15m',risk:0.5,mode:'PAPER',state:'RUNNING',pnl:184.3,trades:41},
 {id:'002',ex:'Bybit',m:'ETH/USDT',st:'Mean Reversion',v:'1.2',tf:'1h',risk:0.5,mode:'PAPER',state:'RUNNING',pnl:61.8,trades:27},
 {id:'003',ex:'OKX',m:'SOL/USDT',st:'Breakout AI',v:'1.8',tf:'30m',risk:0.4,mode:'PAPER',state:'RUNNING',pnl:38.1,trades:19}];
const POS=[
 {m:'BTC/USDT',side:'LONG',entry:63940.2,qty:0.062,lev:3,stop:62420,tp:67800,st:'Trend AI',bot:'001'},
 {m:'ETH/USDT',side:'LONG',entry:3361.1,qty:0.9,lev:2,stop:3240,tp:3590,st:'Mean Reversion',bot:'002'},
 {m:'SOL/USDT',side:'LONG',entry:161.8,qty:9,lev:2,stop:154.5,tp:179,st:'Breakout AI',bot:'003'}];
const STRATS=[
 {n:'Trend AI',vs:['1.0','1.1','2.0','2.1'],v:'2.1',risk:'Medium',ind:'EMA, ADX, market structure, momentum',tf:'15m–4h',par:{ema_fast:20,ema_slow:50,adx_min:25,atr_stop:2.0},desc:'Follows established trends and sits out ranges. Entries need EMA alignment and ADX confirmation.'},
 {n:'Momentum AI',vs:['1.0','1.2','1.4'],v:'1.4',risk:'Medium–High',ind:'RSI, MACD, volume, momentum',tf:'5m–1h',par:{rsi_len:14,macd:'12/26/9',vol_mult:1.5},desc:'Buys strength when volume confirms a momentum expansion.'},
 {n:'Mean Reversion',vs:['1.0','1.2'],v:'1.2',risk:'Low',ind:'Bollinger Bands, VWAP, RSI, deviation',tf:'30m–4h',par:{bb_len:20,bb_dev:2.0,rsi_len:14,z_entry:2.0},desc:'Fades stretched moves back toward VWAP in range-bound markets.'},
 {n:'Breakout AI',vs:['1.0','1.4','1.8'],v:'1.8',risk:'High',ind:'Support/resistance, ATR, volume, volatility',tf:'15m–1h',par:{lookback:48,atr_len:14,vol_mult:1.8},desc:'Trades range escapes with volume and volatility expansion.'},
 {n:'AI Strategy',vs:['0.3'],v:'0.3',risk:'Experimental',ind:'Market Intelligence layer',tf:'1h–1d',par:{min_conf:0.7,max_regime_age:12},desc:'Consumes the full Market State. Experimental: cannot be activated for live trading.'}];
const EX={
 Binance:{sym:'BTCUSDT',tick:'0.01',qty:'0.00001',min:'5 USDT',fee:'0.10% / 0.10%',lev:'up to 20x (demo)',types:'Market, Limit, Stop-Limit, OCO',bal:'12,450.28 USDT',conn:true},
 Bybit:{sym:'BTCUSDT',tick:'0.10',qty:'0.001',min:'1 USDT',fee:'0.10% / 0.055%',lev:'up to 25x (demo)',types:'Market, Limit, Conditional',bal:'4,210.00 USDT',conn:true},
 OKX:{sym:'BTC-USDT',tick:'0.10',qty:'0.0001',min:'1 USDT',fee:'0.08% / 0.10%',lev:'up to 20x (demo)',types:'Market, Limit, Trigger, OCO',bal:'not connected',conn:false}};
const mins=n=>new Date(Date.now()+n*60000);
const hm=d=>two(d.getHours())+':'+two(d.getMinutes());
const EVENTS=[
 {t:120,n:'FOMC rate decision',c:'US',imp:'HIGH',a:'BTC, ETH, all crypto',vol:'Very high',sent:'Neutral',ai:'Outcome is mostly priced in. The press conference tone is the real risk. Strategies reduce size ahead of the release.'},
 {t:150,n:'Fed chair press conference',c:'US',imp:'HIGH',a:'BTC, ETH',vol:'High',sent:'Uncertain',ai:'Expect whipsaw in the first 15 minutes. Mean-reversion entries are paused by the event filter.'},
 {t:310,n:'CPI (YoY)',c:'US',imp:'HIGH',a:'BTC, ETH, SOL',vol:'Very high',sent:'Neutral',ai:'Surprise versus consensus matters more than the absolute number.'},
 {t:470,n:'GDP (QoQ)',c:'US',imp:'MEDIUM',a:'BTC',vol:'Moderate',sent:'Neutral',ai:'Low expected impact unless it diverges sharply from forecast.'},
 {t:700,n:'SOL token unlock (est.)',c:'Crypto',imp:'MEDIUM',a:'SOL',vol:'Moderate',sent:'Bearish',ai:'Scheduled supply increase. Check whether the unlock is already reflected in funding and spreads.'}];
const NEWS=[
 {h:'Spot BTC ETF flows turn positive for third session',src:'Demo Wire',ago:'8m',a:['BTC','ETH','Crypto market'],sent:'Bullish',imp:'HIGH',conf:87,exp:'+0.8% to +1.5% over 4h',react:'+0.9% so far',ai:'Reaction is in line with the expected impact, so the news adds little beyond what price already shows.'},
 {h:'Major exchange reports brief withdrawal delays',src:'Demo Wire',ago:'21m',a:['Exchange risk'],sent:'Bearish',imp:'MEDIUM',conf:71,exp:'Short-term spread widening',react:'Spreads +12% on one venue',ai:'Operational issue, not a solvency signal. Order engine raises slippage allowance on that venue only.'},
 {h:'Layer-1 network upgrade scheduled for next week',src:'Demo Crypto Desk',ago:'1h',a:['SOL'],sent:'Bullish',imp:'MEDIUM',conf:64,exp:'Mild positive drift',react:'None yet',ai:'Upgrade events often sell the news. No standalone buy signal is generated.'},
 {h:'Regulator signals review of stablecoin reserve rules',src:'Demo Policy Watch',ago:'2h',a:['Stablecoins','Crypto market'],sent:'Bearish',imp:'MEDIUM',conf:58,exp:'Uncertain; low until details',react:'Muted',ai:'Wait for detail. Headline alone does not change regime.'}];
const LOGSEED=[
 ['Market data received','sys'],['Trend AI generated BUY signal (conf 0.82)','sig'],['Risk engine approved: size 0.50%, stop attached','risk'],['Order submitted to paper exchange','ord'],['Order accepted','ord'],['Order filled 0.062 BTC @ 63,940.20','ord'],
 ['Event filter armed: FOMC in 2h','risk'],['AI recommended Trend AI (assisted mode, awaiting user)','ai'],['Correlated exposure 0.87 BTC/ETH: warning raised','risk']];
const S={page:'dashboard',exch:'Binance',mode:'PAPER',mkt:'BTC/USDT',range:'1M',halted:false,draw:null,
  ind:{ema:1,sma:0,vwap:1,rsi:1,macd:0,vol:1,sr:1},stratMode:'AI Assisted',activeStrat:'Trend AI',pendingStrat:null,
  logs:[],watch:'Crypto',orderFilter:'All',bt:null,btSel:null,ostate:{minScore:0,trend:'All'}};

/* ---------- indicators ---------- */
const ema=(a,n)=>{const k=2/(n+1);let e=a[0];return a.map(x=>e=x*k+e*(1-k))};
const sma=(a,n)=>a.map((_,i)=>i<n-1?null:a.slice(i-n+1,i+1).reduce((x,y)=>x+y,0)/n);
function rsi(c,n=14){const o=[null];let g=0,l=0;for(let i=1;i<c.length;i++){const d=c[i]-c[i-1];if(i<=n){g+=Math.max(d,0);l+=Math.max(-d,0);o.push(i<n?null:100-100/(1+g/(l||1e-9)))}else{g=(g*(n-1)+Math.max(d,0))/n;l=(l*(n-1)+Math.max(-d,0))/n;o.push(100-100/(1+g/(l||1e-9)))}}return o}
function macd(c){const f=ema(c,12),s=ema(c,26),m=f.map((x,i)=>x-s[i]),sg=ema(m,9);return{m,sg,h:m.map((x,i)=>x-sg[i])}}
function atr(cs,n=14){const t=cs.map((x,i)=>i?Math.max(x.h-x.l,Math.abs(x.h-cs[i-1].c),Math.abs(x.l-cs[i-1].c)):x.h-x.l);return t.slice(-n).reduce((a,b)=>a+b,0)/n}
function vwap(cs){let pv=0,v=0;return cs.map(x=>{pv+=(x.h+x.l+x.c)/3*x.v;v+=x.v;return pv/v})}

/* ---------- canvas drawing ---------- */
function setup(cv){const r=cv.getBoundingClientRect(),d=window.devicePixelRatio||1;cv.width=Math.max(1,r.width*d);cv.height=Math.max(1,r.height*d);const g=cv.getContext('2d');g.setTransform(d,0,0,d,0,0);g.font='10px '+col('--f-mono');return{g,W:r.width,H:r.height}}
function drawCandles(cv,all,opt={}){
  if(!cv)return;const {g,W,H}=setup(cv),n=opt.n||90,cs=all.slice(-n),ind=opt.ind||S.ind,off=all.length-cs.length;
  const subs=(ind.rsi?1:0)+(ind.macd?1:0),subH=64,mainH=H-subs*subH-16,pad=58,pw=W-pad,cw=pw/cs.length;
  const up=col('--up'),dn=col('--dn'),grid=col('--line'),mut=col('--dim'),ai=col('--ai'),warn=col('--warn');
  const hi=Math.max(...cs.map(x=>x.h)),lo=Math.min(...cs.map(x=>x.l)),rg=hi-lo||1,top=8,ph=mainH-8;
  const y=p=>top+(hi-p)/rg*ph*0.82;
  g.clearRect(0,0,W,H);g.strokeStyle=grid;g.fillStyle=mut;g.lineWidth=1;
  for(let i=0;i<=4;i++){const p=hi-rg*i/4,yy=y(p);g.beginPath();g.moveTo(0,yy);g.lineTo(pw,yy);g.stroke();g.fillText(fmt(p,p<10?4:2),pw+5,yy+3)}
  if(ind.vol){const mv=Math.max(...cs.map(x=>x.v));cs.forEach((x,i)=>{g.fillStyle=x.c>=x.o?up:dn;g.globalAlpha=.22;const h=x.v/mv*ph*0.16;g.fillRect(i*cw+1,top+ph-h,Math.max(1,cw-2),h)});g.globalAlpha=1}
  if(ind.sr){const sr=[hi-rg*0.04,lo+rg*0.06];g.setLineDash([4,4]);g.strokeStyle=warn;g.globalAlpha=.55;sr.forEach(p=>{g.beginPath();g.moveTo(0,y(p));g.lineTo(pw,y(p));g.stroke()});g.setLineDash([]);g.globalAlpha=1}
  cs.forEach((x,i)=>{const c=x.c>=x.o?up:dn,xx=i*cw+cw/2;g.strokeStyle=c;g.fillStyle=c;g.beginPath();g.moveTo(xx,y(x.h));g.lineTo(xx,y(x.l));g.stroke();const y1=y(x.o),y2=y(x.c);g.fillRect(i*cw+Math.max(1,cw*.18),Math.min(y1,y2),Math.max(1,cw*.64),Math.max(1,Math.abs(y1-y2)))});
  const closes=all.map(x=>x.c),line=(arr,color,dash)=>{g.strokeStyle=color;g.lineWidth=1.4;g.setLineDash(dash||[]);g.beginPath();let s=false;arr.slice(off).forEach((v,i)=>{if(v==null)return;const xx=i*cw+cw/2;s?g.lineTo(xx,y(v)):(g.moveTo(xx,y(v)),s=true)});g.stroke();g.setLineDash([]);g.lineWidth=1};
  if(ind.ema)line(ema(closes,20),ai);if(ind.sma)line(sma(closes,50),warn);if(ind.vwap)line(vwap(all),'#c58bff',[3,3]);
  (opt.marks||[]).forEach(m=>{const i=m.i-off;if(i<0||i>=cs.length)return;const xx=i*cw+cw/2,yy=y(m.p);g.fillStyle=m.k==='in'?up:m.k==='sl'?dn:m.k==='tp'?ai:warn;g.beginPath();if(m.k==='in'){g.moveTo(xx,yy+3);g.lineTo(xx-4,yy+10);g.lineTo(xx+4,yy+10)}else{g.moveTo(xx,yy-3);g.lineTo(xx-4,yy-10);g.lineTo(xx+4,yy-10)}g.fill()});
  const last=cs[cs.length-1].c;g.fillStyle=last>=cs[cs.length-1].o?up:dn;g.fillRect(pw,y(last)-7,pad-2,14);g.fillStyle='#07101a';g.fillText(fmt(last,last<10?4:2),pw+4,y(last)+3);
  let yo=mainH;
  const sub=(label,fn)=>{g.strokeStyle=grid;g.beginPath();g.moveTo(0,yo+4);g.lineTo(W,yo+4);g.stroke();g.fillStyle=mut;g.fillText(label,4,yo+15);fn(yo+10,subH-14);yo+=subH};
  if(ind.rsi)sub('RSI 14',(y0,h)=>{const r=rsi(closes).slice(off),yy=v=>y0+(100-v)/100*h;g.setLineDash([2,3]);[30,70].forEach(v=>{g.beginPath();g.moveTo(0,yy(v));g.lineTo(pw,yy(v));g.stroke()});g.setLineDash([]);g.strokeStyle=ai;g.beginPath();let s=false;r.forEach((v,i)=>{if(v==null)return;s?g.lineTo(i*cw+cw/2,yy(v)):(g.moveTo(i*cw+cw/2,yy(v)),s=true)});g.stroke();g.fillStyle=mut;g.fillText(fmt(r[r.length-1]||0,1),pw+5,y0+h/2)});
  if(ind.macd)sub('MACD',(y0,h)=>{const m=macd(closes),H2=m.h.slice(off),M=m.m.slice(off),mx=Math.max(...H2.map(Math.abs),...M.map(Math.abs))||1,yy=v=>y0+h/2-v/mx*h/2;H2.forEach((v,i)=>{g.fillStyle=v>=0?up:dn;g.globalAlpha=.5;g.fillRect(i*cw+1,Math.min(yy(0),yy(v)),Math.max(1,cw-2),Math.abs(yy(v)-yy(0)));g.globalAlpha=1});g.strokeStyle=ai;g.beginPath();M.forEach((v,i)=>i?g.lineTo(i*cw+cw/2,yy(v)):g.moveTo(cw/2,yy(v)));g.stroke()});
  const t0=cs[0].t,t1=cs[cs.length-1].t;g.fillStyle=mut;g.fillText(new Date(t0).toLocaleDateString('en-US',{month:'short',day:'numeric'}),2,H-3);g.fillText(new Date(t1).toLocaleDateString('en-US',{month:'short',day:'numeric'}),pw-34,H-3)}
function drawLines(cv,series,opt={}){
  if(!cv)return;const {g,W,H}=setup(cv),pad=opt.pad??54,pw=W-pad,all=series.flatMap(s=>s.d),lo=opt.lo??Math.min(...all),hi=opt.hi??Math.max(...all),rg=(hi-lo)||1,top=6,bt=H-6;
  const y=v=>top+(hi-v)/rg*(bt-top),grid=col('--line'),mut=col('--dim');g.clearRect(0,0,W,H);g.strokeStyle=grid;g.fillStyle=mut;
  for(let i=0;i<=3;i++){const v=hi-rg*i/3,yy=y(v);g.beginPath();g.moveTo(0,yy);g.lineTo(pw,yy);g.stroke();g.fillText(opt.f?opt.f(v):fmt(v,0),pw+5,yy+3)}
  series.forEach(s=>{const n=s.d.length,x=i=>i/(n-1)*pw;g.beginPath();s.d.forEach((v,i)=>i?g.lineTo(x(i),y(v)):g.moveTo(x(i),y(v)));
    if(s.fill){const base=opt.base!=null?y(opt.base):bt;g.save();g.lineTo(pw,base);g.lineTo(0,base);g.closePath();const gr=g.createLinearGradient(0,top,0,bt);gr.addColorStop(0,s.fill);gr.addColorStop(1,'transparent');g.fillStyle=gr;g.fill();g.restore();g.beginPath();s.d.forEach((v,i)=>i?g.lineTo(x(i),y(v)):g.moveTo(x(i),y(v)))}
    g.strokeStyle=s.c;g.lineWidth=s.w||1.6;g.setLineDash(s.dash||[]);g.stroke();g.setLineDash([]);g.lineWidth=1;
    if(s.end){g.fillStyle=s.c;g.beginPath();g.arc(x(n-1),y(s.d[n-1]),3,0,7);g.fill()}})}
const spark=(arr,c)=>{const lo=Math.min(...arr),hi=Math.max(...arr),r=hi-lo||1;return `<svg width="64" height="22" viewBox="0 0 64 22" aria-hidden="true"><polyline fill="none" stroke="${c}" stroke-width="1.5" points="${arr.map((v,i)=>(i/(arr.length-1)*62+1).toFixed(1)+','+(20-(v-lo)/r*18).toFixed(1)).join(' ')}"/></svg>`};

/* ---------- series ---------- */
const RANGES={'1D':48,'1W':56,'1M':60,'3M':90,'1Y':120,'All':160};
function equity(range){const n=RANGES[range],r=rng(hash(range)),eq=[],bm=[];let e=1,b=1;const sc=range==='1D'?0.0016:range==='1W'?0.003:0.006;
  for(let i=0;i<n;i++){e*=1+(r()-0.43)*sc;b*=1+(r()-0.47)*sc*1.25;eq.push(e);bm.push(b)}
  const tgt=12450.28/eq[n-1],E=eq.map(v=>v*tgt),k=E[0]/bm[0];return{eq:E,bm:bm.map(v=>v*k)}}
function dd(a){let pk=a[0];return a.map(v=>{pk=Math.max(pk,v);return (v/pk-1)*100})}

/* ---------- UI helpers ---------- */
const pill=(t,c='')=>`<span class="pill ${c}">${t}</span>`;
const dotc=s=>s==='RUNNING'?'':s==='PAUSED'||s==='STARTING'||s==='STOPPING'?'w':s==='ERROR'?'r':'o';
function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');clearTimeout(toast.t);toast.t=setTimeout(()=>e.classList.remove('show'),2600)}
function modal(html){const m=$('#modal');$('#modalBox').classList.remove('wide');$('#modalBox').innerHTML=html;m.classList.add('show');const f=$('#modalBox button,#modalBox input');f&&f.focus()}
function closeModal(){$('#modal').classList.remove('show')}
function log(msg,k='sys'){S.logs.unshift({t:new Date(),m:msg,k});if(S.logs.length>300)S.logs.pop();if(S.page==='logs')renderLogRows()}
function audit(msg){log('AUDIT: '+msg,'audit')}
LOGSEED.forEach((l,i)=>S.logs.push({t:new Date(Date.now()-(LOGSEED.length-i)*37000),m:l[0],k:l[1]}));S.logs.reverse();
const posPnl=p=>{const cur=mk(p.m).p;return (cur-p.entry)*p.qty*(p.side==='LONG'?1:-1)};

/* ---------- pages ---------- */
const PAGES=[['dashboard','Dashboard'],['markets','Markets'],['strategies','Strategies'],['activity','Live Activity'],['pkgs','Strategy Manager'],['backtest','Backtesting'],['paper','Paper Trading'],['live','Live Trading'],['positions','Positions'],['orders','Orders'],['perf','Performance'],['ailab','AI Lab'],['news','News & Events'],['exchange','Exchange Manager'],['risk','Risk Manager'],['logs','Logs'],['settings','Settings']];
const R={};
const mktState=m=>({trend:m.c>1?'BULLISH':m.c<0?'BEARISH':'NEUTRAL',mom:Math.abs(m.c)>2?'STRONG':'MODERATE',vol:Math.abs(m.c)>2?'HIGH':'MEDIUM',liq:m.s==='DOGE/USDT'?'MEDIUM':'HIGH',reg:Math.abs(m.c)>1.5?'TRENDING':'RANGING',conf:Math.round(58+Math.min(30,Math.abs(m.c)*8))});
function intel(m){const s=mktState(m),tc=s.trend==='BULLISH'?'up':s.trend==='BEARISH'?'dn':'';
 return `<div class="kvg">
  ${[['Trend',s.trend,tc],['Momentum',s.mom,s.mom==='STRONG'?'up':''],['Volatility',s.vol,s.vol==='HIGH'?'warn':''],['Volume',m.v+' average',''],['Liquidity',s.liq,''],['Regime',s.reg,'ai'],['AI confidence',s.conf+'%','ai']].map(x=>`<div class="tile"><div class="l">${x[0]}</div><div class="v ${x[2]}">${x[1]}</div></div>`).join('')}</div>
  <p class="note" style="margin:10px 0 0">Confidence is the model's certainty that it has detected this pattern. It is not a probability of profit.</p>`}

R.dashboard=()=>{
  const eq=equity(S.range),m=mk(S.mkt);
  return `
  <div class="row sb"><div class="row">${pill('DEMO DATA','warn')}<span class="mut">Simulated portfolio, not real results</span></div></div>
  <div class="grid kpis">
   ${[['Portfolio value','$12,450.28','+2.34% today','up'],['Today P&amp;L','+$284.21','+2.34%','up'],['Total P&amp;L','+$2,450','+24.5% since start','up'],['Win rate','63.4%','143 closed trades',''],['Max drawdown','3.8%','limit 10%',''],['Active bots','3','all paper','']].map(k=>`<div class="card kpi"><div class="l">${k[0]}</div><div class="v ${k[3]==='up'&&k[0]!=='Win rate'?'up':''}">${k[1]}</div><div class="s ${k[3]||'mut'}">${k[2]}</div></div>`).join('')}
  </div>
  <div class="grid g2">
   <div class="card"><h2>Portfolio performance <span class="tabs" id="rtabs">${Object.keys(RANGES).map(r=>`<button aria-pressed="${r===S.range}" data-range="${r}">${r}</button>`).join('')}</span></h2>
     <canvas id="cEq" style="height:210px"></canvas><canvas id="cDd" style="height:54px;margin-top:4px"></canvas>
     <div class="row mut" style="margin-top:6px;font-size:11px"><span><b class="ai">&#9632;</b> Equity</span><span><b class="mut">&#9632;</b> Benchmark (BTC buy-and-hold, demo)</span><span><b class="dn">&#9632;</b> Drawdown</span></div></div>
   <div class="card"><h2>Market overview ${pill('SIMULATED','warn')}</h2><div style="display:grid;gap:7px">${MK.slice(0,3).map(x=>`<button class="mkt" data-mkt="${x.s}" aria-pressed="${x.s===S.mkt}"><span><span class="n">${x.s}</span><br><span class="mut" style="font-size:11px">vol ${x.v} avg</span></span>${spark(x.cs.slice(-30).map(c=>c.c),x.c>=0?col('--up'):col('--dn'))}<span style="text-align:right"><span class="p num" data-px="${x.s}">$${fmt(x.p,x.d)}</span><br><span class="num ${cls(x.c)}" data-chg="${x.s}">${sgn(x.c)}%</span></span></button>`).join('')}</div></div>
  </div>
  <div class="grid g2">
   <div class="card"><h2>${S.mkt} <span class="row">${pill('15m','')}<button class="btn sm" data-go="markets">Open market</button></span></h2><canvas id="cMain" style="height:300px"></canvas></div>
   <div class="card"><h2>AI market intelligence ${pill('AI','ai')}</h2>${intel(m)}</div>
  </div>
  <div class="grid g4">
   <div class="card"><h2>News &amp; events <button class="btn sm" data-go="news">All</button></h2>${EVENTS.slice(0,3).map(e=>`<div class="row sb" style="padding:5px 0;border-top:1px solid var(--line)"><span><b>${e.n}</b><br><span class="mut mono" style="font-size:11px">${hm(mins(e.t))} &middot; in ${Math.floor(e.t/60)}h ${e.t%60}m</span></span>${pill(e.imp,e.imp==='HIGH'?'dn':'warn')}</div>`).join('')}</div>
   <div class="card"><h2>Active bots <button class="btn sm" data-go="paper">Manage</button></h2>${BOTS.map(b=>`<div class="row sb" style="padding:5px 0;border-top:1px solid var(--line)"><span><b>Bot #${b.id}</b> <span class="mut">${b.m}</span><br><span class="mut" style="font-size:11px">${b.st} v${b.v} &middot; ${b.ex}</span></span><span><i class="dot ${dotc(b.state)}"></i>${b.state}</span></div>`).join('')}</div>
   <div class="card"><h2>Risk status <button class="btn sm" data-go="risk">Details</button></h2>${riskMeters()}</div>
   <div class="card"><h2>Recent paper trades</h2>${recentTrades()}</div>
  </div>`};
function riskMeters(){return [['Daily loss','1.20%','3.00%',40],['Drawdown','3.8%','10%',38],['Exposure','42%','70%',60],['Open positions','3','5',60]].map(r=>`<div class="meter"><div class="row sb"><span class="mut">${r[0]}</span><span class="num">${r[1]} / ${r[2]}</span></div><div class="bar ${r[3]>=80?'d':r[3]>=60?'w':''}"><i style="width:${r[3]}%"></i></div></div>`).join('')+`<div class="row sb"><span class="mut">Status</span>${pill(S.halted?'HALTED':'SAFE',S.halted?'dn':'up')}</div>`}
function recentTrades(){const t=[['BTC/USDT','BUY','63,940.20','+0.0%'],['ETH/USDT','SELL','3,402.10','+$42.60'],['SOL/USDT','SELL','171.30','+$18.90'],['BTC/USDT','SELL','64,310.00','-$21.40']];return t.map(x=>`<div class="row sb" style="padding:4px 0;border-top:1px solid var(--line)"><span><b>${x[0]}</b> <span class="${x[1]==='BUY'?'up':'dn'}">${x[1]}</span></span><span class="num">${x[2]} <span class="${x[3].startsWith('-')?'dn':x[3]==='+0.0%'?'mut':'up'}">${x[3]==='+0.0%'?'open':x[3]}</span></span></div>`).join('')}

R.markets=()=>{const m=mk(S.mkt),ex=EX[S.exch],st=mktState(m);
  const sc=MK.map(x=>({x,s:mktState(x),score:Math.round(40+Math.abs(x.c)*9+(x.cs.length%7))})).filter(r=>r.score>=S.ostate.minScore&&(S.ostate.trend==='All'||r.s.trend===S.ostate.trend));
  const wl={Crypto:['BTC/USDT','ETH/USDT','SOL/USDT'],'High Volatility':['SOL/USDT','DOGE/USDT']};
  return `
  <div class="grid g4">${MK.map(x=>`<button class="mkt" data-mkt="${x.s}" aria-pressed="${x.s===S.mkt}"><span class="n">${x.s}</span><span style="text-align:right"><span class="p num" data-px="${x.s}">$${fmt(x.p,x.d)}</span><br><span class="num ${cls(x.c)}" data-chg="${x.s}">${sgn(x.c)}%</span></span></button>`).join('')}</div>
  <div class="grid g2">
   <div class="card"><h2>${m.s} &middot; ${S.exch} <span class="tabs" id="itabs">${[['ema','EMA 20'],['sma','SMA 50'],['vwap','VWAP'],['vol','Volume'],['sr','S/R'],['rsi','RSI'],['macd','MACD']].map(i=>`<button data-ind="${i[0]}" aria-pressed="${!!S.ind[i[0]]}">${i[1]}</button>`).join('')}</span></h2>
     <canvas id="cMain" style="height:${360+(S.ind.rsi?64:0)+(S.ind.macd?64:0)}px"></canvas>
     <div class="row mut mono" style="font-size:11px;margin-top:4px"><span>ATR(14) <b class="num" id="atrv">${fmt(atr(m.cs),m.d)}</b></span><span>Simulated 15m candles</span></div></div>
   <div style="display:grid;gap:12px;align-content:start">
     <div class="card"><h2>Market intelligence ${pill('AI','ai')}</h2>${intel(m)}</div>
     <div class="card"><h2>Order book ${pill('SIMULATED','warn')}</h2><div class="ob" id="ob"></div></div>
   </div>
  </div>
  <div class="grid g3">
   <div class="card"><h2>Market state</h2><dl class="kv"><dt>Price trend</dt><dd>${st.trend}</dd><dt>Momentum</dt><dd>${st.mom}</dd><dt>Order book</dt><dd>Bid pressure 62%</dd><dt>Open interest</dt><dd class="up">+7.2%</dd><dt>Funding</dt><dd>+0.012%</dd><dt>On-chain</dt><dd>Neutral / Bullish</dd><dt>News</dt><dd>Positive</dd><dt>Macro</dt><dd>Neutral</dd><dt>Upcoming</dt><dd class="warn">FOMC in 2h</dd><dt>Regime</dt><dd class="ai">${st.vol==='HIGH'?'High-vol ':''}${st.trend.toLowerCase()} ${st.reg.toLowerCase()}</dd></dl></div>
   <div class="card"><h2>Derivatives ${pill('DEMO','warn')}</h2><dl class="kv"><dt>Open interest</dt><dd class="up">+7.2%</dd><dt>Funding</dt><dd>+0.012%</dd><dt>Long/short</dt><dd>1.38</dd><dt>Liquidations 24h</dd><dd>$84M</dd><dt>Basis</dt><dd>+6.1% ann.</dd><dt>Positioning</dt><dd class="warn">Crowded long</dd></dl><h2 style="margin-top:14px">On-chain ${pill('INTERFACE ONLY','')}</h2><dl class="kv"><dt>Exchange inflow</dt><dd class="warn">HIGH</dd><dt>Whale activity</dt><dd>HIGH</dd><dt>Stablecoin supply</dt><dd class="up">RISING</dd><dt>Network activity</dt><dd class="up">RISING</dd></dl></div>
   <div class="card"><h2>Recent trades ${pill('SIMULATED','warn')}</h2><div class="scroll"><table class="tbl" id="trades"></table></div></div>
  </div>
  <div class="grid g2">
   <div class="card"><h2>Market scanner <span class="row"><label class="field">Min score <input id="minScore" type="range" min="0" max="90" value="${S.ostate.minScore}"></label><label class="field"><select id="scTrend"><option>All</option><option>BULLISH</option><option>BEARISH</option><option>NEUTRAL</option></select></label></span></h2>
    <div class="scroll"><table class="tbl"><tr><th>Market</th><th>Setup</th><th>Trend</th><th>Spread</th><th>AI score</th></tr>${sc.map(r=>`<tr class="click" data-mkt="${r.x.s}"><td><b>${r.x.s}</b></td><td>${r.s.reg==='TRENDING'?'Trend':'Range'}</td><td class="${r.s.trend==='BULLISH'?'up':r.s.trend==='BEARISH'?'dn':''}">${r.s.trend}</td><td class="num">${(0.01+r.x.seed%5/100).toFixed(2)}%</td><td><b class="num">${r.score}/100</b></td></tr>`).join('')||'<tr><td colspan="5" class="mut">No markets match these filters.</td></tr>'}</table></div></div>
   <div class="card"><h2>Watchlists <span class="tabs">${Object.keys(wl).map(k=>`<button data-wl="${k}" aria-pressed="${k===S.watch}">${k}</button>`).join('')}<button data-act="newwl">+ New</button></span></h2>${(wl[S.watch]||[]).map(s=>`<div class="row sb" style="padding:5px 0;border-top:1px solid var(--line)"><b>${s}</b><span class="num ${cls(mk(s).c)}">${sgn(mk(s).c)}%</span></div>`).join('')}</div>
  </div>`};

R.strategies=()=>{const ex=EX[S.exch],m=mk(S.mkt);
  return `
  <div class="card"><h2>Apply strategy <span class="row">${pill('Mode: '+S.stratMode,'ai')}<button class="btn sm" data-go="pkgs">Import Algo ZIP</button></span></h2>
   <div class="form"><label>Exchange<select id="aEx">${Object.keys(EX).map(e=>`<option ${e===S.exch?'selected':''}>${e}</option>`).join('')}</select></label>
   <label>Market<select id="aMk">${MK.map(x=>`<option ${x.s===S.mkt?'selected':''}>${x.s}</option>`).join('')}</select></label>
   <label>Strategy<select id="aSt">${STRATS.filter(s=>s.n!=='AI Strategy'||1).map(s=>`<option ${s.n===S.activeStrat?'selected':''}>${s.n}</option>`).join('')}</select></label>
   <label>&nbsp;<button class="btn pri" id="applyBtn">Apply</button></label></div>
   <div class="grid g3" style="margin-top:12px" id="adapt">${adaptTiles()}</div></div>
  <div class="card"><h2>Strategy library <span class="row"><span class="mut" style="text-transform:none;letter-spacing:0">Versions are immutable once used in a backtest</span><button class="btn sm" data-act="addst">+ Add strategy</button></span></h2>
   <div class="grid g3">${STRATS.map(s=>`<div class="card" style="background:transparent"><div class="row sb"><b style="font-size:15px">${s.n}</b><span class="row" style="gap:5px">${s.custom?pill('CUSTOM','warn'):''}${pill('v'+s.v,'ai')}</span></div>
    <div class="row" style="margin:5px 0">${pill('Risk: '+s.risk,s.risk==='High'?'dn':s.risk.startsWith('Medium')?'warn':s.risk==='Low'?'up':'')}${s.n===S.activeStrat?pill('ACTIVE','up'):''}</div>
    <div class="mut">${s.desc}</div>
    <div id="det-${s.n.replace(/\W/g,'')}" hidden style="margin-top:8px"><dl class="kv"><dt>Indicators</dt><dd style="font-family:var(--f-ui)">${s.ind}</dd><dt>Timeframes</dt><dd>${s.tf}</dd><dt>Markets</dt><dd>BTC, ETH, SOL</dd><dt>Versions</dt><dd>${s.vs.map(v=>'v'+v).join(' ')}</dd>${Object.entries(s.par).map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl></div>
    <div class="row" style="margin-top:10px"><button class="btn sm" data-det="${s.n.replace(/\W/g,'')}">Details</button><button class="btn sm" data-bt="${s.n}">Backtest</button><button class="btn sm pri" data-use="${s.n}">Use strategy</button></div></div>`).join('')}</div></div>
  <div class="card"><h2>Strategy mode</h2><div class="row">${['Manual','AI Assisted','Fully Automated'].map(x=>`<label class="opt"><input type="radio" name="smode" value="${x}" ${x===S.stratMode?'checked':''}><span><b>${x}</b><br><span class="mut" style="font-size:11px">${x==='Manual'?'You choose the strategy.':x==='AI Assisted'?'AI recommends. You approve.':'AI selects within Risk Engine limits. Disabled for live.'}</span></span></label>`).join('')}</div></div>
  <div class="card"><h2>Strategy comparison ${pill('DEMO DATA','warn')}</h2>${compTable()}<p class="note" style="margin:10px 0 0">Ranked by several measures. A higher ROI with a deeper drawdown is not automatically better.</p></div>`};
function adaptTiles(){const e=EX[S.exch],base=S.mkt.replace('/','');return [['Native symbol',S.exch.startsWith('OKX')?S.mkt.replace('/','-'):base],['Tick size',e.tick],['Qty precision',e.qty],['Min order',e.min],['Fees (maker / taker)',e.fee],['Leverage',e.lev],['Order types',e.types],['Account balance',e.bal]].map(x=>`<div class="tile"><div class="l">${x[0]}</div><div class="v mono" style="font-size:12px">${x[1]}</div></div>`).join('')}
function compTable(){const d=[['ROI','+31.2%','+44.8%','+52.1%'],['Win rate','58%','46%','39%'],['Max drawdown','6.1%','11.4%','16.9%'],['Profit factor','1.62','1.38','1.21'],['Trades','212','388','147'],['Sharpe','1.41','1.02','0.74']];return `<div class="scroll"><table class="tbl"><tr><th></th><th>Trend AI</th><th>Momentum AI</th><th>Breakout AI</th></tr>${d.map(r=>`<tr><td class="mut">${r[0]}</td>${r.slice(1).map(v=>`<td class="num">${v}</td>`).join('')}</tr>`).join('')}</table></div>`}

R.backtest=()=>{const b=S.bt;
  return `<div class="row">${pill('SIMULATED RESULTS','warn')}<span class="mut">The engine here is a demo. Numbers come from a seeded generator, not from a real strategy run.</span></div>
  <div class="card"><h2>Backtest setup</h2><form class="form" id="btForm">
   <label>Strategy<select id="bStrat">${STRATS.map(s=>`<option ${s.n===(S.btSel||'Trend AI')?'selected':''}>${s.n}</option>`).join('')}</select></label>
   <label>Version<select id="bVer"></select></label>
   <label>Market<select id="bMk">${MK.map(x=>`<option ${x.s===S.mkt?'selected':''}>${x.s}</option>`).join('')}</select></label>
   <label>Timeframe<select id="bTf"><option>15m</option><option>1h</option><option>4h</option></select></label>
   <label>Start<input id="bS" type="date" value="2025-10-01"></label><label>End<input id="bE" type="date" value="2026-09-30"></label>
   <label>Capital (USDT)<input id="bCap" type="number" value="10000" min="100"></label>
   <label>Fee % per side<input id="bFee" type="number" step="0.01" value="0.10" min="0"></label>
   <label>Slippage %<input id="bSlip" type="number" step="0.01" value="0.05" min="0"></label>
   <label>Sizing<select id="bSize"><option>Fixed 0.5% risk</option><option>Fixed 1% risk</option><option>Kelly (capped)</option></select></label>
   <label>Leverage<input id="bLev" type="number" value="3" min="1" max="50"></label>
   <label>&nbsp;<button class="btn pri" type="submit">Run backtest</button></label></form></div>
  <div id="btOut">${b?btHtml(b):`<div class="card mut">Choose settings and run a backtest. Every run records strategy, version, parameters, data period, market, fees, slippage and execution assumptions.</div>`}</div>`};
function runBt(){
  const g=id=>$('#'+id).value,st=STRATS.find(s=>s.n===g('bStrat')),cfg={st:st.n,ver:g('bVer'),mk:g('bMk'),tf:g('bTf'),s:g('bS'),e:g('bE'),cap:+g('bCap'),fee:+g('bFee'),slip:+g('bSlip'),size:g('bSize'),lev:+g('bLev')};
  const seed=hash(JSON.stringify(cfg)),r=rng(seed),days=Math.max(10,Math.round((new Date(cfg.e)-new Date(cfg.s))/864e5)||90);
  const base={'Trend AI':[.44,2.3,1],'Momentum AI':[.48,1.6,1],'Mean Reversion':[.62,.9,1.15],'Breakout AI':[.38,2.9,1],'AI Strategy':[.5,1.5,1]}[cfg.st]||[.45,1.8,1],tfm={'15m':3.2,'1h':1,'4h':.3}[cfg.tf],N=Math.max(4,Math.round(days*.55*tfm*(.8+r()*.4)));
  const m=mk(cfg.mk),cs=genCandles({...m,seed:m.seed+Math.round(days)},Math.min(220,Math.max(60,N*2))),costPct=(cfg.fee+cfg.slip)*2,rk=cfg.size.includes('1%')?1:.5,rets=[],trs=[];
  let eq=cfg.cap;const curve=[eq];
  for(let i=0;i<N;i++){const win=r()<base[0],mag=(win?base[1]:-1)*(.6+r()*.8)*rk*(cfg.lev>1?1+Math.log(cfg.lev)*.25:1),net=mag-costPct*rk*cfg.lev*.15,pnl=eq*net/100,en=Math.floor(i/N*(cs.length-8))+2,du=2+Math.floor(r()*5),side=r()<.8?'LONG':'SHORT';
    eq+=pnl;curve.push(eq);rets.push(net);trs.push({n:i+1,en,ex:Math.min(cs.length-1,en+du),side,pnl,net,win:net>0,reason:cfg.st==='Trend AI'?['EMA crossover','ADX confirmation','Volume confirmation']:cfg.st==='Mean Reversion'?['Price below lower band','RSI oversold','Below VWAP']:['Signal conditions met','Risk engine approved'],exitK:net>0?'tp':'sl'})}
  const wins=trs.filter(t=>t.win),gp=wins.reduce((a,t)=>a+t.pnl,0),gl=-trs.filter(t=>!t.win).reduce((a,t)=>a+t.pnl,0),mean=rets.reduce((a,x)=>a+x,0)/N,sd=Math.sqrt(rets.reduce((a,x)=>a+(x-mean)**2,0)/N)||1,dsd=Math.sqrt(rets.filter(x=>x<0).reduce((a,x)=>a+x*x,0)/N)||1,ann=Math.sqrt(N/days*365);
  const ddv=dd(curve),mdd=Math.min(...ddv),tr=curve.slice(0,Math.floor(curve.length*.6)),va=curve.slice(Math.floor(curve.length*.6));
  const trainR=(tr[tr.length-1]/tr[0]-1)*100,valR=(va[va.length-1]/va[0]-1)*100,nPar=Object.keys(st.par).length;
  const wf=[0,1,2,3,4].map(i=>{const t=trainR*(.8+r()*.5),v=valR*(.5+r()*.9),o=i===4&&base[1]>2.5?valR*.25:valR*(.4+r()*.9);return{w:i+1,t,v,o}});
  const warns=[];if(nPar>6)warns.push('Many tunable parameters relative to trade count.');if(N<30)warns.push('Fewer than 30 trades. Statistics are unreliable.');if(trainR>0&&valR<trainR*.35)warns.push('Training performance is far better than validation.');if(cfg.fee===0||cfg.slip===0)warns.push('Zero fees or slippage is unrealistic.');if(cfg.lev>10)warns.push('High leverage assumption inflates results.');if(wf.some(w=>w.o<w.t*.2))warns.push('Out-of-sample result collapsed in at least one walk-forward window.');
  S.bt={cfg,seed,cs,trs,curve,ddv,warns,wf,sel:null,m:{net:eq-cfg.cap,roi:(eq/cfg.cap-1)*100,wr:wins.length/N*100,pf:gp/(gl||1),sharpe:mean/sd*ann,sortino:mean/dsd*ann,mdd,n:N,avg:(eq-cfg.cap)/N,lw:Math.max(...trs.map(t=>t.pnl)),ll:Math.min(...trs.map(t=>t.pnl))}};
  audit(`Backtest run: ${cfg.st} v${cfg.ver} on ${cfg.mk}, ${cfg.s} to ${cfg.e}, fee ${cfg.fee}%, slip ${cfg.slip}%`)}
function btHtml(b){const m=b.m,c=b.cfg;
  return `<div class="grid kpis">${[['Net profit','$'+fmt(m.net),cls(m.net)],['ROI',sgn(m.roi)+'%',cls(m.roi)],['Win rate',fmt(m.wr,1)+'%',''],['Profit factor',fmt(m.pf),''],['Sharpe',fmt(m.sharpe),''],['Sortino',fmt(m.sortino),''],['Max drawdown',fmt(m.mdd,1)+'%','dn'],['Trades',m.n,''],['Avg trade','$'+fmt(m.avg),cls(m.avg)],['Largest win','$'+fmt(m.lw),'up'],['Largest loss','$'+fmt(m.ll),'dn']].map(k=>`<div class="card kpi"><div class="l">${k[0]}</div><div class="v ${k[2]}" style="font-size:17px">${k[1]}</div></div>`).join('')}</div>
  ${b.warns.length?`<div class="warnbox"><b>POSSIBLE OVERFITTING</b><ul style="margin:6px 0 0;padding-left:18px">${b.warns.map(w=>`<li>${w}</li>`).join('')}</ul></div>`:`<div class="note">No overfitting flags raised for this run. One run is still not proof.</div>`}
  <div class="grid g2"><div class="card"><h2>Trades on price ${pill('SIMULATED','warn')}</h2><canvas id="cBt" style="height:300px"></canvas><canvas id="cBe" style="height:90px;margin-top:6px"></canvas><canvas id="cBd" style="height:48px;margin-top:4px"></canvas></div>
  <div style="display:grid;gap:12px;align-content:start"><div class="card"><h2>Run record</h2><dl class="kv"><dt>Strategy</dt><dd>${c.st} v${c.ver}</dd><dt>Market / TF</dt><dd>${c.mk} ${c.tf}</dd><dt>Period</dt><dd>${c.s} to ${c.e}</dd><dt>Fees</dt><dd>${c.fee}% / side</dd><dt>Slippage</dt><dd>${c.slip}%</dd><dt>Sizing</dt><dd style="font-family:var(--f-ui)">${c.size}</dd><dt>Leverage</dt><dd>${c.lev}x</dd><dt>Fills</dt><dd style="font-family:var(--f-ui)">Next-bar open</dd><dt>Seed</dt><dd>${b.seed}</dd></dl></div>
  <div class="card" id="tradeDetail"><h2>Trade detail</h2><span class="mut">Select a trade below.</span></div></div></div>
  <div class="card"><h2>Walk-forward validation</h2><div class="scroll"><table class="tbl"><tr><th>Window</th><th>Train ROI</th><th>Validation ROI</th><th>Out-of-sample ROI</th><th>Flag</th></tr>${b.wf.map(w=>`<tr><td>#${w.w}</td><td class="num">${sgn(w.t,1)}%</td><td class="num">${sgn(w.v,1)}%</td><td class="num ${cls(w.o)}">${sgn(w.o,1)}%</td><td>${w.o<w.t*.2?pill('DEGRADED','warn'):pill('OK','up')}</td></tr>`).join('')}</table></div></div>
  <div class="card"><h2>Trades</h2><div class="scroll" style="max-height:300px;overflow-y:auto"><table class="tbl"><tr><th>#</th><th>Side</th><th>Bars</th><th>P&amp;L</th></tr>${b.trs.map(t=>`<tr class="click" data-trade="${t.n}"><td>#${t.n}</td><td>${t.side}</td><td class="num">${t.ex-t.en}</td><td class="num ${cls(t.pnl)}">${sgn(t.pnl)}</td></tr>`).join('')}</table></div></div>`}

R.paper=()=>`<div class="row">${pill('PAPER TRADING','warn')}<span class="mut">Same pipeline as live: Strategy &rarr; Signal &rarr; Risk &rarr; Order engine &rarr; Paper exchange. No real money.</span></div>
  <div class="grid g3">${BOTS.map(botCard).join('')}</div>
  <div class="card"><h2>Pipeline</h2><div class="row" style="gap:6px">${['Strategy','Signal','Risk Engine','Order Engine','Paper Exchange'].map((x,i)=>`${i?'<span class="mut">&rarr;</span>':''}<span class="pill ${x==='Risk Engine'?'up':'ai'}">${x}</span>`).join('')}</div><p class="note" style="margin:10px 0 0">Signals never place orders directly. Every signal passes the Risk Engine first, and bots keep isolated state.</p></div>`;
function botCard(b){const run=b.state==='RUNNING',pa=b.state==='PAUSED';
  return `<div class="card"><div class="row sb"><b style="font-size:14px">BOT #${b.id}</b><span><i class="dot ${dotc(b.state)}"></i><b>${b.state}</b></span></div>
  <dl class="kv" style="margin:8px 0"><dt>Exchange</dt><dd>${b.ex}</dd><dt>Market</dt><dd>${b.m}</dd><dt>Strategy</dt><dd>${b.st} v${b.v}</dd><dt>Timeframe</dt><dd>${b.tf}</dd><dt>Risk / trade</dt><dd>${b.risk}%</dd><dt>Mode</dt><dd>${b.mode}</dd><dt>Paper P&amp;L</dt><dd class="${cls(b.pnl)}">${sgn(b.pnl)}</dd></dl>
  <div class="row"><button class="btn sm" data-bot="${b.id}" data-do="${pa?'resume':'pause'}" ${run||pa?'':'disabled'}>${pa?'Resume':'Pause'}</button><button class="btn sm" data-bot="${b.id}" data-do="${b.state==='STOPPED'?'start':'stop'}" ${['STARTING','STOPPING'].includes(b.state)?'disabled':''}>${b.state==='STOPPED'?'Start':'Stop'}</button><button class="btn sm" data-bot="${b.id}" data-do="config">Configure</button></div></div>`}

R.live=()=>`<div class="card disabled-big"><h2>LIVE TRADING</h2><span class="mut">Coming after paper-trading validation.</span><button class="btn" disabled>LIVE TRADING DISABLED</button></div>
  <div class="card"><h2>Requirements before live activation</h2>${[['Exchange connected',1],['API verified',0],['Trading permission granted (withdraw must stay off)',0],['Risk configuration reviewed',1],['User confirmation',0],['Explicit live activation',0]].map((x,i)=>`<div class="step"><span class="pill ${x[1]?'up':''}">${x[1]?'DONE':'PENDING'}</span><span>${i+1}. ${x[0]}</span></div>`).join('')}<p class="note" style="margin:10px 0 0">Paper never switches to live automatically. Experimental AI models cannot be activated live.</p></div>`;

R.positions=()=>`<div class="card"><h2>Open positions ${pill('PAPER','warn')}</h2><div class="scroll"><table class="tbl"><tr><th>Market</th><th>Side</th><th>Entry</th><th>Current</th><th>Unrealized P&amp;L</th><th>Stop</th><th>Take profit</th><th>Lev</th><th>Strategy</th></tr>${POS.map((p,i)=>`<tr><td><b>${p.m}</b></td><td class="up">${p.side}</td><td class="num">${fmt(p.entry)}</td><td class="num" data-px2="${p.m}">${fmt(mk(p.m).p)}</td><td class="num" data-pnl="${i}"></td><td class="num">${fmt(p.stop)}</td><td class="num">${fmt(p.tp)}</td><td>${p.lev}x</td><td>${p.st}</td></tr>`).join('')}</table></div></div>`;
const ORD=(()=>{const r=rng(5),sts=['Filled','Filled','Open','Filled','Cancelled','Rejected','Filled','Partial','Open','Filled','Filled','Cancelled'],o=[];BOTS.forEach(()=>0);for(let i=0;i<12;i++){const b=BOTS[i%3],m=mk(b.m);o.push({id:'PT-'+(48210+i*7),ex:b.ex,m:b.m,side:r()<.55?'BUY':'SELL',type:i%4===2?'LIMIT':i%5===4?'STOP':'MARKET',px:m.p*(1+(r()-.5)*.01),q:+(r()*2).toFixed(3),st:sts[i],strat:b.st,bot:'#'+b.id,t:new Date(Date.now()-i*1.4e6)})}return o})();
R.orders=()=>{const f=['All','Open','Filled','Partial','Cancelled','Rejected'];return `<div class="card"><h2>Orders ${pill('PAPER','warn')}<span class="tabs">${f.map(x=>`<button data-of="${x}" aria-pressed="${x===S.orderFilter}">${x}</button>`).join('')}</span></h2><div class="scroll"><table class="tbl"><tr><th>Order ID</th><th>Exchange</th><th>Symbol</th><th>Side</th><th>Type</th><th>Price</th><th>Qty</th><th>Status</th><th>Strategy</th><th>Bot</th><th>Time</th></tr>${ORD.filter(o=>S.orderFilter==='All'||o.st===S.orderFilter).map(o=>`<tr><td class="mono">${o.id}</td><td>${o.ex}</td><td>${o.m}</td><td class="${o.side==='BUY'?'up':'dn'}">${o.side}</td><td>${o.type}</td><td class="num">${fmt(o.px,o.px<10?4:2)}</td><td class="num">${o.q}</td><td>${pill(o.st.toUpperCase(),o.st==='Filled'?'up':o.st==='Rejected'?'dn':o.st==='Open'||o.st==='Partial'?'warn':'')}</td><td>${o.strat}</td><td>${o.bot}</td><td class="mono">${clock(o.t)}</td></tr>`).join('')}</table></div></div>`};

R.perf=()=>{const eq=equity('3M');return `<div class="row">${pill('DEMO DATA','warn')}<label class="field">View by <select id="pf"><option>Portfolio</option><option>Strategy</option><option>Bot</option><option>Exchange</option><option>Market</option></select></label></div>
  <div class="grid kpis">${[['ROI','+24.5%'],['Win rate','63.4%'],['Profit factor','1.54'],['Max drawdown','3.8%'],['Sharpe','1.38'],['Sortino','1.92'],['Avg trade','$16.90'],['Expectancy','$11.20'],['Best streak','7 wins'],['Worst streak','4 losses']].map(k=>`<div class="card kpi"><div class="l">${k[0]}</div><div class="v" style="font-size:17px">${k[1]}</div></div>`).join('')}</div>
  <div class="card"><h2>Equity (3M)</h2><canvas id="cEq" style="height:240px"></canvas></div>
  <div class="card"><h2>Strategy comparison</h2>${compTable()}</div>`};

R.ailab=()=>{const m=mk('BTC/USDT');return `<div class="row">${pill('AI','ai')}${pill('Experimental models cannot trade live','warn')}</div>
  <div class="grid g2"><div class="card"><h2>Market assessment ${pill('BTC/USDT','')}</h2>
   <dl class="kv"><dt>Direction</dt><dd class="up">BULLISH</dd><dt>Confidence</dt><dd class="ai">82%</dd><dt>Regime</dt><dd>TRENDING</dd><dt>Volatility</dt><dd class="warn">HIGH</dd><dt>Recommended strategy</dt><dd>Trend AI</dd><dt>Risk</dt><dd class="warn">Medium / High</dd></dl>
   <p class="note" style="margin:10px 0">Confidence is model certainty in the detected pattern. It is not an 82% probability of profit.</p>
   <div class="grid g3" style="gap:8px"><div><b class="up">Reasons</b><div class="mut" style="margin-top:4px">&#10003; EMA trend alignment<br>&#10003; Positive momentum<br>&#10003; Volume confirmation<br>&#10003; Strong market structure</div></div><div><b class="warn">Risks</b><div class="mut" style="margin-top:4px">&#9888; High volatility<br>&#9888; FOMC approaching<br>&#9888; Crowded long positioning</div></div><div><b>Recommendation</b><div class="ai" style="margin-top:4px;font-weight:800">WAIT FOR CONFIRMATION</div><div class="mut">Event filter reduces size until FOMC passes.</div></div></div></div>
  <div class="card"><h2>Decision path</h2>${['Market data','News and events','On-chain and derivatives','Macro','Market state','AI / strategy','Signal','Risk engine (final authority)','Order engine'].map((x,i)=>`<div class="step"><span class="pill ${x.startsWith('Risk')?'up':'ai'}">${i+1}</span><span>${x}</span></div>`).join('')}</div></div>
  <div class="grid g3">${[['Market analysis','Regime and direction per market'],['Strategy analysis','Which strategy fits the current regime'],['Model evaluation','Out-of-sample accuracy of the pattern model'],['Feature analysis','Which inputs drive the assessment'],['Parameter optimisation','Walk-forward only, never single-run'],['Regime detection','Trend, range, high-volatility states']].map(x=>`<div class="card"><h2>${x[0]} ${pill('PREVIEW','')}</h2><div class="mut">${x[1]}</div></div>`).join('')}</div>
  <div class="card"><h2>AI can never</h2><div class="row">${['Disable risk limits','Raise leverage past the cap','Remove stop loss','Bypass daily loss limit','Disable emergency stop','Use withdrawal permission','Change exchange credentials'].map(x=>pill(x,'dn')).join('')}</div></div>`};

R.news=()=>`<div class="row">${pill('DEMO DATA','warn')}<span class="mut">News does not equal buy or sell. Each item is classified, then compared with the actual market reaction, then handed to strategies and the Risk Engine.</span></div>
  <div class="grid g2"><div class="card"><h2>Upcoming events <span class="row">${pill('Times in your local zone','')}</span></h2><div class="scroll"><table class="tbl"><tr><th>Time</th><th>Event</th><th>Country</th><th>Impact</th><th>Expected vol.</th></tr>${EVENTS.map((e,i)=>`<tr class="click" data-ev="${i}"><td class="mono">${hm(mins(e.t))}</td><td><b>${e.n}</b></td><td>${e.c}</td><td>${pill(e.imp,e.imp==='HIGH'?'dn':'warn')}</td><td>${e.vol}</td></tr>`).join('')}</table></div><div id="evd" class="note" style="margin-top:10px">Select an event for the AI interpretation.</div></div>
  <div class="card"><h2>Breaking news</h2>${NEWS.map(n=>`<div class="newsi"><h3>${n.h}</h3><div class="mut" style="font-size:11px">${n.src} &middot; ${n.ago} ago</div><div class="chips">${pill(n.sent.toUpperCase(),n.sent==='Bullish'?'up':'dn')}${pill('IMPACT '+n.imp,n.imp==='HIGH'?'dn':'warn')}${pill('CONF '+n.conf+'%','ai')}${n.a.map(a=>pill(a)).join('')}</div><div class="mut" style="margin-top:5px;font-size:12px"><b>Expected:</b> ${n.exp} &middot; <b>Actual:</b> ${n.react}</div><div class="note" style="margin-top:5px">${n.ai}</div></div>`).join('')}</div></div>
  <div class="grid g3"><div class="card"><h2>Crypto events</h2><div class="mut">ETF decisions, unlocks, upgrades, listings, delistings, regulation, hacks. Detectors prepared, demo items only.</div></div><div class="card"><h2>Macro panel ${pill('PROVIDER NOT CONNECTED','')}</h2><div class="mut">Rates, inflation, employment, GDP, DXY and yields will come from an economic data provider. No values are hard-coded.</div></div><div class="card"><h2>Unexpected events</h2><div class="mut">Exchange outages, security incidents, government announcements, market shocks. These trigger the event filter and alerts.</div></div></div>`;

R.exchange=()=>`<div class="card"><h2>Connected exchanges <button class="btn sm" data-act="addex">+ Add exchange</button></h2><div class="grid g3">${Object.entries(EX).map(([n,e])=>`<div class="card" style="background:transparent"><div class="row sb"><b style="font-size:15px">${n}</b><span><i class="dot ${e.conn?'':'o'}"></i>${e.conn?'Connected':'Not connected'}</span></div><dl class="kv" style="margin-top:8px"><dt>Native symbol</dt><dd>${e.sym}</dd><dt>Normalized</dt><dd>BTC/USDT</dd><dt>Read</dt><dd class="up">YES</dd><dt>Trade</dt><dd>${e.conn?(e.trade===false?'NO':'YES'):'-'}</dd><dt>Withdraw</dt><dd class="up">NO</dd>${e.key?`<dt>API key</dt><dd>${e.key}</dd>`:''}</dl><div class="row" style="margin-top:8px">${pill(e.demo?'ADDED (DEMO)':e.conn?'DEMO CONNECTION':'ADAPTER STUB',e.conn?'warn':'')}<button class="btn sm" data-ex="${n}">Use</button>${e.demo?`<button class="btn sm" data-exrm="${n}">Remove</button>`:''}</div></div>`).join('')}</div></div>
  <div class="grid g2"><div class="card"><h2>Rules applied for ${S.exch} ${pill('DEMO VALUES','warn')}</h2><div class="grid g3" style="gap:8px">${adaptTiles()}</div></div>
  <div class="card"><h2>Adapter interface</h2><div class="mono" style="font-size:11px;color:var(--mut)">connect() &middot; disconnect()<br>get_balance() &middot; get_markets()<br>get_ticker() &middot; get_orderbook()<br>create_order() &middot; cancel_order()<br>get_order() &middot; get_open_orders()<br>get_positions()</div><p class="note" style="margin:10px 0 0">Strategies only see normalized symbols and Market State. Credentials live encrypted on the server and never in browser code. A withdrawal permission on any key raises a warning.</p></div></div>
  <div class="card"><h2>Data providers (interfaces prepared)</h2><div class="row">${['Binance','Bybit','OKX','Coin Metrics','Kaiko','CoinGecko','Trading Economics','News API','Glassnode'].map(x=>pill(x)).join('')}</div></div>`;

R.risk=()=>{const cm=[['BTC','ETH',.87],['BTC','SOL',.81],['ETH','SOL',.84]];
  return `<div class="row sb"><div class="row">${pill(S.halted?'HALTED':'SAFE',S.halted?'dn':'up')}${pill('DEMO DATA','warn')}</div><button class="estop" data-act="estop">EMERGENCY STOP</button></div>
  <div class="grid g2"><div class="card"><h2>Risk status</h2>${riskMeters()}<div class="row sb"><span class="mut">Risk per trade</span><b class="num">0.50%</b></div></div>
  <div class="card"><h2>Portfolio risk (budget at stop)</h2>${[['BTC',3],['ETH',2],['SOL',2]].map(x=>`<div class="meter"><div class="row sb"><span>${x[0]} exposure</span><span class="num">${x[1]}%</span></div><div class="bar"><i style="width:${x[1]/8*100}%"></i></div></div>`).join('')}<div class="row sb"><b>Total</b><b class="num">7% <span class="mut">/ 8% cap</span></b></div><div class="warnbox" style="margin-top:10px">HIGH CORRELATED EXPOSURE. BTC, ETH and SOL move together, so three bots act like one larger position.</div></div></div>
  <div class="grid g2"><div class="card"><h2>Correlation (30d, demo)</h2><table class="tbl heat">${cm.map(c=>`<tr><td style="text-align:left">${c[0]} &harr; ${c[1]}</td><td style="background:rgba(246,183,60,${(c[2]-.5)*1.1})">${c[2]}</td></tr>`).join('')}</table></div>
  <div class="card"><h2>Limits ${pill('LOCKED TO AI','ai')}</h2><dl class="kv"><dt>Max position size</dt><dd>10%</dd><dt>Max leverage</dt><dd>5x</dd><dt>Daily loss</dt><dd>3.00%</dd><dt>Max drawdown</dt><dd>10%</dd><dt>Max exposure</dt><dd>70%</dd><dt>Max open positions</dt><dd>5</dd><dt>Correlated exposure</dt><dd>8%</dd><dt>Consecutive losses</dt><dd>4</dd><dt>Cooldown</dt><dd>30 min</dd><dt>Stop loss</dt><dd>Mandatory</dd></dl></div></div>
  <div class="card"><h2>Emergency stop does</h2><div class="mut">Blocks new trades, cancels open orders when configured, stops bots, records an audit event, and optionally closes positions under an explicit policy you choose first. Always asks for confirmation.</div></div>`};

R.logs=()=>`<div class="card"><h2>Activity and audit log <span class="row"><select id="lk" class="search" style="width:auto"><option value="">All</option><option>sys</option><option>sig</option><option>risk</option><option>ord</option><option>ai</option><option>audit</option></select><input class="search" id="lq" type="search" placeholder="Search logs"></span></h2><div class="scroll" style="max-height:62vh;overflow-y:auto"><table class="tbl" id="logt"></table></div></div>`;
function renderLogRows(){const t=$('#logt');if(!t)return;const k=$('#lk').value,q=$('#lq').value.toLowerCase();t.innerHTML=S.logs.filter(l=>(!k||l.k===k)&&(!q||l.m.toLowerCase().includes(q))).slice(0,120).map(l=>`<tr><td class="mono mut">${clock(l.t)}</td><td>${pill(l.k.toUpperCase(),l.k==='risk'?'up':l.k==='ai'?'ai':l.k==='audit'?'warn':'')}</td><td>${l.m}</td></tr>`).join('')}

R.settings=()=>`<div class="grid g2"><div class="card"><h2>Roles and access</h2><table class="tbl"><tr><th>Role</th><th>View</th><th>Trade</th><th>Risk and keys</th></tr><tr><td>Admin</td><td>Yes</td><td>Yes</td><td>Yes</td></tr><tr><td>Trader</td><td>Yes</td><td>Yes</td><td>No</td></tr><tr><td>Viewer</td><td>Yes</td><td>No</td><td>No</td></tr></table></div>
  <div class="card"><h2>Alerts</h2>${['Trade opened / closed','Stop loss and take profit','Bot stopped','Exchange disconnected','Risk limit reached','Drawdown exceeded','High-impact event','AI strategy change'].map((x,i)=>`<label class="row sb" style="padding:4px 0;border-top:1px solid var(--line)"><span>${x}</span><input type="checkbox" ${i%5!==4?'checked':''}></label>`).join('')}<div class="row" style="margin-top:8px">${['In-app','Browser','Email','Telegram'].map(x=>pill(x)).join('')}${pill('Discord later')}</div></div></div>
  <div class="card"><h2>Security</h2><div class="mut">API keys are stored encrypted on the server. Default permissions are Read yes, Trade yes, Withdraw no. This demo holds no credentials.</div></div>`;

/* ---------- order book + trades ---------- */
function updateBook(){const ob=$('#ob');if(!ob)return;const m=mk(S.mkt),r=rng(Math.floor(Date.now()/1500)+m.seed),st=m.p*0.0004;let h='<span class="h">Price</span><span class="h">Size</span><span class="h">Total</span>',a=[],b=[];
  for(let i=5;i>=1;i--){const q=+(r()*3+.1).toFixed(3);a.push(`<span class="ask">${fmt(m.p+st*i,m.d)}</span><span>${q}</span><span class="mut">${(q*(m.p+st*i)).toFixed(0)}</span>`)}
  for(let i=1;i<=5;i++){const q=+(r()*3.6+.2).toFixed(3);b.push(`<span class="bid">${fmt(m.p-st*i,m.d)}</span><span>${q}</span><span class="mut">${(q*(m.p-st*i)).toFixed(0)}</span>`)}
  ob.innerHTML=h+a.join('')+`<span style="grid-column:1/-1;border-top:1px solid var(--line);margin:3px 0"></span>`+b.join('')}
let tradeBuf=[];
function pushTrade(){const t=$('#trades');if(!t)return;const m=mk(S.mkt),r=Math.random();tradeBuf.unshift({t:new Date(),s:r<.52?'BUY':'SELL',p:m.p*(1+(Math.random()-.5)*.0004),q:+(Math.random()*.8+.01).toFixed(3)});tradeBuf=tradeBuf.slice(0,9);t.innerHTML='<tr><th>Time</th><th>Side</th><th>Price</th><th>Size</th></tr>'+tradeBuf.map(x=>`<tr><td class="mono mut">${clock(x.t)}</td><td class="${x.s==='BUY'?'up':'dn'}">${x.s}</td><td class="num">${fmt(x.p,m.d)}</td><td class="num">${x.q}</td></tr>`).join('')}

/* ---------- render engine ---------- */
const REAL_PAGES=['activity','pkgs'];
function pageHtml(p){const h=R[p]();if(REAL_PAGES.indexOf(p)!==-1)return h;
 return '<div class="demo-banner" role="note"><b>DEMO / SIMULATED PAGE.</b> Prices, balances, positions, orders, statuses and charts on this page are generated in your browser. Nothing here comes from the trading engine and no order placed here reaches any exchange. Real backend data: <a href="#" data-go="activity">Live Activity</a> and <a href="#" data-go="pkgs">Strategy Manager</a>.</div>'+h}
function drawPage(){
  const m=mk(S.mkt),cm=$('#cMain');
  if($('#cEq')&&S.page==='dashboard'){const e=equity(S.range);drawLines($('#cEq'),[{d:e.bm,c:col('--dim'),w:1.2,dash:[4,3]},{d:e.eq,c:col('--ai'),fill:'rgba(139,140,255,.22)',end:1}],{f:v=>'$'+fmt(v,0)});drawLines($('#cDd'),[{d:dd(e.eq),c:col('--dn'),fill:'rgba(255,92,108,.25)'}],{lo:Math.min(-6,...dd(e.eq)),hi:0,base:0,f:v=>fmt(v,0)+'%'})}
  if($('#cEq')&&S.page==='perf'){const e=equity('3M');drawLines($('#cEq'),[{d:e.bm,c:col('--dim'),dash:[4,3]},{d:e.eq,c:col('--ai'),fill:'rgba(139,140,255,.22)',end:1}],{f:v=>'$'+fmt(v,0)})}
  if(cm)drawCandles(cm,m.cs,{n:S.page==='dashboard'?70:90});
  if($('#cBt')&&S.bt){const b=S.bt,marks=[];b.trs.forEach(t=>{const c=b.cs[t.en];if(c)marks.push({i:t.en,p:c.l*.998,k:'in'});const x=b.cs[t.ex];if(x)marks.push({i:t.ex,p:x.h*1.002,k:t.exitK})});drawCandles($('#cBt'),b.cs,{n:b.cs.length,ind:{ema:1,vol:1},marks});drawLines($('#cBe'),[{d:b.curve,c:col('--ai'),fill:'rgba(139,140,255,.2)',end:1}],{f:v=>'$'+fmt(v,0)});drawLines($('#cBd'),[{d:b.ddv,c:col('--dn'),fill:'rgba(255,92,108,.25)'}],{lo:Math.min(-2,...b.ddv),hi:0,base:0,f:v=>fmt(v,0)+'%'})}
}
function updatePnl(){$$('[data-pnl]').forEach(e=>{const p=POS[+e.dataset.pnl],v=posPnl(p);e.textContent=sgn(v);e.className='num '+cls(v)})}
function go(p){if(S.page==='activity'&&p!=='activity')stopActivity();S.page=p;$$('#nav button').forEach(b=>b.setAttribute('aria-current',b.dataset.p===p?'page':'false'));$('#view').innerHTML=pageHtml(p);window.scrollTo(0,0);post();log('Opened '+p,'sys');S.logs.shift()}
function rerender(){const y=window.scrollY;$('#view').innerHTML=pageHtml(S.page);post();window.scrollTo(0,y)}
function post(){if(S.page==='activity')activityPost();if(S.page==='pkgs')pkgsPost();drawPage();updatePnl();updateBook();pushTrade();if(S.page==='logs')renderLogRows();
  if(S.page==='backtest'){fillVer();if(S.bt)$('#bStrat').value=S.bt.cfg.st}
  if($('#scTrend'))$('#scTrend').value=S.ostate.trend}
function fillVer(){const s=STRATS.find(x=>x.n===$('#bStrat').value);$('#bVer').innerHTML=s.vs.slice().reverse().map(v=>`<option ${v===s.v?'selected':''}>${v}</option>`).join('')}
function syncBanners(){$('#bPaper').classList.toggle('show',S.mode==='PAPER');$('#bLive').classList.toggle('show',S.mode==='LIVE');$('#bHalt').classList.toggle('show',S.halted)}

/* ---------- events ---------- */
$('#nav').innerHTML=PAGES.map(p=>`<button data-p="${p[0]}"><i></i>${p[1]}</button>`).join('');
$('#selMkt').innerHTML=MK.map(m=>`<option>${m.s}</option>`).join('');
$('#nav').addEventListener('click',e=>{const b=e.target.closest('button');if(b)go(b.dataset.p)});
$('#selEx').addEventListener('change',e=>{S.exch=e.target.value;audit('Exchange changed to '+S.exch);toast('Exchange: '+S.exch+'. Symbol and rules adapted.');if(['strategies','exchange','markets'].includes(S.page))rerender()});
$('#selMode').addEventListener('change',e=>{S.mode=e.target.value;syncBanners();audit('Mode set to '+S.mode);if(S.mode==='LIVE')toast('Live execution is disabled in this demo.')});
$('#selMkt').addEventListener('change',e=>{S.mkt=e.target.value;rerender()});
$('#search').addEventListener('keydown',e=>{if(e.key==='Enter'){const q=e.target.value.toUpperCase(),m=MK.find(x=>x.s.includes(q));if(m){S.mkt=m.s;$('#selMkt').value=m.s;go('markets')}else toast('No market matches "'+e.target.value+'"')}});
$('#bell').addEventListener('click',()=>{modal(`<h3>Notifications</h3><div class="mut">FOMC in 2 hours: event filter armed<br>Correlated exposure 0.87: warning<br>AI recommends Trend AI for BTC/USDT</div><button class="btn pri" data-act="close">Close</button>`);$('#bellN').textContent='0'});
function estop(){modal(`<h3>Emergency stop</h3><div class="mut">This stops new trades and stops all bots. Choose what happens to open positions.</div>
  <label class="opt"><input type="radio" name="ep" value="keep" checked><span><b>Keep positions</b><br><span class="mut">Stops and take-profits stay attached.</span></span></label>
  <label class="opt"><input type="radio" name="ep" value="close"><span><b>Close positions</b><br><span class="mut">Market-close all open positions.</span></span></label>
  <label class="opt"><input type="checkbox" id="epc" checked><span>Cancel open orders</span></label>
  <div class="row"><button class="btn dn" data-act="estopgo">Confirm emergency stop</button><button class="btn" data-act="close">Cancel</button></div>`)}
$('#estop').addEventListener('click',estop);
$('#modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});

/* ---------- add exchange / add strategy (demo forms) ---------- */
const NAME_RE=/^[A-Za-z0-9][A-Za-z0-9 _\-]{2,39}$/,VER_RE=/^\d{1,3}\.\d{1,3}$/,LABEL_RE=/^[A-Za-z0-9 _\-]{1,40}$/;
const COND={price_above_ema:{l:'Price above EMA',p:['length']},price_below_ema:{l:'Price below EMA',p:['length']},ema_fast_above_slow:{l:'Fast EMA above slow EMA',p:['length','slow']},ema_fast_below_slow:{l:'Fast EMA below slow EMA',p:['length','slow']},rsi_below:{l:'RSI below',p:['length','value']},rsi_above:{l:'RSI above',p:['length','value']},volume_ratio_above:{l:'Volume above (x average)',p:['value']}};
const PL={length:'Length',slow:'Slow length',value:'Value'};
const AX={tab:'rules',b:{name:'',version:'1.0',risk:'Medium',atr:2,err:'',long:[{type:'ema_fast_above_slow',length:20,slow:50,value:30}],short:[]}};
const condText=c=>{const t=c.type;return t==='price_above_ema'?`Price above EMA ${c.length}`:t==='price_below_ema'?`Price below EMA ${c.length}`:t==='ema_fast_above_slow'?`EMA ${c.length} above EMA ${c.slow}`:t==='ema_fast_below_slow'?`EMA ${c.length} below EMA ${c.slow}`:t==='rsi_below'?`RSI ${c.length} below ${c.value}`:t==='rsi_above'?`RSI ${c.length} above ${c.value}`:`Volume above ${c.value}x average`};
function exModal(){
  modal(`<h3>Add exchange</h3>
  <div class="note">Demo only. Nothing you type here is sent or stored. In the real app the server keeps keys encrypted and reads the key's permissions from the exchange itself, so permissions cannot be self-declared.</div>
  <div class="form" style="grid-template-columns:1fr 1fr">
   <label>Exchange<select id="fxEx"><option>Binance</option><option>Bybit</option><option>OKX</option></select></label>
   <label>Label<input id="fxLabel" type="text" maxlength="40" placeholder="Main account" autocomplete="off"></label>
   <label>API key<input id="fxKey" type="password" autocomplete="off"></label>
   <label>API secret<input id="fxSecret" type="password" autocomplete="off"></label>
   <label id="fxPassW" hidden>Passphrase (OKX)<input id="fxPass" type="password" autocomplete="off"></label></div>
  <div><div class="mut" style="font-size:11px;margin-bottom:5px">Permissions on this key (demo: tick what you enabled on the exchange)</div>
   <div class="row"><label class="opt"><input type="checkbox" checked disabled> Read</label><label class="opt"><input type="checkbox" id="fxTrade" checked> Trade</label><label class="opt"><input type="checkbox" id="fxWd"> Withdraw</label></div></div>
  <div class="warnbox" id="fxErr" hidden></div>
  <div class="row"><button class="btn pri" data-act="exgo">Verify and add</button><button class="btn" data-act="close">Cancel</button></div>`)}
function exSubmit(){
  const g=id=>$('#'+id).value.trim(),ex=g('fxEx'),label=g('fxLabel'),key=g('fxKey'),sec=g('fxSecret'),err=m=>{const e=$('#fxErr');e.textContent=m;e.hidden=false};
  if(label&&!LABEL_RE.test(label))return err('Label may use letters, digits, spaces, - and _ only.');
  if(key.length<8||sec.length<8)return err('Enter the API key and secret from the exchange.');
  if(ex==='OKX'&&!g('fxPass'))return err('OKX requires the key passphrase.');
  if($('#fxWd').checked){audit('Rejected '+ex+' key: withdrawal permission enabled');return err('Rejected: this key can withdraw funds. Create a new key with Withdraw switched off, then add it.')}
  const id=label?`${ex} (${label})`:`${ex} (new)`;if(EX[id])return err('An exchange with this name already exists.');
  EX[id]={...EX[ex],conn:true,demo:true,trade:$('#fxTrade').checked,key:'••••••••'+key.slice(-4),bal:'after verification'};
  $('#selEx').insertAdjacentHTML('beforeend',`<option>${id}</option>`);audit('Exchange added (demo): '+id+', withdraw off');closeModal();toast('Added '+id+'. Withdrawal permission is off.');if(S.page==='exchange')rerender()}
function codeTab(){return `<div class="mut">Code plugins run Python with full server privileges, so they are off by default and not available in this demo. On a machine you control:</div>
  <ol class="mut" style="margin:0;padding-left:18px"><li>Set <span class="mono">DEWAKO_ALLOW_CODE_PLUGINS=1</span> on the server.</li><li>Put your file in <span class="mono">plugins/strategies/</span>. It subclasses <span class="mono">Strategy</span> and uses <span class="mono">@register</span>.</li><li>Call <span class="mono">POST /strategies/plugins/reload</span> with the admin token.</li></ol>
  <div class="note">Use the no-code builder if you want to add strategies without trusting any code.</div><div class="row"><button class="btn" data-act="close">Close</button></div>`}
function rowsHtml(side){return AX.b[side].map((c,i)=>`<div class="row" style="gap:6px"><select data-brow="${side}:${i}:type" aria-label="Condition">${Object.entries(COND).map(([k,v])=>`<option value="${k}" ${k===c.type?'selected':''}>${v.l}</option>`).join('')}</select>${COND[c.type].p.map(p=>`<input data-brow="${side}:${i}:${p}" type="number" value="${c[p]}" aria-label="${PL[p]}" title="${PL[p]}" style="width:78px">`).join('')}<button class="btn sm" data-ax="rm:${side}:${i}" aria-label="Remove condition">&times;</button></div>`).join('')||'<div class="mut" style="font-size:11px">None</div>'}
function builderTab(){const b=AX.b;return `<div class="form" style="grid-template-columns:2fr 1fr 1fr">
   <label>Name<input id="bName" type="text" maxlength="40" value="${b.name}" placeholder="My RSI Dip" autocomplete="off"></label>
   <label>Version<input id="bVer" type="text" maxlength="7" value="${b.version}"></label>
   <label>Risk<select id="bRisk">${['Low','Medium','Medium-High','High'].map(x=>`<option ${x===b.risk?'selected':''}>${x}</option>`).join('')}</select></label></div>
  <div><div class="row sb"><b>Enter long when all are true</b><button class="btn sm" data-ax="add:long" ${b.long.length>=5?'disabled':''}>+ Condition</button></div><div style="display:grid;gap:6px;margin-top:6px">${rowsHtml('long')}</div></div>
  <div><div class="row sb"><b>Enter short when all are true</b><button class="btn sm" data-ax="add:short" ${b.short.length>=5?'disabled':''}>+ Condition</button></div><div style="display:grid;gap:6px;margin-top:6px">${rowsHtml('short')}</div></div>
  <div class="form" style="grid-template-columns:1fr"><label>Stop distance (x ATR, 0.5 to 10)<input id="bAtr" type="number" step="0.1" value="${b.atr}"></label></div>
  <div class="note">Every entry gets a stop loss, and the Risk Engine still approves each signal. The builder only allows these fixed conditions. No code is run. In this demo the strategy lives in this browser tab until you reload.</div>
  ${b.err?`<div class="warnbox">${b.err}</div>`:''}
  <div class="row"><button class="btn pri" data-act="stgo">Add strategy</button><button class="btn" data-act="close">Cancel</button></div>`}
function stModal(){modal(`<h3>Add strategy</h3><div class="tabs"><button data-ax="tab:rules" aria-pressed="${AX.tab!=='code'}">No-code builder</button><button data-ax="tab:code" aria-pressed="${AX.tab==='code'}">Code plugin</button></div>${AX.tab==='code'?codeTab():builderTab()}`);$('#modalBox').classList.add('wide')}
function axAct(a){const [k,x,y]=a.split(':');if(k==='tab')AX.tab=x;else if(k==='add'&&AX.b[x].length<5)AX.b[x].push({type:'price_above_ema',length:20,slow:50,value:30});else if(k==='rm')AX.b[x].splice(+y,1);stModal()}
function stSubmit(){
  const b=AX.b,all=[...b.long,...b.short],fail=m=>{b.err=m;stModal()};
  if(!NAME_RE.test(b.name))return fail('Name must be 3 to 40 characters: letters, digits, spaces, - or _.');
  if(!VER_RE.test(b.version))return fail('Version must look like 1.0.');
  if(!all.length)return fail('Add at least one entry condition.');
  if(!(b.atr>=0.5&&b.atr<=10))return fail('Stop distance must be between 0.5 and 10 ATR.');
  for(const c of all){const p=COND[c.type].p;
    if(p.includes('length')&&!(c.length>=2&&c.length<=400))return fail('Lengths must be between 2 and 400.');
    if(p.includes('slow')&&!(c.slow>c.length&&c.slow<=800))return fail('Slow EMA length must be larger than the fast length.');
    if(c.type.startsWith('rsi')&&!(c.value>=1&&c.value<=99))return fail('RSI value must be between 1 and 99.');
    if(c.type==='volume_ratio_above'&&!(c.value>0&&c.value<=100))return fail('Volume multiple must be positive.')}
  const ex=STRATS.find(s=>s.n===b.name);if(ex&&ex.vs.includes(b.version))return fail(b.name+' v'+b.version+' already exists. Versions are immutable: use a new version number.');
  const ind=[...b.long.map(c=>'Long: '+condText(c)),...b.short.map(c=>'Short: '+condText(c))].join('; ');
  if(ex){ex.vs.push(b.version);ex.v=b.version;ex.risk=b.risk;ex.ind=ind;ex.par={atr_stop:b.atr,conditions:all.length}}
  else STRATS.push({n:b.name,vs:[b.version],v:b.version,risk:b.risk,ind,tf:'any',par:{atr_stop:b.atr,conditions:all.length},desc:'User-built rules. Held in this browser tab only (demo).',custom:true});
  audit(`Strategy added (demo): ${b.name} v${b.version}, ${all.length} conditions, stop ${b.atr} ATR`);
  AX.b={name:'',version:'1.0',risk:'Medium',atr:2,err:'',long:[{type:'ema_fast_above_slow',length:20,slow:50,value:30}],short:[]};
  closeModal();toast('Added '+b.name+' v'+b.version);if(S.page==='strategies')rerender()}
document.addEventListener('input',e=>{const t=e.target,b=AX.b;
  if(t.id==='bName')b.name=t.value=t.value.replace(/[^A-Za-z0-9 _\-]/g,'');
  else if(t.id==='bVer')b.version=t.value=t.value.replace(/[^0-9.]/g,'');
  else if(t.id==='bAtr')b.atr=+t.value;
  else if(t.dataset&&t.dataset.brow&&t.tagName==='INPUT'){const[s,i,f]=t.dataset.brow.split(':');b[s][+i][f]=+t.value}});
document.addEventListener('change',e=>{const t=e.target;
  if(t.id==='bRisk')AX.b.risk=t.value;
  else if(t.id==='fxEx'){$('#fxPassW').hidden=t.value!=='OKX'}
  else if(t.dataset&&t.dataset.brow&&t.tagName==='SELECT'){const[s,i,f]=t.dataset.brow.split(':');AX.b[s][+i][f]=t.value;stModal()}});

function botDo(id,a){const b=BOTS.find(x=>x.id===id);
  if(a==='config'){modal(`<h3>Configure Bot #${b.id}</h3><dl class="kv"><dt>Exchange</dt><dd>${b.ex}</dd><dt>Market</dt><dd>${b.m}</dd><dt>Strategy</dt><dd>${b.st} v${b.v}</dd><dt>Risk</dt><dd>${b.risk}% (max set by Risk Engine)</dd></dl><div class="mut">Editing is disabled in the demo. Changes will be recorded in the audit log.</div><button class="btn" data-act="close">Close</button>`);return}
  if(S.halted&&(a==='start'||a==='resume')){toast('Emergency stop is active. Clear it first.');return}
  const to={pause:'PAUSED',resume:'RUNNING',stop:'STOPPING',start:'STARTING'}[a];b.state=to;audit(`Bot #${b.id} ${a}`);rerender();
  if(a==='stop'||a==='start')setTimeout(()=>{b.state=a==='stop'?'STOPPED':'RUNNING';if(S.page==='paper'||S.page==='dashboard')rerender()},900)}
function doApply(){const st=S.pendingStrat||$('#aSt').value,pos=POS.find(p=>p.m===S.mkt);
  if(pos&&!S._skip){S.pendingStrat=st;modal(`<h3>Existing position detected</h3><div class="mut">${pos.m} ${pos.side} is open under ${pos.st}. How should it be handled?</div>
   ${[['keep','Keep position'],['close','Close position'],['prev','Let previous strategy manage it']].map((o,i)=>`<label class="opt"><input type="radio" name="ph" value="${o[0]}" ${i?'':'checked'}><span>${o[1]}</span></label>`).join('')}
   <div class="row"><button class="btn pri" data-act="applygo">Continue</button><button class="btn" data-act="applycancel">Cancel</button></div>`);return}
  commit(st,'none')}
function commit(st,how){S.activeStrat=st;S.pendingStrat=null;audit(`Strategy set to ${st} on ${S.exch} ${S.mkt}${how!=='none'?' (position: '+how+')':''}`);closeModal();toast('Applied '+st+' on '+S.exch+' '+S.mkt);if(S.page==='strategies')rerender()}

document.addEventListener('click',e=>{
  const t=e.target.closest('[data-range],[data-mkt],[data-go],[data-ind],[data-act],[data-bot],[data-det],[data-bt],[data-use],[data-of],[data-trade],[data-ev],[data-ex],[data-wl],[data-ax],[data-exrm]');if(!t)return;const d=t.dataset;
  if(d.range){S.range=d.range;rerender()}
  else if(d.mkt){S.mkt=d.mkt;$('#selMkt').value=d.mkt;if(S.page==='dashboard'||S.page==='markets')rerender();else go('markets')}
  else if(d.go)go(d.go);
  else if(d.ind){S.ind[d.ind]=S.ind[d.ind]?0:1;rerender()}
  else if(d.bot)botDo(d.bot,d.do);
  else if(d.det){const el=$('#det-'+d.det);el.hidden=!el.hidden}
  else if(d.bt){S.btSel=d.bt;go('backtest')}
  else if(d.use){S.activeStrat=d.use;if(S.page==='strategies'){const s=$('#aSt');if(s)s.value=d.use;doApply()}}
  else if(d.of){S.orderFilter=d.of;rerender()}
  else if(d.trade&&S.bt){const x=S.bt.trs[+d.trade-1],c=S.bt.cfg;$('#tradeDetail').innerHTML=`<h2>Trade #${x.n}</h2><dl class="kv"><dt>Side</dt><dd>${x.side}</dd><dt>Entry bar</dt><dd>${x.en}</dd><dt>Exit bar</dt><dd>${x.ex}</dd><dt>Duration</dt><dd>${x.ex-x.en} bars</dd><dt>P&amp;L</dt><dd class="${cls(x.pnl)}">${sgn(x.pnl)}</dd><dt>Strategy</dt><dd>${c.st} v${c.ver}</dd></dl><div class="mut" style="margin-top:6px">Reason: ${x.reason.join(', ')}</div>`}
  else if(d.ev){const ev=EVENTS[+d.ev];$('#evd').innerHTML=`<b>${ev.n}</b> &middot; ${ev.a}<br>${ev.ai}`}
  else if(d.ex){S.exch=d.ex;$('#selEx').value=d.ex;rerender();toast('Exchange: '+d.ex)}
  else if(d.wl){S.watch=d.wl;rerender()}
  else if(d.act==='close')closeModal();
  else if(d.act==='estop')estop();
  else if(d.act==='estopgo'){S.halted=true;BOTS.forEach(b=>b.state='STOPPED');const close=$('input[name=ep]:checked').value==='close';audit('EMERGENCY STOP triggered. Positions: '+(close?'close':'keep')+'. Orders cancelled: '+$('#epc').checked);closeModal();syncBanners();toast('Emergency stop recorded. Bots stopped.');rerender()}
  else if(d.act==='applygo'){const h=$('input[name=ph]:checked').value;S._skip=true;commit(S.pendingStrat,h);S._skip=false}
  else if(d.act==='applycancel'){S.pendingStrat=null;closeModal()}
  else if(d.act==='addex')exModal();
  else if(d.act==='addst'){AX.b.err='';stModal()}
  else if(d.act==='exgo')exSubmit();
  else if(d.act==='stgo')stSubmit();
  else if(d.ax)axAct(d.ax);
  else if(d.exrm){delete EX[d.exrm];$$('#selEx option').forEach(o=>{if(o.value===d.exrm)o.remove()});if(S.exch===d.exrm){S.exch='Binance';$('#selEx').value='Binance'}audit('Exchange removed: '+d.exrm);rerender()}
  else if(d.act==='newwl'){toast('Watchlist editing arrives with the persistence layer.')}
});
document.addEventListener('change',e=>{const t=e.target;
  if(t.id==='applyBtn')return;
  if(t.id==='aEx'){S.exch=t.value;$('#selEx').value=t.value;$('#adapt').innerHTML=adaptTiles()}
  else if(t.id==='aMk'){S.mkt=t.value;$('#selMkt').value=t.value;$('#adapt').innerHTML=adaptTiles()}
  else if(t.id==='bStrat')fillVer();
  else if(t.name==='smode'){S.stratMode=t.value;audit('Strategy mode: '+t.value);rerender()}
  else if(t.id==='minScore'){S.ostate.minScore=+t.value;rerender()}
  else if(t.id==='scTrend'){S.ostate.trend=t.value;rerender()}
  else if(t.id==='lk')renderLogRows();
});
document.addEventListener('input',e=>{if(e.target.id==='lq')renderLogRows()});
document.addEventListener('click',e=>{if(e.target.id==='applyBtn')doApply()});
document.addEventListener('submit',e=>{if(e.target.id==='btForm'){e.preventDefault();runBt();$('#btOut').innerHTML=btHtml(S.bt);drawPage()}});
let rz;window.addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(drawPage,120)});

/* ---------- real backend: Live Activity + Strategy Manager (no demo data on these pages) ---------- */
const DA=window.DewakoActivity,DP=window.DewakoPackages;
const BK={token:null,user:null,role:null,perms:[],base:(location.protocol==='http:'||location.protocol==='https:')?location.origin:'http://127.0.0.1:8000',err:'',busy:false};
const AM=new DA.Model(),AF={category:'',severity:'',status:'',strategy:'',q:'',since:'',until:''};
const AS={status:null,more:false,next:null,loadingMore:false,streaming:false,abort:null,attempt:0,tz:'local',lastRender:0,statusTimer:null,staleTimer:null,sel:null,loadErr:''};
const PK={list:null,err:'',up:null,result:null,busy:false,xhr:null};
const reduced=()=>window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function can(p){return BK.perms.indexOf(p)!==-1}
async function bk(path,opt){opt=opt||{};const h=Object.assign({},opt.headers||{});if(BK.token)h.Authorization='Bearer '+BK.token;if(opt.json!==undefined){h['Content-Type']='application/json'}
  try{const r=await fetch(BK.base+path,{method:opt.method||'GET',headers:h,body:opt.json!==undefined?JSON.stringify(opt.json):undefined,signal:opt.signal});let b=null;try{b=await r.json()}catch(e){}
    if(r.status===401&&BK.token&&path!=='/auth/login'){bkExpired()}return{status:r.status,body:b}}catch(e){return{status:0,body:null}}}
function bkExpired(){BK.token=null;BK.err='Your session has expired. Sign in again.';stopActivity();AM.setConn('unauthorized');if(S.page==='activity'||S.page==='pkgs')rerender()}
function loginPanel(why){return `<div class="card"><h2>Connect to the DEWAKO backend <span class="pill ai">REAL DATA</span></h2>
 <p class="mut" style="margin-top:0">${why} The other pages of this prototype still show simulated demo data; this page shows only what the backend returns. Your password is sent once to sign in and is never stored; the session token lives in memory only (a page reload signs you out).</p>
 <form class="bk-login" id="bkForm" autocomplete="off"><label>Backend URL<input id="bkUrl" value="${DA.escapeHtml(BK.base)}" inputmode="url" spellcheck="false"></label>
 <label>Username<input id="bkUser" autocomplete="username" required></label><label>Password<input id="bkPass" type="password" autocomplete="current-password" required></label>
 <button class="btn pri" type="submit" ${BK.busy?'disabled':''}>${BK.busy?'Signing in…':'Sign in'}</button></form>
 ${BK.err?`<div class="bk-err" role="alert">${DA.escapeHtml(BK.err)}</div>`:''}
 <div class="bk-note" style="margin-top:8px">Serve this page from the backend (<span class="mono">/ui</span>) so no cross-origin setup is needed. A different origin requires <span class="mono">DEWAKO_CORS_ORIGINS</span> on the server.</div></div>`}
async function doLogin(){const u=$('#bkUser').value,p=$('#bkPass').value;BK.base=($('#bkUrl').value||'').replace(/\/+$/,'');BK.err='';BK.busy=true;rerender();
  if(!/^https?:\/\//.test(BK.base)){BK.busy=false;BK.err='Enter the backend URL (http:// or https://).';return rerender()}
  const r=await bk('/auth/login',{method:'POST',json:{username:u,password:p}});
  if(r.status===200&&r.body&&r.body.token){BK.token=r.body.token;const me=await bk('/auth/me');if(me.status===200){BK.user=me.body.username;BK.role=me.body.role;BK.perms=me.body.permissions||[]}BK.busy=false;AM.denied=false;AM.setConn('idle');rerender()}
  else{BK.busy=false;BK.err=r.status===0?'Could not reach the backend at '+BK.base+'.':r.status===401?'Invalid username or password.':r.status===429?'Too many attempts. Wait and try again.':'Sign-in failed ('+r.status+').';rerender()}}
async function doLogout(){stopActivity();if(BK.token)await bk('/auth/logout',{method:'POST'});BK.token=BK.user=BK.role=null;BK.perms=[];AM.reset();AM.denied=false;AM.setConn('idle');PK.list=null;PK.result=null;rerender()}
function whoBar(){return `<span class="pill">${DA.escapeHtml(BK.user||'')} &middot; ${DA.escapeHtml(BK.role||'')}</span><button class="btn sm" data-bk="logout">Sign out</button>`}

/* ----- Live Activity ----- */
R.activity=()=>{if(!BK.token)return loginPanel('Sign in to see live backend activity.');
 const opt=(a,c)=>a.map(x=>`<option value="${x}" ${AF[c]===x?'selected':''}>${x||'All'}</option>`).join('');
 return `<div class="row sb"><div class="act-head"><h2 style="margin:0">Live Activity</h2><span class="pill ai">REAL BACKEND DATA</span><span class="pill warn">LIVE TRADING LOCKED</span></div><div class="row">${whoBar()}</div></div>
 <div class="card"><div class="act-head"><span class="act-status IDLE" id="actStatus" role="status"><i></i><span>Loading…</span></span><span class="act-conn" id="actConn" role="status"></span><span class="mut" id="actStatusWhy"></span></div>
  <div id="actAlert" style="margin-top:8px" aria-live="assertive"></div></div>
 <div class="card"><div class="act-filters"><label>Category<select id="fCat" data-f="category">${opt(['','system','auth','safety','signal','risk','order','reconciliation','strategy_package','ai','config'],'category')}</select></label>
  <label>Severity<select id="fSev" data-f="severity">${opt(['','INFO','WARNING','ERROR','CRITICAL'],'severity')}</select></label>
  <label>Status<input id="fStatus" data-f="status" value="${DA.escapeHtml(AF.status)}" placeholder="e.g. UNKNOWN" maxlength="30"></label>
  <label>Strategy<input id="fStrat" data-f="strategy" value="${DA.escapeHtml(AF.strategy)}" maxlength="60"></label>
  <label>From (UTC)<input id="fSince" type="datetime-local" data-f="since" value="${DA.escapeHtml(AF.since?AF.since.slice(0,16):'')}"></label>
  <label>To (UTC)<input id="fUntil" type="datetime-local" data-f="until" value="${DA.escapeHtml(AF.until?AF.until.slice(0,16):'')}"></label>
  <label>Search<input id="fQ" type="search" data-f="q" value="${DA.escapeHtml(AF.q)}" placeholder="summary, id, correlation" maxlength="100"></label></div>
  <div class="row" style="margin-top:8px"><button class="btn sm" data-bk="pause" id="btnPause" aria-pressed="false">Pause display</button><button class="btn sm" data-bk="refresh">Refresh now</button>
   <button class="btn sm" data-bk="tz" id="btnTz">Show UTC</button><button class="btn sm" data-bk="clear">Clear filters</button><span class="mut" id="actCount" aria-live="polite"></span></div></div>
 <div class="card"><div id="actBody"></div><div class="row" style="margin-top:8px"><button class="btn sm" data-bk="more" id="btnMore" hidden>Load older events</button></div></div>`};
function renderActivity(){if(S.page!=='activity'||!BK.token)return;
 const v=AM.view(),st=DA.statusView(AS.status),$s=$('#actStatus');if(!$s)return;
 $s.className='act-status '+(AS.status?st.level:'IDLE');$s.lastElementChild.textContent=AS.status?st.label:'Unknown';
 $('#actStatusWhy').textContent=(st.reasons||[]).join(' · ');
 const cm={live:['live','Connected: receiving live events'],connecting:['wait','Connecting…'],reconnecting:['wait','Disconnected: reconnecting…'],disconnected:['bad','Disconnected'],unauthorized:['bad','Not authorized'],idle:['wait','Not connected']}[AM.conn]||['wait',AM.conn];
 const stale=v==='stale';$('#actConn').className='act-conn '+(stale?'bad':cm[0]);$('#actConn').innerHTML='<i></i>'+DA.escapeHtml(stale?'Connected, but no data or heartbeat for 45 s: this view may be stale':cm[1]);
 let al='';if(st.unresolved)al+=`<div class="act-alert" role="alert">${st.unresolved} order(s) have an UNKNOWN outcome. RECONCILIATION REQUIRED. This screen cannot retry, cancel or submit anything.</div>`;
 if(st.killSwitch)al+=`<div class="act-alert" role="alert">Kill switch is ACTIVE.</div>`;
 if(AM.paused&&AM.pending.length)al+=`<div class="act-alert warn" role="status">Display paused: ${AM.pending.length} new event(s) held${AM.criticalPending()?`, <b>${AM.criticalPending()} critical</b>`:''}. Backend monitoring continues. Resume to show them.</div>`;
 if(v==='disconnected')al+=`<div class="act-alert warn">The event stream is disconnected. Events may be missing until it reconnects; it resumes from the last position it saw.</div>`;
 $('#actAlert').innerHTML=al;
 const rows=AM.visible(AF),b=$('#actBody');
 if(v==='denied')b.innerHTML=`<div class="act-empty" role="alert">Permission denied. Your role cannot read the activity feed, or your session ended.</div>`;
 else if(v==='loading')b.innerHTML=`<div class="act-empty" role="status">Loading activity…</div>`;
 else if(v==='error'||AS.loadErr)b.innerHTML=`<div class="act-empty" role="alert">Backend error: ${DA.escapeHtml(AS.loadErr||AM.error)} <button class="btn sm" data-bk="refresh">Retry</button></div>`;
 else if(!rows.length)b.innerHTML=`<div class="act-empty">${(AM.events.length||Object.keys(AF).some(k=>AF[k]))?'No events match these filters.':'No activity has been recorded yet. Events appear here when the system does something.'}</div>`;
 else{const prev=new Set([...b.querySelectorAll('li.ev')].map(x=>x.dataset.id));const first=!b.querySelector('ul.pipe');
  const keepTop=b.querySelector('ul.pipe')?b.querySelector('ul.pipe').scrollTop:0;
  b.innerHTML=`<ul class="pipe ${AM.conn==='live'&&!reduced()?'flow':''}" role="log" aria-live="off" aria-label="Activity timeline, newest first">${rows.map(e=>DA.renderEvent(e,AS.tz)).join('')}</ul>`;
  const ul=b.querySelector('ul.pipe');ul.scrollTop=keepTop;if(!first&&!reduced())[...ul.querySelectorAll('li.ev')].forEach(li=>{if(!prev.has(li.dataset.id))li.classList.add('enter')})}
 $('#actCount').textContent=`${rows.length} shown${AM.events.length>rows.length?' of '+AM.events.length+' loaded':''}`;
 $('#btnMore').hidden=!AS.more;$('#btnMore').disabled=AS.loadingMore;$('#btnPause').setAttribute('aria-pressed',AM.paused);$('#btnPause').textContent=AM.paused?'Resume display':'Pause display';$('#btnTz').textContent=AS.tz==='utc'?'Show local time':'Show UTC'}
function scheduleRender(){const n=Date.now();if(n-AS.lastRender>250){AS.lastRender=n;renderActivity()}else if(!scheduleRender.t){scheduleRender.t=setTimeout(()=>{scheduleRender.t=null;AS.lastRender=Date.now();renderActivity()},250)}}
async function loadHistory(more){AS.loadErr='';const q=DA.queryString(AF,more?AS.next:null,50);if(more)AS.loadingMore=true;
 const r=await bk('/activity?'+q);AS.loadingMore=false;
 if(r.status===200){AM.ingest(r.body.events,{});AS.more=!!r.body.has_more;AS.next=r.body.next_cursor}
 else if(r.status===401||r.status===403){AM.denied=true;AM.setConn('unauthorized')}else{AS.loadErr=r.status===0?'could not reach the backend':'the server returned '+r.status;AM.fail(AS.loadErr)}
 renderActivity()}
async function loadStatus(){const r=await bk('/activity/status');if(r.status===200){AS.status=r.body}else if(r.status===403||r.status===401){AM.denied=true}renderActivity()}
function handleFrame(f){if(f.comment){AM.touch();return}let d=null;try{d=f.data?JSON.parse(f.data):null}catch(e){return}
 if(f.event==='hello'){if(d&&d.cursor&&!AM.cursor)AM.setCursor(d.cursor);AM.setConn('live')}
 else if(f.event==='activity'){const crit=DA.isCritical(d||{});AM.ingest([d],{live:true});if(f.id)AM.setCursor(f.id);if(crit)loadStatus();scheduleRender()}
 else if(f.event==='cursor'){if(d&&d.cursor)AM.setCursor(d.cursor);AM.touch()}
 else if(f.event==='error'){AM.fail('the event stream reported a backend error');scheduleRender()}
 else if(f.event==='closed'){if(d&&d.reason==='unauthorized'){AM.setConn('unauthorized');AM.denied=true;stopActivity()}}}
async function streamLoop(){if(AS.streaming)return;AS.streaming=true;AM.setConn('connecting');renderActivity();
 while(AS.streaming&&S.page==='activity'&&BK.token){const ctl=new AbortController();AS.abort=ctl;
  try{const h={Authorization:'Bearer '+BK.token};if(AM.cursor)h['Last-Event-ID']=AM.cursor;
   const r=await fetch(BK.base+'/activity/stream',{headers:h,signal:ctl.signal});
   if(r.status===401||r.status===403){AM.setConn('unauthorized');AM.denied=true;break}
   if(!r.ok||!r.body)throw new Error('http '+r.status);
   AM.setConn('live');AS.attempt=0;AM.error=null;renderActivity();const rd=r.body.getReader(),dec=new TextDecoder(),p=new DA.SSEParser();
   for(;;){const x=await rd.read();if(x.done)break;p.feed(dec.decode(x.value,{stream:true})).forEach(handleFrame)}
  }catch(e){if(ctl.signal.aborted)break}
  if(!AS.streaming||S.page!=='activity')break;AM.setConn('reconnecting');renderActivity();await sleep(DA.backoff(AS.attempt++))}
 AS.streaming=false;if(AM.conn!=='unauthorized'&&S.page!=='activity')AM.setConn('idle');renderActivity()}
function stopActivity(){AS.streaming=false;if(AS.abort)AS.abort.abort();clearInterval(AS.statusTimer);clearInterval(AS.staleTimer);AS.statusTimer=AS.staleTimer=null}
function activityPost(){if(!BK.token)return;renderActivity();if(!AS.statusTimer){AS.statusTimer=setInterval(loadStatus,15000);AS.staleTimer=setInterval(renderActivity,5000)}
 if(!AM.loaded&&!AM.denied){loadHistory(false);loadStatus()}if(!AS.streaming&&!AM.denied)streamLoop()}
function refilter(){AM.reset();AS.more=false;AS.next=null;renderActivity();loadHistory(false)}

/* ----- Strategy Manager (backend packages) ----- */
R.pkgs=()=>{if(!BK.token)return loginPanel('Sign in to import and manage strategy packages.');
 const up=PK.up,res=PK.result;
 return `<div class="row sb"><div class="act-head"><h2 style="margin:0">Strategy Manager</h2><span class="pill ai">REAL BACKEND DATA</span><span class="pill warn">LIVE TRADING LOCKED</span></div><div class="row">${whoBar()}</div></div>
 <div class="card"><h2>Import Algo ZIP</h2>
  <p class="mut" style="margin-top:0">Choose a strategy package from this computer. The server validates it (it never runs anything inside it) and tells you whether it was accepted or rejected. Importing does <b>not</b> enable, select or trade a package. Format: <span class="mono">manifest.json</span>, <span class="mono">strategy/rules.json</span>, <span class="mono">parameters.json</span>, <span class="mono">metadata.json</span>; max 1 MiB; declarative rules only (no Python or scripts).</p>
  <div class="pk-drop" id="pkDrop"><input type="file" id="pkFile" accept=".zip,application/zip" class="sr-only" aria-label="Strategy package ZIP file">
   <button class="btn pri" data-bk="pick" ${up?'disabled':''} ${can('strategies')?'':'disabled title="Your role cannot import packages"'}>Import Algo ZIP</button>
   <div class="mut" style="margin-top:6px">or drop a .zip file here</div>
   ${up?`<div style="margin-top:10px" role="status"><div>${DA.escapeHtml(up.name)} (${Math.round(up.size/1024)} KiB): ${DA.escapeHtml(up.phase)}</div><div class="pk-prog" aria-hidden="true"><i style="width:${up.pct}%"></i></div><button class="btn sm" data-bk="cancelup" style="margin-top:6px">Cancel upload</button></div>`:''}</div>
  <div id="pkResult" aria-live="polite">${res?`<div class="pk-res ${res.ok?'ok':'bad'}" style="margin-top:10px" role="${res.ok?'status':'alert'}">${res.html}</div>`:''}</div></div>
 <div class="card"><h2>Packages <span class="row"><button class="btn sm" data-bk="pkrefresh">Refresh</button></span></h2><div id="pkList">${pkListHtml()}</div></div>`};
function pkListHtml(){if(PK.err)return `<div class="act-empty" role="alert">${DA.escapeHtml(PK.err)}</div>`;if(!PK.list)return `<div class="act-empty" role="status">Loading packages…</div>`;
 if(!PK.list.length)return `<div class="act-empty">No packages yet. Import an Algo ZIP above.</div>`;
 return `<div class="scroll"><table class="tbl"><tr><th>ID</th><th>Name</th><th>Version</th><th>Lifecycle</th><th>Fingerprint</th><th>Imported by</th><th></th></tr>${PK.list.map(DP.renderPackageRow).join('')}</table></div>`}
async function pkLoad(){PK.err='';const r=await bk('/strategy-packages');if(r.status===200)PK.list=r.body;else PK.err=DP.describeError(r.status,r.body).text;if(S.page==='pkgs'&&$('#pkList'))$('#pkList').innerHTML=pkListHtml()}
function pkgsPost(){if(!BK.token)return;if(PK.list===null&&!PK.err)pkLoad();const d=$('#pkDrop');if(!d)return;
 d.addEventListener('dragover',e=>{e.preventDefault();d.classList.add('over')});d.addEventListener('dragleave',()=>d.classList.remove('over'));
 d.addEventListener('drop',e=>{e.preventDefault();d.classList.remove('over');const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];if(f)pkUpload(f)});
 $('#pkFile').addEventListener('change',e=>{const f=e.target.files&&e.target.files[0];e.target.value='';if(f)pkUpload(f)})}
function pkShow(ok,html){PK.result={ok:ok,html:html};if(S.page==='pkgs')rerender()}
async function pkUpload(file){const bad=DP.checkFile(file);if(bad){return pkShow(false,`<b>Not uploaded.</b> ${DA.escapeHtml(bad)}`)}
 PK.result=null;PK.up={name:file.name,size:file.size,pct:0,phase:'reading file…'};rerender();
 let b64;try{b64=DP.toBase64(new Uint8Array(await file.arrayBuffer()))}catch(e){PK.up=null;return pkShow(false,'<b>Not uploaded.</b> The file could not be read.')}
 const xhr=new XMLHttpRequest();PK.xhr=xhr;xhr.open('POST',BK.base+'/strategy-packages');xhr.setRequestHeader('Authorization','Bearer '+BK.token);xhr.setRequestHeader('Content-Type','application/json');
 PK.up.phase='uploading…';rerender();
 xhr.upload.onprogress=e=>{if(e.lengthComputable&&PK.up){PK.up.pct=Math.round(e.loaded/e.total*100);const i=$('.pk-prog>i');if(i)i.style.width=PK.up.pct+'%'}};
 xhr.upload.onload=()=>{if(PK.up){PK.up.phase='validating on the server…';PK.up.pct=100;if(S.page==='pkgs')rerender()}};
 xhr.onload=()=>{PK.up=null;PK.xhr=null;let body=null;try{body=JSON.parse(xhr.responseText)}catch(e){}
  if(xhr.status===201&&body){pkShow(true,`<b>Accepted by the server.</b> ${DA.escapeHtml(body.name)} v${DA.escapeHtml(body.version)} &middot; ${DA.escapeHtml(DP.label(body.state)[0])}<br><span class="mono">SHA-256 ${DA.escapeHtml(body.package_sha256)}</span><br><span class="mono">Fingerprint ${DA.escapeHtml(body.fingerprint)}</span><br>Not enabled and not selected. Promotion is a separate, permissioned step.`)}
  else{const e=DP.describeError(xhr.status,body);pkShow(false,`<b>${e.kind==='rejected'?'Rejected by the server.':'Not imported.'}</b> ${DA.escapeHtml(e.text)}${e.kind==='rejected'?'<br>The attempt is recorded in the activity feed.':''}`)}
  if(xhr.status===401){bkExpired()}pkLoad()};
 xhr.onerror=()=>{PK.up=null;PK.xhr=null;pkShow(false,'<b>Upload failed.</b> Could not reach the server. The package was not imported (imports are all-or-nothing).')};
 xhr.onabort=()=>{PK.up=null;PK.xhr=null;pkShow(false,'<b>Upload cancelled</b> before the server confirmed anything. Refresh the list to see whether it was imported.');pkLoad()};
 xhr.send(JSON.stringify({archive_base64:b64}))}
async function pkDetail(id){const r=await bk('/strategy-packages/'+encodeURIComponent(id));if(r.status!==200)return toast(DP.describeError(r.status,r.body).text);
 modal(DP.renderDetail(r.body,can('trade_actions')||can('backtests'))+`<div class="row" style="margin-top:8px"><button class="btn sm" data-bk="pkactivity" data-name="${DA.escapeHtml(r.body.name||'')}">View activity for this package</button></div><div class="bk-err" id="pkDetErr" role="alert"></div>`);$('#modalBox').classList.add('wide')}
async function pkPromote(id,to){const r=await bk('/strategy-packages/'+encodeURIComponent(id)+'/transition',{method:'POST',json:{to:to,reason:'from dashboard'}});
 if(r.status===200){closeModal();toast('Now: '+DP.label(r.body.state)[0]);pkLoad()}else{const e=$('#pkDetErr');if(e)e.textContent=DP.describeError(r.status,r.body).text}}

document.addEventListener('click',e=>{const t=e.target.closest('[data-bk],[data-pk]');if(!t)return;const a=t.dataset.bk,k=t.dataset.pk;
 if(k==='detail')pkDetail(t.dataset.pid);else if(k==='promote')pkPromote(t.dataset.pid,t.dataset.to);
 else if(a==='logout')doLogout();else if(a==='pick')$('#pkFile').click();else if(a==='cancelup'){if(PK.xhr)PK.xhr.abort()}else if(a==='pkrefresh'){PK.list=null;rerender()}
 else if(a==='pause'){if(AM.paused){AM.resume()}else AM.pause();renderActivity()}else if(a==='refresh'){AS.loadErr='';AM.error=null;refilter();loadStatus()}
 else if(a==='tz'){AS.tz=AS.tz==='utc'?'local':'utc';renderActivity()}else if(a==='clear'){Object.keys(AF).forEach(x=>AF[x]='');rerender();refilter()}else if(a==='more')loadHistory(true)
 else if(a==='pkactivity'){closeModal();AF.q=t.dataset.name||'';go('activity')}});
document.addEventListener('submit',e=>{if(e.target.id==='bkForm'){e.preventDefault();doLogin()}});
document.addEventListener('change',e=>{const f=e.target.dataset&&e.target.dataset.f;if(!f||S.page!=='activity')return;let v=e.target.value;if(f==='since'||f==='until')v=v?new Date(v+'Z').toISOString():'';AF[f]=v;refilter()});
document.addEventListener('input',e=>{if(e.target.id==='fQ'&&S.page==='activity'){clearTimeout(input.t);input.t=setTimeout(()=>{AF.q=e.target.value;refilter()},350)}});
function input(){}
document.addEventListener('click',e=>{const li=e.target.closest('li.ev');if(!li||S.page!=='activity')return;const ev=AM.events.concat(AM.pending).find(x=>x.event_id===li.dataset.id);if(ev){modal(DA.renderDetails(ev)+'<div class="row"><button class="btn" data-act="close">Close</button></div>');$('#modalBox').classList.add('wide')}});
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches&&e.target.matches('li.ev')){e.preventDefault();e.target.click()}});

/* ---------- live simulation ---------- */
setInterval(()=>{
  MK.forEach(m=>{const prev=m.p,step=(Math.random()-.5)*.0012;m.p=+(m.p*(1+step)).toFixed(m.d);m.c=(m.p/m.open-1)*100;const l=m.cs[m.cs.length-1];l.c=m.p;l.h=Math.max(l.h,m.p);l.l=Math.min(l.l,m.p);
    $$(`[data-px="${m.s}"]`).forEach(e=>{e.textContent='$'+fmt(m.p,m.d);e.classList.remove('flash-up','flash-dn');void e.offsetWidth;e.classList.add(m.p>=prev?'flash-up':'flash-dn')});
    $$(`[data-chg="${m.s}"]`).forEach(e=>{e.textContent=sgn(m.c)+'%';e.className='num '+cls(m.c)});
    $$(`[data-px2="${m.s}"]`).forEach(e=>e.textContent=fmt(m.p,m.d))});
  updatePnl();if(S.page==='dashboard'||S.page==='markets'){drawPage();if(S.page==='markets'){updateBook();pushTrade()}}},1500);
setInterval(()=>{if(S.halted)return;const msgs=[['Market data received','sys'],['Event filter check: FOMC in '+Math.max(0,Math.round(EVENTS[0].t-(Date.now()%7200000)/60000))+'m','risk'],['Trend AI evaluated BTC/USDT: no new signal','sig'],['Risk engine heartbeat: limits within range','risk'],['AI refreshed market assessment','ai']];const x=msgs[Math.floor(Math.random()*msgs.length)];log(x[0],x[1])},5000);

syncBanners();go('dashboard');
})();
