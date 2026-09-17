"""Conservative high-band noise estimates; not a music/noise classifier.

Masks are channel-linked and attenuation-only in the STFT domain. Finite windows
have spectral leakage: the cutoff is not a brick-wall time-domain guarantee.
Analysis is chunked so spectral working memory does not grow with song length.
"""
from __future__ import annotations

import math
import numpy as np
from scipy import ndimage, signal

from audio_validation import finite_range

NOISE_MODES = ("other", "adaptive_all")
FFT_SIZE = 2048
HOP = 512
BLOCK_SECONDS = 8.0
CONTEXT_SECONDS = 1.5
CONFIDENCE_THRESHOLD = 0.55
MIN_SECONDS = 0.75


def validate_noise_options(sr, amount, low_hz, high_hz, max_attenuation_db):
    finite_range(sr, "sample rate", 22050, 192000)
    finite_range(amount, "denoise amount", 0, 1)
    finite_range(low_hz, "noise-low-hz", 8000, 20000)
    finite_range(high_hz, "noise-high-hz", 8000, 22000)
    finite_range(max_attenuation_db, "noise-max-attenuation-db", 0, 6)
    if not low_hz < high_hz < sr / 2:
        raise ValueError("noise band must satisfy low < high < Nyquist")


def _audio(x):
    arr = np.asarray(x)
    if arr.ndim not in (1, 2) or (arr.ndim == 2 and arr.shape[1] not in (1, 2)):
        raise ValueError("noise analysis requires mono or stereo audio")
    if not np.issubdtype(arr.dtype, np.floating) or not np.isfinite(arr).all():
        raise ValueError("noise analysis requires finite floating-point samples")
    return arr, arr[:, None] if arr.ndim == 1 else arr


def _db_ratio(after, before):
    return float(10 * np.log10(after / before)) if min(after, before) > 0 else None


def _local_correlation(x, y, frames):
    def mean(v):
        return ndimage.uniform_filter1d(v, frames, mode="nearest")
    xm, ym = mean(x), mean(y)
    vx, vy = np.maximum(mean(x * x) - xm * xm, 0), np.maximum(mean(y * y) - ym * ym, 0)
    covariance = mean(x * y) - xm * ym
    return np.clip(covariance / np.sqrt(np.maximum(vx * vy, 1e-24)), -1, 1)


def _spectral_mask(spectra, f, sr, amount, low_hz, high_hz, cap_db):
    power = np.mean(np.abs(spectra) ** 2, axis=0)
    mag = np.sqrt(power)
    attenuation = np.zeros_like(mag)
    neighborhood = max(3, int(round(1.5 * sr / HOP)) | 1)
    protect_frames = max(3, int(round(0.12 * sr / HOP)) | 1)
    music_env = np.sqrt(np.sum(power[(f >= 100) & (f < 6000)], axis=0))
    summaries = []
    for lo in np.arange(low_hz, high_hz, 2000.0):
        hi = min(lo + 2000, high_hz)
        select = (f >= lo) & (f < hi)
        p, m = power[select], mag[select]
        if len(p) < 8:
            continue
        energy = np.mean(p, axis=0)
        flatness = np.exp(np.mean(np.log(np.maximum(p, 1e-24)), axis=0)) / np.maximum(energy, 1e-24)
        spectral_peak = np.max(p, axis=0) / np.maximum(energy, 1e-24)
        p20 = ndimage.percentile_filter(energy, 20, size=neighborhood, mode="nearest")
        p80 = ndimage.percentile_filter(energy, 80, size=neighborhood, mode="nearest")
        stability = np.clip((p20 / np.maximum(p80, 1e-24) - 0.35) / 0.4, 0, 1)
        level = np.sqrt(energy)
        flux = np.zeros_like(level)
        flux[1:] = np.sqrt(np.mean(np.maximum(np.diff(m, axis=1), 0) ** 2, axis=0)) / np.maximum(level[:-1], 1e-12)
        onset = np.zeros_like(level)
        onset[1:] = np.maximum(10 * np.log10(np.maximum(energy[1:], 1e-24) / np.maximum(energy[:-1], 1e-24)), 0)
        transient = np.maximum(np.clip((onset - 1.0) / 4.0, 0, 1), np.clip((flux - 0.65) / 0.5, 0, 1))
        transient = ndimage.maximum_filter1d(transient, protect_frames, mode="nearest")
        correlation = _local_correlation(level, music_env, neighborhood)
        musical = np.clip((correlation - 0.35) / 0.4, 0, 1)
        confidence = (np.clip((flatness - 0.25) / 0.3, 0, 1) * stability
                      * np.clip((24 - spectral_peak) / 12, 0, 1)
                      * (1 - transient) * (1 - musical))
        confidence[energy < 1e-14] = 0
        floor = 0.5 * ndimage.uniform_filter1d(m, neighborhood, axis=1, mode="nearest")
        likelihood = np.clip(floor / np.maximum(m, 1e-12), 0, 1)
        local_spectrum = ndimage.median_filter(m, size=(9, 1), mode="nearest")
        tonal_guard = np.clip((3 - m / np.maximum(local_spectrum, 1e-12)) / 2, 0, 1)
        eligible = np.where(confidence >= CONFIDENCE_THRESHOLD, confidence, 0)
        raw = cap_db * amount * eligible[None, :] * likelihood * tonal_guard
        # Smoothing may not spread suppression into protected transients/harmonics.
        attenuation[select] = np.minimum(raw, ndimage.gaussian_filter1d(raw, 2, axis=1, mode="nearest"))
        summaries.append({"low_hz": float(lo), "high_hz": float(hi),
                          "confidence_mean": float(np.mean(confidence)),
                          "confidence_p95": float(np.percentile(confidence, 95)),
                          "spectral_flatness_mean": float(np.mean(flatness)),
                          "spectral_peak_to_mean_p95": float(np.percentile(spectral_peak, 95)),
                          "temporal_stability_mean": float(np.mean(stability)),
                          "spectral_flux_mean": float(np.mean(flux)),
                          "transient_fraction": float(np.mean(transient > 0.5)),
                          "music_envelope_correlation_mean": float(np.mean(correlation))})
    taper = np.minimum(np.clip((f - low_hz) / 500, 0, 1), np.clip((high_hz - f) / 500, 0, 1))
    attenuation *= (0.5 - 0.5 * np.cos(np.pi * taper))[:, None]
    return np.clip(attenuation, 0, cap_db * amount), summaries


def band_delta_stats(original, delta, sr, low_hz, high_hz):
    """Streaming causal band-pass energy and cross term, summed over channels."""
    sos = signal.butter(4, [low_hz, high_hz], btype="bandpass", fs=sr, output="sos")
    channels = original.shape[1]
    states = [np.zeros((len(sos), 2, channels)) for _ in range(2)]
    e0 = ed = cross = 0.0
    for start in range(0, len(original), 65536):
        a, states[0] = signal.sosfilt(sos, original[start:start + 65536], axis=0, zi=states[0])
        d, states[1] = signal.sosfilt(sos, delta[start:start + 65536], axis=0, zi=states[1])
        e0 += float(np.sum(a * a))
        ed += float(np.sum(d * d))
        cross += float(np.sum(a * d))
    return e0, ed, cross


def constrain_noise_delta(mix, delta, sr, low_hz=8000.0, high_hz=20000.0,
                          max_attenuation_db=6.0):
    """Bound measured band energy and sample peak after adding all stem deltas.

    The triangle bound limits removed band RMS; the quadratic upper bound stops
    cancellation between stems from turning individual cuts into a mix boost.
    This is not a true-peak limiter or a perceptual air/presence guarantee.
    """
    validate_noise_options(sr, 1, low_hz, high_hz, max_attenuation_db)
    _, x = _audio(mix)
    _, d = _audio(delta)
    if x.shape != d.shape:
        raise ValueError("noise delta must match mix shape")
    e0, ed, cross = band_delta_stats(x, d, sr, low_hz, high_hz)
    alpha = 0.0
    if e0 > 0 and ed > 0 and cross < 0 and max_attenuation_db > 0:
        alpha = min(1.0, -2 * cross / ed,
                    (1 - 10 ** (-max_attenuation_db / 20)) * math.sqrt(e0 / ed))
        peak = float(np.max(np.abs(x)))
        for start in range(0, len(x), 65536):
            a, b = x[start:start + 65536], d[start:start + 65536]
            pos, neg = b > 0, b < 0
            if np.any(pos):
                alpha = min(alpha, float(np.min((peak - a[pos]) / b[pos])))
            if np.any(neg):
                alpha = min(alpha, float(np.min((-peak - a[neg]) / b[neg])))
        alpha = max(0.0, alpha)
    accepted = np.asarray(delta) * alpha
    e1 = max(0.0, e0 + 2 * alpha * cross + alpha * alpha * ed)
    return accepted, {"admitted_delta_gain": float(alpha),
                      "applied": bool(alpha > 0 and np.any(accepted)),
                      "band_energy_change_db": _db_ratio(e1, e0),
                      "max_attenuation_db": float(max_attenuation_db),
                      "measurement": "4th-order causal Butterworth band-pass, channel-summed energy",
                      "peak_policy": "denoise delta cannot increase mix sample peak",
                      "reason": "accepted" if alpha > 0 else "no_safe_delta"}


def adaptive_denoise(x, sr, amount=0.0, low_hz=8000.0, high_hz=20000.0,
                     max_attenuation_db=6.0, report=None):
    """Apply high-confidence high-band attenuation, returning input exactly on skip."""
    validate_noise_options(sr, amount, low_hz, high_hz, max_attenuation_db)
    arr, audio = _audio(x)
    stats = {"mode": "adaptive_all", "status": "not_applied", "applied": False,
             "reason": "disabled", "confidence": None, "confidence_threshold": CONFIDENCE_THRESHOLD,
             "low_hz": float(low_hz), "high_hz": float(high_hz), "actual_start_hz": None,
             "amount": float(amount), "max_attenuation_db": float(max_attenuation_db),
             "mask_attenuation_db_p50": 0.0, "mask_attenuation_db_p95": 0.0,
             "mask_attenuation_db_max": 0.0, "band_energy_change_db": None,
             "channel_linked": True, "stft": "2048 Hann, hop 512, zero boundary",
             "percentiles": "0.01 dB histogram of requested-band core frames, before mix budget",
             "bands": [], "analysis_blocks": 0}

    def finish(result, reason):
        stats["reason"] = reason
        if report is not None:
            report.update(stats)
        return result

    if amount == 0 or max_attenuation_db == 0:
        return finish(arr, "disabled")
    if len(audio) < max(FFT_SIZE, int(sr * MIN_SECONDS)):
        return finish(arr, "insufficient_duration")
    if not np.any(audio):
        return finish(arr, "silence")
    # Core boundaries lie on STFT hops; context supports local statistics at joins.
    block = int(BLOCK_SECONDS * sr) // HOP * HOP
    context = int(CONTEXT_SECONDS * sr) // HOP * HOP
    delta = np.zeros(audio.shape, dtype=np.float64)
    histogram = np.zeros(601, dtype=np.int64)
    conf_sum = 0.0
    channel_agreement_sum = 0.0
    band_summaries = []
    for start in range(0, len(audio), block):
        end = min(start + block, len(audio))
        left, right = max(0, start - context), min(len(audio), end + context)
        chunk = np.asarray(audio[left:right], dtype=np.float64)
        spectra = np.asarray([signal.stft(chunk[:, c], sr, nperseg=FFT_SIZE,
                                         noverlap=FFT_SIZE - HOP)[2] for c in range(chunk.shape[1])])
        f = np.fft.rfftfreq(FFT_SIZE, 1 / sr)
        attenuation, summaries = _spectral_mask(spectra, f, sr, amount, low_hz, high_hz, max_attenuation_db)
        offset = start - left
        core_frames = slice(offset // HOP, (end - left + HOP - 1) // HOP)
        high = (f >= low_hz) & (f < high_hz)
        values = attenuation[high, core_frames]
        histogram += np.bincount(np.rint(values.ravel() * 100).astype(int), minlength=601)
        if np.any(values):
            active = np.any(attenuation[:, core_frames] > 0, axis=1)
            first = float(f[active][0])
            stats["actual_start_hz"] = first if stats["actual_start_hz"] is None else min(first, stats["actual_start_hz"])
        stats["mask_attenuation_db_max"] = max(stats["mask_attenuation_db_max"], float(np.max(values)))
        if summaries:
            conf_sum += max(s["confidence_mean"] for s in summaries)
        band_summaries.append(summaries)
        if chunk.shape[1] == 2:
            a, b = spectra[:, high, :]
            denom = math.sqrt(float(np.sum(abs(a) ** 2) * np.sum(abs(b) ** 2)))
            channel_agreement_sum += float(np.sum((a.conj() * b).real) / denom) if denom > 0 else 0.0
        if np.any(attenuation):
            multiplier = np.expm1(-attenuation * (np.log(10) / 20))
            for c, spectrum in enumerate(spectra):
                _, restored = signal.istft(spectrum * multiplier, sr, nperseg=FFT_SIZE,
                                          noverlap=FFT_SIZE - HOP)
                delta[start:end, c] = restored[offset:offset + end - start]
        stats["analysis_blocks"] += 1
    stats["confidence"] = conf_sum / stats["analysis_blocks"]
    stats["channel_spectral_correlation"] = (channel_agreement_sum / stats["analysis_blocks"]
                                                if audio.shape[1] == 2 else None)
    if band_summaries and band_summaries[0]:
        for index, item in enumerate(band_summaries[0]):
            combined = dict(item)
            for key in item.keys() - {"low_hz", "high_hz"}:
                combined[key] = float(np.mean([b[index][key] for b in band_summaries]))
            stats["bands"].append(combined)
    cumulative = np.cumsum(histogram)
    if cumulative[-1]:
        for q, key in ((0.5, "mask_attenuation_db_p50"), (0.95, "mask_attenuation_db_p95")):
            stats[key] = float(np.searchsorted(cumulative, q * cumulative[-1]) / 100)
    if not np.any(delta):
        return finish(arr, "low_confidence")
    delta, budget = constrain_noise_delta(audio, delta, sr, low_hz, high_hz, max_attenuation_db * amount)
    stats["stem_budget"] = budget
    if not budget["applied"]:
        return finish(arr, "no_safe_delta")
    out = audio.astype(np.float64) + delta
    if not np.isfinite(out).all():
        raise RuntimeError("adaptive denoise produced non-finite samples")
    stats.update(applied=True, status="applied", band_energy_change_db=budget["band_energy_change_db"])
    return finish((out[:, 0] if arr.ndim == 1 else out).astype(arr.dtype), "high_confidence_noise")
