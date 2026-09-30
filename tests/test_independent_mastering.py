"""No-style mastering must work without Soren assets and verify real outputs."""
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

import studio_backend as backend
from audio_metrics import measure_audio
from mastering import LOUDNESS_TARGETS, TRUE_PEAK_CEILING_DB
from mastering.finalizer import finalize, linked_limiter, master_file

SR = 44100


def music(seconds=1.2):
    t = np.arange(round(SR * seconds)) / SR
    mid = .07 * np.sin(2 * np.pi * 220 * t) + .04 * np.sin(2 * np.pi * 3700 * t)
    side = .035 * np.sin(2 * np.pi * 610 * t)
    return np.column_stack((mid + side, mid - side))


@pytest.mark.parametrize("mode", LOUDNESS_TARGETS)
def test_loudness_presets_write_verified_pcm24(tmp_path, mode):
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    stats = master_file(src, dst, mode)
    info = sf.info(dst)
    written, _ = sf.read(dst, always_2d=True)
    measured = measure_audio(written, SR)
    assert info.subtype == "PCM_24" and info.frames == len(music())
    assert stats["readback_verified"] and stats["loudness_target_source"] == "application_preset"
    assert stats["actual_lufs"] == pytest.approx(measured["integrated_lufs"], abs=1e-8)
    assert stats["true_peak_dbtp"] <= TRUE_PEAK_CEILING_DB
    assert abs(stats["actual_lufs"] - LOUDNESS_TARGETS[mode]) <= .2
    assert json.loads(Path(str(dst) + ".mastering.json").read_text())["target_met"]


def test_unlimited_output_preserves_spectrum_and_stereo():
    x = music()
    original = x.copy()
    y, stats = finalize(x, loudness="soft")
    np.testing.assert_array_equal(x, original)
    assert stats["limiter"]["max_gain_reduction_db"] == 0
    np.testing.assert_allclose(y, x * 10 ** ((stats["applied_gain_db"] +
                                             stats["safety_trim_db"]) / 20), atol=1e-14)
    f, p = signal.welch(x, SR, nperseg=SR, axis=0)
    _, q = signal.welch(y, SR, nperseg=SR, axis=0)
    bins = [np.argmin(abs(f - hz)) for hz in (220, 610, 3700)]
    gains = 10 * np.log10(q[bins] / p[bins])
    assert np.ptp(gains) < .01
    a, b = measure_audio(x, SR), measure_audio(y, SR)
    assert a["side_mid"]["db"] == pytest.approx(b["side_mid"]["db"], abs=.01)


def test_linked_limiter_preserves_channel_ratio_and_peak():
    rng = np.random.default_rng(48)
    x = rng.standard_normal(30000) * 1.7
    stereo = np.column_stack((x, x * .31))
    y, stats = linked_limiter(stereo, SR * 4, 150)
    assert np.max(abs(y)) <= 10 ** (-.5 / 20) + 1e-12
    np.testing.assert_allclose(y[:, 1], y[:, 0] * .31, atol=1e-12)
    assert stats["max_gain_reduction_db"] > 0


def test_dynamic_budget_allows_below_target():
    x = music(2) * .005
    x[::11025] += .95
    _, stats = finalize(x, loudness="loud")
    assert stats["target_status"] == "below_target"
    assert stats["dynamic_budget_limited"]
    assert stats["limiter"]["max_gain_reduction_db"] <= stats["limiter_peak_budget_db"] + 1e-6
    assert stats["limiter"]["gain_reduction_p95_db"] <= stats["limiter_p95_budget_db"] + 1e-6


def test_finalize_reuses_loop_measurements_and_matches_full_rescan():
    """审计 P1-3：finalize 收尾复用搜索循环对胜出候选算出的 LUFS / 真峰，
    复用值必须与独立全量重测一致（LUFS 逐位相同，真峰差在浮点噪声内）。"""
    from audio_metrics import integrated_lufs
    from mastering.finalizer import true_peak_db
    result, stats = finalize(music(2), SR, "normal")
    fresh_lufs, _ = integrated_lufs(result, SR)
    assert stats["actual_lufs"] == fresh_lufs                       # 同一数组同一函数
    assert abs(stats["true_peak_dbtp"] - true_peak_db(result)) < 1e-9
    assert stats["true_peak_dbtp"] <= TRUE_PEAK_CEILING_DB


def test_finalize_does_not_rescan_result_after_search(monkeypatch):
    """整曲扫描计数：LUFS = 输入 1 次 + 每迭代 1 次；真峰 = 每迭代 1 次。
    收尾复用 → 两者都不得再多出一次对 result 的整曲复测。"""
    import mastering.finalizer as fin
    from audio_metrics import integrated_lufs as real_lufs
    real_peak = fin.true_peak_db
    lufs_calls, peak_calls = [], []
    monkeypatch.setattr(fin, "integrated_lufs",
                        lambda a, sr: (lufs_calls.append(1), real_lufs(a, sr))[1])
    monkeypatch.setattr(fin, "true_peak_db",
                        lambda a: (peak_calls.append(1), real_peak(a))[1])
    _, stats = fin.finalize(music(2), SR, "normal")
    iters = stats["iterations"]
    assert len(lufs_calls) == 1 + iters      # 旧实现为 2 + iters（收尾多一次）
    assert len(peak_calls) == iters          # 旧实现为 iters + 1


@pytest.mark.parametrize("bad", [np.zeros((SR, 2)), np.full((100, 2), np.nan), np.zeros((0, 2))])
def test_invalid_audio_is_rejected(bad):
    with pytest.raises(ValueError):
        finalize(bad)


def test_off_stage_does_not_load_soren_or_reference(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: pytest.fail("Soren requested"))
    monkeypatch.setattr(backend, "_run_stream", lambda cmd, *a, **kw: calls.append((cmd, kw)))
    backend.stage_soren(tmp_path / "in.wav", tmp_path / "out.wav", style_mode="off",
                        genre="unavailable", reference=tmp_path / "missing.wav")
    cmd, kwargs = calls[0]
    assert cmd[1:3] == ["-m", "mastering"]
    assert "--reference" not in cmd and "--genre" not in cmd
    assert kwargs["env"]["PYTHONPATH"] == str(backend.MASTERING_ROOT)


@pytest.mark.parametrize('mastering_bypassed', [False, True])
def test_mono_lew_bypass_normalizes_only_when_processing(tmp_path, monkeypatch, mastering_bypassed):
    monkeypatch.setattr(backend, 'PYTHON', Path(sys.executable))
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: pytest.fail('Soren requested'))
    src = tmp_path/'mono.wav'
    sf.write(src, music(1).mean(axis=1), SR, subtype='PCM_16')
    bypass = ['lew', 'bass', 'drums', 'reshape', 'vocals']
    if mastering_bypassed:
        bypass.append('soren')
    out = backend.run_pipeline(src, tmp_path/'out', bypass=bypass,
                               style_mode='off', loudness='soft', cache_enabled=False)
    if mastering_bypassed:
        assert Path(out).read_bytes() == src.read_bytes()
    else:
        x, sr = sf.read(out, always_2d=True)
        assert x.shape == (SR, 2) and sr == SR
        np.testing.assert_allclose(x[:, 0], x[:, 1], atol=3 / 2**23)
        assert backend.read_mastering_stats(out)['readback_verified']


def test_off_pipeline_cache_and_report_without_soren(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: pytest.fail("Soren requested"))
    monkeypatch.setattr(backend, "SOREN_DIR", tmp_path / "no-soren")
    monkeypatch.setattr(backend, "APOLLO_DIR", tmp_path / "no-models")
    calls = []
    def local_stage(input_wav, out_wav, loudness="normal", progress=None, cancel=None):
        calls.append(1)
        return master_file(input_wav, out_wav, loudness)
    monkeypatch.setattr(backend, "stage_mastering", local_stage)
    src = tmp_path / "music.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    options = dict(style_mode="off", loudness="soft", genre="missing-profile",
                   bypass=["lew", "bass", "drums", "reshape", "vocals"])
    out1 = backend.run_pipeline(src, tmp_path / "out", **options)
    out2 = backend.run_pipeline(src, tmp_path / "out", **options)
    assert len(calls) == 1
    assert Path(out1).read_bytes() == Path(out2).read_bytes()
    assert backend.read_mastering_stats(out2)["readback_verified"]
    assert backend.read_quality_report(out2) is not None


def test_no_style_bypass_does_not_finalize(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: pytest.fail("Soren requested"))
    monkeypatch.setattr(backend, "stage_mastering", lambda *a, **k: pytest.fail("Mastering requested"))
    src = tmp_path / "music.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    dst = backend.run_pipeline(src, tmp_path / "out", style_mode="off",
                               bypass=["lew", "bass", "drums", "reshape", "vocals", "soren"])
    assert Path(dst).read_bytes() == src.read_bytes()
    assert backend.read_mastering_stats(dst) is None


def test_packaged_runtime_resolves_without_soren(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "Apollo").mkdir(parents=True)
    (assets / "mastering").mkdir()
    monkeypatch.setattr(backend, "ROOT", tmp_path / "app")
    monkeypatch.setattr(backend, "_user_gpu_py", lambda: None)
    monkeypatch.setenv("SB_ASSETS", str(assets))
    _, _, _, root = backend._resolve_runtime()
    assert root == assets


@pytest.mark.parametrize("packaged", [False, True])
def test_real_subprocess_reports_progress_and_readback(tmp_path, monkeypatch, packaged):
    if packaged:
        runtime = tmp_path / "runtime"
        (runtime / "mastering").mkdir(parents=True)
        repo = Path(__file__).resolve().parents[1]
        for path in (repo / "mastering").glob("*.py"):
            shutil.copyfile(path, runtime / "mastering" / path.name)
        shutil.copyfile(repo / "audio_metrics.py", runtime / "audio_metrics.py")
        monkeypatch.setattr(backend, "MASTERING_ROOT", runtime)
    monkeypatch.setattr(backend, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: pytest.fail("Soren requested"))
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src, music(.6), SR, subtype="FLOAT")
    progress = []
    stats = backend.stage_soren(src, dst, style_mode="off", loudness="soft",
                               progress=lambda f, label: progress.append(f))
    assert stats["readback_verified"]
    assert any(0 < f < 1 for f in progress) and progress[-1] == 1


def test_subprocess_can_be_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "PYTHON", Path(sys.executable))
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    with pytest.raises(backend.PipelineError, match="用户取消"):
        backend.stage_mastering(src, dst, cancel=lambda: True)
    assert not dst.exists()


def test_master_file_refuses_source_overwrite(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    original = src.read_bytes()
    with pytest.raises(ValueError, match="different files"):
        master_file(src, src)
    assert src.read_bytes() == original


@pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000])
def test_pipeline_resamples_before_independent_mastering(tmp_path, monkeypatch, rate):
    monkeypatch.setattr(backend, "_ensure_dev_runtime", lambda: pytest.fail("Soren requested"))
    def local_stage(input_wav, out_wav, loudness="normal", progress=None, cancel=None):
        return master_file(input_wav, out_wav, loudness)
    monkeypatch.setattr(backend, "stage_mastering", local_stage)
    src = tmp_path / "input.wav"
    t = np.arange(round(rate * .6)) / rate
    sf.write(src, np.column_stack((.1 * np.sin(2 * np.pi * 440 * t),) * 2), rate, subtype="FLOAT")
    dst = backend.run_pipeline(src, tmp_path / "out", style_mode="off", loudness="soft",
                               bypass=["lew", "bass", "drums", "reshape", "vocals"])
    info = sf.info(dst)
    assert info.samplerate == SR and info.channels == 2 and info.frames == round(SR * .6)
