"""Reproducible local probes; music is read-only and never bundled with the app.

Run baseline with the existing audio interpreter; run matchering in the isolated
validation environment. Results and listening files go to --output-dir.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf
from scipy import signal

from audio_metrics import measure_audio
from mastering import LOUDNESS_TARGETS
from mastering.finalizer import master_file


def measurements(path):
    audio, sr = sf.read(path, always_2d=True)
    result = measure_audio(audio, sr)
    result["subtype"] = sf.info(path).subtype
    return result


def timed(call):
    started = time.perf_counter()
    value = call()
    return value, round(time.perf_counter() - started, 3)


def baseline(args):
    report = {"samples": {}}
    env = os.environ.copy()
    legacy_dir = ROOT / "dev_runtime" / "Soren_src"
    legacy_core = legacy_dir / "core_decrypted.py"
    if legacy_core.read_bytes() != (ROOT / "packaging" / "soren_core.py").read_bytes():
        raise RuntimeError("Legacy dev runtime is stale; verify/rebuild it before probing")
    env["PYTHONPATH"] = str(legacy_dir)
    for src in args.input:
        name = src.parent.name + "_" + src.stem
        row = {"input": measurements(src), "runs": {}}
        for mode in LOUDNESS_TARGETS:
            for engine in ("legacy", "independent"):
                dst = args.output_dir / f"{name}_{engine}_{mode}.wav"
                if engine == "legacy":
                    cmd = [sys.executable, str(legacy_core),
                           str(src), str(dst), "--genre", "Pop", "--style-mode", "off",
                           "--loudness", mode]
                    with dst.with_suffix(".log").open("w", encoding="utf-8") as log:
                        _, elapsed = timed(lambda: subprocess.run(
                            cmd, cwd=legacy_dir, env=env, stdout=log, stderr=log, check=True))
                    stats = json.loads(Path(str(dst) + ".mastering.json").read_text(encoding="utf-8"))
                else:
                    stats, elapsed = timed(lambda: master_file(src, dst, mode))
                row["runs"][engine + "_" + mode] = {
                    "seconds": elapsed, "metrics": measurements(dst), "mastering": stats}
        ref_dst = args.output_dir / f"{name}_legacy_reference.wav"
        cmd = [sys.executable, str(legacy_core),
               str(src), str(ref_dst), "--reference", str(args.reference),
               "--reference-tone-only", "--genre", "Pop", "--loudness", "normal"]
        with ref_dst.with_suffix(".log").open("w", encoding="utf-8") as log:
            _, elapsed = timed(lambda: subprocess.run(
                cmd, cwd=legacy_dir, env=env, stdout=log, stderr=log, check=True))
        row["runs"]["legacy_reference"] = {"seconds": elapsed, "metrics": measurements(ref_dst)}
        # Equal-loudness comparison files. One common low target avoids adding
        # clipping to either comparison candidate. Original render is untouched.
        for mode in LOUDNESS_TARGETS:
            pair = [args.output_dir / f"{name}_{engine}_{mode}.wav"
                    for engine in ("legacy", "independent")]
            metrics = [measurements(p) for p in pair]
            target = min(m["integrated_lufs"] - max(0, m["true_peak_4x_dbtp"] + 1)
                         for m in metrics)
            for path, m in zip(pair, metrics):
                audio, sr = sf.read(path, always_2d=True)
                sf.write(path.with_name(path.stem + "_equal_loudness.wav"),
                         audio * 10 ** ((target - m["integrated_lufs"]) / 20), sr, subtype="FLOAT")
        report["samples"][name] = row
        print(f"Completed baseline: {name}", flush=True)
    return report


def matchering_probe(args):
    import matchering as mg
    import matchering.stages as stages
    # A FLOAT/no-limiter request must not execute Matchering's limiter.
    def forbidden(*a, **k):
        raise AssertionError("Matchering limiter unexpectedly called")
    stages.limit = forbidden
    report = {"version": mg.__version__, "runs": {}}
    for src in args.input:
        dst = args.output_dir / (src.parent.name + "_candidate.wav")
        _, elapsed = timed(lambda: mg.process(str(src), str(args.reference),
            [mg.Result(str(dst), "FLOAT", use_limiter=False, normalize=False)]))
        report["runs"][src.parent.name] = {"seconds": elapsed, "metrics": measurements(dst)}
    # Known broadband content allows both alignment and resampling comparisons.
    sr = 44100
    rng = np.random.default_rng(20260919)
    x = signal.sosfilt(signal.butter(2, 14000, fs=sr, output="sos"),
                      rng.normal(0, .06, (sr * 3, 2)), axis=0)
    target = args.output_dir / "probe_source.wav"
    sf.write(target, x, sr, subtype="FLOAT")
    report["sample_rates"] = {}
    unity_reference = args.output_dir / "probe_reference_44100.wav"
    sf.write(unity_reference, x * .8, sr, subtype="FLOAT")
    for rate in (44100, 48000, 88200, 96000):
        divisor = math.gcd(sr, rate)
        ref = args.output_dir / f"probe_reference_{rate}.wav"
        src = args.output_dir / f"probe_source_{rate}.wav"
        ref_audio = signal.resample_poly(x * .8, rate // divisor, sr // divisor, axis=0)
        sf.write(ref, ref_audio, rate, subtype="FLOAT")
        sf.write(src, ref_audio / .8, rate, subtype="FLOAT")
        pairs = [("both", src, ref), ("reference", target, ref),
                 ("source", src, unity_reference)] if rate != sr else [("both", src, ref)]
        for kind, source_path, reference_path in pairs:
            dst = args.output_dir / f"probe_output_{kind}_{rate}.wav"
            _, elapsed = timed(lambda: mg.process(str(source_path), str(reference_path),
                [mg.Result(str(dst), "FLOAT", use_limiter=False, normalize=False)]))
            y, out_sr = sf.read(dst, always_2d=True)
            correlation = signal.correlate(y[:, 0], x[:, 0], mode="full", method="fft")
            lag = int(np.argmax(correlation) - len(x) + 1)
            n = min(len(x) - max(-lag, 0), len(y) - max(lag, 0))
            aligned_x = x[max(-lag, 0):max(-lag, 0) + n, 0]
            aligned_y = y[max(lag, 0):max(lag, 0) + n, 0]
            report["sample_rates"][f"{kind}_{rate}"] = dict(seconds=elapsed, lag_samples=lag,
                frames=len(y), sample_rate=out_sr, finite=bool(np.isfinite(y).all()),
                aligned_correlation=float(np.corrcoef(aligned_x, aligned_y)[0, 1]),
                correlation=float(np.corrcoef(x[:, 0], y[:, 0])[0, 1]) if len(x) == len(y) else None)
    report["edge_cases"] = {}
    for name, audio in {"mono": x[:, :1], "near_mono": np.column_stack((x[:, 0], x[:, 0] + x[:, 1] * .001)),
                        "silence": np.zeros_like(x), "short": x[:1000]}.items():
        src = args.output_dir / f"probe_{name}.wav"
        dst = args.output_dir / f"probe_{name}_out.wav"
        sf.write(src, audio, sr, subtype="FLOAT")
        try:
            mg.process(str(src), str(target), [mg.Result(str(dst), "FLOAT", False, False)])
            report["edge_cases"][name] = {"status": "ok", "metrics": measurements(dst)}
        except Exception as exc:
            report["edge_cases"][name] = {"status": "rejected", "error": str(exc)}
    return report


def json_safe(value):
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("baseline", "matchering"))
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.input = [p.resolve() for p in args.input]
    args.reference = args.reference.resolve()
    report = (baseline if args.mode == "baseline" else matchering_probe)(args)
    report["python"] = sys.version
    report["dependencies"] = {name: importlib.metadata.version(name)
        for name in ("numpy", "scipy", "soundfile", "pyloudnorm", "numba")}
    report["input_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in [*args.input, args.reference]}
    path = args.output_dir / (args.mode + ".json")
    path.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(path, flush=True)


if __name__ == "__main__":
    main()
