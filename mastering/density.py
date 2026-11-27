"""Multiband density compression before the limiter, hosted on ZL Compressor (VST3).

Bands are split by complementary subtraction of zero-phase low-passes (they sum back
to the input exactly); each band runs one ZL Compressor instance (Optical style,
AGPL-3.0, fetched by tools/fetch_plugins.py) through pedalboard. Each compressed
band receives RMS-matched automatic makeup gain so compressing a subset of bands
(e.g. scope="low") tightens that band's dynamics without sinking it relative to
uncompressed bands and tilting the rest of the spectrum up.

Band settings come from the user's Audition "流行音乐大师" variant (2026-11-05
listening: Optical preferred over Classic/Clean). Its thresholds assume a
−8.68 LUFS input, so they move with the input loudness, and per block with the
band's ~3 s section level against its song level — verse and chorus are each
compressed as if they sat at the song's average level, keeping their contrast.
"""
import math
from pathlib import Path
import sys

import numpy as np
from scipy import ndimage, signal
from . import ALL_DENSITY_BANDS, normalize_density_scope

CROSSOVERS_HZ = (120.0, 2000.0, 10000.0)
BAND_NAMES = ("low", "low_mid", "high_mid", "high")
# (threshold dB, ratio, attack ms, release ms) per band, at REFERENCE_LUFS input.
BANDS = ((-22.0, 2.3, 2.0, 100.0), (-22.0, 2.6, 3.0, 100.0),
         (-22.0, 2.6, 2.5, 100.0), (-30.0, 3.0, 3.0, 100.0))
REFERENCE_LUFS = -8.68
STYLE = "Optical"
SECTION_SECONDS = 3.0
# Section following is clipped so fades and silence are not chased down to the floor.
FOLLOW_RANGE_DB = (-12.0, 6.0)
BLOCK = 2048
# Threshold offset per mode: light compresses less than the reference settings.
MODES = {"light": 3.0, "standard": 0.0}
DENSITY_MODES = ("off",) + tuple(MODES)
PLUGIN_BUNDLE = Path(__file__).resolve().parents[1] / "plugins" / "ZL Compressor.vst3"
PLUGIN = (PLUGIN_BUNDLE / "Contents" / "x86_64-win" / "ZL Compressor.vst3"
          if sys.platform == "win32" else PLUGIN_BUNDLE)


def split_bands(audio, sr):
    bands, rest = [], audio
    for hz in CROSSOVERS_HZ:
        low = signal.sosfiltfilt(signal.butter(2, hz, fs=sr, output="sos"), rest, axis=0)
        bands.append(low)
        rest = rest - low
    bands.append(rest)
    return bands


def _compressor(ratio, attack, release):
    from pedalboard import load_plugin
    if not PLUGIN.exists():
        raise RuntimeError(f"ZL Compressor not found: {PLUGIN} (run tools/fetch_plugins.py)")
    plugin = load_plugin(str(PLUGIN))
    plugin.compressor_style_compressor_style = STYLE
    plugin.ratio_ratio = ratio
    plugin.attack_attack = attack
    plugin.release_release = release
    plugin.knee_width_knee_width = 0.0
    plugin.side_stereo_mode_side_stereo_mode = "Left Right Max"
    plugin.side_stereo_link_side_stereo_link = 100.0
    return plugin


def _follow_db(band, sr):
    """Per-block section level minus song level (dB), clipped to FOLLOW_RANGE_DB."""
    power = np.mean(band * band, axis=1)
    section = ndimage.uniform_filter1d(power, round(sr * SECTION_SECONDS), mode="nearest")
    song = max(float(np.mean(power)), 1e-20)
    centers = np.arange(0, len(band), BLOCK) + BLOCK // 2
    level = 10 * np.log10(np.maximum(section[np.minimum(centers, len(band) - 1)], 1e-20) / song)
    return np.clip(level, *FOLLOW_RANGE_DB)


def _reduction_db(band, out, sr):
    """Gain reduction per 50 ms block, from band input vs output power."""
    n = round(sr * .05)
    count = len(band) // n
    p_in = np.mean(band[:count * n] ** 2, axis=1).reshape(count, n).mean(axis=1)
    p_out = np.mean(out[:count * n] ** 2, axis=1).reshape(count, n).mean(axis=1)
    keep = p_in > 1e-10
    return np.maximum(10 * np.log10(p_in[keep] / np.maximum(p_out[keep], 1e-30)), 0.0)


def densify(audio, mode="light", sr=44100, input_lufs=None, *, scope=ALL_DENSITY_BANDS):
    if mode not in DENSITY_MODES:
        raise ValueError(f"Unknown density mode: {mode}")
    scope = normalize_density_scope(scope)
    report = {"mode": mode, "scope": scope, "applied": False}
    if mode == "off" or scope == "none" or len(audio) < sr:
        return audio.copy(), report
    if input_lufs is None:
        from audio_metrics import integrated_lufs
        input_lufs = integrated_lufs(audio, sr)[0]
    shift = input_lufs - REFERENCE_LUFS + MODES[mode]
    result = np.zeros_like(audio)
    bands = {}
    if scope == "global":
        routes = [("global", "global", audio, (-22.0, 2.0, 15.0, 100.0))]
    else:
        routes = zip(("low", "mid", "high", "high"), BAND_NAMES, split_bands(audio, sr), BANDS)
    selected = scope.split(",")
    for group, name, band, (threshold, ratio, attack, release) in routes:
        if group not in selected:
            result += band
            continue
        plugin = _compressor(ratio, attack, release)
        follow = _follow_db(band, sr)
        chunks = []
        for index, start in enumerate(range(0, len(band), BLOCK)):
            plugin.threshold_db_threshold_db = float(np.clip(threshold + shift + follow[index], -100, 0))
            chunk = np.ascontiguousarray(band[start:start + BLOCK].T, dtype=np.float32)
            chunks.append(plugin.process(chunk, sr, reset=index == 0))
        raw_out = np.concatenate(chunks, axis=1).T.astype(np.float64)
        reduction = _reduction_db(band, raw_out, sr)
        # RMS-matched per-band makeup: keeps tonal balance invariant when only a
        # subset of bands (e.g. low-only) is compressed while preserving dynamic tightening.
        p_in = float(np.mean(band * band))
        p_out = float(np.mean(raw_out * raw_out))
        if p_out > 1e-20 and p_in > p_out:
            makeup_db = min(12.0, 10.0 * math.log10(p_in / p_out))
            out = raw_out * (10.0 ** (makeup_db / 20.0))
        else:
            makeup_db = 0.0
            out = raw_out
        result += out
        active = reduction[reduction > .1]
        bands[name] = {"threshold_db": threshold + shift, "ratio": ratio,
                       "attack_ms": attack, "release_ms": release,
                       "makeup_gain_db": round(makeup_db, 3),
                       "active_fraction": float(len(active) / max(len(reduction), 1)),
                       "gain_reduction_p50_active_db": float(np.median(active)) if len(active) else 0.0,
                       "gain_reduction_p95_db": float(np.percentile(reduction, 95)) if len(reduction) else 0.0}
    report.update(applied=True, engine="ZL Compressor 0.6.1", style=STYLE,
                  crossovers_hz=[] if scope == "global" else list(CROSSOVERS_HZ), input_lufs=input_lufs,
                  threshold_shift_db=shift, section_seconds=SECTION_SECONDS, bands=bands)
    return result, report
