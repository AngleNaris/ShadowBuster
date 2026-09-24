import numpy as np
import soundfile as sf
from mastering.draft_levels import measure
from audio_metrics import integrated_lufs


def test_stream_calibration_matches_integrated_measurement(tmp_path):
    sr=44100
    t=np.arange(sr*3)/sr
    audio=np.column_stack((.12*np.sin(2*np.pi*1000*t),.08*np.sin(2*np.pi*220*t)))
    path=tmp_path/'tone.wav'
    sf.write(path,audio,sr,subtype='FLOAT')
    actual=measure(path)
    expected,_=integrated_lufs(audio,sr)
    assert abs(actual-expected)<.2


def test_stream_calibration_silence_and_gain(tmp_path):
    sr=44100
    path=tmp_path/'tone.wav'
    audio=np.zeros((sr,2))
    sf.write(path,audio,sr,subtype='FLOAT')
    assert measure(path)==-70
    audio[:,0]=audio[:,1]=.1*np.sin(2*np.pi*440*np.arange(sr)/sr)
    sf.write(path,audio,sr,subtype='FLOAT');quiet=measure(path)
    sf.write(path,audio*2,sr,subtype='FLOAT');loud=measure(path)
    assert abs(loud-quiet-6.0206)<.001


def test_live_mastering_targets_and_bypass(tmp_path):
    import json
    import shutil
    import subprocess
    import pytest
    from mastering import LOUDNESS_TARGETS
    from mastering.draft_levels import coefficients
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    metadata={'mixLufs':-20,'targets':LOUDNESS_TARGETS,'weighting':coefficients(44100)}
    script=r"""
const fs=require('fs'),{DraftDSP}=require('./ui/draft_audio.js');
const levels=JSON.parse(process.argv[1]),dir=process.argv[2],n=44100*4;
const input=new Float32Array(n*2);for(let i=0;i<n;i++)input[i*2]=input[i*2+1]=.1*Math.sin(2*Math.PI*1000*i/44100);
for(const mode of ['soft','dynamic','normal','loud','bypass']){
 const dsp=new DraftDSP({buffers:{mix:input,original:input},sampleRate:44100,frames:n,gain:1,stream:true,levels});
 dsp.setParams({loudness:mode==='bypass'?'loud':mode,bypass:mode==='bypass'?['soren']:[]});dsp.params={...dsp.target};dsp.playing=true;
 const out=new Float32Array(n*2);
 for(let i=0;i<n;i+=128){const len=Math.min(128,n-i),l=new Float32Array(len),r=new Float32Array(len);dsp.render(l,r);for(let j=0;j<len;j++){out[(i+j)*2]=l[j];out[(i+j)*2+1]=r[j];}}
 fs.writeFileSync(dir+'/'+mode,Buffer.from(out.buffer));
}
"""
    subprocess.run(['node','-e',script,json.dumps(metadata),str(tmp_path)],check=True)
    for mode,target in LOUDNESS_TARGETS.items():
        audio=np.fromfile(tmp_path/mode,dtype='<f4').reshape(-1,2)[44100*2:]
        assert np.isfinite(audio).all()
        assert abs(audio).max()<=.945
        assert abs(integrated_lufs(audio,44100)[0]-target)<.3
    bypass=np.fromfile(tmp_path/'bypass',dtype='<f4').reshape(-1,2)
    expected=.1*np.sin(2*np.pi*1000*np.arange(len(bypass))/44100)
    np.testing.assert_allclose(bypass[:,0],expected,atol=1e-7)
