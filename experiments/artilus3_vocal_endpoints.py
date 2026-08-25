"""Isolated ARTILUS3 parallel vocal-endpoint composition experiment.

ARTILUS3 is the song identity.  Both endpoint renderers receive the same canonical
original vocal; neither endpoint may consume the other endpoint's output.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import soundfile as sf
from scipy import signal

from experiments.bigvgan_revocode import (
    EXPECTED_CONFIG,
    GENERATOR_FILENAME,
    GENERATOR_SHA256,
    SAMPLE_RATE as CANONICAL_RATE,
    validate_config,
    verify_checkpoint_sha256,
)
from experiments.lew_residual import prepare_audio, read_audio_strict
from experiments.objective_compare import sha256_file
from experiments.official_apollo import run_official_apollo
from experiments.vocal_endpoint_audit import audit_vocal_endpoint, peak_gate
from experiments.vocal_replacement import render_vocal_replacement, save_render

SOURCE_RATE = 48_000
ALPHAS = (0.25, 0.50)
APOLLO_CHECKPOINT_SHA256 = "99d9af7f1ff20e63c393035513a655392818d66b4d7fc23d658175c1f15e8d76"


class Artilus3ExperimentError(RuntimeError):
    pass


def _file(value: str | Path, name: str) -> Path:
    if value is None or not str(value).strip():
        raise ValueError(f"{name} path is empty")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{name} not found: {path}")
    return path


def _expected_hash(path: Path, expected: str, name: str) -> str:
    expected = str(expected).lower()
    actual = sha256_file(path)
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError(f"expected {name} SHA256 must be 64 lowercase hex characters")
    if actual != expected:
        raise Artilus3ExperimentError(
            f"{name} SHA256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def _assert_sources_unchanged(hashes: dict[Path, str]) -> None:
    changed = [str(path) for path, digest in hashes.items() if sha256_file(path) != digest]
    if changed:
        raise Artilus3ExperimentError("source immutability check failed: " + ", ".join(changed))


def _write_float(path: Path, audio: np.ndarray, rate: int) -> None:
    data = np.ascontiguousarray(audio, dtype=np.float32)
    if not np.isfinite(data).all():
        raise Artilus3ExperimentError(f"non-finite canonical audio: {path.name}")
    sf.write(path, data, rate, format="WAV", subtype="FLOAT")


def _to_source_rate(audio: np.ndarray, target_shape: tuple[int, ...]) -> tuple[np.ndarray, dict[str, Any]]:
    converted = signal.resample_poly(audio, 160, 147, axis=0).astype(np.float32)
    generated = len(converted)
    target = target_shape[0]
    if generated > target:
        converted, action = converted[:target], "trim_tail"
    elif generated < target:
        converted = np.pad(converted, ((0, target - generated), (0, 0)))
        action = "pad_tail_zero"
    else:
        action = "exact"
    if converted.shape != target_shape:
        raise Artilus3ExperimentError(
            f"endpoint shape {converted.shape} cannot satisfy source shape {target_shape}"
        )
    return np.ascontiguousarray(converted, dtype=np.float32), {
        "method": "scipy.signal.resample_poly",
        "up": 160,
        "down": 147,
        "generated_samples": generated,
        "target_samples": target,
        "length_action": action,
        "normalization": False,
        "limiting": False,
    }


def run_artilus3_vocal_endpoints(
    *,
    source_mix: str | Path,
    source_vocal: str | Path,
    apollo_checkpoint: str | Path,
    bigvgan_model_dir: str | Path,
    output_dir: str | Path,
    expected_source_sha256: str,
    expected_vocal_sha256: str,
    expected_apollo_sha256: str = APOLLO_CHECKPOINT_SHA256,
    apollo_runner: Callable[..., dict[str, Any]] = run_official_apollo,
    bigvgan_renderer: Callable[..., dict[str, Any]] | None = None,
    prepare: Callable[..., Path] = prepare_audio,
    reader: Callable[[Path], tuple[np.ndarray, int]] = read_audio_strict,
    ffmpeg: str | Path = "ffmpeg",
    apollo_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preflight and compose two parallel, dependency-injected vocal endpoints.

    ``bigvgan_renderer`` must write a 44.1 kHz FLOAT WAV and has the call contract
    ``renderer(original_vocal_44100, output_wav, model_dir=...)``.  It is
    intentionally absent by default so this layer never imports a model runtime.
    """
    mix_path = _file(source_mix, "source mix")
    vocal_path = _file(source_vocal, "source vocal")
    apollo_path = _file(apollo_checkpoint, "Apollo checkpoint")
    model_dir = Path(bigvgan_model_dir).expanduser().resolve()
    if not model_dir.is_dir():
        raise FileNotFoundError(f"BigVGAN model directory not found: {model_dir}")
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir path is empty")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")

    # Every identity gate precedes preparation and either inference callback.
    source_hashes = {
        mix_path: _expected_hash(mix_path, expected_source_sha256, "source mix"),
        vocal_path: _expected_hash(vocal_path, expected_vocal_sha256, "source vocal"),
    }
    apollo_hash = _expected_hash(apollo_path, expected_apollo_sha256, "Apollo checkpoint")
    config_path = _file(model_dir / "config.json", "BigVGAN config")
    validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    bigvgan_checkpoint = verify_checkpoint_sha256(
        model_dir / GENERATOR_FILENAME, GENERATOR_SHA256
    )
    if bigvgan_renderer is None:
        raise Artilus3ExperimentError(
            "BigVGAN renderer is required; inference is deliberately not provided by this layer"
        )

    mix, mix_rate = reader(mix_path)
    vocal, vocal_rate = reader(vocal_path)
    if mix_rate != SOURCE_RATE or vocal_rate != SOURCE_RATE:
        raise Artilus3ExperimentError(
            f"source mix and vocal must be exactly 48000 Hz, got {mix_rate} and {vocal_rate}"
        )
    if mix.shape != vocal.shape:
        raise Artilus3ExperimentError(
            f"source mix/vocal shapes must match exactly: {mix.shape} != {vocal.shape}"
        )
    mix = np.ascontiguousarray(mix, dtype=np.float32)
    vocal = np.ascontiguousarray(vocal, dtype=np.float32)
    _assert_sources_unchanged(source_hashes)

    output.mkdir(parents=True, exist_ok=True)
    canonical_vocal = output / "original_vocal_44100_FLOAT.wav"
    apollo_wet = output / "apollo_vocal_44100_FLOAT.wav"
    bigvgan_wet = output / "bigvgan_vocal_44100_FLOAT.wav"
    prepare(vocal_path, canonical_vocal, ffmpeg=ffmpeg)
    canonical, canonical_rate = reader(canonical_vocal)
    if canonical_rate != CANONICAL_RATE:
        raise Artilus3ExperimentError("canonical vocal preparation did not produce 44100 Hz")
    _write_float(canonical_vocal, canonical, CANONICAL_RATE)
    canonical_hash = sha256_file(canonical_vocal)
    _assert_sources_unchanged(source_hashes)

    apollo_runtime = apollo_runner(
        canonical_vocal, apollo_wet, checkpoint=apollo_path, **(apollo_options or {})
    )
    _assert_sources_unchanged(source_hashes)
    bigvgan_runtime = bigvgan_renderer(
        canonical_vocal, bigvgan_wet, model_dir=model_dir
    )
    _assert_sources_unchanged(source_hashes)

    endpoints: dict[str, Any] = {}
    for name, endpoint_path, runtime in (
        ("apollo", apollo_wet, apollo_runtime),
        ("bigvgan", bigvgan_wet, bigvgan_runtime),
    ):
        endpoint_44100, endpoint_rate = reader(endpoint_path)
        if endpoint_rate != CANONICAL_RATE or endpoint_44100.shape != canonical.shape:
            raise Artilus3ExperimentError(
                f"{name} endpoint must match canonical 44100 Hz shape {canonical.shape}"
            )
        _write_float(endpoint_path, endpoint_44100, CANONICAL_RATE)
        endpoint_48000_raw, resampling = _to_source_rate(endpoint_44100, vocal.shape)
        endpoint_48000_raw_path = output / f"{name}_vocal_48000_raw_resampled_FLOAT.wav"
        _write_float(endpoint_48000_raw_path, endpoint_48000_raw, SOURCE_RATE)

        # Apply the approved vocal-active static LS gain/constant-delay correction,
        # retaining the unmodified resampled endpoint as a separate artifact.
        audit = audit_vocal_endpoint(
            vocal, endpoint_48000_raw, SOURCE_RATE, target_shape=vocal.shape,
            correct=True,
            sample_peak_limit=np.finfo(np.float64).max,
            true_peak_limit=np.finfo(np.float64).max,
        )
        endpoint_48000_audited_path = output / f"{name}_vocal_48000_audited_matched_FLOAT.wav"
        _write_float(endpoint_48000_audited_path, audit.audited, SOURCE_RATE)
        peaks = peak_gate(audit.audited)
        renders = {}
        for alpha in ALPHAS:
            render = render_vocal_replacement(
                mix, vocal, audit.audited, SOURCE_RATE, SOURCE_RATE, SOURCE_RATE,
                alpha=alpha,
            )
            render_paths = save_render(
                render, output / f"{name}_alpha_{alpha:.2f}", SOURCE_RATE
            )
            render_peaks = peak_gate(render.processed)
            renders[f"{alpha:.2f}"] = {
                "paths": {key: str(value) for key, value in render_paths.items()},
                "affine_identity": render.report["affine_identity"],
                "peak_gate": render_peaks,
                "audition_included": bool(peaks["passed"] and render_peaks["passed"]),
                "exclusion_reasons": [] if peaks["passed"] and render_peaks["passed"] else [
                    "sample_or_true_peak_gate_failed; no normalization or limiting applied"
                ],
            }
        endpoints[name] = {
            "branch_input": str(canonical_vocal),
            "branch_input_sha256": canonical_hash,
            "upstream_endpoint": None,
            "runtime": runtime,
            "canonical_44100_raw_output": str(endpoint_path),
            "source_rate_48000_raw_resampled_output": str(endpoint_48000_raw_path),
            "source_rate_48000_audited_matched_output": str(endpoint_48000_audited_path),
            "replacement_input": str(endpoint_48000_audited_path),
            "audit_correction_requested": True,
            "resampling": resampling,
            "audit": audit.report,
            "endpoint_peak_gate": peaks,
            "renders": renders,
        }

    _assert_sources_unchanged(source_hashes)
    report = {
        "experiment": "ARTILUS3_parallel_vocal_endpoints",
        "song": "ARTILUS3",
        "topology": "parallel_from_same_original_vocal",
        "stacking": False,
        "source_rate": SOURCE_RATE,
        "canonical_rate": CANONICAL_RATE,
        "normalization": False,
        "limiting": False,
        "sources": {
            "mix": {"path": str(mix_path), "sha256": source_hashes[mix_path]},
            "vocal": {"path": str(vocal_path), "sha256": source_hashes[vocal_path]},
        },
        "checkpoints": {
            "apollo": {"path": str(apollo_path), "sha256": apollo_hash},
            "bigvgan": bigvgan_checkpoint,
        },
        "endpoints": endpoints,
        "source_hashes_unchanged": True,
    }
    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report
