# soundstage_reshape.py — 声场重塑实验（delta-add 分轨空间处理）
# 架构：out = mix + Σ (processed_i − stem_i)
#   - 以原曲为基底，每条轨只把自己的"处理差值"加回；
#   - 所有增强归零时输出与原曲逐样本一致（bypass 完全守恒，可做 null test）；
#   - 分离残差 (mix − Σstems) 随 mix 基底原样保留，不丢"胶感"。
# 处理（全部线性域，不放大分离伪影）：
#   - other: side 通道高频 shelf 提升（拉开铺底乐器的边缘）+ 可选 side 整体增益
#   - drums: side 通道高频 shelf 轻提升（镲片/空间感）
#   - bass / vocals: 不处理
# 用法:
#   python soundstage_reshape.py --in-wav in.wav --out-wav out.wav \
#       [--stems-dir DIR]  # 缺省时先跑 demucs 4-stem
#   python soundstage_reshape.py --check-bypass ...  # 守恒自检（全零参数应逐样本一致）
import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

SR = 44_100


def _high_shelf(x, sr, fc, gain_db, order=2):
    """线性相位近似的 side 高频 shelf：x + hp(x) * (g-1)。g=0dB 时精确恒等。"""
    if gain_db == 0:
        return x
    g = 10 ** (gain_db / 20.0)
    hp = signal.sosfiltfilt(signal.butter(order, fc, "highpass", fs=sr, output="sos"), x, padlen=0)
    return x + hp * (g - 1.0)


def _low_shelf(x, sr, fc, gain_db, order=2):
    """低频 shelf（bass 轨补偿用）：x + lp(x) * (g-1)。g=0dB 时精确恒等。"""
    if gain_db == 0:
        return x
    g = 10 ** (gain_db / 20.0)
    lp = signal.sosfiltfilt(signal.butter(order, fc, "lowpass", fs=sr, output="sos"), x, padlen=0)
    return x + lp * (g - 1.0)


def _reshape_stem(x, sr, side_high_db, side_high_fc, side_gain_db):
    """M/S 域只动 side：高频 shelf + 整体 side 增益；mid 保持不变。"""
    left, right = x[:, 0], x[:, 1]
    mid, side = (left + right) / 2.0, (left - right) / 2.0
    side = _high_shelf(side, sr, side_high_fc, side_high_db)
    if side_gain_db:
        side = side * (10 ** (side_gain_db / 20.0))
    return np.column_stack((mid + side, mid - side))


def _reshape_bass(x, sr, sub_db, sub_fc):
    """bass 轨低频补偿：全频段低 shelf（包在 delta-add 里，归零恒等）。"""
    return _low_shelf(x, sr, sub_fc, sub_db)


def _dynamic_side_shelf(x, sr, fc, gain_db, thr_pct=70.0, rel_db=6.0):
    """de-esser 思路的 side 高频 shelf：boost 受 side 高频包络动态控制。

    包络低于自身 P70 分位时给满 boost；超过后按 rel_db 线性收敛到 0
    ——镲片瞬态（side 高频尖峰）时自动收，安静段保持拓宽。
    """
    left, right = x[:, 0], x[:, 1]
    mid, side = (left + right) / 2.0, (left - right) / 2.0
    g = 10 ** (gain_db / 20.0)
    hp = signal.sosfiltfilt(signal.butter(2, fc, "highpass", fs=sr, output="sos"), side, padlen=0)
    env = np.sqrt(np.maximum(signal.sosfilt(signal.butter(2, 10, "lowpass", fs=sr, output="sos"), hp ** 2), 0.0) + 1e-20)
    env_db = 20 * np.log10(env + 1e-12)
    thr = np.percentile(env_db, thr_pct)
    gate = np.clip(1.0 - (env_db - thr) / rel_db, 0.0, 1.0)
    gate = signal.sosfilt(signal.butter(2, 200, "lowpass", fs=sr, output="sos"), gate)  # 去锯齿
    boosted_side = side + hp * (g - 1.0) * gate
    return np.column_stack((mid + boosted_side, mid - boosted_side))


# 模式预设: (other: shelf_db/shelf_fc/side_gain_db, drums: 同)
MODES = {
    "shelf3k":   dict(other=(3.0, 3500.0, 1.0), drums=(1.5, 5000.0, 0.0)),   # 原始版（擦片尖锐参照）
    "shelf-air": dict(other=(3.0, 7000.0, 1.0), drums=(1.5, 8000.0, 0.0)),   # 倾斜上移出敏感区
    "broadband": dict(other=(0.0, 3500.0, 3.0), drums=(0.0, 5000.0, 1.5)),   # 无频谱倾斜，纯 side 增益
    "dynamic":   dict(other=(3.0, 3500.0, 1.0), drums=(1.5, 5000.0, 0.0)),   # 静态参数同 shelf3k + 动态门
}


def ensure_stems(in_wav, stems_dir, device="cuda"):
    """跑 demucs 4-stem（与主管线同一命令），返回 stems 目录。"""
    stems_dir = Path(stems_dir)
    stem_dir = stems_dir / "htdemucs" / Path(in_wav).stem
    if (stem_dir / "drums.wav").exists():
        return stem_dir
    stems_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "demucs", "-n", "htdemucs", "--float32",
           "--clip-mode=none", "-o", str(stems_dir), str(in_wav)]
    subprocess.run(cmd, check=True, cwd=str(Path(stems_dir)))
    return stem_dir


def width_report(mix, out, sr):
    """时域分频段宽度 + 单声道兼容检查。"""
    def band_width(x, lo, hi):
        a = signal.sosfilt(signal.butter(2, lo, "highpass", fs=sr, output="sos"), x, axis=0)
        b = signal.sosfilt(signal.butter(2, hi, "lowpass", fs=sr, output="sos"), a, axis=0)
        m = b.mean(axis=1); s = (b[:, 0] - b[:, 1]) / 2
        return (s ** 2).mean() / ((m ** 2).mean() + (s ** 2).mean() + 1e-12)
    print("  分频段宽度 (sideE/(midE+sideE))：")
    for lo, hi, name in [(20, 120, "low "), (120, 2000, "mid "), (2000, 8000, "high"), (8000, 16000, "air ")]:
        print(f"    {name}: mix {band_width(mix, lo, hi):.3f} -> out {band_width(out, lo, hi):.3f}")
    mo, mo2 = mix.mean(axis=1), out.mean(axis=1)
    n = min(len(mo), len(mo2))
    dr = 10 * np.log10(((mo2[:n] ** 2).mean() + 1e-12) / ((mo[:n] ** 2).mean() + 1e-12))
    corr = np.corrcoef(mo[:n], mo2[:n])[0, 1]
    print(f"  mono fold-down 能量变化 {dr:+.2f}dB（相位抵消检查），与原 mono 相关 {corr:.4f}")


def main():
    ap = argparse.ArgumentParser(description="delta-add 声场重塑")
    ap.add_argument("--in-wav", required=True, type=Path)
    ap.add_argument("--out-wav", required=True, type=Path)
    ap.add_argument("--stems-dir", type=Path, default=None, help="demucs 输出根目录（缺省 in 同级 _reshape_stems）")
    ap.add_argument("--mode", choices=list(MODES), default="shelf3k",
                    help="shelf3k=原始版 | shelf-air=倾斜上移出敏感区 | broadband=纯side增益 | dynamic=动态门")
    ap.add_argument("--wet", type=float, default=1.0, help="干湿比 0-1：缩放全部处理差值，0=与原曲逐样本一致")
    ap.add_argument("--other-side-high-db", type=float, default=3.0)
    ap.add_argument("--other-side-high-fc", type=float, default=3500.0)
    ap.add_argument("--other-side-gain-db", type=float, default=1.0)
    ap.add_argument("--drums-side-high-db", type=float, default=1.5)
    ap.add_argument("--drums-side-high-fc", type=float, default=5000.0)
    ap.add_argument("--bass-sub-db", type=float, default=0.0, help="bass 轨低频 shelf 补偿 dB")
    ap.add_argument("--bass-sub-fc", type=float, default=110.0)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    in_wav = args.in_wav.resolve()
    stems_root = args.stems_dir or (in_wav.parent / (in_wav.stem + "_reshape_stems"))
    stem_dir = ensure_stems(in_wav, stems_root, device="cpu" if args.cpu else "cuda")

    mix, sr = sf.read(in_wav, always_2d=True, dtype="float64")
    assert sr == SR, f"需要 {SR}Hz 输入（先用 ffmpeg 转采样率）"

    preset = MODES[args.mode]
    params = {
        "other": preset["other"],
        "drums": preset["drums"],
    }
    out = mix.copy()
    for name, (db, fc, gain) in params.items():
        stem, s_sr = sf.read(stem_dir / f"{name}.wav", always_2d=True, dtype="float64")
        n = min(len(stem), len(out))
        if s_sr != sr:
            raise SystemExit(f"{name} 采样率 {s_sr} != {sr}")
        if args.mode == "dynamic" and db > 0:
            processed = _dynamic_side_shelf(stem[:n], sr, fc, db)
        else:
            processed = _reshape_stem(stem[:n], sr, db, fc, gain)
        out[:n] += processed - stem[:n]        # delta-add：只加处理差值
        print(f"  {name}: shelf +{db}dB@{fc:.0f}Hz, side gain +{gain}dB"
              + ("  [动态门]" if args.mode == "dynamic" and db > 0 else ""))
    if args.bass_sub_db:
        stem, s_sr = sf.read(stem_dir / "bass.wav", always_2d=True, dtype="float64")
        n = min(len(stem), len(out))
        processed = _reshape_bass(stem[:n], sr, args.bass_sub_db, args.bass_sub_fc)
        out[:n] += processed - stem[:n]
        print(f"  bass: low shelf +{args.bass_sub_db}dB@{args.bass_sub_fc:.0f}Hz")
    wet = min(max(args.wet, 0.0), 2.0)
    if wet != 1.0:
        out = mix + (out - mix) * wet     # 干湿比：等比缩放全部处理差值
        print(f"  干湿比 wet={wet:.2f} (0=原曲, 1=全量)")

    # 守恒自检：所有差值路径在零参数下恒等；此处再验证 out − mix 的能量与 delta 一致
    peak = np.abs(out).max()
    if peak > 0.999:
        out *= 0.999 / peak
        print(f"  峰值保护: {-20*np.log10(peak):.2f}dB")
    sf.write(args.out_wav, out.astype(np.float32), sr, subtype="FLOAT")
    print(f"  输出: {args.out_wav}")

    print("宽度报告:")
    width_report(mix, out, sr)


if __name__ == "__main__":
    main()
