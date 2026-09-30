"""Persistent audio worker (audit §9.9 / P1-8) — opt-in, OFF by default.

A single long-lived process (separate from the Qt GUI, keeping the historical
QtWebEngine + same-process-CUDA isolation) that runs pipeline stages in-process via
``runpy`` so heavy imports (numpy/scipy/torch) load once and persist. Line-delimited
JSON over stdin/stdout: ``run`` jobs stream progress frames, honour cooperative cancel,
and never crash the worker (a stage exception becomes an ``error`` frame).

This is the FOUNDATION: nothing routes through it unless the desktop shell enables it
(env ``SB_WORKER=1``) and a stage's environment matches the worker's. Each stage is
migrated deliberately after in-app validation; until then the shell keeps spawning the
same ``python -m …`` / ``python script.py`` subprocesses it always has.
"""
import io
import json
import os
import queue
import re
import runpy
import sys
import threading
import traceback
import types

_PROGRESS = re.compile(r"(?:LEW|MASTERING)_PROGRESS\s+([\d.]+)")
_TQDM = re.compile(r"(\d{1,3})%\|")


class WorkerCancelled(Exception):
    """Raised when cooperatively-cancelled stage code aborts via ``sb_worker.check()``."""


# Cooperative-cancel handle shared with dispatched code through the fake ``sb_worker``
# module: a stage loop calls ``sb_worker.check()`` at safe boundaries and aborts.
_state = {"cancel": threading.Event()}
_sb_worker = types.ModuleType("sb_worker")
_sb_worker.cancel_requested = lambda: _state["cancel"].is_set()
_sb_worker.Cancelled = WorkerCancelled
def _check():
    if _state["cancel"].is_set():
        raise WorkerCancelled()
_sb_worker.check = _check
sys.modules["sb_worker"] = _sb_worker

_out_lock = threading.Lock()


def _emit(obj):
    with _out_lock:
        sys.__stdout__.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.__stdout__.flush()


def _tail(text, limit=4000):
    return text[-limit:]


class _Forwarder(io.TextIOBase):
    """Captures a job's stdout/stderr, streaming recognised progress lines out."""
    def __init__(self, job_id):
        self.job_id = job_id
        self._buf = ""
        self.text = []

    def write(self, s):
        if not s:
            return 0
        self.text.append(s)
        self._buf += s
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            m = _PROGRESS.search(line) or _TQDM.search(line)
            if m:
                _emit({"id": self.job_id, "type": "progress",
                       "pct": min(100.0, max(0.0, float(m.group(1))))})
        return len(s)

    def flush(self):
        pass


def _run_job(op, job_id):
    kind, target = op.get("kind"), op.get("target")
    argv, cwd = list(op.get("argv", [])), op.get("cwd")
    if kind not in ("module", "script"):
        raise ValueError(f"unsupported kind: {kind!r}")
    if _state["cancel"].is_set():
        raise WorkerCancelled()
    old = (sys.argv, sys.stdout, sys.stderr, os.getcwd())
    fwd = _Forwarder(job_id)
    try:
        if cwd:
            os.chdir(cwd)
        sys.argv = [str(target)] + [str(a) for a in argv]
        sys.stdout = sys.stderr = fwd
        if kind == "module":
            runpy.run_module(target, run_name="__main__")
        else:
            runpy.run_path(str(target), run_name="__main__")
    finally:
        sys.argv, sys.stdout, sys.stderr, restore_cwd = old
        try:
            os.chdir(restore_cwd)
        except OSError:
            pass
    return "".join(fwd.text)


def _reader(q):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            op = json.loads(line)
        except ValueError:
            continue
        name = op.get("op")
        if name == "cancel":
            _state["cancel"].set()          # interrupts only code that polls check()/cancel_requested()
        elif name == "shutdown":
            q.put(op); return
        elif name in ("run", "ping"):
            q.put(op)


def serve():
    q = queue.Queue()
    threading.Thread(target=_reader, args=(q,), daemon=True).start()
    _emit({"type": "ready"})
    while True:
        try:
            op = q.get()
        except (EOFError, KeyboardInterrupt):
            break
        name = op.get("op")
        if name == "shutdown":
            _emit({"type": "bye"})
            return
        if name == "ping":
            _emit({"id": op.get("id"), "type": "pong"})
            continue
        if name != "run":
            continue
        job_id = op.get("id")
        _state["cancel"].clear()
        try:
            text = _run_job(op, job_id)
            _emit({"id": job_id, "type": "done", "text": text})
        except WorkerCancelled:
            _emit({"id": job_id, "type": "cancelled"})
        except BaseException as exc:                 # keep the worker alive across a bad stage
            _emit({"id": job_id, "type": "error",
                   "message": f"{type(exc).__name__}: {exc}",
                   "traceback": _tail(traceback.format_exc())})


if __name__ == "__main__":
    serve()
