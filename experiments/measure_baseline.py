"""Offline baseline measurements for WAV pairs and isolated experiment outputs."""
from __future__ import annotations
import argparse, json, math
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal

try:
    from experiments.objective_compare import measure_audio
except ModuleNotFoundError:
    from objective_compare import measure_audio

def inspect(path: Path):
    x, sr = sf.read(path, dtype='float64', always_2d=True)
    m = measure_audio(x, sr)
    # compact spectral centroid and band fractions are already in measure_audio
    spec = np.fft.rfft(x, axis=0); freqs=np.fft.rfftfreq(len(x),1/sr)
    p=np.mean(np.abs(spec)**2,axis=1); total=float(p.sum())
    m['spectral_centroid_hz']=float((freqs*p).sum()/total) if total else None
    m['sample_rate']=int(sr); m['channels']=int(x.shape[1]); m['frames']=int(x.shape[0]); m['duration_s']=len(x)/sr
    m['path']=str(path); return m

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',type=Path,required=True); ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args(); rows=[]
    for p in sorted(a.source.glob('*.wav')):
        rows.append(inspect(p))
    payload={'schema_version':1,'source_dir':str(a.source.resolve()),'files':rows,'notes':['Read-only source files were opened for measurement only.','LUFS/true peak use experiments.objective_compare.measure_audio; true peak is 4x oversampled.']}
    a.out.parent.mkdir(parents=True,exist_ok=True); a.out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(rows),'out':str(a.out)},ensure_ascii=False))
if __name__=='__main__': main()
