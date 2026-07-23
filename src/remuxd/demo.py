"""The optional browser demo (served only with ``--demo``).

A reference client for the engine: paste a URL (or resolve an AniList id), play the
returned HLS, and render ASS subs with JASSUB using the session-scoped URLs from
``/start``. Not part of the engine.
"""
import os
from typing import Optional, Tuple

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def asset(name: str) -> Tuple[Optional[bytes], str]:
    """(bytes, content-type) for a demo asset, or (None, "") if missing or if the
    name escapes the assets dir."""
    full = os.path.normpath(os.path.join(_ASSETS, name))
    if not full.startswith(_ASSETS) or not os.path.isfile(full):
        return None, ""
    ctype = ("text/javascript" if full.endswith(".js")
             else "application/wasm" if full.endswith(".wasm")
             else "font/woff2" if full.endswith(".woff2")
             else "application/octet-stream")
    with open(full, "rb") as f:
        return f.read(), ctype


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>remuxd</title>
<style>
 body{font:15px system-ui;margin:0;background:#0b0b0d;color:#eee}
 header{padding:1rem 1.5rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;
        border-bottom:1px solid #222;position:sticky;top:0;background:#0b0b0d}
 input{flex:1;min-width:280px;padding:.55rem .7rem;background:#17171b;border:1px solid #333;
       color:#eee;border-radius:6px;font-size:14px}
 select,button{padding:.55rem .8rem;background:#2a2a31;color:#eee;border:1px solid #333;
       border-radius:6px;cursor:pointer;font-size:14px}
 button{background:#3b6ef5;border-color:#3b6ef5}
 button:disabled{opacity:.5;cursor:default}
 main{padding:1.5rem;max-width:1100px;margin:0 auto}
 video{width:100%;background:#000;border-radius:8px}
 #status{padding:.5rem 1.5rem;color:#9aa;font-size:13px;min-height:1.2em}
 code{color:#bcd}
 #list{list-style:none;padding:0;margin:1rem 0}
 #list li{padding:.5rem .7rem;border:1px solid #222;border-radius:6px;margin-bottom:.4rem;
          cursor:pointer;display:flex;gap:.6rem;align-items:center}
 #list li:hover{background:#17171b;border-color:#3b6ef5}
 #list .g{font-weight:600;min-width:9rem}
 #list .meta{color:#9aa;font-size:12px;flex:1}
 #list .badge{font-size:11px;padding:.1rem .4rem;border-radius:4px;background:#222}
 .copy{color:#5bd67d}.tx{color:#e0b050}
</style></head><body>
<header>
 <input id="anilist" placeholder="AniList id (e.g. 108465 or 108465:1:5)" style="max-width:16rem">
 <button id="find">Find</button>
 <input id="url" placeholder="…or paste an .mkv URL">
 <select id="mode" title="remux = copy video (fast); transcode = re-encode to H.264">
   <option value="remux" selected>remux (copy)</option>
   <option value="auto">auto</option>
   <option value="transcode">transcode</option>
 </select>
 <button id="go">Play</button>
</header>
<div id="status"></div>
<main>
 <div id="subbar" style="margin-bottom:.6rem;display:none">
   <label style="color:#9aa;font-size:13px">Audio:
     <select id="audsel" style="margin-left:.4rem;margin-right:1rem"></select>
   </label>
   <label style="color:#9aa;font-size:13px">Subtitles:
     <select id="subsel" style="margin-left:.4rem"></select>
   </label>
 </div>
 <div id="vwrap" style="position:relative;line-height:0">
   <video id="v" controls playsinline></video>
 </div>
 <ul id="list"></ul>
</main>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script src="/static/jassub.umd.js"></script>
<script>
const $=id=>document.getElementById(id), v=$('v');
let hls, subBase='', subState=null, streamInfo=null;
function status(t){$('status').innerHTML=t;}
v.addEventListener('error',()=>{
  if(v.error) status('video error: <code>'+(v.error.message||('code '+v.error.code))+'</code>');
});
function clearSubs(){
  if(subState&&subState.poll){clearInterval(subState.poll);}
  if(subState&&subState.jassub){try{subState.jassub.destroy();}catch(e){}}
  subState=null;
  const sel=$('subsel'); sel.innerHTML=''; $('subbar').style.display='none';
}
// Subtitles are styled ASS rendered client-side by JASSUB (preserves signs,
// karaoke, typesetting). Fonts (mkv attachments) are fetched from the session's
// fontlist URL. The .ass files are extracted in the background, so we poll until
// the selected one has real Dialogue events before creating the JASSUB instance.
async function fetchAss(url){
  try{
    const r=await fetch(url+'?t='+Date.now(),{cache:'no-store'});
    if(!r.ok) return null;
    const txt=await r.text();
    return txt.indexOf('Dialogue:')>=0 ? txt : null;   // wait for real events
  }catch(e){ return null; }
}
function renderAss(text){
  const s=subState; if(!s) return;
  if(s.jassub){ try{s.jassub.destroy();}catch(e){} s.jassub=null; }
  try{
    const j=new JASSUB({
      video:v, subContent:text, fonts:s.fonts,
      workerUrl:'/static/jassub-worker.js',
      wasmUrl:'/static/jassub-worker.wasm',
      onDemandRender:false,
      availableFonts:{'liberation sans':'/static/default.woff2'},
      fallbackFont:'liberation sans'
    });
    j.addEventListener('ready',()=>{ try{j.resize();}catch(e){}
      status(subBase+' &nbsp;·&nbsp; <span style="color:#5bd67d">subs ✓ rendering</span>'); });
    j.addEventListener('error',e=>{
      const m=(e&&e.error&&e.error.message)||e.message||'unknown';
      status(subBase+' &nbsp;·&nbsp; <span style="color:#e06">subs worker error: '+m+'</span>');
      console.error('JASSUB error', e); });
    const nudge=()=>{ if(s.jassub===j){ try{j.resize();}catch(e){} } };
    ['loadedmetadata','loadeddata','playing','seeked','resize'].forEach(ev=>
      v.addEventListener(ev, nudge));
    [100,300,600,1200,2000].forEach(ms=>setTimeout(nudge, ms));
    s.jassub=j;
  }catch(e){ status(subBase+' &nbsp;·&nbsp; <span style="color:#e06">subs error: '+e.message+'</span>'); }
}
async function showTrack(idx){
  const s=subState; if(!s) return;
  s.cur=idx;
  if(s.jassub){ try{s.jassub.destroy();}catch(e){} s.jassub=null; }
  s.assText='';
  if(idx<0) return;                                   // "Off"
  const text=await fetchAss(s.tracks[idx].url);
  if(text){ s.assText=text; renderAss(text); }        // else the poll retries
}
function hasCueNear(ass, t){
  const re=/^Dialogue:[^,]*,(\d+):(\d\d):(\d\d(?:\.\d+)?),(\d+):(\d\d):(\d\d(?:\.\d+)?)/gm; let m;
  while((m=re.exec(ass))){
    const a=(+m[1])*3600+(+m[2])*60+parseFloat(m[3]);
    const b=(+m[4])*3600+(+m[5])*60+parseFloat(m[6]);
    if(t>=a-20 && t<=b+5) return true;
  }
  return false;
}
function mergeCues(newAss){
  const s=subState; if(!s||!s.assText) return;
  const NL=String.fromCharCode(10);
  const have=new Set(s.assText.split(NL).filter(l=>l.startsWith('Dialogue:')));
  const add=newAss.split(NL).filter(l=>l.startsWith('Dialogue:')&&!have.has(l));
  if(!add.length) return;
  const CR=String.fromCharCode(13);
  let base=s.assText; while(base.endsWith(NL)||base.endsWith(CR)) base=base.slice(0,-1);
  s.assText=base+NL+add.join(NL)+NL;
  if(s.jassub){ try{s.jassub.setTrack(s.assText);}catch(e){renderAss(s.assText);} }
}
let subwinBusy=false;
async function subSeekFill(){
  const s=subState; if(!s||s.cur<0||subwinBusy) return;
  if(!streamInfo||!streamInfo.subwindow) return;      // only seek-copy sessions
  const t=v.currentTime;
  if(hasCueNear(s.assText, t)) return;
  subwinBusy=true;
  try{
    const r=await fetch(streamInfo.subwindow+'?n='+s.cur+'&t='+Math.floor(t),{cache:'no-store'});
    if(r.ok){ const txt=await r.text(); if(txt.indexOf('Dialogue:')>=0) mergeCues(txt); }
  }catch(e){}
  finally{ subwinBusy=false; }
}
v.addEventListener('seeked', subSeekFill);
function loadSubs(tracks){
  clearSubs();
  const sel=$('subsel');
  const s={tracks, jassub:null, cur:-1, seen:tracks.map(()=>0), fonts:[], poll:null};
  subState=s;
  sel.innerHTML='<option value="-1">Off</option>'+
    tracks.map((t,i)=>`<option value="${i}">${t.label}${t.lang?' ('+t.lang+')':''}</option>`).join('');
  const def=tracks.findIndex(t=>t.default);
  const start=def>=0?def:0;
  sel.value=String(start);
  $('subbar').style.display='';
  sel.onchange=()=>showTrack(parseInt(sel.value));
  status(subBase+' &nbsp;·&nbsp; <span style="color:#5bd67d">subs ✓</span> ('+tracks.length+' tracks)');

  const len=async(url)=>{
    try{
      const h=await fetch(url,{headers:{Range:'bytes=0-0'},cache:'no-store'});
      const cr=h.ok?h.headers.get('Content-Range'):null;
      return cr&&cr.includes('/') ? parseInt(cr.split('/')[1]) : (h.ok?0:-1);
    }catch(e){ return -1; }
  };
  let ticks=0, started=false;
  s.poll=setInterval(async()=>{
    ticks++;
    if(ticks>1200){clearInterval(s.poll);return;}
    if(streamInfo&&streamInfo.fontlist){
      try{
        const fr=await fetch(streamInfo.fontlist,{cache:'no-store'});
        if(fr.status===404){clearInterval(s.poll);return;}   // session gone -> stop polling
        if(fr.ok&&!s.fonts.length) s.fonts=await fr.json();
      }catch(e){}
    }
    const idx=s.cur>=0?s.cur:start;
    const L=await len(tracks[idx].url);
    if(L<0) return;
    if(!started){ await showTrack(start); if(s.jassub) started=true; return; }
    if(s.cur>=0 && L>s.seen[s.cur]){
      s.seen[s.cur]=L;
      const text=await fetchAss(tracks[s.cur].url);
      if(text) mergeCues(text);
    }
  },3000);
}
function attach(src, direct){
  if(hls){hls.destroy();hls=null;}
  if(direct){ v.src=src; v.play().catch(()=>{}); return; }
  if(v.canPlayType('application/vnd.apple.mpegurl')){v.src=src;}
  else if(window.Hls&&Hls.isSupported()){
    hls=new Hls({
      lowLatencyMode:false, startPosition:0,
      liveSyncDuration:86400, liveMaxLatencyDuration:172800,
      backBufferLength:Infinity,
      // buffer aggressively forward: the server prefetches fragments ahead of
      // the playhead, so let the client hold a big forward buffer too.
      maxBufferLength:180, maxMaxBufferLength:1800, maxBufferSize:800*1000*1000
    });
    hls.loadSource(src); hls.attachMedia(v);
    hls.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal)status('playback error: <code>'+d.details+'</code>');});
  } else {status('this browser has no HLS support');return;}
  v.play().catch(()=>{});
  if(!direct){
    let synced=false;
    const sync=()=>{
      if(synced||v.readyState<2) return; synced=true;
      v.removeEventListener('playing',sync); v.removeEventListener('timeupdate',sync);
      const t=v.currentTime;
      try{ v.currentTime = t + 0.001; }catch(e){}
    };
    v.addEventListener('playing',sync);
    v.addEventListener('timeupdate',sync);
  }
}
let playCtx=null;
async function playUrl(url, mode, headers, audioIdx, resumeAt){
  mode = mode || $('mode').value;
  playCtx={url, mode, headers};
  $('go').disabled=true; clearSubs();
  status(mode==='transcode' ? 'transcoding…' : 'probing…');
  try{
    let q='/start?mode='+mode+'&src='+encodeURIComponent(url);
    if(headers) q+='&headers='+encodeURIComponent(JSON.stringify(headers));
    if(audioIdx!=null) q+='&audio='+audioIdx;
    const r=await fetch(q);
    const j=await r.json();
    if(!r.ok) throw new Error(j.error||'failed');
    streamInfo=j;
    const pt = j.mode==='passthrough';
    subBase = `${pt?'⚡ direct-play (no transcode) — ':''}`
          +`video: <code>${j.video} ${j.pix_fmt}</code> → ${j.video_action} &nbsp;|&nbsp; `
          +`audio: <code>${j.audio}</code> → ${j.audio_action}`;
    status(subBase + (j.tracks&&j.tracks.length?' &nbsp;·&nbsp; loading subs…':''));
    if(resumeAt){ const seek=()=>{ try{v.currentTime=resumeAt;}catch(e){}
      v.removeEventListener('loadedmetadata',seek); v.removeEventListener('canplay',seek); };
      v.addEventListener('loadedmetadata',seek); v.addEventListener('canplay',seek); }
    attach(j.playlist, pt);
    loadAudios(j.audios||[]);
    if(j.tracks&&j.tracks.length) loadSubs(j.tracks);
  }catch(e){status('error: <code>'+e.message+'</code>');}
  $('go').disabled=false;
}
function loadAudios(audios){
  const sel=$('audsel');
  if(!audios.length){ sel.innerHTML='<option>—</option>'; sel.disabled=true; $('subbar').style.display=''; return; }
  sel.disabled=false;
  sel.innerHTML=audios.map(a=>
    `<option value="${a.index}"${a.default?' selected':''}>${a.label}${a.lang?' ('+a.lang+')':''}</option>`).join('');
  $('subbar').style.display='';
  sel.onchange=()=>{
    if(!playCtx) return;
    playUrl(playCtx.url, playCtx.mode, playCtx.headers, parseInt(sel.value), v.currentTime);
  };
}
function play(){ const url=$('url').value.trim(); if(url) playUrl(url); }
async function find(){
  const id=$('anilist').value.trim(); if(!id)return;
  status('resolving '+id+' …'); $('list').innerHTML=''; $('find').disabled=true;
  try{
    const r=await fetch('/resolve?anilist='+encodeURIComponent(id));
    const j=await r.json();
    if(!r.ok) throw new Error(j.error||'failed');
    status(`${j.results.length} streams for <code>${j.media_id}</code> — ranked; top plays best. Click any to play.`);
    const ul=$('list'); ul.innerHTML='';
    j.results.forEach((s,i)=>{
      const d=s.decision||{};
      const pure = d.video&&d.video.startsWith('copy') && d.audio&&d.audio.startsWith('copy (');
      const li=document.createElement('li');
      const br=((s.bitrate||0)/1e6).toFixed(1);
      li.innerHTML=`<span class="badge ${pure?'copy':'tx'}">${pure?'COPY':'TRANSCODE'}</span>`
        +`<span class="g">${i+1}. ${s.group||'?'}</span>`
        +`<span class="meta">${s.encode||'?'}/${(s.visualTags||[]).join(',')||'-'}/`
        +`${(s.audioTags||[]).join(',')||'-'} · ${br}Mb · ${s.cached?'cached':'UNCACHED'} · ${s.type}/${s.service}<br>`
        +`<small>video: ${d.video||'?'} | audio: ${d.audio||'?'}</small></span>`;
      li.onclick=()=>playUrl(s.url);
      ul.appendChild(li);
    });
    if(j.results[0]) playUrl(j.results[0].url);
  }catch(e){status('error: <code>'+e.message+'</code>');}
  $('find').disabled=false;
}
$('go').onclick=play;
$('find').onclick=find;
$('url').addEventListener('keydown',e=>{if(e.key==='Enter')play();});
$('anilist').addEventListener('keydown',e=>{if(e.key==='Enter')find();});
</script></body></html>"""
