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

from audio_metrics import integrated_lufs, oversampling_filter
from . import ENGINE_VERSION, LOUDNESS_TARGETS, TRUE_PEAK_CEILING_DB

OVERSAMPLING = 4
# Limiter target sits 0.1 dB under the true-peak ceiling; the downsampling overshoot
# left over is removed by the scalar safety trim.
LIMITER_CEILING_DB = TRUE_PEAK_CEILING_DB - 0.1
# Existing product budgets: p95 attenuation and worst instantaneous attenuation.
GR_BUDGETS = {"soft": (4.0, 14.0), "dynamic": (4.0, 14.0),
              "normal": (6.0, 18.0), "loud": (8.0, 20.0)}
# Budget engagement, not just depth: depth alone accepted active_fraction 0.96 at
# 5.09 dB median GR, pinning 84% of 50 ms block peaks to the ceiling — heard as clipping.
ACTIVE_BUDGET_FRACTION = 0.70
MEDIAN_GR_BUDGET_DB = 2.0
# Engagement budgets also hold for the worst window, not only the song average:
# a quiet intro diluted a 100%-active final chorus to a whole-song 0.48 (2026-11-04).
LOCAL_WINDOW_SECONDS = 10.0
LOOKAHEAD_MS = 10.0
ATTACK_RAMP_MS = 5.0
RELEASE_FAST_RATIO = .25
RELEASE_MEMORY_MS = 50.0
# Approved E-variant default for independent mastering pipeline (2026-11-23 listening):
# 4x oversampled stereo-linked C1 peak shaving before linked_limiter.
DEFAULT_SOFT_CLIP_DB = 1.2


def true_peak_db(audio):
    up = signal.resample_poly(audio, OVERSAMPLING, 1, axis=0,
                              window=oversampling_filter(OVERSAMPLING), padtype="line")
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


@njit(cache=True)
def _adaptive_release_envelope(required, fast_coefficient, slow_coefficient, memory_coefficient):
    """Short peaks recover quickly; sustained reduction leaves a slower memory."""
    gain = np.empty_like(required)
    fast_gain = 1.0
    slow_depth = 0.0
    for index in range(len(required)):
        fast_gain = min(required[index], 1.0 - (1.0 - fast_gain) * fast_coefficient)
        depth = 1.0 - required[index]
        coefficient = memory_coefficient if depth > slow_depth else slow_coefficient
        slow_depth = depth + (slow_depth - depth) * coefficient
        gain[index] = min(fast_gain, 1.0 - slow_depth)
    return gain


def _local_engagement(reduction, sample_rate):
    """Worst sliding-window (1 s hop) active fraction and median GR.

    GR moves no faster than the 10 ms attack ramp, so 1 ms decimation is exact enough."""
    points = reduction[::max(1, round(sample_rate * .001))]
    width = min(len(points), round(LOCAL_WINDOW_SECONDS * 1000))
    windows = np.lib.stride_tricks.sliding_window_view(points, width)[::1000]
    return (float(np.max(np.mean(windows > .1, axis=1))),
            float(np.max(np.median(windows, axis=1))))


def soft_clip_peaks(audio, ceiling_db=LIMITER_CEILING_DB, soft_clip_db=0.0):
    """Stereo-linked, C1-smooth, bounded pre-limiter peak shaver (4x oversampled domain).

    Below ``ceiling_db`` the signal is untouched (gain = 1.0). Above ``ceiling_db``,
    excess ``e = 20*log10(peak / ceiling)`` is smoothly shaved by
    ``delta_db = soft_clip_db * (1 - 1 / cosh(e / soft_clip_db))``:
      - C1 continuous at the ceiling (``delta'(0) = 0``, zero knee kink);
      - strictly monotonic (``output' >= 0.5`` everywhere, never folds);
      - strictly bounded attenuation (< ``soft_clip_db``), so sustained loud peaks
        still flow cleanly into ``linked_limiter`` and its dynamic budgets;
      - stereo-linked single gain curve (preserves L/R channel ratio identically).
    """
    if not math.isfinite(soft_clip_db) or not 0.0 <= soft_clip_db <= 6.0:
        raise ValueError(f"soft_clip_db must be finite within [0, 6], got {soft_clip_db!r}")
    if soft_clip_db == 0.0 or len(audio) == 0:
        return audio, {"applied": False, "soft_clip_db": 0.0,
                       "clipped_fraction": 0.0, "max_clip_gr_db": 0.0}
    ceiling = 10 ** (ceiling_db / 20.0)
    peak = np.max(np.abs(audio), axis=1)
    over = peak > ceiling
    if not np.any(over):
        return audio, {"applied": False, "soft_clip_db": float(soft_clip_db),
                       "clipped_fraction": 0.0, "max_clip_gr_db": 0.0}
    excess_db = 20.0 * np.log10(peak[over] / ceiling)
    # 1 / cosh(u) = 2 * exp(-u) / (1 + exp(-2u)) avoids float overflow for large u.
    u = excess_db / soft_clip_db
    e_neg = np.exp(-u)
    sech = (2.0 * e_neg) / (1.0 + e_neg * e_neg)
    gr_over = soft_clip_db * (1.0 - sech)
    gain = np.ones_like(peak)
    gain[over] = 10.0 ** (-gr_over / 20.0)
    out = audio * gain[:, None]
    return out, {
        "applied": True,
        "soft_clip_db": float(soft_clip_db),
        "clipped_fraction": float(np.mean(over)),
        "max_clip_gr_db": float(np.max(gr_over)),
    }


def linked_limiter(audio, sample_rate, release_ms, *, release_mode="adaptive"):
    """10 ms anticipation/hold, 10 ms attack ramp, dual-timescale recovery.

    The ramp is a moving average of half-width ATTACK_RAMP_MS <= LOOKAHEAD_MS over
    the held minimum, so the gain at every peak still meets that peak's ceiling.
    A 1 ms ramp modulated the whole band like a soft clipper on dense material.

    Centered windows are possible because this is offline processing. There is
    no delay to compensate and no independent L/R detector to move the image.
    """
    peak = np.max(np.abs(audio), axis=1)
    ceiling = 10 ** (LIMITER_CEILING_DB / 20)
    required = np.minimum(1.0, ceiling / np.maximum(peak, 1e-30))
    lookahead = max(1, round(sample_rate * LOOKAHEAD_MS / 1000))
    preview = ndimage.minimum_filter1d(required, 2 * lookahead + 1, mode="nearest")
    attack = max(1, round(sample_rate * ATTACK_RAMP_MS / 1000))
    smoothed = ndimage.uniform_filter1d(preview, 2 * attack + 1, mode="nearest")
    required_gain = np.minimum(required, smoothed)
    slow_coefficient = math.exp(-1 / (sample_rate * release_ms / 1000))
    if release_mode == "fixed":
        gain = _release_envelope(required_gain, slow_coefficient)
    elif release_mode == "adaptive":
        gain = _adaptive_release_envelope(
            required_gain,
            math.exp(-1 / (sample_rate * release_ms * RELEASE_FAST_RATIO / 1000)),
            slow_coefficient, math.exp(-1 / (sample_rate * RELEASE_MEMORY_MS / 1000)))
    else:
        raise ValueError(f"Unknown limiter release mode: {release_mode}")
    out = audio * gain[:, None]
    reduction = -20 * np.log10(np.maximum(gain, 1e-30))
    local_active, local_p50 = _local_engagement(reduction, sample_rate)
    stats = {
        "max_gain_reduction_db": float(np.max(reduction)),
        "gain_reduction_p50_db": float(np.percentile(reduction, 50)),
        "gain_reduction_p95_db": float(np.percentile(reduction, 95)),
        "active_fraction": float(np.mean(reduction > .1)),
        "local_active_fraction": local_active,
        "local_gain_reduction_p50_db": local_p50,
        "output_peak": float(np.max(np.abs(out))),
        "release_mode": release_mode,
        "fast_release_ms": release_ms * RELEASE_FAST_RATIO,
        "release_memory_ms": RELEASE_MEMORY_MS,
    }
    return out, stats


def finalize(audio, sample_rate=44100, loudness="normal", *, soft_clip_db=0.0):
    """Return float audio and measured statistics, before final quantization."""
    if loudness not in LOUDNESS_TARGETS:
        raise ValueError(f"Unknown loudness option: {loudness}")
    if not math.isfinite(soft_clip_db) or not 0.0 <= soft_clip_db <= 6.0:
        raise ValueError(f"soft_clip_db must be finite within [0, 6], got {soft_clip_db!r}")
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
    up = signal.resample_poly(data, OVERSAMPLING, 1, axis=0,
                              window=oversampling_filter(OVERSAMPLING))
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
        driven = up * 10 ** (drive / 20)
        if soft_clip_db > 0.0:
            driven, clip_stats = soft_clip_peaks(driven, LIMITER_CEILING_DB, soft_clip_db)
        else:
            clip_stats = None
        limited, stats = linked_limiter(driven, sample_rate * OVERSAMPLING, release_ms)
        if clip_stats is not None:
            stats["soft_clip"] = clip_stats
        untouched = stats["max_gain_reduction_db"] == 0 and not (clip_stats and clip_stats["applied"])
        if untouched:
            # If gain alone meets the ceiling, preserve the original spectrum
            # exactly instead of imposing an unnecessary resampling round trip.
            candidate = data * 10 ** (drive / 20)
        else:
            candidate = signal.resample_poly(limited, 1, OVERSAMPLING, axis=0,
                                             window=oversampling_filter(OVERSAMPLING))[:len(data)]
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
                    and stats["gain_reduction_p50_db"] <= MEDIAN_GR_BUDGET_DB + 1e-6
                    and stats["local_active_fraction"] <= ACTIVE_BUDGET_FRACTION + 1e-6
                    and stats["local_gain_reduction_p50_db"] <= MEDIAN_GR_BUDGET_DB + 1e-6)
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
        "limiter_local_window_seconds": LOCAL_WINDOW_SECONDS,
        "limiter_lookahead_ms": LOOKAHEAD_MS, "limiter_attack_ramp_ms": ATTACK_RAMP_MS,
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
    target_range = stats.get("target_lufs_range")
    if target_range is None:
        met, below = abs(error) <= .2, error > 0
    else:
        lower, upper = target_range
        met, below = lower <= actual <= upper, actual < lower
    stats.update(actual_lufs=actual, target_error_lu=abs(error),
                 target_signed_error_lu=error, target_met=met,
                 target_status="met" if met else ("below_target" if below else "above_target"),
                 true_peak_dbtp=true_peak_dbtp)
    if met:
        stats.update(stop_reason="target_met", dynamic_budget_limited=False)


def _update_measurements(audio, sample_rate, stats):
    _apply_measurements(stats, integrated_lufs(audio, sample_rate)[0], true_peak_db(audio))


def master_file(input_path, output_path, loudness="normal"):
    """Write PCM24 with TPDF dither exactly once, then verify the actual file."""
    if Path(input_path).resolve() == Path(output_path).resolve():
        raise ValueError("Input and output must be different files")
    if loudness == "loud":
        from .pipeline import master_file as master
        return master(input_path, output_path, loudness, density="off")
    audio, sr = sf.read(input_path, dtype="float64", always_2d=True)
    result, stats = finalize(audio, sr, loudness)
    return write_output(result, sr, output_path, stats)


def write_output(result, sr, output_path, stats):
    """Common quantization boundary for no-style and protected reference paths."""
    # Fixed seed: identical input and settings give a bit-identical file.
    rng = np.random.default_rng(0)
    dither = (rng.random(result.shape) - rng.random(result.shape)) / 2 ** 23
    sf.write(output_path, result + dither, sr, subtype="PCM_24")
    ceiling = stats.get("true_peak_ceiling_dbtp", TRUE_PEAK_CEILING_DB)
    peak = None
    if stats.get("target_lufs_range") is not None:
        from .loud import written_true_peak
        peak = written_true_peak(output_path)
        if peak > ceiling:
            trim = ceiling - .015 - peak
            sf.write(output_path, result * 10 ** (trim / 20) + dither, sr, subtype="PCM_24")
            stats["safety_trim_db"] = trim
            peak = written_true_peak(output_path)
    written, written_sr = sf.read(output_path, dtype="float64", always_2d=True)
    if written.shape != result.shape or written_sr != sr:
        raise RuntimeError("Mastering output shape/sample rate changed")
    if peak is None:
        _update_measurements(written, written_sr, stats)
    else:
        _apply_measurements(stats, integrated_lufs(written, written_sr)[0], peak)
    if stats["true_peak_dbtp"] > ceiling:
        raise RuntimeError("Written mastering output exceeds true peak ceiling")
    stats.update(output_subtype=sf.info(output_path).subtype, output_sample_rate=sr,
                 output_frames=len(written), readback_verified=True)
    Path(str(output_path) + ".mastering.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return stats
