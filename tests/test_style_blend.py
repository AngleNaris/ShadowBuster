"""Style strength controls bounded processing, not terminal waveform mixing."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
from soren_resources import SOREN_RESOURCE_DIR
sys.path.insert(0, str(SOREN_RESOURCE_DIR))
sys.path.insert(0, str(ROOT))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load('strength_core', 'soren_core.py')
original = load('strength_original', 'soren_original.py')


def music(seed=5, seconds=2):
    rng = np.random.default_rng(seed)
    mid = signal.sosfilt(signal.butter(2, 12000, fs=44100, output='sos'),
                        rng.normal(0, .04, 44100 * seconds))
    side = rng.normal(0, .01, mid.shape)
    return original.ms_to_lr(mid, side)


def band_gain(output, source, lo, hi):
    f, p = signal.welch(source, 44100, nperseg=8192)
    _, q = signal.welch(output, 44100, nperseg=8192)
    band = (f >= lo) & (f <= hi)
    return 10 * np.log10(np.sum(q[..., band]) / np.sum(p[..., band]))


def windowed_brightness(audio):
    mid = np.mean(audio, axis=0)
    freqs, _, spectrum = signal.stft(
        mid,
        fs=44100,
        nperseg=4096,
        noverlap=2048,
        boundary=None,
        padded=False,
    )
    power = np.abs(spectrum) ** 2
    body = power[(freqs >= 250) & (freqs < 2000)].sum(axis=0) + 1e-24
    high = power[(freqs >= 4000) & (freqs < 16000)].sum(axis=0) + 1e-24
    wide = power[(freqs >= 40) & (freqs < 20000)].sum(axis=0) + 1e-24
    return 10 * np.log10(high / body), 10 * np.log10(wide)


def test_strength_forwarded_without_terminal_mix(monkeypatch):
    captured = {}
    wet = music()

    class StubOriginal:
        class Config:
            pass

        @staticmethod
        def process_audio(target, reference, step, config, profile):
            captured['strength'] = config.style_strength
            return wet.copy()

    def finalize(target, cfg, requested, module):
        captured['target'] = target.copy()
        cfg.last_mastering_stats = {}
        return target

    monkeypatch.setattr(core, 'load_soren_original_module', lambda: StubOriginal)
    monkeypatch.setattr(core, 'process_transparent', finalize)
    cfg = core.Config()
    cfg.style_blend = .5
    out = core.process_audio(music(12), wet, 5, cfg, {'genre': 'Pop', 'lufs': -9})
    assert captured['strength'] == .5
    np.testing.assert_array_equal(captured['target'], wet)
    np.testing.assert_array_equal(out, wet)
    assert cfg.last_mastering_stats['strength_semantics'] == 'processing_amount'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -.1, 1.1])
def test_invalid_strength_rejected(value):
    cfg = core.Config()
    cfg.style_blend = value
    with pytest.raises(ValueError, match='strength'):
        core.process_audio(music(), music(7), 5, cfg, {'lufs': -12})


def test_zero_strength_transparent_but_explicit_eq_survives():
    audio = music()
    cfg = original.Config()
    cfg.style_strength = 0
    np.testing.assert_array_equal(original.process_audio(audio, None, 5, cfg), audio)
    cfg.eq_style = 'Bright'
    out = original.process_audio(audio, None, 5, cfg)
    assert band_gain(out, audio, 10000, 14000) > .9
    np.testing.assert_array_equal(audio, music())


@pytest.mark.parametrize('strength', [0, .25, .5, .75, 1])
def test_complete_chain_loudness_and_no_parallel_notch(strength, monkeypatch):
    monkeypatch.setattr(core, 'load_soren_original_module', lambda: original)
    audio = music()
    reference = signal.sosfilt(signal.butter(4, 4000, fs=44100, output='sos'), music(6), axis=-1)
    cfg = core.Config()
    cfg.style_blend = strength
    cfg.reference_bandwidth_hz = 5000
    out = core.process_audio(audio, reference, 5, cfg, {'genre': 'Pop', 'lufs': -12})
    stats = cfg.last_mastering_stats
    assert stats['target_met']
    assert abs(stats['actual_lufs'] + 12) <= .2
    assert stats['true_peak_dbtp'] <= -.4
    rms_gain = 10 * np.log10(np.mean(out**2) / np.mean(audio**2))
    for lo, hi in ((4000, 6000), (6000, 8000), (8000, 10000), (12000, 16000)):
        assert abs(band_gain(out, audio, lo, hi) - rms_gain) < 1.5


@pytest.mark.parametrize('strength', [.5, 1])
def test_style_does_not_darken_active_short_windows(strength, monkeypatch):
    monkeypatch.setattr(core, 'load_soren_original_module', lambda: original)
    audio = music(seconds=6)
    reference = signal.sosfilt(
        signal.butter(4, 4000, fs=44100, output='sos'),
        music(6, seconds=6),
        axis=-1,
    )

    def render(value):
        cfg = core.Config()
        cfg.style_blend = value
        cfg.reference_bandwidth_hz = 5000
        output = core.process_audio(audio, reference, 5, cfg, {'genre': 'Pop', 'lufs': -12})
        return output, cfg.last_mastering_stats

    transparent, _ = render(0)
    styled, stats = render(strength)
    base_brightness, base_level = windowed_brightness(transparent)
    styled_brightness, _ = windowed_brightness(styled)
    active = base_level >= np.percentile(base_level, 20)
    delta = styled_brightness[active] - base_brightness[active]

    # Whole-track averages must not mask severe short-time high-frequency loss.
    assert np.percentile(delta, 5) > -1.0
    assert np.median(delta) > -0.5
    spectral = stats['style_processing']
    assert spectral['spectral_mid_high_vs_body_db'] >= -.35 * strength - 1e-6
    assert spectral['spectral_mid_darkening_guard_db'] > 0


def test_upstream_eq_and_width_survive_full_master(monkeypatch):
    monkeypatch.setattr(core, 'load_soren_original_module', lambda: original)
    source = music(23)
    mid, side = original.lr_to_ms(source)
    mid = original.low_shelf_tighten(mid, 44100, 200, 10**(3/20))
    mid = original.high_shelf_eq(mid, 44100, 8000, 3)
    changed = original.ms_to_lr(mid, side * 10**(3/20))
    outputs = []
    for audio in (source, changed):
        cfg = core.Config()
        cfg.style_blend = 1
        out = core.process_audio(audio, source, 5, cfg, {'genre': 'Pop', 'lufs': -12})
        outputs.append(out * 10**((-12 - core.calculate_lufs(out, 44100))/20))
    baseline, adjusted = outputs
    center = band_gain(adjusted, baseline, 700, 1400)
    assert band_gain(adjusted, baseline, 40, 130) - center > 1.7
    assert band_gain(adjusted, baseline, 10000, 15000) - center > 1.7
    def width(x):
        m, s = original.lr_to_ms(x)
        return 10*np.log10(np.mean(s*s)/np.mean(m*m))
    assert width(adjusted) - width(baseline) > 1.0


def test_genres_differ_in_linked_dynamics():
    t = np.arange(44100*2) / 44100
    carrier = np.sin(2*np.pi*997*t) * (.03 + .4*(np.sin(2*np.pi*2*t) > .8))
    audio = np.vstack([carrier, carrier * .5])
    stats = []
    for genre in ('Pop', 'Orchestral', 'EDM'):
        cfg = original.Config()
        cfg.oversampling_factor = 1
        out = original.apply_style_dynamics(audio, cfg, {'genre': genre})
        stats.append(cfg.last_mastering_stats['dynamics'])
        assert stats[-1]['max_gain_reduction_db'] <= stats[-1]['budget_db'] + 1e-6
        assert out.shape == audio.shape and np.isfinite(out).all()
        np.testing.assert_allclose(out[1], out[0] * .5, atol=1e-12)
    assert stats[0]['max_gain_reduction_db'] > stats[1]['max_gain_reduction_db'] + .3
    assert len({s['attack_ms'] for s in stats}) == 3


def test_loud_target_and_dynamic_budget():
    audio = music()
    cfg = core.Config()
    cfg.style_mode = 'off'
    cfg.loudness_option = 'loud'
    profile = {'lufs': -9.199554764198626}
    out = core.process_audio(audio, None, 5, cfg, profile)
    assert core.loudness_target_lufs(profile, 'loud') == pytest.approx(-6.6995547642)
    assert abs(core.calculate_lufs(out, 44100) + 6.6995547642) <= .2
    assert cfg.last_mastering_stats['target_status'] == 'met'
    assert cfg.last_mastering_stats['limiter_release_ms'] == 80.0
    assert cfg.limiter_release_ms == 150.0
    cfg.limiter_p95_budget_db = .1
    cfg.limiter_peak_budget_db = .2
    out = core.process_audio(audio, None, 5, cfg, profile)
    stats = cfg.last_mastering_stats
    assert not stats['target_met'] and stats['dynamic_budget_limited']
    assert stats['target_status'] == 'below_target'
    assert stats['limiter']['gain_reduction_p95_db'] <= .100001
    assert stats['true_peak_dbtp'] <= -.4


def test_quiet_input_uses_relative_not_absolute_drive_limit():
    audio = music() * .01
    cfg = core.Config()
    cfg.style_mode = 'off'
    cfg.loudness_option = 'loud'
    out = core.process_audio(audio, None, 5, cfg, {'lufs': -9.2})
    assert cfg.last_mastering_stats['applied_gain_db'] > 30
    assert cfg.last_mastering_stats['target_met']
    assert abs(core.calculate_lufs(out, 44100) + 6.7) < .2


def test_stage_soren_passes_style_blend(monkeypatch, tmp_path):
    import studio_backend as backend
    captured = {}
    monkeypatch.setattr(backend, '_run_stream', lambda cmd, *a, **k: captured.update(cmd=list(cmd)))
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: backend.DEV_SOREN_RUNTIME)
    backend.stage_soren(tmp_path / 'in.wav', tmp_path / 'out.wav', style_blend=.7)
    cmd = captured['cmd']
    assert cmd[cmd.index('--style-blend') + 1] == '0.7'
