"""Stage3 adaptive_all HF 降噪的后端/CLI/缓存/质量报告贯通回归。

锚定契约：
  1. 默认（noise_mode="other"）不转发任何 --noise-* 参数，旧命令行为不变；
     adaptive_all 才把四个标志传给 soundstage_reshape.py；
  2. 显式非法模式/频带在跑任何阶段前拒绝；
  3. CLI 边界：范围在解析期拒绝（exit 2），adaptive_all 要求 low<high（exit 1）；
  4. 旁路/透传阶段不产生也绝不引用上次运行遗留的阶段报告；
  5. bass/reshape 诊断 extras 进入最终质量报告 stages，且跨输出目录的缓存命中
     诊断逐字一致（报告随成品恢复并重绑到本次文件，read_report 仍新鲜有效）。

替身阶段用与生产签名一致的参数名（stage_cache 的包装器按名绑定参数）。
"""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import processing_cli  # noqa: E402
import studio_backend as backend  # noqa: E402
from apollo_scripts.stage_metadata import read_report, write_report  # noqa: E402


def _tone_wav(path, sr=44100, seconds=0.05):
    t = np.arange(int(sr * seconds)) / sr
    data = np.column_stack((0.02 * np.sin(2 * np.pi * 440 * t),) * 2)
    sf.write(path, data, sr, subtype="PCM_16")
    return path


def _capture_reported(monkeypatch):
    seen = []
    monkeypatch.setattr(backend, "_run_reported",
                        lambda cmd, *a, **k: seen.append([str(c) for c in cmd]) or 1.0)
    return seen


RESHAPE_EXTRA = {
    "noise": {"mode": "adaptive_all", "amount": 0.2, "applied": True,
              "reason": "high_confidence_noise",
              "stems": {"other": {"status": "applied", "confidence_mean": 0.8},
                        "bass": {"status": "not_applied", "reason": "disabled"}},
              "mix_budget": {"admitted_delta_gain": 1.0, "applied": True}},
    "width": {"bands": [], "analysis": "4096 Hann / 75% overlap"},
}
BASS_EXTRA = {"clarity": {"low_cut_db": -1.0, "presence_db": 0.5}}


def _write_stage_artifacts(name, extra, calls, in_mix, out_wav, report_json):
    calls.append(name)
    shutil.copyfile(in_mix, out_wav)
    if report_json is not None:
        payload = {} if extra is None else {"extra": extra}
        write_report(report_json, stage=name, scale=1.0,
                     input_path=in_mix, output_path=out_wav, **payload)
    return 1.0


def _fake_bass(extra, calls):
    def fake_bass(stem_dir, in_mix, out_wav, report_json=None, **kwargs):
        return _write_stage_artifacts("bass", extra, calls, in_mix, out_wav, report_json)
    fake_bass.writes_stage_report = True
    fake_bass.report_input_arg = "in_mix"
    return fake_bass


def _fake_drums(extra, calls):
    def fake_drums(stem_dir, rest_wav, out_wav, report_json=None, **kwargs):
        return _write_stage_artifacts("drums", extra, calls, rest_wav, out_wav, report_json)
    fake_drums.writes_stage_report = True
    fake_drums.report_input_arg = "rest_wav"
    return fake_drums


def _fake_reshape(extra, calls):
    def fake_reshape(in_mix, stems_dir, out_wav, report_json=None, **kwargs):
        return _write_stage_artifacts("reshape", extra, calls, in_mix, out_wav, report_json)
    fake_reshape.writes_stage_report = True
    fake_reshape.report_input_arg = "in_mix"
    return fake_reshape


@pytest.fixture
def pipeline_fakes(monkeypatch, tmp_path):
    """旁路 Lew/人声、stub 分离与母带的管线替身；bass/drums/reshape 写真报告。

    参数名必须与生产签名一致：stage_cache 包装器按名绑定参数（output_arg、
    input_args 都从绑定值取）。
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    monkeypatch.setattr(backend, "stage_demucs",
                        lambda input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None: None)
    calls = []
    monkeypatch.setattr(backend, "stage_bass", _fake_bass(BASS_EXTRA, calls))
    monkeypatch.setattr(backend, "stage_drums", _fake_drums(None, calls))
    monkeypatch.setattr(backend, "stage_reshape", _fake_reshape(RESHAPE_EXTRA, calls))

    def fake_soren(input_wav, out_wav, **kwargs):
        calls.append("soren")
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "stage_soren", fake_soren)
    return calls


def _run_kwargs(**overrides):
    kwargs = dict(bypass=["lew", "vocals"], balance_mode=None, cache_enabled=False)
    kwargs.update(overrides)
    return kwargs


# ── 1. 命令转发与默认行为 ────────────────────────────────────────────

def test_stage_reshape_default_and_other_omit_noise_flags(monkeypatch, tmp_path):
    src = _tone_wav(tmp_path / "in.wav")
    for name in ("drums.wav", "other.wav"):
        _tone_wav(tmp_path / name)
    seen = _capture_reported(monkeypatch)
    backend.stage_reshape(src, tmp_path, tmp_path / "a.wav", wet=0.5, denoise=0.2)
    backend.stage_reshape(src, tmp_path, tmp_path / "b.wav", wet=0.5, denoise=0.2,
                          noise_mode="other")
    assert len(seen) == 2
    for cmd in seen:
        assert not any("--noise" in part for part in cmd)
        assert cmd[cmd.index("--other-denoise-amount") + 1] == "0.2"


def test_stage_reshape_adaptive_all_forwards_all_flags(monkeypatch, tmp_path):
    src = _tone_wav(tmp_path / "in.wav")
    for name in ("drums.wav", "other.wav"):
        _tone_wav(tmp_path / name)
    seen = _capture_reported(monkeypatch)
    backend.stage_reshape(src, tmp_path, tmp_path / "out.wav", wet=0.5, denoise=0.2,
                          noise_mode="adaptive_all", noise_low_hz=9000,
                          noise_high_hz=18000, noise_max_attenuation_db=4)
    cmd = seen[-1]
    assert cmd[cmd.index("--noise-mode") + 1] == "adaptive_all"
    assert float(cmd[cmd.index("--noise-low-hz") + 1]) == 9000.0
    assert float(cmd[cmd.index("--noise-high-hz") + 1]) == 18000.0
    assert float(cmd[cmd.index("--noise-max-attenuation-db") + 1]) == 4.0


@pytest.mark.parametrize("kwargs", [
    dict(noise_mode="magic"),
    dict(noise_mode="adaptive_all", noise_low_hz=7000),
    dict(noise_mode="adaptive_all", noise_high_hz=23000),
    dict(noise_mode="adaptive_all", noise_max_attenuation_db=9),
    dict(noise_mode="adaptive_all", noise_low_hz=16000, noise_high_hz=16000),
])
def test_stage_reshape_rejects_invalid_noise_options_early(monkeypatch, tmp_path, kwargs):
    seen = _capture_reported(monkeypatch)
    with pytest.raises(backend.PipelineError):
        backend.stage_reshape(tmp_path / "in.wav", tmp_path, tmp_path / "o.wav", **kwargs)
    assert seen == []


def test_run_pipeline_validates_noise_args_before_anything(tmp_path):
    # 输入不存在 + 非法噪声参数：报错必须是噪声参数（证明先于输入/阶段校验）
    with pytest.raises(backend.PipelineError, match="降噪模式"):
        backend.run_pipeline(tmp_path / "missing.wav", tmp_path / "out",
                             noise_mode="bogus")
    with pytest.raises(backend.PipelineError, match="noise_low_hz"):
        backend.run_pipeline(tmp_path / "missing.wav", tmp_path / "out",
                             noise_mode="adaptive_all", noise_low_hz=1000)


# ── 2. CLI 边界 ──────────────────────────────────────────────────────

def test_cli_forwards_noise_defaults_and_explicit_values(tmp_path, monkeypatch):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    captured = {}

    def fake_run_batch(inputs, output, **kwargs):
        captured.update(kwargs)
        return [(str(inputs[0]), "done.wav", None)]

    monkeypatch.setattr(backend, "run_batch", fake_run_batch)
    assert processing_cli.main(["-i", str(source), "-o", str(tmp_path / "o1")]) == 0
    assert captured["noise_mode"] == "other"
    assert captured["noise_low_hz"] == 8000.0
    assert captured["noise_high_hz"] == 20000.0
    assert captured["noise_max_attenuation_db"] == 6.0
    assert processing_cli.main(
        ["-i", str(source), "-o", str(tmp_path / "o2"),
         "--noise-mode", "adaptive_all", "--noise-low-hz", "12000",
         "--noise-high-hz", "21000", "--noise-max-attenuation-db", "3"]) == 0
    assert (captured["noise_mode"], captured["noise_low_hz"],
            captured["noise_high_hz"], captured["noise_max_attenuation_db"]) == \
        ("adaptive_all", 12000.0, 21000.0, 3.0)


@pytest.mark.parametrize("args", [
    ["--noise-mode", "magic"],
    ["--noise-low-hz", "7000"],
    ["--noise-high-hz", "22001"],
    ["--noise-max-attenuation-db", "6.5"],
])
def test_cli_rejects_invalid_mode_and_ranges_at_parse(tmp_path, args):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")
    with pytest.raises(SystemExit) as exc:
        processing_cli.main(["-i", str(source), "-o", str(tmp_path), *args])
    assert exc.value.code == 2


def test_cli_adaptive_requires_low_below_high(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.wav"
    source.write_bytes(b"test")

    def guard(*a, **k):
        raise AssertionError("run_batch must not run for an invalid noise band")

    monkeypatch.setattr(backend, "run_batch", guard)
    assert processing_cli.main(
        ["-i", str(source), "-o", str(tmp_path / "out"),
         "--noise-mode", "adaptive_all",
         "--noise-low-hz", "18000", "--noise-high-hz", "12000"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert "noise" in report["error"].lower()
    assert "run_batch" not in report["error"]


# ── 3. 质量报告 propagation / 旁路与透传不引用过期报告 ───────────────

def test_stage_extras_propagate_to_quality_report(pipeline_fakes, tmp_path):
    src = _tone_wav(tmp_path / "song.wav")
    final = backend.run_pipeline(
        src, tmp_path / "out", noise_mode="adaptive_all",
        noise_low_hz=9000, noise_high_hz=20000, noise_max_attenuation_db=5,
        **_run_kwargs())
    report = backend.read_quality_report(final)
    assert report["processing"]["noise_mode"] == "adaptive_all"
    assert report["processing"]["noise_low_hz"] == 9000
    stages = report["stages"]
    assert stages["reshape"]["noise"]["stems"]["other"]["confidence_mean"] == 0.8
    assert stages["reshape"]["noise"]["mix_budget"]["applied"] is True
    assert stages["bass"]["clarity"]["low_cut_db"] == -1.0
    assert "drums" not in stages        # 无 extra 的阶段不进入报告


def test_default_run_quality_report_has_no_noise_params(pipeline_fakes, tmp_path):
    src = _tone_wav(tmp_path / "song.wav")
    final = backend.run_pipeline(src, tmp_path / "out", **_run_kwargs())
    report = backend.read_quality_report(final)
    assert "noise_mode" not in report["processing"]
    assert "reshape" in report["stages"]


def test_bypassed_reshape_never_picks_stale_report(pipeline_fakes, tmp_path):
    src = _tone_wav(tmp_path / "song.wav")
    work = tmp_path / "work"
    work.mkdir()
    stale = {"schema_version": 1, "stage": "reshape", "scale": 1.0,
             "extra": {"noise": {"mode": "adaptive_all", "reason": "STALE_MARKER"}}}
    (work / "reshape.json").write_text(json.dumps(stale), encoding="utf-8")
    final = backend.run_pipeline(src, tmp_path / "out", work_dir=work,
                                 bypass=["lew", "vocals", "reshape"],
                                 balance_mode=None, cache_enabled=False)
    stages = backend.read_quality_report(final)["stages"]
    assert "reshape" not in stages       # 旁路阶段即使有遗留报告也不进质量报告
    assert "bass" in stages


def test_zero_wet_passthrough_writes_fresh_not_applied_report(tmp_path, monkeypatch):
    """wet=0/denoise=0 走透传：本次生成"未施加"报告，绝不引用上次运行遗留。"""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: runtime)
    monkeypatch.setattr(backend, "stage_demucs",
                        lambda input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None: None)

    def fake_bass(stem_dir, in_mix, out_wav, **kwargs):
        shutil.copyfile(in_mix, out_wav)
        return 1.0

    def fake_drums(stem_dir, rest_wav, out_wav, **kwargs):
        shutil.copyfile(rest_wav, out_wav)
        return 1.0

    def fake_soren(input_wav, out_wav, **kwargs):
        shutil.copyfile(input_wav, out_wav)

    monkeypatch.setattr(backend, "stage_bass", fake_bass)
    monkeypatch.setattr(backend, "stage_drums", fake_drums)
    monkeypatch.setattr(backend, "stage_soren", fake_soren)
    src = _tone_wav(tmp_path / "song.wav")
    work = tmp_path / "work"
    work.mkdir()
    stale = {"schema_version": 1, "stage": "reshape", "scale": 1.0,
             "extra": {"noise": {"reason": "STALE_MARKER"}}}
    (work / "reshape.json").write_text(json.dumps(stale), encoding="utf-8")
    final = backend.run_pipeline(src, tmp_path / "out", work_dir=work,
                                 bypass=["lew", "vocals"], space_wet=0.0,
                                 space_denoise=0.0, balance_mode=None,
                                 cache_enabled=False)
    stages = backend.read_quality_report(final)["stages"]
    noise = stages["reshape"]["noise"]
    assert noise["reason"] == "passthrough" and noise["applied"] is False
    assert noise["mode"] == "other"


# ── 4. 缓存命中：诊断一致 + 报告重绑后 read_report 仍有效 ────────────

def test_cache_hit_restores_diagnostics_across_output_dirs(
        pipeline_fakes, tmp_path, monkeypatch):
    src = _tone_wav(tmp_path / "song.wav")
    kwargs = _run_kwargs(cache_enabled=True, noise_mode="adaptive_all",
                         noise_low_hz=9000, noise_high_hz=20000,
                         noise_max_attenuation_db=5)
    final1 = backend.run_pipeline(src, tmp_path / "out1", **kwargs)
    first = backend.read_quality_report(final1)["stages"]

    observed = []
    real_rebind = backend._rebind_stage_report

    def spy_rebind(report_path, input_path, output_path):
        real_rebind(report_path, input_path, output_path)
        # 工作目录随运行结束清理，重绑校验必须在运行内完成
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
        scale = read_report(report_path, stage=payload["stage"],
                            input_path=input_path, output_path=output_path)
        observed.append((payload["stage"], isinstance(scale, float)))

    monkeypatch.setattr(backend, "_rebind_stage_report", spy_rebind)
    final2 = backend.run_pipeline(src, tmp_path / "out2", **kwargs)

    assert pipeline_fakes.count("bass") == 1     # 缓存命中：阶段未重算
    assert pipeline_fakes.count("reshape") == 1
    assert {stage for stage, _ in observed} >= {"bass", "reshape"}
    assert all(ok for _, ok in observed), \
        "rebound reports must pass read_report freshness validation"
    second = backend.read_quality_report(final2)["stages"]
    assert second == first                       # 命中运行的诊断与首次逐字一致
    assert second["reshape"]["noise"]["stems"]["other"]["confidence_mean"] == 0.8
