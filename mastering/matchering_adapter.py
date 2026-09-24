"""Pinned public Matchering API. FLOAT candidates are never final output."""
from pathlib import Path
import math
import subprocess
import tempfile

import numpy as np
import soundfile as sf
from scipy import signal

MATCHERING_VERSION = "2.0.6"
SR = 44100


class UnsuitableReference(ValueError):
    """A readable reference cannot be used safely; use the no-reference path."""


def load_audio(path, temp_dir, name):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"音频文件不存在: {path}")
    try:
        data, sr = sf.read(path, dtype="float64", always_2d=True)
    except sf.LibsndfileError:
        # Preserve the real sample rate and float precision at the decode boundary.
        decoded = Path(temp_dir) / (name + "_decoded.wav")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-i", str(path),
                        "-c:a", "pcm_f32le", str(decoded)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        data, sr = sf.read(decoded, dtype="float64", always_2d=True)
    if data.shape[1] not in (1, 2) or not len(data) or not np.isfinite(data).all():
        raise UnsuitableReference(f"{name}: requires finite mono/stereo audio")
    original_sr = int(sr)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    if sr != SR:
        divisor = math.gcd(sr, SR)
        data = signal.resample_poly(data, SR // divisor, sr // divisor, axis=0,
                                    window=("kaiser", 8.6))
    return data, original_sr


def candidate(source_path, reference_path, *, temp_parent=None):
    try:
        import matchering as mg
        from matchering.log import ModuleError
    except ImportError as exc:
        raise RuntimeError("用户参考模式需要音频运行时安装 matchering==2.0.6") from exc
    if mg.__version__ != MATCHERING_VERSION:
        raise RuntimeError(f"需要 matchering=={MATCHERING_VERSION}，当前为 {mg.__version__}")
    # Pipeline work-directory cleanup also removes these files if the child is
    # forcibly cancelled before Python can execute TemporaryDirectory.__exit__.
    with tempfile.TemporaryDirectory(prefix="sb-reference-", dir=temp_parent) as folder:
        source, source_sr = load_audio(source_path, folder, "source")
        reference, reference_sr = load_audio(reference_path, folder, "reference")
        for name, data in (("source", source), ("reference", reference)):
            if len(data) <= 4096 or len(data) > SR * 900:
                raise UnsuitableReference(f"{name}: matching supports >4096 samples and <=15 minutes")
            if float(np.sqrt(np.mean(data * data))) < 1e-7:
                raise UnsuitableReference(f"{name}: silent or near-silent audio")
        inp, ref, out = [Path(folder) / name for name in ("source.wav", "reference.wav", "candidate.wav")]
        sf.write(inp, source, SR, subtype="FLOAT")
        sf.write(ref, reference, SR, subtype="FLOAT")
        try:
            mg.process(str(inp), str(ref),
                       [mg.Result(str(out), "FLOAT", use_limiter=False, normalize=False)],
                       config=mg.Config(allow_equality=True))
        except ModuleError as exc:
            raise UnsuitableReference(f"Matchering rejected the reference: {exc}") from exc
        result, result_sr = sf.read(out, dtype="float64", always_2d=True)
        if result_sr != SR or result.shape != source.shape or not np.isfinite(result).all():
            raise UnsuitableReference("Matchering candidate changed length/rate or contains non-finite samples")
        f, p = signal.welch(reference, SR, nperseg=min(8192, len(reference)), axis=0)
        power = p.mean(axis=1)
        supported = f[power > float(np.max(power)) * 1e-6]
        bandwidth = min(reference_sr / 2, SR / 2, float(supported[-1]) if len(supported) else 0)
        return source, result, {
            "version": mg.__version__, "source_sample_rate": source_sr,
            "reference_sample_rate": reference_sr, "reference_bandwidth_hz": bandwidth,
            "candidate_subtype": "FLOAT", "candidate_frames": len(result),
            "candidate_peak": float(np.max(np.abs(result))),
            "use_limiter": False, "normalize": False,
            "phase_policy": "candidate magnitude only; candidate waveform is not mixed",
        }
