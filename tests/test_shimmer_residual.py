from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from experiments import shimmer_residual as adapter
from experiments.shimmer_residual_cli import build_parser


SR = 44100
SHIMMER_DIR = Path("unused-by-mock")


def _preset() -> SimpleNamespace:
    return SimpleNamespace(
        mix=0.2,
        fade_ms=5.0,
        noise_resynth=0.15,
        high_shelf_hz=12000.0,
        high_shelf_db=-2.0,
        subsonic_hz=25.0,
        presence_hz=6000.0,
        presence_db=1.0,
        lowmid_hz=300.0,
        lowmid_db=-1.0,
        swc_threshold_db=3.0,
        swc_max_makeup_db=1.5,
        tone_kill=0.4,
    )


def _mock_loader(cleaned: np.ndarray, reported_removed: np.ndarray | None = None, calls=None):
    preset = _preset()
    removed = (
        np.subtract(_input_for(cleaned), cleaned, dtype=np.float32)
        if reported_removed is None else reported_removed
    )

    def get_preset(name):
        assert name == "suno_hash"
        return preset

    def clean_and_master(x, sr, received_preset, **kwargs):
        if calls is not None:
            calls.append((x, sr, received_preset, kwargs))
        assert received_preset is preset
        cleaned_out = cleaned[:, None] if cleaned.ndim == 1 else cleaned
        removed_out = removed[:, None] if removed.ndim == 1 else removed
        return cleaned_out.copy(), removed_out.copy(), {"engine": "mock"}

    return lambda _directory: (get_preset, clean_and_master), preset


def _input_for(cleaned: np.ndarray) -> np.ndarray:
    # Test endpoints use this fixed offset unless they supply explicit removed.
    return np.add(cleaned, np.float32(0.125), dtype=np.float32)


def test_strict_zero_bypass_does_not_load_or_run_shimmer():
    x = np.linspace(-0.5, 0.5, 64, dtype=np.float32).reshape(32, 2)
    loader_calls = []

    def must_not_load(directory):
        loader_calls.append(directory)
        raise AssertionError("Shimmer loader must not run for scale=0")

    result = adapter.render_residual(
        x, SR, shimmer_dir=SHIMMER_DIR, scale=0.0, api_loader=must_not_load
    )

    assert loader_calls == []
    assert np.array_equal(result.dry, x)
    assert np.array_equal(result.cleaned, x)
    assert np.array_equal(result.processed, x)
    assert np.array_equal(result.removed, np.zeros_like(x))
    assert np.count_nonzero(result.removed) == 0
    assert result.dry is not x
    assert result.cleaned is not x
    assert result.processed is not x
    assert result.report["bypass"] is True
    assert result.report["shimmer_endpoint_rendered"] is False
    assert result.report["full_residual_validation"]["status"] == "not_run_bypass"
    assert result.report["applied_affine_identity"]["max_abs_error"] == 0.0
    assert result.report["neutralized"]["status"] == "not_run_bypass"


def test_external_affine_scaling_and_exact_clean_endpoint():
    cleaned = np.linspace(-0.3, 0.3, 16, dtype=np.float32)
    x = _input_for(cleaned)
    calls = []
    loader, preset = _mock_loader(cleaned, calls=calls)

    quarter = adapter.render_residual(
        x, SR, shimmer_dir=SHIMMER_DIR, scale=0.25, api_loader=loader
    )
    expected = np.subtract(
        x, np.multiply(x - cleaned, np.float32(0.25), dtype=np.float32), dtype=np.float32
    )
    assert np.array_equal(quarter.processed, expected)
    assert np.array_equal(quarter.removed, x - quarter.processed)
    assert np.allclose(quarter.removed, np.float32(0.25) * (x - cleaned))
    assert np.array_equal(quarter.cleaned, cleaned)
    assert quarter.report["full_residual_validation"]["status"] == "passed"
    assert quarter.report["applied_affine_identity"]["max_abs_error"] == 0.0
    assert len(calls) == 1
    _, sr, _, kwargs = calls[0]
    assert sr == SR
    assert kwargs == {"master_params": None, "raw_analysis": None, "eq_params": None}
    assert preset.mix == 1.0
    assert preset.fade_ms == 0.0
    assert preset.noise_resynth == 0.0
    assert preset.swc_max_makeup_db == 0.0
    assert preset.swc_threshold_db == 1.0e9
    assert preset.tone_kill == 0.0
    for field in ("high_shelf_hz", "high_shelf_db", "subsonic_hz",
                  "presence_hz", "presence_db", "lowmid_hz", "lowmid_db"):
        assert getattr(preset, field) == 0.0

    endpoint = adapter.render_residual(
        x, SR, shimmer_dir=SHIMMER_DIR, scale=1.0, api_loader=loader
    )
    assert np.array_equal(endpoint.processed, cleaned)
    assert np.array_equal(endpoint.removed, x - endpoint.processed)
    assert np.array_equal(endpoint.removed, x - cleaned)


@pytest.mark.parametrize("bad", [
    np.empty((0,), dtype=np.float32),
    np.zeros((8, 0), dtype=np.float32),
    np.zeros((8, 3), dtype=np.float32),
    np.zeros((1, 8, 1), dtype=np.float32),
    np.zeros(8, dtype=np.float64),
    np.array([0.0, np.nan], dtype=np.float32),
    np.array([0.0, np.inf], dtype=np.float32),
])
def test_rejects_invalid_input_before_loading_engine(bad):
    def must_not_load(_):
        raise AssertionError("engine was loaded for invalid input")

    with pytest.raises((TypeError, ValueError)):
        adapter.render_residual(
            bad, SR, shimmer_dir=SHIMMER_DIR, api_loader=must_not_load
        )


def test_accepts_only_mono_or_sample_first_stereo():
    for shape in ((16,), (16, 1), (16, 2)):
        cleaned = np.zeros(shape, dtype=np.float32)
        x = _input_for(cleaned)
        loader, _ = _mock_loader(cleaned)
        result = adapter.render_residual(x, SR, shimmer_dir=SHIMMER_DIR, api_loader=loader)
        assert result.processed.shape == shape
        assert result.processed.dtype == np.float32
        assert np.isfinite(result.processed).all()


def test_rejects_removed_null_mismatch():
    cleaned = np.zeros(16, dtype=np.float32)
    x = _input_for(cleaned)
    wrong = np.zeros_like(x)
    loader, _ = _mock_loader(cleaned, wrong)
    with pytest.raises(adapter.ShimmerResidualError, match="fails x-cleaned null"):
        adapter.render_residual(x, SR, shimmer_dir=SHIMMER_DIR, api_loader=loader)


def test_rejects_bad_engine_shape_and_nonfinite_output():
    x = np.zeros((16, 2), dtype=np.float32)

    def loader_with(cleaned):
        def run(*_args, **_kwargs):
            removed = np.zeros_like(cleaned)
            return cleaned, removed, {}
        return lambda _: (lambda _name: _preset(), run)

    with pytest.raises(adapter.ShimmerResidualError, match="shape"):
        adapter.render_residual(
            x, SR, shimmer_dir=SHIMMER_DIR,
            api_loader=loader_with(np.zeros((16, 1), dtype=np.float32)),
        )
    nonfinite = x.copy()
    nonfinite[0, 0] = np.nan
    with pytest.raises(adapter.ShimmerResidualError, match="non-finite"):
        adapter.render_residual(
            x, SR, shimmer_dir=SHIMMER_DIR, api_loader=loader_with(nonfinite)
        )


def test_float_wav_metadata_hash_report_and_overwrite_guard(tmp_path):
    cleaned = np.linspace(-1.2, 1.2, 20, dtype=np.float32).reshape(10, 2)
    x = _input_for(cleaned)
    loader, _ = _mock_loader(cleaned)
    result = adapter.render_residual(
        x, SR, shimmer_dir=SHIMMER_DIR, scale=1.0, api_loader=loader
    )
    paths = adapter.save_render(result, tmp_path, SR)

    for name in ("dry", "cleaned", "removed", "processed"):
        info = sf.info(paths[name])
        assert info.format == "WAV"
        assert info.subtype == "FLOAT"
        assert info.samplerate == SR
        assert info.channels == 2
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert report["clipping"]["processed"]["clipped"] is True
    assert report["clipping"]["processed"]["peak_linear"] > 1.0
    assert set(report["hashes"]) == {"dry", "cleaned", "removed", "processed"}
    for hashes in report["hashes"].values():
        assert len(hashes["float32_samples_sha256"]) == 64
        assert len(hashes["wav_file_sha256"]) == 64

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        adapter.save_render(result, tmp_path, SR)
    adapter.save_render(result, tmp_path, SR, overwrite=True)


def test_cli_requires_output_dir_and_uses_conservative_default():
    parser = build_parser()
    args = parser.parse_args([
        "input.wav", "--output-dir", "out", "--shimmer-dir", "shimmer"
    ])
    assert args.strength == 0.25
    assert args.output_dir == Path("out")
    with pytest.raises(SystemExit):
        parser.parse_args(["input.wav", "--shimmer-dir", "shimmer"])
