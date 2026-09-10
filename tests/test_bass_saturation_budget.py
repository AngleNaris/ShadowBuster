"""bass_enhance v20260909 饱和低中频预算的回归测试。

锚定四条硬保证：
  1. sat=0 或因子 g=1.0 时与旧版（v20260908 饱和路径）位级一致；
  2. 预算只衰减 sat 新增的 180-500Hz 增量；带通非 brickwall，<120Hz 按实测
     容差断言（只允许裙边泄漏量级改变）；
  3. 原始低中频越浓，允许新增越少（密集 stem 新增量严格减少，上限 -3dB，
     非硬预算）；
  4. 立体声联动同一控制因子；静音/过短/非有限输入安全回退 g=1.0；
     sat_budget_gain 须 finite 且在 [10^(-3/20), 1]。
物理口径：sosfiltfilt 等效幅频 = |H(f)|^2（不做 +3dB 补偿）；tanh 奇对称，
单正弦主要产生奇次谐波（2 次谐波应远小于 3 次谐波）。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import bass_enhance as bass

SR = 44100
G_MAX = 10 ** (-3.0 / 20.0)  # ≈ 0.7079，-3dB 上限


def sine(freq, sec, amp):
    t = np.arange(int(SR * sec)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def legacy_enhance(x, sr, sub_db, punch_db, sat, trans, drive=1.6):
    """v20260908 旧饱和路径的位级参考（sat=0 / g=1.0 的回归锚点）。"""
    x = np.asarray(x, dtype=np.float64)
    x_warm = bass._shelf(x, sr, 30.0, sub_db)
    x_warm = bass._bell(x_warm, sr, 90.0, punch_db, q=1.2)
    lp = signal.sosfiltfilt(
        signal.butter(2, 200.0, 'lowpass', fs=sr, output='sos'), x_warm, padlen=0)
    x_sat = bass.soft_clip(lp, drive)
    x_eff = (1.0 - sat) * lp + sat * x_sat
    x_eff = x_eff + (x_warm - lp)
    x_eff = bass._transient(x_eff, sr, trans * 0.8)
    win = min(int(sr * 0.05), len(x))
    env = np.sqrt(np.convolve(x ** 2, np.ones(win) / (sr * 0.05), mode='same'))
    env_db = 20 * np.log10(env + 1e-12)
    target = np.where(env_db > -45.0, 1.0, 0.0)
    a = 1.0 - np.exp(-1.0 / (sr * 40.0 / 1000.0))
    r = 1.0 - np.exp(-1.0 / (sr * 250.0 / 1000.0))
    gate = np.empty_like(target)
    acc = 0.0
    for i in range(len(target)):
        alpha = a if target[i] >= acc else r
        acc = acc + alpha * (target[i] - acc)
        gate[i] = acc
    return x + (x_eff - x) * gate


def test_sat_zero_bit_identical_to_legacy():
    """sat=0 等旧：预算机制完全不参与（含显式传入合法 g 的情况）。"""
    x = sine(60, 1.0, 0.5) + sine(90, 1.0, 0.2)
    ref = legacy_enhance(x, SR, 4.0, 2.0, 0.0, 0.3).astype(np.float32)
    for g in (None, 1.0, 0.75):
        out = bass.enhance_bass_stem(x, SR, sub_db=4.0, punch_db=2.0, sat=0.0,
                                     trans=0.3, sat_budget_gain=g)
        np.testing.assert_array_equal(out, ref)


def test_gain_one_bit_identical_to_legacy():
    """g=1.0（不触发衰减）时 sat>0 输出与旧版位级一致。"""
    x = sine(60, 1.0, 0.5) + sine(250, 1.0, 0.5)
    ref = legacy_enhance(x, SR, 4.0, 2.0, 0.3, 0.3).astype(np.float32)
    out = bass.enhance_bass_stem(x, SR, sub_db=4.0, punch_db=2.0, sat=0.3,
                                 trans=0.3, sat_budget_gain=1.0)
    np.testing.assert_array_equal(out, ref)


def test_neutral_identity():
    x = sine(60, 0.5, 0.4) + sine(250, 0.5, 0.3)
    np.testing.assert_array_equal(
        bass.enhance_bass_stem(x, SR, 0, 0, 0, 0), x.astype(np.float32))
    x32 = x.astype(np.float32)
    np.testing.assert_array_equal(
        bass.enhance_bass_stem(x32, SR, 0, 0, 0, 0), x32)


def test_odd_harmonic_physics():
    """tanh 奇对称：单正弦新增量 3 次谐波 >> 2/4 次谐波（180-500Hz 预算带依据）。"""
    x = sine(60, 1.0, 0.5)
    lp = bass._lp200(bass._warm_stage(x, SR, 4.0, 2.0), SR)
    d = 0.3 * (bass.soft_clip(lp, 1.6) - lp)
    seg, n = d[SR // 4: 3 * SR // 4], SR // 2
    t = np.arange(n) / SR

    def amp(freq):
        return 2.0 * np.abs(np.mean(seg * np.exp(-2j * np.pi * freq * t)))

    assert amp(180.0) > 20.0 * amp(120.0)  # 3 次 >> 2 次（偶次仅数值残差）
    assert amp(180.0) > 20.0 * amp(240.0)  # 4 次同理


def test_sparse_lowmid_no_trim():
    """纯 sub stem：低中频稀疏 → 不触发衰减（g=1，保持 sat 原意图）。"""
    g, st = bass.compute_sat_budget_gain(sine(60, 1.0, 0.5), SR,
                                         sub_db=4.0, punch_db=2.0, sat=0.3,
                                         return_stats=True)
    assert g == 1.0
    assert st['trim_db'] == 0.0 and st['a_allow'] > st['a_gen']


def test_dense_lowmid_trims_more_and_caps():
    """已有低中频越浓 → 允许新增越少；极端密集打到 -3dB 上限（非硬预算）。"""
    sparse = sine(60, 1.0, 0.5)
    mid = sparse + sine(250, 1.0, 0.35)
    dense = sparse + sine(250, 1.0, 0.5)
    g_mid, st_mid = bass.compute_sat_budget_gain(mid, SR, sat=0.3, return_stats=True)
    g_dense, st_dense = bass.compute_sat_budget_gain(dense, SR, sat=0.3, return_stats=True)
    assert st_mid['a_allow'] > st_dense['a_allow']  # 密集 → 预算收紧
    assert 1.0 >= g_mid >= g_dense
    assert g_dense == pytest.approx(G_MAX, abs=1e-6)   # 上限 -3dB
    assert st_dense['trim_db'] == pytest.approx(3.0)
    assert st_dense['a_allow'] == pytest.approx(0.0, abs=1e-12)


def test_dense_addition_reduced_and_sub_kept():
    """密集 stem 全链路：新增 180-500Hz 严格减少；<120Hz 按实测容差断言
    （带通非 brickwall，只允许裙边泄漏量级改变）。"""
    x = sine(60, 1.5, 0.5) + sine(250, 1.5, 0.5)
    g = bass.compute_sat_budget_gain(x, SR, sub_db=4.0, punch_db=2.0, sat=0.3)
    assert g < 1.0
    kw = dict(sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.0)
    out_trim = bass.enhance_bass_stem(x, SR, sat_budget_gain=g, **kw).astype(np.float64)
    out_full = bass.enhance_bass_stem(x, SR, sat_budget_gain=1.0, **kw).astype(np.float64)
    lo, hi = int(0.15 * SR), int(1.35 * SR)  # 避开 filtfilt / gate 边缘
    added_trim = out_trim[lo:hi] - x[lo:hi]
    added_full = out_full[lo:hi] - x[lo:hi]
    band_trim = bass._band_mean_square(added_trim, SR, 180, 500)
    band_full = bass._band_mean_square(added_full, SR, 180, 500)
    assert 0.0 < band_trim < band_full * 0.98  # 严格下降（增量带内约 -3dB）
    # 两次运行之差 = 被衰减的带内增量，应集中在预算带内
    d_sig = out_full[lo:hi] - out_trim[lo:hi]
    d_in = bass._band_mean_square(d_sig, SR, 200, 480)
    d_low = bass._band_mean_square(d_sig, SR, 30, 90)
    assert d_in > 0.0 and d_low < 0.05 * d_in
    # <120Hz sub 意图保持（30-80Hz 能量变化在数值残差量级）
    sub_trim = bass._band_mean_square(out_trim[lo:hi], SR, 30, 80)
    sub_full = bass._band_mean_square(out_full[lo:hi], SR, 30, 80)
    assert sub_trim > sub_full * 0.999


def test_added_lowmid_monotonic_in_sat():
    """sat 旋钮在可控范围内保持单调：新增低中频能量随 sat 非递减。"""
    x = sine(60, 1.5, 0.5) + sine(250, 1.5, 0.5)
    lo, hi = int(0.15 * SR), int(1.35 * SR)
    prev = -1.0
    for s in (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0):
        out = bass.enhance_bass_stem(x, SR, sub_db=4.0, punch_db=2.0,
                                     sat=s, trans=0.0).astype(np.float64)
        added = bass._band_mean_square(out[lo:hi] - x[lo:hi], SR, 180, 500)
        assert added >= prev * (1.0 - 1e-3), f'sat={s} 新增低中频出现回退'
        prev = added


def test_stereo_same_control():
    """立体声联动：全曲一个标量因子，介于两通道各自结果之间；单声道函数
    可显式传入同一因子共用。"""
    left = sine(60, 1.0, 0.5)                         # 稀疏 → auto g=1
    right = sine(60, 1.0, 0.5) + sine(250, 1.0, 0.5)  # 密集 → auto g<1
    g_shared, st = bass.compute_sat_budget_gain(
        np.column_stack((left, right)), SR, sat=0.3, return_stats=True)
    g_left, _ = bass.compute_sat_budget_gain(left, SR, sat=0.3, return_stats=True)
    g_right, _ = bass.compute_sat_budget_gain(right, SR, sat=0.3, return_stats=True)
    assert isinstance(g_shared, float)
    assert st['channels'] == 2
    assert g_left == 1.0 and g_right < 1.0
    assert g_left >= g_shared >= g_right  # 联动单值，不做 per-channel 独立控制
    kw = dict(sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.0)
    out_l = bass.enhance_bass_stem(left, SR, sat_budget_gain=g_shared, **kw)
    out_r = bass.enhance_bass_stem(right, SR, sat_budget_gain=g_shared, **kw)
    assert out_l.shape == left.shape and out_r.shape == right.shape
    assert np.isfinite(out_l).all() and np.isfinite(out_r).all()
    # mono 直接调用不传因子（auto）等价于显式传入其自身计算出的因子
    np.testing.assert_array_equal(
        bass.enhance_bass_stem(right, SR, **kw),
        bass.enhance_bass_stem(right, SR, sat_budget_gain=g_right, **kw))


def test_budget_gain_validation():
    """sat_budget_gain 越界截断到 [10^(-3/20), 1]，非有限抛 ValueError。"""
    x = sine(60, 1.0, 0.5) + sine(250, 1.0, 0.5)
    kw = dict(sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.0)
    out_clamped = bass.enhance_bass_stem(x, SR, sat_budget_gain=0.5, **kw)
    out_min = bass.enhance_bass_stem(x, SR, sat_budget_gain=G_MAX, **kw)
    np.testing.assert_array_equal(out_clamped, out_min)
    with pytest.raises(ValueError):
        bass.enhance_bass_stem(x, SR, sat_budget_gain=float('nan'), **kw)


def test_silence_short_and_finite():
    """静音/过短/非有限：预算安全回退 g=1.0，输出有限、不崩溃。"""
    silence = np.zeros(SR)
    g, _ = bass.compute_sat_budget_gain(silence, SR, sat=0.3, return_stats=True)
    assert g == 1.0
    out = bass.enhance_bass_stem(silence, SR, sub_db=4.0, punch_db=2.0,
                                 sat=0.3, trans=0.0)
    assert out.shape == silence.shape
    assert np.isfinite(out).all() and float(np.abs(out).max()) == 0.0

    for sec in (0.05, 0.01):  # 0.01s 短于 RMS 窗（0.05s），需截短卷积核
        short = sine(60, sec, 0.5)
        g_short, _ = bass.compute_sat_budget_gain(short, SR, sat=0.3, return_stats=True)
        assert g_short == 1.0
        out_short = bass.enhance_bass_stem(short, SR, sub_db=4.0, punch_db=2.0,
                                           sat=0.3, trans=0.0)
        assert out_short.shape == short.shape and np.isfinite(out_short).all()

    assert bass.compute_sat_budget_gain(np.full(SR, np.nan), SR, sat=0.3) == 1.0
    assert bass.compute_sat_budget_gain(sine(60, 1.0, 0.5), SR, sat=0.0) == 1.0
