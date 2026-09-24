"""Prepare bounded PCM snippets for approximate live audition, never final export."""
import base64
import json
from pathlib import Path
import tempfile
from contextlib import nullcontext

import numpy as np
import soundfile as sf

import prepared_audio
import pipeline_cache


CHUNK_SECONDS = 1  # Keep first-seek WebChannel payloads small; prefetch the next block.


class Session:
    """Disk-backed full-song materials; only a bounded chunk crosses WebChannel."""
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sb-draft-session-')
        self.files = {}
        self.frames = 0

    def chunk(self, index):
        start = int(index) * CHUNK_SECONDS * 44100
        count = min(CHUNK_SECONDS * 44100, self.frames - start)
        if start < 0 or count <= 0:
            raise ValueError('试听位置超出歌曲范围')
        buffers = {}
        for name, path in self.files.items():
            data, rate = sf.read(path, start=start, frames=count, dtype='float32', always_2d=True)
            if rate != 44100 or data.shape != (count, 2) or not np.isfinite(data).all():
                raise ValueError('试听缓存格式或长度不一致，请重新准备')
            buffers[name] = base64.b64encode(data.astype('<f4').tobytes()).decode('ascii')
        return {'index': int(index), 'frames': count, 'buffers': buffers}


def prepare(backend, path, start, end, options, *, progress=None, cancel=None, session=None):
    if options.get('style_mode') == 'styled' and not options.get('reference'):
        raise ValueError('草稿试听支持无风格或用户参考；暂不模拟旧预置风格。')
    info = sf.info(path)
    if session is not None:
        start, end = 0, info.duration
    if not 0 <= start < min(end, info.duration):
        raise ValueError('试听选段无效')
    end = min(end, info.duration) if session is not None else min(end, info.duration, start + 30)
    bypass = set(options.get('bypass', ()))
    context = nullcontext(session.temp.name) if session is not None else tempfile.TemporaryDirectory(prefix='sb-draft-')
    with context as temp:
        work = Path(temp)
        original, mix, dry_stems, stems = prepared_audio.endpoints(backend, Path(path), work,
            device=options['device'], quality=options['quality'], progress=progress, cancel=cancel)
        if cancel and cancel():
            raise backend.PipelineError('用户取消')
        frames = min(round(end*44100)-round(start*44100), sf.info(mix).frames-round(start*44100))
        if frames <= 0:
            raise ValueError('选段超出处理后音频长度')
        def read(audio_path):
            data, rate = sf.read(audio_path, start=round(start*44100), frames=frames,
                                 dtype='float32', always_2d=True)
            if rate != 44100 or data.shape != (frames, 2) or not np.isfinite(data).all():
                raise ValueError('草稿素材长度或格式不一致')
            return data
        files = {'mix': mix, 'original': original}
        for name in ('bass', 'drums', 'vocals', 'other', 'guitar', 'piano'):
            file = stems/(name+'.wav')
            if file.is_file():
                files[name] = file
                files['dry_'+name] = dry_stems/(name+'.wav')
        # Same input+reference => same bounded tone curve, independent of knobs/selection.
        curve = []
        reference = options.get('reference')
        if reference and options.get('style_mode') == 'styled':
            curve_file = work/'curve.json'
            # Keep Matchering/scipy in the audio interpreter, not the desktop shell.
            cmd = [backend.PYTHON, '-m', 'mastering.draft_curve', str(original), str(reference), str(curve_file)]
            def compute_curve():
                backend._run_stream(cmd, backend.MASTERING_ROOT, cancel=cancel)
            code = [backend.MASTERING_ROOT/'mastering'/p for p in ('draft_curve.py', 'guard.py', 'matchering_adapter.py')]
            cache = pipeline_cache.StageCache(True, {'draft_curve': 1, 'python': str(backend.PYTHON),
                                                    'code': [pipeline_cache.md5(p) for p in code]})
            cache.run('draft_reference_curve', [original, reference], {}, [curve_file], compute_curve)
            curve = json.loads(curve_file.read_text(encoding='utf-8'))
        # Calibrate in the audio interpreter; keep scipy out of the desktop shell.
        levels_file = work/'levels.json'
        level_cache = pipeline_cache.StageCache(True, {'draft_levels': 1,
            'code': [pipeline_cache.md5(backend.MASTERING_ROOT/'mastering'/name)
                     for name in ('draft_levels.py', '__init__.py')]})
        def measure_levels():
            backend._run_stream([backend.PYTHON, '-m', 'mastering.draft_levels',
                str(original), str(mix), str(levels_file)], backend.MASTERING_ROOT, cancel=cancel)
        level_cache.run('draft_levels', [original, mix], {}, [levels_file], measure_levels)
        levels = json.loads(levels_file.read_text(encoding='utf-8'))
        # Unity monitoring preserves original loudness; mastering controls its own gain.
        if session is not None:
            session.files, session.frames = files, frames
        else:
            audio = {name: read(file) for name, file in files.items()}
        gain = 1.0
        return {'draft': True, 'path': str(path), 'start': start, 'end': start+frames/44100,
                'sampleRate': 44100, 'frames': frames, 'gain': gain, 'curve': curve, 'endpoints': True, 'levels': levels,
                **({'stream': True, 'chunkFrames': CHUNK_SECONDS*44100} if session is not None else
                   {'buffers': {name:base64.b64encode(x.astype('<f4').tobytes()).decode('ascii') for name,x in audio.items()}}),
                'notices': ['草稿为近似试听；降噪、瞬态、响度和保护以正式导出为准。']}
