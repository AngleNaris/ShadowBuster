"""Prepare bounded PCM snippets for approximate live audition, never final export."""
import base64
import json
from pathlib import Path
import threading
import tempfile
from contextlib import nullcontext

import numpy as np
import soundfile as sf

import prepared_audio
import pipeline_cache


CHUNK_SECONDS = 1  # Keep first-seek WebChannel payloads small; prefetch the next block.

# §9.5 time-to-first-audio: the live player used to pay for FULL-song Lew + 2× demucs
# before the first 1 s chunk could be served. A first audition now prepares a bounded
# window around the requested selection synchronously, and completes the whole song on
# a background thread. Chunks beyond the window simply wait for the background (the
# player only keeps two outstanding requests), so the protocol is unchanged.
CONTEXT_SECONDS = 8.0     # padding on each side of the selection (crossfade / lead-in)
MIN_WINDOW_SECONDS = 16.0  # even a short selection gets a useful audition runway
FULL_SYNC_SECONDS = 45.0   # songs this short are cheaper to prepare whole (no double AI)


def _collect_files(mix, original, wet_stems, dry_stems):
    """Map audition buffers to their on-disk paths (wet stem + its dry counterpart)."""
    files = {'mix': Path(mix), 'original': Path(original)}
    wet, dry = Path(wet_stems), Path(dry_stems)
    for name in ('bass', 'drums', 'vocals', 'other', 'guitar', 'piano'):
        stem = wet / (name + '.wav')
        if stem.is_file():
            files[name] = stem
            files['dry_' + name] = dry / (name + '.wav')
    return files


class Session:
    """Progressive full-song materials; only a bounded chunk crosses WebChannel.

    ``_window`` (offset + frame count) covers the first audition; ``_full`` replaces it
    once the background pass finishes. ``chunk`` blocks on ``_cv`` for regions not yet
    prepared rather than erroring, since the frontend treats a chunk error as fatal.
    """
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sb-draft-session-')
        self.files = {}          # legacy: current-view file map (kept for compatibility)
        self.frames = 0          # legacy total (== total_frames)
        self.total_frames = 0
        self.chunk_frames = CHUNK_SECONDS * 44100
        self.deferred_release = False   # a background thread owns the GPU lock release
        self.background = None
        self._window = None
        self._full = None
        self._error = None
        self._cancel = lambda: False
        self._release = lambda: None
        self._cv = threading.Condition()

    def configure(self, total_frames, cancel, release):
        self.total_frames = self.frames = int(total_frames)
        self._cancel = cancel or (lambda: False)
        self._release = release or (lambda: None)

    def set_window(self, files, offset, length):
        with self._cv:
            self._window = {'files': files, 'offset': int(offset), 'length': int(length)}
            self.files = files

    def set_full(self, files):
        with self._cv:
            self._full = {'files': files}
            self.files = files
            self._cv.notify_all()

    def fail(self, error):
        with self._cv:
            self._error = error
            self._cv.notify_all()

    def release(self):
        self._release()

    def _cover(self, f0, f1):
        """Return (files, offset) that serve [f0, f1), or None if not ready yet.

        A background failure only surfaces for regions the prepared window can't cover;
        the first audition (inside the window) stays playable."""
        if self._full is not None:
            return self._full['files'], 0
        window = self._window
        if window is not None and window['offset'] <= f0 and f1 <= window['offset'] + window['length']:
            return window['files'], window['offset']
        if self._error is not None:
            raise self._error
        return None

    def chunk(self, index):
        f0 = int(index) * self.chunk_frames
        f1 = min(self.total_frames, f0 + self.chunk_frames)
        count = f1 - f0
        if f0 < 0 or count <= 0:
            raise ValueError('试听位置超出歌曲范围')
        with self._cv:
            while True:
                if self._cancel():
                    raise RuntimeError('用户取消')
                ready = self._cover(f0, f1)
                if ready is not None:
                    break
                self._cv.wait()
            files, offset = ready
            buffers = {}
            for name, path in files.items():
                data, rate = sf.read(path, start=f0 - offset, frames=count,
                                     dtype='float32', always_2d=True)
                if rate != 44100 or data.shape != (count, 2) or not np.isfinite(data).all():
                    raise ValueError('试听缓存格式或长度不一致，请重新准备')
                buffers[name] = base64.b64encode(data.astype('<f4').tobytes()).decode('ascii')
        return {'index': int(index), 'frames': count, 'buffers': buffers}


def _read_slice(path, start, end):
    data, rate = sf.read(path, start=start, frames=end - start, dtype='float64', always_2d=True)
    return data, rate


def _payload(path, start, end, frames, curve, levels):
    return {'draft': True, 'path': str(path), 'start': start, 'end': start + frames / 44100,
            'sampleRate': 44100, 'frames': frames, 'gain': 1.0, 'curve': curve,
            'endpoints': True, 'levels': levels,
            'notices': ['草稿为近似试听；降噪、瞬态、响度和保护以正式导出为准。']}


def _bounded_tone_curve(backend, work, original, reference, style_mode, cancel):
    """Reference tone curve for the draft UI, cached by (original, reference) content.

    Runs in the audio interpreter so Matchering/scipy stay out of the desktop shell.
    """
    curve = []
    if not reference or style_mode != 'styled':
        return curve
    curve_file = work / 'curve.json'
    cmd = [backend.PYTHON, '-m', 'mastering.draft_curve', str(original), str(reference), str(curve_file)]
    def compute():
        backend._run_stream(cmd, backend.MASTERING_ROOT, cancel=cancel)
    code = [backend.MASTERING_ROOT / 'mastering' / p
            for p in ('draft_curve.py', 'guard.py', 'matchering_adapter.py')]
    cache = pipeline_cache.StageCache(True, {'draft_curve': 1, 'python': str(backend.PYTHON),
                                             'code': [pipeline_cache.md5(p) for p in code]})
    cache.run('draft_reference_curve', [original, reference], {}, [curve_file], compute)
    return json.loads(curve_file.read_text(encoding='utf-8'))


def _measured_levels(backend, work, original, mix, cancel):
    """Original-vs-mix level calibration, computed in the audio interpreter."""
    levels_file = work / 'levels.json'
    level_cache = pipeline_cache.StageCache(True, {'draft_levels': 1,
        'code': [pipeline_cache.md5(backend.MASTERING_ROOT / 'mastering' / name)
                 for name in ('draft_levels.py', '__init__.py')]})
    def measure():
        backend._run_stream([backend.PYTHON, '-m', 'mastering.draft_levels',
            str(original), str(mix), str(levels_file)], backend.MASTERING_ROOT, cancel=cancel)
    level_cache.run('draft_levels', [original, mix], {}, [levels_file], measure)
    return json.loads(levels_file.read_text(encoding='utf-8'))


def prepare(backend, path, start, end, options, *, progress=None, cancel=None,
            session=None, gpu_release=None):
    if options.get('style_mode') == 'styled' and not options.get('reference'):
        raise ValueError('草稿试听支持无风格或用户参考；暂不模拟旧预置风格。')
    info = sf.info(path)
    duration = info.duration
    if session is not None:
        return _prepare_session(backend, Path(path), start, end, options, info,
                                session, progress=progress, cancel=cancel, gpu_release=gpu_release)
    if not 0 <= start < min(end, duration):
        raise ValueError('试听选段无效')
    end = min(end, duration, start + 30)
    with tempfile.TemporaryDirectory(prefix='sb-draft-') as temp:
        work = Path(temp)
        original, mix, dry_stems, stems = prepared_audio.endpoints(
            backend, Path(path), work, device=options['device'], quality=options['quality'],
            progress=progress, cancel=cancel)
        if cancel and cancel():
            raise backend.PipelineError('用户取消')
        frames = min(round(end * 44100) - round(start * 44100),
                     sf.info(mix).frames - round(start * 44100))
        if frames <= 0:
            raise ValueError('选段超出处理后音频长度')
        def read(audio_path):
            data, rate = sf.read(audio_path, start=round(start * 44100), frames=frames,
                                 dtype='float32', always_2d=True)
            if rate != 44100 or data.shape != (frames, 2) or not np.isfinite(data).all():
                raise ValueError('草稿素材长度或格式不一致')
            return data
        files = _collect_files(mix, original, stems, dry_stems)
        curve = _bounded_tone_curve(backend, work, original, options.get('reference'),
                                    options.get('style_mode'), cancel)
        levels = _measured_levels(backend, work, original, mix, cancel)
        audio = {name: read(file) for name, file in files.items()}
        return {**_payload(path, start, end, frames, curve, levels),
                'buffers': {name: base64.b64encode(x.astype('<f4').tobytes()).decode('ascii')
                            for name, x in audio.items()}}


def _prepare_session(backend, path, start, end, options, info, session, *,
                     progress, cancel, gpu_release):
    duration = info.duration
    total_frames = int(info.frames)
    session.configure(total_frames, cancel, gpu_release)
    work = Path(session.temp.name)

    # Short songs: preparing the whole track is cheaper than a window plus a rebuild,
    # and it keeps the "no extra AI on chunk reads" property exact.
    if total_frames <= FULL_SYNC_SECONDS * 44100:
        original, mix, dry_stems, stems = prepared_audio.endpoints(
            backend, path, work / 'full', device=options['device'], quality=options['quality'],
            progress=progress, cancel=cancel)
        session.set_full(_collect_files(mix, original, stems, dry_stems))
        curve = _bounded_tone_curve(backend, work, original, options.get('reference'),
                                    options.get('style_mode'), cancel)
        levels = _measured_levels(backend, work, original, mix, cancel)
        return {**_payload(path, 0.0, duration, total_frames, curve, levels),
                'stream': True, 'chunkFrames': session.chunk_frames}

    # Long songs: leading window synchronously, full song on a background thread.
    sel_start = max(0.0, min(float(start), duration))
    sel_end = max(sel_start, min(float(end) if end else duration, duration))
    win_lo = max(0.0, sel_start - CONTEXT_SECONDS)
    win_hi = min(duration, sel_end + CONTEXT_SECONDS)
    if win_hi - win_lo < MIN_WINDOW_SECONDS:
        win_hi = min(duration, win_lo + MIN_WINDOW_SECONDS)
        win_lo = max(0.0, win_hi - MIN_WINDOW_SECONDS)
    a, b = round(win_lo * 44100), max(round(win_hi * 44100), round(win_lo * 44100) + 1)
    window_source = work / 'window_source.wav'
    data, rate = _read_slice(path, a, b)
    # PCM_24 (no timestamped WAV PEAK chunk) keeps the slice bytes identical across
    # prepares, so its content-id caches the window AI for a repeat audition. A FLOAT
    # write would embed a creation time and force the small window to re-run every time.
    sf.write(window_source, data, rate, subtype='PCM_24')
    original, mix, dry_stems, stems = prepared_audio.endpoints(
        backend, window_source, work / 'window', device=options['device'],
        quality=options['quality'], progress=progress, cancel=cancel)
    session.set_window(_collect_files(mix, original, stems, dry_stems), a, b - a)
    curve = _bounded_tone_curve(backend, work, original, options.get('reference'),
                                options.get('style_mode'), cancel)
    levels = _measured_levels(backend, work, original, mix, cancel)

    def background():
        try:
            full_original, full_mix, full_dry, full_stems = prepared_audio.endpoints(
                backend, path, work / 'full', device=options['device'],
                quality=options['quality'], cancel=cancel)
            if cancel and cancel():
                session.fail(backend.PipelineError('用户取消'))
                return
            session.set_full(_collect_files(full_mix, full_original, full_stems, full_dry))
        except BaseException as exc:                 # surface to any blocked reader
            session.fail(exc)
        finally:
            session.release()                         # hand the GPU lock back exactly once

    session.background = threading.Thread(target=background, daemon=True)
    session.background.start()
    # Only defer the GPU-lock release once the background thread actually owns it;
    # if start() raised above, the caller's finally still releases exactly once.
    session.deferred_release = True
    return {**_payload(path, 0.0, duration, total_frames, curve, levels),
            'stream': True, 'chunkFrames': session.chunk_frames}
