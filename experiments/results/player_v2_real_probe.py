import os,sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import numpy as np,soundfile as sf
import studio_backend as b,draft_preview as d,prepared_audio as a
root=Path('experiments/results/player_v2_real_20260920').resolve();root.mkdir(parents=True,exist_ok=True)
os.environ['SB_PROCESSING_CACHE_DIR']=str(root/'cache')
src=Path('experiments/results/wanxiang_mastering_20260919/01_clip/input.wav').resolve()
calls=[];old=b._run_stream
def run(cmd,*args,**kw):
 calls.append([str(x) for x in cmd]);return old(cmd,*args,**kw)
b._run_stream=run
opts=dict(device='cuda',quality=0,guidance=1.5,demucs_model='htdemucs_6s',style_mode='off')
s=d.Session();start=time.perf_counter();meta=d.prepare(b,src,0,12,opts,session=s)
report={'prepare_seconds':time.perf_counter()-start,'prepare_processes':calls.copy(),'variants':[]}
for guide in (0,.8,2):
 before=len(calls)
 out=b.run_pipeline(src,root/str(guide),quality=0,guidance=guide,device='cuda',demucs_model='htdemucs_6s',style_mode='off',guitar_presence_db=guide,bypass=['soren'],balance_mode=None)
 new=calls[before:];ai=[c for c in new if 'demucs' in c or any('lew_upscale.py' in str(x) for x in c)]
 assert not ai,ai
 y,rate=sf.read(out);report['variants'].append({'guidance':guide,'output':str(out),'ai_calls':len(ai),'peak':float(np.max(np.abs(y))),'frames':len(y),'finite':bool(np.isfinite(y).all())})
 assert np.isfinite(y).all()
# Exact expected residual under shared interpolation/peak scaling.
bundle=a.endpoints(b,src,root/'check',quality=0,device='cuda')
a.mix_endpoints(*bundle,root/'mix.wav',root/'mixed_stems',1.5)
dry,r=sf.read(bundle[0]);wet,r=sf.read(bundle[1]);mix,r=sf.read(root/'mix.wav')
expected=dry*.25+wet*.75;scale=min(1,.999/max(float(np.max(np.abs(expected))),1e-12))
assert np.max(np.abs(mix-expected*scale))<1e-6
stems=['bass','drums','vocals','other','guitar','piano']
res=mix-sum(sf.read(root/'mixed_stems'/(n+'.wav'))[0] for n in stems)
expected_res=(dry-sum(sf.read(bundle[2]/(n+'.wav'))[0] for n in stems))*.25+(wet-sum(sf.read(bundle[3]/(n+'.wav'))[0] for n in stems))*.75
report['residual_error']=float(np.max(np.abs(res-expected_res*scale)));assert report['residual_error']<1e-6
(root/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
