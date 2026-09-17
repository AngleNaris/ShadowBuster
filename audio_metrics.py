"""Production audio measurements and bounded quality reporting.

The module deliberately keeps measurement separate from processing.  It is used by
both the desktop backend and the headless CLI, while the Soren mastering sidecar
remains an engine-owned compatibility contract.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import soundfile as sf
try:
    from scipy import signal
except ImportError:  # desktop shell excludes scipy; analysis runs in audio runtime
    signal = None

def _require_signal():
    if signal is None:
        raise RuntimeError("scipy is required for production audio measurement")
    return signal


QUALITY_SCHEMA_VERSION = 1
QUALITY_REPORT_SUFFIX = ".quality.json"
BANDS_HZ = ((0, 4_000), (4_000, 8_000), (8_000, 12_000),
            (12_000, 16_000), (16_000, 20_000), (20_000, 22_000))


def _db(value: float, *, power: bool = False) -> float | None:
    if not math.isfinite(value) or value <= 0:
        return None
    return (10.0 if power else 20.0) * math.log10(value)


def _finite_or_none(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _k_weight(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    sig = _require_signal()
    # BS.1770 K-weighting: high shelf followed by RLB high pass.
    shelf_b, shelf_a = sig.bilinear(
        [1.53512485958697,
         2 * math.pi * 1681.974450955533 * 1.69065929318241,
         (2 * math.pi * 1681.974450955533) ** 2],
        [1.0,
         2 * math.pi * 1681.974450955533 * 1.69065929318241,
         (2 * math.pi * 1681.974450955533) ** 2], fs=sample_rate)
    high_b, high_a = sig.bilinear(
        [1.0, 0.0, 0.0],
        [1.0, 2 * math.pi * 38.13547087602444,
         (2 * math.pi * 38.13547087602444) ** 2], fs=sample_rate)
    weighted = sig.lfilter(shelf_b, shelf_a, audio, axis=0)
    return sig.lfilter(high_b, high_a, weighted, axis=0)


def _block_loudness(audio: np.ndarray, sample_rate: int,
                    block_seconds: float = 0.400,
                    hop_seconds: float = 0.100) -> np.ndarray:
    if len(audio) == 0:
        return np.empty(0, dtype=np.float64)
    weighted = _k_weight(audio, sample_rate)
    block = max(1, int(round(block_seconds * sample_rate)))
    hop = max(1, int(round(hop_seconds * sample_rate)))
    if len(weighted) < block:
        starts = (0,)
        block = len(weighted)
    else:
        starts = range(0, len(weighted) - block + 1, hop)
    energies = np.asarray([
        float(np.mean(np.sum(weighted[start:start + block] ** 2, axis=1)))
        for start in starts
    ], dtype=np.float64)
    return -0.691 + 10.0 * np.log10(np.maximum(energies, 1e-300))


def integrated_lufs(audio: np.ndarray, sample_rate: int) -> tuple[float, str]:
    data = np.asarray(audio, dtype=np.float64)
    try:
        import pyloudnorm as pyln  # type: ignore
        value = float(pyln.Meter(sample_rate).integrated_loudness(data))
        if math.isfinite(value) or value == float("-inf"):
            return value, "pyloudnorm"
    except (ImportError, AttributeError, RuntimeError, ValueError, FloatingPointError):
        pass
    levels = _block_loudness(data, sample_rate)
    absolute = levels >= -70.0
    if not absolute.any():
        return float("-inf"), "bs1770_fallback"
    preliminary = float(-0.691 + 10.0 * math.log10(
        np.mean(10.0 ** ((levels[absolute] + 0.691) / 10.0))))
    gated = absolute & (levels >= preliminary - 10.0)
    if not gated.any():
        return float("-inf"), "bs1770_fallback"
    level = float(-0.691 + 10.0 * math.log10(
        np.mean(10.0 ** ((levels[gated] + 0.691) / 10.0))))
    return level, "bs1770_fallback"


def _loudness_range(short_term: np.ndarray) -> float | None:
    finite = short_term[np.isfinite(short_term) & (short_term > -70.0)]
    if finite.size < 2:
        return None
    # EBU-style robust range: gate 10 LU below the mean, then take 10/95 percentiles.
    gated = finite[finite >= np.mean(finite) - 10.0]
    if gated.size < 2:
        gated = finite
    return float(np.percentile(gated, 95) - np.percentile(gated, 10))


def _mean_spectrum(data: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Whole-file Hann power spectrum with bounded FFT working memory."""
    size = min(4096, len(data))
    hop = max(1, size // 2)
    window = np.hanning(size) if size > 2 else np.ones(size)
    power = np.zeros(size // 2 + 1)
    count = 0
    for start in range(0, max(1, len(data) - size + 1), hop):
        spectrum = np.fft.rfft(data[start:start + size] * window[:, None], axis=0)
        power += np.mean(np.abs(spectrum) ** 2, axis=1)
        count += 1
    last = max(0, len(data) - size)
    if last % hop:
        spectrum = np.fft.rfft(data[last:] * window[:, None], axis=0)
        power += np.mean(np.abs(spectrum) ** 2, axis=1)
        count += 1
    return np.fft.rfftfreq(size, 1.0 / sample_rate), power / count


def _band_energies(data: np.ndarray, sample_rate: int, spectrum=None) -> dict[str, dict[str, float]]:
    frequencies, power = _mean_spectrum(data, sample_rate) if spectrum is None else spectrum
    total = float(np.sum(power))
    result: dict[str, dict[str, float]] = {}
    for low, high in BANDS_HZ:
        effective_high = min(float(high), sample_rate / 2.0)
        selected = (frequencies >= low) & (frequencies < effective_high)
        value = float(np.sum(power[selected])) if effective_high > low else 0.0
        key = f"{low // 1000}-{high // 1000}k"
        result[key] = {
            "relative_energy_db": float(10 * math.log10(value / total)) if value > 0 and total > 0 else -300.0,
            "fraction": value / total if total else 0.0,
        }
    return result


def _low_band_ratio(data: np.ndarray, sample_rate: int) -> float | None:
    if data.shape[1] != 2:
        return None
    mid = (data[:, 0] + data[:, 1]) * 0.5
    side = (data[:, 0] - data[:, 1]) * 0.5
    sig = _require_signal()
    sos = sig.butter(4, [20.0, min(180.0, sample_rate / 2.0 - 1.0)],
                        btype="bandpass", fs=sample_rate, output="sos")
    mid = sig.sosfiltfilt(sos, mid, padlen=0)
    side = sig.sosfiltfilt(sos, side, padlen=0)
    mid_rms = float(np.sqrt(np.mean(mid * mid)))
    side_rms = float(np.sqrt(np.mean(side * side)))
    return side_rms / mid_rms if mid_rms > 1e-12 else None


def measure_audio(audio: np.ndarray, sample_rate: int) -> dict[str, Any]:
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
        raise ValueError("audio must be a nonempty [frames, channels] array")
    if sample_rate < 8_000:
        raise ValueError("sample rate is too low for measurement")
    if not np.isfinite(data).all():
        raise ValueError("audio contains NaN or infinity")

    loudness, loudness_backend = integrated_lufs(data, sample_rate)
    sample_peak = float(np.max(np.abs(data)))
    oversampled = _require_signal().resample_poly(data, 4, 1, axis=0,
                                       window=("kaiser", 8.6), padtype="line")
    true_peak = max(sample_peak, float(np.max(np.abs(oversampled))))
    rms = float(np.sqrt(np.mean(data ** 2)))
    short_term = _block_loudness(data, sample_rate, 3.0, 1.0)
    momentary = _block_loudness(data, sample_rate, 0.400, 0.100)

    stereo_corr = None
    side_mid = None
    mono_loss = None
    if data.shape[1] == 2:
        left = data[:, 0] - np.mean(data[:, 0])
        right = data[:, 1] - np.mean(data[:, 1])
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        stereo_corr = float(np.dot(left, right) / denominator) if denominator else 0.0
        mid = (data[:, 0] + data[:, 1]) * 0.5
        side = (data[:, 0] - data[:, 1]) * 0.5
        mid_energy = float(np.mean(mid ** 2))
        side_energy = float(np.mean(side ** 2))
        ratio = math.sqrt(side_energy / mid_energy) if mid_energy else None
        side_mid = {"ratio": ratio, "db": _db(ratio) if ratio is not None else None}
        stereo_energy = float(np.mean(data ** 2))
        mono_energy = float(np.mean(mid ** 2))
        mono_loss = (10.0 * math.log10(mono_energy / stereo_energy)
                     if stereo_energy > 1e-30 and mono_energy > 1e-30 else None)

    clipping = int(np.count_nonzero(np.abs(data) >= 0.99999))
    spectrum = _mean_spectrum(data, sample_rate)
    frequencies, power = spectrum
    dc_offset = np.mean(data, axis=0)
    return {
        "sample_rate": int(sample_rate),
        "channels": int(data.shape[1]),
        "frames": int(data.shape[0]),
        "duration_seconds": float(data.shape[0] / sample_rate),
        "integrated_lufs": loudness,
        "loudness_backend": loudness_backend,
        "short_term_lufs": {
            "min": _finite_or_none(np.min(short_term)) if short_term.size else None,
            "max": _finite_or_none(np.max(short_term)) if short_term.size else None,
            "p50": _finite_or_none(np.percentile(short_term, 50)) if short_term.size else None,
        },
        "momentary_lufs": {
            "min": _finite_or_none(np.min(momentary)) if momentary.size else None,
            "max": _finite_or_none(np.max(momentary)) if momentary.size else None,
        },
        "lra_lu": _loudness_range(short_term),
        "sample_peak": sample_peak,
        "sample_peak_dbfs": _db(sample_peak),
        "true_peak_4x": true_peak,
        "true_peak_4x_dbtp": _db(true_peak),
        "rms": rms,
        "crest_factor_db": _db(sample_peak / rms) if rms else None,
        "stereo_correlation": stereo_corr,
        "side_mid": side_mid,
        "low_band_side_mid": {
            "ratio": _low_band_ratio(data, sample_rate),
        },
        "mono_fold_down_loss_db": mono_loss,
        "band_energies": _band_energies(data, sample_rate, spectrum),
        "spectrum_measurement": "whole-file mean Hann power spectrum, 4096 samples / 50% overlap",
        "spectral_centroid_hz": float(np.sum(frequencies * power) / max(float(np.sum(power)), 1e-30)),
        "dc_offset": [float(v) for v in dc_offset],
        "clipping_samples": clipping,
    }


def _constraint(status: str, value: Any = None, reason: str = "") -> dict[str, Any]:
    return {"status": status, "value": value, "reason": reason}


def assess_quality(input_metrics: Mapping[str, Any], output_metrics: Mapping[str, Any],
                   mastering_stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    peak = output_metrics.get("true_peak_4x_dbtp")
    corr = output_metrics.get("stereo_correlation")
    mono_loss = output_metrics.get("mono_fold_down_loss_db")
    constraints: dict[str, Any] = {}
    constraints["finite_audio"] = _constraint("pass", True)
    constraints["true_peak"] = (_constraint("pass", peak) if peak is None or peak <= -0.4
                                 else _constraint("fail", peak, "true peak exceeds -0.4 dBTP"))
    constraints["sample_clipping"] = (_constraint("pass", output_metrics.get("clipping_samples"))
                                      if output_metrics.get("clipping_samples") == 0
                                      else _constraint("fail", output_metrics.get("clipping_samples"), "samples at digital full scale"))
    constraints["stereo_correlation"] = (_constraint("pass", corr) if corr is None or corr >= 0.0
                                          else _constraint("warn", corr, "negative broadband correlation"))
    constraints["mono_fold_down"] = (_constraint("pass", mono_loss) if mono_loss is None or mono_loss >= -3.0
                                      else _constraint("warn", mono_loss, "mono fold-down loses more than 3 dB"))
    if mastering_stats:
        error = mastering_stats.get("target_error_lu")
        constraints["loudness_target"] = (_constraint("pass", error) if error is not None and abs(float(error)) <= 0.2
                                           else _constraint("warn", error, "integrated loudness is outside target tolerance"))
        if mastering_stats.get("dynamic_budget_limited"):
            constraints["dynamic_budget"] = _constraint("warn", True, "limiter budget limited target convergence")
        else:
            constraints["dynamic_budget"] = _constraint("pass", False)
    statuses = [v["status"] for v in constraints.values()]
    overall = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    return {"overall": overall, "constraints": constraints}


def quality_report_path(wav_path: str | Path) -> Path:
    return Path(str(wav_path) + QUALITY_REPORT_SUFFIX)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _noise_quality(stages: Mapping[str, Any]) -> dict[str, Any] | None:
    noise = stages.get("reshape", {}).get("noise", {})
    if noise.get("mode") != "adaptive_all":
        return None
    budget = noise.get("mix_budget", {})
    if not noise.get("applied"):
        return _constraint("unknown", None, "adaptive denoise not applied; no noise-removal claim")
    change = _finite_or_none(budget.get("band_energy_change_db"))
    cap = _finite_or_none(budget.get("max_attenuation_db"))
    if change is None or cap is None:
        return _constraint("unknown", None, "adaptive denoise band budget unavailable")
    valid = -cap - 1e-6 <= change <= 1e-6
    return _constraint("pass" if valid else "fail", change,
                       "denoise-only band energy before width/mastering; not a perceptual quality score")


THIRD_OCTAVE_CENTERS = (25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0,
                        160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0,
                        1000.0, 1250.0, 1600.0, 2000.0, 2500.0, 3150.0, 4000.0,
                        5000.0, 6300.0, 8000.0, 10000.0, 12500.0, 16000.0, 20000.0)


def _third_octave_levels(data: np.ndarray, sample_rate: int) -> tuple[list, list]:
    """1/3 倍频程频段电平（相对本文件总能量，dB）——响度无关的音色分布，
    供报告页做处理前后对比；两份文件用同一积分方式保证可比。"""
    frequencies, power = _mean_spectrum(data, sample_rate)
    total = float(np.sum(power))
    centers: list = []
    levels: list = []
    for center in THIRD_OCTAVE_CENTERS:
        if center >= sample_rate / 2.0:
            break
        low = center * 2.0 ** (-1.0 / 6.0)
        high = center * 2.0 ** (1.0 / 6.0)
        selected = (frequencies >= low) & (frequencies < high)
        value = float(np.sum(power[selected]))
        centers.append(int(center) if float(center).is_integer() else center)
        levels.append(float(10 * math.log10(value / total))
                      if value > 0 and total > 0 else -90.0)
    return centers, levels


def write_quality_report(input_wav: str | Path, output_wav: str | Path,
                         *, params: Mapping[str, Any] | None = None,
                         mastering_stats: Mapping[str, Any] | None = None,
                         stage_reports: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source, source_sr = sf.read(str(input_wav), dtype="float64", always_2d=True)
    rendered, rendered_sr = sf.read(str(output_wav), dtype="float64", always_2d=True)
    input_metrics = measure_audio(source, int(source_sr))
    output_metrics = measure_audio(rendered, int(rendered_sr))
    report = {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "input": {"path": str(input_wav), "metrics": input_metrics},
        "output": {"path": str(output_wav), "metrics": output_metrics},
        "processing": _json_safe(dict(params or {})),
        "stages": _json_safe(dict(stage_reports or {})),
        "mastering": _json_safe(dict(mastering_stats or {})) if mastering_stats else None,
        "quality": assess_quality(input_metrics, output_metrics, mastering_stats),
    }
    # 1/3 倍频程前后对比（各自相对总能量，响度无关）——报告页主图数据源
    centers, in_oct = _third_octave_levels(source, int(source_sr))
    _, out_oct = _third_octave_levels(rendered, int(rendered_sr))
    report["compare"] = {"centers": centers, "input_db": in_oct,
                         "output_db": out_oct,
                         "note": "relative to each file's total energy; loudness-independent"}
    noise_constraint = _noise_quality(stage_reports or {})
    if noise_constraint is not None:
        report["quality"]["constraints"]["denoise_band_budget"] = noise_constraint
        if noise_constraint["status"] == "fail":
            report["quality"]["overall"] = "fail"
        elif noise_constraint["status"] == "unknown" and report["quality"]["overall"] == "pass":
            report["quality"]["overall"] = "warn"
    path = quality_report_path(output_wav)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_json_safe(report), stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return report


def read_quality_report(wav_path: str | Path) -> dict[str, Any] | None:
    try:
        value = json.loads(quality_report_path(wav_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value else None


def quality_summary(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    metrics = report.get("output", {}).get("metrics", {})
    quality = report.get("quality", {})
    return {
        "overall": quality.get("overall"),
        "integrated_lufs": metrics.get("integrated_lufs"),
        "true_peak_4x_dbtp": metrics.get("true_peak_4x_dbtp"),
        "lra_lu": metrics.get("lra_lu"),
        "crest_factor_db": metrics.get("crest_factor_db"),
        "stereo_correlation": metrics.get("stereo_correlation"),
        "mono_fold_down_loss_db": metrics.get("mono_fold_down_loss_db"),
        "report_path": report.get("output", {}).get("path", "") + QUALITY_REPORT_SUFFIX,
    }
