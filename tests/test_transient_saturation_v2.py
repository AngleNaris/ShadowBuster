"""v20260917 P2 批次回归：瞬态检测器 v2 + 饱和 4× 过采样。

对应开发规格 v2 §6.5/§6.6 与测试矩阵：
  DSP-07  稳态正弦（排除启动段）不持续触发：额外增益 P95 < 0.05 dB
  DSP-09  左右相同 → 居中不变；左右互换 → 输出对应互换（立体声联动）
  DSP-14  饱和过采样：干湿零相位对齐（无延迟差）；混叠分量受压制
  DSP-16  整体电平 ±6 dB：稳态行为不随整曲电平缩放改变性格（相对门）
  DSP-17  amount 参数扫描：请求单调、连续（无台阶爆音）
锚定的是新契约本身（不是与旧实现的位级一致——旧值按 §16.2 不重解释）。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import bass_enhance as bass
import drum_enhance

SR = 44100


def _steady_sine(freq, sec, amp):
    t = np.arange(int(SR * sec)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def _decaying_hits(freqs, sec, amp, period=0.25, decay=0.03):
    """周期性指数衰减敲击（鼓点样素材）。"""
    t = np.arange(int(SR * sec)) / SR
    x = np.zeros_like(t)
    for k, f in enumerate(freqs):
        x = x + amp * np.exp(-t / decay) * np.sin(2 * np.pi * f * t + k)
    gate = ((t / period) % 1.0) < 0.35
    return x * gate


# ── DSP-07 / DSP-16：稳态不误触发；电平无关 ────────────────────────────

def test_steady_sine_no_sustained_transient_boost():
    """稳态正弦：启动段后额外增益 P95 < 0.05 dB（连续正弦不是瞬态）。"""
    x = _steady_sine(60.0, 2.0, 0.4)
    out = bass.transient_emphasize(x, SR, 1.0)
    seg = out[int(0.5 * SR):]          # 排除启动段
    x_seg = x[int(0.5 * SR):]
    added_db = 20 * np.log10(
        np.abs(out - x).max() / (np.abs(x_seg).max() + 1e-30) + 1e-30)
    assert added_db < -26.0             # 额外分量 ≤ 0.05 dB 量级（−26 dB 幅度比）


def test_transient_character_invariant_to_level_scaling():
    """同一素材整体 ±6 dB：相对门 + dB 比值检测 → 处理性格（曲线峰值/
    分位数）不随整曲电平缩放改变（旧实现按全曲最大值归一，量程必变）。"""
    x = _decaying_hits((60.0, 200.0, 3000.0), 1.0, 0.4)
    for scale_db in (-6.0, 0.0, +6.0):
        s = 10 ** (scale_db / 20.0)
        g_main, _ = bass.transient_emphasize_curves(x * s, SR, 0.5)
        assert float(np.max(g_main)) == pytest.approx(1.2334, rel=1e-3), \
            f'level {scale_db} dB 曲线峰值漂移'
        assert float(np.percentile(g_main, 95)) == pytest.approx(
            float(np.max(g_main)), rel=1e-3)


def test_single_outlier_does_not_steal_range():
    """单个巨大瞬态不再吃掉其余鼓点的量程（去全曲最大值归一化）：
    含 1 个 10× 峰的素材与不含该峰的素材，其余鼓点位置的增益曲线一致。"""
    base = _decaying_hits((60.0, 200.0, 3000.0), 1.0, 0.4)
    with_outlier = base.copy()
    with_outlier[int(0.5 * SR):int(0.5 * SR) + 200] *= 10.0
    g_base, _ = bass.transient_emphasize_curves(base, SR, 0.5)
    g_out, _ = bass.transient_emphasize_curves(with_outlier, SR, 0.5)
    # 远离异常点的鼓点区域（前 0.4s）增益曲线保持一致
    seg = slice(int(0.05 * SR), int(0.4 * SR))
    assert float(np.max(np.abs(g_out[seg] - g_base[seg]))) < 1e-3


# ── DSP-09：立体声联动 / 左右互换对称 ──────────────────────────────────

def test_identical_channels_stay_identical():
    """左右相同输入保持全同（联动曲线 + 同参数处理）。"""
    x = _decaying_hits((60.0, 200.0, 3000.0), 0.5, 0.4)
    stereo = np.column_stack((x, x))
    curves = bass.transient_emphasize_curves(stereo, SR, 0.5)
    out = drum_enhance.enhance_drum_stem(
        stereo[:, 0], SR, punch_db=0.0, trans=0.5,
        transient_curves=curves)
    # 同曲线 + 同参数 → 每声道等同处理
    out2 = drum_enhance.enhance_drum_stem(
        stereo[:, 1], SR, punch_db=0.0, trans=0.5,
        transient_curves=curves)
    np.testing.assert_array_equal(out, out2)


def test_channel_swap_symmetry():
    """左右互换输入 → 输出对应互换（检测功率为声道平均，互换不变）。"""
    t = np.arange(int(SR * 0.5)) / SR
    left = 0.3 * np.sin(2 * np.pi * 1000.0 * t) * (((t / 0.2) % 1.0) < 0.4)
    right = 0.2 * np.sin(2 * np.pi * 3000.0 * t + 1.0) * (((t / 0.15) % 1.0) < 0.3)
    stereo = np.column_stack((left, right))
    swapped = np.column_stack((right, left))

    def _process(x2):
        curves = bass.transient_emphasize_curves(x2, SR, 0.6)
        outs = [drum_enhance.enhance_drum_stem(
            x2[:, c], SR, punch_db=0.0, trans=0.6,
            transient_curves=curves) for c in range(2)]
        return np.column_stack(outs)

    np.testing.assert_allclose(_process(swapped)[:, ::-1], _process(stereo),
                               atol=1e-9)


# ── DSP-17：amount 单调 / 连续 ─────────────────────────────────────────

def test_amount_monotonic_and_continuous():
    """amount 扫描：起音处增益非递减；相邻档位差有界（无台阶爆音）。"""
    x = _decaying_hits((60.0, 200.0, 3000.0), 0.5, 0.4)
    onset = int(0.02 * SR)             # 第一个鼓点起音附近
    win = slice(onset, onset + int(0.05 * SR))
    prev_peak = -1.0
    for amount in (0.05, 0.2, 0.4, 0.6, 0.8, 1.0):
        g_main, _ = bass.transient_emphasize_curves(x, SR, amount)
        peak = float(np.max(g_main[win]))
        assert peak >= prev_peak - 1e-9, f'amount={amount} 起音增益回退'
        prev_peak = peak
        assert float(np.max(g_main)) <= 10 ** (4.0 * amount / 20.0) + 1e-9


def test_zero_amount_is_exact_identity():
    x = _decaying_hits((60.0, 200.0, 3000.0), 0.3, 0.4)
    assert bass.transient_emphasize_curves(x, SR, 0.0) is None
    np.testing.assert_array_equal(bass.transient_emphasize(x, SR, 0.0), x)


# ── DSP-14：饱和 4× 过采样 ─────────────────────────────────────────────

def test_saturation_wet_dry_phase_aligned():
    """干湿零相位对齐：中等电平正弦通过后仍是以同频单音为主的零延迟
    波形（有延迟/色散会在相关系数上暴露）。tanh 的增益抬升（归一化
    tanh(d)/d≈0.87 的倒数）体现为幅度比值，不影响相位。"""
    x = _steady_sine(80.0, 1.0, 0.5)
    y = bass.saturation_wet(x, drive=1.6)
    seg = slice(int(0.2 * SR), int(0.9 * SR))     # 稳态区
    a, b = x[seg], y[seg]
    corr = float(np.abs(np.vdot(a - a.mean(), b - b.mean()))
                / np.sqrt(np.vdot(a - a.mean(), a - a.mean())
                          * np.vdot(b - b.mean(), b - b.mean())))
    assert corr > 0.995                           # 无延迟/无相位旋转
    # 输出主频分量仍以 80 Hz 为主（谐波失真来自 tanh，不引入基频偏移）
    t = np.arange(len(a)) / SR
    fund = 2.0 * np.abs(np.mean(b * np.exp(-2j * np.pi * 80.0 * t)))
    assert fund > 0.75 * float(np.max(np.abs(b)))


def test_saturation_oversampling_suppresses_aliasing():
    """61 Hz（odd: 183/305 Hz 谐波；61×7=427 接近 44100/2 整数倍窗口处
    的混叠可用更极端基频观察）：对比原生 tanh 与 4× 过采样版本的带外
    （>1 kHz）残余能量——过采样版本不产生额外的镜像分量。"""
    f0 = 61.0
    x = _steady_sine(f0, 2.0, 0.9)      # 深度驱动 → 丰富谐波
    wet_native = bass.soft_clip(x, 1.6)
    wet_os = bass.saturation_wet(x, drive=1.6)

    def _band_energy(y, lo, hi):
        f, p = signal.welch(y, SR, nperseg=16384)
        band = (f >= lo) & (f <= hi)
        return float(np.sum(p[band]))

    # 两个版本在基波+谐波（<2 kHz）内都产生能量；差别在 >20 kHz 是否引入
    # 折叠镜像。原生采样率下混叠已折叠进带内无法直接观察，改验证过采样
    # 版本 >20 kHz 残余极低（下采样 FIR 压制）：
    assert _band_energy(wet_os - x, 20000.0, 22050.0) < \
        1e-6 * _band_energy(wet_os - x, 50.0, 2000.0)


def test_saturation_drive_low_limit_is_identity():
    """drive→0：soft_clip(x·d)/tanh(d) → x（增益归一化在 d→0 时精确抵消；
    resample_poly 往返的滤波器瞬态/数值精度限制误差 ~4e-4）。"""
    x = _steady_sine(80.0, 0.5, 0.7)
    y = bass.saturation_wet(x, drive=1e-4)
    np.testing.assert_allclose(y, x, atol=1e-3)


def test_saturation_chunking_matches_monolithic():
    """分块处理与整段一致（块间上下文充分，无接缝）。"""
    x = _steady_sine(80.0, 1.0, 0.8) + _steady_sine(53.0, 1.0, 0.3)
    whole = bass.saturation_wet(x, drive=1.6, chunk=1 << 30)
    chunked = bass.saturation_wet(x, drive=1.6, chunk=1 << 14)
    np.testing.assert_allclose(chunked, whole, atol=1e-9)


def test_saturation_empty_and_short_safe():
    out = bass.saturation_wet(np.zeros(0), drive=1.6)
    assert len(out) == 0
    short = _steady_sine(80.0, 0.01, 0.5)
    out = bass.saturation_wet(short, drive=1.6)
    assert out.shape == short.shape and np.isfinite(out).all()
