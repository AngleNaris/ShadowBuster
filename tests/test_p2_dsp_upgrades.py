"""Unit tests for P2 professional mastering & low-end upgrades:
  - P2-1: mastering.resonance.tame_resonances (bounded dynamic resonance suppression)
  - P2-2: bass_enhance asymmetric even+odd harmonic saturation (unit small-signal gain, zero DC)
  - P2-3: mastering.soundstage.tighten_low_side (gentle 12 dB/oct Side Mono-Maker)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apollo_scripts"))

import bass_enhance as bass  # noqa: E402
from mastering.resonance import MAX_ATTENUATION_DB, tame_resonances  # noqa: E402
from mastering.soundstage import tighten_low_side  # noqa: E402

SR = 44100


def _band_energy(x, lo, hi):
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=SR, output="sos")
    return float(np.sum(signal.sosfiltfilt(sos, x, axis=0, padlen=0) ** 2))


# ── P2-1: Dynamic resonance suppression ──────────────────────────────────

def test_resonance_zero_amount_is_bit_exact():
    rng = np.random.default_rng(1)
    x = rng.standard_normal((SR, 2)) * 0.1
    out, rep = tame_resonances(x, SR, amount=0.0)
    np.testing.assert_array_equal(out, x)
    assert not rep["applied"]


def test_resonance_tames_narrowband_mud_and_glare_only():
    t = np.arange(SR * 2) / SR
    rng = np.random.default_rng(42)
    # Smooth broadband bed + harsh narrowband resonances at 320 Hz and 3200 Hz + out-of-band 1 kHz
    bed = rng.standard_normal(len(t)) * 0.03
    res_mud = 0.14 * np.sin(2 * np.pi * 320.0 * t)
    res_glare = 0.14 * np.sin(2 * np.pi * 3200.0 * t)
    mid_ref = 0.10 * np.sin(2 * np.pi * 1000.0 * t)
    mono = bed + res_mud + res_glare + mid_ref
    stereo = np.column_stack((mono, mono * 0.55))

    out, rep = tame_resonances(stereo, SR, amount=1.0)
    assert rep["applied"]
    assert rep["bands"]["low_mid_mud"]["max_attenuation_db"] <= MAX_ATTENUATION_DB + 1e-6
    assert rep["bands"]["upper_mid_glare"]["max_attenuation_db"] <= MAX_ATTENUATION_DB + 1e-6

    # Both narrowband resonances are measurably tamed
    mud_drop = 10 * np.log10(_band_energy(out, 290, 350) / _band_energy(stereo, 290, 350))
    glare_drop = 10 * np.log10(_band_energy(out, 3050, 3350) / _band_energy(stereo, 3050, 3350))
    assert mud_drop < -1.0
    assert glare_drop < -1.0

    # Out-of-band 1 kHz is untouched
    ref_delta = 10 * np.log10(_band_energy(out, 900, 1100) / _band_energy(stereo, 900, 1100))
    assert abs(ref_delta) < 0.08

    # Stereo image (L/R ratio) is strictly preserved (linked attenuation mask)
    np.testing.assert_allclose(out[:, 1], out[:, 0] * 0.55, atol=1e-10)

    # Channel swap symmetry
    swapped, _ = tame_resonances(stereo[:, ::-1], SR, amount=1.0)
    np.testing.assert_allclose(swapped[:, ::-1], out, atol=1e-10)


def test_resonance_leaves_smooth_broadband_signal_mostly_untouched():
    rng = np.random.default_rng(7)
    x = rng.standard_normal((SR * 2, 2)) * 0.1
    out, _ = tame_resonances(x, SR, amount=0.8)
    delta_db = 10 * np.log10(np.mean(out ** 2) / np.mean(x ** 2))
    assert abs(delta_db) < 0.15


# ── P2-2: Asymmetric even+odd harmonic bass saturation ───────────────────

def test_asymmetric_saturation_adds_2nd_harmonic_with_unit_small_signal_gain_and_no_dc():
    t = np.arange(SR * 2) / SR
    # 1. Small-signal unity gain: weak 60 Hz fundamental is NOT boosted
    weak = 0.01 * np.sin(2 * np.pi * 60.0 * t)
    y_weak = bass.saturation_wet(weak, drive=1.6, even=0.5, sr=SR)
    sec = slice(SR // 2, -SR // 2)
    gain_db = 10 * np.log10(np.mean(y_weak[sec] ** 2) / np.mean(weak[sec] ** 2))
    assert abs(gain_db) < 0.02

    # 2. Strong 60 Hz signal: generates rich 2nd harmonic (120 Hz) + keeps 3rd (180 Hz) + zero DC
    strong = 0.6 * np.sin(2 * np.pi * 60.0 * t)
    y_odd = bass.saturation_wet(strong, drive=1.6, even=0.0, sr=SR)[sec]
    y_even = bass.saturation_wet(strong, drive=1.6, even=0.6, sr=SR)[sec]
    ts = np.arange(len(y_even)) / SR
    harm = lambda sig, hz: 2.0 * abs(np.mean(sig * np.exp(-2j * np.pi * hz * ts)))
    assert harm(y_odd, 120.0) / harm(y_odd, 60.0) < 1e-4
    assert harm(y_even, 120.0) / harm(y_even, 60.0) > 0.02
    assert harm(y_even, 180.0) / harm(y_even, 60.0) > 0.01
    # DC offset removed
    assert abs(float(np.mean(y_even))) < 1e-4


# ── P2-3: Gentle low-end Side Mono-Maker ─────────────────────────────────

def test_tighten_low_side_centers_sub_side_while_preserving_mid_and_high_width():
    t = np.arange(SR * 2) / SR
    mid = 0.25 * np.sin(2 * np.pi * 60.0 * t) + 0.15 * np.sin(2 * np.pi * 1500.0 * t)
    side = 0.18 * np.sin(2 * np.pi * 45.0 * t) + 0.12 * np.sin(2 * np.pi * 1500.0 * t)
    stereo = np.column_stack((mid + side, mid - side))

    out0, rep0 = tighten_low_side(stereo, SR, cutoff_hz=90.0, amount=0.0)
    np.testing.assert_array_equal(out0, stereo)
    assert not rep0["applied"]

    out, rep = tighten_low_side(stereo, SR, cutoff_hz=90.0, amount=0.8)
    assert rep["applied"] and rep["low_side_change_db"] < -5.0

    # Mid is bit-exact untouched
    out_mid = (out[:, 0] + out[:, 1]) * 0.5
    np.testing.assert_allclose(out_mid, mid, atol=1e-12)

    # Low Side (45 Hz) attenuated strongly; high Side (1500 Hz) untouched
    out_side = (out[:, 0] - out[:, 1]) * 0.5
    low_drop = 10 * np.log10(_band_energy(out_side[:, None], 30, 65) / _band_energy(side[:, None], 30, 65))
    high_delta = 10 * np.log10(_band_energy(out_side[:, None], 1200, 1800) / _band_energy(side[:, None], 1200, 1800))
    assert low_drop < -6.0
    assert abs(high_delta) < 0.05

    # Channel swap symmetry
    swapped, _ = tighten_low_side(stereo[:, ::-1], SR, cutoff_hz=90.0, amount=0.8)
    np.testing.assert_allclose(swapped[:, ::-1], out, atol=1e-12)
