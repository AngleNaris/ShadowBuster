/* Approximate live audition only. Formal exports never use this processor. */
(function (root) {
  "use strict";
  class DraftDSP {
    constructor(data) {
      this.data = data; this.pos = 0; this.sr = data.sampleRate; this.filters = {};
      this.fast = 0; this.slow = 0; this.low = [0, 0]; this.sideLow = 0;
      this.params = {}; this.target = {}; this.playing = false; this.original = false;
      this.delta = false; this.deltaMix = 0;
      this.originalMix=0;
      this.meterStates=[[[0,0],[0,0]],[[0,0],[0,0]]];this.meterEnergy=0;this.meterCount=0;this.meterBlocks=[];
      this.measuredLufs=data.levels?.mixLufs??-20;this.masterGain=1;this.masterStarted=false;this.limiterGain=1;
      this.setParams({}); this.params = {...this.target};
    }
    setParams(p) {
      const off = new Set(p.bypass || []);
      this.masterEnabled=!off.has('soren');this.masterTarget=this.data.levels?.targets[p.loudness||'normal']??-9.2;
      this.target = {guidance:off.has('lew') ? 0 : Math.max(0,Math.min(1,+(p.guidance ?? 1.5)/2)), sub:off.has('bass') ? 0 : +(p.sub || 0), sat:off.has('bass') ? 0 : +(p.sat || 0),
        punch:off.has('drums') ? 0 : +(p.punch || 0), trans:off.has('drums') ? 0 : +(p.trans || 0),
        vocal:off.has('vocals') ? 0 : +(p.vocal || 0), guitar:off.has('reshape') ? 0 : +(p.guitar || 0),
        space:off.has('reshape') ? 0 : +(p.space || 0), width:off.has('reshape') ? 0 : +(p.space_width || 0),
        denoise:off.has('reshape') ? 0 : +(p.denoise || 0),
        strength:off.has('soren') || p.style_mode !== 'styled' ? 0 : +(p.style_blend ?? .85),
        loud:off.has('soren') ? 0 : ({soft:-3.1,dynamic:-1.94,normal:0,loud:2.5}[p.loudness] || 0)};
      this.eq = off.has('soren') ? 'Neutral' : (p.eq || 'Neutral');
    }
    filter(key, input, channel, kind, hz, db, q=.8) {
      let f = this.filters[key];
      if (!f) f = this.filters[key] = {db:NaN, z:[[0,0],[0,0]]};
      if (f.db !== db) {
        const A = 10**(db/40), w = 2*Math.PI*hz/this.sr, c=Math.cos(w), s=Math.sin(w), alpha=s/(2*q);
        let b,a;
        if (kind==='peak') { b=[1+alpha*A,-2*c,1-alpha*A]; a=[1+alpha/A,-2*c,1-alpha/A]; }
        else {
          const beta=Math.sqrt(2*A)*s;
          if(kind==='low') { b=[A*(A+1-(A-1)*c+beta),2*A*(A-1-(A+1)*c),A*(A+1-(A-1)*c-beta)]; a=[A+1+(A-1)*c+beta,-2*(A-1+(A+1)*c),A+1+(A-1)*c-beta]; }
          else { b=[A*(A+1+(A-1)*c+beta),-2*A*(A-1+(A+1)*c),A*(A+1+(A-1)*c-beta)]; a=[A+1-(A-1)*c+beta,2*(A-1-(A+1)*c),A+1-(A-1)*c-beta]; }
        }
        f.b=b.map(v=>v/a[0]); f.a=a.map(v=>v/a[0]); f.db=db;
      }
      const z=f.z[channel], y=f.b[0]*input+z[0];
      z[0]=f.b[1]*input-f.a[1]*y+z[1]; z[1]=f.b[2]*input-f.a[2]*y;
      return y;
    }
    measure(left,right) {
      const coeff=this.data.levels.weighting;
      for(let c=0;c<2;c++){
        let x=c?right:left;
        for(let k=0;k<2;k++){
          const [b,a]=coeff[k],z=this.meterStates[k][c],y=b[0]*x+z[0];
          z[0]=b[1]*x-a[1]*y+z[1];z[1]=b[2]*x-a[2]*y;x=y;
        }
        this.meterEnergy+=x*x;
      }
      if(++this.meterCount>=this.sr*.1){
        const power=this.meterEnergy/this.meterCount;this.meterCount=0;this.meterEnergy=0;
        this.meterBlocks.push(power);if(this.meterBlocks.length>30)this.meterBlocks.shift();
        const audible=this.meterBlocks.filter(p=>p>1e-7);
        if(audible.length>=4)this.measuredLufs=-.691+10*Math.log10(audible.reduce((a,b)=>a+b,0)/audible.length);
      }
    }
    render(left, right) {
      if (!this.playing) { left.fill(0); right.fill(0); return; }
      // Coefficients interpolate per audio block; scalar controls ramp over 30 ms.
      const blend=1-Math.exp(-left.length/(this.sr*.03));
      for(const k of Object.keys(this.target)) this.params[k]+=(this.target[k]-this.params[k])*blend;
      const p=this.params, src=this.data.buffers, a=1-Math.exp(-2*Math.PI*200/this.sr);
      const af=1-Math.exp(-1/(this.sr*.005)), as=1-Math.exp(-1/(this.sr*.08));
      const aw=1-Math.exp(-2*Math.PI*150/this.sr), gain=this.data.levels?1:this.data.gain*10**(p.loud/20);
      for(let i=0;i<left.length;i++) {
        const n=this.pos*2; let out=[0,0];
        const sample=(name,c)=>{
          const wet=src[name]; if(!wet)return 0;
          if(!this.data.endpoints)return wet[n+c];
          const dry=src[name==='mix'?'original':'dry_'+name];
          return dry ? dry[n+c]*(1-p.guidance)+wet[n+c]*p.guidance : wet[n+c];
        };
        const drum=src.drums, energy=drum ? (sample('drums',0)**2+sample('drums',1)**2)/2 : 0;
        this.fast+=af*(energy-this.fast); this.slow+=as*(energy-this.slow);
        const onset=Math.max(0,Math.min(1,(10*Math.log10((this.fast+1e-12)/(this.slow+1e-12))-3)/9));
        for(let c=0;c<2;c++) {
          let x=sample('mix',c);
          if(src.bass) {
            const dry=sample('bass',c); let wet=this.filter('bass',dry,c,'low',65,p.sub);
            this.low[c]+=a*(wet-this.low[c]);
            wet+=p.sat*(Math.tanh(this.low[c]*8)/8-this.low[c]);
            x+=wet-dry;
          }
          if(drum) x+=this.filter('drums',sample('drums',c),c,'peak',90,p.punch,1.2)*10**(4*p.trans*onset/20)-sample('drums',c);
          if(src.vocals) x+=sample('vocals',c)*(10**(p.vocal/20)-1);
          if(src.guitar) x+=this.filter('guitar',sample('guitar',c),c,'high',2500,4*p.guitar)-sample('guitar',c);
          if(src.other) x+=this.filter('space',sample('other',c),c,'peak',450,-1.5*p.space)-sample('other',c);
          x=this.filter('noise',x,c,'high',10000,-3*p.denoise);
          for(let k=0;k<(this.data.curve || []).length;k++) {
            const [hz,db]=this.data.curve[k]; x=this.filter('ref'+k,x,c,'peak',hz,db*p.strength,1.1);
          }
          const warm=this.eq==='Warm'?1.5:this.eq==='Fusion'?.8:0;
          const air=this.eq==='Bright'?1.5:this.eq==='Fusion'?.8:0;
          x=this.filter('eqlo',x,c,'low',150,warm);
          x=this.filter('eqmid',x,c,'peak',3200,this.eq==='Bright'?1:0,.9);
          out[c]=this.filter('eqhi',x,c,'high',9000,air);
        }
        const mid=(out[0]+out[1])/2, side=(out[0]-out[1])/2;
        this.sideLow+=aw*(side-this.sideLow);
        const wide=side+(side-this.sideLow)*Math.min(.4,p.space*(10**(p.width/20)-1));
        out=[(mid+wide)*gain,(mid-wide)*gain];
        if(this.data.levels){
          this.measure(out[0],out[1]);
          const desired=this.masterEnabled?10**(Math.max(-24,Math.min(24,this.masterTarget-this.measuredLufs))/20):1;
          if(!this.masterStarted){this.masterGain=desired;this.masterStarted=true;}
          this.masterGain+=(desired-this.masterGain)*(1-Math.exp(-1/(this.sr*.5)));
          out=out.map(v=>v*this.masterGain);
          const required=this.masterEnabled?Math.min(1,.944/Math.max(Math.abs(out[0]),Math.abs(out[1]),1e-12)):1;
          this.limiterGain=required<this.limiterGain?required:required+(this.limiterGain-required)*Math.exp(-1/(this.sr*.08));
          out=out.map(v=>v*this.limiterGain);
        }
        // Same-frame, same-gain subtraction; ramp only the monitor mode, not DSP state.
        const deltaTarget=this.delta&&!this.original?1:0;
        this.deltaMix+=Math.max(-1/(this.sr*.02),Math.min(1/(this.sr*.02),deltaTarget-this.deltaMix));
        out[0]-=src.original[n]*this.data.gain*this.deltaMix;
        out[1]-=src.original[n+1]*this.data.gain*this.deltaMix;
        this.originalMix+=Math.max(-1/(this.sr*.02),Math.min(1/(this.sr*.02),(this.original?1:0)-this.originalMix));
        out=out.map((v,c)=>v*(1-this.originalMix)+src.original[n+c]*this.data.gain*this.originalMix);
        // Linked instantaneous safety gain, not the production loudness limiter.
        const trim=Math.min(1,(this.data.levels?.999:.89)/Math.max(Math.abs(out[0]),Math.abs(out[1]),1e-12));
        const edge=this.data.stream ? 1 : Math.min(1,this.pos/128,(this.data.frames-1-this.pos)/128);
        left[i]=out[0]*trim*edge; right[i]=out[1]*trim*edge;
        this.pos=(this.pos+1)%this.data.frames;
      }
    }
  }

  class DraftStream {
    constructor(data, emit) {
      this.data=data; this.emit=emit; this.pos=0; this.playing=false; this.original=false;
      this.delta=false;
      this.generation=0; this.chunks=new Map(); this.requested=new Set();
      this.controls={}; this.loop=null; this.tick=0; this.fade=128;
      this.prefetch();
    }
    setParams(p) { this.controls=p; if(this.dsp)this.dsp.setParams(p); }
    prefetch() {
      const index=Math.floor(this.pos/this.data.chunkFrames);
      for(const k of [...this.chunks.keys()]) if(k<index || k>index+1)this.chunks.delete(k);
      for(const k of [...this.requested]) if(k<index || k>index+1)this.requested.delete(k);
      for(const k of [index,index+1]) {
        if(k*this.data.chunkFrames>=this.data.frames || this.chunks.has(k) || this.requested.has(k))continue;
        this.requested.add(k); this.emit({type:'need',index:k,generation:this.generation});
      }
    }
    chunk(p) { if(p.generation===this.generation && this.requested.has(p.index))this.chunks.set(p.index,p); }
    seek(frame) {
      this.pos=Math.max(0,Math.min(this.data.frames-1,Math.round(frame)));
      // Retain decoded immutable chunks when seeking within the resident window.
      this.generation++; this.requested.clear(); this.dsp=null; this.fade=128;
      this.prefetch(); this.status(!this.chunks.has(Math.floor(this.pos/this.data.chunkFrames)));
    }
    setLoop(range) {
      this.loop=range ? [Math.max(0,Math.round(range[0])),Math.min(this.data.frames,Math.round(range[1]))] : null;
      if(this.loop && this.loop[1]<=this.loop[0])this.loop=null;
      if(this.loop && (this.pos<this.loop[0] || this.pos>=this.loop[1]))this.seek(this.loop[0]);
    }
    status(waiting=false) {
      this.emit({type:'position',frame:this.pos,playing:this.playing,waiting,generation:this.generation});
    }
    render(left,right) {
      left.fill(0); right.fill(0);
      if(!this.playing)return;
      let offset=0, waiting=false;
      while(offset<left.length && this.playing) {
        const end=this.loop ? this.loop[1] : this.data.frames;
        if(this.pos>=end) {
          if(this.loop) { this.seek(this.loop[0]); }
          else { this.playing=false; this.status(); break; }
        }
        this.prefetch();
        const index=Math.floor(this.pos/this.data.chunkFrames), chunk=this.chunks.get(index);
        if(!chunk) { waiting=true; break; }
        const local=this.pos-index*this.data.chunkFrames;
        const data={...this.data,...chunk,stream:true};
        if(!this.dsp) { this.dsp=new DraftDSP(data); this.dsp.setParams(this.controls); this.dsp.deltaMix=this.delta?1:0; this.dsp.originalMix=this.original?1:0; }
        this.dsp.data=data; this.dsp.pos=local; this.dsp.playing=true; this.dsp.original=this.original;
        this.dsp.delta=this.delta;
        const count=Math.min(left.length-offset,chunk.frames-local,end-this.pos);
        this.dsp.render(left.subarray(offset,offset+count),right.subarray(offset,offset+count));
        for(let i=offset;i<offset+count;i++) {
          const gain=Math.min(1,(128-this.fade)/128,(end-(this.pos+i-offset))/128);
          left[i]*=gain; right[i]*=gain; this.fade=Math.max(0,this.fade-1);
        }
        this.pos+=count; offset+=count;
      }
      if(++this.tick%16===0)this.status(waiting);
    }
  }

  class DraftPlayer {
    async load(payload) {
      if(!this.context) this.context=new AudioContext({sampleRate:44100,latencyHint:'interactive'});
      if(!this.context.audioWorklet) throw new Error('此环境不支持草稿实时播放');
      if(!this.loaded) {
        const code=`const DraftDSP=${DraftDSP.toString()};const DraftStream=${DraftStream.toString()};
class Player extends AudioWorkletProcessor {
  constructor(){super();this.port.onmessage=({data:d})=>{
    if(d.type==='load'){this.session=d.value.id;this.playRevision=0;this.dsp=d.value.stream ? new DraftStream(d.value,v=>this.port.postMessage({...v,session:this.session,playRevision:this.playRevision,audioTime:currentTime})) : new DraftDSP(d.value);}
    if(!this.dsp)return;
    if(d.type==='params')this.dsp.setParams(d.value);
    if(d.type==='play'){this.playRevision=d.revision;this.dsp.playing=d.value;this.dsp.status?.();}
    if(d.type==='original')this.dsp.original=d.value;
    if(d.type==='delta')this.dsp.delta=d.value;
    if(d.type==='seek')this.dsp.seek(d.value);
    if(d.type==='loop')this.dsp.setLoop(d.value);
    if(d.type==='chunk' && d.value.session===this.session)this.dsp.chunk(d.value);
  };}
  process(i,o){if(this.dsp)this.dsp.render(o[0][0],o[0][1]);return true;}
} registerProcessor('sb-draft',Player);`;
        const url=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));
        try { await this.context.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
        this.node=new AudioWorkletNode(this.context,'sb-draft',{numberOfInputs:0,numberOfOutputs:1,outputChannelCount:[2]});
        this.node.port.onmessage=({data:p})=>{
          if(p.session!==this.session)return;
          if(p.type==='need' && this.onneed && p.generation>=this.generation) { this.generation=p.generation; this.onneed(p); }
          if(p.type==='position') {
            if(p.generation<this.generation||p.playRevision<this.playRevision)return;
            this.generation=p.generation;
            if(this.displayGeneration!==p.generation){this.displayPosition=p.frame/44100;this.displayGeneration=p.generation;}
            this.position=p.frame/44100; this.playing=p.playing;
            this.waiting=p.waiting;this.positionAt=p.audioTime??this.context.currentTime;
            if(this.onposition)this.onposition(p);
          }
        };
        this.volumeNode=this.context.createGain(); this.node.connect(this.volumeNode); this.volumeNode.connect(this.context.destination); this.loaded=true;
      }
      this.session=payload.id;this.playRevision=0; this.position=0; this.duration=payload.frames/44100;this.generation=0;this.displayPosition=0;this.displayGeneration=0;this.waiting=true;this.positionAt=this.context.currentTime;
      if(payload.stream) this.node.port.postMessage({type:'load',value:payload});
      else this.sendBuffers('load',payload);
      this.playing=false;
    }
    sendBuffers(type,payload) {
      const buffers={};
      for(const [name,b64] of Object.entries(payload.buffers)) {
        let bytes;
        if(Uint8Array.fromBase64) bytes=Uint8Array.fromBase64(b64);
        else {const raw=atob(b64);bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);}
        buffers[name]=new Float32Array(bytes.buffer);
      }
      this.node.port.postMessage({type,value:{...payload,buffers}},Object.values(buffers).map(b=>b.buffer));
    }
    chunk(payload) { if(payload.session===this.session && payload.generation===this.generation)this.sendBuffers('chunk',payload); }
    currentPosition() {
      const predicted=(this.position||0)+(this.playing&&!this.waiting?Math.min(.06,Math.max(0,this.context.currentTime-this.positionAt)):0);
      this.displayPosition=Math.min(this.duration||0,Math.max(this.displayPosition||0,predicted));
      return this.displayPosition;
    }
    seek(seconds) { this.generation++;this.position=seconds;this.displayPosition=seconds;this.displayGeneration=this.generation;this.waiting=true;this.positionAt=this.context.currentTime;this.node.port.postMessage({type:'seek',value:seconds*44100}); }
    loop(range) { this.node.port.postMessage({type:'loop',value:range ? range.map(x=>Math.round(x*44100)) : null}); }
    volume(value) { if(this.volumeNode)this.volumeNode.gain.setTargetAtTime(value,this.context.currentTime,.02); }
    params(p) { if(this.node)this.node.port.postMessage({type:'params',value:p}); }
    play(value) {
      const revision=++this.playRevision;this.playing=value;
      const send=()=>{if(revision===this.playRevision)this.node.port.postMessage({type:'play',value,revision});};
      if(value&&this.context.state!=='running')return this.context.resume().then(send).catch(error=>{if(revision===this.playRevision)this.playing=false;throw error;});
      send();return Promise.resolve();
    }
    original(value) { if(this.node)this.node.port.postMessage({type:'original',value}); }
    delta(value) { if(this.node)this.node.port.postMessage({type:'delta',value}); }
    stop() { if(this.node)this.play(false);else this.playing=false; }
  }
  root.DraftDSP=DraftDSP; root.DraftPlayer=DraftPlayer;
  if(typeof module!=='undefined') module.exports={DraftDSP,DraftStream};
})(typeof window!=='undefined'?window:globalThis);
