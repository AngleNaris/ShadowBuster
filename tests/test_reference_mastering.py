"""Reference integration: real Matchering plus independent safety contracts."""
import importlib.util
import json
import shutil
from pathlib import Path
import sys

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

import studio_backend as backend
from mastering import guard
from mastering import pipeline
from mastering.matchering_adapter import candidate, UnsuitableReference
from mastering.soundstage import widen
from mastering.tone import apply_eq

SR = 44100
needs_matchering = pytest.mark.skipif(importlib.util.find_spec("matchering") is None,
                                    reason="matchering==2.0.6 is required for real adapter tests")


def audio(seconds=1):
    rng = np.random.default_rng(20260919)
    mid = signal.sosfilt(signal.butter(1, 7000, fs=SR, output="sos"), rng.normal(0, .1, int(SR*seconds)))
    side = signal.sosfilt(signal.butter(1, 10000, fs=SR, output="sos"), rng.normal(0, .02, len(mid)))
    return np.column_stack((mid + side, mid - side))


def write(path, x, sr=SR):
    sf.write(path, x, sr, subtype="FLOAT")
    return path


@needs_matchering
@pytest.mark.parametrize("rate", [44100, 48000, 88200, 96000])
def test_real_adapter_preserves_reference_rate_and_source_length(tmp_path, rate):
    x = audio()
    ref = signal.resample(x * .75, rate, axis=0)
    source, out, stats = candidate(write(tmp_path/'source.wav', x),
                                    write(tmp_path/'ref.wav', ref, rate))
    assert out.shape == source.shape == x.shape and np.isfinite(out).all()
    assert stats['reference_sample_rate'] == rate and stats['candidate_subtype'] == 'FLOAT'
    assert stats['use_limiter'] is False and stats['normalize'] is False


@needs_matchering
def test_mono_reference_is_accepted_without_collapsing_stereo(tmp_path):
    x = audio()
    src, ref, dst = tmp_path/'source.wav', tmp_path/'ref.wav', tmp_path/'out.wav'
    write(src, x); write(ref, x.mean(axis=1))
    stats = pipeline.master_file(src, dst, 'soft', reference=ref)
    y, _ = sf.read(dst)
    assert np.mean((y[:, 0] - y[:, 1])**2) > 0
    assert stats['readback_verified'] and stats['true_peak_dbtp'] <= -.4
    assert stats['target_lufs'] == -12.3


@needs_matchering
@pytest.mark.parametrize('x', [np.zeros((SR, 2)), np.ones((1000, 2))*.01])
def test_unusable_reference_falls_back_with_reason(tmp_path, x):
    src = write(tmp_path/'source.wav', audio())
    ref = write(tmp_path/'ref.wav', x)
    stats = pipeline.master_file(src, tmp_path/'out.wav', 'soft', reference=ref)
    assert stats['reference']['status'] == 'fallback'
    assert stats['reference']['accepted_strength'] == 0
    assert stats['reference']['reason']
    assert stats['engine'].startswith('shadowbuster')


def test_zero_strength_never_loads_reference_or_matchering(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'candidate', lambda *a, **k: pytest.fail('Reference was loaded'))
    src = write(tmp_path/'source.wav', audio())
    stats = pipeline.master_file(src, tmp_path/'out.wav', 'soft', reference=tmp_path/'absent', strength=0)
    assert stats['reference']['reason'] == 'zero_strength'


@pytest.mark.parametrize('mode,reference,strength', [
    ('off', None, .85), ('eq_only', None, .85),
    ('styled', 'reference.wav', 0), ('styled', 'reference.wav', .65),
])
def test_global_eq_and_strength_reach_mastering(tmp_path, monkeypatch, mode, reference, strength):
    calls = []
    monkeypatch.setattr(backend, '_run_stream', lambda cmd, *a, **kw: calls.append(cmd))
    backend.stage_soren(tmp_path/'in.wav', tmp_path/'out.wav', style_mode=mode,
                        eq_profile='Bright', reference=reference, style_blend=strength,
                        lowpass_cutoff=18000)
    cmd = calls[0]
    assert cmd[cmd.index('--eq-profile') + 1] == 'Bright'
    assert cmd[cmd.index('--lowpass-cutoff') + 1] == '18000'
    if reference:
        assert float(cmd[cmd.index('--strength') + 1]) == strength
    else:
        assert '--reference' not in cmd


@pytest.mark.parametrize('reference_case', ['off', 'zero', 'accepted', 'fallback'])
def test_global_eq_changes_final_audio_in_every_reference_case(tmp_path, monkeypatch, reference_case):
    x = audio()
    src = write(tmp_path/'source.wav', x)
    def reference_candidate(*a, **kw):
        if reference_case == 'fallback':
            raise UnsuitableReference('test reference rejected')
        return x, x * .8, {'reference_bandwidth_hz': 20000}
    monkeypatch.setattr(pipeline, 'candidate', reference_candidate)
    outputs = {}
    for profile in ('Neutral', 'Bright'):
        dst = tmp_path/(profile + '.wav')
        stats = pipeline.master_file(src, dst, 'soft', eq_profile=profile,
                                    reference=None if reference_case == 'off' else 'provided',
                                    strength=0 if reference_case == 'zero' else .5)
        outputs[profile], _ = sf.read(dst)
        assert stats['eq_profile'] == profile
        expected_status = {'off': 'disabled', 'zero': 'disabled',
                           'accepted': 'accepted', 'fallback': 'fallback'}[reference_case]
        assert stats['reference']['status'] == expected_status
    assert high_to_body_db(outputs['Bright']) - high_to_body_db(outputs['Neutral']) > .8


def high_to_body_db(x):
    f, power = signal.welch(x.mean(axis=1), SR, nperseg=8192)
    return 10 * np.log10(power[(f > 11000) & (f < 16000)].sum() /
                        power[(f > 500) & (f < 1500)].sum())


@needs_matchering
def test_real_reference_strength_changes_written_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'PYTHON', Path(sys.executable))
    x = audio(2)
    src = write(tmp_path/'source.wav', x)
    ref = write(tmp_path/'reference.wav', apply_eq(x, 'Bright'))
    changes = []
    for amount in (0, .25, .5, .75):
        dst = tmp_path/f'strength_{amount}.wav'
        stats = backend.stage_soren(src, dst, style_mode='styled', reference=ref,
                                    loudness='soft', style_blend=amount)
        assert stats['reference']['requested_strength'] == amount
        assert stats['reference']['accepted_strength'] == amount
        assert stats['readback_verified']
        y, _ = sf.read(dst)
        changes.append(high_to_body_db(y))
    assert all(b > a + .03 for a, b in zip(changes, changes[1:]))


def test_parameter_strength_is_tonal_and_zero_latency():
    x = audio(2)
    # A colored + delayed Matchering-style candidate. Its delay and gain must
    # not be transferred to the actual audio.
    raw = signal.sosfiltfilt(signal.butter(1, 6000, fs=SR, output='sos'), x, axis=0) * 4
    f, db = guard.matching_curve(x, np.roll(raw, 1, axis=0), 20000)
    prior = 0
    for amount in (0, .25, .5, 1):
        y = guard.apply_curve(x, f, db, amount)
        ff, p = signal.welch(x.mean(axis=1), SR, nperseg=8192)
        _, q = signal.welch(y.mean(axis=1), SR, nperseg=8192)
        band = (ff > 7000) & (ff < 10000)
        body = (ff > 500) & (ff < 2000)
        change = 10*np.log10((q[band].sum()/q[body].sum()) / (p[band].sum()/p[body].sum()))
        assert change <= prior + .02
        prior = change
        corr = signal.correlate(y[:, 0], x[:, 0], method='fft')
        assert int(np.argmax(corr) - len(x) + 1) == 0
    np.testing.assert_array_equal(guard.apply_curve(x, f, db, 0), x)


def test_original_source_curve_does_not_cancel_upstream_air():
    x = audio(2)
    f, db = guard.matching_curve(x, x*.7, 20000)
    boosted = apply_eq(x, 'Bright')
    y = guard.apply_curve(boosted, f, db, 1)
    # Same-reference analysis yields a neutral correction even though the
    # repaired signal has explicit Bright EQ. The repair is not re-matched away.
    np.testing.assert_allclose(y[2048:-2048], boosted[2048:-2048], atol=1e-6)


def test_missing_reference_bandwidth_does_not_cut_air():
    x = audio()
    low = signal.sosfiltfilt(signal.butter(6, 5000, fs=SR, output='sos'), x, axis=0)
    f, db = guard.matching_curve(x, low, 5500)
    assert np.max(abs(db[f >= 5500])) == 0


def test_matching_guard_falls_back_without_waveform_blending(monkeypatch):
    x = audio()
    monkeypatch.setattr(guard, 'violations', lambda *a: ['forced_budget'])
    result, stats = guard.protected_match(x, x, x*.8, 20000, .8)
    np.testing.assert_array_equal(result, x)
    assert len(stats['attempts']) == 4 and stats['accepted_strength'] == 0


def test_width_monotonic_preserves_mid_and_sub_side():
    x = audio(2)
    t = np.arange(len(x)) / SR
    x += np.column_stack((.03*np.sin(2*np.pi*55*t), -.03*np.sin(2*np.pi*55*t)))
    prior = -np.inf
    for wet in np.linspace(0, 1, 11):
        y, stats = widen(x, float(wet), 6)
        np.testing.assert_allclose(y.mean(axis=1), x.mean(axis=1), atol=1e-14)
        side_energy = np.mean((y[:, 0] - y[:, 1])**2)
        assert side_energy >= prior - 1e-12
        prior = side_energy
        sos = signal.butter(4, 70, fs=SR, output='sos')
        old = signal.sosfiltfilt(sos, x[:, 0] - x[:, 1])
        new = signal.sosfiltfilt(sos, y[:, 0] - y[:, 1])
        assert abs(10*np.log10(np.mean(new**2)/np.mean(old**2))) < .03


def test_width_budget_tiers_admit_near_request_and_keep_side_below_mid():
    """频段预算阶梯（2026-09-22 放宽）：小请求全额兑现，大请求按最紧频段截断，
    任何请求下 Side 能量都不超过 Mid（单声道兼容硬上限）。"""
    x = audio(2)
    t = np.arange(len(x)) / SR
    x = x + np.column_stack((.03*np.sin(2*np.pi*55*t), -.03*np.sin(2*np.pi*55*t)))
    se = float(np.mean(((x[:, 0] - x[:, 1]) / 2)**2))
    me = float(np.mean(x.mean(axis=1)**2))
    gains = []
    for request in (1.0, 2.0, 4.0, 8.0, 12.0):
        y, stats = widen(x, 1.0, request)
        gains.append(stats["accepted_delta_gain"])
        np.testing.assert_allclose(y.mean(axis=1), x.mean(axis=1), atol=1e-14)
        after = float(np.mean(((y[:, 0] - y[:, 1]) / 2)**2))
        assert after <= max(se, me) + 1e-12            # Side ≤ Mid；原本就更宽时不收窄
    assert all(b >= a - 1e-12 for a, b in zip(gains, gains[1:]))        # 请求单调
    assert gains[0] == pytest.approx(10 ** (1/20) - 1, rel=1e-9)        # 1dB 请求全额兑现
    # 旧阶梯（120–2kHz 增长上限 +1.5dB）在此素材上最多兑现 ~+1.6dB
    assert 20 * np.log10(1 + gains[-1]) > 2.9


def test_width_keeps_mono_and_already_wide_audio():
    x = audio()
    mono = np.column_stack((x[:, 0], x[:, 0]))
    wide = np.column_stack((x[:, 0], -x[:, 0]))
    for original in (mono, wide):
        result, report = widen(original, 1, 12)
        np.testing.assert_array_equal(result, original)
        assert not report['applied']


def test_final_guard_uses_no_reference_finalizer_on_rejection(tmp_path, monkeypatch):
    x = audio()
    src = write(tmp_path/'source.wav', x)
    monkeypatch.setattr(pipeline, 'candidate', lambda *a, **k: (x, x*.8, {'reference_bandwidth_hz':20000}))
    monkeypatch.setattr(pipeline, 'violations', lambda *a: ['forced_final_budget'])
    stats = pipeline.master_file(src, tmp_path/'ref.wav', 'soft', reference='provided', eq_profile='Bright')
    base = pipeline.master_file(src, tmp_path/'base.wav', 'soft', eq_profile='Bright')
    assert stats['reference']['reason'] == 'final_output_budget'
    assert stats['reference']['accepted_strength'] == 0
    y, _ = sf.read(tmp_path/'ref.wav'); b, _ = sf.read(tmp_path/'base.wav')
    np.testing.assert_allclose(y, b, atol=4e-7)
    assert stats['target_lufs'] == base['target_lufs']


@needs_matchering
@pytest.mark.parametrize('packaged', [False, True])
def test_actual_reference_subprocess_has_no_soren_dependency(tmp_path, monkeypatch, packaged):
    if packaged:
        runtime = tmp_path / 'runtime'
        repo = Path(__file__).resolve().parents[1]
        shutil.copytree(repo / 'mastering', runtime / 'mastering', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(repo / 'audio_metrics.py', runtime / 'audio_metrics.py')
        monkeypatch.setattr(backend, 'MASTERING_ROOT', runtime)
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: pytest.fail('Soren requested'))
    monkeypatch.setattr(backend, 'PYTHON', Path(sys.executable))
    src = write(tmp_path/'source.wav', audio())
    ref = write(tmp_path/'ref.wav', audio()*.7)
    stats = backend.stage_soren(src, tmp_path/'out.wav', reference=ref, genre='not-a-profile',
                                loudness='soft', match_source=src, style_blend=.5)
    assert stats['reference']['adapter']['version'] == '2.0.6'
    assert stats['loudness_target_source'] == 'application_preset'
    assert stats['target_lufs'] == -12.3


@pytest.mark.parametrize('profile', ['Neutral', 'Warm', 'Bright', 'Fusion'])
def test_user_eq_frequency_response_and_channel_symmetry(profile):
    # Independent steady-state measurements across the audible range.
    gains = []
    for hz in np.geomspace(30, 18000, 24):
        wave = .1 * np.sin(2 * np.pi * hz * np.arange(SR // 2) / SR)
        x = np.column_stack((wave, wave))
        y = apply_eq(x, profile)
        np.testing.assert_array_equal(y[:, 0], y[:, 1])
        gains.append(20 * np.log10(np.linalg.norm(y[4096:, 0]) / np.linalg.norm(wave[4096:])))
        if profile == 'Neutral':
            np.testing.assert_array_equal(x, y)
    assert min(gains) > -.1 and max(gains) < 2
    if profile == 'Warm':
        assert gains[0] > 1.4 and abs(gains[-1]) < .1
    elif profile == 'Bright':
        assert abs(gains[0]) < .1 and gains[-1] > 1.4
    elif profile == 'Fusion':
        assert .7 < gains[0] < .9 and .7 < gains[-1] < .9


@pytest.mark.parametrize('reference_mode', [False, True])
def test_pipeline_splits_repairs_from_final_width(tmp_path, monkeypatch, reference_mode):
    src = write(tmp_path/'source.wav', audio())
    ref = write(tmp_path/'ref.wav', audio()*.7) if reference_mode else None
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: pytest.fail('Soren requested'))
    monkeypatch.setattr(backend, 'stage_demucs', lambda *a, **k: None)
    seen = {}
    def reshape(in_mix, stems_dir, out_wav, **kwargs):
        seen['repair'] = kwargs
        shutil.copyfile(in_mix, out_wav)
        return 1.0
    def master(input_wav, out_wav, **kwargs):
        seen['master'] = kwargs
        shutil.copyfile(input_wav, out_wav)
    monkeypatch.setattr(backend, 'stage_reshape', reshape)
    monkeypatch.setattr(backend, 'stage_soren', master)
    backend.run_pipeline(src, tmp_path/'out', reference=ref,
        style_mode='styled' if ref else 'off', space_wet=.7, space_width_db=5, space_denoise=.3,
        bypass=['lew', 'bass', 'drums', 'vocals'], cache_enabled=False)
    assert seen['repair']['wet'] == 0
    assert seen['repair']['space_amount'] == .7 and seen['repair']['denoise'] == .3
    assert seen['master']['final_space_wet'] == .7 and seen['master']['final_width_db'] == 5
    if reference_mode:
        assert seen['master']['match_source'] == src
        assert seen['master']['upstream_delta_path'] is None


def test_reference_cache_restores_report_and_invalidates_changed_reference(tmp_path, monkeypatch):
    src = write(tmp_path/'source.wav', audio())
    ref = write(tmp_path/'ref.wav', audio()*.7)
    monkeypatch.setattr(backend, '_ensure_dev_runtime', lambda: pytest.fail('Soren requested'))
    calls = []
    def master(input_wav, out_wav, loudness, progress, cancel, **kwargs):
        calls.append(kwargs)
        return pipeline.master_file(input_wav, out_wav, loudness, strength=0, reference=ref)
    monkeypatch.setattr(backend, 'stage_mastering', master)
    opts = dict(reference=ref, style_mode='styled', loudness='soft',
                bypass=['lew', 'bass', 'drums', 'reshape', 'vocals'])
    first = backend.run_pipeline(src, tmp_path/'out', **opts)
    second = backend.run_pipeline(src, tmp_path/'out', **opts)
    assert len(calls) == 1 and Path(first).read_bytes() == Path(second).read_bytes()
    assert backend.read_mastering_stats(second)['reference']['reason'] == 'zero_strength'
    write(ref, audio()*.6)
    backend.run_pipeline(src, tmp_path/'out', **opts)
    assert len(calls) == 2
