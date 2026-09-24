const draft = {id:0,busy:false,ready:false,key:null,player:new DraftPlayer()};
const transport = {file:null,duration:0,source:'original',versions:[],loop:false,range:[0,0],dragging:false,token:0};
const nativeAudio = $('player-audio');
function draftTime(t) { t=Math.max(0,Math.floor(t||0));return `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`; }
function playerPosition() { return transport.source==='draft' ? draft.player.position||0 : nativeAudio.currentTime||0; }
function playerPlaying() { return transport.source==='draft' ? draft.player.playing : !nativeAudio.paused; }
function playerError(message) { $('draft-status').textContent=message; $('draft-status').hidden=!message; }
function cacheState(status, text, progress=0) {
  $('player').dataset.cache=status;
  $('player-track').style.setProperty('--cache-progress',`${Math.max(0,Math.min(1,progress))*100}%`);
  $('player-track').title=text; $('player-cache-description').textContent=text;
  $('draft-prepare').textContent=status==='preparing'?'取消准备':draft.ready?'更新缓存':'准备试听';
}
function paintTransport() {
  const t=playerPosition();
  $('draft-play').dataset.playing=String(playerPlaying());
  $('draft-play').setAttribute('aria-label',playerPlaying()?'暂停':'播放');
  $('draft-play').disabled=!transport.file || (transport.source==='draft'&&!draft.ready);
  $('draft-seek').disabled=!transport.duration || (transport.source==='draft'&&!draft.ready);
  $('draft-seek').max=transport.duration||1;
  if(!transport.dragging) {
    $('draft-seek').value=t;
    $('draft-time').textContent=`${draftTime(t)} / ${draftTime(transport.duration)}`;
  }
  $('draft-seek').setAttribute('aria-valuetext',`${draftTime(t)}，总长 ${draftTime(transport.duration)}`);
}
function stopTransport() { nativeAudio.pause();draft.player.stop();paintTransport(); }
function setPlayerLoop() {
  const duration=transport.duration;
  let [a,b]=transport.range;
  a=Math.max(0,Math.min(a,duration));b=Math.max(a,Math.min(b,duration));
  if(b<=a)transport.loop=false;
  transport.range=[a,b];
  $('draft-loop').setAttribute('aria-pressed',String(transport.loop));
  $('player-a').value=draftTime(a);$('player-b').value=draftTime(b);
  const band=$('player-loop-region');band.hidden=!transport.loop;
  band.style.left=`${duration?a/duration*100:0}%`;band.style.width=`${duration?(b-a)/duration*100:0}%`;
  if(draft.ready)draft.player.loop(transport.loop?[a,b]:null);
  if(transport.loop && transport.source!=='draft' && (playerPosition()<a || playerPosition()>=b))nativeAudio.currentTime=a;
}
function seekPlayer(t) {
  t=Math.max(0,Math.min(transport.duration,t));
  if(transport.loop && (t<transport.range[0] || t>=transport.range[1])) {transport.loop=false;setPlayerLoop();}
  if(transport.source==='draft' && draft.ready)draft.player.seek(t);
  else if(nativeAudio.readyState)nativeAudio.currentTime=t;
  paintTransport();
}
async function chooseSource(source,keepPlaying=true) {
  if(source==='draft'&&!draft.ready)return;
  const time=playerPosition(), playing=keepPlaying&&playerPlaying(), token=++transport.token;
  stopTransport();transport.source=source;renderPlayerSources();playerError('');
  if(source==='draft') {
    draft.player.seek(time);draft.player.params(collectParams());draft.player.volume(+$('player-volume').value);
    setPlayerLoop();if(playing)await draft.player.play(true);
  } else {
    const path=source==='original'?transport.file:source;
    if(!path)return;
    nativeAudio.src=fileUrl(path);nativeAudio.volume=+$('player-volume').value;
    try {
      await new Promise((resolve,reject)=>{
        const done=()=>{nativeAudio.removeEventListener('loadedmetadata',ok);nativeAudio.removeEventListener('error',fail);};
        const ok=()=>{done();resolve();};const fail=()=>{done();reject(new Error('音频无法播放，请检查文件'));};
        nativeAudio.addEventListener('loadedmetadata',ok);nativeAudio.addEventListener('error',fail);
      });
      if(token!==transport.token)return;
      nativeAudio.currentTime=Math.min(time,Math.max(0,nativeAudio.duration-.001));
      if(playing)await nativeAudio.play();
    } catch(e) {if(token===transport.token)playerError(String(e));}
  }
  paintTransport();
}
function renderPlayerSources() {
  const wrap=$('player-sources');wrap.innerHTML='';
  const items=[['original','原声'],['draft','草稿'],...transport.versions.map(v=>[v.path,`V${v.version}`])];
  for(const [key,label] of items) {
    const b=document.createElement('button');b.type='button';b.className='player-source';b.textContent=label;
    b.setAttribute('aria-pressed',String(transport.source===key));b.disabled=key==='draft'&&!draft.ready;
    b.addEventListener('click',()=>chooseSource(key));wrap.appendChild(b);
  }
  $('player-mode').textContent=items.find(x=>x[0]===transport.source)?.[1]||'成品';
}
function refreshPreviewFiles() {
  const file=state.selectedFile;if(file===transport.file)return;
  stopTransport();transport.token++;transport.file=file;transport.source='original';transport.duration=0;
  transport.range=[0,0];transport.loop=false;transport.versions=[];
  draft.ready=false;nativeAudio.removeAttribute('src');nativeAudio.load();
  setPlayerLoop();cacheState('empty','缓存尚未准备');playerError('');paintTransport();renderPlayerSources();
  if(file){api.audioInfo(file);chooseSource('original',false);refreshPreviewOutputs();}
}
function onAudioInfo(raw) {
  const p=JSON.parse(raw);if(normPath(p.path)!==normPath(transport.file))return;
  if(p.error){playerError('无法读取歌曲：'+p.error);return;}
  transport.duration=p.duration;transport.range=[0,p.duration];setPlayerLoop();paintTransport();
}
async function refreshPreviewOutputs() {
  const file=state.selectedFile;let outs=[];
  try {if(file&&state.output)outs=JSON.parse(await api.listOutputs(state.output,stemOf(file))).outputs||[];}catch(e){}
  if(file!==transport.file)return;
  transport.versions=outs;
  if(!['original','draft'].includes(transport.source)&&!outs.some(x=>x.path===transport.source))chooseSource('original',false);
  renderPlayerSources();
}
function draftKey() {
  const p=collectParams();return JSON.stringify([state.selectedFile,p.quality,p.reference,p.style_mode==='styled'&&!p.reference]);
}
async function startDraft() {
  if(draft.busy){api.cancel();cacheState('preparing','正在取消准备');return;}
  if(state.processing||!transport.file){playerError('请先选择歌曲，并等待当前处理完成');return;}
  if(!transport.duration){playerError('正在读取歌曲，请稍后重试');return;}
  stopTransport();draft.ready=false;draft.busy=true;draft.key=draftKey();draft.id++;
  cacheState('preparing','正在准备试听缓存');playerError('');renderPlayerSources();paintTransport();
  api.draftPrepare(state.selectedFile,0,transport.duration,collectParams(),draft.id);
}
async function onDraftReady(raw) {
  const p=JSON.parse(raw);if(p.id!==draft.id)return;
  if(draft.key!==draftKey()){draft.busy=false;cacheState('stale','输入或精度已变化，请准备缓存');return;}
  try {
    await draft.player.load(p);
    if(p.id!==draft.id||draft.key!==draftKey()){draft.player.stop();draft.busy=false;cacheState('stale','输入或精度已变化，请准备缓存');return;}
    draft.busy=false;draft.ready=true;draft.player.params(collectParams());
    transport.duration=draft.player.duration;
    cacheState('ready','试听缓存就绪',1);playerError('');
    await chooseSource('draft',false);setPlayerLoop();paintTransport();
  }catch(e){onDraftFailed(JSON.stringify({id:p.id,error:String(e)}));}
}
function onDraftFailed(raw) {
  const p=JSON.parse(raw);if(p.id!==draft.id)return;
  draft.busy=false;draft.ready=false;cacheState('error','缓存准备失败');playerError(p.error);renderPlayerSources();paintTransport();
}
function onDraftProgress(raw) {const p=JSON.parse(raw);if(p.id===draft.id)cacheState('preparing',`${p.label} · ${Math.round(p.fraction*100)}%`,p.fraction);}
async function togglePlayer() {
  if(!transport.file)return;
  playerError('');
  try {
    if(transport.source==='draft') {
      if(!draft.ready)return;
      if(draft.player.position>=draft.player.duration)draft.player.seek(transport.loop?transport.range[0]:0);
      await draft.player.play(!draft.player.playing);
    }else if(nativeAudio.paused)await nativeAudio.play();else nativeAudio.pause();
  }catch(e){playerError('播放失败：'+String(e));}
  paintTransport();
}
draft.player.onneed=p=>api.draftReadChunk(draft.id,p.index,p.generation);
draft.player.onposition=p=>{if(transport.source==='draft'&&draft.ready){paintTransport();$('player-track').classList.toggle('buffering',p.waiting);}};
$('draft-prepare').addEventListener('click',startDraft);
$('draft-play').addEventListener('click',togglePlayer);
$('player-start').addEventListener('click',()=>seekPlayer(0));
$('draft-seek').addEventListener('input',()=>{transport.dragging=true;$('draft-time').textContent=`${draftTime(+$('draft-seek').value)} / ${draftTime(transport.duration)}`;});
$('draft-seek').addEventListener('change',()=>{transport.dragging=false;seekPlayer(+$('draft-seek').value);});
$('draft-loop').addEventListener('click',()=>{transport.loop=!transport.loop;setPlayerLoop();});
for(const [id,index] of [['player-mark-a',0],['player-mark-b',1]])$(id).addEventListener('click',()=>{transport.range[index]=playerPosition();setPlayerLoop();});
for(const [id,index] of [['player-a',0],['player-b',1]])$(id).addEventListener('change',()=>{
  const parts=$(id).value.split(':').map(Number),t=parts.length===2?parts[0]*60+parts[1]:parts[0];
  if(Number.isFinite(t))transport.range[index]=t;setPlayerLoop();
});
$('player-volume').addEventListener('input',()=>{const volume=+$('player-volume').value;nativeAudio.volume=volume;draft.player.volume(volume);});
for(const event of ['play','pause','ended','loadedmetadata'])nativeAudio.addEventListener(event,paintTransport);
function nativeTick(){
  if(transport.source!=='draft'&&!nativeAudio.paused){
    if(transport.loop&&nativeAudio.currentTime>=transport.range[1])nativeAudio.currentTime=transport.range[0];
    paintTransport();
  }
  requestAnimationFrame(nativeTick);
}
requestAnimationFrame(nativeTick);
document.addEventListener('keydown',e=>{
  if(e.code!=='Space'||['INPUT','SELECT','TEXTAREA','BUTTON'].includes(e.target.tagName)||helpOpen||e.target.isContentEditable)return;
  e.preventDefault();togglePlayer();
});
let lastDraftParams='';
setInterval(()=>{
  if(!draft.ready)return;
  if(draft.key!==draftKey()){
    draft.player.stop();draft.ready=false;cacheState('stale','输入、精度或参考已变化，请重新准备');
    playerError('输入、精度或参考已变化，请重新准备。');
    if(transport.source==='draft')chooseSource('original',false);
    renderPlayerSources();paintTransport();return;
  }
  const p=collectParams(),key=JSON.stringify(p);if(key!==lastDraftParams){lastDraftParams=key;draft.player.params(p);}
},50);
renderPlayerSources();
