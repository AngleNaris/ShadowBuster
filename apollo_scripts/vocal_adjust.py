# vocal_adjust.py — 人声 stem 整体增益（delta-add 保留分离残差）
# 背景：Lew 高频重建、声场拓宽（side 增益）、Soren 频谱匹配/限器都会抬升伴奏的
# 相对能量，实测 300Hz 以上 side 比 mid 多涨 2dB+，人声（集中在中央 mid）因此
# 听感靠后。本脚本对人声所在轨直接做整体增益：
#   out = in_mix + (vocals * gain - vocals)
# 只动人声轨的差值，Demucs 分离残差与其他轨逐样本不变；0dB 由管线直接透传。
# 用法: python vocal_adjust.py --vocals vocals.wav --in-mix premix.wav --out out.wav --vocal-gain-db 2
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf


def main():
    ap = argparse.ArgumentParser(description="Vocal stem gain via delta-add (residual preserving)")
    ap.add_argument("--vocals", required=True, type=Path, help="分离出的 vocals stem (wav)")
    ap.add_argument("--in-mix", required=True, type=Path,
                    help="上一阶段完整混音；通过 delta-add 保留分离残差")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--vocal-gain-db", type=float, default=0.0, help="vocals 整体增益 dB（-12 ~ +12）")
    args = ap.parse_args()

    if not np.isfinite(args.vocal_gain_db) or not -12.0 <= args.vocal_gain_db <= 12.0:
        ap.error("--vocal-gain-db must be finite and within [-12, 12]")

    vocals, sr = sf.read(args.vocals, always_2d=True)
    in_mix, sr2 = sf.read(args.in_mix, always_2d=True)
    assert sr2 == sr, f"采样率不一致: {args.in_mix}"

    gain = 10 ** (args.vocal_gain_db / 20.0)
    n = min(len(vocals), len(in_mix))
    out = in_mix[:n] + (vocals[:n] * gain - vocals[:n])

    # 与 bass/drum_enhance 相同约定：非零处理后留 -0.5dB 母带余量（真峰值由 Soren 负责）
    peak = np.max(np.abs(out)) if out.size else 0.0
    ceiling = 10 ** (-0.5 / 20.0)
    if peak > ceiling:
        out *= ceiling / peak

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    print(f"Vocal gain done: {args.out} | gain={args.vocal_gain_db:+.1f}dB | {sr}Hz {vocals.shape[1]}ch")


if __name__ == "__main__":
    main()
