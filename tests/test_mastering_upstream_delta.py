"""方案2（2026-09-10）：styled 母带的"上游意图感知"回归测试。

锚定三条保证：
  1. 后端能从（原始输入, 母带输入）计算出 M/S 频谱意图 delta（形状曲线 +
     宽带 rms 比例）；两文件一致时 delta ≈ 0；
  2. original 的频谱匹配仅做有限修正，delta 进一步减少对上游音色的反向校正；
  3. core 的 styled 路径把 config.upstream_delta 传给 original；
     stage_soren 把 --upstream-delta 写进子进程命令。
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'apollo_scripts'))

import studio_backend as backend
import bass_enhance as bass


from soren_resources import SOREN_RESOURCE_DIR as RESOURCE_DIR


def load(name, path):
    sys.path.insert(0, str(RESOURCE_DIR))  # core 顶层 import test_model 资源
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _band_db(x, sr, lo, hi, ref):
    sos = signal.butter(2, [lo, hi], btype='bandpass', fs=sr, output='sos')
    y = signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64), padlen=0)
    r = signal.sosfiltfilt(sos, np.asarray(ref, dtype=np.float64), padlen=0)
    return 10.0 * np.log10(max(np.mean(y * y), 1e-30) / max(np.mean(r * r), 1e-30))


def _write_wav(path, data, sr):
    # scipy 写入不经 libsndfile：本机曾出现 libsndfile 写路径的间歇性环境故障
    # （读路径始终正常），测试用 scipy 产 wav、被测代码用 soundfile 读，两者兼容。
    # 自动识别 (n, ch) / (ch, n)：通道数更小的轴视为通道。
    from scipy.io import wavfile
    data = np.asarray(data)
    if data.ndim == 2 and data.shape[0] < data.shape[1]:
        data = data.T
    wavfile.write(path, sr, data.astype(np.float32))


def test_delta_curves_from_wav_pair(tmp_path):
    sr = 44100
    rng = np.random.default_rng(7)
    orig = (0.15 * rng.standard_normal((sr * 3, 2)))          # (n, ch)
    post = np.column_stack([bass._shelf(orig[:, c], sr, 60.0, 3.0)
                            for c in range(2)])
    p_orig, p_post = tmp_path / 'o.wav', tmp_path / 'p.wav'
    _write_wav(p_orig, orig, sr)
    _write_wav(p_post, post, sr)

    payload = backend._upstream_delta_curves(p_orig, p_post)
    assert payload is not None
    freqs = np.asarray(payload['freqs'])
    mid = np.asarray(payload['mid_db'])
    assert freqs[0] == 20.0 and len(freqs) == len(mid) and np.all(np.diff(freqs) > 0)
    assert 1.0 < payload['rms_mid'] < 1.4          # +3dB shelf 的宽带效应
    assert payload['rms_side'] == pytest.approx(payload['rms_mid'], abs=0.02)
    low = float(np.mean(mid[(freqs >= 20) & (freqs <= 40)]))
    high = float(np.mean(mid[(freqs >= 4000) & (freqs <= 6000)]))
    assert low - high > 1.5                         # 低频形状被保留在曲线里
    assert abs(high) <= 3.0                         # 高频段接近宽带水平

    same = backend._upstream_delta_curves(p_orig, p_orig)
    assert same is not None
    assert float(np.max(np.abs(same['mid_db']))) < 0.5
    assert float(np.max(np.abs(same['side_db']))) < 0.5
    assert same['rms_mid'] == pytest.approx(1.0, abs=0.02)


def test_matching_respects_upstream_delta():
    original = load('original_ud', ROOT / 'packaging/soren_original.py')
    sr = 44100
    rng = np.random.default_rng(11)
    # 宽带带限噪声：真实音乐类频谱（全部 FFT bin 有内容），避免匹配器在
    # 无内容 bin 上的地板/平滑伪影（稀疏正弦会病态触发，与生产场景无关）。
    sos = signal.butter(4, 12000.0, 'lowpass', fs=sr, output='sos')
    ref_mid = signal.sosfilt(sos, rng.standard_normal(sr * 2)) * 0.1
    ref_side = signal.sosfilt(sos, rng.standard_normal(sr * 2)) * 0.03
    target_mid = bass._shelf(ref_mid, sr, 60.0, 3.0)   # 上游低频意图 +3dB
    target_side = ref_side

    # 测试直接喂 44.1k 信号，config 必须声明一致的分析率（生产中信号先经
    # oversample() 真 4× 升采样，声明率与真实率一致，无此错位）。
    cfg_plain = original.Config()
    cfg_plain.oversampling_factor = 1
    out_plain, _ = original.match_frequencies_ms(
        target_mid, target_side, ref_mid, ref_side, cfg_plain)

    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        td = Path(td)
        _write_wav(td / 'o.wav', np.column_stack([ref_mid, ref_mid]), sr)
        _write_wav(td / 'p.wav', np.column_stack([target_mid, target_mid]), sr)
        payload = backend._upstream_delta_curves(td / 'o.wav', td / 'p.wav')
    assert payload is not None
    cfg_ud = original.Config()
    cfg_ud.oversampling_factor = 1
    cfg_ud.upstream_delta = payload
    out_ud, _ = original.match_frequencies_ms(
        target_mid, target_side, ref_mid, ref_side, cfg_ud)

    plain_db = _band_db(out_plain, sr, 25, 70, target_mid)
    ud_db = _band_db(out_ud, sr, 25, 70, target_mid)
    assert -0.8 < plain_db < -0.1                   # bounded matching only nudges the input
    assert abs(ud_db) < 0.2                         # delta further protects the intended shelf
    assert ud_db - plain_db > 0.15


def test_core_styled_propagates_upstream_delta(monkeypatch):
    core = load('core_ud', ROOT / 'packaging/soren_core.py')
    captured = {}

    class StubOriginal:
        class Config:
            pass

        @staticmethod
        def process_audio(target, reference, step, config, genre_profile):
            captured['upstream_delta'] = getattr(config, 'upstream_delta', 'MISSING')
            return np.asarray(target, dtype=np.float64)

    monkeypatch.setattr(core, 'load_soren_original_module', lambda: StubOriginal)
    rng = np.random.default_rng(3)
    audio = rng.normal(0, .1, (2, 44100))
    ud = {'freqs': [20.0, 1000.0], 'mid_db': [1.0, 0.0], 'side_db': [0.5, 0.0],
          'rms_mid': 1.1, 'rms_side': 0.9}
    cfg = core.Config()
    cfg.upstream_delta = ud
    core.process_original_styled(audio, audio.copy(), 5, cfg, {'genre': 'Pop', 'lufs': -9.0})
    assert captured['upstream_delta'] == ud


def test_stage_soren_command_includes_delta(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd, env=None, cancel=None, on_progress=None):
        captured['cmd'] = list(cmd)

    monkeypatch.setattr(backend, '_run_stream', fake_run)
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: backend.DEV_SOREN_RUNTIME)
    delta = tmp_path / 'upstream_delta.json'
    delta.write_text(json.dumps({'freqs': [20.0], 'mid_db': [0.0], 'side_db': [0.0],
                                 'rms_mid': 1.0, 'rms_side': 1.0}), encoding='utf-8')
    backend.stage_soren(tmp_path / 'in.wav', tmp_path / 'out.wav',
                        upstream_delta_path=delta, upstream_delta_hash='abc')
    cmd = captured['cmd']
    assert '--upstream-delta' in cmd
    assert cmd[cmd.index('--upstream-delta') + 1] == str(delta)

    captured.clear()
    backend.stage_soren(tmp_path / 'in.wav', tmp_path / 'out.wav')
    assert '--upstream-delta' not in captured['cmd']
