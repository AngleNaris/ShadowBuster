#!/usr/bin/env python
"""P4-01 black-box capability probe for the Soren mastering engine.

Runs core_decrypted.py as a subprocess exactly the way
studio_backend.stage_soren does (cwd=soren_dir, PYTHONPATH=soren_dir) over a
fixed probe matrix, then measures every output with the repo's own
audio_metrics module (integrated LUFS, sample peak, 4x true peak) plus
soundfile format info and the engine-written <out>.mastering.json sidecar.

Pure stdlib + numpy/scipy/soundfile for the runner; audio_metrics is imported
from the repo root via sys.path insertion. Idempotent: all outputs go to a
report dir (default: a temp dir outside the repo), no repo audio is touched.

Usage:
    python tools/probe_soren_capabilities.py [--out-dir DIR]
        [--soren-dir DIR] [--python PY] [--input WAV] [--reference WAV]
        [--timeout SECONDS] [--runs name1,name2,...]

The engine needs pyloudnorm/librosa/numba, so --python should point at the
backend runtime python (default: SOREN_PROBE_PYTHON env var, else the
UniverSR venv python discovered next to the repo, else sys.executable).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import audio_metrics  # noqa: E402  (repo root inserted above)

DEFAULT_SOREN_DIR = REPO_ROOT / "dev_runtime" / "Soren_src"

# The suggested P4-01 matrix. Every run reuses the same input/reference so the
# numbers are directly comparable. "expect_target_lufs" is what the profile
# JSON + loudness offset table predicts (Pop base -9.20 for off/eq_only runs
# because core_decrypted defaults genre to Pop when only --style-mode is set).
PROBE_MATRIX = [
    {
        "name": "1_baseline_off_normal",
        "args": ["--style-mode", "off", "--loudness", "normal"],
        "note": "baseline: transparent, loudness only (genre defaults to Pop profile)",
        "expect_target_lufs": -9.199554764198625 + 0.0,
    },
    {
        "name": "2_styled_pop_blend085",
        "args": ["--style-mode", "styled", "--genre", "Pop",
                 "--style-blend", "0.85", "--loudness", "normal"],
        "note": "full styled Pop at default blend",
        "expect_target_lufs": -9.199554764198625 + 0.0,
    },
    {
        "name": "3_styled_pop_blend035",
        "args": ["--style-mode", "styled", "--genre", "Pop",
                 "--style-blend", "0.35", "--loudness", "normal"],
        "note": "same as run 2 but blend 0.35: does blend change output measurably?",
        "expect_target_lufs": -9.199554764198625 + 0.0,
    },
    {
        "name": "4_eqonly_bright",
        "args": ["--style-mode", "eq_only", "--eq-profile", "Bright",
                 "--loudness", "normal"],
        "note": "EQ-only Bright; loudness still Pop-profile driven",
        "expect_target_lufs": -9.199554764198625 + 0.0,
    },
    {
        "name": "5_off_soft",
        "args": ["--style-mode", "off", "--loudness", "soft"],
        "note": "soft loudness preset (offset -3.10 LU)",
        "expect_target_lufs": -9.199554764198625 + (-3.10),
    },
    {
        "name": "6_off_loud",
        "args": ["--style-mode", "off", "--loudness", "loud"],
        "note": "loud loudness preset (offset +2.50 LU, limiter release 80 ms)",
        "expect_target_lufs": -9.199554764198625 + 2.50,
    },
    {
        "name": "7_reference_styled_blend085",
        "args": ["--reference", "REF", "--style-mode", "styled",
                 "--style-blend", "0.85", "--loudness", "normal"],
        "note": "custom reference: reference overrides genre; does reference set LUFS target?",
        "expect_target_lufs": None,  # filled at runtime: measured ref LUFS + offset
    },
    {
        "name": "8_styled_orchestral_blend085",
        "args": ["--style-mode", "styled", "--genre", "Orchestral",
                 "--style-blend", "0.85", "--loudness", "normal"],
        "note": "genre profile LUFS shift: Orchestral -19.88 vs Pop -9.20",
        "expect_target_lufs": -19.878332966828747 + 0.0,
    },
    {
        "name": "9_invalid_genre_orchestra",
        "args": ["--style-mode", "styled", "--genre", "Orchestra",
                 "--style-blend", "0.85", "--loudness", "normal"],
        "note": "invalid genre name (profile file is Orchestral_profile.json): error handling check, fails fast",
        "expect_target_lufs": None,
    },
]


def find_python() -> str:
    """Locate a python that has the engine's deps (pyloudnorm, librosa, numba)."""
    env = os.environ.get("SOREN_PROBE_PYTHON")
    if env and Path(env).is_file():
        return str(Path(env))
    candidate = REPO_ROOT.parent / "UniverSR" / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def run_engine(python: str, soren_dir: Path, input_wav: Path, out_wav: Path,
               extra_args: list[str], timeout: float) -> dict:
    cmd = [str(python), str(soren_dir / "core_decrypted.py"),
           str(input_wav), str(out_wav)] + extra_args
    env = os.environ.copy()
    env["PYTHONPATH"] = str(soren_dir)
    started = time.time()
    proc = None
    try:
        proc = subprocess.run(cmd, cwd=str(soren_dir), env=env,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        ok = proc.returncode == 0 and out_wav.is_file()
        stdout_tail = proc.stdout[-4000:]
        stderr_tail = proc.stderr[-4000:]
    except subprocess.TimeoutExpired as exc:
        ok = False
        stdout_tail = (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else ""
        stderr_tail = f"TIMEOUT after {timeout}s"
    elapsed = time.time() - started
    return {"ok": ok, "cmd": cmd, "returncode": None if proc is None else proc.returncode,
            "seconds": elapsed, "stdout_tail": stdout_tail, "stderr_tail": stderr_tail}


def measure_wav(path: Path) -> dict:
    info = sf.info(str(path))
    audio, sr = sf.read(str(path), dtype="float64", always_2d=True)
    m = audio_metrics.measure_audio(audio, sr)
    return {
        "path": str(path),
        "samplerate": info.samplerate,
        "channels": info.channels,
        "subtype": info.subtype,
        "format": info.format,
        "duration_seconds": info.frames / info.samplerate,
        "integrated_lufs": m["integrated_lufs"],
        "loudness_backend": m["loudness_backend"],
        "lra_lu": m["lra_lu"],
        "sample_peak_dbfs": m["sample_peak_dbfs"],
        "true_peak_4x_dbtp": m["true_peak_4x_dbtp"],
        "crest_factor_db": m["crest_factor_db"],
        "short_term_lufs_p50": m["short_term_lufs"]["p50"],
        "short_term_lufs_max": m["short_term_lufs"]["max"],
        "clipping_samples": m["clipping_samples"],
    }


def read_sidecar(out_wav: Path) -> dict | None:
    sidecar = Path(str(out_wav) + ".mastering.json")
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except ValueError as exc:
        return {"_error": f"unparseable sidecar: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=None,
                        help="report/output dir (default: <repo parent>/_e2e_tmp/soren_capability_probe)")
    parser.add_argument("--soren-dir", default=str(DEFAULT_SOREN_DIR))
    parser.add_argument("--python", default=find_python())
    parser.add_argument("--input",
                        default=r"D:\_3.AI\audio_upscale\_e2e_tmp\inference_samples\set_2\input.wav")
    parser.add_argument("--reference", default=r"D:\_3.AI\audio_upscale\_e2e_tmp\pop_ref.wav")
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--runs", default=None,
                        help="comma-separated subset of run names to execute")
    args = parser.parse_args()

    soren_dir = Path(args.soren_dir).resolve()
    engine = soren_dir / "core_decrypted.py"
    if not engine.is_file():
        print(f"ERROR: engine not found at {engine}", file=sys.stderr)
        return 2
    model_dir = soren_dir / "model"
    model_ok = model_dir.is_dir() and any(model_dir.iterdir())
    input_wav = Path(args.input)
    reference_wav = Path(args.reference)

    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO_ROOT.parent / "_e2e_tmp" / "soren_capability_probe")
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = PROBE_MATRIX
    if args.runs:
        wanted = {n.strip() for n in args.runs.split(",")}
        selected = [r for r in PROBE_MATRIX if r["name"] in wanted]

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": str(engine),
        "python": args.python,
        "model_junction_resolves": bool(model_ok),
        "input": measure_wav(input_wav),
        "reference": measure_wav(reference_wav) if reference_wav.is_file() else None,
        "runs": [],
    }

    print(f"engine: {engine}")
    print(f"python: {args.python}   model ok: {model_ok}")
    print(f"input LUFS: {report['input']['integrated_lufs']:.2f}  "
          f"TP: {report['input']['true_peak_4x_dbtp']:.2f} dBTP  "
          f"sr: {report['input']['samplerate']} {report['input']['subtype']}")
    if report["reference"]:
        print(f"reference LUFS: {report['reference']['integrated_lufs']:.2f}  "
              f"TP: {report['reference']['true_peak_4x_dbtp']:.2f} dBTP")
        ref_lufs = report["reference"]["integrated_lufs"]
    else:
        ref_lufs = None

    for run in selected:
        name = run["name"]
        extra = list(run["args"])
        if "REF" in extra:
            extra[extra.index("REF")] = str(reference_wav)
        expect = run["expect_target_lufs"]
        if expect is None and ref_lufs is not None and run["name"].startswith("7_"):
            expect = ref_lufs + 0.0  # normal -> offset 0 applied to measured ref LUFS
        out_wav = out_dir / f"{name}_out.wav"
        if out_wav.exists():
            out_wav.unlink()
        sidecar_old = Path(str(out_wav) + ".mastering.json")
        if sidecar_old.exists():
            sidecar_old.unlink()

        print(f"\n=== {name}: {' '.join(extra)}")
        result = run_engine(args.python, soren_dir, input_wav, out_wav,
                            extra, args.timeout)
        entry = {"name": name, "args": extra, "note": run["note"],
                 "expect_target_lufs": expect, "engine_run": result}
        if result["ok"]:
            entry["output"] = measure_wav(out_wav)
            entry["mastering_sidecar"] = read_sidecar(out_wav)
            lu = entry["output"]["integrated_lufs"]
            tp = entry["output"]["true_peak_4x_dbtp"]
            print(f"    ok in {result['seconds']:.1f}s -> LUFS {lu:.2f}  "
                  f"TP {tp:.2f} dBTP  {entry['output']['subtype']}")
            if expect is not None:
                print(f"    target predicted {expect:.2f} LUFS, error {lu - expect:+.2f} LU")
        else:
            tail = (result["stderr_tail"] or "") + (result["stdout_tail"] or "")
            print(f"    FAILED rc={result['returncode']} after {result['seconds']:.1f}s")
            print("    tail: " + tail.strip().splitlines()[-1][:200]
                  if tail.strip() else "    (no output)")
        report["runs"].append(entry)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"soren_capability_probe_{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"\nreport written: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
