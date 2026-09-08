"""Measure original and memory-bounded fast presets in separate processes."""
import sys,time,json,subprocess,os,runpy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PY=ROOT/'packaging/stage/runtime_gpu/env/python.exe'
LEW=ROOT/'packaging/stage/runtime_gpu/Apollo/lew_upscale.py'
if len(sys.argv)>1:
    import torch
    import numpy as np
    import soundfile as sf
    chunk,overlap,dest=sys.argv[1:];dest=Path(dest)
    sys.path.insert(0,str(LEW.parent))
    namespace=runpy.run_path(str(LEW))
    model=namespace['look2hear'].models.BaseModel.from_pretrain(str(namespace['LEW_CKPT']),sr=44100,win=20,feature_dim=384,layer=6).cuda().eval()
    x,sr=namespace['load_audio'](str(dest/'input.wav'))
    torch.cuda.reset_peak_memory_stats();torch.cuda.synchronize();start=time.perf_counter()
    with torch.inference_mode():
        y=namespace['run_model'](model,x,torch.device('cuda'),int(float(chunk)*44100),int(float(overlap)*44100))
    torch.cuda.synchronize()
    result={'chunk':float(chunk),'overlap':float(overlap),'inference_seconds':time.perf_counter()-start,'peak_allocated_mib':torch.cuda.max_memory_allocated()/2**20,'peak_reserved_mib':torch.cuda.max_memory_reserved()/2**20,'finite':bool(torch.isfinite(y).all()),'shape':list(y.shape),'gpu':torch.cuda.get_device_name()}
    namespace['save_audio'](str(dest/('chunk_'+chunk+'.wav')),y,sr)
    (dest/('chunk_'+chunk+'.json')).write_text(json.dumps(result,indent=2))
else:
    from datetime import datetime
    import soundfile as sf
    from scipy import signal
    from math import gcd
    dest=ROOT/'experiments/results'/('lew_fast_'+datetime.now().strftime('%Y%m%d_%H%M%S'));dest.mkdir()
    src=Path(r'D:\_4.Projects\_MY\中二病晚期患者\5.愚蠢人类观察家.wav');sr=sf.info(src).samplerate
    x,_=sf.read(src,frames=sr*30,always_2d=True)
    divisor=gcd(sr,44100);x=signal.resample_poly(x,44100//divisor,sr//divisor,axis=0)
    sf.write(dest/'input.wav',x,44100,subtype='FLOAT')
    for chunk,overlap in [('20','1'),('6','.5')]:
        with (dest/('chunk_'+chunk+'.log')).open('w',encoding='utf-8') as f:
            subprocess.run([str(PY),__file__,chunk,overlap,str(dest)],stdout=f,stderr=subprocess.STDOUT,check=True,env=dict(os.environ,PYTHONIOENCODING='utf-8'))
    print(dest)
    for p in dest.glob('*.json'):print(p.read_text())
