"""Shared AI preparation cache. No mastering/DSP settings in AI identities."""
from pathlib import Path
import inspect
import threading
import tempfile
import shutil

import pipeline_cache

_lock = threading.RLock()


def _identity(backend, stage):
    files = [Path(backend.PYTHON)]
    roots = [Path(backend.APOLLO_DIR) / 'ckpts']
    if stage == 'demucs':
        import os
        roots = [Path(os.environ.get('TORCH_HOME', Path.home()/'.cache'/'torch')) / 'hub' / 'checkpoints']
        roots += [Path(os.environ.get('HF_HOME', Path.home()/'.cache'/'huggingface'))/'hub'/'models--adefossez--HTDemucs-6s']
        if backend.ASSETS:
            roots += [Path(backend.ASSETS)/'torch_home', Path(backend.ASSETS)/'hf_home']
    for root in roots:
        if root.exists():
            files.extend(p for p in root.rglob('*') if p.is_file() and p.suffix in ('.bin', '.th', '.pt', '.pth', '.ckpt', '.safetensors'))
    identity = {'schema': 'shared-ai-v2', 'stage': stage, 'runtime': str(backend.PYTHON),
                'weights': [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(files) if p.is_file()]}
    names = ('stage_lew', 'mix_wet_dry', 'ffmpeg_convert') if stage == 'lew' else ('stage_demucs',)
    identity['code'] = []
    for name in names:
        try:
            identity['code'].append(inspect.getsource(getattr(backend, name)))
        except (TypeError, OSError):
            identity['code'].append(name)
    script = Path(backend.APOLLO_DIR)/'lew_upscale.py'
    if stage == 'lew' and script.is_file():
        identity['lew_script'] = pipeline_cache.md5(script)
        identity['chunks'] = backend.QUALITY_CHUNKS
    return identity


def lew(backend, input_wav, out_wav, *, cache_enabled=True, **kwargs):
    if kwargs.get('cancel') and kwargs['cancel']():
        raise backend.PipelineError('用户取消')
    guidance = float(kwargs.pop('guidance', 1.5))
    # Inference is independent of the later dry/wet guidance mix.
    kwargs['guidance'] = 2.0
    params = {k:v for k,v in kwargs.items() if k not in ('progress', 'cancel')}
    with _lock, tempfile.TemporaryDirectory(prefix='sb-ai-lew-') as temp:
        raw = Path(temp)/'reconstructed.wav'
        cache = pipeline_cache.StageCache(cache_enabled, _identity(backend, 'lew'))
        cache.run('prepared_lew', [input_wav], params, [raw],
                   lambda: backend.stage_lew(input_wav, raw, **kwargs))
        wet = min(1.0, max(0.0, guidance/2))
        if wet == 1:
            shutil.copyfile(raw, out_wav)
        else:
            dry = Path(temp)/'original.wav'
            def mix():
                backend.ffmpeg_convert(input_wav, dry)
                backend.mix_wet_dry(dry, raw, out_wav, wet)
            # FLOAT WAV can contain a creation timestamp in its PEAK chunk.
            # Restore the mixed artifact too, keeping downstream content keys stable.
            cache.run('prepared_lew_mix', [input_wav, raw], {'wet': wet}, [out_wav], mix)


def demucs(backend, input_wav, out_dir, *, model='htdemucs', cache_enabled=True, **kwargs):
    if kwargs.get('cancel') and kwargs['cancel']():
        raise backend.PipelineError('用户取消')
    # Cache the flat stems, not an output tree tied to the input filename.
    target = Path(out_dir)/model/Path(input_wav).stem
    params = {'model': model, **{k:v for k,v in kwargs.items() if k not in ('progress', 'cancel')}}
    with _lock:
        cache = pipeline_cache.StageCache(cache_enabled, _identity(backend, 'demucs'))
        cache.run('prepared_demucs', [input_wav], params, [target],
                  lambda: backend.stage_demucs(input_wav, out_dir, model=model, **kwargs))
    return target


def endpoints(backend, source, work, *, quality=1, device='cuda', reconstruct=True,
              cache_enabled=True, progress=None, cancel=None):
    """Immutable dry/wet endpoints. Guidance and guitar controls never enter AI keys."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    original, raw = work/'original.wav', work/'raw.wav'
    backend.ffmpeg_convert(source, original)
    def phase(offset, span):
        return (lambda f, label: progress(offset+span*f, label)) if progress else None
    if reconstruct:
        lew(backend, source, raw, quality=quality, guidance=2.0, device=device,
            cache_enabled=cache_enabled, progress=phase(0, .4), cancel=cancel)
    else:
        raw = original
    dry_stems = demucs(backend, original, work/'dry_stems', model='htdemucs_6s', device=device,
                      cache_enabled=cache_enabled, progress=phase(.4, .3), cancel=cancel)
    wet_stems = demucs(backend, raw, work/'wet_stems', model='htdemucs_6s', device=device,
                      cache_enabled=cache_enabled, progress=phase(.7, .3), cancel=cancel) if reconstruct else dry_stems
    return original, raw, dry_stems, wet_stems


def mix_endpoints(original, raw, dry_stems, wet_stems, output, stems, guidance, cancel=None):
    """One shared peak gain for the mixed signal AND every stem; preserve residuals."""
    import numpy as np
    import soundfile as sf
    wet = max(0., min(1., float(guidance)/2))
    def blocks(a, b):
        with sf.SoundFile(a) as dry, sf.SoundFile(b) as reconstructed:
            if (dry.samplerate, dry.channels, len(dry)) != (reconstructed.samplerate, reconstructed.channels, len(reconstructed)):
                raise ValueError('原曲与重建素材未对齐')
            while dry.tell() < len(dry):
                if cancel and cancel():
                    raise RuntimeError('用户取消')
                yield dry.read(65536, dtype='float64', always_2d=True)*(1-wet) + reconstructed.read(65536, dtype='float64', always_2d=True)*wet
    peak = max((float(np.max(np.abs(x))) for x in blocks(original, raw)), default=0.)
    scale = min(1., .999/max(peak, 1e-12))
    Path(stems).mkdir(parents=True, exist_ok=True)
    pairs = [(original, raw, Path(output))] + [(Path(dry_stems)/(n+'.wav'), Path(wet_stems)/(n+'.wav'), Path(stems)/(n+'.wav'))
             for n in ('bass', 'drums', 'vocals', 'other', 'guitar', 'piano')]
    for a, b, out in pairs:
        info = sf.info(a)
        with sf.SoundFile(out, 'w', samplerate=info.samplerate, channels=info.channels, subtype='FLOAT') as target:
            for x in blocks(a, b):
                target.write(x*scale)
    return scale
