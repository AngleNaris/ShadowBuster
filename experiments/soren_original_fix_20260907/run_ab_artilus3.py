# -*- coding: utf-8 -*-
"""ARTILUS3.wav A/B：同一前置处理 → 旧 DSP baseline vs 修复后 DSP。

前置处理（Lew → Demucs → bass → drums，以及 REFERENCE 人声平衡所需参考）
只跑一次；两个分支唯一差异是 experiments/soren_original_fix_20260907/old_dsp
备份的旧 soundstage_reshape.py + 旧 soren_original.py（经同一 core_decrypted
适配器，old_soren_runtime 与线上 runtime_gpu 的 core_decrypted.py 逐字节一致）。

预设（experiments/run_requested_preset.py 同款）：
quality2 guidance1.8 vocal0 sub4 punch4 trans.4 sat.4 space_wet.8 denoise.2
width6 Pop loud Neutral cuda。
严禁切回旧 soren_core 母带链：baseline 仅作为测量分支在沙盒内运行，
正式链路仍是 styled soren_original（runtime_gpu/Soren_src，已同步修复版）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:/_3.AI/audio_upscale/SorenStudio")
EXP = ROOT / "experiments/soren_original_fix_20260907"
SRC = Path(r"D:/_4.Projects/_MY/造梦双子星2/ARTILUS3.wav")
RUNTIME_GPU = ROOT / "packaging/stage/runtime_gpu"

sys.path.insert(0, str(ROOT))
import studio_backend as backend  # noqa: E402

# ── 后端路径指向 GPU 打包 runtime（与安装版行为一致） ──────────────────────
backend.PYTHON = RUNTIME_GPU / "env/python.exe"
backend.APOLLO_DIR = RUNTIME_GPU / "Apollo"
backend.DSP_DIR = RUNTIME_GPU / "Apollo"
backend.SOREN_DIR = RUNTIME_GPU / "Soren_src"
os.environ["SB_FFMPEG"] = str(RUNTIME_GPU / "ffmpeg/bin/ffmpeg.exe")
os.environ["HF_HOME"] = str(RUNTIME_GPU / "hf_home")
os.environ["TORCH_HOME"] = str(RUNTIME_GPU / "torch_home")
os.environ["HF_HUB_OFFLINE"] = "1"

PRESET = dict(quality=2, guidance=1.8, vocal_gain_db=0.0, sub_db=4.0,
              punch_db=4.0, trans=0.4, sat=0.4, space_wet=0.8,
              space_denoise=0.2, space_width_db=6.0, genre="Pop",
              loudness="loud", eq_profile="Neutral", device="cuda")

STEM = SRC.stem
LOG = None


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if LOG is not None:
        with LOG.open("a", encoding="utf-8") as h:
            h.write(line + "\n")


def progress(idx, label):
    def inner(frac, text=""):
        log(f"{idx} {label} {frac:.0%} {text}")
    return inner


def main():
    out = EXP / "results" / ("ab_artilus3_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    shared = out / "shared"
    old = out / "old_dsp"
    fixed = out / "fixed_dsp"
    for d in (shared, old, fixed):
        d.mkdir(parents=True)
    LOG = out / "run.log"
    globals()["LOG"] = LOG

    assert SRC.exists(), SRC
    assert backend.PYTHON.exists(), backend.PYTHON
    report = {"source": str(SRC), "preset": PRESET, "output_dir": str(out),
              "started": datetime.now().isoformat()}

    # ── 共享前置处理：Lew → Demucs → bass → drums（+ 人声平衡参考） ──────────
    t0 = time.time()
    lew_out = shared / f"{STEM}_lew.wav"
    backend.stage_lew(SRC, lew_out, device=PRESET["device"], quality=PRESET["quality"],
                      guidance=PRESET["guidance"], progress=progress("1/6", "Lew"))
    backend.stage_demucs(lew_out, shared / "stems", progress=progress("2/6", "Demucs"))
    stem_dir = shared / "stems" / "htdemucs" / lew_out.stem

    bass_out = shared / f"{STEM}_bassmix.wav"
    bass_scale = backend.stage_bass(stem_dir, lew_out, bass_out,
                                    sub_db=PRESET["sub_db"], sat=PRESET["sat"],
                                    punch_db=0.0, trans=0.0, bass_gain_db=0.0,
                                    progress=progress("3/6", "Bass"),
                                    report_json=shared / "bass.json")
    drum_out = shared / f"{STEM}_drummix.wav"
    drums_scale = backend.stage_drums(stem_dir, bass_out, drum_out,
                                      punch_db=PRESET["punch_db"], trans=PRESET["trans"],
                                      progress=progress("4/6", "Drums"),
                                      report_json=shared / "drums.json")

    # REFERENCE 人声平衡参考（与 run_pipeline 完全一致的处理）
    original_mix = shared / "original_reference.wav"
    backend.ffmpeg_convert(SRC, original_mix, sr=44100)
    backend.stage_demucs(original_mix, shared / "original_reference_stems")
    original_vocals = (shared / "original_reference_stems" / "htdemucs"
                       / original_mix.stem / "vocals.wav")
    report["shared_seconds"] = round(time.time() - t0, 1)
    report["bass_scale"] = bass_scale
    report["drums_scale"] = drums_scale
    log(f"共享前置处理完成 {report['shared_seconds']}s bass_scale={bass_scale} "
        f"drums_scale={drums_scale}")

    vocal_scale_base = bass_scale * drums_scale

    # ── 旧 DSP 沙盒（old_soren_runtime + old_dsp 两份旧脚本） ────────────────
    old_soren = out / "old_runtime" / "Soren_src"
    old_apollo = out / "old_runtime" / "Apollo"
    old_soren.parent.mkdir(parents=True)
    shutil.copytree(EXP / "old_soren_runtime", old_soren)
    shutil.copyfile(EXP / "old_dsp" / "soren_original.py", old_soren / "soren_original.py")
    old_apollo.mkdir()
    for name in ("soundstage_reshape.py", "audio_validation.py", "stage_metadata.py"):
        shutil.copyfile(EXP / "old_dsp" / name if name == "soundstage_reshape.py"
                        else backend.DSP_DIR / name, old_apollo / name)

    def run_branch(tag, branch_dir, reshape_script, soren_dir, use_backend_reshape):
        shape_out = branch_dir / f"{STEM}_shapemix.wav"
        vocal_out = branch_dir / f"{STEM}_vocalmix.wav"
        final_out = out / f"ARTILUS3_{tag}_shadowbuster.wav"
        t = time.time()
        if use_backend_reshape:
            reshape_scale = backend.stage_reshape(
                drum_out, stem_dir, shape_out, wet=PRESET["space_wet"],
                denoise=PRESET["space_denoise"], width_db=PRESET["space_width_db"],
                progress=progress("5/6", f"声场重塑[{tag}]"),
                report_json=branch_dir / "reshape.json")
        else:
            cmd = [str(backend.PYTHON), str(reshape_script),
                   "--in-mix", str(drum_out), "--out-wav", str(shape_out),
                   "--stems-dir", str(stem_dir), "--mode", "broadband",
                   "--wet", str(PRESET["space_wet"]),
                   "--side-gain-db", str(PRESET["space_width_db"]),
                   "--other-denoise-amount", str(PRESET["space_denoise"]),
                   "--report-json", str(branch_dir / "reshape.json")]
            proc = subprocess.run(cmd, cwd=str(old_apollo), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeError(f"旧声场重塑失败:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
            reshape_scale = backend.read_report(branch_dir / "reshape.json", stage="reshape",
                                                input_path=drum_out, output_path=shape_out)
            (branch_dir / "reshape_console.log").write_text(proc.stdout, encoding="utf-8")
        log(f"[{tag}] reshape scale={reshape_scale} 用时 {time.time()-t:.0f}s")

        backend.stage_vocals(stem_dir, shape_out, vocal_out,
                             gain_db=PRESET["vocal_gain_db"],
                             balance_mode=backend.REFERENCE_MODE,
                             reference_mix=original_mix,
                             reference_vocals=original_vocals,
                             vocal_scale=vocal_scale_base * reshape_scale,
                             progress=progress("5/6", f"人声[{tag}]"))

        env = os.environ.copy()
        env["PYTHONPATH"] = str(soren_dir)
        cmd = [str(backend.PYTHON), str(soren_dir / "core_decrypted.py"),
               str(vocal_out), str(final_out),
               "--loudness", PRESET["loudness"], "--eq-profile", PRESET["eq_profile"],
               "--genre", PRESET["genre"]]
        t = time.time()
        proc = subprocess.run(cmd, cwd=str(soren_dir), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        (branch_dir / "soren_console.log").write_text(
            (proc.stdout or "") + "\n=====STDERR=====\n" + (proc.stderr or ""),
            encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"[{tag}] Soren 母带失败:\n{(proc.stderr or proc.stdout)[-3000:]}")
        log(f"[{tag}] soren 完成 用时 {time.time()-t:.0f}s -> {final_out}")
        return {"final": str(final_out), "vocalmix": str(vocal_out),
                "shapemix": str(shape_out), "reshape_scale": reshape_scale}

    report["old"] = run_branch("OLD", old, old_apollo / "soundstage_reshape.py",
                               old_soren, use_backend_reshape=False)
    report["fixed"] = run_branch("FIXED", fixed, backend.DSP_DIR / "soundstage_reshape.py",
                                 backend.SOREN_DIR, use_backend_reshape=True)

    report["finished"] = datetime.now().isoformat()
    (out / "ab_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    print("FINAL_REPORT=" + str(out / "ab_report.json"), flush=True)


if __name__ == "__main__":
    main()
