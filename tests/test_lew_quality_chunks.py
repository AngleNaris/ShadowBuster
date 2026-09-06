from pathlib import Path
import studio_backend as backend


def test_fast_chunks_use_less_context_and_overlap_than_other_presets():
    fast, overlap = backend.QUALITY_CHUNKS[0]
    assert (fast, overlap) == (6.0, 0.5)
    for quality in (1, 2):
        chunk, other_overlap = backend.QUALITY_CHUNKS[quality]
        assert fast < chunk
        assert fast / (fast - overlap) < chunk / (chunk - other_overlap)
    assert backend.QUALITY_CHUNKS[1] == (15.0, 2.0)
    assert backend.QUALITY_CHUNKS[2] == (10.0, 3.0)


def test_fast_chunk_arguments_reach_lew(monkeypatch, tmp_path):
    calls=[]
    monkeypatch.setattr(backend,'ffmpeg_convert',lambda *a,**k:None)
    monkeypatch.setattr(backend,'_run_stream',lambda cmd,*a,**k:calls.append(cmd))
    backend.stage_lew(tmp_path/'input.wav',tmp_path/'output.wav',quality=0,guidance=2)
    cmd=calls[0]
    assert cmd[cmd.index('--chunk-seconds')+1]=='6.0'
    assert cmd[cmd.index('--overlap-seconds')+1]=='0.5'
    assert cmd[cmd.index('--device')+1]=='cuda'
