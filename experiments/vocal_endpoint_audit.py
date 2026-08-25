"""Generic, inference-free audit for already-rendered isolated-vocal endpoints.

Optional correction is deliberately narrow: one least-squares static gain and one
constant (integer + fractional) delay estimated from vocal-active segments.  It
never time-warps, normalizes, limits, or conceals a shape mismatch; diagnostics
are repeated after correction and the corrected result must satisfy the gates.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
from scipy import signal


class VocalEndpointAuditError(RuntimeError):
    """Raised when an endpoint violates an audit contract or gate."""


class AuditResult(NamedTuple):
    reference: np.ndarray
    endpoint: np.ndarray
    audited: np.ndarray
    report: dict[str, Any]


def _audio(value: np.ndarray, name: str) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name} must be a numpy.ndarray")
    if value.dtype not in (np.dtype("float32"), np.dtype("float64")):
        raise TypeError(f"{name} must be float32 or float64")
    if value.ndim not in (1, 2) or not value.shape[0] or (value.ndim == 2 and value.shape[1] not in (1, 2)):
        raise ValueError(f"{name} must be nonempty (samples,), (samples, 1), or (samples, 2)")
    if not np.isfinite(value).all():
        raise ValueError(f"{name} must contain only finite samples")
    return value


def _rate(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise TypeError("sample_rate must be a positive integer")
    if int(value) <= 0:
        raise ValueError("sample_rate must be a positive integer")
    return int(value)


def _sha(value: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(value).view(np.uint8)).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reconcile_target_shape(audio: np.ndarray, target_shape: tuple[int, ...], *, policy: str = "exact", reason: str | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Reconcile only by an explicit policy and documented reason.

    Policies are ``exact``, ``trim_tail``, ``pad_tail``, and
    ``mono_to_stereo``.  Non-exact policies reject a missing/blank reason.
    Channel loss, arbitrary cropping, and combined hidden transformations are
    never inferred.
    """
    value = _audio(audio, "audio")
    target = tuple(target_shape)
    if len(target) not in (1, 2) or target[0] <= 0 or (len(target) == 2 and target[1] not in (1, 2)):
        raise ValueError("target_shape is not a supported audio shape")
    if value.shape == target:
        if policy != "exact":
            raise ValueError("non-exact reconciliation is unexplained for an exact shape")
        return value.copy(), {"policy": "exact", "reason": None, "source_shape": list(value.shape), "target_shape": list(target)}
    if policy == "exact" or not reason or not reason.strip():
        raise VocalEndpointAuditError("unexplained target shape mismatch")
    result: np.ndarray
    if policy == "trim_tail" and value.shape[1:] == target[1:] and len(value) > target[0]:
        result = value[:target[0]].copy()
    elif policy == "pad_tail" and value.shape[1:] == target[1:] and len(value) < target[0]:
        result = np.zeros(target, dtype=value.dtype)
        result[:len(value)] = value
    elif policy == "mono_to_stereo" and len(target) == 2 and target[1] == 2 and len(value) == target[0] and (value.ndim == 1 or value.shape[1] == 1):
        mono = value if value.ndim == 1 else value[:, 0]
        result = np.column_stack((mono, mono)).astype(value.dtype, copy=False)
    else:
        raise VocalEndpointAuditError(f"policy {policy!r} cannot reconcile {value.shape} to {target}")
    return result, {"policy": policy, "reason": reason.strip(), "source_shape": list(value.shape), "target_shape": list(target)}


def _mono(value: np.ndarray) -> np.ndarray:
    return value.astype(np.float64, copy=False) if value.ndim == 1 else np.mean(value, axis=1, dtype=np.float64)


def active_mask(reference: np.ndarray, sample_rate: int, *, relative_rms: float = 0.05, absolute_rms: float = 1e-6) -> tuple[np.ndarray, dict[str, Any]]:
    """Select vocal-active samples using non-overlapping 100 ms RMS frames."""
    ref, rate = _audio(reference, "reference"), _rate(sample_rate)
    if not 0 <= relative_rms <= 1 or not math.isfinite(relative_rms) or absolute_rms <= 0 or not math.isfinite(absolute_rms):
        raise ValueError("invalid activity threshold")
    mono, width = _mono(ref), max(1, round(rate * 0.100))
    levels = np.asarray([np.sqrt(np.mean(mono[start:min(start + width, len(mono))] ** 2)) for start in range(0, len(mono), width)])
    threshold = max(float(absolute_rms), float(levels.max(initial=0)) * float(relative_rms))
    active_frames = levels >= threshold
    mask = np.repeat(active_frames, width)[:len(mono)]
    if not mask.any():
        raise VocalEndpointAuditError("no vocal-active 100ms frames")
    return mask, {"frame_ms": 100, "frame_samples": width, "threshold_rms": threshold, "active_frames": int(active_frames.sum()), "total_frames": len(levels)}


def least_squares_level(reference: np.ndarray, endpoint: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    ref, candidate = _mono(_audio(reference, "reference")), _mono(_audio(endpoint, "endpoint"))
    if ref.shape != candidate.shape or mask.shape != ref.shape or mask.dtype != bool:
        raise ValueError("level matching inputs must have identical sample shape and a boolean mask")
    denominator = float(np.dot(candidate[mask], candidate[mask]))
    if denominator <= np.finfo(float).tiny:
        raise VocalEndpointAuditError("endpoint has no energy in vocal-active frames")
    gain = float(np.dot(candidate[mask], ref[mask]) / denominator)
    if not math.isfinite(gain) or gain <= 0:
        raise VocalEndpointAuditError("least-squares gain is not finite and positive")
    return {"gain_linear": gain, "gain_db": 20 * math.log10(gain), "active_samples": int(mask.sum())}


def lag_diagnostics(reference: np.ndarray, endpoint: np.ndarray, mask: np.ndarray, *, max_lag: int = 64, min_segment_samples: int = 64) -> dict[str, Any]:
    """Report integer correlation peaks and parabolic fractional offsets per active run."""
    ref, candidate = _mono(reference), _mono(endpoint)
    if ref.shape != candidate.shape or max_lag < 1:
        raise ValueError("lag inputs or max_lag are invalid")
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    runs = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    items = []
    for start, stop in runs:
        if stop - start < max(min_segment_samples, 2 * max_lag + 3):
            continue
        a, b = ref[start:stop], candidate[start:stop]
        a, b = a - a.mean(), b - b.mean()
        corr = signal.correlate(b, a, mode="full", method="fft")
        lags = signal.correlation_lags(len(b), len(a), mode="full")
        selected = np.abs(lags) <= max_lag
        scores, local_lags = corr[selected], lags[selected]
        index = int(np.argmax(scores))
        integer = int(local_lags[index])  # positive means endpoint is delayed
        fraction = 0.0
        if 0 < index < len(scores) - 1:
            left, center, right = map(float, scores[index - 1:index + 2])
            divisor = left - 2 * center + right
            if divisor:
                fraction = float(np.clip(0.5 * (left - right) / divisor, -0.5, 0.5))
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        items.append({"start": int(start), "samples": int(stop - start), "integer_samples": integer, "fractional_samples": fraction, "lag_samples": integer + fraction, "correlation": float(scores[index] / denom) if denom else 0.0})
    if not items:
        raise VocalEndpointAuditError("insufficient vocal-active segments for lag diagnostics")
    lags_out = np.asarray([item["lag_samples"] for item in items])
    return {"segments": items, "median_lag_samples": float(np.median(lags_out)), "drift_samples": float(lags_out.max() - lags_out.min())}


def _shift(value: np.ndarray, lag: float) -> np.ndarray:
    """Advance by positive lag using linear interpolation and zero boundaries."""
    positions = np.arange(len(value), dtype=np.float64) + lag
    if value.ndim == 1:
        return np.interp(positions, np.arange(len(value)), value, left=0, right=0).astype(value.dtype)
    return np.column_stack([np.interp(positions, np.arange(len(value)), value[:, channel], left=0, right=0) for channel in range(value.shape[1])]).astype(value.dtype)


def peak_gate(audio: np.ndarray, *, sample_peak_limit: float = 1.0, true_peak_limit: float = 1.0) -> dict[str, Any]:
    """Measure sample peak and scipy polyphase 4x true peak, then apply gates."""
    value = _audio(audio, "audio").astype(np.float64, copy=False)
    if sample_peak_limit <= 0 or true_peak_limit <= 0:
        raise ValueError("peak limits must be positive")
    sample_peak = float(np.max(np.abs(value)))
    oversampled = signal.resample_poly(value, 4, 1, axis=0, window=("kaiser", 8.6), padtype="line")
    true_peak = max(sample_peak, float(np.max(np.abs(oversampled))))
    return {"sample_peak": sample_peak, "true_peak_4x": true_peak, "sample_peak_limit": float(sample_peak_limit), "true_peak_limit": float(true_peak_limit), "passed": bool(sample_peak <= sample_peak_limit and true_peak <= true_peak_limit)}


def _spatial_delta(reference: np.ndarray, endpoint: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    delta = endpoint.astype(np.float64) - reference.astype(np.float64)
    inactive = ~mask
    report: dict[str, Any] = {"inactive_delta_rms": float(np.sqrt(np.mean(delta[inactive] ** 2))) if inactive.any() else 0.0, "inactive_samples": int(inactive.sum())}
    if reference.ndim == 2 and reference.shape[1] == 2:
        def metrics(value: np.ndarray) -> dict[str, float]:
            mid, side = (value[:, 0] + value[:, 1]) * .5, (value[:, 0] - value[:, 1]) * .5
            left, right = value[:, 0] - value[:, 0].mean(), value[:, 1] - value[:, 1].mean()
            denom = np.linalg.norm(left) * np.linalg.norm(right)
            return {"stereo_correlation": float(np.dot(left, right) / denom) if denom else 0.0, "mid_rms": float(np.sqrt(np.mean(mid ** 2))), "side_rms": float(np.sqrt(np.mean(side ** 2)))}
        report.update({"reference": metrics(reference.astype(np.float64)), "endpoint": metrics(endpoint.astype(np.float64)), "delta": metrics(delta)})
    else:
        report.update({"reference": None, "endpoint": None, "delta": None})
    return report


def audit_vocal_endpoint(reference: np.ndarray, endpoint: np.ndarray, sample_rate: int, *, target_shape: tuple[int, ...] | None = None, shape_policy: str = "exact", shape_reason: str | None = None, correct: bool = False, max_lag: int = 64, max_abs_lag: float = 0.5, max_drift: float = 0.5, gain_tolerance_db: float = 0.25, min_segment_correlation: float = 0.80, sample_peak_limit: float = 1.0, true_peak_limit: float = 1.0) -> AuditResult:
    """Audit an endpoint and optionally apply/verify the documented static correction."""
    ref, candidate, rate = _audio(reference, "reference"), _audio(endpoint, "endpoint"), _rate(sample_rate)
    if isinstance(min_segment_correlation, (bool, np.bool_)) or not isinstance(min_segment_correlation, (int, float, np.integer, np.floating)):
        raise TypeError("min_segment_correlation must be a finite number between -1 and 1")
    min_segment_correlation = float(min_segment_correlation)
    if not math.isfinite(min_segment_correlation) or not -1.0 <= min_segment_correlation <= 1.0:
        raise ValueError("min_segment_correlation must be a finite number between -1 and 1")
    if target_shape is not None and tuple(ref.shape) != tuple(target_shape):
        raise VocalEndpointAuditError("reference does not have the declared exact target shape")
    candidate, shape = reconcile_target_shape(candidate, ref.shape, policy=shape_policy, reason=shape_reason)
    if candidate.dtype != ref.dtype:
        raise TypeError("reference and endpoint must have the same float dtype")
    mask, activity = active_mask(ref, rate)

    def diagnostics(value: np.ndarray) -> dict[str, Any]:
        lag = lag_diagnostics(ref, value, mask, max_lag=max_lag)
        minimum_correlation = min(item["correlation"] for item in lag["segments"])
        correlation = {
            "minimum_observed": float(minimum_correlation),
            "minimum_required": min_segment_correlation,
            "passed": bool(minimum_correlation >= min_segment_correlation),
        }
        # Estimate the one static level scalar after compensating the diagnosed
        # constant delay; otherwise decorrelated samples bias LS toward zero.
        level = least_squares_level(ref, _shift(value, lag["median_lag_samples"]), mask)
        peaks = peak_gate(value, sample_peak_limit=sample_peak_limit, true_peak_limit=true_peak_limit)
        passed = abs(level["gain_db"]) <= gain_tolerance_db and abs(lag["median_lag_samples"]) <= max_abs_lag and lag["drift_samples"] <= max_drift and correlation["passed"] and peaks["passed"]
        return {"level": level, "lag": lag, "correlation": correlation, "peaks": peaks, "spatial_and_inactive_delta": _spatial_delta(ref, value, mask), "passed": bool(passed)}

    before = diagnostics(candidate)
    audited = candidate.copy()
    correction = {"applied": False, "method": "none"}
    if correct:
        gain, lag = before["level"]["gain_linear"], before["lag"]["median_lag_samples"]
        audited = _shift(np.multiply(candidate, gain, dtype=candidate.dtype), lag)
        correction = {"applied": True, "method": "single positive LS gain then constant linear-interpolated advance; zero boundary fill", "gain_linear": gain, "gain_db": before["level"]["gain_db"], "advance_samples": lag}
    after = diagnostics(audited)
    if not after["passed"]:
        raise VocalEndpointAuditError("post-correction verification failed" if correct else "endpoint audit gates failed")
    report = {"schema_version": 1, "sample_rate": rate, "shape": list(ref.shape), "dtype": str(ref.dtype), "shape_reconciliation": shape, "activity": activity, "correction": correction, "before": before, "after": after, "hashes": {"reference_samples_sha256": _sha(ref), "endpoint_samples_sha256": _sha(candidate), "audited_samples_sha256": _sha(audited)}}
    return AuditResult(ref.copy(), candidate, audited, report)


def save_audit(result: AuditResult, output_dir: str | Path, sample_rate: int, *, overwrite: bool = False) -> dict[str, Path]:
    """Write reference/endpoint/audited FLOAT WAVs and a hash-bearing JSON report."""
    import soundfile as sf
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir must be explicit and nonempty")
    rate, destination = _rate(sample_rate), Path(output_dir).expanduser().resolve()
    if rate != result.report["sample_rate"]:
        raise ValueError("sample_rate does not match audit")
    tracks = {"reference": result.reference, "endpoint": result.endpoint, "audited": result.audited}
    paths = {name: destination / f"{name}.wav" for name in tracks}
    paths["report"] = destination / "report.json"
    existing = [path for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError("refusing to overwrite: " + ", ".join(map(str, existing)))
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in tracks.items():
        sf.write(paths[name], value, rate, format="WAV", subtype="FLOAT")
    report = dict(result.report)
    report["artifacts"] = {name: {"path": str(paths[name]), "format": "WAV", "subtype": "FLOAT", "samples_sha256": _sha(value), "wav_sha256": _file_sha(paths[name])} for name, value in tracks.items()}
    paths["report"].write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return paths
