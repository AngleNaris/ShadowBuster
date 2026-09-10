"""2026-09-07 DSP 修复回归测试。

覆盖四项修复（不切回旧 soren_core 链，styled soren_original 语义保持）：
1. 母带链滤波器使用过采样率（低架 tighten / EQ style 在 4x 过采样数据上
   必须按 176400 Hz 设计，而不是 internal_sample_rate=44100）。
2. low_shelf_tighten 是真正的 RBJ 低架（低频衰减、高频原样），
   不再是"audio*gain + lowpass*(1-gain)"那种切高频的错误拓扑。
3. frequency_dependent_mix 作用于频域 matching 曲线（FIR 之前），
   频响与时域位置无关（时不变）；不再把 len(audio) 的频率曲线套在时间轴上。
4. soundstage_reshape 只对新增（delta）side 的低频做保护：
   mid 增量与原始 side 不动，wet=0 精确恒等。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
APOLLO_DIR = ROOT / 'apollo_scripts'
# soren_original 顶层 import test_model：资源走统一路由（dev runtime 优先，
# SB_SOREN/外部兜底），不再依赖 packaging/stage 派生副本。
from soren_resources import SOREN_RESOURCE_DIR as _soren_res
if str(_soren_res) not in sys.path:
    sys.path.insert(0, str(_soren_res))
if str(APOLLO_DIR) not in sys.path:
    sys.path.insert(0, str(APOLLO_DIR))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def original():
    return load('soren_original_fix_test', ROOT / 'packaging/soren_original.py')


@pytest.fixture(scope='module')
def reshape():
    return load('soundstage_reshape_fix_test', APOLLO_DIR / 'soundstage_reshape.py')


def _tone_amplitude(x, freq, sr, skip=0.1):
    """稳态区正弦幅度的最小二乘估计（避开滤波器起振/收尾）。"""
    start = int(sr * skip)
    seg = x[start:len(x) - start] if start else x
    t = np.arange(len(seg)) / sr
    c = np.cos(2 * np.pi * freq * t)
    s = np.sin(2 * np.pi * freq * t)
    a = 2 * np.mean(seg * c)
    b = 2 * np.mean(seg * s)
    return np.hypot(a, b)


# ── 修复 1+2：过采样率下的真正低频 side shelf ─────────────────────────────

def test_low_shelf_tighten_cuts_lows_leaves_highs(original):
    sr = 44100 * 4  # 母带链在 4x 过采样域调用
    t = np.arange(sr) / sr
    low = 0.5 * np.sin(2 * np.pi * 40 * t)
    high = 0.5 * np.sin(2 * np.pi * 800 * t)
    out_low = original.low_shelf_tighten(low.copy(), sr, cutoff_freq=100, gain=0.5)
    out_high = original.low_shelf_tighten(high.copy(), sr, cutoff_freq=100, gain=0.5)
    low_gain_db = 20 * np.log10(_tone_amplitude(out_low, 40, sr) / 0.5)
    high_gain_db = 20 * np.log10(_tone_amplitude(out_high, 800, sr) / 0.5)
    assert low_gain_db == pytest.approx(-6.02, abs=0.4)   # 低频衰减 ~gain=-6dB
    assert high_gain_db == pytest.approx(0.0, abs=0.1)    # 高频原样（旧实现会切高频）


def test_side_shelf_and_eq_receive_oversampled_rate(original):
    """process_audio(step>=2) 必须把过采样率传给 side 低架与 EQ style。"""
    cfg = original.Config()
    cfg.loudness_option = 'normal'
    cfg.eq_style = 'Warm'
    rng = np.random.default_rng(5)
    target = (0.1 * rng.standard_normal((2, 44100))).astype(np.float64)
    reference = (0.1 * rng.standard_normal((2, 44100))).astype(np.float64)
    seen = []
    real_shelf = original.low_shelf_tighten
    real_eq = original.apply_eq_style

    def shelf(audio, sample_rate, **kwargs):
        seen.append(('low_shelf_tighten', sample_rate))
        return real_shelf(audio, sample_rate, **kwargs)

    def eq(mid, side, sample_rate, eq_style):
        seen.append(('apply_eq_style', sample_rate))
        return real_eq(mid, side, sample_rate, eq_style)

    original.low_shelf_tighten = shelf
    original.apply_eq_style = eq
    try:
        original.process_audio(target.copy(), reference.copy(), 2, cfg)
    finally:
        original.low_shelf_tighten = real_shelf
        original.apply_eq_style = real_eq
    oversampled = cfg.internal_sample_rate * cfg.oversampling_factor
    assert ('low_shelf_tighten', oversampled) in seen
    assert ('apply_eq_style', oversampled) in seen
    # 修复前两个调用都拿到 44100
    assert all(sr_ == oversampled for _, sr_ in seen)


# ── 修复 3：frequency_dependent_mix 在频域、时不变 ────────────────────────

def test_frequency_matching_does_not_depend_on_time_position(original):
    """修复前：mix 曲线（频率网格）被套在时间轴上做 (1-mix)*dry + mix*fir，
    母带强度随声音出现在歌曲中的位置变化——同一脉冲在不同位置的输出波形不同。
    修复后：mix 作用于频域 matching 曲线（FIR 之前），输出是纯 LTI 卷积，
    同一脉冲在任何位置得到逐样本相同的响应。"""
    cfg = original.Config()
    rng = np.random.default_rng(9)
    sr = 44100
    n = 12 * sr
    # 周期地板（6s 铺两遍）：保证两个脉冲窗口的输入内容逐样本一致，
    # LTI 输出必须一致；差异只能来自随时间变化的处理（即被修复的 bug）。
    period = 6 * sr
    floor_tile = 0.002 * rng.standard_normal(period)
    floor = np.tile(floor_tile, 2)
    pulse_len = 8192
    tp = np.arange(pulse_len) / sr
    pulse = (0.05 * np.sin(2 * np.pi * 1000 * tp)
             + 0.03 * np.sin(2 * np.pi * 3500 * tp)) * signal.windows.hann(pulse_len)
    t1, t2 = 3 * sr, 9 * sr
    mid = floor.copy(); side = floor.copy()
    mid[t1:t1 + pulse_len] += pulse;  side[t1:t1 + pulse_len] += 0.4 * pulse
    mid[t2:t2 + pulse_len] += pulse;  side[t2:t2 + pulse_len] += 0.4 * pulse
    ref_mid = 0.1 * rng.standard_normal(n)
    ref_side = 0.05 * rng.standard_normal(n)

    out_mid, out_side = original.match_frequencies_ms(mid, side, ref_mid, ref_side, cfg)

    for out, name in ((out_mid, 'mid'), (out_side, 'side')):
        seg1 = out[t1:t1 + pulse_len]
        seg2 = out[t2:t2 + pulse_len]
        peak = np.max(np.abs(out)) + 1e-12
        err = np.max(np.abs(seg1 - seg2)) / peak
        assert err < 1e-9, f"{name}: 同一脉冲在不同位置输出不同（err={err:.2e}），mix 仍作用于时间轴"


# ── 修复 4：reshape 只保护新增（delta）side 的低频 ─────────────────────────

def _pack(mid, side):
    return np.column_stack((mid + side, mid - side))


def test_delta_protect_attenuates_only_low_side_delta(reshape):
    sr = 44100
    n = sr
    t = np.arange(n) / sr
    d_mid = 0.01 * np.sin(2 * np.pi * 1000 * t)
    d_side_low = 0.05 * np.sin(2 * np.pi * 60 * t)
    d_side_high = 0.05 * np.sin(2 * np.pi * 5000 * t)
    delta = _pack(d_mid, d_side_low + d_side_high)

    protected = reshape._protect_widen_delta(delta, sr)

    # mid 增量逐样本原样
    mid_in = delta.mean(axis=1)
    mid_out = protected.mean(axis=1)
    np.testing.assert_allclose(mid_out, mid_in, rtol=0, atol=1e-12)
    side_in = (delta[:, 0] - delta[:, 1]) / 2
    side_out = (protected[:, 0] - protected[:, 1]) / 2
    low_gain_db = 20 * np.log10(_tone_amplitude(side_out, 60, sr)
                                / _tone_amplitude(side_in, 60, sr))
    high_gain_db = 20 * np.log10(_tone_amplitude(side_out, 5000, sr)
                                 / _tone_amplitude(side_in, 5000, sr))
    # 保守低频保护：60Hz 约 -1.2dB，5kHz 基本不变
    assert -1.8 < low_gain_db < -0.7
    assert high_gain_db == pytest.approx(0.0, abs=0.05)
    assert high_gain_db == pytest.approx(0.0, abs=0.05)


def test_delta_protect_zero_delta_identity(reshape):
    delta = np.zeros((4410, 2))
    out = reshape._protect_widen_delta(delta, 44100)
    np.testing.assert_array_equal(out, delta)


def test_widen_protect_reduces_low_band_side_growth_only(reshape):
    """端到端语义：低频段 side 增长被压制，中高频段增长与不加保护一致，
    纯 reshape 路径的 mid 完全不变（原始 side 也不进 delta 通路）。"""
    sr = 44100
    n = sr * 2
    t = np.arange(n) / sr
    mid = 0.2 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 60 * t)
    side = 0.03 * np.sin(2 * np.pi * 60 * t) + 0.04 * np.sin(2 * np.pi * 1500 * t)
    stem = _pack(mid, side)

    reshaped = reshape._reshape_stem(stem, sr, 0.0, 3500.0, 6.0)  # broadband +6dB side
    wet = 0.8
    delta = (reshaped - stem) * wet
    assert np.allclose((delta[:, 0] + delta[:, 1]) / 2, 0.0, atol=1e-12)  # 纯 side 增量

    protected = reshape._protect_widen_delta(delta, sr)
    out_raw = stem + delta
    out_pro = stem + protected

    def band_side_growth(a, b, lo, hi):
        def side_e(x):
            hp = signal.sosfilt(signal.butter(2, lo, 'highpass', fs=sr, output='sos'), x, axis=0)
            bp = signal.sosfilt(signal.butter(2, hi, 'lowpass', fs=sr, output='sos'), hp, axis=0)
            m = bp.mean(axis=1)
            s = (bp[:, 0] - bp[:, 1]) / 2
            return (s ** 2).mean()
        return 10 * np.log10(side_e(b) / side_e(a))

    raw_low = band_side_growth(stem, out_raw, 20, 120)
    pro_low = band_side_growth(stem, out_pro, 20, 120)
    assert raw_low - pro_low > 0.3          # 低频拓宽增量只做保守压制
    assert pro_low < raw_low                # 低频增长严格小于不保护的情况

    raw_mid = band_side_growth(stem, out_raw, 1000, 8000)
    pro_mid = band_side_growth(stem, out_pro, 1000, 8000)
    assert abs(pro_mid - raw_mid) < 0.2     # 中高频拓宽行为不变

    # mid 通道与原始 stem 完全一致（mid 增量为 0，原始 side 未被触碰）
    np.testing.assert_allclose(out_pro.mean(axis=1), stem.mean(axis=1),
                               rtol=0, atol=1e-12)


def test_reshape_wet_zero_is_exact_passthrough(reshape):
    sr = 44100
    n = sr // 4
    t = np.arange(n) / sr
    stem = _pack(0.2 * np.sin(2 * np.pi * 220 * t), 0.03 * np.sin(2 * np.pi * 60 * t))
    reshaped = reshape._reshape_stem(stem, sr, 0.0, 3500.0, 6.0)
    delta = (reshaped - stem) * 0.0
    delta = reshape._protect_widen_delta(delta, sr)
    out = stem + delta
    np.testing.assert_array_equal(out, stem)
