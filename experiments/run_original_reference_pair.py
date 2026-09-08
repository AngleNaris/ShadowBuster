"""Two-track offline audition using hash-verified historical upstream stages."""
import os, sys, json, shutil
from pathlib import Path
from datetime import datetime
import numpy as np
import soundfile as sf
from scipy import signal
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'apollo_scripts')]
from experiments.run_requested_preset import PRESET, SOURCE, digest
import studio_backend as b
import vocal_adjust as va
CACHE = ROOT/'experiments/results/analysis_vocal_reference_20260906'
os.environ['HF_HOME'] = str(ROOT/'packaging/stage/runtime_gpu/hf_home')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
b.PYTHON = ROOT/'packaging/stage/runtime_gpu/env/python.exe'

def main():
    dest = SOURCE/('SorenPresenceFix_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    dest.mkdir(exist_ok=False)
    print('OUTPUT_DIRECTORY='+str(dest), flush=True)
    history = json.loads((CACHE/'assessment.json').read_text(encoding='utf-8'))
    report = {'preset':dict(PRESET, balance_target_db=None, balance_mode=va.REFERENCE_MODE),
              'method':'Same original reliable mask for original/output independently HTDemucs-separated estimates. 100ms/50ms; medians, not ground truth.',
              'upstream':'Hash-verified complete historical Lew/bass/drums/shape stages, identical requested preset; copied into independent work directories.',
              'listening_evaluated':False, 'tracks':[]}
    def save():
        (dest/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    for name in ('5.愚蠢人类观察家','6.想象在彼方'):
        src=SOURCE/(name+'.wav'); old=CACHE/name; work=dest/('.work_'+name);work.mkdir()
        hashes={}
        for p in [src,*old.rglob('*.wav')]:
            key=str(p); expected=history['hashes'].get(key) or history['hashes'].get(key.replace('\\','/'))
            actual=digest(p)
            if expected is None:
                expected=next((v for k,v in history['hashes'].items() if Path(k)==p),None)
            assert expected==actual, ('cache hash',str(p))
            hashes[str(p)]=actual
        for filename in ('original.wav','lew.wav','bass.wav','drums.wav','shape.wav','bass.json','drums.json','reshape.json'):
            shutil.copyfile(old/filename,work/filename)
        for directory in ('original_stems','lew_stems'):
            shutil.copytree(old/directory,work/directory)
        log=work/'commands.log'; stream=b._run_stream
        def logged(cmd,cwd,**kw):
            result=b._run(cmd,cwd,env=kw.get('env'))
            with log.open('a',encoding='utf-8') as f:f.write(repr(list(map(str,cmd)))+'\n'+str(result)+'\n')
            return result
        b._run_stream=logged
        stems=work/'lew_stems/htdemucs/lew'
        scales=[json.loads((work/(s+'.json')).read_text())['scale'] for s in ('bass','drums','reshape')]
        vocal=work/'reference_vocal.wav'
        b.stage_vocals(stems,work/'shape.wav',vocal, gain_db=0,
            reference_mix=work/'original.wav',reference_vocals=work/'original_stems/htdemucs/original/vocals.wav',
            vocal_scale=float(np.prod(scales)),balance_mode=va.REFERENCE_MODE)
        final=dest/(name+'.wav')
        b.stage_soren(vocal,final,genre='Pop',loudness='loud',eq_profile='Neutral')
        decoded=work/'final_decoded.wav'; b.ffmpeg_convert(final,decoded)
        b.stage_demucs(decoded,work/'final_stems')
        b.stage_demucs(vocal,work/'pre_master_stems')
        def read(p):
            x,sr=sf.read(p,always_2d=True);assert sr==44100;return x
        original=read(work/'original.wav'); ov=read(work/'original_stems/htdemucs/original/vocals.wav')
        mask=va.vocal_activity_metrics(original,ov,44100)['ratio_valid']
        np.savez_compressed(work/'comparison_mask.npz',valid=mask)
        def measure(m,v):
            assert m.shape==v.shape==original.shape
            results={}
            for band in ('wide_mid','150-5000_mid','all_channels'):
                vv,aa=v,m-v
                if band=='150-5000_mid':
                    sos=signal.butter(4,[150,5000],btype='bandpass',fs=44100,output='sos')
                    vv=signal.sosfilt(sos,vv,axis=0);aa=signal.sosfilt(sos,aa,axis=0)
                def rms(x):
                    if band=='all_channels':return np.sqrt(sum(va.mid_rms_windows(x[:,i:i+1],44100)[1]**2 for i in range(2))/2)
                    return va.mid_rms_windows(x,44100)[1]
                ratio=20*np.log10(np.maximum(rms(vv),1e-12)/np.maximum(rms(aa),1e-12))
                results[band]=float(np.median(ratio[mask]))
            return results
        item={'track':name,'original':measure(original,ov),
              'pre_master_reseparated':measure(read(vocal),read(work/'pre_master_stems/htdemucs/reference_vocal/vocals.wav')),
              'output':measure(read(decoded),read(work/'final_stems/htdemucs/final_decoded/vocals.wav')),
              'mask_valid':int(mask.sum()),'vocal_report':str(vocal.with_suffix('.vocal.json')),
              'source_sha256':hashes[str(src)],'output_path':str(final),'output_sha256':digest(final)}
        audio,sr=sf.read(final,always_2d=True);info=sf.info(final)
        assert np.isfinite(audio).all() and abs(info.duration-sf.info(src).duration)<.1
        item.update(finite=True,sample_rate=sr,duration_seconds=info.duration,peak=float(abs(audio).max()),channels=info.channels)
        item['residual_db']={k:item['output'][k]-item['original'][k] for k in item['original']}
        assert all(digest(Path(p))==h for p,h in hashes.items())
        item['source_and_historical_hashes_reverified']=True
        report['tracks'].append(item);save();print(json.dumps(item,ensure_ascii=False),flush=True)
        b._run_stream=stream
    print('COMPLETE '+str(dest),flush=True)
if __name__=='__main__':main()
