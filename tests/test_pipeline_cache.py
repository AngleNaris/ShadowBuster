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
    def ffmpeg_convert(src, dst, sr=44100):
        Path(dst).write_bytes(b'44k')
    def stage_demucs(input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None):
        calls.append('demucs')
    def stage_soren(input_wav, out_wav, **kwargs):
        calls.append('soren');Path(out_wav).write_bytes(Path(input_wav).read_bytes()+kwargs['loudness'].encode())
    def stage_hygiene(input_wav, out_wav, **kwargs):
        Path(out_wav).write_bytes(Path(input_wav).read_bytes());return 1.0
    monkeypatch.setattr(backend,'ffmpeg_convert',ffmpeg_convert)
    monkeypatch.setattr(backend,'stage_demucs',stage_demucs)
    monkeypatch.setattr(backend,'stage_soren',stage_soren)
    monkeypatch.setattr(backend,'stage_hygiene',stage_hygiene)
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: tmp_path / 'runtime')
    bypass=['lew','bass','drums','reshape','vocals']
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass)
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass)
    assert calls==['soren']
    backend.run_pipeline(src,tmp_path/'out',bypass=bypass,loudness='loud')
    assert calls==['soren','soren']


# ── audit §9.7: content-id 去重复 hash + 按昂贵程度分级淘汰 ───────────────

def test_fingerprint_content_id_memoizes_repeated_hash(tmp_path, monkeypatch):
    """同一文件重复 fingerprint 只算一次 MD5：(size, mtime_ns) 是内容 id；
    改写文件（mtime 前进）后必须重算，返回值仍是真正的 md5。"""
    import hashlib
    original = pc._md5_bytes
    hashed = []
    monkeypatch.setattr(pc, '_md5_bytes', lambda p: (hashed.append(str(p)), original(p))[1])
    f = tmp_path/'stem.wav'; f.write_bytes(b'audio-bytes')
    first = pc.fingerprint(f)
    second = pc.fingerprint(f)
    assert first == second == hashlib.md5(b'audio-bytes').hexdigest()
    assert len(hashed) == 1
    f.write_bytes(b'audio-bytes-changed')        # 内容变 → mtime 变 → 重算
    assert pc.fingerprint(f) == hashlib.md5(b'audio-bytes-changed').hexdigest()
    assert len(hashed) == 2


def test_fingerprint_directory_hashes_each_stem_once(tmp_path, monkeypatch):
    """六个 stem 目录被多个下游阶段反复 fingerprint：整套只各 hash 一次。"""
    original = pc._md5_bytes
    hashed = []
    monkeypatch.setattr(pc, '_md5_bytes', lambda p: (hashed.append(str(p)), original(p))[1])
    d = tmp_path/'stems'; d.mkdir()
    for name in ('bass', 'drums', 'vocals', 'other', 'guitar', 'piano'):
        d.joinpath(name + '.wav').write_bytes((name * 4096).encode())
    snapshot = pc.fingerprint(d)
    after_first = len(hashed)
    assert after_first == 6
    assert pc.fingerprint(d) == snapshot          # 再指纹（模拟 bass/drums/reshape…）全部命中
    assert len(hashed) == after_first


def test_evict_keeps_tier_a_ai_product_over_newer_cheap_dsp(tmp_path):
    """容量吃紧时先淘汰便宜 DSP（Tier C），最贵 AI 产物（Tier A）最后淘汰——
    即便 Tier A 的目录 mtime 比 Tier C 更旧（纯 LRU 会先删 A）。"""
    import os
    src = tmp_path/'input'; src.write_bytes(b'song')
    pc.set_capacity_gb(0.0006)                     # ~644 KiB：容得下一个、容不下两个 500 KiB
    def stage(out_path, payload, identity, tier, stage_name, params):
        out = tmp_path/out_path
        def work():
            out.write_bytes(payload * (500 * 1024 // len(payload)))
            return None
        pc.StageCache(True, identity, tier=tier).run(
            stage_name, [src], params, [out], work)
    stage('a.wav', b'A', 'ai-identity', 'A', 'prepared_demucs', {'model': 'htdemucs_6s'})
    ai_entry = pc.entries()[0]
    stale = 1_000_000_000
    os.utime(ai_entry, (stale, stale))             # 让 Tier A 成为最旧
    stage('c.wav', b'C', 'dsp-identity', 'C', 'bass', {'sub': 2})
    surviving = list(pc.entries())
    assert ai_entry in surviving                   # AI 产物被保下来
    assert len(surviving) == 1                     # 超容量后只剩 Tier A，Tier C 先被淘汰
