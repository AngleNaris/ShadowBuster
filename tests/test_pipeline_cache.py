import json
from pathlib import Path
import pytest
import pipeline_cache as pc


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path / 'cache'))


def test_hit_params_content_and_corruption(tmp_path):
    src=tmp_path/'source'; src.write_bytes(b'input')
    out=tmp_path/'out'; calls=[]
    cache=pc.StageCache(identity='v1')
    def work():
        calls.append(1);out.write_bytes(src.read_bytes()+b'out');return .75
    assert cache.run('bass',[src],{'sub':2},[out],work)==.75
    out.unlink()
    assert cache.run('bass',[src],{'sub':2},[out],work)==.75
    assert len(calls)==1 and out.exists()
    cache.run('bass',[src],{'sub':3},[out],work)
    assert len(calls)==2
    src.write_bytes(b'other');cache.run('bass',[src],{'sub':3},[out],work)
    assert len(calls)==3
    for entry in pc.entries():
        (entry/'0').write_bytes(b'corrupt')
    cache.run('bass',[src],{'sub':3},[out],work)
    assert len(calls)==4


def test_directory_restore_capacity_clear_and_lock(tmp_path):
    src=tmp_path/'src';src.write_bytes(b'x')
    out=tmp_path/'stems'
    def work():
        out.mkdir(exist_ok=True);(out/'bass.wav').write_bytes(b'stem');return None
    pc.StageCache().run('demucs',[src],{},[out],work)
    assert pc.get_info()['entries']==1
    user=pc.root()/'user.txt';user.write_bytes(b'keep')
    with pc.locked():
        with pytest.raises(RuntimeError):pc.clear_cache()
    pc.set_capacity_gb(0)
    assert pc.get_info()['entries']==0 and not pc.get_settings()['enabled']
    assert user.read_bytes()==b'keep'
    pc.clear_cache();assert user.exists()


def test_downstream_reuse(tmp_path):
    src=tmp_path/'src';src.write_bytes(b'original')
    first=tmp_path/'first';second=tmp_path/'second';calls=[]
    cache=pc.StageCache()
    def run(level):
        def one():calls.append('separate');first.write_bytes(b'stems')
        def two():calls.append('enhance');second.write_bytes(first.read_bytes()+str(level).encode())
        cache.run('separate',[src],{},[first],one)
        cache.run('enhance',[first],{'level':level},[second],two)
    run(1);run(1);run(2)
    assert calls==['separate','enhance','enhance']


def test_pipeline_reuses_real_stage_call_sites(tmp_path, monkeypatch):
    import studio_backend as backend
    src=tmp_path/'song.wav';src.write_bytes(b'original')
    calls=[]
    def stage_demucs(input_wav, out_dir, progress=None, cancel=None):
        calls.append('demucs')
        d=Path(out_dir)/'htdemucs'/Path(input_wav).stem
        d.mkdir(parents=True);(d/'bass.wav').write_bytes(b'stem')
    def stage_soren(input_wav, out_wav, **kwargs):
        calls.append('soren');Path(out_wav).write_bytes(Path(input_wav).read_bytes()+kwargs['loudness'].encode())
    monkeypatch.setattr(backend,'stage_demucs',stage_demucs)
    monkeypatch.setattr(backend,'stage_soren',stage_soren)
    bypass=['lew','bass','drums','reshape','vocals']
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass)
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass)
    assert calls==['demucs','soren']
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass,loudness='loud')
    assert calls==['demucs','soren','soren']
