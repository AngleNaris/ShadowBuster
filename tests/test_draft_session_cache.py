"""Disk-backed session identity and activation, independent of audio decoding."""
import json
from pathlib import Path
from main import Bridge


def test_restore_activates_matching_file_and_preserves_other_sessions(tmp_path):
    bridge = Bridge(None)
    params = {'quality': 1, 'reference': '', 'style_mode': 'off', 'bypass': []}
    files = [tmp_path / 'a.wav', tmp_path / 'b.wav']
    sessions = [object(), object()]
    for path, session in zip(files, sessions):
        path.write_bytes(b'audio')
        bridge._draft_sessions[str(path.resolve())] = (
            bridge._draft_cache_key(path, params), session, {'id': 1, 'frames': 44100})
    for request_id, index in enumerate([0, 1, 0, 1], 10):
        result = json.loads(bridge.draftLookup(str(files[index]), params, request_id))
        assert result['restored'] and result['id'] == request_id
        assert bridge._draft_session is sessions[index]
        assert bridge._draft_session_id == request_id
    assert len(bridge._draft_sessions) == 2
    # Real-time gain changes reuse prepared materials; quality changes do not.
    assert bridge.draftLookup(str(files[0]), {**params, 'vocal': 4}, 20) != 'null'
    assert bridge.draftLookup(str(files[0]), {**params, 'quality': 2}, 21) == 'null'
    files[0].write_bytes(b'changed audio')
    assert bridge.draftLookup(str(files[0]), params, 22) == 'null'
    assert bridge.draftLookup(str(files[1]), params, 23) != 'null'


def test_reference_content_and_reconstruction_bypass_invalidate(tmp_path):
    source, reference = tmp_path / 'a.wav', tmp_path / 'ref.wav'
    source.write_bytes(b'audio'); reference.write_bytes(b'reference')
    params = {'quality': 1, 'reference': str(reference), 'bypass': []}
    initial = Bridge._draft_cache_key(source, params)
    assert initial != Bridge._draft_cache_key(source, {**params, 'bypass': ['lew']})
    reference.write_bytes(b'new reference')
    assert initial != Bridge._draft_cache_key(source, params)
