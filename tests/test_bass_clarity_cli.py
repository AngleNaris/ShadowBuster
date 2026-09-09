import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import bass_enhance as bass
import studio_backend as backend
import processing_cli


def tone(harmonic=.01):
    t = np.arange(44100) / 44100
    return .2*np.sin(2*np.pi*80*t)+.08*np.sin(2*np.pi*320*t)+harmonic*np.sin(2*np.pi*800*t)


def test_clarity_dull_bright_and_stereo():
    x = tone()
    gains = bass.analyze_auto_clarity(np.column_stack((x,x)),44100)
    assert -2 <= gains[0] <= 0 and 0 < gains[1] <= 2
    assert gains == bass.analyze_auto_clarity(np.column_stack((x,-x)),44100)
    assert bass.analyze_auto_clarity(tone(.15),44100) == (0,0)


@pytest.mark.parametrize('freq',[40,80,120])
def test_pure_sub_unchanged(freq):
    x = .2*np.sin(2*np.pi*freq*np.arange(44100)/44100)
    assert bass.analyze_auto_clarity(x,44100) == (0,0)


def test_silence_noise_and_neutral():
    assert bass.analyze_auto_clarity(np.zeros(44100),44100) == (0,0)
    assert bass.analyze_auto_clarity(np.random.default_rng(2).normal(0,.1,44100),44100) == (0,0)
    x=tone().astype('float32')
    np.testing.assert_array_equal(bass.enhance_bass_stem(x,44100,0,0,0,0),x)


def test_cli_routes_options_and_reports(tmp_path):
    source=tmp_path/'input.wav'; source.write_bytes(b'test')
    report=tmp_path/'result.json'
    with patch.object(backend,'run_batch',return_value=[(str(source),'done.wav',None)]) as run:
        assert processing_cli.main(['-i',str(source),'-o',str(tmp_path/'out'),'--bass-auto-clarity','--punch-db','4','--result-json',str(report)]) == 0
        assert run.call_args.kwargs['bass_auto_clarity'] is True
        assert run.call_args.kwargs['punch_db'] == 4
    assert json.loads(report.read_text())['status']=='success'


def test_cli_refuses_existing_output(tmp_path):
    source=tmp_path/'input.wav'; source.write_bytes(b'test')
    (tmp_path/'input_shadowbuster.wav').write_bytes(b'keep')
    with patch.object(backend,'run_batch') as run:
        assert processing_cli.main(['-i',str(source),'-o',str(tmp_path)]) == 1
        run.assert_not_called()


def test_mastering_sidecars_stay_private_and_routing(tmp_path):
    source=tmp_path/'input.wav'; source.write_bytes(b'audio')
    out=tmp_path/'out'; out.mkdir()
    old=out/'input_shadowbuster.wav.mastering'; old.write_bytes(b'old')
    def copy_stage(_, inp, dest, **kw):
        Path(dest).write_bytes(Path(inp).read_bytes()); return 1.0
    def master(inp,dest,**kw):
        assert Path(dest).parent != out
        Path(dest).write_bytes(b'mastered')
        Path(str(dest)+'.mastering.json').write_bytes(b'{}')
    with patch.object(backend,'stage_demucs'), patch.object(backend,'stage_bass',side_effect=copy_stage) as b, patch.object(backend,'stage_drums',side_effect=copy_stage) as d, patch.object(backend,'stage_soren',side_effect=master):
        result=backend.run_pipeline(source,out,bypass=['lew','reshape','vocals'],bass_auto_clarity=True,punch_db=4,trans=.5)
    assert b.call_args.kwargs['auto_clarity'] is True
    assert b.call_args.kwargs['punch_db']==b.call_args.kwargs['trans']==0
    assert d.call_args.kwargs['punch_db']==4
    assert Path(result).read_bytes()==b'mastered'
    assert old.read_bytes()==b'old'
    assert not list(out.glob('*.json'))
