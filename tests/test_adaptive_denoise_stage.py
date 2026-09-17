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


def test_wrong_stem_length_rejected(tmp_path, monkeypatch):
    stems, source = fixtures(tmp_path)
    sf.write(stems / 'vocals.wav', np.zeros((100, 2)), SR, subtype='FLOAT')
    with pytest.raises(ValueError, match='length mismatch'):
        invoke(shape, monkeypatch, stems, source, tmp_path / 'out.wav',
               '--noise-mode', 'adaptive_all', '--other-denoise-amount', '.5')
