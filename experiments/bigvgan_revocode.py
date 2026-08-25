"""Isolated, pinned BigVGAN-v2 RevoCode adapter.

The numerical frontend and generator orchestration are dependency-injected so this
module can be tested without importing or downloading the BigVGAN runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

MODEL_ID = "nvidia/bigvgan_v2_44khz_128band_512x"
MODEL_REVISION = "95a9d1dcb12906c03edd938d77b9333d6ded7dfb"
GENERATOR_FILENAME = "bigvgan_generator.pt"
GENERATOR_SHA256 = "d9fe7ec6bd0b44ed9d66973d5012d8181c1570b01e5c72df51973e241dccd357"
SAMPLE_RATE = 44_100
NUM_MELS = 128
N_FFT = 2_048
WIN_LENGTH = 2_048
HOP_LENGTH = 512
FMIN = 0
FMAX = None
REFLECT_PAD = (N_FFT - HOP_LENGTH) // 2
MAGNITUDE_EPSILON = 1e-9
LOG_CLAMP = 1e-5

# Explicit aliases make the two identities difficult to accidentally conflate.
EXPECTED_MODEL_ID = MODEL_ID
EXPECTED_MODEL_REVISION = MODEL_REVISION
EXPECTED_GENERATOR_SHA256 = GENERATOR_SHA256
EXPECTED_CONFIG = {
    "sampling_rate": SAMPLE_RATE,
    "num_mels": NUM_MELS,
    "n_fft": N_FFT,
    "win_size": WIN_LENGTH,
    "hop_size": HOP_LENGTH,
    "fmin": FMIN,
    "fmax": FMAX,
}


class BigVGANRevoCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MelChunk:
    index: int
    core_start: int
    core_stop: int
    expanded_start: int
    expanded_stop: int

    @property
    def core_frames(self) -> int:
        return self.core_stop - self.core_start


_CONFIG_ALIASES = {
    "sampling_rate": ("sampling_rate", "sample_rate"),
    "num_mels": ("num_mels", "n_mels"),
    "n_fft": ("n_fft",),
    "win_size": ("win_size", "win_length"),
    "hop_size": ("hop_size", "hop_length"),
    "fmin": ("fmin",),
    "fmax": ("fmax",),
}


def _config_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON config scalars, treating non-boolean numbers semantically."""
    numeric = (int, float)
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, numeric) and isinstance(right, numeric):
        return math.isfinite(left) and math.isfinite(right) and left == right
    return type(left) is type(right) and left == right


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only the pinned frontend identity and return canonical values."""
    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")
    canonical: dict[str, Any] = {}
    for key, aliases in _CONFIG_ALIASES.items():
        present = [(alias, config[alias]) for alias in aliases if alias in config]
        if not present:
            raise BigVGANRevoCodeError(f"BigVGAN config is missing {key}")
        if any(not _config_values_equal(value, present[0][1]) for _, value in present[1:]):
            raise BigVGANRevoCodeError(f"BigVGAN config has conflicting {key} aliases")
        canonical[key] = present[0][1]
    mismatches = {
        key: {"expected": expected, "actual": canonical[key]}
        for key, expected in EXPECTED_CONFIG.items()
        if not _config_values_equal(canonical[key], expected)
    }
    if mismatches:
        raise BigVGANRevoCodeError(
            f"BigVGAN config does not match pinned frontend: {json.dumps(mismatches, sort_keys=True)}"
        )
    return dict(EXPECTED_CONFIG)


def validate_model_identity(model_id: str, revision: str) -> dict[str, str]:
    if model_id != MODEL_ID or revision != MODEL_REVISION:
        raise BigVGANRevoCodeError(
            f"expected {MODEL_ID}@{MODEL_REVISION}, got {model_id}@{revision}"
        )
    return {"model_id": MODEL_ID, "revision": MODEL_REVISION}


def _audio_channels(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2 or not data.shape[0] or not data.shape[1]:
        raise ValueError("audio must be a nonempty samples-by-channels array")
    if not np.issubdtype(data.dtype, np.number) or not np.isfinite(data).all():
        raise ValueError("audio must contain only finite numeric samples")
    if data.shape[0] <= REFLECT_PAD:
        raise ValueError(f"audio must contain more than {REFLECT_PAD} samples for reflect padding")
    return np.ascontiguousarray(data, dtype=np.float32)


def numpy_mel_spectrogram(
    audio: np.ndarray,
    *,
    mel_filter: np.ndarray | None = None,
    mel_filter_fn: Callable[..., np.ndarray] | None = None,
) -> np.ndarray:
    """Reference NumPy implementation of the pinned BigVGAN mel frontend.

    ``mel_filter_fn`` should implement librosa.filters.mel semantics. Supplying the
    matrix directly makes the transform independently testable.
    """
    channels = _audio_channels(audio).T
    padded = np.pad(channels, ((0, 0), (REFLECT_PAD, REFLECT_PAD)), mode="reflect")
    frame_count = 1 + (padded.shape[1] - N_FFT) // HOP_LENGTH
    if frame_count < 1:
        raise ValueError("audio is too short for one mel frame")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT, axis=1)
    frames = frames[:, ::HOP_LENGTH, :][:, :frame_count, :]
    window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)
    spectrum = np.fft.rfft(frames * window[None, None, :], n=N_FFT, axis=-1)
    magnitude = np.sqrt(
        np.square(spectrum.real) + np.square(spectrum.imag) + MAGNITUDE_EPSILON
    )
    if mel_filter is None:
        if mel_filter_fn is None:
            raise ValueError("mel_filter or librosa-compatible mel_filter_fn is required")
        mel_filter = mel_filter_fn(
            sr=SAMPLE_RATE, n_fft=N_FFT, n_mels=NUM_MELS,
            fmin=FMIN, fmax=FMAX, htk=False, norm="slaney",
        )
    basis = np.asarray(mel_filter)
    if basis.shape != (NUM_MELS, N_FFT // 2 + 1):
        raise ValueError(f"mel filter must have shape {(NUM_MELS, N_FFT // 2 + 1)}")
    mel = np.einsum("mf,ctf->cmt", basis, magnitude, optimize=True)
    return np.log(np.maximum(mel, LOG_CLAMP)).astype(np.float32)


def torch_mel_spectrogram(audio: Any, *, torch_module=None, librosa_module=None):
    """Exact torch/librosa frontend; imports are deferred and injectable."""
    if torch_module is None:
        import torch as torch_module  # type: ignore[no-redef]
    if librosa_module is None:
        import librosa as librosa_module  # type: ignore[no-redef]
    torch = torch_module
    waveform = audio if hasattr(audio, "dim") else torch.as_tensor(audio, dtype=torch.float32)
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    if waveform.dim() != 2 or waveform.shape[-1] <= REFLECT_PAD:
        raise ValueError("audio must be channels-by-samples with sufficient length")
    if not bool(torch.isfinite(waveform).all()):
        raise ValueError("audio must contain only finite samples")
    waveform = torch.nn.functional.pad(
        waveform.unsqueeze(1), (REFLECT_PAD, REFLECT_PAD), mode="reflect"
    ).squeeze(1)
    window = torch.hann_window(N_FFT, device=waveform.device, dtype=waveform.dtype)
    spectrum = torch.stft(
        waveform, N_FFT, hop_length=HOP_LENGTH, win_length=WIN_LENGTH,
        window=window, center=False, pad_mode="reflect", normalized=False,
        onesided=True, return_complex=False,
    )
    magnitude = torch.sqrt(torch.sum(spectrum * spectrum, dim=-1) + MAGNITUDE_EPSILON)
    basis_np = librosa_module.filters.mel(
        sr=SAMPLE_RATE, n_fft=N_FFT, n_mels=NUM_MELS,
        fmin=FMIN, fmax=FMAX, htk=False, norm="slaney",
    )
    basis = torch.as_tensor(basis_np, device=waveform.device, dtype=waveform.dtype)
    mel = torch.matmul(basis, magnitude)
    return torch.log(torch.clamp(mel, min=LOG_CLAMP))


def plan_mel_chunks(total_frames: int, core_frames: int, context_frames: int) -> list[MelChunk]:
    total_frames, core_frames, context_frames = map(
        int, (total_frames, core_frames, context_frames)
    )
    if total_frames < 1 or core_frames < 1 or context_frames < 0:
        raise ValueError("require positive total/core frames and nonnegative context")
    chunks = []
    for index, core_start in enumerate(range(0, total_frames, core_frames)):
        core_stop = min(core_start + core_frames, total_frames)
        chunks.append(MelChunk(
            index=index,
            core_start=core_start,
            core_stop=core_stop,
            expanded_start=max(0, core_start - context_frames),
            expanded_stop=min(total_frames, core_stop + context_frames),
        ))
    return chunks


def _generator_numpy(value: Any) -> np.ndarray:
    if isinstance(value, (tuple, list)):
        if len(value) != 1:
            raise BigVGANRevoCodeError("generator returned an ambiguous sequence")
        value = value[0]
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    output = np.asarray(value)
    if output.ndim == 3 and output.shape[1] == 1:
        output = output[:, 0, :]
    if output.ndim == 1:
        output = output[None, :]
    if output.ndim != 2 or not np.isfinite(output).all():
        raise BigVGANRevoCodeError("generator must return finite batch-by-samples audio")
    return output.astype(np.float32, copy=False)


def reconcile_output_length(audio: np.ndarray, target_samples: int) -> tuple[np.ndarray, dict]:
    data = np.asarray(audio)
    target_samples = int(target_samples)
    if data.ndim != 2 or target_samples < 0:
        raise ValueError("audio must be samples-by-channels and target must be nonnegative")
    generated = data.shape[0]
    if generated > target_samples:
        result, action = data[:target_samples], "trim_right"
    elif generated < target_samples:
        result = np.pad(data, ((0, target_samples - generated), (0, 0)))
        action = "pad_right_zero"
    else:
        result, action = data, "exact"
    report = {
        "target_samples": target_samples,
        "generated_samples": generated,
        "delta_samples": generated - target_samples,
        "action": action,
        "final_samples": result.shape[0],
    }
    return np.ascontiguousarray(result, dtype=np.float32), report


def reconstruct_from_mel(
    mel: np.ndarray,
    generator: Callable[[np.ndarray], Any],
    *,
    target_samples: int,
    core_frames: int,
    context_frames: int,
) -> tuple[np.ndarray, dict]:
    """Run mono channels as one synchronized batch and retain chunk centers."""
    features = np.asarray(mel, dtype=np.float32)
    if features.ndim != 3 or features.shape[1] != NUM_MELS or not np.isfinite(features).all():
        raise ValueError("mel must be finite channels-by-128-by-frames")
    plans = plan_mel_chunks(features.shape[2], core_frames, context_frames)
    retained = []
    chunk_reports = []
    for plan in plans:
        generated = _generator_numpy(
            generator(features[:, :, plan.expanded_start:plan.expanded_stop])
        )
        if generated.shape[0] != features.shape[0]:
            raise BigVGANRevoCodeError("generator changed the per-channel batch size")
        expected = (plan.expanded_stop - plan.expanded_start) * HOP_LENGTH
        if generated.shape[1] != expected:
            raise BigVGANRevoCodeError(
                f"generator returned {generated.shape[1]} samples; expected {expected}"
            )
        keep_start = (plan.core_start - plan.expanded_start) * HOP_LENGTH
        keep_samples = plan.core_frames * HOP_LENGTH
        retained.append(generated[:, keep_start:keep_start + keep_samples])
        chunk_reports.append({
            **asdict(plan), "generated_samples": generated.shape[1],
            "retained_start": keep_start, "retained_samples": keep_samples,
        })
    joined = np.concatenate(retained, axis=1).T
    output, length_report = reconcile_output_length(joined, target_samples)
    return output, {
        "channel_policy": "independent_mono_channels_in_synchronized_batch",
        "channels": features.shape[0],
        "hop_length": HOP_LENGTH,
        "chunks": chunk_reports,
        "length_reconciliation": length_report,
    }


def seam_comparison(
    chunked: np.ndarray,
    reference: np.ndarray,
    boundaries: Sequence[int],
    *,
    radius: int = HOP_LENGTH,
) -> dict:
    """Compare chunk/reference error near seams and away from seams."""
    left, right = np.asarray(chunked), np.asarray(reference)
    if left.shape != right.shape or left.ndim not in (1, 2) or not np.isfinite(left).all() or not np.isfinite(right).all():
        raise ValueError("chunked and reference must be matching finite audio")
    radius = int(radius)
    if radius < 1:
        raise ValueError("radius must be positive")
    error = np.abs(left.astype(np.float64) - right.astype(np.float64))
    sample_error = error if error.ndim == 1 else np.max(error, axis=1)
    seam_mask = np.zeros(left.shape[0], dtype=bool)
    valid = []
    for boundary in boundaries:
        boundary = int(boundary)
        if 0 < boundary < left.shape[0]:
            seam_mask[max(0, boundary - radius):min(left.shape[0], boundary + radius)] = True
            valid.append(boundary)
    def metrics(values):
        return {
            "samples": int(values.size),
            "max_abs": float(np.max(values)) if values.size else 0.0,
            "rms": float(np.sqrt(np.mean(values * values))) if values.size else 0.0,
        }
    return {
        "boundaries": valid, "radius": radius,
        "seam": metrics(sample_error[seam_mask]),
        "non_seam": metrics(sample_error[~seam_mask]),
        "overall": metrics(sample_error),
    }


def verify_checkpoint_sha256(path: str | Path, expected_sha256: str = GENERATOR_SHA256) -> dict:
    checkpoint = Path(path).expanduser().resolve()
    if not checkpoint.is_file():
        raise FileNotFoundError(f"generator checkpoint not found: {checkpoint}")
    digest = hashlib.sha256()
    with checkpoint.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise BigVGANRevoCodeError(
            f"generator SHA256 mismatch: expected {expected_sha256}, got {actual}"
        )
    return {"path": str(checkpoint), "sha256": actual, "bytes": checkpoint.stat().st_size}


def load_verified_runtime(
    model_dir: str | Path,
    *,
    loader: Callable[..., Any] | None = None,
    device: str = "cpu",
):
    """Verify local identity/checkpoint before entering the separate runtime path."""
    root = Path(model_dir).expanduser().resolve()
    config_path = root / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"BigVGAN config not found: {config_path}")
    validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    checkpoint = verify_checkpoint_sha256(
        root / GENERATOR_FILENAME, GENERATOR_SHA256
    )
    if loader is None:
        from bigvgan import BigVGAN
        loader = BigVGAN.from_pretrained
    # The adapter never permits the optional fused CUDA activation kernel.
    model = loader(
        str(root), use_cuda_kernel=False, local_files_only=True,
        revision=MODEL_REVISION,
    )
    if hasattr(model, "to"):
        model = model.to(device)
    if hasattr(model, "eval"):
        model.eval()
    return model, {
        **validate_model_identity(MODEL_ID, MODEL_REVISION),
        "config": dict(EXPECTED_CONFIG), "checkpoint": checkpoint,
        "use_cuda_kernel": False, "device": device,
    }


def save_float_artifacts(
    output_dir: str | Path,
    artifacts: Mapping[str, np.ndarray],
    provenance: Mapping[str, Any],
    *,
    overwrite: bool = False,
    soundfile_module=None,
) -> dict:
    """Save named WAVs as FLOAT plus provenance, refusing accidental overwrite."""
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(f"output directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if soundfile_module is None:
        import soundfile as soundfile_module  # type: ignore[no-redef]
    written = []
    for name, audio in artifacts.items():
        if Path(name).name != name or not name.lower().endswith(".wav"):
            raise ValueError(f"artifact name must be a plain .wav filename: {name}")
        data = _audio_channels(audio)
        path = root / name
        soundfile_module.write(path, data, SAMPLE_RATE, subtype="FLOAT")
        written.append(verify_checkpoint_sha256(path, _sha256(path)))
    report = {
        "adapter": "bigvgan_v2_revocode",
        "model": validate_model_identity(MODEL_ID, MODEL_REVISION),
        "frontend": {**EXPECTED_CONFIG, "reflect_pad": REFLECT_PAD,
                     "magnitude_epsilon": MAGNITUDE_EPSILON,
                     "log_clamp": LOG_CLAMP, "mel_scale": "slaney",
                     "stft_center": False},
        "provenance": dict(provenance), "artifacts": written,
    }
    report_path = root / "provenance.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report"] = {"path": str(report_path), "sha256": _sha256(report_path),
                        "bytes": report_path.stat().st_size}
    return report


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
