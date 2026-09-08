"""Same-input full-track Soren preset matrix, independent vocal estimates, fixed masks."""
import os
import sys
import json
import subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
import pyloudnorm as pyln
from scipy import signal

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'apollo_scripts')]
from experiments.capture_soren_baseline import digest
import vocal_adjust as va

DEST=ROOT/'experiments/results/soren_style_20260906_192550_399848'
PY=ROOT/'packaging/stage/runtime_gpu/env/python.exe'
CORE=ROOT/'packaging/stage/runtime_gpu/Soren_src/core_decrypted.py'
MATRIX=[('Pop',eq,'styled') for eq in ('Neutral','Warm','Bright','Fusion')]+[('Rock','Neutral','styled'),('EDM','Neutral','styled'),('Pop','Neutral','off')]
ENV=dict(os.environ,PYTHONIOENCODING='utf-8',HF_HOME=str(ROOT/'packaging/stage/runtime_gpu/hf_home'),HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')


def run(cmd, log):
    with log.open('w',encoding='utf-8') as f:
        f.write(repr(list(map(str,cmd)))+'\n');f.flush()
        subprocess.run(list(map(str,cmd)),env=ENV,stdout=f,stderr=subprocess.STDOUT,check=True,cwd=ROOT)


def read(p):
    x,sr=sf.read(p,always_2d=True,dtype='float64')
    assert sr==44100 and x.shape[1]==2 and np.isfinite(x).all()
    return x


def metrics(x,v,mask):
    sr=44100
    peak=float(np.max(np.abs(x)));tp=float(np.max(np.abs(signal.resample_poly(x,4,1,axis=0))))
    rms=lambda a:float(np.sqrt(np.mean(a*a)))
    db=lambda a:float(20*np.log10(max(a,1e-12)))
    mid=x.mean(axis=1);side=(x[:,0]-x[:,1])/2
    item={'lufs':float(pyln.Meter(sr).integrated_loudness(x)),'sample_peak_dbfs':db(peak),'true_peak_4x_dbtp':db(tp),'mid_side_db':db(rms(mid)/max(rms(side),1e-12)),'crest_db':db(peak/max(rms(x),1e-12)),'vocal_estimate':{}}
    for name,band in [('wide_mid',None),('1-4k_mid',(1000,4000)),('4-8k_mid',(4000,8000))]:
        vv=v;aa=x-v
        if band:
            sos=signal.butter(4,band,fs=sr,btype='bandpass',output='sos')
            vv=signal.sosfilt(sos,vv,axis=0);aa=signal.sosfilt(sos,aa,axis=0)
        vr=va.mid_rms_windows(vv,sr)[1];ar=va.mid_rms_windows(aa,sr)[1]
        assert len(vr)==len(mask)
        item['vocal_estimate'][name]={'level_db':float(np.median(20*np.log10(np.maximum(vr[mask],1e-12)))),'vocal_accompaniment_db':float(np.median(20*np.log10(np.maximum(vr[mask],1e-12)/np.maximum(ar[mask],1e-12))))}
    return item


def main():
    manifest=json.loads((DEST/'baseline.json').read_text(encoding='utf-8'))
    report={'method':'Independent HTDemucs estimates with fixed historical original-vocal activity mask; estimates are not ground truth. Full track, 100ms/50ms medians. No listening claim.', 'core_sha256':digest(CORE),'runtime':str(PY),'matrix':MATRIX,'tracks':[]}
    for source,h in manifest['inputs'].items():
        source=Path(source);assert digest(source)==h
        name=source.parent.name.removeprefix('.work_')
        dest=DEST/name;dest.mkdir(exist_ok=True)
        baseline_stem=dest/'stems/htdemucs/reference_vocal/vocals.wav'
        if not baseline_stem.exists():
            run([PY,'-m','demucs','--float32','--clip-mode=none','--shifts','0','-n','htdemucs','-d','cuda','-o',dest/'stems',source],dest/'premaster.demucs.log')
        x=read(source);v=read(baseline_stem)
        mask=np.load(source.parent/'comparison_mask.npz')['valid']
        item={'track':name,'input':str(source),'input_sha256':h,'baseline':metrics(x,v,mask),'versions':[]}
        report['tracks'].append(item)
        for genre,eq,mode in MATRIX:
            label=f'{genre}_{eq}_{mode}_loud';out=dest/(label+'.wav')
            if not out.exists():
                run([PY,CORE,source,out,'--genre',genre,'--eq-profile',eq,'--loudness','loud','--style-mode',mode],dest/(label+'.log'))
            stem=dest/'stems/htdemucs'/label/'vocals.wav'
            if not stem.exists():
                run([PY,'-m','demucs','--float32','--clip-mode=none','--shifts','0','-n','htdemucs','-d','cuda','-o',dest/'stems',out],dest/(label+'.demucs.log'))
            y=read(out);assert y.shape==x.shape
            measured=metrics(y,read(stem),mask)
            measured.update(label=label,genre=genre,eq=eq,style_mode=mode,output=str(out),output_sha256=digest(out),limiter=json.loads(Path(str(out)+'.mastering.json').read_text()))
            item['versions'].append(measured)
            assert digest(source)==h
            (DEST/'measurements.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            print(name,label,measured['lufs'],flush=True)
    print('COMPLETE',flush=True)

if __name__=='__main__':main()
