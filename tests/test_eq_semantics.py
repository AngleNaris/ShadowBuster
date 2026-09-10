"""Mastering EQ semantics tests.

Verifies actual frequency response (sine probes and FFT band energies), not
config values. Canonical curves live in packaging/soren_original.py;
packaging/soren_core.py must delegate so styled and eq_only never diverge.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'packaging/soren_core.py'
ORIGINAL = ROOT / 'packaging/soren_original.py'
from soren_resources import SOREN_RESOURCE_DIR  # noqa: E402

sys.path.insert(0, str(SOREN_RESOURCE_DIR))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load('soren_eq_semantics_core', CORE)
original = _load('soren_eq_semantics_original', ORIGINAL)

STYLES = ('Warm', 'Bright', 'Fusion')


def _band_gain_db(apply_fn, style, freq, sample_rate, channel='mid'):
    """Steady-state gain of the EQ at `freq` via a sine probe (dB)."""
    n = 1 << 16
    t = np.arange(n) / sample_rate
    probe = np.sin(2 * np.pi * freq * t)
    mid_in = probe if channel == 'mid' else np.zeros_like(probe)
    side_in = np.zeros_like(probe) if channel == 'mid' else probe
    mid_out, side_out = apply_fn(mid_in.copy(), side_in.copy(), sample_rate, style)
    out = mid_out if channel == 'mid' else side_out
    half = slice(n // 2, None)
    ref_rms = np.sqrt(np.mean(probe[half] ** 2))
    out_rms = np.sqrt(np.mean(out[half] ** 2))
    return 20 * np.log10(out_rms / ref_rms)


def _band_energy_db(x, sample_rate, center, width):
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.shape[-1], 1 / sample_rate)
    mask = (freqs >= center - width) & (freqs <= center + width)
    return 20 * np.log10(np.sqrt(np.mean(spec[mask] ** 2)))


def test_neutral_is_strict_identity():
    rng = np.random.default_rng(7)
    for sample_rate in (44100, 96000):
        mid = rng.normal(0, 0.1, 2048)
        side = rng.normal(0, 0.05, 2048)
        for apply_fn in (original.apply_eq_style, core.apply_eq_style):
            out_mid, out_side = apply_fn(mid.copy(), side.copy(), sample_rate, 'Neutral')
            assert np.array_equal(out_mid, mid)
            assert np.array_equal(out_side, side)


def test_core_delegates_to_canonical_bitwise():
    rng = np.random.default_rng(11)
    mid = rng.normal(0, 0.1, 4096)
    side = rng.normal(0, 0.05, 4096)
    for style in STYLES:
        core_out = core.apply_eq_style(mid.copy(), side.copy(), 44100, style)
        canon_out = original.apply_eq_style(mid.copy(), side.copy(), 44100, style)
        np.testing.assert_array_equal(core_out, canon_out)


def test_bright_response_44k():
    g = lambda f, ch='mid': _band_gain_db(original.apply_eq_style, 'Bright', f, 44100, ch)
    # Air shelves on BOTH mid and side, relative to untouched low mids.
    assert g(12000) - g(1000) >= 1.0
    assert 1.0 <= g(14000) <= 2.0
    assert g(12000, 'side') - g(1000, 'side') >= 1.2
    # The former ~550 Hz boost (vocal thickening) is gone.
    assert abs(g(550)) <= 0.3
    # Presence is moderate, not harsh.
    assert 0.6 <= g(3000) <= 1.4
    # No low boost added to the mid; side low-mid cleanup retained.
    assert g(200) <= 0.3
    assert -1.2 <= g(250, 'side') <= -0.3


def test_warm_warms_without_muffling():
    g = lambda f, ch='mid': _band_gain_db(original.apply_eq_style, 'Warm', f, 44100, ch)
    assert 0.7 <= g(80) <= 1.9          # light low shelf
    assert abs(g(1000)) <= 0.4
    assert abs(g(2500)) <= 0.4          # old -1 dB presence cut removed
    assert abs(g(10000)) <= 0.3         # mono air untouched (no mid air cut)
    assert abs(g(10000, 'side')) <= 0.3  # no broadband air cut on side
    assert abs(g(300, 'side')) <= 0.3    # old +0.5 dB side low boost removed


def test_fusion_is_mild_tilt():
    probes = (80, 300, 550, 1000, 3000, 6000, 12000)
    g = lambda f, ch='mid': _band_gain_db(original.apply_eq_style, 'Fusion', f, 44100, ch)
    assert 0.3 <= g(80) <= 1.4
    assert g(12000) - g(1000) >= 0.4
    assert g(12000, 'side') - g(1000, 'side') >= 0.4
    assert g(3000) <= 0.6  # no presence emphasis
    for ch in ('mid', 'side'):
        moves = [g(f, ch) for f in probes]
        assert max(abs(m) for m in moves) <= 1.2  # every move stays gentle


def test_mono_input_stays_mono():
    rng = np.random.default_rng(23)
    left = right = rng.normal(0, 0.1, (1, 4096))[0]
    stereo = np.vstack([left, right])
    for style in ('Neutral',) + STYLES:
        mid, side = core.lr_to_ms(stereo.copy())
        mid, side = core.apply_eq_style(mid, side, 44100, style)
        out = core.ms_to_lr(mid, side)
        np.testing.assert_allclose(out[0], out[1], rtol=0, atol=1e-12)


def test_shelves_are_correct_at_high_sample_rates():
    bright_mid = lambda f: _band_gain_db(original.apply_eq_style, 'Bright', f, 96000)
    assert bright_mid(16000) - bright_mid(1000) >= 1.0
    warm_mid = lambda f: _band_gain_db(original.apply_eq_style, 'Warm', f, 96000)
    assert warm_mid(80) >= 0.7
    fusion_side = lambda f: _band_gain_db(original.apply_eq_style, 'Fusion', f, 96000, 'side')
    assert fusion_side(16000) - fusion_side(1000) >= 0.4
    bright_mid_192 = lambda f: _band_gain_db(original.apply_eq_style, 'Bright', f, 192000)
    assert bright_mid_192(16000) - bright_mid_192(1000) >= 1.0
    assert abs(bright_mid_192(550)) <= 0.3


def test_eq_only_end_to_end_matches_canonical_semantics(monkeypatch):
    cfg = core.Config()
    cfg.style_mode = 'eq_only'
    rng = np.random.default_rng(31)
    audio = rng.normal(0, 0.01, (2, 2 * 44100))

    def transparent(target, config, requested, module):
        config.last_mastering_stats = {'transparent_stub': True}
        return target

    monkeypatch.setattr(core, 'process_transparent', transparent)

    def lift(style, center, width):
        cfg.eq_style = style
        out = core.process_audio(audio.copy(), None, 5, cfg, {'lufs': -12})
        stats = cfg.last_mastering_stats
        assert stats['style_mode'] == 'eq_only' and stats['eq_profile'] == style
        assert stats['spectral_processing'] is True
        before = _band_energy_db(audio[0], 44100, center, width)
        after = _band_energy_db(out[0], 44100, center, width)
        return after - before

    bright_air = lift('Bright', 12000, 300) - lift('Bright', 500, 100)
    assert bright_air >= 0.8
    bright_presence = lift('Bright', 3000, 200) - lift('Bright', 500, 100)
    assert bright_presence >= 0.4
    bright_550 = abs(lift('Bright', 550, 50) - lift('Bright', 500, 100))
    assert bright_550 <= 0.4
    warm_air = lift('Warm', 12000, 300) - lift('Warm', 500, 100)
    assert abs(warm_air) <= 0.4  # Warm must not dull the air
    warm_low = lift('Warm', 80, 30)
    assert warm_low >= 0.5


def test_bright_air_has_no_crossover_notch():
    for sample_rate in (44100, 48000, 96000):
        for channel in ('mid', 'side'):
            for freq in (6000, 8000, 9000, 10000, 12000, 16000):
                gain = _band_gain_db(original.apply_eq_style, 'Bright', freq, sample_rate, channel)
                assert gain >= -0.02


def test_styled_eq_runs_after_spectral_matching():
    text = ORIGINAL.read_text(encoding='utf-8')
    p_match = text.rfind('match_frequencies_ms(target_mid', 0, text.find('# Apply EQ style after frequency matching'))
    p_eq = text.find('target_mid, target_side = apply_eq_style')
    p_lowpass = text.find('apply_lowpass_filter(result, config)', p_eq)
    assert -1 < p_match < p_eq < p_lowpass
