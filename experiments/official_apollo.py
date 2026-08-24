"""Isolated official Apollo endpoint and conservative-residual experiment."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from experiments.lew_residual import (
    DEFAULT_PYTHON,
    SAMPLE_RATE,
    _artifact,
    _finite_number,
    _git_info,
    _probe_cuda,
    _require_file,
    conservation_metrics,
    extract_added,
    mix_without_normalization,
    prepare_audio,
    read_audio_strict,
    require_matching_audio,
)

DEFAULT_APOLLO_DIR = Path(r"D:\_3.AI\audio_upscale\Apollo")
DEFAULT_INFERENCE_SCRIPT = DEFAULT_APOLLO_DIR / "inference.py"
DEFAULT_OFFICIAL_CHECKPOINT = DEFAULT_APOLLO_DIR / "ckpts" / "pytorch_model.bin"


class OfficialApolloExperimentError(RuntimeError):
    pass


def run_official_apollo(input_wav, output_wav, *, python=DEFAULT_PYTHON,
                        inference_script=DEFAULT_INFERENCE_SCRIPT,
                        checkpoint=DEFAULT_OFFICIAL_CHECKPOINT, device="cuda",
                        chunk_seconds=None, overlap_seconds=1.0,
                        chunk_batch_size=1, timeout=7200.0) -> dict:
    input_wav = _require_file(input_wav, "prepared input")
    python = _require_file(python, "Python runtime")
    inference_script = _require_file(inference_script, "Apollo inference script")
    checkpoint = _require_file(checkpoint, "official Apollo checkpoint")
    if output_wav is None or not str(output_wav).strip():
        raise ValueError("Apollo output path is empty")
    output_wav = Path(output_wav).expanduser().resolve()
    if output_wav in {input_wav, python, inference_script, checkpoint}:
        raise ValueError("Apollo output must not alias an input or runtime artifact")
    if device not in {"auto", "cuda", "cpu"}:
        raise ValueError("device must be auto, cuda, or cpu")
    if device == "cuda":
        _probe_cuda(python, float(timeout))
    timeout = _finite_number("timeout", timeout, minimum=np.finfo(float).eps)
    overlap_seconds = _finite_number("overlap_seconds", overlap_seconds, minimum=0.0)
    chunk_batch_size = int(chunk_batch_size)
    if chunk_batch_size < 1:
        raise ValueError("chunk_batch_size must be positive")
    if chunk_seconds is not None:
        chunk_seconds = _finite_number("chunk_seconds", chunk_seconds,
                                       minimum=1 / SAMPLE_RATE)
        if overlap_seconds * 2 > chunk_seconds:
            raise ValueError("overlap_seconds must not exceed half chunk_seconds")

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(python), str(inference_script), "--in_wav", str(input_wav),
           "--out_wav", str(output_wav), "--checkpoint", str(checkpoint),
           "--device", device, "--overlap-seconds", str(overlap_seconds),
           "--chunk-batch-size", str(chunk_batch_size)]
    if chunk_seconds is not None:
        cmd.extend(["--chunk-seconds", str(chunk_seconds)])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(inference_script.parent)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd, cwd=inference_script.parent, env=env, capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OfficialApolloExperimentError(
            f"official Apollo timed out after {timeout:g} seconds"
        ) from exc
    elapsed = time.monotonic() - started
    if completed.returncode:
        detail = (completed.stderr or completed.stdout)[-1500:]
        raise OfficialApolloExperimentError(
            f"official Apollo failed ({completed.returncode}): {detail}"
        )
    if not output_wav.is_file():
        raise OfficialApolloExperimentError("official Apollo did not create its output")
    return {"command": cmd, "seconds": elapsed,
            "stdout": completed.stdout[-1500:]}


def audit_endpoint(dry: np.ndarray, wet: np.ndarray, *, strength=0.5,
                   extra_wets=(), allow_over=False, n_fft=4096, hop=1024,
                   max_lag=64, min_correlation=0.90, alignment_windows=7,
                   alignment_lowpass_hz=5_000.0, ratio_limit=0.5,
                   activity_floor_db=-60.0, support_floor_db=-70.0,
                   constructive_gate_dry_floor_db=-120.0,
                   lowband_max_db=-35.0, silent_max_db=-40.0,
                   silence_db=-60.0) -> tuple[np.ndarray, np.ndarray, dict]:
    added, alignment = extract_added(
        dry, wet, extra_wets=extra_wets, strength=strength, n_fft=n_fft,
        hop=hop, max_lag=max_lag, min_correlation=min_correlation,
        alignment_windows=alignment_windows,
        alignment_lowpass_hz=alignment_lowpass_hz,
        activity_floor_db=activity_floor_db, ratio_limit=ratio_limit,
        support_floor_db=support_floor_db,
        constructive_gate_dry_floor_db=constructive_gate_dry_floor_db,
    )
    mixed, clipping = mix_without_normalization(
        dry, added, allow_over=allow_over
    )
    conservation = conservation_metrics(
        dry, added, mixed, lowband_max_db=lowband_max_db,
        silent_max_db=silent_max_db, silence_db=silence_db,
    )
    return added, mixed, {
        "alignment": alignment,
        "constructive_gate": alignment["constructive_gate"],
        "conservation": conservation,
        "clipping": clipping,
    }


def run_experiment(input_path, output_dir, *, strength=0.5, device="cuda",
                   chunk_seconds=None, overlap_seconds=1.0,
                   chunk_batch_size=1, timeout=7200.0, allow_over=False,
                   overwrite=False, python=DEFAULT_PYTHON,
                   inference_script=DEFAULT_INFERENCE_SCRIPT,
                   checkpoint=DEFAULT_OFFICIAL_CHECKPOINT, ffmpeg="ffmpeg",
                   **audit_options) -> dict:
    started_wall = time.time()
    started = time.monotonic()
    source = _require_file(input_path, "input")
    if output_dir is None or not str(output_dir).strip():
        raise ValueError("output_dir path is empty")
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    dry_path = output_dir / "dry_prepared.wav"
    wet_path = output_dir / "apollo_official_wet.wav"
    residual_path = output_dir / "apollo_official_residual_raw.wav"
    added_path = output_dir / "apollo_official_residual_added.wav"
    mix_path = output_dir / "apollo_official_conservative.wav"
    prepare_audio(source, dry_path, ffmpeg=ffmpeg)
    dry, dry_rate = read_audio_strict(dry_path)
    runtime = run_official_apollo(
        dry_path, wet_path, python=python, inference_script=inference_script,
        checkpoint=checkpoint, device=device, chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds, chunk_batch_size=chunk_batch_size,
        timeout=timeout,
    )
    wet, wet_rate = read_audio_strict(wet_path)
    require_matching_audio(dry, wet, dry_rate, wet_rate)
    added, mixed, audit = audit_endpoint(
        dry, wet, strength=strength, allow_over=allow_over, **audit_options
    )
    sf.write(residual_path, (wet - dry).astype(np.float32), SAMPLE_RATE,
             subtype="FLOAT")
    sf.write(added_path, added.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")
    sf.write(mix_path, mixed.astype(np.float32), SAMPLE_RATE, subtype="FLOAT")

    checkpoint = _require_file(checkpoint, "official Apollo checkpoint")
    inference_script = _require_file(inference_script, "Apollo inference script")
    artifacts = [_artifact(path) for path in
                 (dry_path, wet_path, residual_path, added_path, mix_path)]
    report = {
        "adapter": "official_apollo_endpoint_audit",
        "input": _artifact(source),
        "sample_rate": SAMPLE_RATE,
        "shape": list(dry.shape),
        "params": {
            "strength": float(strength), "device": device,
            "chunk_seconds": chunk_seconds,
            "overlap_seconds": float(overlap_seconds),
            "chunk_batch_size": int(chunk_batch_size),
            "allow_over": bool(allow_over), **audit_options,
        },
        **audit,
        "git": _git_info(Path(__file__).resolve().parents[1]),
        "provenance": {
            "checkpoint": _artifact(checkpoint),
            "inference_script": _artifact(inference_script),
            "apollo_git": _git_info(inference_script.parent),
        },
        "runtime": {
            "started_unix": started_wall,
            "total_seconds": time.monotonic() - started,
            "apollo_seconds": runtime["seconds"],
            "python": str(Path(python).resolve()),
        },
        "artifacts": artifacts,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True),
                           encoding="utf-8")
    report["report"] = _artifact(report_path)
    return report
