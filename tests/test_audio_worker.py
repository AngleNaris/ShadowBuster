"""Headless tests for the opt-in persistent audio worker (§9.9 / P1-8).

These exercise the RPC transport, progress streaming, cooperative cancel, crash
isolation, and the subprocess fallback — all with CPU-only targets, so they run without
torch/GUI/ffmpeg. They do NOT validate CUDA-stage behaviour (that needs the real app).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_cache  # noqa: E402  (imported to keep module graph honest)
import studio_backend as backend


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.delenv('SB_WORKER', raising=False)
    monkeypatch.delenv('SB_WORKER_DISABLE', raising=False)
    yield
    if backend._worker_instance is not None:
        backend._worker_instance.stop()
    backend._worker_instance = None


def _write_script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding='utf-8')
    return path


def test_worker_disabled_uses_subprocess_stream(worker_env, tmp_path, monkeypatch):
    """Default (SB_WORKER unset): stream_or_worker delegates to _run_stream, never spawning."""
    calls = []
    monkeypatch.setattr(backend, '_run_stream',
                        lambda cmd, cwd, env=None, on_progress=None, cancel=None:
                        calls.append(('subprocess', cmd)) or 'SUB')
    script = _write_script(tmp_path, 'noop.py', 'print("MASTERING_PROGRESS 1")\n')
    out = backend.stream_or_worker([backend.PYTHON, str(script)], backend.MASTERING_ROOT)
    assert out == 'SUB' and len(calls) == 1
    assert backend._worker_instance is None          # nothing spawned


def test_worker_env_mismatch_falls_back_to_subprocess(worker_env, monkeypatch):
    monkeypatch.setenv('SB_WORKER', '1')
    calls = []
    monkeypatch.setattr(backend, '_run_stream',
                        lambda *a, **k: calls.append(a) or 'SUB')
    # A torch-home stage's env is not the worker's fixed env → must fall back.
    out = backend.stream_or_worker([backend.PYTHON, '-m', 'demucs', 'x.wav'],
                                   backend.MASTERING_ROOT, env={'TORCH_HOME': '/tmp/th'})
    assert out == 'SUB' and len(calls) == 1


def test_worker_runs_script_and_streams_progress(worker_env, tmp_path, monkeypatch):
    monkeypatch.setenv('SB_WORKER', '1')
    marker = tmp_path / 'ran.txt'
    script = _write_script(tmp_path, 'job.py',
        'import sys\n'
        'print("MASTERING_PROGRESS 10", flush=True)\n'
        'print("MASTERING_PROGRESS 90", flush=True)\n'
        'open(sys.argv[1], "w").write("ok")\n')
    seen = []
    out = backend.stream_or_worker([backend.PYTHON, str(script), str(marker)],
                                   backend.MASTERING_ROOT,
                                   on_progress=lambda f: seen.append(round(f, 3)))
    assert marker.read_text() == 'ok'
    assert 0.1 in seen and 0.9 in seen               # progress frames forwarded to the callback
    assert 'MASTERING_PROGRESS 90' in out            # captured stage output returned to caller


def test_worker_crash_is_isolated_and_survives(worker_env, tmp_path, monkeypatch):
    monkeypatch.setenv('SB_WORKER', '1')
    bad = _write_script(tmp_path, 'bad.py', 'raise ValueError("boom")\n')
    with pytest.raises(backend.PipelineError):
        backend.stream_or_worker([backend.PYTHON, str(bad)], backend.MASTERING_ROOT)
    good = tmp_path / 'g.txt'
    ok = _write_script(tmp_path, 'ok.py', 'import sys; open(sys.argv[1],"w").write("2")\n')
    backend.stream_or_worker([backend.PYTHON, str(ok), str(good)], backend.MASTERING_ROOT)
    assert good.read_text() == '2'                   # worker stayed alive after the failing job


def test_worker_cooperative_cancel(worker_env, tmp_path, monkeypatch):
    monkeypatch.setenv('SB_WORKER', '1')
    script = _write_script(tmp_path, 'spin.py',
        'import time, sb_worker\n'
        'print("MASTERING_PROGRESS 5", flush=True)\n'
        'for _ in range(100000):\n'
        '    sb_worker.check()\n'
        '    time.sleep(0.005)\n')
    progress_seen = {'got': False}
    def on_progress(_):
        progress_seen['got'] = True
    def cancel():
        return progress_seen['got']                  # request cancel once the first frame lands
    with pytest.raises(backend.PipelineError, match='用户取消'):
        backend.stream_or_worker([backend.PYTHON, str(script)], backend.MASTERING_ROOT,
                                 on_progress=on_progress, cancel=cancel)
