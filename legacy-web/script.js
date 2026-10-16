/* Kalkulator Lot XAUUSD (IDR) — script.js
   Algoritma lot, tabel SL/TP, matriks risk %, dan Monte Carlo. */
const LOTS=Array.from({length:1000},(_,i)=>(i+1)/100); // 0.01 … 10.00, kelipatan 0.01
const MAXLOT=10; // batas lot otomatis: mentok 10.00, lebihnya = peringatan
const $=id=>document.getElementById(id);
function parseAmt(raw,isMoney){
  let s=String(raw).trim().toLowerCase().replace(/\s|rp|\$|idr|usd/g,"");if(!s)return NaN;
  let mult=1;
  const suf=s.match(/(jt|juta|rb|ribu|k|m)$/);
  if(suf){mult={jt:1e6,juta:1e6,m:1e6,rb:1e3,ribu:1e3,k:1e3}[suf[1]];s=s.slice(0,-suf[1].length);}
  const dots=(s.match(/\./g)||[]).length,commas=(s.match(/,/g)||[]).length;
  if(dots&&commas){ // mixed: last separator is decimal
    if(s.lastIndexOf(",")>s.lastIndexOf("."))s=s.replace(/\./g,"").replace(",",".");
    else s=s.replace(/,/g,"");
  }else if(commas>1)s=s.replace(/,/g,"");
  else if(dots>1)s=s.replace(/\./g,"");
  else if(commas===1){s=(isMoney&&!suf&&/^\d{1,3},\d{3}$/.test(s))?s.replace(",",""):s.replace(",",".");}
  else if(dots===1&&isMoney&&!suf&&/^\d{1,3}\.\d{3}$/.test(s))s=s.replace(".","");
  return parseFloat(s)*mult;
}
function num(id){return parseAmt($(id).value,id==="balance"||id==="kurs");}
const idr=x=>isFinite(x)?"Rp"+Math.round(x).toLocaleString("id-ID"):"–";
const usd=x=>isFinite(x)?"$"+x.toLocaleString("id-ID",{minimumFractionDigits:2,maximumFractionDigits:2}):"–";
const one=x=>x.toLocaleString("id-ID",{minimumFractionDigits:1,maximumFractionDigits:1});
const lotTxt=l=>l.toFixed(2);
let rows=[];

function calc(){
  const bal=num("balance"),kurs=num("kurs"),rp=num("risk"),ratio=num("ratio"),plan=num("plan");
  $("balHint").textContent=isFinite(bal)?`Terbaca: ${idr(bal)}`+(bal<100000?" — pastikan nominalnya benar (bisa ketik 1.500.000 atau 1,5jt)":""):"Contoh: 10.000.000 atau 10jt";
  $("balHint").className="hint"+(isFinite(bal)&&bal<100000?" bad":"");
  const ok=[bal,kurs,rp,ratio].every(v=>isFinite(v)&&v>0);
  if(!ok){["rIdr","rUsd","tIdr","tUsd"].forEach(i=>$(i).textContent="–");$("tbody").innerHTML=`<tr><td colspan="5" style="text-align:left;color:var(--muted)">Lengkapi balance, kurs, risk, dan rasio dengan angka lebih dari nol.</td></tr>`;$("pickmsg").textContent="";rows=[];tblArgs=null;clearTimeout(tblT);$("mx").innerHTML="";$("auto").classList.add("empty");setLot("–");convert();return;}
  const riskIdr=bal*rp/100,riskUsd=riskIdr/kurs,tgtIdr=riskIdr*ratio,tgtUsd=tgtIdr/kurs;
  $("rIdr").textContent=idr(riskIdr);$("rUsd").textContent=usd(riskUsd)+` · ${rp}% balance`;
  $("tIdr").textContent=idr(tgtIdr);$("tUsd").textContent=usd(tgtUsd)+` · 1:${ratio}`;
  rows=LOTS.map(lot=>{
    const sl=Math.round(riskUsd/(lot*10)*10)/10, tp=Math.round(sl*ratio*10)/10;
    return {lot,sl,tp,r:Math.round(sl*lot*10*kurs),t:Math.round(tp*lot*10*kurs)};
  });
  // pick: largest lot whose SL pips still >= planned SL
  let pick=-1;
  if(isFinite(plan)&&plan>0){rows.forEach((r,i)=>{if(r.sl>=plan)pick=i;});}
  renderTablesSoon(bal,kurs,rp,plan,pick);
  renderAuto(riskUsd,kurs,ratio,plan,bal,rp);
  convert();save();
}
// tabel utama + matriks dirender dengan debounce (sekarang 1000 baris, biar ngetik tetap lancar)
let tblT=0,tblArgs=null;
function renderTablesSoon(bal,kurs,rp,plan,pick){tblArgs=[bal,kurs,rp,plan,pick];clearTimeout(tblT);tblT=setTimeout(renderTablesNow,120);}
function renderTablesNow(){
  if(!tblArgs)return;const[bal,kurs,rp,plan,pick]=tblArgs;
  $("tbody").innerHTML=rows.map((r,i)=>`<tr class="${i===pick?"pick":""}${r.sl<10?" tight":""}"><td class="lot">${lotTxt(r.lot)}</td><td class="sl">${one(r.sl)}</td><td class="tp">${one(r.tp)}</td><td>${r.r.toLocaleString("id-ID")}</td><td>${r.t.toLocaleString("id-ID")}</td></tr>`).join("");
  $("pickmsg").textContent="";
  requestAnimationFrame(()=>{const tr=document.querySelector("#tbody tr.pick"),box=tr&&tr.closest(".tbl");
    if(tr&&box){box.scrollTop=tr.offsetTop-box.clientHeight/2+tr.offsetHeight/2;}});
  renderMatrix(bal,kurs,rp,plan);
}
const PCTS=[1,2,3,5,10,15,20,25,50,100];
function renderMatrix(bal,kurs,rp,plan){
  const pcts=[...new Set([...PCTS,rp])].sort((a,b)=>a-b);
  const hasPlan=isFinite(plan)&&plan>0;
  const fitIdx={};
  pcts.forEach(pc=>{const ru=bal*pc/100/kurs;let f=-1;
    if(hasPlan)LOTS.forEach((l,i)=>{if(Math.round(ru/(l*10)*10)/10>=plan)f=i;});fitIdx[pc]=f;});
  let h=`<thead><tr><th>Lot</th>${pcts.map(pc=>`<th class="${pc===rp?"me":""}">${pc.toLocaleString("id-ID")}%<small>${idr(bal*pc/100)}</small></th>`).join("")}</tr></thead><tbody>`;
  LOTS.forEach((l,i)=>{
    h+=`<tr><td class="lot">${lotTxt(l)}</td>`+pcts.map(pc=>{
      const pips=Math.round(bal*pc/100/kurs/(l*10)*10)/10;
      const cls=[pc===rp?"me":"",fitIdx[pc]===i?"fit":"",pips<10?"dead":""].filter(Boolean).join(" ");
      return `<td class="${cls}">${one(pips)}</td>`;}).join("")+`</tr>`;
  });
  $("mx").innerHTML=h+"</tbody>";
}
function setLot(t,bad){const el=$("aLot");el.textContent=t;el.style.color=bad?"var(--red)":"";}
function renderAuto(riskUsd,kurs,ratio,plan,bal,rp){
  const box=$("auto");
  if(!(isFinite(plan)&&plan>0)){box.classList.add("empty");setLot("–");
    $("aSub").textContent="Isi maksimal SL (pips) untuk menghitung lot otomatis.";return;}
  box.classList.remove("empty");
  const exact=riskUsd/(plan*10), lot=Math.floor(exact*100+1e-9)/100;
  const minRiskIdr=plan*0.01*10*kurs;
  if(lot<0.01){
    const needPct=minRiskIdr/bal*100, needBal=minRiskIdr/(rp/100);
    setLot("0.00");
    $("aSub").textContent=`Belum cukup untuk 0.01 lot. SL ${one(plan)} pips di 0.01 lot = ${idr(minRiskIdr)}, sedangkan risiko ${rp}% hanya ${idr(bal*rp/100)}. `+
      (needPct<=100?`Butuh risk minimal ${needPct.toFixed(2)}%, atau balance minimal ${idr(needBal)} di risk ${rp}%, atau SL maksimal ${one(Math.floor(riskUsd/0.1*10)/10)} pips.`:`Balance ${idr(bal)} bahkan tidak menutup 0.01 lot untuk SL ini — cek lagi nominal balance.`);
    $("aRisk").textContent=idr(minRiskIdr);$("aTp").textContent=one(plan*ratio);$("aTgt").textContent=idr(minRiskIdr*ratio);$("aPip").textContent=idr(0.1*kurs);return;}
  if(lot>MAXLOT){
    setLot("10.00+",true);
    $("aSub").textContent=`Butuh ${exact.toFixed(2)} lot — melebihi batas ${lotTxt(MAXLOT)} lot. Kecilkan risk % atau perbesar SL minimal ${one(Math.ceil(riskUsd/(MAXLOT*10)*10-1e-9)/10)} pips di ${lotTxt(MAXLOT)} lot.`;
    $("aRisk").textContent="–";$("aTp").textContent=one(plan*ratio)+" pips";$("aTgt").textContent="–";$("aPip").textContent="–";return;}
  const act=plan*lot*10*kurs;
  setLot(lot.toFixed(2));
  $("aSub").textContent=`SL ${one(plan)} pips · lot presisi ${exact.toFixed(3)} dibulatkan ke bawah`;
  $("aRisk").textContent=idr(act)+` (${(act/bal*100).toFixed(2)}%)`;
  $("aTp").textContent=one(plan*ratio)+" pips";
  $("aTgt").textContent=idr(act*ratio);
  $("aPip").textContent=idr(lot*10*kurs);
}
function convert(){}
function save(){try{const o={};["balance","kurs","risk","ratio","plan"].forEach(k=>o[k]=$(k).value);localStorage.setItem("lotxau2",JSON.stringify(o));}catch(e){}}
try{const o=JSON.parse(localStorage.getItem("lotxau2")||"null");if(o)Object.entries(o).forEach(([k,v])=>{if($(k)&&v!=null&&v!=="")$(k).value=v;});}catch(e){}
document.querySelectorAll("input").forEach(el=>el.addEventListener("input",()=>calc()));

// ---- kurs live (API publik, tanpa API key) ----
const KURS_SOURCES=[
  {name:"open.er-api.com",url:"https://open.er-api.com/v6/latest/USD",pick:d=>d&&d.rates&&d.rates.IDR},
  {name:"frankfurter.app",url:"https://api.frankfurter.app/latest?from=USD&to=IDR",pick:d=>d&&d.rates&&d.rates.IDR}
];
function setStatus(t,cls){const el=$("kursStatus");el.textContent=t;el.className="hint"+(cls?" "+cls:"");}
function saveMode(m){try{localStorage.setItem("lotxau_kursmode",m);}catch(e){}}
async function fetchWithTimeout(url,ms){
  const ctl=new AbortController(),t=setTimeout(()=>ctl.abort(),ms);
  try{const r=await fetch(url,{signal:ctl.signal});if(!r.ok)throw new Error("HTTP "+r.status);return await r.json();}
  finally{clearTimeout(t);}
}
async function fetchLive(){
  setStatus("Mengambil kurs terbaru…");
  for(const src of KURS_SOURCES){
    try{
      const rate=+src.pick(await fetchWithTimeout(src.url,8000));
      if(!isFinite(rate)||rate<1000)continue;
      $("kurs").value=Math.round(rate).toLocaleString("id-ID");
      setStatus(`Kurs live ${src.name} · ${new Date().toLocaleString("id-ID",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}`,"ok");
      calc();return;
    }catch(e){/* coba sumber berikutnya */}
  }
  setStatus("Kurs live gagal diambil (cek koneksi internet). Isi kurs manual atau coba lagi.","bad");
}
$("liveBtn").addEventListener("click",()=>{saveMode("live");fetchLive();});
$("kurs").addEventListener("input",()=>{saveMode("manual");setStatus("Memakai kurs yang kamu isi manual.");});
document.querySelectorAll("[data-kurs]").forEach(btn=>btn.addEventListener("click",()=>{
  $("kurs").value=(+btn.dataset.kurs).toLocaleString("id-ID");saveMode("fixed");
  setStatus("Memakai kurs tetap Rp10.000 (akun fixed rate).");calc();}));

// ---- export CSV ----
$("exportBtn").hidden=false;
$("exportBtn").addEventListener("click",()=>{
  if(!rows.length)return;
  const csv=["Lot,SL Pips,TP Pips,Risk (IDR),Target (IDR)",...rows.map(r=>[lotTxt(r.lot),r.sl,r.tp,r.r,r.t].join(","))].join("\n");
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));
  a.download=`xauusd-lot-${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
});

// ---- start ----
(function init(){
  let mode="live";try{mode=localStorage.getItem("lotxau_kursmode")||"live";}catch(e){}
  if(mode==="live")fetchLive();
  else setStatus(mode==="fixed"?"Memakai kurs tetap Rp10.000 (akun fixed rate).":"Memakai kurs tersimpan. Tekan \u201cPakai kurs sekarang\u201d untuk kurs terbaru.");
})();

// ---- Monte Carlo ----
function pctl(a,p){if(!a.length)return NaN;const i=(a.length-1)*p,lo=Math.floor(i),hi=Math.ceil(i);return a[lo]+(a[hi]-a[lo])*(i-lo);}
let mcTimer=null,MC=null;
function scheduleMC(){clearTimeout(mcTimer);mcTimer=setTimeout(runMC,250);}
function runMC(){
  const bal=num("balance"),rp=num("risk")/100,rr=num("ratio"),sl=num("plan");
  const wr=num("wr")/100,N=Math.max(1,Math.min(2000,Math.round(num("nTr")))),S=Math.max(200,Math.min(20000,Math.round(num("nSim"))));
  const cost=Math.max(0,num("cost")||0),dd=num("ddLim")/100,comp=$("comp").checked;
  if(![bal,rp,rr,wr].every(v=>isFinite(v)&&v>0)||wr>=1&&false){$("mcVerdict").textContent="Lengkapi input di atas.";MC=null;return;}
  const cR=(isFinite(sl)&&sl>0)?cost/sl:0;         // biaya dalam satuan R
  const win=rr-cR,loss=1+cR;                        // hasil per trade dalam R
  const exp=wr*win-(1-wr)*loss, beWr=loss/(win+loss);
  const paths=new Array(N+1).fill(0).map(()=>new Float64Array(S));
  const ends=new Float64Array(S),mdd=new Float64Array(S),streak=new Int32Array(S);let hitDD=0,blown=0;
  for(let k=0;k<S;k++){
    let b=bal,peak=bal,worst=0,run=0,best=0,hit=false;paths[0][k]=b;
    for(let t=1;t<=N;t++){
      const riskAmt=comp?b*rp:bal*rp;
      if(b>0){ if(Math.random()<wr){b+=riskAmt*win;run=0;} else {b-=riskAmt*loss;run++;if(run>best)best=run;} }
      if(b<=0){b=0;}
      if(b>peak)peak=b;const d=(peak-b)/peak;if(d>worst)worst=d;if(d>=dd)hit=true;
      paths[t][k]=b;
    }
    ends[k]=b;mdd[k]=worst;streak[k]=best;if(hit)hitDD++;if(b<=bal*0.01)blown++;
  }
  const bands=paths.map(col=>{const a=Array.from(col).sort((x,y)=>x-y);return[.05,.25,.5,.75,.95].map(q=>pctl(a,q));});
  const e=Array.from(ends).sort((a,b)=>a-b),m=Array.from(mdd).sort((a,b)=>a-b),st=Array.from(streak).sort((a,b)=>a-b);
  const pProfit=e.filter(x=>x>bal).length/S;
  MC={bal,N,S,dd,bands,ends:e};
  const med=pctl(e,.5);
  $("mcVerdict").innerHTML=`Peluang profit setelah ${N} trade: <span class="${pProfit>=.5?"g":"r"}">${Math.round(pProfit*100)}%</span>`;
  $("mcSub").textContent=`Ekspektasi ${exp>=0?"+":""}${exp.toFixed(2)}R per trade`+(cR?` (sudah dipotong biaya ${one(cost)} pips = ${cR.toFixed(2)}R)`:"")+`. Win rate impas: ${(beWr*100).toFixed(1)}%.`+(exp<0?" Dengan angka ini sistemnya rugi dalam jangka panjang.":"");
  const cell=(v,l)=>`<div><b>${v}</b><span>${l}</span></div>`;
  $("mcStats").innerHTML=[
    cell(idr(med),"Median balance akhir"),
    cell(`${med>=bal?"+":""}${((med/bal-1)*100).toFixed(1)}%`,"Median return"),
    cell(idr(pctl(e,.05)),"Skenario buruk (5%)"),
    cell(idr(pctl(e,.95)),"Skenario bagus (95%)"),
    cell((pctl(m,.5)*100).toFixed(1)+"%","Median max drawdown"),
    cell((pctl(m,.95)*100).toFixed(1)+"%","Drawdown terburuk (95%)"),
    cell(Math.round(hitDD/S*100)+"%",`Peluang drawdown ≥ ${Math.round(dd*100)}%`),
    cell(Math.round(pctl(st,.5))+"×",`Median loss beruntun terpanjang (95%: ${Math.round(pctl(st,.95))}×)`)
  ].join("")+(blown?cell(Math.round(blown/S*100)+"%","Akun habis"):"");
  drawMC();
}
function cssv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}
function cv(c){if(!c.dataset.h)c.dataset.h=c.getAttribute("height");const dpr=Math.min(window.devicePixelRatio||1,3),w=Math.min(c.clientWidth||320,1600),h=+c.dataset.h;c.width=w*dpr;c.height=h*dpr;c.style.height=h+"px";
  const x=c.getContext&&c.getContext("2d");if(!x)return null;x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);x.font="11px 'Instrument Sans',sans-serif";return{x,w,h};}
const short=v=>{const a=Math.abs(v);return a>=1e9?"Rp"+(v/1e9).toFixed(1).replace(".",",")+"M":a>=1e6?"Rp"+(v/1e6).toFixed(1).replace(".",",")+"jt":a>=1e3?"Rp"+Math.round(v/1e3)+"rb":"Rp"+Math.round(v);};
function drawMC(){
  if(!MC)return;const {bands,N,bal,dd,ends,S}=MC;
  let g=cv($("mcFan"));if(g){const{x,w,h}=g,L=58,R=8,T=10,B=22;
    const floor=bal*(1-dd);let lo=Math.min(floor,...bands.map(b=>b[0])),hi=Math.max(bal,...bands.map(b=>b[4]));const pd=(hi-lo)*.05||1;lo-=pd;hi+=pd;
    const X=t=>L+(w-L-R)*t/N,Y=v=>T+(h-T-B)*(1-(v-lo)/(hi-lo));
    x.strokeStyle=cssv("--line");x.fillStyle=cssv("--muted");
    for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,y=Y(v);x.beginPath();x.moveTo(L,y);x.lineTo(w-R,y);x.stroke();x.textAlign="right";x.fillText(short(v),L-5,y+4);}
    x.textAlign="center";const step=Math.max(1,Math.ceil(N/5/10)*10);for(let t=step;t<=N;t+=step)x.fillText(t+" trade",Math.min(X(t),w-28),h-6);
    const band=(a,b,col)=>{x.beginPath();bands.forEach((q,t)=>t?x.lineTo(X(t),Y(q[a])):x.moveTo(X(t),Y(q[a])));for(let t=N;t>=0;t--)x.lineTo(X(t),Y(bands[t][b]));x.closePath();x.fillStyle=col;x.fill();};
    band(0,4,cssv("--band1"));band(1,3,cssv("--band2"));
    x.strokeStyle=cssv("--ink");x.lineWidth=1;x.beginPath();x.moveTo(L,Y(bal));x.lineTo(w-R,Y(bal));x.stroke();
    x.strokeStyle=cssv("--red");x.setLineDash([5,4]);x.beginPath();x.moveTo(L,Y(floor));x.lineTo(w-R,Y(floor));x.stroke();x.setLineDash([]);
    x.strokeStyle=cssv("--gold");x.lineWidth=2.5;x.beginPath();bands.forEach((q,t)=>t?x.lineTo(X(t),Y(q[2])):x.moveTo(X(t),Y(q[2])));x.stroke();}
  g=cv($("mcHist"));if(g){const{x,w,h}=g,L=34,R=6,T=8,B=22,k=24;
    const lo=ends[Math.floor(S*.005)],hi=ends[Math.ceil(S*.995)-1];const stp=(hi-lo)/k||1;const cnt=new Array(k).fill(0);
    ends.forEach(v=>{const i=Math.min(k-1,Math.max(0,Math.floor((v-lo)/stp)));cnt[i]++;});
    const mx=Math.max(...cnt),bw=(w-L-R)/k;x.fillStyle=cssv("--muted");x.textAlign="right";x.fillText(Math.round(mx/S*100)+"%",L-4,T+8);
    cnt.forEach((c,i)=>{const bh=(h-T-B)*c/mx,mid=lo+stp*(i+.5);x.fillStyle=mid>=bal?cssv("--good"):cssv("--red");x.fillRect(L+i*bw+1,h-B-bh,Math.max(1,bw-2),bh);});
    x.fillStyle=cssv("--muted");x.textAlign="left";x.fillText(short(lo),L,h-6);x.textAlign="right";x.fillText(short(hi),w-R,h-6);
    const bx=L+(w-L-R)*(bal-lo)/(hi-lo);if(bx>L&&bx<w-R){x.strokeStyle=cssv("--ink");x.setLineDash([3,3]);x.beginPath();x.moveTo(bx,T);x.lineTo(bx,h-B);x.stroke();x.setLineDash([]);x.textAlign="center";x.fillText("modal",bx,h-6);}}
}
["wr","nTr","cost","ddLim","nSim","comp"].forEach(id=>$(id).addEventListener(id==="comp"?"change":"input",scheduleMC));
let rsz;window.addEventListener("resize",()=>{clearTimeout(rsz);rsz=setTimeout(drawMC,150);});
try{const mq=matchMedia("(prefers-color-scheme: dark)");mq.addEventListener&&mq.addEventListener("change",drawMC);}catch(e){}
try{const o=JSON.parse(localStorage.getItem("lotxau_mc")||"null");if(o)Object.entries(o).forEach(([k,v])=>{if($(k)){if(k==="comp")$(k).checked=v;else $(k).value=v;}});}catch(e){}
const _save=save;save=function(){_save();try{const o={};["wr","nTr","cost","ddLim","nSim"].forEach(k=>o[k]=$(k).value);o.comp=$("comp").checked;localStorage.setItem("lotxau_mc",JSON.stringify(o));}catch(e){}};
const _calc=calc;calc=function(){_calc();scheduleMC();};
calc();

// ---- Rules Recovery (tabel 1-35, pola: naik +20%, SL 10%, X recovery 7,41%) ----
// Balance rules n = round(200000 x 1.2^(n-1)) — terverifikasi persis sama dengan PDF tabel.
const RULE_BALANCES=[200000,240000,288000,345600,414720,497664,597197,716636,859963,1031956,1238347,1486017,1783220,2139864,2567837,3081404,3697685,4437222,5324667,6389600,7667520,9201024,11041229,13249475,15899369,19079243,22895092,27474110,32968932,39562719,47475263,56970315,68364378,82037254,98444705];
const ruleSel=$("ruleSel"),ruleInfo=$("ruleInfo");
let ruleState={sel:"",mode:"normal"}; // mode: normal = main rules (10% · 1:2) | rec = recovery (X% · 1:1)
// round-half-up eksak (tanpa galat float): round(B x m/10)
const rDiv10=(v,d)=>Math.floor((v*d+5)/10);           // d=9 -> 0.9 ; d=12 -> 1.2
function ruleData(n){
  const B=RULE_BALANCES[n-1],after=rDiv10(B,9),sl=B-after;
  const prev=n>=2?RULE_BALANCES[n-2]:null;
  const diff=prev!=null?after-prev:B-after;           // rules 1: balik ke balance awal
  const next=rDiv10(B,12);
  return{n,B,after,sl,prev,next,diff,x:diff/after*100};
}
function fillInputs(bal,risk,ratio){$("balance").value=bal.toLocaleString("id-ID");$("risk").value=risk;$("ratio").value=ratio;calc();}
function renderRule(){
  const d=ruleData(+ruleState.sel),xpct=d.x.toFixed(2).replace(".",",");
  ruleInfo.hidden=false;
  $("riBal").textContent=idr(d.B);
  $("riBalL").textContent="Balance Rules "+d.n;
  $("riUp").textContent="+"+idr(d.next);
  $("riUpL").textContent=d.n<35?`Menang +20% → Rules ${d.n+1}`:"Menang +20% (di luar tabel)";
  $("riAft").textContent=idr(d.after);
  $("riAftL").textContent=`Setelah SL 10% (−${idr(d.sl)})`;
  $("riX").textContent=xpct+"%";
  $("riXL").textContent=`X recovery: +${idr(d.diff)} → ${d.prev!=null?"Rules "+(d.n-1):"balance awal"}`;
  if(ruleState.mode==="rec"){
    $("recBtn").textContent="← Kembali ke mode rules (10% · 1:2)";
    $("riNote").textContent=`Mode recovery aktif: balance = sisa setelah SL, risk ${xpct}% dengan rasio 1:1 — sekali menang balik persis ke balance Rules ${d.prev!=null?d.n-1:d.n}.`;
  }else{
    $("recBtn").textContent=`Kalau SL → isi mode recovery (${xpct}% · 1:1)`;
    $("riNote").textContent=`Mode rules: risk 10% dengan rasio 1:2 → menang = +20% naik ke rules berikutnya. Isi "Maksimal SL (pips)" di bawah — lot langsung terhitung.`;
  }
}
function applyRuleMode(){
  const d=ruleData(+ruleState.sel);
  if(ruleState.mode==="rec")fillInputs(d.after,d.x.toFixed(2).replace(".",","),"1");
  else fillInputs(d.B,"10","2");
  renderRule();
}
function saveRule(){try{localStorage.setItem("lotxau_rules",JSON.stringify(ruleState));}catch(e){}}
RULE_BALANCES.forEach((b,i)=>{const o=document.createElement("option");o.value=String(i+1);o.textContent=`Rules ${i+1} · ${idr(b)}`;ruleSel.appendChild(o);});
ruleSel.addEventListener("change",()=>{
  ruleState.sel=ruleSel.value;ruleState.mode="normal";
  if(ruleState.sel)applyRuleMode();else ruleInfo.hidden=true;
  saveRule();
});
$("recBtn").addEventListener("click",()=>{
  if(!ruleState.sel)return;
  ruleState.mode=ruleState.mode==="rec"?"normal":"rec";
  applyRuleMode();saveRule();
});
try{
  const s=JSON.parse(localStorage.getItem("lotxau_rules")||"null");
  if(s&&s.sel&&RULE_BALANCES[+s.sel-1]!=null){ruleState={sel:String(s.sel),mode:s.mode==="rec"?"rec":"normal"};ruleSel.value=ruleState.sel;applyRuleMode();}
}catch(e){}
