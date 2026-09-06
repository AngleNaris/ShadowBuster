import sys
import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apollo_scripts"))
spec = importlib.util.spec_from_file_location("auto_vocal", ROOT / "apollo_scripts" / "vocal_adjust.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _mix(vocal_level=0.03, accompaniment_level=0.05, seconds=2.0, sr=44100):
    t = np.arange(round(sr * seconds)) / sr
    vocal = vocal_level * np.sin(2 * np.pi * 440 * t)
    accompaniment = accompaniment_level * np.sin(2 * np.pi * 110 * t)
    return np.column_stack((vocal + accompaniment, vocal + accompaniment)), np.column_stack((vocal, vocal)), sr


def test_zero_offset_runs_fixed_default_balance():
    mix, vocals, sr = _mix()
    out, report = mod.apply_mid_prominence_control(
        mix, vocals, sr, user_gain_db=0.0, balance_target_db=-3.0, return_report=True)
    assert report["mode"] == "auto_fixed_target"
    assert report["bypass"] is False
    assert report["fixed_baseline_db"] == -3.0
    assert report["ratio_valid_window_count"] > 0
    assert np.max(np.abs(out - mix)) > 0


def test_user_offset_is_applied_on_top_of_fixed_target():
    mix, vocals, sr = _mix()
    neutral, _ = mod.apply_mid_prominence_control(
        mix, vocals, sr, user_gain_db=0.0, balance_target_db=-3.0, return_report=True)
    forward, report = mod.apply_mid_prominence_control(
        mix, vocals, sr, user_gain_db=3.0, balance_target_db=-3.0, return_report=True)
    assert report["target_offset_db"] == 3.0
    assert report["fixed_baseline_db"] == -3.0
    assert np.max(np.abs(forward - neutral)) > 1e-5
    assert report["actual_gain_min_db"] >= -9.0
    assert report["actual_gain_max_db"] <= 9.0


def test_instrumental_only_does_not_apply_auto_vocal_gain():
    mix, vocals, sr = _mix(vocal_level=0.0)
    out, report = mod.apply_mid_prominence_control(
        mix, vocals, sr, user_gain_db=3.0, balance_target_db=-3.0, return_report=True)
    np.testing.assert_array_equal(out, mix)
    assert report["active_window_count"] == 0
    assert report["ratio_valid_window_count"] == 0


def test_stable_gain_uses_whole_track_median_and_offsets():
    mix, vocals, sr = _mix(seconds=4)
    vocals[sr:2*sr] *= .3
    mix += vocals * .2
    gains = []
    for offset in (-3, 0, 3):
        _, report = mod.apply_mid_prominence_control(
            mix, vocals, sr, offset, balance_target_db=-3, return_report=True)
        metrics = mod.vocal_activity_metrics(mix, vocals, sr)
        expected = np.clip(-3 + offset - np.median(metrics['ratio_db'][metrics['valid']]), -9, 9)
        assert report['fixed_vocal_mid_gain_db'] == expected
        assert report['actual_gain_min_db'] == report['actual_gain_max_db'] == expected
        assert report['gain_max_step_db'] == 0
        assert not report['gain_is_activity_gated']
        gains.append(expected)
    np.testing.assert_allclose(np.diff(gains), [3, 3], atol=1e-12)


def test_invalid_ratio_acapella_side_and_short_bypass():
    mix, vocals, sr = _mix()
    side = vocals * np.array([1, -1])
    for m, v in ((vocals, vocals), (mix, side), (mix[:20], vocals[:20]),
                 (np.zeros_like(mix), np.zeros_like(vocals))):
        out, report = mod.apply_mid_prominence_control(m, v, sr, 3,
            balance_target_db=-3, return_report=True)
        assert np.isfinite(out).all()
        np.testing.assert_array_equal(out, m)
        assert report['fixed_vocal_mid_gain_db'] == 0


def test_eq_is_residual_mid_only_bounded_and_releases():
    sr = 16000
    t = np.arange(sr*4) / sr
    vm = .03 * np.sin(2*np.pi*1500*t)
    vm[t >= 2] = 0
    am = .08 * np.sin(2*np.pi*1500*t) + .03*np.sin(2*np.pi*3000*t)
    side = .02 * np.sin(2*np.pi*700*t)
    v = np.column_stack((vm, vm))
    a = np.column_stack((am + side, am - side))
    mix = v + a
    out, report = mod.apply_mid_prominence_control(mix, v, sr,
        balance_target_db=-3, return_report=True)
    gain = 10**(report['fixed_vocal_mid_gain_db']/20)
    eq_delta = out - mix - v * (gain-1)
    assert np.max(abs(eq_delta)) > 1e-4
    np.testing.assert_allclose(eq_delta[:, 0], eq_delta[:, 1], atol=1e-15)
    np.testing.assert_allclose(eq_delta[3*sr:], 0, atol=1e-15)
    np.testing.assert_allclose(out[:, 0]-out[:, 1], mix[:, 0]-mix[:, 1], atol=1e-15)
    eq = report['eq']
    assert eq['max_used_linear_budget'] <= 1 - 10**(-1.5/20) + 1e-15
    envelopes = np.array([b['envelope'] for b in eq['bands']])
    assert np.all((envelopes >= 0) & (envelopes <= 1))
    assert np.max(np.diff(envelopes, axis=1)) <= .5 + 1e-12
    assert np.min(np.diff(envelopes, axis=1)) >= -.1 - 1e-12
    assert report['reconstruction_max_error'] < 1e-15
    # Fixed filters themselves obey the summed attenuation budget at every frequency.
    responses = []
    for frequency in (1500, 3000):
        b, acoef = mod.signal.iirpeak(frequency, Q=.8, fs=sr)
        _, h = mod.signal.freqz(b, acoef, worN=8192)
        responses.append(h)
    response = 1 - eq['linear_delta_budget']/2 * sum(responses)
    assert np.min(abs(response)) >= 10**(-1.5/20) - 1e-12

def test_original_reference_fixed_gain_same_mask_and_offset():
    reference, vocals, sr = _mix(seconds=4)
    for accompaniment_factor in (.5, 2):
        current = vocals + (reference-vocals)*accompaniment_factor
        gains = []
        for offset in (-1, 0, 1):
            out, report = mod.apply_mid_prominence_control(current, vocals, sr, offset,
                reference_mix=reference, reference_vocals=vocals,
                balance_mode=mod.REFERENCE_MODE, return_report=True)
            rm = mod.vocal_activity_metrics(reference, vocals, sr)
            cm = mod.vocal_activity_metrics(current, vocals, sr)
            mask = rm['valid'] & cm['valid']
            expected = np.median(rm['ratio_db'][mask])-np.median(cm['ratio_db'][mask])+offset
            np.testing.assert_allclose(report['fixed_vocal_mid_gain_db'], expected, atol=1e-12)
            np.testing.assert_array_equal(report['matched_valid_mask'], mask)
            assert report['gain_max_step_db'] == 0
            assert report['mode'] == mod.REFERENCE_MODE
            assert report['target_source'] == 'original_input_htdemucs'
            assert np.isfinite(out).all()
            gains.append(report['fixed_vocal_mid_gain_db'])
        np.testing.assert_allclose(np.diff(gains), [1, 1], atol=1e-12)
        np.testing.assert_allclose(gains[1], 20*np.log10(accompaniment_factor), atol=1e-10)


def test_original_reference_no_activity_safety():
    mix, v, sr = _mix()
    for rm, rv in ((mix, np.zeros_like(v)), (v, v),
                   (mix, v*np.array([1,-1])), (mix[:20], v[:20])):
        current = mix[:len(rm)]
        out, report = mod.apply_mid_prominence_control(current, v[:len(rm)], sr, 3,
            reference_mix=rm, reference_vocals=rv, balance_mode=mod.REFERENCE_MODE, return_report=True)
        np.testing.assert_array_equal(out, current)
        assert report['fixed_vocal_mid_gain_db'] == 0


def test_original_reference_scale_and_unequal_validity():
    mix, v, sr = _mix(seconds=4)
    rv = v.copy(); rv[:sr] = 0
    reference = mix-v+rv
    scale = .3
    current = (mix + (mix-v)*.6)*scale
    _, report = mod.apply_mid_prominence_control(current, v, sr,
        reference_mix=reference, reference_vocals=rv, vocal_scale=scale,
        balance_mode=mod.REFERENCE_MODE, return_report=True)
    rm = mod.vocal_activity_metrics(reference, rv, sr)
    cm = mod.vocal_activity_metrics(current, v*scale, sr)
    mask = rm['valid'] & cm['valid']
    np.testing.assert_array_equal(report['matched_valid_mask'], mask)
    expected = np.median(rm['ratio_db'][mask])-np.median(cm['ratio_db'][mask])
    assert report['fixed_vocal_mid_gain_db'] == expected
    assert report['reference_scale'] == 1


def test_balance_configuration_has_no_genre_or_width_linkage():
    import inspect
    import studio_backend as backend
    params = inspect.signature(mod.apply_mid_prominence_control).parameters
    assert 'genre' not in params and 'width' not in params
    pipeline = inspect.signature(backend.run_pipeline).parameters
    assert pipeline['balance_target_db'].default is None
    assert pipeline['balance_mode'].default == mod.REFERENCE_MODE


def test_no_band_vocal_energy_does_not_trigger_masking():

    mix, v, sr = _mix()
    # Exact residual-free signal, regardless of vocal gain request, cannot duck.
    metrics = mod.vocal_activity_metrics(v, v, sr)
    delta, eq = mod._masking_eq(np.zeros_like(v), v, sr, metrics)
    np.testing.assert_array_equal(delta, 0)
    assert eq['max_used_linear_budget'] == 0
