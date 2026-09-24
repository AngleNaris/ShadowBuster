"""File-picker paths must survive the waveform request/response unchanged."""
import json

import numpy as np
import soundfile as sf

import main


def test_file_picker_path_is_preserved_in_waveform_response(tmp_path, monkeypatch):
    source = tmp_path / '歌曲.wav'
    sf.write(source, np.zeros((4410, 2)), 44100)
    request = source.as_posix()
    bridge = main.Bridge(None)
    responses = []
    bridge.previewPeaks.connect(responses.append)

    class InlineThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(main.threading, 'Thread', InlineThread)
    bridge.previewLoad(request)
    payload = json.loads(responses[0])
    assert payload['path'] == request
    assert payload['duration'] == .1
    assert payload['spec']


def test_stale_waveform_error_is_not_reported():
    bridge = main.Bridge(None)
    errors = []
    bridge.previewFailed.connect(errors.append)
    bridge._wave_generation = 2
    bridge._preview_load_worker('missing-old-song.wav', generation=1)
    assert errors == []
