"""母带前卫生滤波（40Hz 低切 + 20kHz 低通）合同测试。

覆盖三层：DSP 实际响应（低频收掉、可听段不动、20kHz 以上摘掉、样本对齐）、
阶段命令与报告、以及 run_pipeline 的接入位置（在母带引擎之前、母带旁路时不执行）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
APOLLO_DIR = ROOT / "apollo_scripts"
for path in (str(ROOT), str(APOLLO_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import processing_cli  # noqa: E402
import studio_backend as backend  # noqa: E402
import premaster_hygiene  # noqa: E402  (Apollo 工具脚本)


# 63Hz 是贝斯基频区：必须有真实音调，否则该带只有 40Hz 泄漏，测不出「低切不碰它」
def _tone(path, sr=44100, seconds=2.0,
          freqs=(25.0, 40.0, 63.0, 1000.0, 15000.0, 21500.0),
          amps=(0.2, 0.2, 0.15, 0.2, 0.1, 0.05)):
    t = np.arange(int(sr * seconds)) / sr
    mono = sum(a * np.sin(2 * np.pi * f * t) for f, a in zip(freqs, amps))
    sf.write(path, np.column_stack((mono, mono)).astype(np.float32), sr, subtype="FLOAT")
    return path


def _band_level_db(audio, sr, hz, width=0.07):
    """单频带绝对电平（dBFS 域）：给合成音用，不做全曲归一。"""
    from scipy import signal
    edges = [max(1.0, hz * (1 - width)), min(hz * (1 + width), sr / 2.0 - 1.0)]
    sos = signal.butter(4, edges, btype="bandpass", fs=sr, output="sos")
    band = signal.sosfiltfilt(sos, audio, axis=0, padlen=0)
    return 10 * np.log10(max(float(np.mean(band ** 2)), 1e-30))


# ── 1. DSP 响应 ──────────────────────────────────────────────────────

def test_low_cut_attenuates_sub_bass_only(tmp_path):
    sr = 44100
    src = _tone(tmp_path / "in.wav", sr=sr)
    audio, _ = sf.read(src, dtype="float64", always_2d=True)
    out = premaster_hygiene.apply_hygiene(audio, sr, low_cut_hz=40.0, lowpass_hz=None)
    drop_25 = _band_level_db(audio, sr, 25.0) - _band_level_db(out, sr, 25.0)
    drop_40 = _band_level_db(audio, sr, 40.0) - _band_level_db(out, sr, 40.0)
    drop_1k = _band_level_db(audio, sr, 1000.0) - _band_level_db(out, sr, 1000.0)
    # 零相位 2 阶（幅度平方 = 4 阶 24dB/oct），请求值即实际 −3dB 点
    assert abs(drop_40 - 3.0) < 0.6
    assert abs(drop_25 - 11.4) < 0.8
    assert abs(drop_1k) < 0.05               # 1kHz 不动
    drop_63 = _band_level_db(audio, sr, 63.0) - _band_level_db(out, sr, 63.0)
    assert abs(drop_63 - 0.56) < 0.2         # 63Hz 设计值 −0.56dB，贝斯基频基本保住


def test_lowpass_removes_synthetic_ultrasonic_band(tmp_path):
    sr = 44100
    src = _tone(tmp_path / "in.wav", sr=sr)
    audio, _ = sf.read(src, dtype="float64", always_2d=True)
    out = premaster_hygiene.apply_hygiene(audio, sr, low_cut_hz=None, lowpass_hz=20000.0)
    assert _band_level_db(audio, sr, 21500.0) - _band_level_db(out, sr, 21500.0) > 60.0
    assert abs(_band_level_db(audio, sr, 15000.0) - _band_level_db(out, sr, 15000.0)) < 0.05


def test_filter_keeps_sample_alignment_and_shape(tmp_path):
    sr = 44100
    src = _tone(tmp_path / "in.wav", sr=sr)
    audio, _ = sf.read(src, dtype="float64", always_2d=True)
    out = premaster_hygiene.apply_hygiene(audio, sr, low_cut_hz=40.0, lowpass_hz=20000.0)
    assert out.shape == audio.shape and np.isfinite(out).all()
    start, span, slack = sr // 2, 20000, 64
    reference = audio[start:start + span, 0]
    lag = int(np.argmax(np.correlate(out[start - slack:start + span + slack, 0],
                                     reference, mode="valid"))) - slack
    assert lag == 0


def test_report_response_matches_design():
    # 低切实际经 filtfilt（幅度平方），报告必须按平方后的真实响应记录
    low_cut = premaster_hygiene._response_db(
        premaster_hygiene.low_cut_sos(44100, 40.0),
        premaster_hygiene.LOW_CUT_PROBE_HZ, 44100, sos=True, zero_phase=True)
    lowpass = premaster_hygiene._response_db(
        premaster_hygiene.lowpass_kernel(44100, 20000.0),
        premaster_hygiene.LOWPASS_PROBE_HZ, 44100)
    assert abs(float(low_cut["40"]) + 3.01) < 0.1 and abs(float(low_cut["20"]) + 17.6) < 0.2
    assert abs(float(lowpass["19000"])) < 0.05 and float(lowpass["20000"]) < -5.0
    assert float(lowpass["21000"]) < -60.0 and float(lowpass["22000"]) < -60.0


def test_disabled_paths_are_bit_exact(tmp_path):
    sr = 44100
    src = _tone(tmp_path / "in.wav", sr=sr)
    audio, _ = sf.read(src, dtype="float64", always_2d=True)
    out = premaster_hygiene.apply_hygiene(audio, sr, low_cut_hz=0, lowpass_hz=None)
    assert np.array_equal(out, audio)


# ── 1b. 素材自适应低切裁决（2026-09-30，审计 P0-3）────────────────────

def _gated(seconds, sr):
    """1s 开 / 1s 关的门控 × 0.5Hz 慢起伏：活跃帧内有 dB 方差可测联动。"""
    t = np.arange(int(sr * seconds)) / sr
    gate = ((t % 2.0) < 1.0).astype(float)
    gate = np.convolve(gate, np.ones(int(sr * .02)) / (sr * .02), mode="same")
    return t, gate * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t))


def _musical_sub_audio(sr=44100, seconds=6.0):
    """31.5Hz 音乐性 sub：与 100Hz 基频带共享同一包络，静默段一起消失。"""
    t, env = _gated(seconds, sr)
    mono = env * (0.15 * np.sin(2 * np.pi * 31.5 * t)
                  + 0.3 * np.sin(2 * np.pi * 100 * t)
                  + 0.05 * np.sin(2 * np.pi * 1000 * t))
    return np.column_stack((mono, mono))


def _rumble_audio(sr=44100, seconds=6.0):
    """恒定 20-30Hz rumble：与音乐包络无关，静默段照样漂移。"""
    from scipy import signal as sps
    t, env = _gated(seconds, sr)
    rng = np.random.default_rng(3)
    sos = sps.butter(4, 30.0, btype="lowpass", fs=sr, output="sos")
    rumble = sps.sosfiltfilt(sos, rng.normal(0, 0.5, len(t)))
    mono = env * (0.3 * np.sin(2 * np.pi * 100 * t)
                  + 0.05 * np.sin(2 * np.pi * 1000 * t)) + rumble
    return np.column_stack((mono, mono))


def test_musical_sub_is_not_filtered():
    audio = _musical_sub_audio()
    chosen, analysis = premaster_hygiene.choose_low_cut_hz(audio, 44100, 40.0)
    assert chosen == 0.0                       # 强音乐性 → 不处理
    assert analysis["envelope_correlation"] >= 0.5
    assert analysis["sub_ratio_db"] >= -20.0
    assert analysis["silent_leak_db"] <= -6.0
    assert analysis["chosen_hz"] == 0.0 and analysis["requested_hz"] == 40.0


def test_rumble_keeps_requested_low_cut():
    audio = _rumble_audio()
    chosen, analysis = premaster_hygiene.choose_low_cut_hz(audio, 44100, 40.0)
    assert chosen == 40.0                      # 与音乐无关的漂移 → 请求值全额处理
    assert analysis["envelope_correlation"] < 0.35


def test_constant_tone_and_short_material_keep_requested():
    # 恒定电平（合成测试音）：包络无起伏 → 无联动证据 → 请求值
    sr = 44100
    t = np.arange(int(sr * 3)) / sr
    mono = 0.2 * np.sin(2 * np.pi * 30 * t) + 0.2 * np.sin(2 * np.pi * 100 * t)
    chosen, analysis = premaster_hygiene.choose_low_cut_hz(
        np.column_stack((mono, mono)), sr, 40.0)
    assert chosen == 40.0 and analysis["envelope_correlation"] == 0.0
    # 素材过短：保守沿用请求值
    chosen, analysis = premaster_hygiene.choose_low_cut_hz(
        _musical_sub_audio(seconds=1.0), sr, 40.0)
    assert chosen == 40.0 and analysis["skipped"] == "insufficient_material"


def test_chosen_never_exceeds_requested():
    for requested in (25.0, 30.0, 40.0, 100.0):
        chosen, _ = premaster_hygiene.choose_low_cut_hz(
            _musical_sub_audio(), 44100, requested)
        assert 0.0 <= chosen <= requested


def test_cli_records_adaptive_decision_in_report(tmp_path):
    sr = 44100
    src = tmp_path / "musical.wav"
    sf.write(src, _musical_sub_audio().astype(np.float32), sr, subtype="FLOAT")
    import subprocess
    result = subprocess.run(
        [sys.executable, str(APOLLO_DIR / "premaster_hygiene.py"),
         "--in", str(src), "--out", str(tmp_path / "out.wav"),
         "--low-cut-hz", "40", "--report-json", str(tmp_path / "r.json")],
        capture_output=True, text=True, cwd=str(APOLLO_DIR), timeout=300)
    assert result.returncode == 0, result.stderr
    import json
    extra = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["extra"]
    assert extra["low_cut"]["requested_hz"] == 40.0
    assert extra["low_cut"]["hz"] == 0.0 and not extra["low_cut"]["applied"]
    assert extra["low_cut"]["analysis"]["chosen_hz"] == 0.0
    # 裁决不处理时输出与输入位级一致（只有低通未启用、无峰值缩放路径）
    out, _ = sf.read(tmp_path / "out.wav", dtype="float64", always_2d=True)
    src_audio, _ = sf.read(src, dtype="float64", always_2d=True)
    np.testing.assert_array_equal(out, src_audio)


# ── 2. 阶段命令与报告 ────────────────────────────────────────────────

def test_stage_hygiene_forwards_requested_cutoffs(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(backend, "_run_reported",
                        lambda cmd, *a, **k: seen.append([str(c) for c in cmd]) or 1.0)
    backend.stage_hygiene(tmp_path / "in.wav", tmp_path / "out.wav",
                          low_cut_hz=45.0, lowpass_hz=19000.0)
    cmd = seen[-1]
    assert float(cmd[cmd.index("--low-cut-hz") + 1]) == 45.0
    assert float(cmd[cmd.index("--lowpass-hz") + 1]) == 19000.0


def test_stage_hygiene_defaults_come_from_table(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(backend, "_run_reported",
                        lambda cmd, *a, **k: seen.append([str(c) for c in cmd]) or 1.0)
    backend.stage_hygiene(tmp_path / "in.wav", tmp_path / "out.wav")
    cmd = seen[-1]
    assert float(cmd[cmd.index("--low-cut-hz") + 1]) == backend.DEFAULTS["hygiene_low_cut_hz"]
    assert float(cmd[cmd.index("--lowpass-hz") + 1]) == backend.DEFAULTS["hygiene_lowpass_hz"]


def test_cli_forwards_hygiene_values(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    captured = {}

    def fake_run_batch(inputs, output, **kwargs):
        captured.update(kwargs)
        return [(str(inputs[0]), "done.wav", None)]

    monkeypatch.setattr(backend, "run_batch", fake_run_batch)
    assert processing_cli.main(["-i", str(source), "-o", str(tmp_path / "o1")]) == 0
    assert captured["hygiene_low_cut_hz"] == backend.DEFAULTS["hygiene_low_cut_hz"]
    assert captured["hygiene_lowpass_hz"] == backend.DEFAULTS["hygiene_lowpass_hz"]
    assert processing_cli.main(
        ["-i", str(source), "-o", str(tmp_path / "o2"),
         "--hygiene-low-cut-hz", "0", "--hygiene-lowpass-hz", "0"]) == 0
    assert captured["hygiene_low_cut_hz"] == 0.0
    assert captured["hygiene_lowpass_hz"] == 0.0


# ── 3. 管线接入位置 ──────────────────────────────────────────────────

@pytest.fixture
def pipeline_fakes(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    monkeypatch.setattr(backend, "stage_demucs",
                        lambda input_wav, out_dir, model="htdemucs", progress=None,
                        cancel=None, device=None: None)
    calls = []
    for name, arg in (("stage_bass", "in_mix"), ("stage_drums", "rest_wav"),
                      ("stage_reshape", "in_mix")):
        def make(stage=name):
            def stub(*args, **kwargs):
                calls.append(stage)
                return 1.0
            setattr(stub, "writes_stage_report", False)
            return stub
        monkeypatch.setattr(backend, name, make())
    monkeypatch.setattr(backend, "stage_vocals",
                        lambda stem_dir, in_mix, out_wav, **kwargs: shutil.copyfile(in_mix, out_wav))
    mastering_input = {}

    def fake_hygiene(in_wav, out_wav, **kwargs):
        calls.append("hygiene")
        mastering_input["hygiene"] = (str(in_wav), kwargs.get("low_cut_hz"),
                                      kwargs.get("lowpass_hz"))
        shutil.copyfile(in_wav, out_wav)
        return 1.0
    monkeypatch.setattr(backend, "stage_hygiene", fake_hygiene)

    def fake_soren(in_wav, out_wav, **kwargs):
        calls.append("soren")
        mastering_input["soren"] = str(in_wav)
        shutil.copyfile(in_wav, out_wav)
    monkeypatch.setattr(backend, "stage_soren", fake_soren)
    return calls, mastering_input


def _run(tmp_path, **overrides):
    src = _tone(tmp_path / "in.wav")
    kwargs = dict(bypass=["lew", "bass", "drums", "reshape", "vocals"],
                  balance_mode=None, cache_enabled=False, style_mode="eq_only")
    kwargs.update(overrides)
    return src, backend.run_pipeline(src, tmp_path / "out", **kwargs)


def test_pipeline_filters_premaster_before_mastering(pipeline_fakes, tmp_path):
    calls, seen = pipeline_fakes
    _, out = _run(tmp_path)
    assert calls == ["hygiene", "soren"]
    assert seen["hygiene"][1] == backend.DEFAULTS["hygiene_low_cut_hz"]
    assert seen["hygiene"][2] == backend.DEFAULTS["hygiene_lowpass_hz"]
    # 母带引擎收到的是滤波后的 premaster，而不是 vocal 混音
    assert seen["soren"] != seen["hygiene"][0]
    assert Path(seen["soren"]).name.endswith("_premaster.wav")
    assert Path(out).is_file()


def test_pipeline_skips_hygiene_when_mastering_bypassed(pipeline_fakes, tmp_path):
    calls, _ = pipeline_fakes
    _, out = _run(tmp_path, bypass=["lew", "bass", "drums", "reshape", "vocals", "soren"])
    assert "hygiene" not in calls and "soren" not in calls
    assert Path(out).is_file()


def test_pipeline_skips_hygiene_when_both_disabled(pipeline_fakes, tmp_path):
    calls, seen = pipeline_fakes
    _run(tmp_path, hygiene_low_cut_hz=0, hygiene_lowpass_hz=0)
    assert calls == ["soren"]
    assert Path(seen["soren"]).name.endswith("_vocalmix.wav")


def test_pipeline_records_hygiene_in_quality_report(pipeline_fakes, tmp_path):
    _, out = _run(tmp_path)
    report = backend.read_quality_report(out)
    assert report["processing"]["hygiene_low_cut_hz"] == 40.0
    assert report["processing"]["hygiene_lowpass_hz"] == 20000.0
