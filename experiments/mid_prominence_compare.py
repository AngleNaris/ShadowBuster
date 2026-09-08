"""Run an isolated real-material mid-prominence comparison.

This deliberately never writes into experiments/results/real_20260905 and never invokes Lew.
It uses the pre-existing 30 s stems plus a cached Demucs separation of 现实速通者 60..90 s.
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(r"D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe")
SOREN = Path(r"D:/_3.AI/audio_upscale/Soren_src")
ORIGINALS = Path(r"D:/_4.Projects/_MY/中二病晚期患者")

def sha(p):
    h=hashlib.sha256();
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()

def run(cmd, cwd=None):
    subprocess.run([str(x) for x in cmd], check=True, cwd=str(cwd) if cwd else None,
                   env={**os.environ, 'PYTHONPATH':str(ROOT)})

def read(p): return sf.read(str(p), always_2d=True, dtype='float64')
def write(p,x,sr): sf.write(str(p), np.asarray(x,dtype=np.float32), sr, subtype='FLOAT')
def safe_dir(base):
    out=base
    i=1
    while out.exists(): out=base.with_name(base.name+f'_{i:02d}'); i+=1
    out.mkdir(parents=True)
    return out

def process_case(name, mix, stems, out, gain):
    mix_a,sr=read(mix); v,vsr=read(stems/'vocals.wav')
    if sr!=vsr or len(mix_a)!=len(v): raise ValueError(f'{name}: stem shape mismatch')
    # Explicitly import the implementation from this checkout, not backend/UI.
    sys.path.insert(0, str(ROOT/'apollo_scripts'))
    from vocal_adjust import apply_mid_prominence_control
    results={}
    for g in gain:
        tag=f'{name}_g{g:+d}'
        old=mix_a + v * (10**(g/20)-1)
        new, report=apply_mid_prominence_control(mix_a,v,sr,g,mix_a,v,vocal_scale=1,return_report=True)
        oldp=out/f'{tag}_legacy.wav'; newp=out/f'{tag}_control.wav'
        write(oldp,old,sr); write(newp,new,sr)
        report['reference_provenance']={'path':str(mix.resolve()),'sha256':sha(mix),'is_processed_mix':False,
                                        'lew_run':False,'note':'offline source; Lew intentionally skipped'}
        results[str(g)]={'legacy':str(oldp),'control':str(newp),'control_report':report}
        # Soren is run only on the control/legacy pair and remains an independent artifact.
        for label,p in [('legacy',oldp),('control',newp)]:
            master=out/f'{tag}_{label}_soren.wav'
            run([PYTHON,SOREN/'core_decrypted.py',p,master,'--genre','Pop','--loudness','normal','--eq-profile','Neutral'],cwd=SOREN)
    return results

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path); ap.add_argument('--no-second',action='store_true'); args=ap.parse_args()
    base=args.out or ROOT/'experiments'/'results'/ 'mid_prominence_compare_20260905'
    out=safe_dir(base.resolve())
    oldroot=ROOT/'experiments/results/real_20260905'; stems=oldroot/'demucs/htdemucs/clip30'
    cases=[('imagined_end',oldroot/'clip30.wav',stems)]
    second=ORIGINALS/'2.现实速通者.wav'; second_stems=out/'demucs_realistic'
    if not args.no_second:
        second_stems.mkdir()
        # Explicitly offline: Demucs must already have a local checkpoint; no download allowed.
        run([PYTHON,'-m','demucs','--name','htdemucs','--two-stems','vocals','-o',second_stems,second],cwd=ROOT)
        found=list(second_stems.rglob('vocals.wav'))
        if not found: raise RuntimeError('cached Demucs did not produce vocals.wav')
        # two-stems has no bass/drums; preserve the real source as residual-compatible stems.
        src=found[0].parent; mix,sr=read(second); a=int(60*sr); b=int(90*sr)
        clip=out/'realistic_60_90.wav'; write(clip,mix[a:b],sr)
        clipst=out/'stems_realistic'; clipst.mkdir()
        for n in ('vocals','bass','drums','other'):
            x=read(src/(n+'.wav'))[0][a:b] if (src/(n+'.wav')).exists() else np.zeros_like(mix[a:b])
            write(clipst/n+'.wav',x,sr)
        cases.append(('realistic_60_90',clip,clipst))
    manifest={'schema_version':1,'reference_policy':'original/offline; no Lew; processed mix never used as reference',
              'source_hashes':{str(p):sha(p) for _,p,_ in cases},'cases':{}}
    for name,mix,st in cases: manifest['cases'][name]=process_case(name,mix,st,out,[0,4,5])
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    print(out)
if __name__=='__main__': main()
