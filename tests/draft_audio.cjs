const assert=require('node:assert/strict');
const {DraftDSP}=require('../ui/draft_audio.js');
const sr=44100,n=sr, buffers={};
for(const k of ['mix','original','bass','drums','vocals','other','guitar']) buffers[k]=new Float32Array(n*2);
for(let i=0;i<n;i++)for(let c=0;c<2;c++){
  const t=i/sr;
  buffers.bass[2*i+c]=.07*Math.sin(2*Math.PI*55*t);
  buffers.drums[2*i+c]=.05*Math.sin(2*Math.PI*90*t)*Math.exp(-((t%.2)*35));
  buffers.vocals[2*i+c]=.03*Math.sin(2*Math.PI*900*t);
  buffers.other[2*i+c]=.015*Math.sin(2*Math.PI*(c?8100:6800)*t);
  buffers.guitar[2*i+c]=.015*Math.sin(2*Math.PI*2600*t);
  buffers.mix[2*i+c]=['bass','drums','vocals','other','guitar'].reduce((s,k)=>s+buffers[k][2*i+c],0);
  buffers.original[2*i+c]=buffers.mix[2*i+c];
}
const data={buffers,frames:n,sampleRate:sr,gain:1,curve:[[630,2],[4000,-2]]};
function render(params,original=false){
  const dsp=new DraftDSP(data);dsp.setParams(params);dsp.params={...dsp.target};dsp.playing=true;dsp.original=original;
  let out=[];for(let i=0;i<Math.ceil(n/128);i++){let l=new Float32Array(128),r=new Float32Array(128);dsp.render(l,r);out.push(...l);}
  assert(out.every(Number.isFinite));assert(Math.max(...out.map(Math.abs))<=.89001);return out.slice(4096,n-1024);
}
const base=render({style_mode:'off'});
function delta(a,b){return Math.sqrt(a.reduce((s,v,i)=>s+(v-b[i])**2,0)/a.length);}
for(const p of [{sub:6},{sat:1},{punch:6},{trans:1},{vocal:6},{guitar:1},{space:1},{denoise:1},{space:.6,space_width:.5},{eq:'Bright'},{loudness:'loud'}]){
  assert(delta(base,render({style_mode:'off',...p}))>1e-5,JSON.stringify(p));
}
// 宽度新语义（2026-09-28）：0-1 授权比例，默认以上必须仍有响应（旧 0.4
// 硬盖在默认档就饱和，向上调整零变化）；满档响应 ≈ 3× 半档（线性请求）。
const halfWidth=render({style_mode:'off',space:.6,space_width:.5});
const fullWidth=render({style_mode:'off',space:.6,space_width:1});
assert(delta(base,fullWidth)>delta(base,halfWidth)*1.5,'width responds above default');
// 饱和度与正式链路 soft_clip(drive=1.6) 同形：小信号增益 1.6/tanh(1.6)>1。
assert(delta(base,render({style_mode:'off',sat:.2}))>1e-5,'default saturation audible');
const ref0=render({style_mode:'styled',style_blend:0});
const ref5=render({style_mode:'styled',style_blend:.5});
const ref1=render({style_mode:'styled',style_blend:1});
assert(delta(base,ref0)<1e-7);assert(delta(base,ref1)>delta(base,ref5));
assert(delta(render({eq:'Bright',style_mode:'off'}),render({eq:'Bright',style_mode:'styled',style_blend:0}))<1e-7);
assert(delta(base,render({sub:12,vocal:12,eq:'Bright',bypass:['bass','vocals','soren']}))<1e-7);
assert(delta(base,render({sub:12,vocal:12},true))<1e-7);
const dsp=new DraftDSP(data);let l=new Float32Array(128),r=new Float32Array(128);dsp.render(l,r);assert(l.every(v=>v===0));
console.log('Draft DSP: parameter effects, reference strength, bypass, original A/B, finite output and safety passed');
const {DraftStream}=require('../ui/draft_audio.js');
const messages=[];
const stream=new DraftStream({...data,stream:true,chunkFrames:4096},m=>messages.push(m));
function supply(){
  for(const m of messages.splice(0))if(m.type==='need'){
    const a=m.index*4096,b=Math.min(n,a+4096),buf={};
    for(const [k,v]of Object.entries(buffers))buf[k]=v.slice(a*2,b*2);
    stream.chunk({index:m.index,generation:m.generation,frames:b-a,buffers:buf});
  }
}
stream.setParams({sub:4,eq:'Bright'});stream.playing=true;
const full=new DraftDSP({...data,stream:true});full.setParams({sub:4,eq:'Bright'});full.playing=true;
let played=0;
while(played<n){
  supply();const size=Math.min(128,n-played),a=new Float32Array(size),b=new Float32Array(size),x=new Float32Array(size),y=new Float32Array(size);
  stream.render(a,b);full.render(x,y);
  for(let i=0;i<size;i++)if(played+i>128 && played+i<n-128)assert(Math.abs(a[i]-x[i])<2e-5,'chunk continuity');
  played+=size;assert(stream.chunks.size<=2);
}
supply();stream.render(new Float32Array(128),new Float32Array(128));assert(!stream.playing);assert.equal(stream.pos,n);
stream.seek(n/2);const generation=stream.generation;
stream.chunk({index:0,generation:generation-1,frames:10,buffers:{}});assert.equal(stream.chunks.size,0);
supply();stream.playing=true;stream.render(new Float32Array(128),new Float32Array(128));assert.equal(stream.pos,n/2+128);
stream.setLoop([1000,1500]);supply();
for(let i=0;i<10;i++){supply();stream.render(new Float32Array(128),new Float32Array(128));assert(stream.pos>=1000 && stream.pos<=1500);}
console.log('Full-song stream: bounded chunks, continuous DSP, seek, stale chunk isolation, ending and loop passed');
// Endpoint interpolation is audible without rebuilding or reloading buffers.
const endpointBuffers={...buffers,original:Float32Array.from(buffers.mix,x=>x*.5)};
for(const name of ['bass','drums','vocals','other','guitar'])endpointBuffers['dry_'+name]=Float32Array.from(buffers[name],x=>x*.5);
function endpointRender(guidance,bypass=[]){
 const d=new DraftDSP({...data,endpoints:true,buffers:endpointBuffers,stream:true});
 d.setParams({guidance,bypass});d.params={...d.target};d.playing=true;
 const l=new Float32Array(4096),r=new Float32Array(4096);d.render(l,r);return l;
}
const dryEndpoint=endpointRender(0),wetEndpoint=endpointRender(2),halfEndpoint=endpointRender(1);
for(let i=0;i<4096;i++)assert(Math.abs(halfEndpoint[i]-(dryEndpoint[i]+wetEndpoint[i])/2)<1e-6);
assert(delta([...dryEndpoint],[...wetEndpoint])>1e-3);
assert(delta([...dryEndpoint],[...endpointRender(2,['lew'])])<1e-7);
console.log('Dry/wet endpoints: guidance interpolation and reconstruction bypass passed');

// Difference monitoring uses synchronized samples with a common gain, before safety trim.
function differenceRender(params, enabled, input=data) {
 const d=new DraftDSP({...input,stream:true});d.setParams(params);d.params={...d.target};d.playing=true;d.delta=enabled;
 const l=new Float32Array(4096),r=new Float32Array(4096);d.render(l,r);return {d,l,r};
}
const nullDelta=differenceRender({},true);
assert(nullDelta.l.slice(1024).every(v=>Math.abs(v)<1e-7),'unchanged signal cancels');
const wet=differenceRender({vocal:6},false),diff=differenceRender({vocal:6},true);
for(let i=1024;i<4096;i++)for(const [channel,key] of [[0,'l'],[1,'r']])
 assert(Math.abs(diff[key][i]-(wet[key][i]-buffers.original[i*2+channel]))<1e-7,'sample-aligned delta');
assert(diff.l.slice(1024).some(v=>Math.abs(v)>1e-3),'adjustment remains audible');
const before=diff.d.pos;diff.d.delta=false;
diff.d.render(new Float32Array(1024),new Float32Array(1024));
assert.equal(diff.d.pos,before+1024);assert.equal(diff.d.deltaMix,0);
const ramp=differenceRender({},true,{...data,buffers:{...buffers,mix:new Float32Array(n*2).fill(.1),original:new Float32Array(n*2).fill(.1)}});
assert(Math.abs(ramp.l[0]-.1)<.001);assert(Math.abs(ramp.l[1000])<1e-7);
const loudDiff=differenceRender({},true,{...data,gain:20,buffers:{...buffers,original:Float32Array.from(buffers.original,v=>-v)}});
assert(loudDiff.l.every(v=>Number.isFinite(v)&&Math.abs(v)<=.89001));
stream.setLoop(null);stream.delta=true;stream.seek(4090);supply();stream.playing=true;
stream.render(new Float32Array(128),new Float32Array(128));
assert.equal(stream.pos,4218);assert.equal(stream.dsp.delta,true);assert.equal(stream.dsp.deltaMix,1);
console.log('Delta: cancellation, stereo subtraction, transition, peak safety and stream seek passed');

// Switching monitor sources must not invalidate queued audio or advance/rewind the clock.
const clockBefore=stream.pos,generationBefore=stream.generation,queuedBefore=stream.chunks.size;
stream.original=true;stream.delta=false;
assert.equal(stream.pos,clockBefore);assert.equal(stream.generation,generationBefore);assert.equal(stream.chunks.size,queuedBefore);
for(let i=0;i<10;i++){supply();stream.render(new Float32Array(128),new Float32Array(128));}
assert.equal(stream.pos,clockBefore+1280);assert.equal(stream.dsp.originalMix,1);
stream.original=false;
for(let i=0;i<10;i++){supply();stream.render(new Float32Array(128),new Float32Array(128));}
assert.equal(stream.pos,clockBefore+2560);assert.equal(stream.generation,generationBefore);assert.equal(stream.dsp.originalMix,0);
console.log('Shared original/draft clock: crossfade without seeks, invalidation or silent reload passed');

// A resident seek must play immediately without asking Python to reload that block.
stream.setLoop(null);supply();
const residentIndex=Math.floor(stream.pos/4096),resident=stream.chunks.get(residentIndex);
assert(resident);messages.length=0;
stream.seek(residentIndex*4096+256);
assert.equal(stream.chunks.get(residentIndex),resident);
assert(!messages.some(m=>m.type==='need'&&m.index===residentIndex));
assert.equal(messages.find(m=>m.type==='position').waiting,false);
const residentStart=stream.pos;
stream.render(new Float32Array(128),new Float32Array(128));
assert.equal(stream.pos,residentStart+128,'resident seek renders on the next audio block');
console.log('Resident seek: retained PCM, no reload, no buffering and immediate render passed');
