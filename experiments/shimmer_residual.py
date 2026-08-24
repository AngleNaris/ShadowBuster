"""Strict, experiment-only residual adapter for the external Shimmer tree.

This module deliberately does not import Shimmer at module import time.  The
caller supplies the checkout directory, and the endpoint is rendered exactly
once before any residual strength is applied externally.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, NamedTuple

import numpy as np


PRESET_NAME = "suno_hash"
DEFAULT_STRENGTH = 0.25
DEFAULT_REMOVED_ATOL = 2.0e-5
DEFAULT_REMOVED_RTOL = 1.0e-4


class ShimmerResidualError(RuntimeError):
    """Raised when Shimmer violates the strict residual contract."""


class ResidualRender(NamedTuple):
    dry: np.ndarray
    cleaned: np.ndarray
    removed: np.ndarray
    processed: np.ndarray
    report: dict[str, Any]


def _strict_audio(audio: np.ndarray) -> np.ndarray:
    if not isinstance(audio, np.ndarray):
        raise TypeError("audio must be a numpy.ndarray")
    if audio.dtype != np.float32:
        raise TypeError("audio must have dtype float32")
    if audio.ndim == 1:
        if audio.shape[0] == 0:
            raise ValueError("audio must not be empty")
    elif audio.ndim == 2:
        if audio.shape[0] == 0:
            raise ValueError("audio must not be empty")
        if audio.shape[1] not in (1, 2):
            raise ValueError("audio must be sample-first mono or stereo")
    else:
        raise ValueError("audio must be shaped (samples,), (samples, 1), or (samples, 2)")
    if not np.isfinite(audio).all():
        raise ValueError("audio must contain only finite samples")
    return audio


def _strict_result(name: str, value: Any, shape: tuple[int, ...]) -> np.ndarray:
    result = np.asarray(value)
    if result.dtype != np.float32:
        raise ShimmerResidualError(f"Shimmer {name} must have dtype float32")
    if result.shape != shape:
        raise ShimmerResidualError(
            f"Shimmer {name} shape {result.shape} does not match input {shape}"
        )
    if not np.isfinite(result).all():
        raise ShimmerResidualError(f"Shimmer {name} contains non-finite samples")
    return result


def load_shimmer_api(shimmer_dir: str | Path) -> tuple[Callable[[str], Any], Callable[..., Any]]:
    """Dynamically load the required API from one explicit Shimmer checkout."""
    root = Path(shimmer_dir).expanduser().resolve()
    package = root / "shimmer"
    if not package.is_dir():
        raise FileNotFoundError(f"Shimmer package directory not found: {package}")

    cached = sys.modules.get("shimmer")
    if cached is not None:
        cached_file = Path(getattr(cached, "__file__", "")).resolve()
        try:
            cached_file.relative_to(package)
        except (ValueError, OSError):
            raise ShimmerResidualError(
                f"a different Shimmer package is already loaded from {cached_file}"
            )

    root_text = str(root)
    sys.path.insert(0, root_text)
    try:
        presets = importlib.import_module("shimmer.presets")
        pipeline = importlib.import_module("shimmer.pipeline")
    finally:
        try:
            sys.path.remove(root_text)
        except ValueError:
            pass

    return presets.get_preset, pipeline.clean_and_master


def _neutralize_preset(preset: Any, *, disable_tone_kill: bool) -> None:
    """Remove every internal blend, cosmetic, compensation, and random stage."""
    values = {
        "mix": 1.0,
        "fade_ms": 0.0,
        "noise_resynth": 0.0,
        "high_shelf_hz": 0.0,
        "high_shelf_db": 0.0,
        "subsonic_hz": 0.0,
        "presence_hz": 0.0,
        "presence_db": 0.0,
        "lowmid_hz": 0.0,
        "lowmid_db": 0.0,
        "swc_threshold_db": 1.0e9,
        "swc_max_makeup_db": 0.0,
    }
    if disable_tone_kill:
        values["tone_kill"] = 0.0
    for name, value in values.items():
        if not hasattr(preset, name):
            raise ShimmerResidualError(f"Shimmer preset lacks required field {name!r}")
        setattr(preset, name, value)


def _peak_report(value: np.ndarray) -> dict[str, Any]:
    absolute = np.abs(value)
    peak = float(np.max(absolute))
    return {
        "peak_linear": peak,
        "clipped": bool(peak > 1.0),
        "clipped_samples": int(np.count_nonzero(absolute > 1.0)),
    }


def render_residual(
    audio: np.ndarray,
    sample_rate: int,
    *,
    shimmer_dir: str | Path,
    scale: float = DEFAULT_STRENGTH,
    disable_tone_kill: bool = True,
    removed_atol: float = DEFAULT_REMOVED_ATOL,
    removed_rtol: float = DEFAULT_REMOVED_RTOL,
    api_loader: Callable[[str | Path], tuple[Callable[[str], Any], Callable[..., Any]]] | None = None,
) -> ResidualRender:
    """Render Shimmer's full-clean endpoint once and scale its residual outside.

    No normalization, volume preservation, clipping protection, mastering,
    automatic tone curve, or user EQ is applied.  ``scale=0`` returns a copy
    that is bit-for-bit equal to the input through an explicit bypass branch.
    """
    x = _strict_audio(audio)
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, np.integer)):
        raise TypeError("sample_rate must be a positive integer")
    if int(sample_rate) <= 0:
        raise ValueError("sample_rate must be a positive integer")
    scale = float(scale)
    if not np.isfinite(scale) or not 0.0 <= scale <= 1.0:
        raise ValueError("scale must be finite and in [0, 1]")
    if removed_atol < 0.0 or removed_rtol < 0.0:
        raise ValueError("removed tolerances must be non-negative")

    if scale == 0.0:
        dry = x.copy()
        cleaned = x.copy()
        removed = np.zeros_like(x)
        processed = x.copy()
        report = {
            "adapter": "strict_shimmer_residual",
            "preset": PRESET_NAME,
            "sample_rate": int(sample_rate),
            "samples": int(x.shape[0]),
            "channels": 1 if x.ndim == 1 else int(x.shape[1]),
            "scale": scale,
            "bypass": True,
            "shimmer_endpoint_rendered": False,
            "tone_kill_disabled": bool(disable_tone_kill),
            "full_residual_validation": {
                "status": "not_run_bypass",
                "atol": float(removed_atol),
                "rtol": float(removed_rtol),
                "max_abs_error": None,
            },
            "applied_affine_identity": {
                "identity": "dry - processed == removed",
                "max_abs_error": 0.0,
            },
            "neutralized": {
                "status": "not_run_bypass",
                "internal_mix": "not_run",
                "fade": "not_run",
                "post_filters": "not_run",
                "side_compensation": "not_run",
                "noise_resynth": "not_run",
                "mastering_and_tone_curve": "not_run",
                "user_eq": "not_run",
            },
            "clipping": {name: _peak_report(value) for name, value in {
                "dry": dry,
                "cleaned": cleaned,
                "removed": removed,
                "processed": processed,
            }.items()},
            "shimmer_params": {},
            "shimmer_report": {},
        }
        return ResidualRender(dry, cleaned, removed, processed, report)

    loader = api_loader or load_shimmer_api
    get_preset, clean_and_master = loader(shimmer_dir)
    preset = get_preset(PRESET_NAME)
    _neutralize_preset(preset, disable_tone_kill=disable_tone_kill)

    engine_input = x[:, None] if x.ndim == 1 else x
    cleaned_raw, shimmer_removed_raw, shimmer_report = clean_and_master(
        engine_input,
        int(sample_rate),
        preset,
        master_params=None,
        raw_analysis=None,
        eq_params=None,
    )
    cleaned_engine = _strict_result(
        "cleaned endpoint", cleaned_raw, engine_input.shape
    )
    shimmer_removed_engine = _strict_result(
        "removed signal", shimmer_removed_raw, engine_input.shape
    )
    cleaned = cleaned_engine[:, 0] if x.ndim == 1 else cleaned_engine
    shimmer_removed = (
        shimmer_removed_engine[:, 0] if x.ndim == 1 else shimmer_removed_engine
    )
    full_residual = np.subtract(x, cleaned, dtype=np.float32)

    if not np.allclose(
        shimmer_removed, full_residual,
        atol=float(removed_atol), rtol=float(removed_rtol),
    ):
        max_error = float(np.max(np.abs(shimmer_removed - full_residual)))
        raise ShimmerResidualError(
            "Shimmer-reported removed signal fails x-cleaned null: "
            f"max_error={max_error:.9g}, atol={removed_atol:g}, rtol={removed_rtol:g}"
        )

    if scale == 1.0:
        processed = cleaned.copy()  # preserve the exact rendered endpoint
    else:
        processed = np.subtract(
            x,
            np.multiply(full_residual, np.float32(scale), dtype=np.float32),
            dtype=np.float32,
        )
    if not np.isfinite(processed).all():
        raise ShimmerResidualError("externally scaled result contains non-finite samples")

    # This is the residual actually applied, including float32 endpoint
    # accounting. It is intentionally derived from the returned processed track.
    removed = np.subtract(x, processed, dtype=np.float32)
    affine_error = float(np.max(np.abs((x - processed) - removed)))

    params_report = asdict(preset) if is_dataclass(preset) else dict(vars(preset))
    report = {
        "adapter": "strict_shimmer_residual",
        "preset": PRESET_NAME,
        "sample_rate": int(sample_rate),
        "samples": int(x.shape[0]),
        "channels": 1 if x.ndim == 1 else int(x.shape[1]),
        "scale": scale,
        "bypass": False,
        "shimmer_endpoint_rendered": True,
        "tone_kill_disabled": bool(disable_tone_kill),
        "full_residual_validation": {
            "status": "passed",
            "identity": "dry - cleaned == shimmer_reported_removed",
            "atol": float(removed_atol),
            "rtol": float(removed_rtol),
            "max_abs_error": float(np.max(np.abs(shimmer_removed - full_residual))),
        },
        "applied_affine_identity": {
            "identity": "dry - processed == removed",
            "max_abs_error": affine_error,
        },
        "neutralized": {
            "internal_mix": True,
            "fade": True,
            "post_filters": True,
            "side_compensation": True,
            "noise_resynth": True,
            "mastering_and_tone_curve": True,
            "user_eq": True,
        },
        "clipping": {name: _peak_report(value) for name, value in {
            "dry": x, "cleaned": cleaned, "removed": removed, "processed": processed
        }.items()},
        "shimmer_params": params_report,
        "shimmer_report": shimmer_report if isinstance(shimmer_report, Mapping) else {},
    }
    return ResidualRender(x.copy(), cleaned.copy(), removed, processed, report)


def _array_sha256(value: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(value)
    return hashlib.sha256(contiguous.view(np.uint8)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_render(
    render: ResidualRender,
    output_dir: str | Path,
    sample_rate: int,
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Save all four tracks as IEEE FLOAT WAV and a hash/report JSON."""
    import soundfile as sf

    destination = Path(output_dir).expanduser().resolve()
    paths = {name: destination / f"{name}.wav" for name in (
        "dry", "cleaned", "removed", "processed"
    )}
    paths["report"] = destination / "report.json"
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("refusing to overwrite: " + ", ".join(map(str, existing)))
    destination.mkdir(parents=True, exist_ok=True)

    tracks = {
        "dry": render.dry,
        "cleaned": render.cleaned,
        "removed": render.removed,
        "processed": render.processed,
    }
    for name, value in tracks.items():
        sf.write(paths[name], value, int(sample_rate), format="WAV", subtype="FLOAT")

    report = dict(render.report)
    report["hashes"] = {
        name: {
            "float32_samples_sha256": _array_sha256(value),
            "wav_file_sha256": _file_sha256(paths[name]),
        }
        for name, value in tracks.items()
    }
    paths["report"].write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return paths
