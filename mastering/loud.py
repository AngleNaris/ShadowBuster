"""Unified ZL Limiter mastering for all loudness tiers: section-relative drive,
4x oversampled C1 pre-limiter soft clipping, tuned ZL Limiter 0.0.1, and GR overload budgets.
"""
import importlib.util
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
from scipy import ndimage, signal
import soundfile as sf

from audio_metrics import _block_loudness, integrated_lufs, oversampling_filter
from . import (ENGINE_VERSION, LOUDNESS_TARGETS, LOUDNESS_TARGET_RANGE,
               LOUD_TRUE_PEAK_CEILING_DB, TRUE_PEAK_CEILING_DB)
from .finalizer import OVERSAMPLING, soft_clip_peaks, true_peak_db

FIT_TARGET_LUFS = LOUDNESS_TARGET_RANGE[0] + .06
PRE_QUIET_DB = 2.0
POST_QUIET_DB = .5
PLUGIN_NAME = "ZL Limiter 0.0.1"
PLUGIN_BUNDLE = Path(__file__).resolve().parents[1] / "plugins" / "ZL Limiter.vst3"
PLUGIN_PATH = (PLUGIN_BUNDLE / "Contents" / "x86_64-win" / "ZL Limiter.vst3"
               if sys.platform == "win32" else PLUGIN_BUNDLE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_WINDOW_SECONDS = 10.0

# Tuned per-tier ZL Limiter profiles:
#   - Longer lookahead (6-8 ms) + faster RMS stage attack (20-25 ms) + 120-150 ms recovery
#     cut 100 Hz THD by 17-23 dB and reduce ceiling hard-clipping kinks by 5-7x vs defaults
#     (2 ms lookahead / 100 ms attack / 50 ms recovery).
#   - 4x oversampled C1 pre-limiter soft clipping shaves sparse transients smoothly before
#     hitting the brickwall stage.
#   - Whole-track + 10 s local GR budgets stop extreme-dynamic tracks from slamming +24 dB
#     into flat-top overload while allowing normal/loud pop & OST mixes to reach targets cleanly.
ZL_TIER_PROFILES = {
    "soft": {
        "ceiling_db": TRUE_PEAK_CEILING_DB,
        "lookahead_ms": 6.0,
        "recovery_ms": 120.0,
        "attack_ms": 25.0,
        "release_ms": 250.0,
        "default_soft_clip_db": 1.2,
        "pre_quiet_db": 0.6,
        "post_quiet_db": 0.0,
        "gr_p50_budget_db": 1.2,
        "gr_p95_budget_db": 3.5,
        "gr_peak_budget_db": 8.0,
        "local_gr_p50_budget_db": 2.2,
        "local_gr_p95_budget_db": 5.0,
    },
    "dynamic": {
        "ceiling_db": TRUE_PEAK_CEILING_DB,
        "lookahead_ms": 6.0,
        "recovery_ms": 120.0,
        "attack_ms": 25.0,
        "release_ms": 250.0,
        "default_soft_clip_db": 1.2,
        "pre_quiet_db": 0.8,
        "post_quiet_db": 0.0,
        "gr_p50_budget_db": 1.5,
        "gr_p95_budget_db": 4.5,
        "gr_peak_budget_db": 9.5,
        "local_gr_p50_budget_db": 2.8,
        "local_gr_p95_budget_db": 6.0,
    },
    "normal": {
        "ceiling_db": TRUE_PEAK_CEILING_DB,
        "lookahead_ms": 6.0,
        "recovery_ms": 120.0,
        "attack_ms": 25.0,
        "release_ms": 250.0,
        "default_soft_clip_db": 1.2,
        "pre_quiet_db": 1.2,
        "post_quiet_db": 0.25,
        "gr_p50_budget_db": 2.2,
        "gr_p95_budget_db": 5.5,
        "gr_peak_budget_db": 11.0,
        "local_gr_p50_budget_db": 3.5,
        "local_gr_p95_budget_db": 7.0,
    },
    "loud": {
        "ceiling_db": LOUD_TRUE_PEAK_CEILING_DB,
        "lookahead_ms": 8.0,
        "recovery_ms": 150.0,
        "attack_ms": 20.0,
        "release_ms": 250.0,
        "default_soft_clip_db": 1.5,
        "pre_quiet_db": PRE_QUIET_DB,
        "post_quiet_db": POST_QUIET_DB,
        "gr_p50_budget_db": 3.0,
        "gr_p95_budget_db": 6.5,
        "gr_peak_budget_db": 11.5,
        "local_gr_p50_budget_db": 4.5,
        "local_gr_p95_budget_db": 8.2,
    },
}


def zl_limiter_available():
    """True when the pinned ZL Limiter VST3 bundle and pedalboard host are available."""
    return PLUGIN_PATH.exists() and importlib.util.find_spec("pedalboard") is not None


def _ffmpeg(args):
    return subprocess.run(
        [os.environ.get("SB_FFMPEG") or "ffmpeg", "-hide_banner", "-nostats", *args],
        capture_output=True, check=True, creationflags=_NO_WINDOW,
    )


def written_true_peak(path):
    # Match the independent 192 kHz measurement used to approve C+.
    result = _ffmpeg(["-i", str(path), "-af",
                      "aformat=sample_fmts=dbl,aresample=192000,"
                      "astats=measure_perchannel=none:measure_overall=Peak_level",
                      "-f", "null", "-"])
    levels = re.findall(rb"Peak level dB: (-?\d+\.\d+|-inf)", result.stderr)
    if not levels:
        raise RuntimeError("FFmpeg did not report the written mastering true peak")
    return float(levels[-1])


def _read_section_source(path, sample_rate):
    try:
        audio, rate = sf.read(path, always_2d=True)
        if audio.shape[1] <= 2 and rate >= 8000:
            return audio, rate
    except sf.LibsndfileError:
        pass
    decoded = _ffmpeg(["-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "2",
                       "-ar", str(sample_rate), "-f", "f32le", "-"])
    return np.frombuffer(decoded.stdout, dtype="<f4").reshape(-1, 2), sample_rate


def section_profile(audio, sample_rate, source_path=None):
    """Map the original time axis onto the processed stereo mix."""
    source, rate = audio, sample_rate
    report = {"source": "premaster", "max_pre_limiter_discount_db": PRE_QUIET_DB,
              "max_post_limiter_discount_db": POST_QUIET_DB, "min_pre_limiter_boost_fraction": .65}
    if source_path is not None:
        original, original_rate = _read_section_source(source_path, sample_rate)
        if len(original) and abs(len(original) / original_rate - len(audio) / sample_rate) <= .1:
            source, rate = original, original_rate
            report["source"] = "original_input"
        else:
            report["fallback_reason"] = "source_duration_mismatch"

    short = _block_loudness(source, rate, 3.0, .1)
    smooth = ndimage.median_filter(short, size=61, mode="nearest")
    active = smooth > -70.0
    if not active.any():
        return np.zeros(len(audio)), report
    anchor = float(np.percentile(smooth[active], 70))
    severity = np.clip((anchor - 3.0 - smooth) / 6.0, 0.0, 1.0)
    severity = ndimage.gaussian_filter1d(severity, sigma=10, mode="nearest")
    centers = min(1.5, len(source) / (2 * rate)) + np.arange(len(severity)) * .1
    weights = np.interp(np.arange(len(audio)) / sample_rate, centers, severity)
    report["reference_short_term_p70_lufs"] = anchor
    return weights, report


def _render_limiter(audio, sample_rate, *, ceiling_db=LOUD_TRUE_PEAK_CEILING_DB,
                    lookahead_ms=8.0, recovery_ms=150.0, attack_ms=20.0, release_ms=250.0):
    from pedalboard import load_plugin

    if not PLUGIN_PATH.exists():
        raise RuntimeError(f"ZL Limiter not found: {PLUGIN_PATH} (run tools/fetch_plugins.py)")
    limiter = load_plugin(str(PLUGIN_PATH))
    limiter.output_ceiling_output_ceiling = float(ceiling_db)
    limiter.true_peak_true_peak = True
    limiter.oversampling_oversampling = "4x"
    limiter.channel_delta_channel_delta = 0.0
    limiter.lookahead_lookahead = float(lookahead_ms)
    limiter.recovery_recovery = float(recovery_ms)
    limiter.attack_attack = float(attack_ms)
    limiter.release_release = float(release_ms)
    # Pad past the lookahead so offline hosts can return the original tail.
    padded = np.zeros((2, len(audio) + 4096), dtype=np.float32)
    padded[:, :len(audio)] = audio.T
    rendered = limiter.process(padded, sample_rate, buffer_size=8192, reset=True)
    if rendered.shape[0] != 2 or rendered.shape[1] < len(audio):
        raise RuntimeError("ZL Limiter returned an incomplete mastering buffer")
    return rendered[:, :len(audio)].T.astype(np.float64)


def _measure_zl_gr(pre_audio, limited_audio, sample_rate):
    """Measure time-aligned 10 ms RMS gain reduction and worst 10 s local engagement."""
    blk = max(1, round(sample_rate * 0.010))
    nblk = len(pre_audio) // blk
    if nblk == 0:
        return {
            "max_gain_reduction_db": 0.0, "gain_reduction_p50_db": 0.0,
            "gain_reduction_p95_db": 0.0, "active_fraction": 0.0,
            "local_active_fraction": 0.0, "local_gain_reduction_p50_db": 0.0,
            "local_gain_reduction_p95_db": 0.0,
            "output_peak": float(np.max(np.abs(limited_audio))) if len(limited_audio) else 0.0,
        }
    end = nblk * blk
    e_in = np.sqrt(np.mean(pre_audio[:end].reshape(nblk, blk, -1) ** 2, axis=(1, 2)))
    e_out = np.sqrt(np.mean(limited_audio[:end].reshape(nblk, blk, -1) ** 2, axis=(1, 2)))
    active_mask = e_in > 10.0 ** (-45.0 / 20.0)
    gr = np.maximum(0.0, 20.0 * np.log10(np.maximum(e_in, 1e-12) / np.maximum(e_out, 1e-12)))
    gr_act = gr[active_mask] if np.any(active_mask) else gr

    win_blocks = min(nblk, max(1, round(LOCAL_WINDOW_SECONDS * 100)))
    hop_blocks = max(1, min(100, win_blocks))
    if nblk <= win_blocks:
        local_p50 = float(np.percentile(gr_act, 50))
        local_p95 = float(np.percentile(gr_act, 95))
        local_active = float(np.mean(gr_act > 0.25))
    else:
        local_p50 = 0.0
        local_p95 = 0.0
        local_active = 0.0
        for start in range(0, nblk - win_blocks + 1, hop_blocks):
            sl = slice(start, start + win_blocks)
            w_act = active_mask[sl]
            if np.mean(w_act) < 0.25:
                continue
            w_gr = gr[sl][w_act]
            local_p50 = max(local_p50, float(np.percentile(w_gr, 50)))
            local_p95 = max(local_p95, float(np.percentile(w_gr, 95)))
            local_active = max(local_active, float(np.mean(w_gr > 0.25)))

    return {
        "max_gain_reduction_db": float(np.max(gr_act)),
        "gain_reduction_p50_db": float(np.percentile(gr_act, 50)),
        "gain_reduction_p95_db": float(np.percentile(gr_act, 95)),
        "active_fraction": float(np.mean(gr_act > 0.25)),
        "local_active_fraction": local_active,
        "local_gain_reduction_p50_db": local_p50,
        "local_gain_reduction_p95_db": local_p95,
        "output_peak": float(np.max(np.abs(limited_audio))),
    }


def finalize_zl(audio, sample_rate, loudness="normal", sections=None, *, soft_clip_db=None):
    """Unified ZL Limiter mastering for soft / dynamic / normal / loud."""
    if loudness not in ZL_TIER_PROFILES:
        raise ValueError(f"Unknown loudness option: {loudness}")
    data = np.asarray(audio, dtype=np.float64)
    if data.ndim != 2 or data.shape[1] != 2 or not len(data):
        raise ValueError("Mastering requires nonempty stereo audio")
    if not np.isfinite(data).all():
        raise ValueError("Mastering input contains non-finite samples")

    profile = ZL_TIER_PROFILES[loudness]
    sc_db = profile["default_soft_clip_db"] if soft_clip_db is None else float(soft_clip_db)
    if not math.isfinite(sc_db) or not 0.0 <= sc_db <= 6.0:
        raise ValueError(f"soft_clip_db must be finite within [0, 6], got {soft_clip_db!r}")

    if sections is None:
        weights, section_report = section_profile(data, sample_rate)
    else:
        weights, section_report = sections
    section_report = dict(section_report)
    section_report["max_pre_limiter_discount_db"] = profile["pre_quiet_db"]
    section_report["max_post_limiter_discount_db"] = profile["post_quiet_db"]

    initial, method = integrated_lufs(data, sample_rate)
    if not math.isfinite(initial):
        raise ValueError("Cannot master silent or ungated audio")

    is_loud = loudness == "loud"
    target_lufs = LOUDNESS_TARGET_RANGE[0] if is_loud else LOUDNESS_TARGETS[loudness]
    fit_target = FIT_TARGET_LUFS if is_loud else target_lufs
    ceiling_db = profile["ceiling_db"]
    clip_ceiling_db = ceiling_db - 0.1
    tol_lu = 0.02 if is_loud else 0.06

    # Upsample once outside the drive search loop for fast 4x C1 soft clipping.
    up_base = None
    if sc_db > 0.0:
        up_base = signal.resample_poly(data, OVERSAMPLING, 1, axis=0,
                                       window=oversampling_filter(OVERSAMPLING))
        base_peak = max(float(np.max(np.abs(data))), float(np.max(np.abs(up_base))))
    else:
        base_peak = float(np.max(np.abs(data)))
    peak_db = 20.0 * math.log10(base_peak) if base_peak > 0 else -300.0
    ceiling_linear = 10.0 ** (clip_ceiling_db / 20.0)
    post_gain = np.power(10.0, -profile["post_quiet_db"] * weights / 20.0)[:, None] if profile["post_quiet_db"] > 0 else None

    # Guaranteed-clean lower bound where neither soft-clipper nor limiter engages.
    lower = float(np.clip(min(fit_target - initial, clip_ceiling_db - peak_db - 0.05), -6.0, 24.0))
    upper = None
    drive = float(np.clip(fit_target - initial + (0.6 if is_loud else 0.15), lower, 24.0))
    best = None
    budget_limited = False
    reason = "iteration_limit"
    previous = None

    for attempt in range(14):
        discount = min(profile["pre_quiet_db"], 0.35 * max(drive, 0.0))
        gain_curve = np.power(10.0, (drive - discount * weights) / 20.0)
        driven = data * gain_curve[:, None]
        max_driven = float(np.max(np.abs(driven)))

        # Pure-gain fast path: below limiter/clip ceiling, preserve original spectrum bit-exact.
        if max_driven <= ceiling_linear * 0.92 and (
            up_base is None or float(np.max(np.abs(up_base))) * float(np.max(gain_curve)) <= ceiling_linear
        ):
            clip_stats = {"applied": False, "soft_clip_db": sc_db,
                          "clipped_fraction": 0.0, "max_clip_gr_db": 0.0}
            candidate = driven if post_gain is None else driven * post_gain
            gr_stats = {
                "max_gain_reduction_db": 0.0, "gain_reduction_p50_db": 0.0,
                "gain_reduction_p95_db": 0.0, "active_fraction": 0.0,
                "local_active_fraction": 0.0, "local_gain_reduction_p50_db": 0.0,
                "local_gain_reduction_p95_db": 0.0,
                "output_peak": float(np.max(np.abs(candidate))),
            }
        else:
            if sc_db > 0.0 and up_base is not None:
                # Section gain moves over seconds; 4x repeat at 176.4 kHz is smooth and exact.
                g_up = np.repeat(gain_curve, OVERSAMPLING)[:len(up_base)]
                up_driven = up_base * g_up[:, None]
                clipped_up, clip_stats = soft_clip_peaks(up_driven, clip_ceiling_db, sc_db)
                if clip_stats["applied"]:
                    pre_lim = signal.resample_poly(
                        clipped_up, 1, OVERSAMPLING, axis=0,
                        window=oversampling_filter(OVERSAMPLING),
                    )[:len(data)]
                else:
                    pre_lim = driven
            else:
                clip_stats = {"applied": False, "soft_clip_db": 0.0,
                              "clipped_fraction": 0.0, "max_clip_gr_db": 0.0}
                pre_lim = driven

            limited = _render_limiter(
                pre_lim, sample_rate,
                ceiling_db=ceiling_db,
                lookahead_ms=profile["lookahead_ms"],
                recovery_ms=profile["recovery_ms"],
                attack_ms=profile["attack_ms"],
                release_ms=profile["release_ms"],
            )
            gr_stats = _measure_zl_gr(pre_lim, limited, sample_rate)
            candidate = limited if post_gain is None else limited * post_gain

        trim = 0.0
        peak_before_trim = None
        if not is_loud:
            peak_before_trim = true_peak_db(candidate)
            trim = min(0.0, ceiling_db - 0.02 - peak_before_trim)
            if trim < 0.0:
                candidate = candidate * (10.0 ** (trim / 20.0))

        actual, _ = integrated_lufs(candidate, sample_rate)
        if not math.isfinite(actual):
            raise RuntimeError("ZL Limiter produced unmeasurable audio")
        error = fit_target - actual
        feasible = (
            gr_stats["gain_reduction_p50_db"] <= profile["gr_p50_budget_db"] + 1e-6
            and gr_stats["gain_reduction_p95_db"] <= profile["gr_p95_budget_db"] + 1e-6
            and gr_stats["max_gain_reduction_db"] <= profile["gr_peak_budget_db"] + 1e-6
            and gr_stats["local_gain_reduction_p50_db"] <= profile["local_gr_p50_budget_db"] + 1e-6
            and gr_stats["local_gain_reduction_p95_db"] <= profile["local_gr_p95_budget_db"] + 1e-6
        )
        # First trial might start above budget; ensure lower bound is always evaluated if needed.
        if feasible and (best is None or abs(error) < best[0]):
            best = (abs(error), candidate, gr_stats, clip_stats, drive, trim, actual, peak_before_trim)
        if feasible and abs(error) <= tol_lu:
            reason = "fit_converged" if is_loud else "target_met"
            break

        if not feasible or error < 0:
            upper = drive if upper is None else min(upper, drive)
            budget_limited |= not feasible
            if best is None and attempt == 0:
                # Immediately anchor at guaranteed-clean lower bound next.
                drive = lower
                previous = None
                continue
        else:
            lower = max(lower, drive)

        if upper is not None:
            if upper - lower <= 0.03:
                reason = "dynamic_budget" if budget_limited else "converged"
                break
            drive = (lower + upper) * 0.5
        else:
            if previous is not None and abs(drive - previous[0]) > 1e-4:
                slope = float(np.clip((actual - previous[1]) / (drive - previous[0]), 0.15, 1.0))
            else:
                slope = 0.65
            next_drive = float(np.clip(drive + float(np.clip(error / slope, -2.5, 3.0)), lower, 24.0))
            if abs(next_drive - drive) <= 0.005:
                reason = "drive_limit"
                break
            previous = (drive, actual)
            drive = next_drive

    if best is None:
        # Fallback to guaranteed clean lower bound if somehow unrecorded.
        gain_curve = np.power(10.0, (lower - min(profile["pre_quiet_db"], 0.35 * max(lower, 0.0)) * weights) / 20.0)
        candidate = data * gain_curve[:, None]
        if post_gain is not None:
            candidate = candidate * post_gain
        actual, _ = integrated_lufs(candidate, sample_rate)
        peak_before_trim = None if is_loud else true_peak_db(candidate)
        best = (abs(fit_target - actual), candidate, _measure_zl_gr(candidate, candidate, sample_rate),
                {"applied": False, "soft_clip_db": sc_db, "clipped_fraction": 0.0, "max_clip_gr_db": 0.0},
                lower, 0.0, actual, peak_before_trim)

    _, output, gr_stats, clip_stats, drive, trim, actual_lufs, peak_before_trim = best
    limiter_info = {
        "engine": PLUGIN_NAME,
        "oversampling": 4,
        "true_peak": True,
        "stereo_link": 1.0,
        "lookahead_ms": profile["lookahead_ms"],
        "recovery_ms": profile["recovery_ms"],
        "attack_ms": profile["attack_ms"],
        "release_ms": profile["release_ms"],
        **gr_stats,
        "soft_clip": clip_stats,
    }
    stats = {
        "engine": ENGINE_VERSION, "style_mode": "off", "spectral_processing": False,
        "loudness_option": loudness, "loudness_target_source": "application_preset",
        "target_lufs": target_lufs,
        "input_lufs": initial, "loudness_method": method, "stop_reason": reason,
        "dynamic_budget_limited": budget_limited,
        "limiter_p50_budget_db": profile["gr_p50_budget_db"],
        "limiter_p95_budget_db": profile["gr_p95_budget_db"],
        "limiter_peak_budget_db": profile["gr_peak_budget_db"],
        "limiter_local_p50_budget_db": profile["local_gr_p50_budget_db"],
        "limiter_local_p95_budget_db": profile["local_gr_p95_budget_db"],
        "true_peak_ceiling_dbtp": ceiling_db,
        "limiter": limiter_info,
        "sections": section_report, "iterations": attempt + 1,
        "applied_gain_db": drive, "safety_trim_db": trim,
    }
    if is_loud:
        stats["target_lufs_range"] = LOUDNESS_TARGET_RANGE
    else:
        from .finalizer import _apply_measurements
        tp = (peak_before_trim + trim) if peak_before_trim is not None else true_peak_db(output)
        _apply_measurements(stats, actual_lufs, tp)
    return output, stats


def finalize_loud(audio, sample_rate, sections, *, soft_clip_db=None):
    """Backward-compatible wrapper for the loud tier."""
    return finalize_zl(audio, sample_rate, "loud", sections, soft_clip_db=soft_clip_db)
