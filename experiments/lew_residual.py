"""Isolated conservative residual extraction for Lew upscaling."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

SAMPLE_RATE = 44_100
DEFAULT_PYTHON = Path(r"D:\_3.AI\audio_upscale\UniverSR\.venv\Scripts\python.exe")
DEFAULT_LEW_SCRIPT = Path(r"D:\_3.AI\audio_upscale\Apollo\lew_upscale.py")
DEFAULT_CHECKPOINT = DEFAULT_LEW_SCRIPT.parent / "ckpts" / "lew" / "apollo_model_uni.ckpt"


class LewResidualError(RuntimeError):
    pass


def _finite_number(name: str, value: float, *, minimum=None, maximum=None) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def validate_chunking(chunk_seconds: float, overlap_seconds: float) -> tuple[float, float]:
    chunk = _finite_number("chunk_seconds", chunk_seconds, minimum=1 / SAMPLE_RATE)
    overlap = _finite_number("overlap_seconds", overlap_seconds, minimum=0.0)
    if overlap * 2 > chunk:
        raise ValueError("overlap_seconds must not exceed half chunk_seconds")
    return chunk, overlap


def validate_audio(audio: np.ndarray, name: str = "audio") -> np.ndarray:
    data = np.asarray(audio)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
        raise ValueError(f"{name} must be a nonempty samples-by-channels array")
    if not np.issubdtype(data.dtype, np.number) or not np.isfinite(data).all():
        raise ValueError(f"{name} must contain only finite samples")
    return np.ascontiguousarray(data, dtype=np.float64)


def _require_file(path, name: str) -> Path:
    if path is None or not str(path).strip():
        raise ValueError(f"{name} path is empty")
    result = Path(path).expanduser().resolve()
    if not result.is_file():
        raise FileNotFoundError(f"{name} not found: {result}")
    return result


def read_audio_strict(path) -> tuple[np.ndarray, int]:
    source = _require_file(path, "audio")
    try:
        audio, sample_rate = sf.read(source, dtype="float64", always_2d=True)
    except (RuntimeError, ValueError) as exc:
        raise LewResidualError(f"cannot read audio: {source}") from exc
    return validate_audio(audio), int(sample_rate)


def prepare_audio(source, destination, *, ffmpeg="ffmpeg") -> Path:
    source = _require_file(source, "input")
    if destination is None or not str(destination).strip():
        raise ValueError("destination path is empty")
    destination = Path(destination).expanduser().resolve()
    if destination == source:
        raise ValueError("destination must differ from input")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_channels = None
    try:
        audio, sample_rate = sf.read(source, dtype="float32", always_2d=True)
        validate_audio(audio, "input audio")
        source_channels = audio.shape[1]
    except (RuntimeError, ValueError):
        audio, sample_rate = None, None
    if sample_rate == SAMPLE_RATE:
        sf.write(destination, audio, SAMPLE_RATE, subtype="FLOAT")
        return destination
    executable = shutil.which(str(ffmpeg))
    if executable is None and Path(str(ffmpeg)).is_file():
        executable = str(Path(str(ffmpeg)).resolve())
    if executable is None:
        raise FileNotFoundError(f"ffmpeg not found: {ffmpeg}")
    cmd = [executable, "-y", "-v", "error", "-i", str(source), "-ar", str(SAMPLE_RATE),
           "-c:a", "pcm_f32le", str(destination)]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode:
        raise LewResidualError(f"ffmpeg failed ({completed.returncode}): {completed.stderr[-1000:]}")
    prepared, rate = read_audio_strict(destination)
    if rate != SAMPLE_RATE or (source_channels is not None and prepared.shape[1] != source_channels):
        raise LewResidualError("ffmpeg preparation changed sample rate or channel count")
    return destination


def _probe_cuda(python: Path, timeout: float) -> None:
    try:
        result = subprocess.run(
            [str(python), "-c", "import torch; raise SystemExit(0 if torch.cuda.is_available() else 2)"],
            capture_output=True, text=True, timeout=min(timeout, 120.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise LewResidualError("CUDA availability probe timed out") from exc
    if result.returncode:
        raise LewResidualError("explicit CUDA requested but runtime reports CUDA unavailable")


def run_lew(input_wav, output_wav, *, python=DEFAULT_PYTHON, lew_script=DEFAULT_LEW_SCRIPT,
            checkpoint=DEFAULT_CHECKPOINT, device="cuda", chunk_seconds=15.0,
            overlap_seconds=2.0, chunk_batch_size=1, timeout=7200.0) -> dict:
    input_wav = _require_file(input_wav, "prepared input")
    python = _require_file(python, "Python runtime")
    lew_script = _require_file(lew_script, "Lew script")
    checkpoint = _require_file(checkpoint, "Lew checkpoint")
    if output_wav is None or not str(output_wav).strip():
        raise ValueError("Lew output path is empty")
    output_wav = Path(output_wav).expanduser().resolve()
    if output_wav == input_wav or output_wav in {python, lew_script, checkpoint}:
        raise ValueError("Lew output must not alias an input or runtime artifact")
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    chunk, overlap = validate_chunking(chunk_seconds, overlap_seconds)
    timeout = _finite_number("timeout", timeout, minimum=np.finfo(float).eps)
    chunk_batch_size = int(chunk_batch_size)
    if chunk_batch_size < 1:
        raise ValueError("chunk_batch_size must be positive")
    if device not in {"auto", "cuda", "cpu"}:
        raise ValueError("device must be auto, cuda, or cpu")
    if device == "cuda":
        _probe_cuda(python, timeout)
    cmd = [str(python), str(lew_script), "--in_wav", str(input_wav), "--out_wav", str(output_wav),
           "--checkpoint", str(checkpoint), "--chunk-seconds", str(chunk),
           "--overlap-seconds", str(overlap), "--chunk-batch-size", str(chunk_batch_size),
           "--device", device]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(lew_script.parent)
    started = time.monotonic()
    try:
        completed = subprocess.run(cmd, cwd=lew_script.parent, env=env, capture_output=True,
                                   text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LewResidualError(f"Lew timed out after {timeout:g} seconds") from exc
    elapsed = time.monotonic() - started
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-1500:]
        raise LewResidualError(f"Lew failed ({completed.returncode}): {detail}")
    if not output_wav.is_file():
        raise LewResidualError("Lew did not create its output")
    return {"command": cmd, "seconds": elapsed, "stdout": completed.stdout[-1500:]}


def require_matching_audio(dry: np.ndarray, wet: np.ndarray, dry_rate: int, wet_rate: int) -> None:
    if int(dry_rate) != SAMPLE_RATE or int(wet_rate) != SAMPLE_RATE:
        raise ValueError(f"audio must be exactly {SAMPLE_RATE} Hz")
    if np.asarray(dry).shape != np.asarray(wet).shape:
        raise ValueError(f"wet shape {np.asarray(wet).shape} does not match dry shape {np.asarray(dry).shape}")


def _low_band(audio: np.ndarray, sample_rate: int, cutoff: float = 5_000.0) -> np.ndarray:
    if audio.shape[0] < 16:
        return audio
    cutoff = _finite_number("alignment_cutoff", cutoff, minimum=1.0, maximum=5_500.0)
    sos = signal.butter(4, cutoff / (sample_rate / 2), btype="low", output="sos")
    padlen = min(audio.shape[0] - 1, 3 * (2 * len(sos) + 1))
    return signal.sosfiltfilt(sos, audio, axis=0, padlen=padlen)


def verify_exact_alignment(dry: np.ndarray, wet: np.ndarray, *, sample_rate: int = SAMPLE_RATE,
                           max_lag: int = 64, windows: int = 7, min_correlation: float = 0.90,
                           lowpass_hz: float = 5_000.0, active_rms: float = 1e-6) -> dict:
    dry = validate_audio(dry, "dry")
    wet = validate_audio(wet, "wet")
    require_matching_audio(dry, wet, sample_rate, sample_rate)
    max_lag, windows = int(max_lag), int(windows)
    min_correlation = _finite_number("min_correlation", min_correlation, minimum=0.0, maximum=1.0)
    active_rms = _finite_number("alignment_active_rms", active_rms, minimum=np.finfo(float).eps)
    if max_lag < 1 or windows < 1:
        raise ValueError("max_lag and windows must be positive")
    if np.array_equal(dry, wet):
        channels = [{"channel": channel, "passed": True, "windows": [
            {"window": "identity", "start": 0, "samples": len(dry),
             "dry_rms": _rms(dry[:, channel]), "peak_lag": 0,
             "correlation": 1.0, "passed": True}
        ]} for channel in range(dry.shape[1])]
        return {"passed": True, "identity": True, "max_lag": max_lag,
                "min_correlation": min_correlation, "lowpass_hz": float(lowpass_hz),
                "accepted_windows": len(channels), "channels": channels}
    low_dry = _low_band(dry, sample_rate, lowpass_hz)
    low_wet = _low_band(wet, sample_rate, lowpass_hz)
    n = len(dry)
    width = min(n - 2 * max_lag, max(1024, n // windows))
    if width < 32:
        raise LewResidualError("audio is too short for strict alignment verification")
    starts = np.linspace(max_lag, n - max_lag - width, windows, dtype=int)
    channels, accepted = [], 0
    for channel in range(dry.shape[1]):
        checks = []
        for window_index, start in enumerate(np.unique(starts)):
            stop = start + width
            reference = low_dry[start:stop, channel]
            reference = reference - np.mean(reference)
            dry_rms = float(np.sqrt(np.mean(reference * reference)))
            if dry_rms < active_rms:
                continue
            correlations = []
            for lag in range(-max_lag, max_lag + 1):
                candidate = low_wet[start + lag:stop + lag, channel]
                candidate = candidate - np.mean(candidate)
                denominator = np.linalg.norm(reference) * np.linalg.norm(candidate)
                correlations.append(float(np.dot(reference, candidate) / denominator)
                                    if denominator > 1e-15 else float("nan"))
            finite = np.isfinite(correlations)
            if not finite.any():
                continue
            scores = np.where(finite, correlations, -np.inf)
            peak_index = int(np.argmax(scores))
            peak_lag = peak_index - max_lag
            peak_correlation = correlations[peak_index]
            passed = peak_lag == 0 and peak_correlation >= min_correlation
            checks.append({"window": int(window_index), "start": int(start), "samples": int(width),
                           "dry_rms": dry_rms, "peak_lag": peak_lag,
                           "correlation": peak_correlation, "passed": passed})
            accepted += 1
        channels.append({"channel": channel, "windows": checks,
                         "passed": bool(checks) and all(item["passed"] for item in checks)})
    passed = accepted > 0 and all(item["passed"] for item in channels)
    result = {"passed": passed, "identity": False, "max_lag": max_lag,
              "min_correlation": min_correlation, "lowpass_hz": float(lowpass_hz),
              "accepted_windows": accepted, "channels": channels}
    if not passed:
        raise LewResidualError(f"wet failed exact zero-lag alignment verification: {json.dumps(result)}")
    return result


def stft_channels(audio: np.ndarray, *, n_fft: int = 4096, hop: int = 1024) -> np.ndarray:
    audio = validate_audio(audio)
    n_fft, hop = int(n_fft), int(hop)
    if n_fft < 2 or hop < 1 or hop > n_fft:
        raise ValueError("require n_fft >= 2 and 1 <= hop <= n_fft")
    pad = n_fft // 2
    padded = np.pad(audio, ((pad, pad), (0, 0)))
    frames = 1 + math.ceil(max(0, padded.shape[0] - n_fft) / hop)
    total = (frames - 1) * hop + n_fft
    padded = np.pad(padded, ((0, total - padded.shape[0]), (0, 0)))
    window = signal.windows.hann(n_fft, sym=False)
    view = np.lib.stride_tricks.sliding_window_view(padded, n_fft, axis=0)[::hop]
    return np.fft.rfft(view * window[None, None, :], axis=-1).transpose(2, 0, 1)


def istft_channels(spectrum: np.ndarray, length: int, *, n_fft: int = 4096, hop: int = 1024) -> np.ndarray:
    spectrum = np.asarray(spectrum)
    length, n_fft, hop = int(length), int(n_fft), int(hop)
    if spectrum.ndim != 3 or spectrum.shape[0] != n_fft // 2 + 1 or length < 1:
        raise ValueError("invalid channel spectrum or output length")
    frames = np.fft.irfft(spectrum.transpose(1, 2, 0), n=n_fft, axis=-1)
    window = signal.windows.hann(n_fft, sym=False)
    total = (frames.shape[0] - 1) * hop + n_fft
    output = np.zeros((total, frames.shape[1]), dtype=np.float64)
    weight = np.zeros(total, dtype=np.float64)
    for index, frame in enumerate(frames):
        start = index * hop
        output[start:start + n_fft] += frame.T * window[:, None]
        weight[start:start + n_fft] += window * window
    output /= np.maximum(weight[:, None], 1e-12)
    pad = n_fft // 2
    return output[pad:pad + length]


def frequency_mask(n_fft: int = 4096, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    frequencies = np.fft.rfftfreq(int(n_fft), 1 / int(sample_rate))
    nyquist = sample_rate / 2
    anchors_f = np.array([0.0, 6000.0, 8000.0, 16000.0, 20000.0, nyquist])
    anchors_v = np.array([0.0, 0.0, 1.0, 1.0, 0.2, 0.0])
    if nyquist < 20000:
        anchors_f = np.array([0.0, 6000.0, 8000.0, 16000.0, nyquist])
        anchors_v = np.array([0.0, 0.0, 1.0, 1.0, 0.0])
    mask = np.interp(frequencies, anchors_f, anchors_v)
    transitions = ((6000.0, 8000.0, 0.0, 1.0), (16000.0, 20000.0, 1.0, 0.2),
                   (20000.0, nyquist, 0.2, 0.0))
    for low, high, left, right in transitions:
        if high <= low:
            continue
        selected = (frequencies >= low) & (frequencies <= high)
        x = (frequencies[selected] - low) / (high - low)
        smooth = x * x * (3 - 2 * x)
        mask[selected] = left + (right - left) * smooth
    return mask


def activity_gate(dry_spectrum: np.ndarray, frequencies: np.ndarray, floor_db: float = -60.0) -> np.ndarray:
    spectrum = np.asarray(dry_spectrum)
    band = (frequencies >= 2000) & (frequencies <= 12000)
    if not band.any():
        raise ValueError("STFT has no 2-12 kHz bins")
    activity = np.sqrt(np.mean(np.abs(spectrum[band]) ** 2, axis=0))
    reference = np.max(activity, axis=0, keepdims=True)
    threshold = np.maximum(reference * (10 ** (_finite_number("activity_floor_db", floor_db) / 20)), 1e-12)
    return np.clip(activity / (threshold * 4), 0.0, 1.0)


def soft_support_mask(residual_spectrum: np.ndarray, dry_spectrum: np.ndarray,
                      ratio_limit: float = 0.5, floor_db: float = -70.0) -> np.ndarray:
    ratio_limit = _finite_number("ratio_limit", ratio_limit, minimum=np.finfo(float).eps)
    residual_mag = np.abs(residual_spectrum)
    dry_mag = np.abs(dry_spectrum)
    floor = max(float(np.max(dry_mag)), 1e-12) * 10 ** (_finite_number("support_floor_db", floor_db) / 20)
    support = residual_mag / (residual_mag + floor)
    ratio = ratio_limit * dry_mag / (residual_mag + 1e-12)
    return support * np.clip(ratio, 0.0, 1.0)


def consistency_mask(residual_spectra: list[np.ndarray], floor: float = 1e-12) -> np.ndarray:
    if not residual_spectra:
        raise ValueError("at least one residual spectrum is required")
    shapes = {np.asarray(item).shape for item in residual_spectra}
    if len(shapes) != 1:
        raise ValueError("consistency spectra must have matching shapes")
    if len(residual_spectra) == 1:
        return np.ones(next(iter(shapes)), dtype=np.float64)
    unit = [item / np.maximum(np.abs(item), floor) for item in residual_spectra]
    coherence = np.abs(np.mean(unit, axis=0))
    magnitudes = np.stack([np.abs(item) for item in residual_spectra])
    agreement = np.min(magnitudes, axis=0) / np.maximum(np.max(magnitudes, axis=0), floor)
    return np.clip(coherence * agreement, 0.0, 1.0)


def constructive_residual_gate(dry_spectrum: np.ndarray, candidate_residual: np.ndarray, *,
                               dry_floor_db: float = -120.0) -> tuple[np.ndarray, dict]:
    """Reject destructive residual coefficients without changing dry or residual phase."""
    dry = np.asarray(dry_spectrum)
    candidate = np.asarray(candidate_residual)
    if dry.shape != candidate.shape or dry.ndim < 1 or dry.size == 0:
        raise ValueError("dry and candidate spectra must have matching nonempty shapes")
    if not np.iscomplexobj(dry) or not np.iscomplexobj(candidate):
        raise ValueError("dry and candidate spectra must be complex")
    if not np.isfinite(dry).all() or not np.isfinite(candidate).all():
        raise ValueError("dry and candidate spectra must contain only finite coefficients")
    dry_floor_db = _finite_number("constructive_gate_dry_floor_db", dry_floor_db, maximum=0.0)

    dry_magnitude = np.abs(dry)
    reference = float(np.max(dry_magnitude)) if dry.size else 0.0
    dry_floor = max(reference * 10 ** (dry_floor_db / 20), np.finfo(np.float64).tiny)
    meaningful = dry_magnitude > dry_floor
    candidate_magnitude = np.abs(candidate)
    candidate_active = candidate_magnitude > 0.0
    cross_term = np.real(np.conj(dry) * candidate)
    cross_tolerance = 8 * np.finfo(np.float64).eps * dry_magnitude * candidate_magnitude
    destructive = meaningful & candidate_active & (cross_term < -cross_tolerance)

    accepted = candidate.copy()
    accepted[destructive] = 0.0
    # Guard the exact energy invariant against exceptional roundoff while retaining phase.
    energy_tolerance = 16 * np.finfo(np.float64).eps
    reduced = meaningful & (np.abs(dry + accepted) < dry_magnitude * (1.0 - energy_tolerance))
    accepted[reduced] = 0.0
    destructive |= reduced
    candidate_bins = int(np.count_nonzero(candidate_active))
    rejected_bins = int(np.count_nonzero(destructive))
    accepted_bins = candidate_bins - rejected_bins

    if meaningful.any():
        dry_energy = np.square(dry_magnitude[meaningful])
        mixed_energy = np.square(np.abs((dry + accepted)[meaningful]))
        minimum_energy_ratio = float(np.min(mixed_energy / dry_energy))
    else:
        minimum_energy_ratio = 1.0
    stats = {
        "enabled": True,
        "dry_floor_db": dry_floor_db,
        "dry_floor": dry_floor,
        "meaningful_bins": int(np.count_nonzero(meaningful)),
        "candidate_bins": candidate_bins,
        "accepted_bins": accepted_bins,
        "rejected_destructive_bins": rejected_bins,
        "accepted_fraction": float(accepted_bins / candidate_bins) if candidate_bins else 1.0,
        "rejected_destructive_fraction": float(rejected_bins / candidate_bins) if candidate_bins else 0.0,
        "minimum_analysis_bin_energy_ratio": minimum_energy_ratio,
    }
    return accepted, stats


def extract_added(dry: np.ndarray, wet: np.ndarray, *, extra_wets=(), strength: float = 0.5,
                  n_fft: int = 4096, hop: int = 1024, max_lag: int = 64,
                  min_correlation: float = 0.90, alignment_windows: int = 7,
                  alignment_lowpass_hz: float = 5_000.0,
                  activity_floor_db: float = -60.0, ratio_limit: float = 0.5,
                  support_floor_db: float = -70.0,
                  constructive_gate_dry_floor_db: float = -120.0):
    dry = validate_audio(dry, "dry")
    wet = validate_audio(wet, "wet")
    require_matching_audio(dry, wet, SAMPLE_RATE, SAMPLE_RATE)
    strength = _finite_number("strength", strength, minimum=0.0, maximum=1.0)
    verification = verify_exact_alignment(
        dry, wet, max_lag=max_lag, windows=alignment_windows,
        min_correlation=min_correlation, lowpass_hz=alignment_lowpass_hz,
    )
    extras, extra_verifications = [], []
    for index, extra in enumerate(extra_wets):
        extra = validate_audio(extra, f"extra wet {index}")
        require_matching_audio(dry, extra, SAMPLE_RATE, SAMPLE_RATE)
        extra_verifications.append(verify_exact_alignment(
            dry, extra, max_lag=max_lag, windows=alignment_windows,
            min_correlation=min_correlation, lowpass_hz=alignment_lowpass_hz,
        ))
        extras.append(extra)
    dry_spec = stft_channels(dry, n_fft=n_fft, hop=hop)
    residual_specs = [stft_channels(wet - dry, n_fft=n_fft, hop=hop)]
    residual_specs += [stft_channels(item - dry, n_fft=n_fft, hop=hop) for item in extras]
    frequencies = np.fft.rfftfreq(n_fft, 1 / SAMPLE_RATE)
    mask = frequency_mask(n_fft)[:, None, None] * activity_gate(
        dry_spec, frequencies, activity_floor_db
    )[None, :, :]
    mask = mask * soft_support_mask(residual_specs[0], dry_spec, ratio_limit, support_floor_db)
    if extras:
        mask = mask * consistency_mask(residual_specs)
    candidate = residual_specs[0] * mask * strength
    accepted, constructive_gate = constructive_residual_gate(
        dry_spec, candidate, dry_floor_db=constructive_gate_dry_floor_db,
    )
    added = istft_channels(accepted, len(dry), n_fft=n_fft, hop=hop)
    return added, {"passed": True, "primary": verification, "extras": extra_verifications,
                   "constructive_gate": constructive_gate}


def mix_without_normalization(dry: np.ndarray, added: np.ndarray, *, allow_over: bool = False):
    dry = validate_audio(dry, "dry")
    added = validate_audio(added, "added")
    if dry.shape != added.shape:
        raise ValueError("dry and added shapes must match")
    mixed = dry + added
    peak = float(np.max(np.abs(mixed)))
    clipped = int(np.count_nonzero(np.abs(mixed) > 1.0))
    if clipped and not allow_over:
        raise LewResidualError(f"mix exceeds full scale: peak={peak:.8g}, samples={clipped}")
    return mixed, {"peak": peak, "over_samples": clipped, "would_clip": bool(clipped)}


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def _relative_db(numerator: float, denominator: float) -> float:
    if numerator <= 1e-15:
        return -300.0
    return max(-300.0, 20 * math.log10(numerator / max(denominator, 1e-15)))


def conservation_metrics(dry: np.ndarray, added: np.ndarray, mixed: np.ndarray, *,
                         lowband_max_db: float = -35.0, silent_max_db: float = -40.0,
                         silence_db: float = -60.0, frame_samples: int = 4096) -> dict:
    dry, added, mixed = (validate_audio(item, name) for item, name in
                         ((dry, "dry"), (added, "added"), (mixed, "mix")))
    if dry.shape != added.shape or dry.shape != mixed.shape:
        raise ValueError("conservation metric shapes must match")
    lowband_max_db = _finite_number("lowband_max_db", lowband_max_db)
    silent_max_db = _finite_number("silent_max_db", silent_max_db)
    silence_db = _finite_number("silence_db", silence_db, maximum=0.0)
    identity_error = float(np.max(np.abs(mixed - (dry + added))))
    finite = bool(np.isfinite(dry).all() and np.isfinite(added).all() and np.isfinite(mixed).all())
    low_dry, low_added = _low_band(dry, SAMPLE_RATE, 4_000.0), _low_band(added, SAMPLE_RATE, 4_000.0)
    low_ratio_db = _relative_db(_rms(low_added), _rms(low_dry))
    frame_samples = int(frame_samples)
    if frame_samples < 1:
        raise ValueError("frame_samples must be positive")
    frame_count = len(dry) // frame_samples
    silent_rms = 0.0
    silent_relative_db = -300.0
    silent_frames = 0
    if frame_count:
        dry_frames = dry[:frame_count * frame_samples].reshape(frame_count, frame_samples, -1)
        added_frames = added[:frame_count * frame_samples].reshape(frame_count, frame_samples, -1)
        dry_levels = np.sqrt(np.mean(dry_frames * dry_frames, axis=(1, 2)))
        reference = max(float(np.max(dry_levels)), 1e-15)
        silent = dry_levels <= reference * 10 ** (silence_db / 20)
        silent_frames = int(np.count_nonzero(silent))
        if silent_frames:
            silent_rms = _rms(added_frames[silent])
            active_added_rms = _rms(added_frames[~silent]) if (~silent).any() else 0.0
            silent_relative_db = _relative_db(silent_rms, max(active_added_rms, reference))
    checks = {"finite": finite, "mix_identity": identity_error <= 1e-12,
              "lowband": low_ratio_db <= lowband_max_db,
              "dry_silence": silent_frames == 0 or silent_relative_db <= silent_max_db}
    result = {"passed": all(checks.values()), "checks": checks,
              "mix_identity_max_abs": identity_error, "lowband_added_relative_db": low_ratio_db,
              "lowband_max_db": lowband_max_db, "dry_silent_frames": silent_frames,
              "dry_silent_added_rms": silent_rms,
              "dry_silent_added_relative_db": silent_relative_db,
              "silent_max_db": silent_max_db, "silence_definition_db": silence_db}
    if not result["passed"]:
        raise LewResidualError(f"conservation gate failed: {json.dumps(result)}")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_info(root: Path) -> dict:
    def query(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    return {"commit": query("rev-parse", "HEAD"), "branch": query("branch", "--show-current"),
            "dirty": bool(query("status", "--porcelain"))}


def _artifact(path: Path) -> dict:
    return {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size}


def run_experiment(input_path, output_dir, *, extra_wet_paths=(), strength=0.5, device="cuda",
                   chunk_seconds=15.0, overlap_seconds=2.0, chunk_batch_size=1, timeout=7200.0,
                   allow_over=False, overwrite=False, python=DEFAULT_PYTHON,
                   lew_script=DEFAULT_LEW_SCRIPT, checkpoint=DEFAULT_CHECKPOINT, ffmpeg="ffmpeg",
                   n_fft=4096, hop=1024, max_lag=64, min_correlation=0.90,
                   alignment_windows=7, alignment_lowpass_hz=5_000.0, ratio_limit=0.5,
                   activity_floor_db=-60.0, support_floor_db=-70.0,
                   constructive_gate_dry_floor_db=-120.0,
                   lowband_max_db=-35.0, silent_max_db=-40.0, silence_db=-60.0) -> dict:
    started_wall = time.time()
    started = time.monotonic()
    source = _require_file(input_path, "input")
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir path is empty")
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    strength = _finite_number("strength", strength, minimum=0.0, maximum=1.0)
    validate_chunking(chunk_seconds, overlap_seconds)
    prepared_path = output_dir / "dry_prepared.wav"
    wet_path = output_dir / "lew_wet.wav"
    residual_path = output_dir / "residual_raw.wav"
    added_path = output_dir / "residual_added.wav"
    mix_path = output_dir / "mix.wav"
    prepare_audio(source, prepared_path, ffmpeg=ffmpeg)
    dry, dry_rate = read_audio_strict(prepared_path)
    lew_runtime = run_lew(prepared_path, wet_path, python=python, lew_script=lew_script,
                          checkpoint=checkpoint, device=device, chunk_seconds=chunk_seconds,
                          overlap_seconds=overlap_seconds, chunk_batch_size=chunk_batch_size,
                          timeout=timeout)
    wet, wet_rate = read_audio_strict(wet_path)
    require_matching_audio(dry, wet, dry_rate, wet_rate)
    extras = []
    for item in extra_wet_paths:
        audio, rate = read_audio_strict(item)
        require_matching_audio(dry, audio, dry_rate, rate)
        extras.append(audio)
    added, alignment = extract_added(
        dry, wet, extra_wets=extras, strength=strength, n_fft=n_fft, hop=hop, max_lag=max_lag,
        min_correlation=min_correlation, alignment_windows=alignment_windows,
        alignment_lowpass_hz=alignment_lowpass_hz, activity_floor_db=activity_floor_db,
        ratio_limit=ratio_limit, support_floor_db=support_floor_db,
        constructive_gate_dry_floor_db=constructive_gate_dry_floor_db,
    )
    residual = wet - dry
    mixed, clipping = mix_without_normalization(dry, added, allow_over=allow_over)
    conservation = conservation_metrics(
        dry, added, mixed, lowband_max_db=lowband_max_db, silent_max_db=silent_max_db,
        silence_db=silence_db,
    )
    sf.write(residual_path, residual.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")
    sf.write(added_path, added.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")
    sf.write(mix_path, mixed.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")
    params = {"strength": strength, "device": device, "chunk_seconds": float(chunk_seconds),
              "overlap_seconds": float(overlap_seconds), "chunk_batch_size": int(chunk_batch_size),
              "timeout": float(timeout), "allow_over": bool(allow_over), "n_fft": int(n_fft),
              "hop": int(hop), "max_lag": int(max_lag),
              "min_correlation": float(min_correlation), "alignment_windows": int(alignment_windows),
              "alignment_lowpass_hz": float(alignment_lowpass_hz),
              "ratio_limit": float(ratio_limit), "activity_floor_db": float(activity_floor_db),
              "support_floor_db": float(support_floor_db),
              "constructive_gate": True,
              "constructive_gate_dry_floor_db": float(constructive_gate_dry_floor_db),
              "lowband_max_db": float(lowband_max_db),
              "silent_max_db": float(silent_max_db), "silence_db": float(silence_db)}
    artifacts = [_artifact(path) for path in
                 (prepared_path, wet_path, residual_path, added_path, mix_path)]
    checkpoint = _require_file(checkpoint, "Lew checkpoint")
    lew_script = _require_file(lew_script, "Lew script")
    provenance = {"checkpoint": _artifact(checkpoint), "lew_script": _artifact(lew_script),
                  "apollo_git": _git_info(lew_script.parent)}
    report = {"input": _artifact(source), "sample_rate": SAMPLE_RATE,
              "shape": list(dry.shape), "params": params, "alignment": alignment,
              "constructive_gate": alignment["constructive_gate"],
              "conservation": conservation, "clipping": clipping,
              "git": _git_info(Path(__file__).resolve().parents[1]), "provenance": provenance,
              "runtime": {"started_unix": started_wall, "total_seconds": time.monotonic() - started,
                          "lew_seconds": lew_runtime["seconds"], "python": str(Path(python).resolve()),
                          "lew_script": str(lew_script), "checkpoint": str(checkpoint)},
              "artifacts": artifacts}
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report"] = _artifact(report_path)
    return report
