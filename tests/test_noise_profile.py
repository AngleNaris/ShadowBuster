"""Properties of opt-in high-band denoising; not a listening-quality benchmark."""
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apollo_scripts"))
import noise_profile as noise

SR = 44100


def hiss(seconds=3, seed=17):
    rng = np.random.default_rng(seed)
    data = rng.normal(size=int(seconds * SR))
    sos = signal.butter(4, [8500, 19500], btype="bandpass", fs=SR, output="sos")
    return signal.sosfiltfilt(sos, data) * 0.05


def sine(hz, seconds=3):
    return 0.1 * np.sin(2 * np.pi * hz * np.arange(int(seconds * SR)) / SR)


def test_steady_hiss_is_reduced_with_bounded_mask_and_band_energy():
    x = hiss()
    report = {}
    y = noise.adaptive_denoise(x, SR, amount=1, report=report)
    assert report['applied'], report
    assert report['mask_attenuation_db_max'] <= 6
    assert report['mask_attenuation_db_p95'] > 0
    assert -6 - 1e-8 <= report['band_energy_change_db'] < -0.1
    assert np.max(abs(y)) <= np.max(abs(x)) + 1e-12
    assert y.shape == x.shape and y.dtype == x.dtype and np.isfinite(y).all()


@pytest.mark.parametrize('kind', ['tone', 'harmonics', 'cymbal', 'sibilance', 'silence', 'short'])
def test_uncertain_or_musical_signals_skip_exactly(kind):
    if kind == 'tone':
        x = sine(12000)
    elif kind == 'harmonics':
        x = sum(sine(hz) / i for i, hz in enumerate(range(1000, 20000, 1000), 1))
    elif kind in ('cymbal', 'sibilance'):
        t = np.arange(SR * 3) / SR
        if kind == 'cymbal':
            env = np.zeros(len(t))
            for start in (.3, 1.2, 2.1):
                env += np.where(t >= start, np.exp(-np.maximum(t - start, 0) / .07), 0)
        else:
            env = ((t > .4) & (t < .55) | (t > 1.4) & (t < 1.55)).astype(float)
        x = hiss() * env
    elif kind == 'silence':
        x = np.zeros(SR * 3)
    else:
        x = hiss()[:100]
    report = {}
    y = noise.adaptive_denoise(x, SR, 1, report=report)
    np.testing.assert_array_equal(y, x)
    assert not report['applied'], report


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
def test_zero_amount_identity(dtype):
    x = np.column_stack((hiss(), -hiss())).astype(dtype)
    report = {}
    np.testing.assert_array_equal(noise.adaptive_denoise(x, SR, 0, report=report), x)
    assert report['reason'] == 'disabled'


def test_linked_mask_preserves_polarity_and_channel_ratio():
    mono = hiss()
    x = np.column_stack((mono, -.5 * mono))
    r = {}
    y = noise.adaptive_denoise(x, SR, 1, report=r)
    assert r['applied'], r
    np.testing.assert_allclose(y[:, 1], -.5 * y[:, 0], atol=1e-12)
    assert r['channel_spectral_correlation'] == pytest.approx(-1)


def test_midrange_not_processed_as_noise():
    low = sine(1000)
    x = hiss() + low
    y = noise.adaptive_denoise(x, SR, 1)
    sos = signal.butter(6, 6000, btype='lowpass', fs=SR, output='sos')
    delta = signal.sosfiltfilt(sos, y - x)[2048:-2048]
    assert np.sqrt(np.mean(delta ** 2)) < 1e-4


def test_peak_or_cancellation_growth_rejects_delta():
    x = np.column_stack((hiss(), hiss()))
    accepted, report = noise.constrain_noise_delta(x, .1 * x, SR)
    np.testing.assert_array_equal(accepted, np.zeros_like(x))
    assert not report['applied']
    accepted, report = noise.constrain_noise_delta(x, -10 * x, SR)
    assert report['applied'] and -6 - 1e-8 <= report['band_energy_change_db'] <= 0
    assert np.max(abs(x + accepted)) <= np.max(abs(x))


def test_chunk_boundaries_and_late_noise(monkeypatch):
    x = np.r_[sine(12000, 2), hiss(4)]
    a = noise.adaptive_denoise(x, SR, 1)
    monkeypatch.setattr(noise, 'BLOCK_SECONDS', 2.0)
    r = {}
    b = noise.adaptive_denoise(x, SR, 1, report=r)
    assert r['analysis_blocks'] >= 3 and r['applied'], r
    np.testing.assert_allclose(a, b, atol=2e-4, rtol=0)
    assert np.mean(b[-SR:] ** 2) < np.mean(x[-SR:] ** 2)


@pytest.mark.parametrize('kwargs', [dict(amount=-1), dict(amount=np.nan),
    dict(max_attenuation_db=7), dict(low_hz=7000), dict(high_hz=23000),
    dict(low_hz=18000, high_hz=12000), dict(sr=8000)])
def test_invalid_parameters_rejected(kwargs):
    options = dict(sr=SR, amount=1)
    options.update(kwargs)
    with pytest.raises(ValueError):
        noise.adaptive_denoise(hiss(), **options)


def test_independent_channel_noise_keeps_energy_balance():
    x = np.column_stack((hiss(), .6 * hiss(seed=23)))
    y = noise.adaptive_denoise(x, SR, 1)
    before = np.sqrt(np.mean(x[:, 0] ** 2) / np.mean(x[:, 1] ** 2))
    after = np.sqrt(np.mean(y[:, 0] ** 2) / np.mean(y[:, 1] ** 2))
    assert abs(20 * np.log10(after / before)) < .5
    assert abs(np.corrcoef(y.T)[0, 1] - np.corrcoef(x.T)[0, 1]) < .01


def test_music_correlated_high_band_remains_unprocessed():
    t = np.arange(3 * SR) / SR
    envelope = .6 + .35 * np.sin(2 * np.pi * 4 * t)
    x = envelope * (hiss() + sine(1000))
    report = {}
    y = noise.adaptive_denoise(x, SR, 1, report=report)
    np.testing.assert_array_equal(y, x)
    assert not report['applied']


def test_invalid_audio_rejected():
    for x in [np.array([np.nan]), np.array([np.inf]), np.zeros((10, 3)), np.zeros(100, dtype=int)]:
        with pytest.raises(ValueError):
            noise.adaptive_denoise(x, SR, 1)
