"""Run the user-provided preset on the six original WAVs without overwriting sources."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import studio_backend as backend

SOURCE = Path(r"D:\_4.Projects\_MY\中二病晚期患者")
PRESET = dict(quality=2, guidance=1.8, vocal_gain_db=0.0, balance_target_db=None, sub_db=4.0,
              punch_db=4.0, trans=0.4, sat=0.4, space_wet=0.8,
              space_denoise=0.2, space_width_db=6.0, genre="Pop",
              loudness="loud", eq_profile="Neutral", reference=None,
              device="cuda", bypass=())


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-label", default="StableEQ")
    args = parser.parse_args()
    if not args.output_label or any(c in args.output_label for c in '/\\:'):
        parser.error("output label must be a single directory-name component")
    files = sorted(p for p in SOURCE.glob("*.wav") if "_" not in p.stem)
    if len(files) != 6:
        raise RuntimeError(f"Expected six originals, found {files}")
    dest = SOURCE / ("ShadowBuster_截图预设_" + args.output_label + "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    dest.mkdir(exist_ok=False)
    report = {"preset": PRESET, "output": str(dest), "files": []}
    report_path = dest / "processing_report.json"
    def save():
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.environ["HF_HOME"] = str(ROOT / "packaging/stage/runtime_gpu/hf_home")
    os.environ["HF_HUB_OFFLINE"] = "1"
    backend.PYTHON = ROOT / "packaging/stage/runtime_gpu/env/python.exe"
    stream = backend._run_stream
    current_log = None
    def logged(cmd, cwd, **kwargs):
        try:
            result = stream(cmd, cwd, **kwargs)
            if current_log:
                with current_log.open("a", encoding="utf-8") as handle:
                    handle.write("\nCOMMAND: " + repr([str(c) for c in cmd]) + "\n" + str(result) + "\n")
            return result
        except Exception as exc:
            if current_log:
                with current_log.open("a", encoding="utf-8") as handle:
                    handle.write("\nFAILED: " + str(exc) + "\n")
            raise
    backend._run_stream = logged
    print("OUTPUT_DIRECTORY=" + str(dest), flush=True)
    for index, src in enumerate(files):
        item = {"source": str(src), "source_sha256": digest(src), "status": "processing"}
        report["files"].append(item)
        save()
        current_log = dest / (src.stem + ".log")
        work = dest / (".work_" + str(index))
        previous = None
        def progress(stage, fraction, label):
            nonlocal previous
            mark = (stage, int(fraction * 10), label)
            if mark != previous:
                print(f"[{index+1}/6] {src.name} | {stage} {fraction:.0%} {label}", flush=True)
                previous = mark
            # Pipeline removes its workspace when finished; preserve diagnostics first.
            if stage == 5 and fraction == 0:
                for path in work.glob("*.json"):
                    shutil.copyfile(path, dest / (src.stem + "." + path.name))
                    diagnostic = json.loads(path.read_text(encoding="utf-8"))
                    if diagnostic.get("algorithm"):
                        with current_log.open("a", encoding="utf-8") as handle:
                            handle.write("\nVOCAL_DIAGNOSTIC: " + json.dumps({key: diagnostic[key] for key in
                                ("mode", "algorithm", "fixed_vocal_mid_gain_db", "gain_max_step_db")}) + "\n")
        try:
            out = Path(backend.run_pipeline(src, dest, work_dir=work, progress=progress, **PRESET))
            data, sr = sf.read(out, always_2d=True)
            info = sf.info(out)
            original = sf.info(src)
            if not len(data) or not np.isfinite(data).all():
                raise RuntimeError("Invalid output samples")
            if abs(info.duration - original.duration) > 0.1:
                raise RuntimeError("Unexpected output duration difference")
            if digest(src) != item["source_sha256"]:
                raise RuntimeError("Source hash changed")
            log_text = current_log.read_text(encoding="utf-8")
            if backend.REFERENCE_MODE not in log_text:
                raise RuntimeError("New vocal algorithm missing from stage log")
            vocal_reports = []
            for path in dest.glob(src.stem + ".*.json"):
                diagnostic = json.loads(path.read_text(encoding="utf-8"))
                if diagnostic.get("algorithm") == backend.REFERENCE_MODE:
                    vocal_reports.append(str(path))
                    if diagnostic["gain_max_step_db"] != 0:
                        raise RuntimeError("Vocal gain was not constant")
            if not vocal_reports:
                raise RuntimeError("Missing saved stable vocal diagnostics")
            item.update(finite=True, source_unchanged=True, new_vocal_mode_verified=True,
                        vocal_reports=vocal_reports, original_duration_seconds=original.duration)
            item.update(status="completed", output=str(out), output_sha256=digest(out),
                        sample_rate=sr, channels=info.channels, subtype=info.subtype,
                        duration_seconds=info.duration, sample_peak=float(np.abs(data).max()))
            print("COMPLETED=" + str(out), flush=True)
        except Exception as exc:
            item.update(status="failed", error=str(exc))
            print("FAILED=" + src.name + " " + str(exc), flush=True)
        save()
    print("FINAL_REPORT=" + str(report_path), flush=True)
    if any(item["status"] != "completed" for item in report["files"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
