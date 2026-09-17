import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apollo_scripts"))
import bass_enhance as bass

SR = 44100


def tone(freq, seconds=2.0, amp=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def kick_train(seconds=2.0):
    n = int(SR * seconds)
    drums = np.zeros((n, 1), dtype=np.float64)
    for start in (0.35, 0.85, 1.35, 1.85):
        i = int(start * SR)
        length = min(int(0.16 * SR), n - i)
        t = np.arange(length) / SR
        drums[i:i + length, 0] += 0.9 * np.exp(-22 * t) * np.sin(2 * np.pi * 70 * t)
        click = int(0.006 * SR)
        drums[i:i + click, 0] += 0.35 * np.exp(-180 * t[:click]) * np.sin(2 * np.pi * 1800 * t[:click])
    return drums


def band_rms(x, lo, hi):
    sos = signal.butter(3, [lo, hi], btype="bandpass", fs=SR, output="sos")
    y = signal.sosfiltfilt(sos, np.asarray(x), axis=0, padlen=0)
    return float(np.sqrt(np.mean(y * y)))


def test_kick_train_ducks_bass_and_recovers_smoothly():
    bass_in = np.column_stack((tone(60, amp=0.45), tone(60, amp=0.45)))
    drums = kick_train()
    report = {}
    out = bass.apply_bass_sidechain(
        bass_in, SR, drums, amount=1.0, attack_ms=5, release_ms=150,
        max_duck_db=6, report=report)
    assert report["applied"]
    assert 0 < report["duck_db_max"] <= 6
    assert band_rms(out, 40, 100) < band_rms(bass_in, 40, 100)
    # The release is smoothed, not an instantaneous step.
    env, conf = bass.detect_kick_envelope(drums, SR)
    assert conf > 0 and np.max(env) > 0
    duck = bass._follower(6 * env, SR, 5, 150)
    assert np.max(np.abs(np.diff(duck))) < 0.1


def test_amount_zero_is_exact_identity_and_reports_off():
    x = np.column_stack((tone(60), tone(60) * 0.7)).astype(np.float32)
    report = {}
    out = bass.apply_bass_sidechain(x, SR, kick_train(), amount=0, report=report)
    np.testing.assert_array_equal(out, x)
    assert report["applied"] is False and report["duck_db_max"] == 0.0


def test_non_kick_drums_do_not_trigger():
    t = np.arange(int(SR * 2.0)) / SR
    snare = (0.5 * np.sin(2 * np.pi * 2200 * t) * (np.mod(t, 0.5) < 0.02))[:, None]
    out, conf = bass.detect_kick_envelope(snare, SR)
    assert conf == 0.0
    assert np.max(out) == 0.0


def test_channels_share_one_duck_curve_and_high_band_is_preserved():
    bass_in = np.column_stack((tone(60, amp=0.4), tone(60, amp=0.2)))
    bass_in += np.column_stack((tone(700, amp=0.1), tone(700, amp=0.05)))
    out = bass.apply_bass_sidechain(bass_in, SR, kick_train(), amount=0.8)
    low_delta_l = bass_in[:, 0] - out[:, 0]
    low_delta_r = bass_in[:, 1] - out[:, 1]
    assert np.isclose(band_rms(low_delta_l, 40, 100) / 0.4,
                      band_rms(low_delta_r, 40, 100) / 0.2, rtol=0.08)
    np.testing.assert_allclose(
        band_rms(out, 600, 900), band_rms(bass_in, 600, 900), rtol=0.03)


@pytest.mark.parametrize("bad", [-0.1, 1.1, np.nan, np.inf])
def test_invalid_amount_rejected(bad):
    with pytest.raises(ValueError):
        bass.apply_bass_sidechain(tone(60), SR, kick_train(), amount=bad)


def test_short_nonfinite_and_mismatched_inputs_are_safe():
    x = tone(60, seconds=0.05)
    out = bass.apply_bass_sidechain(x, SR, np.zeros_like(x), amount=1)
    np.testing.assert_array_equal(out, x)
    bad = x.copy()
    bad[0] = np.nan
    out = bass.apply_bass_sidechain(bad, SR, kick_train()[:len(bad)], amount=1)
    np.testing.assert_array_equal(out, bad)
    # A longer drums stem is aligned safely to the bass duration.
    out = bass.apply_bass_sidechain(x, SR, kick_train()[:-1], amount=1)
    assert len(out) == len(x)


def test_report_contains_attack_release_and_bound():
    report = {}
    bass.apply_bass_sidechain(tone(60), SR, kick_train(), amount=1,
                              attack_ms=9, release_ms=210, max_duck_db=4,
                              report=report)
    assert report["attack_ms"] == 9.0
    assert report["release_ms"] == 210.0
    assert report["max_duck_db"] == 4.0
    assert report["duck_db_max"] <= report["max_duck_db"]
