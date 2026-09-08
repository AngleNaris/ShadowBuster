"""Non-destructive 30-second pipeline timing on the local GPU runtime."""
import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1]
os.environ['SB_ASSETS']=str(ROOT/'packaging/stage/runtime_gpu')
sys.path.insert(0,str(ROOT))
import studio_backend as b
b.DSP_DIR=ROOT/'apollo_scripts'
import soundfile as sf
import torch

out=ROOT/'experiments/results'/('performance_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
out.mkdir()
source=Path(r'D:\_4.Projects\_MY\中二病晚期患者\5.愚蠢人类观察家.wav')
x,sr=sf.read(source,frames=sf.info(source).samplerate*30,always_2d=True)
sf.write(out/'input.wav',x,sr,subtype='FLOAT')
report={'gpu':torch.cuda.get_device_name(),'vram_bytes':torch.cuda.get_device_properties(0).total_memory,'torch':torch.__version__,'cuda':torch.version.cuda,'arch_list':torch.cuda.get_arch_list(),'python':str(b.PYTHON),'seconds':len(x)/sr,'calls':[]}
original=b._run_stream

def timed(cmd,cwd,**kwargs):
    start=time.perf_counter()
    result=original(cmd,cwd,**kwargs)
    report['calls'].append({'cmd':list(map(str,cmd)),'seconds':time.perf_counter()-start})
    (out/'timing.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return result
b._run_stream=timed
start=time.perf_counter()
b.run_pipeline(out/'input.wav',out/'output',style_mode='off',device='cuda',quality=1,loudness='normal',space_wet=.6,space_denoise=.2)
report['total_seconds']=time.perf_counter()-start
(out/'timing.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(out,flush=True)
print(json.dumps(report,indent=2),flush=True)
