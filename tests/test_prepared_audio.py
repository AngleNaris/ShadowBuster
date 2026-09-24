from pathlib import Path
import shutil

import numpy as np
import soundfile as sf

import draft_preview
import prepared_audio
import studio_backend as backend


def setup_audio(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path/'cache'))
    monkeypatch.setattr(backend, 'APOLLO_DIR', tmp_path/'apollo')
    source=tmp_path/'song.wav'
    t=np.arange(44100)/44100
    x=np.column_stack((.1*np.sin(2*np.pi*220*t),.08*np.sin(2*np.pi*220*t)))
    sf.write(source,x,44100,subtype='FLOAT')
    calls={'lew':0,'demucs':0}
    def lew(inp,out,**kw):
        calls['lew']+=1
        y,sr=sf.read(inp); sf.write(out,y*.9,sr,subtype='FLOAT')
    def demucs(inp,out,model='htdemucs',**kw):
        calls['demucs']+=1
        dst=Path(out)/model/Path(inp).stem; dst.mkdir(parents=True,exist_ok=True)
        y,sr=sf.read(inp)
        for name in ('bass','drums','vocals','other','guitar','piano'):
            sf.write(dst/(name+'.wav'),y/6,sr,subtype='FLOAT')
    monkeypatch.setattr(backend,'stage_lew',lew)
    monkeypatch.setattr(backend,'stage_demucs',demucs)
    monkeypatch.setattr(backend,'ffmpeg_convert',lambda src,dst,*a,**kw:shutil.copyfile(src,dst))
    return source,calls


def test_draft_changes_and_formal_consumer_share_ai_results(tmp_path,monkeypatch):
    src,calls=setup_audio(tmp_path,monkeypatch)
    options=dict(device='cpu',quality=1,guidance=1.5,demucs_model='htdemucs',style_mode='off')
    first=draft_preview.prepare(backend,src,0,.5,options)
    assert calls=={'lew':1,'demucs':2}
    second=draft_preview.prepare(backend,src,.3,.8,{**options,'eq_profile':'Bright','style_blend':.2})
    assert calls=={'lew':1,'demucs':2}
    assert first['frames']==second['frames']==22050
    assert set(first['buffers'])=={'original','mix'} | {prefix+n for prefix in ('','dry_') for n in ('bass','drums','vocals','other','guitar','piano')}
    out=tmp_path/'formal_lew.wav'
    bundle=prepared_audio.endpoints(backend,src,tmp_path/'formal',quality=1,device='cpu')
    for guidance in (0, .8, 1.5, 2):
        prepared_audio.mix_endpoints(*bundle,out,tmp_path/'mixed_stems',guidance)
        assert (tmp_path/'mixed_stems'/'guitar.wav').is_file()
        mix,sr=sf.read(out)
        summed=sum(sf.read(tmp_path/'mixed_stems'/(n+'.wav'))[0] for n in ('bass','drums','vocals','other','guitar','piano'))
        np.testing.assert_allclose(mix,summed,atol=1e-7)
    assert calls=={'lew':1,'demucs':2}
    prepared_audio.lew(backend,src,out,device='cpu',quality=2,guidance=1.5)
    assert calls['lew']==2


def test_cache_does_not_reuse_different_separation_model(tmp_path,monkeypatch):
    src,calls=setup_audio(tmp_path,monkeypatch)
    for model in ('htdemucs','htdemucs_6s','htdemucs'):
        prepared_audio.demucs(backend,src,tmp_path/'stems',model=model,device='cpu')
    assert calls['demucs']==2


def test_failed_preparation_is_not_cached(tmp_path,monkeypatch):
    import pytest
    src,calls=setup_audio(tmp_path,monkeypatch)
    original=backend.stage_lew
    def failed(*a,**kw):
        calls['lew']+=1; raise RuntimeError('cancelled')
    monkeypatch.setattr(backend,'stage_lew',failed)
    with pytest.raises(RuntimeError): prepared_audio.lew(backend,src,tmp_path/'out.wav')
    monkeypatch.setattr(backend,'stage_lew',original)
    prepared_audio.lew(backend,src,tmp_path/'out.wav')
    assert calls['lew']==2


def test_full_song_session_reads_bounded_chunks_without_more_ai(tmp_path, monkeypatch):
    src, calls = setup_audio(tmp_path, monkeypatch)
    samples, rate = sf.read(src)
    sf.write(src, np.concatenate([np.tile(samples, (36, 1)), samples[:rate//4]]), rate, subtype='FLOAT')
    session = draft_preview.Session()
    options = dict(device='cpu', quality=1, guidance=1.5, demucs_model='htdemucs', style_mode='off')
    result = draft_preview.prepare(backend, src, 0, 1, options, session=session)
    assert result['end'] == 36.25
    assert result['stream'] and 'buffers' not in result
    import base64
    for index, frames in [(0, rate), (30, rate), (36, rate//4)]:
        chunk = session.chunk(index)
        assert chunk['frames'] == frames
        assert len(base64.b64decode(chunk['buffers']['mix'])) == frames*2*4
    assert calls == {'lew': 1, 'demucs': 2}
