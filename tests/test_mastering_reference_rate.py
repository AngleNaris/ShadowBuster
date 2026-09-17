import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
from soren_resources import SOREN_RESOURCE_DIR
sys.path.insert(0, str(SOREN_RESOURCE_DIR))
spec = importlib.util.spec_from_file_location('reference_rate_core', ROOT/'packaging/soren_core.py')
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


@pytest.mark.parametrize('reference_rate,custom', [(11025, False), (48000, True)])
def test_reference_rate_and_bandwidth_are_preserved(monkeypatch, tmp_path, reference_rate, custom):
    target = np.zeros((2, 44100))
    reference = np.sin(2*np.pi*1000*np.arange(reference_rate)/reference_rate)
    if not custom:
        reference = np.vstack([reference, reference])
    cfg = core.Config()
    cfg.genre = None if custom else 'Pop'
    captured = {}
    monkeypatch.setattr(core, 'load_audio', lambda path, config: (target, 44100) if path == 'input.wav' else (reference, reference_rate))
    monkeypatch.setattr(core, 'load_genre_profile', lambda genre: {'genre': 'Pop', 'lufs': -9})
    monkeypatch.setattr(core, 'create_reference_from_profile', lambda *args: (reference, reference_rate))
    monkeypatch.setattr(core, 'log_audio_metrics', lambda *args: None)

    class Captured(Exception):
        pass

    def process(audio, ref, step, config, profile):
        captured.update(reference=ref, bandwidth=config.reference_bandwidth_hz)
        raise Captured

    monkeypatch.setattr(core, 'process_audio', process)
    with pytest.raises(Captured):
        core.master_audio('input.wav', str(tmp_path/'out.wav'), cfg, 'Neutral')
    ref = captured['reference']
    assert ref.shape == (2, 44100)
    assert captured['bandwidth'] == min(reference_rate, 44100)/2
    f,p = signal.welch(ref[0], 44100, nperseg=44100)
    assert f[np.argmax(p)] == 1000
