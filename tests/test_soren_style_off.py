import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
# 权威源码：packaging/soren_core.py（打包/运行时同名 core_decrypted.py）；
# test_model 等资源走统一路由（dev runtime 优先，SB_SOREN/外部兜底）。
CORE = ROOT/'packaging/soren_core.py'
from soren_resources import SOREN_RESOURCE_DIR
sys.path.insert(0, str(SOREN_RESOURCE_DIR))
STAGE_CPU = ROOT/'packaging/stage/runtime/Soren_src/core_decrypted.py'
STAGE_GPU = ROOT/'packaging/stage/runtime_gpu/Soren_src/core_decrypted.py'
spec = importlib.util.spec_from_file_location('soren_style_test_core', CORE)
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


def test_off_skips_all_style_functions_and_preserves_spectrum(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Style function called')
    for name in ('match_rms_ms', 'add_subtle_mid_channel_saturation', 'match_frequencies_ms',
                 'low_shelf_tighten', 'apply_eq_style', 'apply_lowpass_filter',
                 'gradual_level_correction', 'correct_mid_presence', 'finalize_stereo_image',
                 'improved_anti_aliasing_filter'):
        monkeypatch.setattr(core, name, forbidden)
    sr = 44100
    t = np.arange(sr * 3) / sr
    x = sum(.015 * np.sin(2*np.pi*f*t) for f in (1000, 2000, 4000, 8000, 12000))
    audio = np.vstack([x, x*.7])
    before = audio.copy()
    cfg = core.Config(); cfg.style_mode = 'off'
    target = core.calculate_lufs(audio, sr) + 1
    out = core.process_audio(audio, None, 5, cfg, {'lufs':target})
    np.testing.assert_array_equal(audio, before)
    assert abs(core.calculate_lufs(out, sr) - target) < .2
    assert core.calculate_true_peak(out, sr) <= cfg.true_peak_ceiling_db
    f, p = signal.welch(audio[0], sr, nperseg=44100)
    _, q = signal.welch(out[0], sr, nperseg=44100)
    bins = np.array([1000, 2000, 4000, 8000, 12000])
    gains = 10*np.log10(q[bins]/p[bins])
    assert np.ptp(gains) < .06
    np.testing.assert_allclose(out[1], out[0]*.7, atol=3e-7)
    assert cfg.last_mastering_stats['limiter']['max_gain_reduction_db'] == 0


def test_off_active_limiter_target_and_peak():
    sr=44100; rng=np.random.default_rng(21)
    audio=signal.sosfilt(signal.butter(2, 6000, fs=sr, output='sos'),rng.normal(size=(2,sr*3)),axis=-1)*.1
    audio[:, sr] = 0.8
    cfg=core.Config(); cfg.style_mode='off'
    out=core.process_audio(audio,None,5,cfg,{'lufs':-12})
    assert abs(core.calculate_lufs(out,sr)+12)<.2
    assert core.calculate_true_peak(out,sr)<=cfg.true_peak_ceiling_db
    assert cfg.last_mastering_stats['limiter']['max_gain_reduction_db']>0


def test_backend_default_and_off_cli(monkeypatch):
    import studio_backend as b
    monkeypatch.setattr(b, '_ensure_dev_runtime', lambda: b.DEV_SOREN_RUNTIME)
    calls=[]
    monkeypatch.setattr(b,'_run_stream',lambda cmd,*a,**k:calls.append(cmd))
    b.stage_soren('in.wav','out.wav')
    assert '--style-mode' not in calls[-1]
    b.stage_soren('in.wav','out.wav',style_mode='off')
    assert calls[-1][1:3] == ['-m', 'mastering']
    assert '--genre' not in calls[-1] and '--style-mode' not in calls[-1]
    with pytest.raises(ValueError):b.stage_soren('in.wav','out.wav',style_mode='invalid')


def test_refuse_input_overwrite(tmp_path):
    p=tmp_path/'input.wav';p.write_bytes(b'unchanged')
    with pytest.raises(ValueError):core.master_audio(str(p),str(p),core.Config(),'Neutral')
    assert p.read_bytes()==b'unchanged'


def test_batch_pipeline_style_routing(tmp_path, monkeypatch):
    import shutil
    import studio_backend as b
    source=tmp_path/'source.wav'; source.write_bytes(b'unchanged')
    calls=[]
    monkeypatch.setattr(b,'stage_demucs',lambda *a,**k:None)
    def master(src,dst,**kwargs):
        calls.append(kwargs)
        shutil.copyfile(src,dst)
    monkeypatch.setattr(b,'stage_soren',master)
    # 母带前卫生滤波独立于风格路由：本用例审计风格参数转发，替身直通
    monkeypatch.setattr(b,'stage_hygiene',lambda src,dst,**kw:shutil.copyfile(src,dst))
    monkeypatch.setattr(b, '_ensure_dev_runtime', lambda: b.DEV_SOREN_RUNTIME)
    b.run_batch([source],tmp_path/'out',style_mode='off',
                bypass=('lew','bass','drums','reshape','vocals'))
    assert len(calls)==1 and calls[0]['style_mode']=='off'
    assert source.read_bytes()==b'unchanged'


def test_eq_only_applies_selected_eq_without_reference_processing(monkeypatch):
    cfg=core.Config();cfg.style_mode='eq_only';cfg.eq_style='Warm'
    audio=np.random.default_rng(5).normal(0,.01,(2,44100))
    before=audio.copy();calls=[]
    def eq(mid,side,sr,style):
        calls.append((sr,style))
        return mid*.8,side
    monkeypatch.setattr(core,'apply_eq_style',eq)
    def transparent(target,config,requested,module):
        config.last_mastering_stats={}
        return target
    monkeypatch.setattr(core,'process_transparent',transparent)
    out=core.process_audio(audio,None,5,cfg,{'lufs':-12})
    assert calls==[(44100,'Warm')]
    assert not np.array_equal(out,audio)
    np.testing.assert_array_equal(audio,before)
    assert cfg.last_mastering_stats['style_mode']=='eq_only'
    assert cfg.last_mastering_stats['spectral_processing'] is True


def test_gui_routes_reference_and_eq_and_loudness():
    js=(ROOT/'ui/app.js').read_text(encoding='utf-8')
    html=(ROOT/'ui/index.html').read_text(encoding='utf-8')
    # 2026-09-28 移除内置流派（版权参考曲目难以获取）：风格只来自用户参考；
    # 参考音频入口挪进母带面板，响度改为与 EQ 风格一致的分段组件。
    assert 'dd-genre' not in js and 'dd-genre' not in html
    assert 'getGenre' not in js
    assert 'data-loudness="normal"' in html and 'data-loudness="soft"' in html
    assert 'id="dd-loudness"' not in html
    ref_at = html.find('id="ref-card"')
    assert ref_at > html.find('data-rack="soren"') > 0   # 参考卡位于母带面板内
    assert 'id="ref-card"' not in html.split('data-rack="soren"')[0]
    assert 'state.eq === "Neutral" ? "off" : "eq_only"' in js
    assert 'style_mode: state.reference ? "styled" : (state.eq === "Neutral" ? "off" : "eq_only")' in js
    assert 'loudness: getLoudness(), eq: state.eq' in js
    # 默认值自 v1.6.10 起来自 backend.DEFAULTS 单一来源（ENG-01）；
    # 不传参时仍解析为 'styled'（tests/test_default_source.py 校验）。
    main=(ROOT/'main.py').read_text(encoding='utf-8')
    assert 'style_mode=params.get("style_mode", d["style_mode"])' in main


@pytest.mark.skipif(not STAGE_CPU.is_file(),
                    reason='packaging/stage 为派生产物且未构建；一致性由 runtime_sync 自检覆盖')
def test_staged_cores_match_canonical():
    """stage 副本存在时必须与 canonical 逐字节一致（构建一致性抽查）。"""
    canonical = (ROOT/'packaging/soren_core.py').read_bytes()
    assert STAGE_CPU.read_bytes() == canonical
    if STAGE_GPU.is_file():
        assert STAGE_GPU.read_bytes() == canonical
