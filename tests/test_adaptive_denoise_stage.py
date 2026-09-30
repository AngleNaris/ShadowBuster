import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import soundstage_reshape as shape

SR = 44100


def fixtures(tmp_path):
    n = SR * 2
    t = np.arange(n) / SR
    rng = np.random.default_rng(7)
    stems = tmp_path / 'stems'
    stems.mkdir()
    mix = np.zeros((n, 2))
    for name in ('vocals', 'drums', 'bass', 'other', 'guitar', 'piano'):
        x = .004 * rng.normal(size=(n, 2))
        x += (.02 * np.sin(2 * np.pi * 440 * t))[:, None]
        sf.write(stems / f'{name}.wav', x, SR, subtype='FLOAT')
        mix += sf.read(stems / f'{name}.wav', always_2d=True)[0]
    mix += (.005 * np.sin(2 * np.pi * 777 * t))[:, None]
    path = tmp_path / 'input.wav'
    sf.write(path, mix, SR, subtype='FLOAT')
    return stems, path


def invoke(module, monkeypatch, stem_dir, source, output, *extra):
    report = output.with_suffix('.json')
    monkeypatch.setattr(sys, 'argv', ['shape', '--in-mix', str(source), '--out-wav', str(output),
                        '--stems-dir', str(stem_dir), '--wet', '0', '--report-json', str(report), *extra])
    module.main()
    return sf.read(output, always_2d=True)[0], json.loads(report.read_text(encoding='utf-8'))


def test_legacy_default_matches_prechange_code(tmp_path, monkeypatch):
    original = subprocess.check_output(['git', 'show', 'HEAD:apollo_scripts/soundstage_reshape.py'], cwd=ROOT)
    legacy_path = tmp_path / 'legacy_shape.py'
    legacy_path.write_bytes(original)
    spec = importlib.util.spec_from_file_location('legacy_shape', legacy_path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    stems, source = fixtures(tmp_path)
    a, _ = invoke(legacy, monkeypatch, stems, source, tmp_path / 'a.wav', '--other-denoise-amount', '.5')
    b, _ = invoke(shape, monkeypatch, stems, source, tmp_path / 'b.wav', '--other-denoise-amount', '.5')
    np.testing.assert_array_equal(a, b)


def test_adaptive_all_covers_available_six_stems_and_preserves_residual(tmp_path, monkeypatch):
    stems, source = fixtures(tmp_path)
    calls = []

    def clean(x, sr, *args, report):
        calls.append(x.copy())
        report.update(applied=True, reason='test')
        return x * .99

    monkeypatch.setattr(shape, 'adaptive_denoise', clean)
    monkeypatch.setattr(shape, 'constrain_noise_delta', lambda mix, delta, *a: (delta, {'applied': True}))
    y, report = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
                       '--noise-mode', 'adaptive_all', '--other-denoise-amount', '.5')
    expected = sf.read(source, always_2d=True)[0] + sum(x * .99 - x for x in calls)
    np.testing.assert_array_equal(y, expected.astype(np.float32))
    assert len(calls) == 6   # 人声轨参与降噪（嘶声主要落在 vocals 轨），不设豁免
    assert set(report['extra']['noise']['stems']) == {'vocals', 'drums', 'bass', 'other', 'guitar', 'piano'}


def test_adaptive_vocal_stem_gets_halved_cap(tmp_path, monkeypatch):
    """人声轨降噪上限减半（空气感余量）：cap 以参数传给分析器。"""
    stems, source = fixtures(tmp_path)
    caps = []

    def clean(x, sr, amount, low, high, max_attenuation_db, report):
        caps.append(max_attenuation_db)
        report.update(applied=True, reason='test')
        return x * .99

    monkeypatch.setattr(shape, 'adaptive_denoise', clean)
    monkeypatch.setattr(shape, 'constrain_noise_delta', lambda mix, delta, *a: (delta, {'applied': True}))
    invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
           '--noise-mode', 'adaptive_all', '--other-denoise-amount', '.5',
           '--noise-max-attenuation-db', '6')
    assert sorted(set(caps)) == [3.0, 6.0]   # vocals=3.0（减半），其余 6.0


def test_adaptive_missing_optional_stems_reported_and_real_output_safe(tmp_path, monkeypatch):
    stems, source = fixtures(tmp_path)
    for name in ('guitar', 'piano', 'bass'):
        (stems / f'{name}.wav').unlink()
    y, report = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
                      '--noise-mode', 'adaptive_all', '--other-denoise-amount', '.5')
    noise = report['extra']['noise']
    assert noise['stems']['bass']['reason'] == 'missing_stem'
    assert noise['stems']['guitar']['status'] == 'unavailable'
    assert noise['stems']['vocals']['status'] == 'applied'
    assert noise['mix_budget']['band_energy_change_db'] <= 0
    assert np.isfinite(y).all()
    assert np.max(abs(y)) <= np.max(abs(sf.read(source, always_2d=True)[0])) + 1e-8


def test_adaptive_amount_zero_does_not_call_analyzer(tmp_path, monkeypatch):
    stems, source = fixtures(tmp_path)
    def forbidden(*a, **kw):
        pytest.fail('analyzer called for zero amount')
    monkeypatch.setattr(shape, 'adaptive_denoise', forbidden)
    y, report = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav', '--noise-mode', 'adaptive_all')
    np.testing.assert_array_equal(y, sf.read(source, always_2d=True)[0])
    assert not report['extra']['noise']['applied']


def test_adaptive_width_delta_computed_on_denoised_stem(tmp_path, monkeypatch):
    """审计 P0-4：同轨 denoise → width 真串联。12kHz 纯 side 嘶声被降噪削掉后，
    宽度 delta 不得再从 RAW stem 把它放大带回（旧实现两 delta 独立叠加会带回）。"""
    from scipy import signal as sps
    n = SR * 2
    t = np.arange(n) / SR
    stems = tmp_path / 'stems'
    stems.mkdir()
    hiss = (.02 * np.sin(2 * np.pi * 12000 * t))[:, None] * np.array([1., -1.])
    body = (.05 * np.sin(2 * np.pi * 440 * t))[:, None] * np.ones((1, 2))
    mix = np.zeros((n, 2))
    for name in ('vocals', 'drums', 'bass', 'other', 'guitar', 'piano'):
        x = body + (hiss if name == 'other' else 0)
        sf.write(stems / f'{name}.wav', x, SR, subtype='FLOAT')
        mix += x
    source = tmp_path / 'input.wav'
    sf.write(source, mix, SR, subtype='FLOAT')

    def remove_hiss(x, sr, *args, report):
        report.update(applied=True, reason='test')
        sos = sps.butter(4, 8000, btype='lowpass', fs=sr, output='sos')
        return sps.sosfiltfilt(sos, x, axis=0)

    monkeypatch.setattr(shape, 'adaptive_denoise', remove_hiss)
    monkeypatch.setattr(shape, 'constrain_noise_delta',
                        lambda mix_, delta, *a: (delta, {'applied': True}))
    y, _ = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
                  '--wet', '1', '--noise-mode', 'adaptive_all',
                  '--other-denoise-amount', '.5')

    def band_energy(x):
        sos = sps.butter(4, [11000, 13000], btype='bandpass', fs=SR, output='sos')
        return float(np.mean(sps.sosfiltfilt(sos, x, axis=0) ** 2))
    # 输出里 12kHz 嘶声应基本消失；旧独立叠加实现会把它重新放大（> 输入水平）
    assert band_energy(y) < 1e-4 * band_energy(mix)


def test_wet_zero_bypasses_width_dsp_and_stem_io(tmp_path, monkeypatch):
    """审计 P1-1：wet=0 且无 legacy 降噪时，宽度整块旁路——
    不调用任何宽度 DSP，也不加载 drums/other stem，输出与输入位级一致。"""
    stems, source = fixtures(tmp_path)

    def forbidden(*a, **kw):
        pytest.fail('width DSP called with wet=0')

    for fn in ('_reshape_stem', '_dynamic_side_shelf', 'drum_width_envelope',
               '_protect_widen_delta', '_spectral_denoise'):
        monkeypatch.setattr(shape, fn, forbidden)

    reads = []
    orig_read = sf.read

    def tracking_read(path, *a, **kw):
        reads.append(Path(path).name)
        return orig_read(path, *a, **kw)

    monkeypatch.setattr(sf, 'read', tracking_read)
    y, report = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav')
    np.testing.assert_array_equal(y, orig_read(source, always_2d=True)[0])
    assert 'drums.wav' not in reads      # 宽度旁路：drums stem 根本不加载
    assert 'other.wav' not in reads      # legacy 降噪关闭时 other 也不加载
    assert report['extra']['width']['bands'] == []


def test_wet_zero_keeps_legacy_other_denoise(tmp_path, monkeypatch):
    """wet=0 但 legacy other 降噪开启时：宽度仍旁路，降噪照常生效。"""
    stems, source = fixtures(tmp_path)

    def forbidden(*a, **kw):
        pytest.fail('width DSP called with wet=0')

    for fn in ('_reshape_stem', '_dynamic_side_shelf', 'drum_width_envelope',
               '_protect_widen_delta'):
        monkeypatch.setattr(shape, fn, forbidden)

    reads = []
    orig_read = sf.read

    def tracking_read(path, *a, **kw):
        reads.append(Path(path).name)
        return orig_read(path, *a, **kw)

    monkeypatch.setattr(sf, 'read', tracking_read)
    y, report = invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
                       '--other-denoise-amount', '.5')
    assert 'drums.wav' not in reads      # 宽度旁路：drums 不加载
    assert 'other.wav' in reads          # legacy 降噪仍需 other stem
    assert report['extra']['noise']['applied']
    assert report['extra']['width']['bands'] == []
    assert np.isfinite(y).all()


def test_wrong_stem_length_rejected(tmp_path, monkeypatch):
    stems, source = fixtures(tmp_path)
    sf.write(stems / 'vocals.wav', np.zeros((100, 2)), SR, subtype='FLOAT')
    with pytest.raises(ValueError, match='length mismatch'):
        invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
               '--noise-mode', 'adaptive_all', '--other-denoise-amount', '.5')
