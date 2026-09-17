"""Bounded delta-add enhancer for optional six-stem outputs.

Two product kinds:
- guitar: the htdemucs_6s guitar stem, processed alone;
- synth:  the "other" + "piano" stems summed into one synth/keys/FX group
  (exact math on same-model stems — this is a grouping, not a synth separator).

Every control defaults to 0 dB and the stage then passes the mix through
bit-exact. Six-stem separation is not instrument isolation: mislabelled
content is possible, and the piano contribution to the synth group can be
near-silent on songs without piano. All gains are hard-bounded and
channel-linked (no per-channel independent processing).
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
from scipy import signal

import soundfile as sf

from audio_validation import finite_range, validate_audio, validate_audio_pair
from stage_metadata import write_report

SR = 44_100
KINDS = ("guitar", "synth")

# 控制范围即硬上限：任何增益都不可能超过这里声明的界。
GAIN_DB = (-6.0, 6.0)          # 全轨增益
MUD_DB = (0.0, 6.0)            # 低中频泥浊削减（300Hz 钟形）
MUD_FC = 300.0
MUD_Q = 1.2
PRESENCE_DB = (0.0, 6.0)       # 存在感提升（3kHz 高架）
PRESENCE_FC = 3000.0
HARSH_DB = (0.0, 6.0)          # 高频毛刺收敛（7kHz 高架衰减）
HARSH_FC = 7000.0
WIDTH_DB = (0.0, 6.0)          # stem side 增益（M/S，声道天然联动）


def _low_shelf(x, sr, fc, gain_db, q=0.7071):
    """low-shelf：模拟原型 + 双线性变换 + filtfilt 零相位（与 bass_enhance 同设计）。

    不用 RBJ shelf：低 fc/小增益组合会失稳；此设计对任意 G>0 恒稳定。
    filtfilt 幅频取平方，故单次按 gain_db/2 设计，总增益精确等于 gain_db；
    gain_db=0 精确恒等。负 gain_db 即高频保持、低频衰减。
    """
    if gain_db == 0.0:
        return np.asarray(x, dtype=np.float64).copy()
    G = 10 ** (gain_db / 40.0)
    w0 = 2.0 * sr * np.tan(np.pi * fc / sr)
    g4 = G ** 0.25
    num = [1.0, g4 * w0 / q, np.sqrt(G) * w0 * w0]
    den = [1.0, w0 / (g4 * q), w0 * w0 / np.sqrt(G)]
    b, a = signal.bilinear(num, den, fs=sr)
    return signal.filtfilt(b, a, np.asarray(x, dtype=np.float64), axis=0, padlen=0)


def _high_shelf(x, sr, fc, gain_db, q=0.7071):
    """high-shelf：low-shelf 原型的 s² 项与常数项对调（G 方向同步翻转）。

    低频保持 0dB、高频按 gain_db 提升/衰减；系数恒正 → 任意 G>0 稳定。
    filtfilt 设计同上：单次 gain_db/2，总增益精确 gain_db。
    """
    if gain_db == 0.0:
        return np.asarray(x, dtype=np.float64).copy()
    G = 10 ** (gain_db / 40.0)
    w0 = 2.0 * sr * np.tan(np.pi * fc / sr)
    g4 = G ** 0.25
    num = [np.sqrt(G), g4 * w0 / q, w0 * w0]
    den = [1.0 / np.sqrt(G), w0 / (g4 * q), w0 * w0]
    b, a = signal.bilinear(num, den, fs=sr)
    return signal.filtfilt(b, a, np.asarray(x, dtype=np.float64), axis=0, padlen=0)


def _bell(x, sr, fc, gain_db, q=1.2):
    """二阶 peaking（RBJ biquad + filtfilt 零相位）：只在中心频率附近按
    gain_db 提升，频带外保持平直。filtfilt 幅频取平方，故单次按 gain_db/2
    设计（A = 10^(gain_db/80)，两次合计精确 gain_db）；gain_db=0 精确恒等。
    """
    if gain_db == 0.0:
        return np.asarray(x, dtype=np.float64).copy()
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * q)
    A = 10 ** (gain_db / 80.0)
    b = np.array([1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A])
    a = np.array([1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A])
    return signal.filtfilt(b / a[0], a / a[0], np.asarray(x, dtype=np.float64),
                           axis=0, padlen=0)


def enhance_stem(stem, sr, *, gain_db=0.0, mud_cut_db=0.0, presence_db=0.0,
                 harsh_cut_db=0.0, width_db=0.0):
    """有界 EQ + side 增益；全零参数时调用方必须走位级旁路，不进本函数。"""
    out = _bell(stem, sr, MUD_FC, -abs(mud_cut_db), MUD_Q)
    out = _high_shelf(out, sr, PRESENCE_FC, abs(presence_db))
    out = _high_shelf(out, sr, HARSH_FC, -abs(harsh_cut_db))
    if gain_db:
        out = out * (10 ** (gain_db / 20.0))
    if width_db:
        mid = out.mean(axis=1)
        side = (out[:, 0] - out[:, 1]) / 2.0
        side = side * (10 ** (width_db / 20.0))
        out = np.column_stack((mid + side, mid - side))
    if not np.isfinite(out).all():
        raise RuntimeError("stem enhancement produced non-finite samples")
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Bounded optional-stem (guitar / synth group) delta-add enhancer")
    ap.add_argument("--stem", type=Path, required=True,
                    help="primary source stem (guitar kind) or the synth group's other stem")
    ap.add_argument("--stem2", type=Path, default=None,
                    help="optional second source stem merged into the group (synth kind: piano)")
    ap.add_argument("--in-mix", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--kind", choices=KINDS, required=True)
    ap.add_argument("--gain-db", type=float, default=0.0)
    ap.add_argument("--mud-cut-db", type=float, default=0.0)
    ap.add_argument("--presence-db", type=float, default=0.0)
    ap.add_argument("--harsh-cut-db", type=float, default=0.0)
    ap.add_argument("--width-db", type=float, default=0.0)
    ap.add_argument("--report-json", type=Path, default=None)
    args = ap.parse_args()

    sources = [args.stem] + ([args.stem2] if args.stem2 is not None else [])
    missing = [str(p) for p in sources if not p.exists()]
    if missing:
        # 缺失任一源分轨透明降级：透传混音并如实报告，绝不把 other 冒充本轨。
        shutil.copyfile(args.in_mix, args.out)
        if args.report_json:
            write_report(args.report_json, stage=args.kind, scale=1.0,
                         input_path=args.in_mix, output_path=args.out,
                         extra={"stem": args.kind, "sources": [str(p) for p in sources],
                                "applied": False, "status": "unavailable",
                                "reason": "missing_stem", "missing": missing})
        print(f"{args.kind} sources missing ({missing}); mix passed through unchanged")
        return

    loaded = [sf.read(p, always_2d=True) for p in sources]
    stem, sr = loaded[0]
    in_mix, sr_mix = sf.read(args.in_mix, always_2d=True)
    if sr_mix != sr:
        ap.error(f"sample rate mismatch: {args.in_mix} is {sr_mix}, expected {sr}")
    try:
        validate_audio_pair(stem, in_mix, sr, primary_name=args.kind,
                            secondary_name="in-mix")
        for path, (data, data_sr) in zip(sources[1:], loaded[1:]):
            if data_sr != sr:
                ap.error(f"sample rate mismatch: {path} is {data_sr}, expected {sr}")
            validate_audio_pair(data, in_mix, sr, primary_name=str(path),
                                secondary_name="in-mix")
        for value, name, (lo, hi) in (
                (args.gain_db, "gain-db", GAIN_DB),
                (args.mud_cut_db, "mud-cut-db", MUD_DB),
                (args.presence_db, "presence-db", PRESENCE_DB),
                (args.harsh_cut_db, "harsh-cut-db", HARSH_DB),
                (args.width_db, "width-db", WIDTH_DB)):
            finite_range(value, name, lo, hi)
    except ValueError as exc:
        ap.error(str(exc))

    group = stem.astype(np.float64) + sum(
        data.astype(np.float64) for data, _ in loaded[1:])
    neutral = (args.gain_db == 0 and args.mud_cut_db == 0 and
               args.presence_db == 0 and args.harsh_cut_db == 0 and
               args.width_db == 0)
    if neutral:
        # 位级旁路：不进浮点运算，不触碰混音样本。
        shutil.copyfile(args.in_mix, args.out)
        if args.report_json:
            write_report(args.report_json, stage=args.kind, scale=1.0,
                         input_path=args.in_mix, output_path=args.out,
                         extra={"stem": args.kind, "sources": [str(p) for p in sources],
                                "applied": False, "status": "not_applied",
                                "reason": "disabled"})
        print(f"{args.kind} enhancement neutral; mix passed through unchanged")
        return

    processed = enhance_stem(
        group, sr, gain_db=args.gain_db,
        mud_cut_db=args.mud_cut_db, presence_db=args.presence_db,
        harsh_cut_db=args.harsh_cut_db, width_db=args.width_db)
    out = in_mix.astype(np.float64) + (processed - group)

    # 混音后样本峰值保护：与其他阶段同一约定（-0.5dB 静态缩放，非 limiter）。
    peak = np.max(np.abs(out)) if out.size else 0.0
    ceiling = 10 ** (-0.5 / 20.0)
    scale = 1.0
    if peak > ceiling:
        scale = ceiling / peak
        out *= scale

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    if args.report_json:
        write_report(args.report_json, stage=args.kind,
                     scale=float(scale), input_path=args.in_mix,
                     output_path=args.out,
                     extra={"stem": args.kind, "applied": True,
                            "status": "applied",
                            "sources": [str(p) for p in sources],
                            "gains_db": {"gain": float(args.gain_db),
                                         "mud_cut": float(args.mud_cut_db),
                                         "presence": float(args.presence_db),
                                         "harsh_cut": float(args.harsh_cut_db),
                                         "width": float(args.width_db)}})
    src_txt = "+".join(p.stem for p in sources)
    print(f"{args.kind} enhanced mix done: {args.out} (sources={src_txt} "
          f"gain={args.gain_db}dB mud={args.mud_cut_db}dB "
          f"presence={args.presence_db}dB harsh={args.harsh_cut_db}dB "
          f"width={args.width_db}dB)")


if __name__ == "__main__":
    raise SystemExit(main())
