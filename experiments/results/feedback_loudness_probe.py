import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import numpy as np
import soundfile as sf
from mastering.draft_levels import measure,coefficients
from mastering import LOUDNESS_TARGETS
from mastering.finalizer import finalize
from audio_metrics import integrated_lufs

root=Path('experiments/results/feedback_loudness_20260920').resolve();root.mkdir(exist_ok=True)
source=Path('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav')
x,sr=sf.read(source,always_2d=True)
assert sr==44100
x=np.tile(x,(3,1))
sf.write(root/'input.wav',x,sr,subtype='FLOAT')
x.astype('<f4').tofile(root/'input.f32')
levels={'mixLufs':measure(root/'input.wav'),'originalLufs':measure(root/'input.wav'),'targets':LOUDNESS_TARGETS,'weighting':coefficients(sr)}
(root/'levels.json').write_text(json.dumps(levels),encoding='utf-8')
script=r"""
const fs=require('fs'),{DraftDSP}=require(process.cwd()+'/ui/draft_audio.js');
const dir=process.argv[1],raw=fs.readFileSync(dir+'/input.f32'),buf=new Float32Array(raw.buffer,raw.byteOffset,raw.length/4),levels=JSON.parse(fs.readFileSync(dir+'/levels.json'));
for(const mode of ['soft','dynamic','normal','loud']){
 const dsp=new DraftDSP({buffers:{mix:buf,original:buf},frames:buf.length/2,sampleRate:44100,gain:1,stream:true,levels});
 dsp.setParams({loudness:mode});dsp.params={...dsp.target};dsp.playing=true;
 const out=new Float32Array(buf.length);
 for(let i=0;i<buf.length/2;i+=128){const n=Math.min(128,buf.length/2-i),l=new Float32Array(n),r=new Float32Array(n);dsp.render(l,r);for(let j=0;j<n;j++){out[2*(i+j)]=l[j];out[2*(i+j)+1]=r[j];}}
 fs.writeFileSync(dir+'/'+mode+'.f32',Buffer.from(out.buffer));
}
"""
subprocess.run(['node','-e',script,str(root)],check=True)
results=[]
for mode,target in LOUDNESS_TARGETS.items():
    preview=np.fromfile(root/(mode+'.f32'),dtype='<f4').reshape(-1,2)
    formal,stats=finalize(x,sr,mode)
    live_lufs=integrated_lufs(preview,sr)[0];formal_lufs=integrated_lufs(formal,sr)[0]
    results.append(dict(mode=mode,target=target,preview_lufs=live_lufs,formal_lufs=formal_lufs,difference=live_lufs-formal_lufs,peak=float(abs(preview).max())))
(root/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results),flush=True)
