"""Isolated strict vocal-replacement affine experiment.

This module performs no separation, normalization, limiting, mastering, or
production-pipeline integration.  It only validates already-rendered tracks,
verifies that the two vocal stems are exactly time aligned, and evaluates
``y = x + alpha * (v_repaired - v_original)``.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np


DEFAULT_ALPHA = 1.0


class VocalReplacementError(RuntimeError):
    """Raised when the strict vocal replacement contract is violated."""


class VocalReplacementRender(NamedTuple):
    mix: np.ndarray
    vocal_original: np.ndarray
    vocal_repaired: np.ndarray
    replacement_delta: np.ndarray
    processed: np.ndarray
    report: dict[str, Any]


def _strict_audio(value: np.ndarray, name: str) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name} must be a numpy.ndarray")
    if value.dtype not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise TypeError(f"{name} must have dtype float32 or float64")
    if value.ndim == 1:
        valid = value.shape[0] > 0
    elif value.ndim == 2:
        valid = value.shape[0] > 0 and value.shape[1] in (1, 2)
    else:
        valid = False
    if not valid:
        raise ValueError(
            f"{name} must be nonempty and shaped (samples,), (samples, 1), or (samples, 2)"
        )
    if not np.isfinite(value).all():
        raise ValueError(f"{name} must contain only finite samples")
    return value


def _strict_rate(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise TypeError(f"{name} must be a positive integer")
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_parameter(name: str, value: float, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def _rms(value: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(value, dtype=np.float64))))


def _array_sha256(value: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(value).view(np.uint8)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _signal_report(value: np.ndarray) -> dict[str, Any]:
    absolute = np.abs(value)
    peak = float(np.max(absolute))
    return {
        "peak_linear": peak,
        "clipped": bool(peak > 1.0),
        "clipped_samples": int(np.count_nonzero(absolute > 1.0)),
        "samples_sha256": _array_sha256(value),
    }


def verify_vocal_alignment(
    vocal_original: np.ndarray,
    vocal_repaired: np.ndarray,
    original_rate: int,
    repaired_rate: int,
    *,
    max_lag: int = 64,
    windows: int = 7,
    min_active_windows: int = 3,
    min_correlation: float = 0.80,
    active_rms: float = 1.0e-6,
    active_relative: float = 0.05,
) -> dict[str, Any]:
    """Verify zero lag in multiple vocal-active windows.

    Every accepted window must peak at exactly zero samples.  Consequently a
    constant offset and time-varying drift both fail instead of being repaired
    by cropping, padding, shifting, or resampling.
    """
    original = _strict_audio(vocal_original, "vocal_original")
    repaired = _strict_audio(vocal_repaired, "vocal_repaired")
    original_rate = _strict_rate(original_rate, "original_rate")
    repaired_rate = _strict_rate(repaired_rate, "repaired_rate")
    if original_rate != repaired_rate:
        raise ValueError(
            f"vocal sample rates differ: original={original_rate}, repaired={repaired_rate}"
        )
    if original.shape != repaired.shape:
        raise ValueError(
            f"vocal_repaired shape {repaired.shape} does not match vocal_original {original.shape}"
        )
    if original.dtype != repaired.dtype:
        raise TypeError("vocal_original and vocal_repaired must have the same float dtype")

    max_lag, windows, min_active_windows = int(max_lag), int(windows), int(min_active_windows)
    if max_lag < 1 or windows < 2 or min_active_windows < 2:
        raise ValueError("max_lag must be positive and alignment requires at least two windows")
    if min_active_windows > windows:
        raise ValueError("min_active_windows must not exceed windows")
    min_correlation = _finite_parameter("min_correlation", min_correlation, minimum=0.0)
    if min_correlation > 1.0:
        raise ValueError("min_correlation must be <= 1.0")
    active_rms = _finite_parameter("active_rms", active_rms, minimum=np.finfo(float).eps)
    active_relative = _finite_parameter("active_relative", active_relative, minimum=0.0)
    if active_relative > 1.0:
        raise ValueError("active_relative must be <= 1.0")

    original_2d = original[:, None] if original.ndim == 1 else original
    repaired_2d = repaired[:, None] if repaired.ndim == 1 else repaired
    sample_count = len(original_2d)
    width = min(max(1024, sample_count // windows), sample_count - 2 * max_lag)
    if width < 64:
        raise VocalReplacementError("vocal tracks are too short for strict multiwindow alignment")
    starts = np.unique(
        np.linspace(max_lag, sample_count - max_lag - width, windows, dtype=int)
    )
    levels = [_rms(original_2d[start:start + width]) for start in starts]
    threshold = max(active_rms, (max(levels) if levels else 0.0) * active_relative)

    checks: list[dict[str, Any]] = []
    for window_index, (start, level) in enumerate(zip(starts, levels)):
        if level < threshold:
            continue
        stop = int(start + width)
        reference = original_2d[start:stop].astype(np.float64, copy=False)
        reference = reference - np.mean(reference, axis=0, keepdims=True)
        reference_flat = reference.reshape(-1)
        scores: list[float] = []
        for lag in range(-max_lag, max_lag + 1):
            candidate = repaired_2d[start + lag:stop + lag].astype(np.float64, copy=False)
            candidate = candidate - np.mean(candidate, axis=0, keepdims=True)
            candidate_flat = candidate.reshape(-1)
            denominator = np.linalg.norm(reference_flat) * np.linalg.norm(candidate_flat)
            scores.append(
                float(np.dot(reference_flat, candidate_flat) / denominator)
                if denominator > np.finfo(float).tiny else float("nan")
            )
        finite = np.isfinite(scores)
        if not finite.any():
            continue
        peak_index = int(np.argmax(np.where(finite, scores, -np.inf)))
        peak_lag = peak_index - max_lag
        correlation = scores[peak_index]
        checks.append({
            "window": int(window_index),
            "start": int(start),
            "samples": int(width),
            "original_rms": float(level),
            "peak_lag": int(peak_lag),
            "correlation": float(correlation),
            "passed": bool(peak_lag == 0 and correlation >= min_correlation),
        })

    lags = [item["peak_lag"] for item in checks]
    enough_windows = len(checks) >= min_active_windows
    zero_lag = bool(checks) and all(lag == 0 for lag in lags)
    correlation_passed = bool(checks) and all(
        item["correlation"] >= min_correlation for item in checks
    )
    drift_samples = int(max(lags) - min(lags)) if lags else None
    result = {
        "passed": bool(enough_windows and zero_lag and correlation_passed),
        "sample_rate": original_rate,
        "max_lag": max_lag,
        "requested_windows": windows,
        "active_windows": len(checks),
        "min_active_windows": min_active_windows,
        "activity_threshold_rms": float(threshold),
        "min_correlation": min_correlation,
        "zero_lag": zero_lag,
        "drift_samples": drift_samples,
        "windows": checks,
    }
    if not result["passed"]:
        raise VocalReplacementError(
            "vocal tracks failed strict zero-lag multiwindow alignment: "
            + json.dumps(result, allow_nan=False)
        )
    return result


def render_vocal_replacement(
    mix: np.ndarray,
    vocal_original: np.ndarray,
    vocal_repaired: np.ndarray,
    mix_rate: int,
    original_rate: int,
    repaired_rate: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    max_lag: int = 64,
    alignment_windows: int = 7,
    min_active_windows: int = 3,
    min_correlation: float = 0.80,
    active_rms: float = 1.0e-6,
    active_relative: float = 0.05,
) -> VocalReplacementRender:
    """Apply the exact affine vocal replacement without modifying its level."""
    x = _strict_audio(mix, "mix")
    original = _strict_audio(vocal_original, "vocal_original")
    repaired = _strict_audio(vocal_repaired, "vocal_repaired")
    rates = (
        _strict_rate(mix_rate, "mix_rate"),
        _strict_rate(original_rate, "original_rate"),
        _strict_rate(repaired_rate, "repaired_rate"),
    )
    if len(set(rates)) != 1:
        raise ValueError(
            f"sample rates must match exactly: mix={rates[0]}, original={rates[1]}, repaired={rates[2]}"
        )
    if original.shape != x.shape or repaired.shape != x.shape:
        raise ValueError(
            f"all shapes must match exactly: mix={x.shape}, original={original.shape}, repaired={repaired.shape}"
        )
    if original.dtype != x.dtype or repaired.dtype != x.dtype:
        raise TypeError("mix and vocal tracks must have the same float dtype")
    alpha = _finite_parameter("alpha", alpha, minimum=0.0)

    if alpha == 0.0:
        delta = np.zeros_like(x)
        processed = x.copy()
        alignment: dict[str, Any] = {"status": "not_run_exact_bypass"}
        bypass = True
    else:
        alignment = verify_vocal_alignment(
            original,
            repaired,
            rates[1],
            rates[2],
            max_lag=max_lag,
            windows=alignment_windows,
            min_active_windows=min_active_windows,
            min_correlation=min_correlation,
            active_rms=active_rms,
            active_relative=active_relative,
        )
        stem_difference = np.subtract(repaired, original, dtype=x.dtype)
        scaled_difference = np.multiply(stem_difference, alpha, dtype=x.dtype)
        processed = np.add(x, scaled_difference, dtype=x.dtype)
        if not np.isfinite(processed).all():
            raise VocalReplacementError("affine result contains non-finite samples")
        # Report the exact delta represented by the returned affine result.
        delta = np.subtract(processed, x, dtype=x.dtype)
        bypass = False

    expected = np.add(x, delta, dtype=x.dtype)
    identity_exact = bool(np.array_equal(processed, expected))
    if not identity_exact:
        raise VocalReplacementError("internal affine identity failure")
    tracks = {
        "mix": x,
        "vocal_original": original,
        "vocal_repaired": repaired,
        "replacement_delta": delta,
        "processed": processed,
    }
    report = {
        "experiment": "strict_vocal_replacement",
        "formula": "y = x + alpha * (v_repaired - v_original)",
        "sample_rate": rates[0],
        "samples": int(x.shape[0]),
        "channels": 1 if x.ndim == 1 else int(x.shape[1]),
        "dtype": str(x.dtype),
        "alpha": alpha,
        "bypass": bypass,
        "normalization": False,
        "limiting": False,
        "alignment": alignment,
        "affine_identity": {
            "identity": "processed == mix + replacement_delta",
            "exact": identity_exact,
            "max_abs_error": 0.0,
        },
        "signals": {name: _signal_report(value) for name, value in tracks.items()},
    }
    return VocalReplacementRender(
        x.copy(), original.copy(), repaired.copy(), delta, processed, report
    )


def save_render(
    render: VocalReplacementRender,
    output_dir: str | Path,
    sample_rate: int,
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write FLOAT WAV artifacts only beneath one explicit output directory."""
    import soundfile as sf

    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir must be explicit and nonempty")
    sample_rate = _strict_rate(sample_rate, "sample_rate")
    if sample_rate != int(render.report["sample_rate"]):
        raise ValueError("artifact sample_rate does not match render sample_rate")
    destination = Path(output_dir).expanduser().resolve()
    tracks = {
        "mix": render.mix,
        "vocal_original": render.vocal_original,
        "vocal_repaired": render.vocal_repaired,
        "replacement_delta": render.replacement_delta,
        "processed": render.processed,
    }
    paths = {name: destination / f"{name}.wav" for name in tracks}
    paths["report"] = destination / "report.json"
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("refusing to overwrite: " + ", ".join(map(str, existing)))
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in tracks.items():
        sf.write(paths[name], value, sample_rate, format="WAV", subtype="FLOAT")

    report = dict(render.report)
    report["artifacts"] = {
        name: {
            "path": str(paths[name]),
            "samples_sha256": _array_sha256(value),
            "wav_sha256": _file_sha256(paths[name]),
            "bytes": paths[name].stat().st_size,
            "format": "WAV",
            "subtype": "FLOAT",
        }
        for name, value in tracks.items()
    }
    paths["report"].write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return paths
