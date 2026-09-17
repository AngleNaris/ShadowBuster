"""人声有界压缩与空气 EQ（vocal_adjust.apply_vocal_compression / apply_vocal_air）。

锚定契约：
  1. 压缩是宽带联动增益：不改变频谱（空气感不受频谱损失）；GR 有硬上限
     4.5×amount；包络有限斜率；amount=0 精确恒等；静音直接跳过；
  2. 响的窗口被压、安静窗口几乎不动（阈值取活动窗口 P55）；
  3. 空气 EQ 只加不削：高频能量只增不减；air_db=0 精确恒等；
  4. 主流程集成：comp/air 关闭时输出与旧实现逐位一致；开启时 user=0 仍生效。
"""
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import vocal_adjust as va  # noqa: E402

SR = 44100


def _vocal_phrase(seconds=8.0):
    """连续起伏的人声式乐句：电平平滑涨落，响段应被压、轻段几乎不动。"""
    t = np.arange(int(SR * seconds)) / SR
    tone = np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 550 * t)
    amp = 0.04 + 0.26 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.25 * t)) ** 2
    return (amp * tone / np.max(np.abs(amp * tone)) * 0.25)[:, None] * np.ones((1, 2))


def _band_energy(x, lo, hi):
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=SR, output="sos")
    return float(np.sum(signal.sosfilt(sos, x, axis=0) ** 2))


def test_compression_zero_amount_is_exact():
    v = _vocal_phrase()
    out = va.apply_vocal_compression(v, SR, 0.0)
    np.testing.assert_array_equal(out, v)


def test_compression_bounds_and_shape():
    v = _vocal_phrase()
    report = {}
    out = va.apply_vocal_compression(v, SR, 1.0, report=report)
    assert report["applied"] and report["gr_max_db"] <= 4.5 + 1e-9
    # 宽带联动增益不改频谱：两个有真实内容的频段能量比保持不变
    before = _band_energy(v, 150, 350) / _band_energy(v, 450, 700)
    after = _band_energy(out, 150, 350) / _band_energy(out, 450, 700)
    assert abs(after / before - 1) < 0.02


def test_compression_targets_loud_windows():
    v = _vocal_phrase()
    out = va.apply_vocal_compression(v, SR, 1.0)
    rms = np.sqrt(np.mean(v ** 2, axis=1))
    loud = rms > np.percentile(rms, 90)     # 最响的窗口
    quiet = rms < np.percentile(rms, 10)    # 最轻的窗口
    loud_drop = np.sqrt(np.mean(out[loud] ** 2)) / np.sqrt(np.mean(v[loud] ** 2))
    quiet_drop = np.sqrt(np.mean(out[quiet] ** 2)) / np.sqrt(np.mean(v[quiet] ** 2))
    assert loud_drop < 0.85           # 响段明显衰减
    assert quiet_drop > 0.9           # 轻段基本不动（释放尾巴允许 <1dB 残余）


def test_compression_silence_skips():
    silence = np.zeros((SR, 2))
    report = {}
    out = va.apply_vocal_compression(silence, SR, 0.8, report=report)
    np.testing.assert_array_equal(out, silence)
    assert report["applied"] is False


def test_air_eq_boost_only_and_exact_at_zero():
    # 含 200Hz 真实成分：低频"保持不动"的断言才有信号可测（纯噪声底比值无意义）
    t = np.arange(SR) / SR
    v = ((0.1 * np.sin(2 * np.pi * 12000 * t)
          + 0.2 * np.sin(2 * np.pi * 200 * t))[:, None]) * np.ones((1, 2))
    out0 = va.apply_vocal_air(v, SR, 0.0)
    np.testing.assert_array_equal(out0, v)
    out = va.apply_vocal_air(v, SR, 1.5)
    assert _band_energy(out, 10000, 16000) > _band_energy(v, 10000, 16000)
    low_before = 10 * np.log10(_band_energy(v, 100, 500))
    low_after = 10 * np.log10(_band_energy(out, 100, 500))
    assert abs(low_after - low_before) < 0.5   # 低频保持（±0.5dB）


def test_full_control_comp_air_change_output_at_zero_user_gain():
    """comp/air 开启时，即使人声旋钮为 0 也不再是纯透传（有真实处理发生）。"""
    v = _vocal_phrase()
    mix = np.zeros_like(v)
    out_off, _ = va.apply_mid_prominence_control(mix, v, SR, 0.0, return_report=True)
    np.testing.assert_array_equal(out_off, mix)          # 关闭：位级透传
    out_on, report = va.apply_mid_prominence_control(
        mix, v, SR, 0.0, return_report=True, comp_amount=0.5, air_db=1.5)
    assert not np.array_equal(out_on, mix)
    assert report["comp"]["applied"] and report["air"]["applied"]
    assert report["bypass"] is False


def test_invalid_params_rejected():
    v = _vocal_phrase(0.5)
    with pytest.raises(ValueError):
        va.apply_vocal_compression(v, SR, 1.5)
    with pytest.raises(ValueError):
        va.apply_vocal_air(v, SR, -0.5)
