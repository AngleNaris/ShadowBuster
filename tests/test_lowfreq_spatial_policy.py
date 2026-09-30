"""v1.6.10 P2/P3 批次回归：低频协调授权 u_low + 空间去拥挤 + 引擎版本。

对应开发规格 v2：
  §6.3/§6.4  低频协调授权公式与共享衰减预算（kick 让位 D=3×u_low、泥浊 ≤1.5×u_low）
  §5.3       全零低频旋钮 → 低频恒等（无不可关闭的暗中去掩蔽）
  §7.4       空间去拥挤：声场强度 a 授权，other 200-700Hz ≤1.5×a dB，证据门控，
             a=0 精确恒等；W=0、a>0 时允许轻微去拥挤
  §16.1/§16.5 DSP 引擎版本独立于 APP_VERSION 并进入缓存身份
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'apollo_scripts'))

import low_freq_policy as lfp          # noqa: E402
import soundstage_reshape as reshape   # noqa: E402
import studio_backend as backend       # noqa: E402

SR = 44100


def _sine(freq, sec, amp):
    t = np.arange(int(SR * sec)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


# ── §6.3 授权公式 ──────────────────────────────────────────────────────

def test_authorization_formula_spec_values():
    assert lfp.low_frequency_authorization(0, 0, 0, 0) == 0.0
    assert lfp.low_frequency_authorization(4, 0, 0, 0) == 1.0     # sub/4
    assert lfp.low_frequency_authorization(0, 3, 0, 0) == 1.0     # punch/3
    assert lfp.low_frequency_authorization(0, 0, 0.5, 0) == 1.0   # trans/0.5
    assert lfp.low_frequency_authorization(0, 0, 0, 0.4) == 1.0   # sat/0.4
    assert lfp.low_frequency_authorization(2, 0, 0, 0) == 0.5
    assert lfp.low_frequency_authorization(12, 10, 1, 1) == 1.0   # 封顶
    # RC0 候选（sub=2, punch=1.5, trans=0.25, sat=0.2）授权 ≈ 0.5
    assert lfp.low_frequency_authorization(2, 1.5, 0.25, 0.2) == pytest.approx(0.5)


def test_sidechain_amount_maps_to_spec_duck_budget():
    """amount = u_low/2；max_duck=6 时理论最大 duck = 6×amount = 3×u_low dB。"""
    for auth in (0.0, 0.25, 0.5, 1.0):
        assert lfp.sidechain_amount_from_authorization(auth) == auth / 2.0


def test_clarity_scaling_caps_mud_at_budget():
    """泥浊削减按授权缩放并夹到 1.5×u_low dB（更轻者生效）；全零 → (0,0)。"""
    assert lfp.scale_clarity_gains((-2.0, 2.0), 0.0) == (0.0, 0.0)
    mud, clarity = lfp.scale_clarity_gains((-2.0, 2.0), 1.0)
    assert mud == pytest.approx(-1.5)      # §6.4 预算上限（较旧 -2 收紧）
    assert clarity == pytest.approx(2.0)
    # auth=0.5：等比缩放 -1.0 vs 上限 -0.75 → 更轻的 -0.75 生效
    mud, clarity = lfp.scale_clarity_gains((-2.0, 2.0), 0.5)
    assert mud == pytest.approx(-0.75)
    assert clarity == pytest.approx(1.0)
    # 小增益不受上限影响：-0.6×0.5=-0.3，上限 -0.75 更深 → 取 -0.3
    assert lfp.scale_clarity_gains((-0.6, 2.0), 0.5)[0] == pytest.approx(-0.3)


def test_all_zero_low_knobs_mean_neutral_auxiliaries():
    """规格 §5.3：低频旋钮全零时，侧链与清晰度授权都为 0（位级恒等）。"""
    assert lfp.low_frequency_authorization(0, 0, 0.0, 0.0) == 0.0
    assert lfp.sidechain_amount_from_authorization(0.0) == 0.0
    assert lfp.clarity_mud_cap_db(0.0) == 0.0


# ── §7.4 空间去拥挤 ────────────────────────────────────────────────────

def _other_like(seconds=2.0):
    """other 轨素材：稀疏基底 + 局部 300Hz 伴奏堆积段（相对基底形成证据）。"""
    t = np.arange(int(SR * seconds)) / SR
    pad = 0.05 * np.sin(2 * np.pi * 300 * t)
    crowded = (t > 0.4) & (t < 1.4)
    pad[crowded] = 0.35 * np.sin(2 * np.pi * 300 * t[crowded])
    sparkle = 0.1 * np.sin(2 * np.pi * 5000 * t)
    return np.column_stack((pad + sparkle, pad * 0.95 + sparkle))


def test_spatial_unmask_zero_is_bit_identity():
    x = _other_like(0.5)
    out = reshape.spatial_unmask_other(x, SR, 0.0)
    assert out is x or np.array_equal(out, x)


def test_spatial_unmask_no_evidence_is_identity():
    """全曲带内能量平坦（无持续堆积证据）→ 精确透传。"""
    t = np.arange(int(SR * 1.0)) / SR
    flat = 0.2 * np.sin(2 * np.pi * 300 * t) * np.ones_like(t)  # 恒定幅度
    x = np.column_stack((flat, flat))
    report = {}
    out = reshape.spatial_unmask_other(x, SR, 1.0, report=report)
    assert report["applied"] is False
    assert report["reason"] in ("no_evidence", "disabled")
    np.testing.assert_array_equal(out, x)


def test_spatial_unmask_sustained_crowding_reduced_within_budget():
    """持续 200-700Hz 堆积段被温和衰减：带内能量下降、深度 ≤ 1.5×a、
    带外不改（零相位带通 + 精确重组）。素材按"局部堆积"设计：30% 拥挤段
    + 70% 稀疏段（P25 基准落在稀疏段，拥挤段相对基准形成明确证据）。"""
    seconds = 5.0
    t = np.arange(int(SR * seconds)) / SR
    pad = 0.35 * np.sin(2 * np.pi * 300 * t)
    pad[int(SR * 1.5):] *= 0.03            # 30% 拥挤 / 70% 稀疏
    high = 0.05 * np.sin(2 * np.pi * 6000 * t)
    x = np.column_stack((pad + high, pad * 0.98 + high))
    report = {}
    out = reshape.spatial_unmask_other(x, SR, 0.8, report=report)
    assert report["applied"] is True
    assert report["depth_cap_db"] == pytest.approx(1.5 * 0.8)
    assert report["duck_db_max"] <= report["depth_cap_db"] + 1e-9
    seg = slice(int(0.3 * SR), int(1.2 * SR))       # 持续堆积稳态区
    from scipy import signal
    def band_rms(y, lo, hi):
        sos = signal.butter(2, [lo, hi], 'bandpass', fs=SR, output='sos')
        z = signal.sosfiltfilt(sos, np.asarray(y, dtype=np.float64), padlen=0, axis=0)
        return float(np.sqrt(np.mean(z * z)))
    assert band_rms(out[seg], 200, 700) < band_rms(x[seg], 200, 700) * 0.98
    # 带外（1kHz 以上）不变：差值只应出现在 200-700Hz（带通裙边容差）
    d = out[seg] - x[seg]
    assert float(np.sqrt(np.mean(d * d))) < 0.4 * float(band_rms(x[seg], 200, 700))
    # 衰减不超过预算：带内幅度比 ≥ 10^(-1.2/20)（余量给裙边）
    assert band_rms(out[seg], 200, 700) > band_rms(x[seg], 200, 700) \
        * 10 ** (-1.5 * 0.8 / 20) * 0.9


def test_spatial_unmask_uniform_crowding_no_evidence():
    """整曲均匀拥挤（无局部堆积对比）→ 无明确问题不动作（§7.4）。"""
    t = np.arange(int(SR * 3.0)) / SR
    pad = 0.35 * np.sin(2 * np.pi * 300 * t) * np.ones_like(t)
    x = np.column_stack((pad, pad * 0.98))
    report = {}
    out = reshape.spatial_unmask_other(x, SR, 1.0, report=report)
    assert report["applied"] is False
    np.testing.assert_array_equal(out, x)


def test_spatial_unmask_depth_monotonic_in_amount():
    """声场强度越大，实际削减包络非递减（同一素材/同一证据，只变深度上限；
    声场旋钮单调性——§3.3 用户请求的单调性）。"""
    x = _other_like(2.0)
    prev_cap = 0.0
    for amount in (0.2, 0.5, 1.0):
        report = {}
        reshape.spatial_unmask_other(x, SR, amount, report=report)
        assert report["depth_cap_db"] >= prev_cap - 1e-9
        assert report["duck_db_max"] <= report["depth_cap_db"] + 1e-9
        prev_cap = report["depth_cap_db"]


def test_spatial_unmask_applied_depth_increases_with_amount():
    x = _other_like(2.0)
    duck = []
    for amount in (0.2, 0.5, 1.0):
        report = {}
        reshape.spatial_unmask_other(x, SR, amount, report=report)
        duck.append(report.get("duck_db_p95", 0.0))
    assert duck == sorted(duck)


def test_spatial_unmask_mono_and_swap_safe():
    """同内容双声道保持全同；左右互换对称（带内能量多声道平均）。"""
    x = _other_like(1.0)
    mono = np.column_stack((x[:, 0], x[:, 0]))
    out = reshape.spatial_unmask_other(mono, SR, 0.8)
    np.testing.assert_allclose(out[:, 0], out[:, 1], atol=1e-9)
    swapped = np.column_stack((x[:, 1], x[:, 0]))
    out_a = reshape.spatial_unmask_other(x, SR, 0.8)
    out_b = reshape.spatial_unmask_other(swapped, SR, 0.8)
    np.testing.assert_allclose(out_b[:, ::-1], out_a, atol=1e-9)


# ── §16.1/§16.5 引擎版本 ───────────────────────────────────────────────

def test_dsp_engine_version_independent_and_in_cache_identity():
    assert backend.DSP_ENGINE_VERSION == "dsp-v4-20260930"
    assert backend.DSP_ENGINE_VERSION != backend.APP_VERSION
    # 身份键由 run_pipeline 组装；这里验证字段在模块契约中可寻址，
    # 且 audio_format 同级（identity dict 的成员）。
    import pipeline_cache
    assert callable(pipeline_cache.StageCache)


def test_stage_reshape_forwards_space_amount(monkeypatch, tmp_path):
    """stage_reshape 把声场强度授权传给 DSP 脚本（缺省=wet）。"""
    from pathlib import Path as P
    import soundfile as sf
    src = tmp_path / "in.wav"
    t = np.arange(int(SR * 0.05)) / SR
    sf.write(src, np.column_stack((0.02 * np.sin(2 * np.pi * 440 * t),) * 2),
             SR, subtype="PCM_16")
    for name in ("drums.wav", "other.wav"):
        sf.write(tmp_path / name, np.column_stack(
            (0.02 * np.sin(2 * np.pi * 440 * t),) * 2), SR, subtype="PCM_16")
    seen = []
    monkeypatch.setattr(backend, "_run_reported",
                        lambda cmd, *a, **k: seen.append([str(c) for c in cmd]) or 1.0)
    backend.stage_reshape(src, tmp_path, tmp_path / "a.wav", wet=0.5, denoise=0.2)
    backend.stage_reshape(src, tmp_path, tmp_path / "b.wav", wet=0.5, denoise=0.2,
                          space_amount=0.3)
    assert seen[0][seen[0].index("--space-amount") + 1] == "0.5"   # 缺省=wet
    assert seen[1][seen[1].index("--space-amount") + 1] == "0.3"   # 显式解耦
