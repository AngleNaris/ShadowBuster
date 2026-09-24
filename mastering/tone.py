"""Explicit user EQ, independent of reference matching (RBJ cookbook biquads)."""
import numpy as np
from scipy import signal

EQ_PROFILES = ("Neutral", "Warm", "Bright", "Fusion")


def _eq(x, sr, kind, hz, gain_db, q=.9):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * hz / sr
    c, s = np.cos(w), np.sin(w)
    if kind == "peak":
        alpha = s / (2 * q)
        b = [1 + alpha * a, -2 * c, 1 - alpha * a]
        d = [1 + alpha / a, -2 * c, 1 - alpha / a]
    else:
        beta = 2 * np.sqrt(a) * s / np.sqrt(2)
        if kind == "low":
            b = [a * (a+1-(a-1)*c+beta), 2*a*(a-1-(a+1)*c), a*(a+1-(a-1)*c-beta)]
            d = [a+1+(a-1)*c+beta, -2*(a-1+(a+1)*c), a+1+(a-1)*c-beta]
        else:
            b = [a * (a+1+(a-1)*c+beta), -2*a*(a-1+(a+1)*c), a*(a+1+(a-1)*c-beta)]
            d = [a+1-(a-1)*c+beta, 2*(a-1-(a+1)*c), a+1-(a-1)*c-beta]
    return signal.sosfilt([np.r_[np.asarray(b) / d[0], np.asarray(d) / d[0]]], x)


def apply_eq(audio, profile="Neutral", lowpass=None, sr=44100):
    if profile not in EQ_PROFILES:
        raise ValueError(f"Unknown EQ profile: {profile}")
    mid, side = audio.mean(axis=1), (audio[:, 0] - audio[:, 1]) / 2
    if profile == "Warm":
        mid = _eq(mid, sr, "low", 150, 1.5)
    elif profile == "Bright":
        mid = _eq(_eq(mid, sr, "peak", 3200, 1), sr, "high", 9000, 1.5)
        side = _eq(_eq(side, sr, "peak", 250, -.7, 1), sr, "high", 8000, 1.5)
    elif profile == "Fusion":
        mid = _eq(_eq(mid, sr, "low", 150, .8), sr, "high", 9000, .8)
        side = _eq(_eq(side, sr, "peak", 250, -.26, 1), sr, "high", 8000, .8)
    result = audio.copy() if profile == "Neutral" else np.column_stack((mid + side, mid - side))
    if lowpass is not None:
        if not np.isfinite(lowpass) or not 20 <= lowpass < sr / 2:
            raise ValueError("Invalid mastering lowpass cutoff")
        result = signal.sosfilt(signal.butter(4, lowpass, fs=sr, output="sos"), result, axis=0)
    return result
