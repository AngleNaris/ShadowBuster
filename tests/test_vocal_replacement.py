from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from experiments import vocal_replacement as experiment


SR = 44_100


def _active_tracks(*, samples: int = 28_000, dtype=np.float32):
    rng = np.random.default_rng(12345)
    vocal = rng.normal(0.0, 0.08, samples).astype(dtype)
    repaired = (vocal + rng.normal(0.0, 0.002, samples).astype(dtype)).astype(dtype)
    mix = rng.normal(0.0, 0.15, samples).astype(dtype)
    return mix, vocal, repaired


def _shift(audio: np.ndarray, samples: int) -> np.ndarray:
    shifted = np.zeros_like(audio)
    if samples > 0:
        shifted[samples:] = audio[:-samples]
    else:
        shifted[:samples] = audio[-samples:]
    return shifted


def _drift_windows(audio: np.ndarray) -> np.ndarray:
    result = audio.copy()
    midpoint = len(audio) // 2
    result[midpoint + 1:] = audio[midpoint:-1]
    result[midpoint] = 0.0
    return result


def test_alpha_zero_is_exact_bypass_and_skips_alignment(monkeypatch):
    mix = np.array([0.25, -0.5, 0.75], dtype=np.float32)
    original = np.zeros_like(mix)
    repaired = np.array([np.nan, 0.0, 0.0], dtype=np.float32)
    with pytest.raises(ValueError, match="finite"):
        experiment.render_vocal_replacement(mix, original, repaired, SR, SR, SR, alpha=0.0)

    repaired = _shift(original, 1)
    called = False

    def must_not_align(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("alignment must not run for alpha=0")

    monkeypatch.setattr(experiment, "verify_vocal_alignment", must_not_align)
    render = experiment.render_vocal_replacement(
        mix, original, repaired, SR, SR, SR, alpha=0.0
    )
    assert called is False
    assert np.array_equal(render.processed, mix)
    assert np.array_equal(render.replacement_delta, np.zeros_like(mix))
    assert render.processed is not mix
    assert render.report["bypass"] is True
    assert render.report["alignment"]["status"] == "not_run_exact_bypass"
    assert render.report["affine_identity"]["exact"] is True


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_exact_affine_identity_preserves_float_dtype_without_level_control(dtype):
    mix, original, repaired = _active_tracks(dtype=dtype)
    mix[0], original[0], repaired[0] = dtype(0.9), dtype(-0.7), dtype(0.7)
    alpha = 0.75
    render = experiment.render_vocal_replacement(
        mix, original, repaired, SR, SR, SR, alpha=alpha
    )
    expected = np.add(
        mix,
        np.multiply(np.subtract(repaired, original, dtype=dtype), alpha, dtype=dtype),
        dtype=dtype,
    )
    assert np.array_equal(render.processed, expected)
    assert np.array_equal(render.processed, mix + render.replacement_delta)
    assert render.processed.dtype == dtype
    assert render.report["normalization"] is False
    assert render.report["limiting"] is False
    assert render.report["signals"]["processed"]["clipped"] is True
    assert render.report["signals"]["processed"]["peak_linear"] > 1.0


@pytest.mark.parametrize("lag", [-7, -1, 1, 11])
def test_alignment_rejects_nonzero_lag(lag):
    _, original, repaired = _active_tracks()
    with pytest.raises(experiment.VocalReplacementError, match="zero-lag multiwindow"):
        experiment.verify_vocal_alignment(original, _shift(repaired, lag), SR, SR)


def test_alignment_rejects_drift_between_active_windows():
    _, original, repaired = _active_tracks()
    with pytest.raises(experiment.VocalReplacementError, match="zero-lag multiwindow") as caught:
        experiment.verify_vocal_alignment(original, _drift_windows(repaired), SR, SR)
    assert '"drift_samples": 1' in str(caught.value)


def test_alignment_reports_multiple_vocal_active_zero_lag_windows():
    _, original, repaired = _active_tracks()
    report = experiment.verify_vocal_alignment(original, repaired, SR, SR)
    assert report["passed"] is True
    assert report["zero_lag"] is True
    assert report["drift_samples"] == 0
    assert report["active_windows"] >= 3
    assert all(item["peak_lag"] == 0 for item in report["windows"])


def test_alignment_rejects_insufficient_vocal_activity():
    original = np.zeros(28_000, dtype=np.float32)
    repaired = original.copy()
    with pytest.raises(experiment.VocalReplacementError, match="zero-lag multiwindow"):
        experiment.verify_vocal_alignment(original, repaired, SR, SR)


@pytest.mark.parametrize("bad", [
    np.array([], dtype=np.float32),
    np.zeros((8, 0), dtype=np.float32),
    np.zeros((8, 3), dtype=np.float32),
    np.zeros((2, 4, 1), dtype=np.float32),
    np.zeros(8, dtype=np.int16),
    np.array([0.0, np.inf], dtype=np.float32),
])
def test_rejects_invalid_audio(bad):
    valid = np.zeros(8, dtype=np.float32)
    with pytest.raises((TypeError, ValueError)):
        experiment.render_vocal_replacement(bad, valid, valid, SR, SR, SR, alpha=0.0)


def test_rejects_shape_rate_dtype_and_alpha_mismatch():
    valid = np.zeros(2048, dtype=np.float32)
    with pytest.raises(ValueError, match="shapes must match"):
        experiment.render_vocal_replacement(valid, valid[:-1], valid, SR, SR, SR)
    with pytest.raises(ValueError, match="sample rates must match"):
        experiment.render_vocal_replacement(valid, valid, valid, SR, 48_000, SR)
    with pytest.raises(TypeError, match="same float dtype"):
        experiment.render_vocal_replacement(valid, valid.astype(np.float64), valid, SR, SR, SR)
    for alpha in (-0.1, np.nan, np.inf):
        with pytest.raises(ValueError, match="alpha"):
            experiment.render_vocal_replacement(valid, valid, valid, SR, SR, SR, alpha=alpha)


def test_float_artifacts_hashes_and_overwrite_protection(tmp_path):
    mix, original, repaired = _active_tracks(samples=12_000)
    render = experiment.render_vocal_replacement(
        mix, original, repaired, SR, SR, SR, alpha=0.5,
        alignment_windows=5, min_active_windows=3,
    )
    output = tmp_path / "explicit-output"
    paths = experiment.save_render(render, output, SR)
    for name in (
        "mix", "vocal_original", "vocal_repaired", "replacement_delta", "processed"
    ):
        info = sf.info(paths[name])
        assert info.format == "WAV"
        assert info.subtype == "FLOAT"
        assert info.samplerate == SR
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert set(report["artifacts"]) == {
        "mix", "vocal_original", "vocal_repaired", "replacement_delta", "processed"
    }
    for artifact in report["artifacts"].values():
        assert artifact["subtype"] == "FLOAT"
        assert len(artifact["samples_sha256"]) == 64
        assert len(artifact["wav_sha256"]) == 64
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        experiment.save_render(render, output, SR)
    experiment.save_render(render, output, SR, overwrite=True)


def test_save_requires_explicit_output_dir_and_matching_rate():
    mix = np.zeros(128, dtype=np.float32)
    render = experiment.render_vocal_replacement(mix, mix, mix, SR, SR, SR, alpha=0.0)
    with pytest.raises(ValueError, match="explicit"):
        experiment.save_render(render, "", SR)
    with pytest.raises(ValueError, match="does not match"):
        experiment.save_render(render, "out", 48_000)
