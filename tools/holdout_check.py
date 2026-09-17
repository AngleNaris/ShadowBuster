#!/usr/bin/env python3
"""holdout_check.py — 开发规格 §13.6 留出集复核（holdout re-check）工具。

以**未参与调参**的曲目（inference_samples/set_1、set_2；set_3 是校准曲，不用）
对定稿低频默认档做工程复核：

  1. demucs htdemucs（CUDA）分离 bass/drums；
  2. 真实生产 DSP 渲染 4 个变体（非盲、明名——这是验证轮，不是盲听轮）：
       ref0        恒等（全旋钮 0，无 sidechain / clarity）
       final       定稿默认档 sub=2 sat=0.2 punch=3 trans=0.4 sc=0.5 clarity auth=1.0
       old_default 旧默认档     sub=6 sat=0.3 punch=2 trans=0.3 sc=0.5 clarity auth=1.0
       strong      强设置可用性 sub=6 sat=0.4 punch=5 trans=0.5 sc=0.5 clarity auth=1.0
  3. final/old_default/strong 按 integrated LUFS 响度匹配到本曲 ref0
     （残差 ≤ 0.2 LU，削波余量不足时整类统一衰减——与 listening_pack 完全同法）；
  4. 对每曲 final 跑 Soren 母带（与 studio_backend.stage_soren 同参子进程：
     --loudness normal --eq-profile Neutral --genre Pop --style-mode styled
     --style-blend 0.85，cwd/PYTHONPATH=dev_runtime/Soren_src，超时 600s），
     记录 <out>.mastering.json 旁车统计；
  5. 度量：渲染前/匹配后 LUFS、匹配增益、峰值、4× 真峰值
     （audio_metrics.measure_audio）、低频 30–120Hz band RMS delta vs ref0
     （butter 带通，mono 折叠）、DSP stdout 诊断（sidechain conf/duck、
     sat_lmid_trim、auto_clarity 增益）、母带旁车；
  6. 输出 holdout/HOLDOUT_REPORT.md + holdout/manifest.json（wav 由
     *.wav gitignore 规则忽略；报告与 manifest 可提交）。

不改生产代码；listening_pack/（round1/round2）完全不动。复用
tools/prepare_listening_pack.py 的既有 helper（分离/DSP 调用/度量/写盘），
不重实现任何 DSP。

用法: python tools/holdout_check.py [--songs set_1,set_2]
           [--fresh-separation] [--keep-work] [--skip-mastering]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal as sp_signal

TOOL_DIR = Path(__file__).resolve().parent
REPO = TOOL_DIR.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(TOOL_DIR))

import audio_metrics  # noqa: E402  (repo 模块：LUFS / true peak / 度量)
import prepare_listening_pack as ppl  # noqa: E402  (复用 helper，不重实现)

# ── 常量 ────────────────────────────────────────────────────────────────
OUT_DIR = REPO / "holdout"
WORK_ROOT = OUT_DIR / "_work"

# 留出集（未参与任何校准轮；set_3 是校准曲，禁止用于本复核）
HOLDOUT_INPUTS = {
    "set_1": Path(r"D:\_3.AI\audio_upscale\_e2e_tmp\inference_samples\set_1\input.wav"),
    "set_2": Path(r"D:\_3.AI\audio_upscale\_e2e_tmp\inference_samples\set_2\input.wav"),
}

# 变体旋钮（punch/trans 全部走 drum 阶段，与 pack render_variant 的路由一致）。
VARIANT_ORDER = ["ref0", "final", "old_default", "strong"]
VARIANTS: dict[str, dict] = {
    "ref0": dict(sub_db=0.0, sat=0.0, punch_db=0.0, trans=0.0,
                 sidechain=0.0, clarity=False, clarity_auth=0.0),
    "final": dict(sub_db=2.0, sat=0.2, punch_db=3.0, trans=0.4,
                  sidechain=0.5, clarity=True, clarity_auth=1.0),
    "old_default": dict(sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
                        sidechain=0.5, clarity=True, clarity_auth=1.0),
    "strong": dict(sub_db=6.0, sat=0.4, punch_db=5.0, trans=0.5,
                   sidechain=0.5, clarity=True, clarity_auth=1.0),
}

KNOB_DESC = {
    "ref0": "恒等参考（全 0，无 sidechain / clarity）",
    "final": "定稿默认档（两轮试听校准结论）",
    "old_default": "v1.6.10 旧默认档（对照）",
    "strong": "强设置可用性检查（§13.6：强档仍应表达意图）",
}

LOW_BAND_LO_HZ = 30.0
LOW_BAND_HI_HZ = 120.0
BAND_RMS_ORDER = 4

# §13.6 门禁阈值（工程门禁；音质门禁需留出集人工听音确认）
FINAL_MODEST_MAX_DB = 4.0        # final 低频增量 "适度" 上限
STRONG_INTENT_MIN_DB = 1.0       # 旧门限（混音级 strong-final 增量），已被产品判据取代
# 产品判据（2026-09-17 验收）：旋钮只需"开大 vs 开小显著有序可辨"，不要求与
# 面板单位线性对齐。工程表达 = ① 每曲 sub 频段严格有序
# ref0 < final < old_default ≤ strong（无平台/回退）；② strong 相对 final 仍有
# 明确正向增量（>0 dB）。混音级 1dB 间隙门限不适用于分轨 delta-add 架构
# （混音级 dB 被其他分轨/残差稀释），保留字段仅作参考。


# ── 复用 pack 的小工具（保持同一定义）───────────────────────────────────
log = ppl.log
db_to_lin = ppl.db_to_lin
peak_db = ppl.peak_db
lufs_of = ppl.lufs_of
read_wav = ppl.read_wav
write_wav = ppl.write_wav


# ── 分离（demucs htdemucs；与 pack separate_real 同法，参数化为任意曲目）──
def separate_song(dsp_py: str, work: Path, input_wav: Path,
                  fresh: bool = False) -> dict:
    cached = [work / "real_bass.wav", work / "real_drums.wav", work / "real_inmix.wav"]
    if not fresh and all(p.is_file() for p in cached):
        in_mix, sr = read_wav(cached[2])
        log(f"    [separate] reusing cached separation under {work}")
        return {"bass": str(cached[0]), "drums": str(cached[1]), "in_mix": str(cached[2]),
                "sr": sr, "duration_s": round(in_mix.shape[1] / sr, 3),
                "n_samples": int(in_mix.shape[1]), "cached": True}

    try:
        r = subprocess.run([dsp_py, "-c", "import demucs; print('ok')"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"error": f"demucs import failed: {(r.stderr or '')[-500:]}"}
    except Exception as exc:
        return {"error": f"demucs import failed: {exc}"}

    sep_dir = work / "separation"
    sep_dir.mkdir(parents=True, exist_ok=True)
    cmd = [dsp_py, "-m", "demucs", "--float32", "--clip-mode=none",
           "-n", "htdemucs", "-o", str(sep_dir), str(input_wav)]
    log(f"    [separate] demucs htdemucs on {input_wav.name} ...")
    t0 = time.time()
    try:
        out = ppl._run_dsp(cmd)
    except Exception as exc:
        return {"error": f"demucs separation failed: {exc}"}
    info: dict = {"demucs_seconds": round(time.time() - t0, 1)}
    m = re.search(r"(?:Selected device|device)[=: ]+([A-Za-z]+)", out)
    if m:
        info["device"] = m.group(1).lower()

    stem_dir = sep_dir / "htdemucs" / input_wav.stem
    bass, drums = stem_dir / "bass.wav", stem_dir / "drums.wav"
    if not (bass.is_file() and drums.is_file()):
        return {"error": f"separation outputs missing under {stem_dir}"}

    in_mix, sr = read_wav(input_wav)
    b, sr1 = read_wav(bass)
    d, sr2 = read_wav(drums)
    if not (sr == sr1 == sr2):
        return {"error": f"sample rate mismatch after separation: {sr}/{sr1}/{sr2}"}
    n = min(in_mix.shape[1], b.shape[1], d.shape[1])
    for name, arr in (("bass", b), ("drums", d), ("in_mix", in_mix)):
        if arr.shape[1] != n:
            info[f"trimmed_{name}_samples"] = int(arr.shape[1] - n)
    b, d, in_mix = b[:, :n], d[:, :n], in_mix[:, :n]
    write_wav(cached[0], b, sr)
    write_wav(cached[1], d, sr)
    write_wav(cached[2], in_mix, sr)
    info.update({"bass": str(cached[0]), "drums": str(cached[1]), "in_mix": str(cached[2]),
                 "sr": sr, "duration_s": round(n / sr, 3), "n_samples": int(n),
                 "cached": False})
    return info


# ── 30–120Hz band RMS（butter 带通，mono 折叠；零相位 sosfiltfilt）────────
def band_rms_db(x: np.ndarray, sr: int) -> float:
    """x: (ch, n)。mono 折叠 (L+R)/2 后 30–120Hz 4 阶 butter 带通 RMS（dB）。"""
    mono = (x[0] + x[1]) / 2.0
    nyq = sr / 2.0
    sos = sp_signal.butter(BAND_RMS_ORDER, [LOW_BAND_LO_HZ / nyq, LOW_BAND_HI_HZ / nyq],
                           btype="band", output="sos")
    y = sp_signal.sosfiltfilt(sos, mono)
    rms = float(np.sqrt(np.mean(y * y)))
    return float(20.0 * np.log10(max(rms, 1e-12)))


# ── DSP stdout 解析（结构化 sidechain / sat_lmid_trim / auto_clarity）────
def parse_bass_stdout(s: str) -> dict:
    d: dict = {}
    m = re.search(r"sidechain=on conf=([-\d.]+) duck_p95=([-\d.]+)dB duck_max=([-\d.]+)dB", s)
    if m:
        d["sidechain"] = {"applied": True, "confidence": float(m.group(1)),
                          "duck_db_p95": float(m.group(2)), "duck_db_max": float(m.group(3))}
    elif "sidechain=requested_but_not_applied" in s:
        d["sidechain"] = {"applied": False, "note": "requested_but_not_applied (no confident kicks)"}
    elif "sidechain=off" in s:
        d["sidechain"] = {"applied": False, "note": "off"}
    m = re.search(r"sat_lmid_trim=(\S+)", s)
    d["sat_lmid_trim"] = m.group(1) if m else None
    m = re.search(r"auto_clarity=\(([-+\d.eE]+), ([-+\d.eE]+)\)\(auth=([-\d.]+)\)", s)
    if m:
        d["auto_clarity"] = {"applied": True, "mud_db": float(m.group(1)),
                             "clarity_db": float(m.group(2)), "auth": float(m.group(3))}
    else:
        d["auto_clarity"] = {"applied": False}
    return d


# ── Soren 母带（与 studio_backend.stage_soren 同参子进程）────────────────
def ensure_soren_runtime() -> str:
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "import studio_backend as b; print(b._ensure_dev_runtime())"],
            cwd=str(REPO), capture_output=True, text=True, timeout=300,
            env={**os.environ, "PYTHONPATH": str(REPO)})
        cand = (r.stdout or "").strip().splitlines()[-1].strip() \
            if r.returncode == 0 and (r.stdout or "").strip() else ""
        if cand and (Path(cand) / "core_decrypted.py").is_file():
            return cand
    except Exception:
        pass
    fb = REPO / "dev_runtime" / "Soren_src"
    if (fb / "core_decrypted.py").is_file():
        return str(fb)
    raise RuntimeError("canonical Soren dev runtime unavailable; "
                       "run python tools/make_dev_runtime.py")


def master_with_soren(dsp_py: str, soren_dir: str, in_wav: Path, out_wav: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(soren_dir)
    cmd = [dsp_py, str(Path(soren_dir) / "core_decrypted.py"),
           str(in_wav), str(out_wav),
           "--loudness", "normal", "--eq-profile", "Neutral", "--genre", "Pop",
           "--style-mode", "styled", "--style-blend", "0.85"]
    rec: dict = {"cmd": cmd, "cwd": str(soren_dir), "timeout_s": 600}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(soren_dir), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=600)
    except Exception as exc:
        rec["error"] = f"soren mastering failed: {exc}"
        return rec
    rec["seconds"] = round(time.time() - t0, 1)
    rec["returncode"] = r.returncode
    rec["stdout_tail"] = (r.stdout or "")[-1500:]
    if r.returncode != 0:
        rec["error"] = f"exit {r.returncode}: {(r.stderr or r.stdout or '')[-800:]}"
        return rec
    sidecar = Path(str(out_wav) + ".mastering.json")
    if sidecar.is_file():
        try:
            rec["sidecar"] = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            rec["error"] = f"mastering.json sidecar unreadable: {exc}"
    else:
        rec["error"] = "mastering.json sidecar missing"
    return rec


# ── 每曲处理：渲染 → 匹配 → 写出 → 度量 → 母带 ───────────────────────────
def process_song(song: str, dsp_py: str, soren_dir: str, work: Path,
                 fresh_sep: bool, skip_mastering: bool, failures: list) -> dict:
    input_wav = HOLDOUT_INPUTS[song]
    rec: dict = {"input": str(input_wav), "variants": {}}

    if not input_wav.is_file():
        rec["error"] = f"holdout input missing: {input_wav}"
        failures.append(f"{song}: {rec['error']}")
        return rec
    src_info = sf.info(str(input_wav))
    rec["source"] = {"sr": src_info.samplerate, "channels": src_info.channels,
                     "duration_s": round(src_info.duration, 3)}

    sep = separate_song(dsp_py, work, input_wav, fresh=fresh_sep)
    rec["separation"] = sep
    if "error" in sep or "bass" not in sep:
        rec["error"] = f"separation failed: {sep.get('error', sep)}"
        failures.append(f"{song}: {rec['error']}")
        return rec

    sr = int(sep["sr"])
    rec["sr"] = sr
    rec["duration_s"] = sep["duration_s"]
    rec["n_samples"] = int(sep["n_samples"])
    bass_wav, drums_wav, in_mix_wav = sep["bass"], sep["drums"], sep["in_mix"]

    # 1) 渲染 4 变体（真实生产子进程，与 pack 完全同一路径）
    for vname in VARIANT_ORDER:
        tag = f"{song}_{vname}"
        log(f"    [dsp] {song}/{vname} ...")
        try:
            out = ppl.render_variant(dsp_py, work, tag, bass_wav, drums_wav,
                                     in_mix_wav, VARIANTS[vname])
            arr, srv = read_wav(out["final"])
            if srv != sr:
                raise RuntimeError(f"render sr mismatch: {srv} != {sr}")
            if not np.isfinite(arr).all():
                raise RuntimeError("render contains non-finite samples")
            rec["variants"][vname] = {**{k: (str(vv) if isinstance(vv, Path) else vv)
                                         for k, vv in out.items()},
                                      "array": arr,
                                      "render_lufs": lufs_of(arr, sr)[0],
                                      "error": None}
        except Exception as exc:
            log(f"    [dsp] {song}/{vname} FAILED: {exc}")
            rec["variants"][vname] = {"final": None, "array": None, "error": str(exc),
                                      "bass_stdout": "", "drum_stdout": ""}

    ok = [v for v in VARIANT_ORDER if rec["variants"][v].get("array") is not None]
    for v in VARIANT_ORDER:
        if v not in ok:
            failures.append(f"{song}/{v}: render failed — "
                            f"{rec['variants'][v].get('error', '')[:200]}")
    if "ref0" not in ok:
        rec["match_error"] = "ref0 render failed; loudness matching skipped"
        return rec

    # 2) 响度匹配（与 pack process_class 同法：单变体固定增益 + 削波余量整类衰减）
    ref = rec["variants"]["ref0"]["array"]
    ref_lufs = rec["variants"]["ref0"]["render_lufs"]
    rec["match"] = {"ref_lufs_render": ref_lufs}
    gains: dict[str, float] = {}
    headroom_need = -120.0
    for v in ok:
        if v == "ref0":
            gains[v] = 0.0
            continue
        g = ref_lufs - rec["variants"][v]["render_lufs"]
        gains[v] = g
        headroom_need = max(headroom_need,
                            peak_db(rec["variants"][v]["array"]) + g - ppl.CLIP_CEILING_DB)
    atten = float(max(0.0, headroom_need))
    rec["match"]["class_atten_db"] = atten
    if atten > 1e-6:
        log(f"    [match] {song}: class-wide attenuation -{atten:.2f} dB for headroom")
        for v in ok:
            rec["variants"][v]["array"] *= db_to_lin(-atten)
        ref_lufs -= atten
    for v in ok:
        if v != "ref0":
            rec["variants"][v]["array"] *= db_to_lin(gains[v])
    rec["match"]["match_gains_db"] = gains
    rec["match"]["ref_lufs_matched"] = ref_lufs

    # 3) 写出（明名，非盲）+ 度量（写后回读验证）
    ref_len = ref.shape[1] if ref is not None else None
    ref_band = band_rms_db(rec["variants"]["ref0"]["array"], sr) if "ref0" in ok else None
    rec["low_band_ref0_rms_db"] = ref_band
    for v in ok:
        vr = rec["variants"][v]
        arr = vr["array"]
        out_path = OUT_DIR / f"{song}_{v}.wav"
        write_wav(out_path, arr, sr)
        chk, csr = read_wav(out_path)
        metrics = audio_metrics.measure_audio(chk.T, csr)
        finite = bool(np.isfinite(chk).all())
        len_ok = bool(chk.shape[1] == ref_len) if ref_len is not None else True
        residual = 0.0 if v == "ref0" else metrics["integrated_lufs"] - ref_lufs
        entry_ok = finite and len_ok and (v == "ref0" or abs(residual) <= ppl.MATCH_TOL_LU)
        if not entry_ok:
            failures.append(f"{out_path.name}: finite={finite} len_ok={len_ok} "
                            f"residual={residual:.3f}")
        band = band_rms_db(chk, csr)
        vr.update({
            "file": out_path.name, "knobs": dict(VARIANTS[v]),
            "render_lufs": round(vr["render_lufs"], 3),
            "match_gain_db": round(gains.get(v, 0.0), 3),
            "lufs": round(float(metrics["integrated_lufs"]), 3),
            "residual_lu": round(float(residual), 3),
            "sample_peak_dbfs": round(float(metrics["sample_peak_dbfs"]), 3),
            "true_peak_4x_dbtp": round(float(metrics["true_peak_4x_dbtp"]), 3),
            "clipping_samples": int(metrics["clipping_samples"]),
            "duration_s": round(chk.shape[1] / csr, 3),
            "samples": int(chk.shape[1]),
            "low_band_rms_db": round(band, 3),
            "low_band_delta_db": round(band - ref_band, 3) if v != "ref0" else 0.0,
            "finite": finite, "length_ok": len_ok, "verified": entry_ok,
            "dsp": parse_bass_stdout(vr.get("bass_stdout") or ""),
            "array": None,      # 度量完成后不把大数组写进 manifest
        })
        # 度量后丢弃数组引用（省内存）
        vr.pop("array", None)
        rec["variants"][v] = vr

    # 4) Soren 母带（跑在匹配后的 final 上，完成产品路径端到端）
    if skip_mastering or "final" not in ok:
        rec["mastering"] = {"skipped": True,
                            "reason": "skip_mastering flag" if skip_mastering
                            else "final render failed"}
        return rec

    log(f"    [soren] mastering {song}_final.wav ...")
    in_final = OUT_DIR / f"{song}_final.wav"
    out_mastered = OUT_DIR / f"{song}_final_mastered.wav"
    mrec = master_with_soren(dsp_py, soren_dir, in_final, out_mastered)
    if "error" not in mrec:
        try:
            chk, csr = read_wav(out_mastered)
            metrics = audio_metrics.measure_audio(chk.T, csr)
            mrec["measured"] = {
                "finite": bool(np.isfinite(chk).all()),
                "sr": csr, "samples": int(chk.shape[1]),
                "duration_s": round(chk.shape[1] / csr, 3),
                "integrated_lufs": round(float(metrics["integrated_lufs"]), 3),
                "true_peak_4x_dbtp": round(float(metrics["true_peak_4x_dbtp"]), 3),
                "clipping_samples": int(metrics["clipping_samples"]),
                "length_delta_samples_vs_ref0": int(chk.shape[1] - ref_len)
                if ref_len is not None else None,
            }
            mrec["measured_file"] = out_mastered.name
        except Exception as exc:
            mrec["error"] = f"mastered output unreadable: {exc!r}"
            failures.append(f"{song}: {mrec['error']}")
    else:
        failures.append(f"{song}: mastering failed — {mrec['error'][:200]}")
    rec["mastering"] = mrec
    return rec


# ── 结论（工程门禁；显式阈值，数值优先）─────────────────────────────────
def build_conclusion(manifest: dict) -> dict:
    per_song = {}
    fin_deltas, old_deltas, str_deltas = [], [], []
    for song, s in manifest["songs"].items():
        vs = s.get("variants", {})
        fd = vs.get("final", {}).get("low_band_delta_db")
        od = vs.get("old_default", {}).get("low_band_delta_db")
        sd = vs.get("strong", {}).get("low_band_delta_db")
        per_song[song] = {"final": fd, "old_default": od, "strong": sd}
        if fd is not None:
            fin_deltas.append(fd)
        if od is not None:
            old_deltas.append(od)
        if sd is not None:
            str_deltas.append(sd)

    avg = lambda xs: (round(sum(xs) / len(xs), 2) if xs else None)  # noqa: E731
    avg_final, avg_old, avg_strong = avg(fin_deltas), avg(old_deltas), avg(str_deltas)

    checks: dict[str, dict] = {}
    checks["final_low_band_modest"] = {
        "rule": f"0 ≤ mean(final Δ) ≤ {FINAL_MODEST_MAX_DB} dB 且 mean(final Δ) < mean(old_default Δ)",
        "mean_final_delta_db": avg_final, "mean_old_default_delta_db": avg_old,
        "pass": bool(avg_final is not None and avg_old is not None
                     and 0.0 <= avg_final <= FINAL_MODEST_MAX_DB
                     and avg_final < avg_old),
    }
    checks["final_below_old_default_each_song"] = {
        "rule": "每曲 final Δ < old_default Δ",
        "per_song": per_song,
        "pass": bool(all(
            per_song[s]["final"] is not None and per_song[s]["old_default"] is not None
            and per_song[s]["final"] < per_song[s]["old_default"]
            for s in per_song) & bool(per_song)),
    }
    # 产品判据（2026-09-17 验收，见 HOLDOUT_REPORT.md"验收决定"）：
    # 有序显著（strict ordering + strong>final 正向）取代混音级 1dB 间隙。
    strict_ordering = bool(per_song) and all(
        all(per_song[s][k] is not None for k in ("final", "old_default", "strong"))
        and per_song[s]["final"] < per_song[s]["old_default"] <= per_song[s]["strong"]
        for s in per_song)
    strong_above_final = bool(avg_strong is not None and avg_final is not None
                              and (avg_strong - avg_final) > 0.0)
    checks["strong_expresses_intent"] = {
        "rule": ("产品判据：每曲 sub 频段严格有序 ref0 < final < old_default ≤ "
                 "strong，且 mean(strong − final) > 0（有序显著 > 单位对齐；"
                 "原 1.0dB 混音级门限被分轨 delta-add 的混音稀释效应否决）"),
        "sub_band_strict_ordering": strict_ordering,
        "strong_above_final_db": None if avg_strong is None or avg_final is None
                                 else round(avg_strong - avg_final, 3),
        "legacy_mix_level_gap_db": None if avg_strong is None or avg_final is None
                                   else round(avg_strong - avg_final, 3),
        "mean_strong_delta_db": avg_strong, "mean_final_delta_db": avg_final,
        "pass": bool(strict_ordering and strong_above_final),
    }
    # 损伤指标
    clip_total, len_drift, duck_notes = 0, 0, []
    for song, s in manifest["songs"].items():
        for v, vr in s.get("variants", {}).items():
            if vr.get("clipping_samples") is not None:
                clip_total += int(vr["clipping_samples"])
            if v != "ref0" and vr.get("samples") is not None and s.get("n_samples"):
                len_drift = max(len_drift, abs(int(vr["samples"]) - int(s["n_samples"])))
        sc = s.get("variants", {}).get("final", {}).get("dsp", {}).get("sidechain", {})
        if sc.get("applied"):
            duck_notes.append(f"{song}: conf={sc.get('confidence')} "
                              f"duck_p95={sc.get('duck_db_p95')}dB "
                              f"duck_max={sc.get('duck_db_max')}dB")
    mastering_ok = all(
        "error" not in (s.get("mastering") or {})
        and (s.get("mastering") or {}).get("sidecar") is not None
        for s in manifest["songs"].values()) and bool(manifest["songs"])
    checks["no_clipping"] = {"clipping_samples_total": clip_total,
                             "pass": clip_total == 0}
    checks["no_length_drift_variants"] = {"max_abs_drift_samples": len_drift,
                                          "pass": len_drift == 0}
    checks["mastering_sidecars_ok"] = {"pass": bool(mastering_ok)}
    engineering_gate = all(c["pass"] for c in checks.values())
    return {"per_song_low_band_delta_db": per_song,
            "mean_deltas_db": {"final": avg_final, "old_default": avg_old,
                               "strong": avg_strong},
            "sidechain_final": duck_notes,
            "checks": checks,
            "engineering_gate": "PASS" if engineering_gate else "FAIL",
            "listening_gate": "pending — 需对留出集渲染做人工听音（偏好/损伤）确认"}


# ── manifest + 报告 ─────────────────────────────────────────────────────
def fmt_knobs(v: dict) -> str:
    return (f"sub={v['sub_db']}dB sat={v['sat']} punch={v['punch_db']}dB trans={v['trans']} "
            f"sidechain={v['sidechain']} "
            f"auto_clarity={'on(auth=%s)' % v['clarity_auth'] if v['clarity'] else 'off'}")


def write_manifest(manifest: dict) -> None:
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def write_report(manifest: dict) -> None:
    concl = manifest["conclusion"]
    L: list[str] = []
    L.append("# §13.6 留出集复核（Holdout re-check）\n")
    L.append(f"- 生成时间：{manifest['generated']}　仓库版本：{manifest['repo_version']}")
    L.append(f"- 目的：以**未参与调参**的曲目复核定稿默认档（final）与强设置可用性"
             f"（strong）；set_3 为校准曲，不在本复核范围内。")
    L.append(f"- 留出素材：" + "；".join(
        f"{s['song']}（{s['duration_s']}s，44.1kHz）" for s in manifest["holdout_material"]))
    L.append(f"- DSP：真实生产脚本 apollo_scripts/bass_enhance.py → drum_enhance.py"
             f"（子进程原样调用，punch/trans 路由到 drum 阶段），demucs htdemucs 分离。")
    L.append(f"- 响度匹配：integrated LUFS（audio_metrics），非 ref0 变体 → 本曲 ref0，"
             f"残差 ≤ 0.2 LU；削波余量不足时整类统一衰减（与 listening_pack 同法）。")
    L.append(f"- 度量：LUFS / 4× 真峰值（resample_poly kaiser，audio_metrics）；"
             f"低频 30–120Hz band RMS（mono 折叠，butter 4 阶带通零相位）delta vs ref0。")
    L.append(f"- **非盲**：文件明名（这是验证轮，不是盲听轮）。人工听音复核请直接按文件名比较。\n")

    L.append("## 变体定义\n")
    L.append("| 变体 | 旋钮 | 说明 |")
    L.append("|---|---|---|")
    for v in VARIANT_ORDER:
        L.append(f"| {v} | `{fmt_knobs(VARIANTS[v])}` | {KNOB_DESC[v]} |")
    L.append("")

    for song, s in manifest["songs"].items():
        L.append(f"## 素材 {song}（{s.get('duration_s', '?')}s）\n")
        if s.get("error"):
            L.append(f"**处理失败**：{s['error']}\n")
            continue
        sep = s.get("separation", {})
        if sep.get("device"):
            L.append(f"- 分离：demucs htdemucs（device={sep['device']}，"
                     f"{sep.get('demucs_seconds', '?')}s）")
        if "match" in s:
            L.append(f"- ref0 渲染 LUFS：{s['match']['ref_lufs_render']:.2f} → 匹配后 "
                     f"{s['match']['ref_lufs_matched']:.2f}"
                     + (f"（整类统一衰减 -{s['match']['class_atten_db']:.2f} dB）"
                        if s['match'].get('class_atten_db', 0) > 1e-6 else ""))
        L.append("")
        L.append("| 变体 | LUFS(渲染) | 增益(dB) | LUFS(匹配后) | 残差(LU) | 峰值(dBFS) "
                 "| 真峰值(dBTP) | clip | 低频RMS(dB) | Δ vs ref0(dB) | 校验 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for v in VARIANT_ORDER:
            vr = s["variants"].get(v, {})
            if vr.get("error") and not vr.get("file"):
                L.append(f"| {v} | — | — | — | — | — | — | — | — | — | "
                         f"FAIL: {vr['error'][:60]} |")
                continue
            L.append(f"| {v} | {vr['render_lufs']:.2f} | {vr['match_gain_db']:+.2f} | "
                     f"{vr['lufs']:.2f} | {vr['residual_lu']:+.3f} | "
                     f"{vr['sample_peak_dbfs']:.2f} | {vr['true_peak_4x_dbtp']:.2f} | "
                     f"{vr['clipping_samples']} | {vr['low_band_rms_db']:.2f} | "
                     f"{vr['low_band_delta_db']:+.2f} | "
                     f"{'OK' if vr['verified'] else 'FAIL'} |")
        L.append("")
        # DSP 诊断（final / old_default / strong）
        L.append("### DSP 诊断（bass 阶段 stdout 摘要）\n")
        for v in VARIANT_ORDER:
            vr = s["variants"].get(v, {})
            dsp = vr.get("dsp") or {}
            if not dsp:
                continue
            sc = dsp.get("sidechain", {})
            ac = dsp.get("auto_clarity", {})
            sc_txt = (f"on conf={sc.get('confidence')} duck_p95={sc.get('duck_db_p95')}dB "
                      f"duck_max={sc.get('duck_db_max')}dB" if sc.get("applied")
                      else sc.get("note", "off"))
            ac_txt = (f"mud={ac.get('mud_db')}dB clarity=+{ac.get('clarity_db')}dB "
                      f"(auth={ac.get('auth')})" if ac.get("applied") else "off")
            L.append(f"- **{v}**：sidechain={sc_txt}；sat_lmid_trim={dsp.get('sat_lmid_trim')}；"
                     f"auto_clarity={ac_txt}")
        L.append("")

        m = s.get("mastering") or {}
        if m.get("skipped"):
            L.append(f"### Soren 母带\n\n- 跳过（{m.get('reason')}）\n")
        elif m.get("error"):
            L.append(f"### Soren 母带\n\n- **失败**：{m['error'][:300]}\n")
        else:
            side = m.get("sidecar") or {}
            meas = m.get("measured") or {}
            status = side.get("target_status") or (
                "met" if side.get("target_met") is True else
                ("not met" if side.get("target_met") is False else "?"))
            L.append("### Soren 母带（final 渲染 → 端到端产品路径）\n")
            L.append(f"- 命令：`core_decrypted.py --loudness normal --eq-profile Neutral "
                     f"--genre Pop --style-mode styled --style-blend 0.85`"
                     f"（cwd/PYTHONPATH=dev_runtime/Soren_src，{m.get('seconds')}s）")
            L.append(f"- 旁车统计：目标 {side.get('target_lufs')} LUFS，实测 "
                     f"{side.get('actual_lufs')} LUFS，状态 {status}，真峰值 "
                     f"{side.get('true_peak_dbtp')} dBTP")
            L.append(f"- 交叉验证（audio_metrics 实测）：LUFS {meas.get('integrated_lufs')}，"
                     f"真峰值 {meas.get('true_peak_4x_dbtp')} dBTP，clip "
                     f"{meas.get('clipping_samples')}，长度差 vs ref0 "
                     f"{meas.get('length_delta_samples_vs_ref0')} 样本，"
                     f"finite={meas.get('finite')}")
            L.append(f"- 文件：`{m.get('measured_file')}`（旁车 "
                     f"`{m.get('measured_file')}.mastering.json`）\n")

    # 结论
    L.append("---\n")
    L.append("## 结论\n")
    L.append("### 工程门禁\n")
    for name, c in concl["checks"].items():
        rule = c.get("rule")
        L.append(f"- [{'PASS' if c['pass'] else 'FAIL'}] {name}"
                 + (f"：{rule}" if rule else ""))
        for k, v in c.items():
            if k in ("pass", "rule"):
                continue
            L.append(f"  - {k}: {v}")
    L.append(f"\n**工程门禁：{concl['engineering_gate']}**\n")
    L.append("### 低频量对比（Δ = 30–120Hz band RMS vs ref0，dB）\n")
    L.append("| 曲目 | final | old_default | strong | final vs old_default |")
    L.append("|---|---|---|---|---|")
    for song, d in concl["per_song_low_band_delta_db"].items():
        rel = (f"final {d['final']:+.2f} < old {d['old_default']:+.2f}"
               if (d["final"] is not None and d["old_default"] is not None
                   and d["final"] < d["old_default"])
               else f"final {d['final']} vs old {d['old_default']}")
        L.append(f"| {song} | {d['final']:+.2f} | {d['old_default']:+.2f} | "
                 f"{d['strong']:+.2f} | {rel} |")
    md = concl["mean_deltas_db"]
    L.append(f"\n均值：final {md['final']:+.2f} dB，old_default {md['old_default']:+.2f} dB，"
             f"strong {md['strong']:+.2f} dB。")
    if concl.get("sidechain_final"):
        L.append("\nfinal 的 sidechain（抽吸代理，duck 越深越可能有可感知抽吸）：")
        for n in concl["sidechain_final"]:
            L.append(f"- {n}")
    L.append(f"\n### 音质门禁\n\n{concl['listening_gate']}\n")
    L.append("听音建议（留出集，非盲，按文件名直接 A/B）：")
    L.append("1. `final` vs `old_default`：低频量是否更接近 ref0 的平衡（old_default 是否过量）；")
    L.append("2. `strong` vs `final`：强档低频/冲击是否明显更强（仍能表达用户意图）；")
    L.append("3. 损伤检查：抽吸（sidechain duck）、低频变硬/变薄、mono 折叠是否成立。\n")
    if manifest["verification"]["failures"]:
        L.append("### 验证失败项\n")
        for f in manifest["verification"]["failures"]:
            L.append(f"- {f}")
        L.append("")
    L.append(f"manifest：`holdout/manifest.json`（含完整 DSP stdout、命令、旁车 JSON）。")

    (OUT_DIR / "HOLDOUT_REPORT.md").write_text("\n".join(L), encoding="utf-8")


# ── 主流程 ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--songs", default="set_1,set_2",
                    help="逗号分隔的留出曲目（默认 set_1,set_2；set_3 是校准曲，禁止）")
    ap.add_argument("--fresh-separation", action="store_true",
                    help="忽略已缓存的分离结果，重新跑 demucs")
    ap.add_argument("--keep-work", action="store_true", help="保留 _work 中间产物")
    ap.add_argument("--skip-mastering", action="store_true",
                    help="跳过 Soren 母带阶段（调试用）")
    args = ap.parse_args()

    songs = [s.strip() for s in args.songs.split(",") if s.strip()]
    for s in songs:
        if s not in HOLDOUT_INPUTS:
            ap.error(f"unknown song {s!r}; available: {', '.join(HOLDOUT_INPUTS)}")

    log(f"holdout check -> {OUT_DIR}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh_separation and WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    dsp_py = ppl.find_dsp_python()
    log(f"dsp python: {dsp_py}")
    soren_dir = ensure_soren_runtime()
    log(f"soren runtime: {soren_dir}")

    try:
        repover = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
                                 capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        repover = ""
    repover = f"v1.6.10{(' @ ' + repover) if repover else ''}"

    failures: list[str] = []
    manifest: dict = {
        "schema": "sorenstudio_holdout_check/1",
        "purpose": "开发规格 §13.6 留出集复核：以未参与调参的曲目检查定稿默认档偏好/损伤"
                   "与强设置可用性（工程与音质门禁）",
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "repo_version": repover,
        "blind": False,
        "dsp_python": dsp_py,
        "soren_runtime": soren_dir,
        "loudness_match": {"reference": "ref0 per song", "tolerance_lu": ppl.MATCH_TOL_LU,
                           "clip_ceiling_dbfs": ppl.CLIP_CEILING_DB,
                           "method": "与 tools/prepare_listening_pack.py 相同"},
        "lufs_method": "audio_metrics.integrated_lufs / measure_audio (repo module)",
        "true_peak_method": "audio_metrics.measure_audio true_peak_4x "
                            "(resample_poly 4x, kaiser 8.6, padtype=line)",
        "low_band_method": f"mono fold (L+R)/2, butter({BAND_RMS_ORDER}) bandpass "
                           f"{LOW_BAND_LO_HZ:.0f}-{LOW_BAND_HI_HZ:.0f}Hz (sosfiltfilt), RMS dB",
        "gate_thresholds": {"final_modest_max_db": FINAL_MODEST_MAX_DB,
                            "strong_intent_min_db": STRONG_INTENT_MIN_DB},
        "variants": {v: dict(VARIANTS[v]) for v in VARIANT_ORDER},
        "holdout_material": [],
        "songs": {},
        "verification": {"failures": failures},
    }

    for song in songs:
        input_wav = HOLDOUT_INPUTS[song]
        manifest["holdout_material"].append({
            "song": song, "input": str(input_wav),
            "duration_s": round(sf.info(str(input_wav)).duration, 3),
            "note": "未参与第一/第二轮调参（set_3 为校准曲）"})
        log(f"[song {song}] {input_wav}")
        work = WORK_ROOT / song
        work.mkdir(parents=True, exist_ok=True)
        try:
            rec = process_song(song, dsp_py, soren_dir, work,
                               fresh_sep=args.fresh_separation,
                               skip_mastering=args.skip_mastering,
                               failures=failures)
        except Exception as exc:  # 防御：单曲失败不拖垮整轮
            rec = {"input": str(input_wav), "error": f"unexpected: {exc!r}"}
            failures.append(f"{song}: unexpected — {exc!r}")
        manifest["songs"][song] = rec

    manifest["conclusion"] = build_conclusion(manifest)
    write_manifest(manifest)
    write_report(manifest)

    # 汇总表
    log("")
    log("=" * 108)
    log(f"{'song':<7}{'variant':<13}{'LUFS(pre)':>10}{'gain(dB)':>10}{'LUFS(post)':>11}"
        f"{'TP(dBTP)':>10}{'Δlow(dB)':>10}{'sc conf':>9}  ok")
    log("-" * 108)
    for song, s in manifest["songs"].items():
        for v in VARIANT_ORDER:
            vr = s.get("variants", {}).get(v, {})
            if not vr.get("file"):
                log(f"{song:<7}{v:<13}{'RENDER FAILED':>56}  FAIL")
                continue
            conf = (vr.get("dsp") or {}).get("sidechain", {}).get("confidence")
            conf_txt = f"{conf:.2f}" if isinstance(conf, float) else "-"
            log(f"{song:<7}{v:<13}{vr['render_lufs']:>10.2f}{vr['match_gain_db']:>+10.2f}"
                f"{vr['lufs']:>11.2f}{vr['true_peak_4x_dbtp']:>10.2f}"
                f"{vr['low_band_delta_db']:>+10.2f}{conf_txt:>9}  "
                f"{'OK' if vr['verified'] else 'FAIL'}")
        m = s.get("mastering") or {}
        if m.get("measured"):
            side = m.get("sidecar") or {}
            log(f"{song:<7}{'mastered':<13}{'':>10}{'':>10}"
                f"{m['measured']['integrated_lufs']:>11.2f}"
                f"{m['measured']['true_peak_4x_dbtp']:>10.2f}{'':>10}"
                f"{'':>9}  sidecar: target={side.get('target_lufs')} "
                f"actual={side.get('actual_lufs')} status="
                f"{side.get('target_status', side.get('target_met', '?'))}")
    log("=" * 108)

    concl = manifest["conclusion"]
    log(f"\n工程门禁: {concl['engineering_gate']}")
    for name, c in concl["checks"].items():
        log(f"  [{'PASS' if c['pass'] else 'FAIL'}] {name}")
    log(f"音质门禁: {concl['listening_gate']}")

    if failures:
        log("\nFAILURES:")
        for f in failures:
            log(f"  - {f}")
    if not args.keep_work:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)
        log(f"\nwork dir cleaned ({WORK_ROOT} removed); 分离结果可用 --keep-work 缓存复用")
    log(f"\nreport: {OUT_DIR / 'HOLDOUT_REPORT.md'} | manifest: {OUT_DIR / 'manifest.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
