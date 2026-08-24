"""Isolated objective audio comparison and deterministic blind audition utility."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

import numpy as np
import soundfile as sf
from scipy import signal

BANDS_HZ = ((0, 4_000), (4_000, 8_000), (8_000, 12_000),
            (12_000, 16_000), (16_000, 20_000), (20_000, 22_000))


class ComparisonError(RuntimeError):
    """Raised when comparison inputs or generated auditions are unsafe."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_path(value, name: str) -> Path:
    if value is None or not str(value).strip():
        raise ValueError(f"{name} path is empty")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{name} not found: {path}")
    return path


def load_float_candidates(dry_path, candidates: Mapping[str, object]):
    """Load canonical FLOAT WAVs and require one common rate and sample shape."""
    if not candidates:
        raise ValueError("at least one candidate is required")
    labels = [str(label) for label in candidates]
    if any(not label.strip() for label in labels) or len(set(labels)) != len(labels):
        raise ValueError("candidate labels must be nonempty and unique")

    items = [("dry", _canonical_path(dry_path, "dry"))]
    items.extend((str(label), _canonical_path(path, f"candidate {label}"))
                 for label, path in candidates.items())
    paths = [path for _, path in items]
    if len(set(paths)) != len(paths):
        raise ValueError("dry and candidate paths must be distinct")

    loaded: dict[str, np.ndarray] = {}
    hashes: dict[str, str] = {}
    expected_shape = None
    expected_rate = None
    for label, path in items:
        info = sf.info(path)
        if info.format != "WAV" or info.subtype != "FLOAT":
            raise ValueError(f"{label} must be a FLOAT WAV, got {info.format}/{info.subtype}")
        audio, rate = sf.read(path, dtype="float64", always_2d=True)
        if audio.size == 0 or not np.isfinite(audio).all():
            raise ValueError(f"{label} must contain finite, nonempty audio")
        if expected_shape is None:
            expected_shape, expected_rate = audio.shape, int(rate)
        elif audio.shape != expected_shape or int(rate) != expected_rate:
            raise ValueError(
                f"{label} shape/rate {audio.shape}/{rate} does not match "
                f"dry {expected_shape}/{expected_rate}"
            )
        loaded[label] = np.ascontiguousarray(audio)
        hashes[label] = sha256_file(path)
    return loaded, int(expected_rate), dict(items), hashes


def _fallback_loudness(audio: np.ndarray, sample_rate: int) -> float:
    """Gated BS.1770-style fallback with K weighting and channel-safe behavior."""
    if sample_rate < 8_000:
        raise ValueError("sample rate is too low for loudness measurement")
    # ITU-R BS.1770 K-weighting: high shelf followed by RLB high pass.
    shelf_b, shelf_a = signal.bilinear(
        [1.53512485958697, 2 * math.pi * 1681.974450955533 * 1.69065929318241,
         (2 * math.pi * 1681.974450955533) ** 2],
        [1.0, 2 * math.pi * 1681.974450955533 * 1.69065929318241,
         (2 * math.pi * 1681.974450955533) ** 2], fs=sample_rate,
    )
    high_b, high_a = signal.bilinear(
        [1.0, 0.0, 0.0],
        [1.0, 2 * math.pi * 38.13547087602444 * 1.0,
         (2 * math.pi * 38.13547087602444) ** 2], fs=sample_rate,
    )
    weighted = signal.lfilter(shelf_b, shelf_a, audio, axis=0)
    weighted = signal.lfilter(high_b, high_a, weighted, axis=0)
    block = max(1, int(round(0.400 * sample_rate)))
    hop = max(1, int(round(0.100 * sample_rate)))
    if len(weighted) < block:
        starts = [0]
        block = len(weighted)
    else:
        starts = range(0, len(weighted) - block + 1, hop)
    energies = np.asarray([
        float(np.sum(np.mean(weighted[start:start + block] ** 2, axis=0)))
        for start in starts
    ])
    levels = -0.691 + 10 * np.log10(np.maximum(energies, 1e-300))
    absolute = levels >= -70.0
    if not absolute.any():
        return float("-inf")
    preliminary = -0.691 + 10 * math.log10(float(np.mean(energies[absolute])))
    gated = absolute & (levels >= preliminary - 10.0)
    if not gated.any():
        return float("-inf")
    return -0.691 + 10 * math.log10(float(np.mean(energies[gated])))


def integrated_loudness(audio: np.ndarray, sample_rate: int) -> tuple[float, str]:
    data = np.asarray(audio, dtype=np.float64)
    try:
        import pyloudnorm as pyln  # type: ignore
        value = float(pyln.Meter(sample_rate).integrated_loudness(data))
        if math.isfinite(value) or value == float("-inf"):
            return value, "pyloudnorm"
    except (ImportError, AttributeError, RuntimeError, ValueError, FloatingPointError):
        pass
    return _fallback_loudness(data, sample_rate), "bs1770_fallback"


def _db(value: float, *, power: bool = False) -> float:
    if value <= 0:
        return -300.0
    return max(-300.0, (10.0 if power else 20.0) * math.log10(value))


def measure_audio(audio: np.ndarray, sample_rate: int) -> dict:
    data = np.asarray(audio, dtype=np.float64)
    loudness, backend = integrated_loudness(data, sample_rate)
    sample_peak = float(np.max(np.abs(data)))
    oversampled = signal.resample_poly(data, 4, 1, axis=0,
                                       window=("kaiser", 8.6), padtype="line")
    true_peak = max(sample_peak, float(np.max(np.abs(oversampled))))
    rms = float(np.sqrt(np.mean(data ** 2)))

    spectrum = np.fft.rfft(data, axis=0)
    frequencies = np.fft.rfftfreq(len(data), 1 / sample_rate)
    # Sum |X|^2 and normalize by the sum over all bins; report absolute dBFS too.
    bin_power = np.mean(np.abs(spectrum) ** 2, axis=1)
    total = float(np.sum(bin_power))
    bands = {}
    for low, high in BANDS_HZ:
        effective_high = min(float(high), sample_rate / 2)
        selected = (frequencies >= low) & (frequencies < effective_high)
        power = float(np.sum(bin_power[selected])) if effective_high > low else 0.0
        key = f"{low // 1000}-{high // 1000}k"
        bands[key] = {
            "relative_energy_db": _db(power / total, power=True) if total else -300.0,
            "fraction": power / total if total else 0.0,
        }

    stereo_corr = None
    side_mid = None
    if data.shape[1] == 2:
        left = data[:, 0] - np.mean(data[:, 0])
        right = data[:, 1] - np.mean(data[:, 1])
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        stereo_corr = float(np.dot(left, right) / denominator) if denominator else 0.0
        mid = (data[:, 0] + data[:, 1]) * 0.5
        side = (data[:, 0] - data[:, 1]) * 0.5
        mid_energy = float(np.mean(mid ** 2))
        side_energy = float(np.mean(side ** 2))
        side_mid = {
            "ratio": math.sqrt(side_energy / mid_energy) if mid_energy else None,
            "db": _db(math.sqrt(side_energy / mid_energy)) if mid_energy else None,
        }
    return {
        "integrated_lufs": loudness,
        "loudness_backend": backend,
        "sample_peak": sample_peak,
        "sample_peak_dbfs": _db(sample_peak),
        "true_peak_4x": true_peak,
        "true_peak_4x_dbtp": _db(true_peak),
        "rms": rms,
        "crest_factor_db": _db(sample_peak / rms) if rms else None,
        "band_energies": bands,
        "stereo_correlation": stereo_corr,
        "side_mid": side_mid,
    }


def _static_loudness_gain(target_lufs: float, source_lufs: float) -> tuple[float, float]:
    if target_lufs == float("-inf") and source_lufs == float("-inf"):
        return 0.0, 1.0
    if not math.isfinite(target_lufs) or not math.isfinite(source_lufs):
        raise ComparisonError("cannot loudness-match silent audio to non-silent audio")
    gain_db = target_lufs - source_lufs
    return gain_db, 10 ** (gain_db / 20)


def generate_blind_comparison(dry_path, candidates: Mapping[str, object], blind_dir,
                              key_path, *, seed: int, overwrite: bool = False) -> dict:
    """Measure canonicals and make statically LUFS-matched anonymous FLOAT WAVs."""
    blind_dir = Path(blind_dir).expanduser().resolve()
    key_path = Path(key_path).expanduser().resolve()
    try:
        key_path.relative_to(blind_dir)
    except ValueError:
        pass
    else:
        raise ValueError("private key JSON must be outside the blind directory")
    if blind_dir.exists() and any(blind_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"refusing to overwrite nonempty blind directory: {blind_dir}")
    if key_path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite private key: {key_path}")

    audio, sample_rate, source_paths, source_hashes = load_float_candidates(
        dry_path, candidates
    )
    metrics = {label: measure_audio(data, sample_rate) for label, data in audio.items()}
    dry_lufs = metrics["dry"]["integrated_lufs"]
    auditions = {}
    for label, data in audio.items():
        gain_db, gain = _static_loudness_gain(dry_lufs, metrics[label]["integrated_lufs"])
        copy = data * gain
        peak = float(np.max(np.abs(copy)))
        if peak > 1.0:
            raise ComparisonError(
                f"post-gain clipping rejected for {label}: peak={peak:.9g}, gain={gain_db:.6g} dB"
            )
        auditions[label] = (copy.astype(np.float32), gain_db, peak)

    order = list(audio)
    random.Random(int(seed)).shuffle(order)
    anonymous = {label: f"candidate_{index + 1:03d}.wav"
                 for index, label in enumerate(order)}

    parent = blind_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{blind_dir.name}.stage-", dir=parent))
    try:
        output_hashes = {}
        for label in order:
            filename = anonymous[label]
            sf.write(stage / filename, auditions[label][0], sample_rate,
                     format="WAV", subtype="FLOAT")
            output_hashes[label] = sha256_file(stage / filename)
        if blind_dir.exists():
            shutil.rmtree(blind_dir)
        os.replace(stage, blind_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    final_hashes = {label: sha256_file(path) for label, path in source_paths.items()}
    if final_hashes != source_hashes:
        shutil.rmtree(blind_dir, ignore_errors=True)
        raise ComparisonError("canonical source hash changed during generation")

    key = {
        "schema_version": 1,
        "seed": int(seed),
        "parameters": {
            "loudness_target": "dry integrated LUFS",
            "gain": "static scalar only",
            "clipping_policy": "reject peak > 1.0; no normalization or limiting",
            "true_peak_oversampling": 4,
            "output_subtype": "FLOAT",
        },
        "sample_rate": sample_rate,
        "shape": list(next(iter(audio.values())).shape),
        "entries": [{
            "anonymous_wav": anonymous[label],
            "identity": label,
            "source_path": str(source_paths[label]),
            "source_sha256": source_hashes[label],
            "output_sha256": output_hashes[label],
            "static_gain_db": auditions[label][1],
            "post_gain_sample_peak": auditions[label][2],
            "canonical_metrics": metrics[label],
        } for label in order],
    }
    temporary_key = key_path.with_name(f".{key_path.name}.tmp")
    try:
        temporary_key.write_text(json.dumps(key, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary_key, key_path)
    finally:
        temporary_key.unlink(missing_ok=True)
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", required=True, type=Path)
    parser.add_argument("--candidate", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--blind-dir", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    candidates = {}
    for item in args.candidate:
        if "=" not in item:
            raise SystemExit("--candidate must be LABEL=PATH")
        label, path = item.split("=", 1)
        if label in candidates:
            raise SystemExit(f"duplicate candidate label: {label}")
        candidates[label] = Path(path)
    generate_blind_comparison(args.dry, candidates, args.blind_dir, args.key,
                              seed=args.seed, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
