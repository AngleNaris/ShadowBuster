"""Independent loudness/peak mastering; arrays are (samples, channels).

No Soren imports, profile assets, reference matching or spectral processing.
The limiter uses offline peak anticipation and one linked gain for both channels.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from numba import njit
from scipy import ndimage, signal

from audio_metrics import integrated_lufs
from . import ENGINE_VERSION, LOUDNESS_TARGETS, TRUE_PEAK_CEILING_DB

OVERSAMPLING = 4
LIMITER_CEILING_DB = -0.5
# Existing product budgets: p95 attenuation and worst instantaneous attenuation.
GR_BUDGETS = {"soft": (4.0, 14.0), "dynamic": (4.0, 14.0),
              "normal": (6.0, 18.0), "loud": (8.0, 20.0)}
# Budget engagement, not just depth: depth alone accepted active_fraction 0.96 at
# 5.09 dB median GR, pinning 84% of 50 ms block peaks to the ceiling — heard as clipping.
ACTIVE_BUDGET_FRACTION = 0.70
MEDIAN_GR_BUDGET_DB = 2.0


def true_peak_db(audio):
    up = signal.resample_poly(audio, OVERSAMPLING, 1, axis=0,
                              window=("kaiser", 8.6), padtype="line")
    peak = max(float(np.max(np.abs(audio))), float(np.max(np.abs(up))))
    return 20 * math.log10(peak) if peak > 0 else float("-inf")


@njit(cache=True)
def _release_envelope(required, coefficient):
    gain = np.empty_like(required)
    held = 1.0
    for index in range(len(required)):
        held = min(required[index], 1.0 - (1.0 - held) * coefficient)
        gain[index] = held
    return gain


def linked_limiter(audio, sample_rate, release_ms):
    """5 ms anticipation/hold, 1 ms attack smoothing, exponential release.

    Centered windows are possible because this is offline processing. There is
    no delay to compensate and no independent L/R detector to move the image.
    """
    peak = np.max(np.abs(audio), axis=1)
    ceiling = 10 ** (LIMITER_CEILING_DB / 20)
    required = np.minimum(1.0, ceiling / np.maximum(peak, 1e-30))
    lookahead = max(1, round(sample_rate * .005))
    preview = ndimage.minimum_filter1d(required, 2 * lookahead + 1, mode="nearest")
    attack = max(1, round(sample_rate * .001))
    smoothed = ndimage.uniform_filter1d(preview, 2 * attack + 1, mode="nearest")
    gain = _release_envelope(np.minimum(preview, smoothed),
                             math.exp(-1 / (sample_rate * release_ms / 1000)))
    out = audio * gain[:, None]
    reduction = -20 * np.log10(np.maximum(gain, 1e-30))
    stats = {
        "max_gain_reduction_db": float(np.max(reduction)),
        "gain_reduction_p50_db": float(np.percentile(reduction, 50)),
        "gain_reduction_p95_db": float(np.percentile(reduction, 95)),
        "active_fraction": float(np.mean(reduction > .1)),
        "output_peak": float(np.max(np.abs(out))),
    }
    return out, stats


def finalize(audio, sample_rate=44100, loudness="normal"):
    """Return float audio and measured statistics, before final quantization."""
    if loudness not in LOUDNESS_TARGETS:
        raise ValueError(f"Unknown loudness option: {loudness}")
    data = np.asarray(audio, dtype=np.float64)
    if sample_rate != 44100:
        raise ValueError("Mastering requires 44100 Hz; resample at the input boundary")
    if data.ndim != 2 or data.shape[1] != 2 or not len(data):
        raise ValueError("Mastering requires nonempty stereo audio")
    if not np.isfinite(data).all():
        raise ValueError("Mastering input contains non-finite samples")
    initial, method = integrated_lufs(data, sample_rate)
    if not math.isfinite(initial):
        raise ValueError("Cannot master silent or ungated audio")
    target = LOUDNESS_TARGETS[loudness]
    release_ms = 80.0 if loudness == "loud" else 150.0
    p95_budget, peak_budget = GR_BUDGETS[loudness]
    up = signal.resample_poly(data, OVERSAMPLING, 1, axis=0, window=("kaiser", 8.6))
    peak_db = 20 * math.log10(float(np.max(np.abs(up))))
    # Start with a guaranteed uncompressed candidate, then search from the
    # original samples. An infeasible trial never becomes another trial's input.
    lower = min(target - initial, LIMITER_CEILING_DB - peak_db - .02)
    upper = None
    drive = lower
    best = None
    budget_limited = False
    reason = "iteration_limit"
    for attempt in range(18):
        limited, stats = linked_limiter(up * 10 ** (drive / 20),
                                       sample_rate * OVERSAMPLING, release_ms)
        if stats["max_gain_reduction_db"] == 0:
            # If gain alone meets the ceiling, preserve the original spectrum
            # exactly instead of imposing an unnecessary resampling round trip.
            candidate = data * 10 ** (drive / 20)
        else:
            candidate = signal.resample_poly(limited, 1, OVERSAMPLING, axis=0,
                                             window=("kaiser", 8.6))[:len(data)]
        # 审计 P1-3：本次迭代对候选的整曲真峰扫描只此一次——trim 由它决定，
        # 胜出后真峰严格平移 trim dB（标量增益），LUFS 就是下面算出的 actual，
        # 收尾复用二者，不再对 result 重跑整曲 LUFS + 4× 重采样。
        peak_before_trim = true_peak_db(candidate)
        trim = min(0.0, TRUE_PEAK_CEILING_DB - .02 - peak_before_trim)
        candidate *= 10 ** (trim / 20)
        actual, _ = integrated_lufs(candidate, sample_rate)
        error = target - actual
        feasible = (stats["gain_reduction_p95_db"] <= p95_budget + 1e-6
                    and stats["max_gain_reduction_db"] <= peak_budget + 1e-6
                    and stats["active_fraction"] <= ACTIVE_BUDGET_FRACTION + 1e-6
                    and stats["gain_reduction_p50_db"] <= MEDIAN_GR_BUDGET_DB + 1e-6)
        if feasible and (best is None or abs(error) < best[0]):
            best = (abs(error), candidate, stats, drive, trim, actual, peak_before_trim)
        if feasible and abs(error) <= .1:
            reason = "target_met"
            break
        if not feasible or error < 0:
            upper = drive
            budget_limited |= not feasible
        else:
            lower = drive
        if upper is not None:
            if upper - lower < .02:
                reason = "dynamic_budget" if budget_limited else "converged"
                break
            drive = (lower + upper) / 2
        else:
            next_drive = min(LIMITER_CEILING_DB - peak_db + peak_budget,
                             drive + float(np.clip(error * 1.4, .5, 4)))
            if next_drive <= drive:
                reason, budget_limited = "dynamic_budget", True
                break
            drive = next_drive
    if best is None:
        raise RuntimeError("No safe mastering candidate")
    _, result, limiter_stats, drive, trim, actual_lufs, peak_before_trim = best
    stats = {
        "engine": ENGINE_VERSION, "style_mode": "off", "spectral_processing": False,
        "loudness_option": loudness, "loudness_target_source": "application_preset",
        "target_lufs": target, "input_lufs": initial, "loudness_method": method,
        "stop_reason": reason, "dynamic_budget_limited": budget_limited,
        "limiter_p95_budget_db": p95_budget, "limiter_peak_budget_db": peak_budget,
        "limiter_active_budget_fraction": ACTIVE_BUDGET_FRACTION,
        "limiter_median_gr_budget_db": MEDIAN_GR_BUDGET_DB,
        "limiter_release_ms": release_ms, "limiter": limiter_stats,
        "iterations": attempt + 1, "applied_gain_db": drive, "safety_trim_db": trim,
        "true_peak_ceiling_dbtp": TRUE_PEAK_CEILING_DB,
    }
    # result 即胜出候选（已含 trim）。LUFS 复用循环里对同一数组算出的 actual_lufs
    # （逐位相同）；真峰 = peak_before_trim + trim（标量增益在实数域严格平移，
    # 浮点差 ~1e-15 dB，远低于 0.01 dB 报告口径，且在 write_output 路径会被回读
    # 测量覆盖）。收尾不再对 result 重跑整曲 LUFS + 4× 重采样真峰。
    _apply_measurements(stats, actual_lufs, peak_before_trim + trim)
    return result, stats


def _apply_measurements(stats, actual, true_peak_dbtp):
    """Record loudness/peak outcomes into stats. Values may be freshly measured
    (`_update_measurements`) or reused from the search loop (`finalize`, audit
    P1-3) — the target-status logic is identical either way."""
    error = stats["target_lufs"] - actual
    met = abs(error) <= .2
    stats.update(actual_lufs=actual, target_error_lu=abs(error),
                 target_signed_error_lu=error, target_met=met,
                 target_status="met" if met else ("below_target" if error > 0 else "above_target"),
                 true_peak_dbtp=true_peak_dbtp)
    if met:
        stats.update(stop_reason="target_met", dynamic_budget_limited=False)


def _update_measurements(audio, sample_rate, stats):
    _apply_measurements(stats, integrated_lufs(audio, sample_rate)[0], true_peak_db(audio))


def master_file(input_path, output_path, loudness="normal"):
    """Write PCM24 with TPDF dither exactly once, then verify the actual file."""
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise ValueError("Input and output must be different files")
    audio, sr = sf.read(input_path, dtype="float64", always_2d=True)
    result, stats = finalize(audio, sr, loudness)
    return write_output(result, sr, output_path, stats)


def write_output(result, sr, output_path, stats):
    """Common quantization boundary for no-style and protected reference paths."""
    rng = np.random.default_rng()
    dither = (rng.random(result.shape) - rng.random(result.shape)) / 2 ** 23
    sf.write(output_path, result + dither, sr, subtype="PCM_24")
    written, written_sr = sf.read(output_path, dtype="float64", always_2d=True)
    if written.shape != result.shape or written_sr != sr:
        raise RuntimeError("Mastering output shape/sample rate changed")
    _update_measurements(written, written_sr, stats)
    if stats["true_peak_dbtp"] > TRUE_PEAK_CEILING_DB:
        raise RuntimeError("Written mastering output exceeds true peak ceiling")
    stats.update(output_subtype=sf.info(output_path).subtype, output_sample_rate=sr,
                 output_frames=len(written), readback_verified=True)
    Path(str(output_path) + ".mastering.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return stats
