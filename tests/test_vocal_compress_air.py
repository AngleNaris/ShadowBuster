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
        va.apply_vocal_deesser(v, SR, 1.5)
    with pytest.raises(ValueError):
        va.apply_vocal_defizz(v, SR, 1.5)
    with pytest.raises(ValueError):
        va.apply_vocal_air(v, SR, -0.5)


def test_deesser_targets_sibilance_only_and_keeps_vowels_and_image():
    """分频 De-Esser 只压 5–9.5 kHz 齿音突发，不伤正常元音、保持左右立体声比例与 0 档恒等。"""
    t = np.arange(int(SR * 2.0)) / SR
    vowel = 0.25 * (np.sin(2 * np.pi * 400 * t) + 0.4 * np.sin(2 * np.pi * 1200 * t))
    sib_gate = ((t >= 0.8) & (t <= 1.2)).astype(np.float64)
    rng = np.random.default_rng(11)
    sib_sos = signal.butter(4, [6000.0, 8500.0], btype="bandpass", fs=SR, output="sos")
    sib_noise = signal.sosfiltfilt(sib_sos, rng.standard_normal(len(t)))
    sib_noise = sib_noise / np.max(np.abs(sib_noise)) * 0.35 * sib_gate
    # 元音段与齿音段错开，模拟真实 "ah — sss — ah"
    mono = vowel * (1.0 - 0.85 * sib_gate) + sib_noise
    stereo = np.column_stack((mono, mono * 0.6))

    np.testing.assert_array_equal(va.apply_vocal_deesser(stereo, SR, 0.0), stereo)

    report = {}
    out = va.apply_vocal_deesser(stereo, SR, 1.0, report=report)
    assert report["applied"] and report["gr_max_db"] <= 4.0 + 1e-9
    # 立体声通道比例严格保持（功率域单条联动曲线）
    np.testing.assert_allclose(out[:, 1], out[:, 0] * 0.6, atol=1e-12)
    # 齿音段 5-9.5kHz 明显衰减
    sib_slice = slice(int(0.85 * SR), int(1.15 * SR))
    sib_before = _band_energy(stereo[sib_slice], 5500, 9000)
    sib_after = _band_energy(out[sib_slice], 5500, 9000)
    assert 10 * np.log10(sib_after / sib_before) < -2.0
    # 纯元音段（0.1-0.6s）基本不动
    vowel_slice = slice(int(0.1 * SR), int(0.6 * SR))
    np.testing.assert_allclose(out[vowel_slice], stereo[vowel_slice], atol=1e-12)


def test_defizz_flattens_ai_high_hump_and_preserves_low_mid_and_harmonics():
    """De-Fizz 仅压制 ≥5kHz 高平坦度嘶声（尤其 8.5-11.5kHz 异常隆起）与非相干 Side 电噪，
    保持 <4.5kHz 主体不动、纯净高次谐波受保护、左右互换对称、0 档位级恒等。"""
    t = np.arange(int(SR * 2.0)) / SR
    rng = np.random.default_rng(23)
    # 清晰人声主体（300Hz + 1200Hz）+ 6kHz 纯净谐波线 + 8.5-11.5kHz 独立双声道白噪电噪隆起
    body = 0.25 * (np.sin(2 * np.pi * 300 * t) + 0.5 * np.sin(2 * np.pi * 1200 * t))
    harmonic_6k = 0.05 * np.sin(2 * np.pi * 6000 * t)
    hump_sos = signal.butter(4, [8500.0, 11500.0], btype="bandpass", fs=SR, output="sos")
    fizz_l = signal.sosfiltfilt(hump_sos, rng.standard_normal(len(t)))
    fizz_r = signal.sosfiltfilt(hump_sos, rng.standard_normal(len(t)))
    fizz_l = fizz_l / np.max(np.abs(fizz_l)) * 0.12
    fizz_r = fizz_r / np.max(np.abs(fizz_r)) * 0.12
    stereo = np.column_stack((body + harmonic_6k + fizz_l, body + harmonic_6k + fizz_r))

    np.testing.assert_array_equal(va.apply_vocal_defizz(stereo, SR, 0.0), stereo)

    report = {}
    out = va.apply_vocal_defizz(stereo, SR, 0.8, report=report)
    assert report["applied"]
    assert report["max_broadband_att_db"] <= 9.0 * 0.8 + 1e-6
    assert report["max_side_extra_att_db"] > 0.5

    # 8.5-11.5kHz AI 嘶声隆起显著衰减（>3 dB），且 6kHz 纯谐波受 tonal_guard 保护（损失 <0.5 dB）
    hump_drop = 10 * np.log10(_band_energy(out, 8500, 11500) / _band_energy(stereo, 8500, 11500))
    assert hump_drop < -3.0
    harm_drop = 10 * np.log10(_band_energy(out, 5800, 6200) / _band_energy(stereo, 5800, 6200))
    assert harm_drop > -0.5
    # <4kHz 人声主体几乎零变动（±0.05 dB）
    body_delta = 10 * np.log10(_band_energy(out, 200, 4000) / _band_energy(stereo, 200, 4000))
    assert abs(body_delta) < 0.05
    # 左右互换对称
    swapped = va.apply_vocal_defizz(stereo[:, ::-1], SR, 0.8)
    np.testing.assert_allclose(swapped[:, ::-1], out, atol=1e-10)
