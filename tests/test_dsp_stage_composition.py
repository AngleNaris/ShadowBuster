"""Regression probes for the 2026-09-18 DSP composition audit."""
import json
from pathlib import Path
import sys

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apollo_scripts'))
import drum_enhance as drums
import stem_enhance as stems
import soundstage_reshape as reshape
import studio_backend as backend

SR = 44100


def tone(seconds=1, frequency=90):
    t = np.arange(int(seconds * SR)) / SR
    return np.sin(2 * np.pi * frequency * t)


def write(path, data):
    sf.write(path, data, SR, subtype='FLOAT')


def invoke(monkeypatch, module, argv):
    monkeypatch.setattr(sys, 'argv', [module.__name__, *map(str, argv)])
    module.main()


@pytest.mark.parametrize('length', [1, 441, 881, 882])
def test_short_drum_gate_keeps_input_length(length):
    x = np.full(length, .02)
    out = drums.enhance_drum_stem(x, SR, punch_db=2, trans=0)
    assert out.shape == x.shape
    assert np.isfinite(out).all()


def test_drum_cli_links_gate_across_unequal_channels(tmp_path, monkeypatch):
    x = tone()[:, None] * np.array([.02, .004])
    source, out = tmp_path / 'drums.wav', tmp_path / 'out.wav'
    write(source, x)
    invoke(monkeypatch, drums, ['--drums', source, '--in-mix', source,
                               '--out', out, '--punch-db', 6, '--trans', 0])
    y, _ = sf.read(out)
    # Equal processing must preserve the original 5:1 pan ratio, including tails.
    np.testing.assert_allclose(y[:, 0], y[:, 1] * 5, atol=2e-8)
    segment = slice(SR // 2, 3 * SR // 4)
    gain = np.sqrt(np.mean(y[segment] ** 2, axis=0) /
                   np.mean(x[segment] ** 2, axis=0))
    np.testing.assert_allclose(20 * np.log10(gain), [6, 6], atol=.01)


@pytest.mark.parametrize('module,kind', [(drums, 'drums'), (stems, 'guitar'), (stems, 'synth')])
@pytest.mark.parametrize('scale', [.5, .2, 1.0])
def test_gain_after_upstream_trim(module, kind, scale, tmp_path, monkeypatch):
    x = np.column_stack((.1 * tone(frequency=1000),) * 2)
    source, mix, out = [tmp_path / name for name in ('stem.wav', 'mix.wav', 'out.wav')]
    write(source, x)
    write(mix, x * scale)
    if module is drums:
        args = ['--drums', source, '--punch-db', 0, '--trans', 0, '--drums-gain-db', -6]
    else:
        args = ['--stem', source, '--kind', kind, '--gain-db', -6]
    invoke(monkeypatch, module, args + ['--in-mix', mix, '--out', out, '--stem-scale', scale])
    y, _ = sf.read(out)
    np.testing.assert_allclose(y, x * scale * 10 ** (-6 / 20), atol=1e-8)


@pytest.mark.parametrize('scale', [1.0, .5])
def test_unmask_reaches_real_stage_output(tmp_path, monkeypatch, scale):
    t = np.arange(2 * SR) / SR
    envelope = np.where((t > .4) & (t < 1.4), .35, .05)
    pad = envelope * np.sin(2 * np.pi * 300 * t)
    x = np.column_stack((pad, pad * .95)).astype(np.float32).astype(float)
    write(tmp_path / 'other.wav', x)
    write(tmp_path / 'drums.wav', np.zeros_like(x))
    mix, out, report = [tmp_path / name for name in ('mix.wav', 'out.wav', 'report.json')]
    write(mix, x * scale)
    stats = {}
    expected = reshape.spatial_unmask_other(x * scale, SR, 1, report=stats)
    assert stats['applied']
    invoke(monkeypatch, reshape, ['--stems-dir', tmp_path, '--in-mix', mix,
                                 '--out-wav', out, '--space-amount', 1,
                                 '--wet', 0, '--other-denoise-amount', 0,
                                 '--stem-scale', scale, '--report-json', report])
    actual, _ = sf.read(out)
    np.testing.assert_allclose(actual, expected, atol=2e-8)
    assert np.max(abs(actual - x * scale)) > .01
    assert json.loads(report.read_text())['extra']['spatial_unmask']['applied']


def test_pipeline_accumulates_trim_through_cached_stages(tmp_path, monkeypatch):
    from test_six_stem_pipeline import StemFakes, _tone_wav
    StemFakes(monkeypatch, tmp_path)
    seen = []
    def stage(name, scale):
        def render(src, out_wav, stem_scale):
            x, sr = sf.read(src, always_2d=True)
            sf.write(out_wav, x * scale, sr, subtype='FLOAT')
            seen.append((name, stem_scale))
            return scale
        def run(stem_dir, in_mix, out_wav, stem_scale=1.0, **kwargs):
            return render(in_mix, out_wav, stem_scale)
        if name == 'drums':
            def run(stem_dir, rest_wav, out_wav, stem_scale=1.0, **kwargs):
                return render(rest_wav, out_wav, stem_scale)
        elif name == 'reshape':
            def run(in_mix, stems_dir, out_wav, stem_scale=1.0, **kwargs):
                return render(in_mix, out_wav, stem_scale)
        run.__name__ = 'stage_' + name
        return run
    for name, scale in [('bass', .5), ('drums', .8), ('reshape', .9),
                        ('guitar', .7), ('synth', .6)]:
        monkeypatch.setattr(backend, 'stage_' + name, stage(name, scale))
    vocals = []
    def vocal(stem_dir, src, dst, **kwargs):
        import shutil
        vocals.append(kwargs['vocal_scale'])
        shutil.copyfile(src, dst)
    monkeypatch.setattr(backend, 'stage_vocals', vocal)
    source = _tone_wav(tmp_path / 'song.wav')
    for folder in ('first', 'cached'):
        backend.run_pipeline(source, tmp_path / folder, bypass=['lew', 'soren'],
                             demucs_model='htdemucs_6s', balance_mode=None,
                             guitar_gain_db=1, synth_gain_db=1)
    assert [name for name, _ in seen] == ['bass', 'drums', 'reshape', 'guitar', 'synth']
    np.testing.assert_allclose([value for _, value in seen], [1, .5, .4, .36, .252])
    assert vocals[0] == pytest.approx(.1512)
