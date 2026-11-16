"""C+ loud mastering: section-relative drive, ZL Limiter, and soft-section trim."""
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
from scipy import ndimage
import soundfile as sf

from audio_metrics import _block_loudness, integrated_lufs
from . import ENGINE_VERSION, LOUDNESS_TARGET_RANGE, LOUD_TRUE_PEAK_CEILING_DB

FIT_TARGET_LUFS = LOUDNESS_TARGET_RANGE[0] + .06
PRE_QUIET_DB = 2.0
POST_QUIET_DB = .5
PLUGIN_NAME = "ZL Limiter 0.0.1"
PLUGIN_BUNDLE = Path(__file__).resolve().parents[1] / "plugins" / "ZL Limiter.vst3"
PLUGIN_PATH = (PLUGIN_BUNDLE / "Contents" / "x86_64-win" / "ZL Limiter.vst3"
               if sys.platform == "win32" else PLUGIN_BUNDLE)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def _render_limiter(audio, sample_rate):
    from pedalboard import load_plugin

    if not PLUGIN_PATH.exists():
        raise RuntimeError(f"ZL Limiter not found: {PLUGIN_PATH} (run tools/fetch_plugins.py)")
    limiter = load_plugin(str(PLUGIN_PATH))
    limiter.output_ceiling_output_ceiling = LOUD_TRUE_PEAK_CEILING_DB
    limiter.true_peak_true_peak = True
    limiter.oversampling_oversampling = "4x"
    limiter.channel_delta_channel_delta = 0.0
    # Pad past the lookahead so offline hosts can return the original tail.
    padded = np.zeros((2, len(audio) + 4096), dtype=np.float32)
    padded[:, :len(audio)] = audio.T
    rendered = limiter.process(padded, sample_rate, buffer_size=8192, reset=True)
    if rendered.shape[0] != 2 or rendered.shape[1] < len(audio):
        raise RuntimeError("ZL Limiter returned an incomplete mastering buffer")
    return rendered[:, :len(audio)].T


def finalize_loud(audio, sample_rate, sections):
    weights, section_report = sections
    initial, method = integrated_lufs(audio, sample_rate)
    if not math.isfinite(initial):
        raise ValueError("Cannot master silent or ungated audio")

    drive = float(np.clip(FIT_TARGET_LUFS - initial + .75, -6.0, 24.0))
    previous = None
    best = None
    reason = "iteration_limit"
    for attempt in range(9):
        discount = min(PRE_QUIET_DB, .35 * max(drive, 0.0))
        driven = audio * np.power(10.0, (drive - discount * weights) / 20.0)[:, None]
        candidate = _render_limiter(driven, sample_rate)
        actual, _ = integrated_lufs(candidate, sample_rate)
        if not math.isfinite(actual):
            raise RuntimeError("ZL Limiter produced unmeasurable audio")
        error = FIT_TARGET_LUFS - actual
        if best is None or abs(error) < best[0]:
            best = (abs(error), candidate, drive)
        if abs(error) <= .02:
            reason = "fit_converged"
            break
        slope = .5 if previous is None else np.clip(
            (actual - previous[1]) / (drive - previous[0]), .1, 1.0)
        next_drive = float(np.clip(drive + np.clip(error / slope, -2.5, 2.5), -6.0, 24.0))
        if abs(next_drive - drive) <= .001:
            reason = "drive_limit"
            break
        previous = drive, actual
        drive = next_drive

    _, audio, drive = best
    output = audio * np.power(10.0, -POST_QUIET_DB * weights / 20.0)[:, None]
    stats = {
        "engine": ENGINE_VERSION, "style_mode": "off", "spectral_processing": False,
        "loudness_option": "loud", "loudness_target_source": "application_preset",
        "target_lufs": LOUDNESS_TARGET_RANGE[0], "target_lufs_range": LOUDNESS_TARGET_RANGE,
        "input_lufs": initial, "loudness_method": method, "stop_reason": reason,
        "dynamic_budget_limited": False, "true_peak_ceiling_dbtp": LOUD_TRUE_PEAK_CEILING_DB,
        "limiter": {"engine": PLUGIN_NAME, "oversampling": 4, "true_peak": True, "stereo_link": 1.0},
        "sections": section_report, "iterations": attempt + 1,
        "applied_gain_db": drive, "safety_trim_db": 0.0,
    }
    return output, stats
