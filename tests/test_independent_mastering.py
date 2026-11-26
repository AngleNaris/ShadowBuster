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
from mastering import LOUDNESS_TARGETS, TRUE_PEAK_CEILING_DB, LOUD_TRUE_PEAK_CEILING_DB
from mastering.loud import PLUGIN_PATH, section_profile
import mastering.finalizer as finalizer
from mastering.finalizer import (ACTIVE_BUDGET_FRACTION, finalize, linked_limiter,
                                 master_file)

SR = 44100


def music(seconds=1.2):
    t = np.arange(round(SR * seconds)) / SR
    mid = .07 * np.sin(2 * np.pi * 220 * t) + .04 * np.sin(2 * np.pi * 3700 * t)
    side = .035 * np.sin(2 * np.pi * 610 * t)
    return np.column_stack((mid + side, mid - side))


@pytest.mark.parametrize("mode", LOUDNESS_TARGETS)
def test_loudness_presets_write_verified_pcm24(tmp_path, mode):
    if mode == "loud":
        pytest.importorskip("pedalboard")
        if not PLUGIN_PATH.exists() or shutil.which("ffmpeg") is None:
            pytest.skip("ZL Limiter VST3 and FFmpeg are required")
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(src, music(), SR, subtype="FLOAT")
    stats = master_file(src, dst, mode)
    info = sf.info(dst)
    written, _ = sf.read(dst, always_2d=True)
    measured = measure_audio(written, SR)
    assert info.subtype == "PCM_24" and info.frames == len(music())
    assert stats["readback_verified"] and stats["loudness_target_source"] == "application_preset"
    assert stats["actual_lufs"] == pytest.approx(measured["integrated_lufs"], abs=1e-8)
    ceiling = LOUD_TRUE_PEAK_CEILING_DB if mode == "loud" else TRUE_PEAK_CEILING_DB
    assert stats["true_peak_dbtp"] <= ceiling
    if mode == "loud":
        assert stats["limiter"]["engine"] == "ZL Limiter 0.0.1"
        assert stats["target_lufs_range"] == (-8.0, -6.0)
    assert abs(stats["actual_lufs"] - LOUDNESS_TARGETS[mode]) <= .2
    assert json.loads(Path(str(dst) + ".mastering.json").read_text())["target_met"]


def test_loud_sections_use_original_audio_on_the_same_timeline(tmp_path):
    premaster = music(24)
    original = premaster.copy()
    original[:12 * SR] *= .2
    path = tmp_path / "original.wav"
    sf.write(path, original, SR, subtype="FLOAT")
    weights, stats = section_profile(premaster, SR, path)
    assert stats["source"] == "original_input"
    assert np.min(weights[2 * SR:8 * SR]) > .9
    assert np.max(weights[16 * SR:22 * SR]) < .01
    premaster_weights, _ = section_profile(premaster, SR)
    assert np.max(premaster_weights) < .01


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


def test_adaptive_release_restores_the_tail_after_an_isolated_peak():
    x = np.full((SR, 2), .01)
    x[SR // 2] = 2
    fixed, _ = linked_limiter(x, SR, 150, release_mode="fixed")
    adaptive, stats = linked_limiter(x, SR, 150)
    tail = slice(round(.55 * SR), round(.65 * SR))
    assert np.mean(adaptive[tail]) > np.mean(fixed[tail]) * 1.10
    assert np.max(abs(adaptive)) <= 10 ** (finalizer.LIMITER_CEILING_DB / 20) + 1e-12
    assert stats["release_mode"] == "adaptive"


def test_adaptive_release_remembers_sustained_reduction():
    x = np.full((SR * 2, 2), .01)
    isolated = x.copy()
    isolated[SR] = 2
    sustained = x.copy()
    sustained[SR // 2:SR + 1] = 2
    a, _ = linked_limiter(isolated, SR, 150)
    b, _ = linked_limiter(sustained, SR, 150)
    tail = slice(SR + round(.08 * SR), SR + round(.15 * SR))
    assert np.mean(b[tail]) < np.mean(a[tail]) * .90
    np.testing.assert_allclose(a[:, 0], a[:, 1], atol=0)


def test_dynamic_budget_allows_below_target():
    x = music(2) * .005
    x[::11025] += .95
    _, stats = finalize(x, loudness="loud")
    assert stats["target_status"] == "below_target"
    assert stats["dynamic_budget_limited"]
    assert stats["limiter"]["max_gain_reduction_db"] <= stats["limiter_peak_budget_db"] + 1e-6
    assert stats["limiter"]["gain_reduction_p95_db"] <= stats["limiter_p95_budget_db"] + 1e-6
    assert stats["limiter"]["active_fraction"] <= stats["limiter_active_budget_fraction"] + 1e-6
    assert stats["limiter"]["gain_reduction_p50_db"] <= stats["limiter_median_gr_budget_db"] + 1e-6


def _block_peak_iqr(y, ms=50.0):
    a = np.abs(y).max(axis=1)
    w = round(SR * ms / 1000)
    blk = a[:len(a) - len(a) % w].reshape(-1, w).max(axis=1)
    return float(np.percentile(blk, 75) - np.percentile(blk, 25))


def dense_noise(seconds=4.0, band=1200.0, seed=7):
    """Peak-heavy material whose 响亮 target is out of reach: the regime where a
    depth-only budget is satisfied by limiting on every single sample."""
    rng = np.random.default_rng(seed)
    x = signal.sosfiltfilt(signal.butter(4, [100 / (SR / 2), band / (SR / 2)],
                                         btype="band", output="sos"),
                           rng.standard_normal(int(SR * seconds)))
    x = x / np.abs(x).max() * .55
    side = np.roll(x, 37) * .25
    return np.column_stack((x + side, x - side))


def test_engagement_budget_stops_before_peaks_are_pinned():
    """Depth budgets alone (p95/max GR) accept "limited everywhere": the 响亮 run
    that motivated this gate delivered active_fraction 0.96 at 5.09 dB median GR,
    pinning 84% of 50 ms block peaks to one ceiling value (IQR 0.0047) — heard as
    clipping even though no sample folds. Engagement is budgeted too."""
    y, stats = finalize(dense_noise(), loudness="loud")
    assert stats["target_status"] == "below_target"
    assert stats["dynamic_budget_limited"]
    assert stats["limiter"]["active_fraction"] <= stats["limiter_active_budget_fraction"] + 1e-6
    assert stats["limiter"]["gain_reduction_p50_db"] <= stats["limiter_median_gr_budget_db"] + 1e-6
    assert _block_peak_iqr(y) > .05


def test_depth_budgets_alone_deliver_a_pinned_master(monkeypatch):
    """Same input with the engagement gate widened off — proves the gate, not the
    input, is what keeps the peak contour alive."""
    monkeypatch.setattr(finalizer, "ACTIVE_BUDGET_FRACTION", 1.01)
    monkeypatch.setattr(finalizer, "MEDIAN_GR_BUDGET_DB", 1e9)
    y, stats = finalize(dense_noise(), loudness="loud")
    assert stats["limiter"]["gain_reduction_p95_db"] <= stats["limiter_p95_budget_db"] + 1e-6
    assert stats["limiter"]["active_fraction"] > .95
    assert _block_peak_iqr(y) < .01


def test_engagement_budget_is_inert_when_the_target_is_reachable():
    # 响亮 sits at the budget edge (active ≈ .70) since the −1.0 dBTP ceiling.
    y, stats = finalize(dense_noise(band=6000.0), loudness="normal")
    assert stats["target_met"] and not stats["dynamic_budget_limited"]
    assert stats["limiter"]["active_fraction"] < ACTIVE_BUDGET_FRACTION


def quiet_intro_dense_chorus():
    """30 s near-silent intro + 12 s dense chorus: the whole-song averages stay under
    budget while the chorus alone would be limited on every sample (2026-11-04)."""
    chorus = dense_noise(seconds=12.0)
    return np.vstack((music(30.0) * .2, chorus))


def test_local_engagement_budget_protects_a_short_dense_section():
    y, stats = finalize(quiet_intro_dense_chorus(), loudness="loud")
    limiter = stats["limiter"]
    assert stats["dynamic_budget_limited"]
    assert limiter["local_active_fraction"] <= ACTIVE_BUDGET_FRACTION + 1e-6
    assert limiter["local_gain_reduction_p50_db"] <= stats["limiter_median_gr_budget_db"] + 1e-6
    assert _block_peak_iqr(y[SR * 31:]) > .05


def test_whole_song_budget_alone_pins_the_dense_section(monkeypatch):
    """Same input with the local windows disabled: the song average passes while the
    chorus is pinned — proves the window, not the input, keeps the chorus alive."""
    monkeypatch.setattr(finalizer, "_local_engagement", lambda reduction, sr: (0.0, 0.0))
    y, stats = finalize(quiet_intro_dense_chorus(), loudness="loud")
    assert stats["limiter"]["active_fraction"] <= ACTIVE_BUDGET_FRACTION + 1e-6
    assert _block_peak_iqr(y[SR * 31:]) < .01


def test_limiter_attack_is_a_ramp_not_a_step():
    """An isolated peak is approached over 2 * ATTACK_RAMP_MS, not within 1 ms."""
    x = np.full((SR, 2), 1e-3)
    x[SR // 2] = 2.0
    y, _ = linked_limiter(x, SR, 150)
    gain = y[:SR // 2, 0] / x[:SR // 2, 0]
    required = 10 ** (finalizer.LIMITER_CEILING_DB / 20) / 2.0
    ramp = np.count_nonzero((gain < .999) & (gain > required * 1.001))
    assert ramp >= SR * 2 * finalizer.ATTACK_RAMP_MS / 1000 * .9
    assert np.max(abs(y)) <= 10 ** (finalizer.LIMITER_CEILING_DB / 20) + 1e-12


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
    def local_stage(input_wav, out_wav, loudness="normal", progress=None, cancel=None,
                    density="light", density_scope="low,mid,high"):
        calls.append(density_scope)
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
    out3 = backend.run_pipeline(src, tmp_path / "out", density_scope="high,low", **options)
    assert calls == ["low,mid,high", "low,high"]
    assert backend.read_quality_report(out3)["processing"]["density_scope"] == "low,high"


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
    def local_stage(input_wav, out_wav, loudness="normal", progress=None, cancel=None,
                    density="light", density_scope="low,mid,high"):
        return master_file(input_wav, out_wav, loudness)
    monkeypatch.setattr(backend, "stage_mastering", local_stage)
    src = tmp_path / "input.wav"
    t = np.arange(round(rate * .6)) / rate
    sf.write(src, np.column_stack((.1 * np.sin(2 * np.pi * 440 * t),) * 2), rate, subtype="FLOAT")
    dst = backend.run_pipeline(src, tmp_path / "out", style_mode="off", loudness="soft",
                               bypass=["lew", "bass", "drums", "reshape", "vocals"])
    info = sf.info(dst)
    assert info.samplerate == SR and info.channels == 2 and info.frames == round(SR * .6)


def test_soft_clip_peaks_is_linked_bounded_and_untouched_below_ceiling():
    rng = np.random.default_rng(19)
    mono = rng.standard_normal(40000) * 1.8
    stereo = np.column_stack((mono, mono * .37))
    untouched, info0 = finalizer.soft_clip_peaks(stereo, finalizer.LIMITER_CEILING_DB, 0.0)
    np.testing.assert_array_equal(untouched, stereo)
    assert not info0["applied"]

    quiet = stereo * .05
    q_out, q_info = finalizer.soft_clip_peaks(quiet, finalizer.LIMITER_CEILING_DB, 1.5)
    np.testing.assert_array_equal(q_out, quiet)
    assert not q_info["applied"]

    clipped, info = finalizer.soft_clip_peaks(stereo, finalizer.LIMITER_CEILING_DB, 1.5)
    assert info["applied"] and 0.0 < info["max_clip_gr_db"] < 1.5
    np.testing.assert_allclose(clipped[:, 1], clipped[:, 0] * .37, atol=1e-12)
    assert np.max(np.abs(clipped)) < np.max(np.abs(stereo))


def test_finalize_with_soft_clip_reduces_limiter_peak_gr_on_spiky_audio():
    # 1. 目标可达的尖峰素材：同等响度下，预限幅软削波显著降低限制器峰值减益
    x = music(2.0)
    x[SR // 2] += .45
    x[3 * SR // 2] += .45
    _, stats_plain = finalize(x, SR, "normal", soft_clip_db=0.0)
    _, stats_clip = finalize(x, SR, "normal", soft_clip_db=1.5)
    assert stats_plain["target_met"] and stats_clip["target_met"]
    assert stats_clip["true_peak_dbtp"] <= TRUE_PEAK_CEILING_DB
    assert stats_clip["limiter"]["soft_clip"]["applied"]
    assert stats_clip["limiter"]["max_gain_reduction_db"] < stats_plain["limiter"]["max_gain_reduction_db"] - 0.5

    # 2. 受动态预算约束的高频尖峰素材：相同预算下软削波换来更高可用响度
    spiky = music(2.0) * .5
    spiky[::11025] += .95
    _, s0 = finalize(spiky, SR, "normal", soft_clip_db=0.0)
    _, s1 = finalize(spiky, SR, "normal", soft_clip_db=1.5)
    assert s1["true_peak_dbtp"] <= TRUE_PEAK_CEILING_DB
    assert s1["actual_lufs"] > s0["actual_lufs"] + 0.2
