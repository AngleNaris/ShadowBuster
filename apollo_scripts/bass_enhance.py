# bass_enhance.py — 贝斯 stem 受控增强（包裹感 + 鼓质感版）
# 目标：补 AI 音乐丢失的①低频包裹感(sub 存在感) ②鼓的质感(punch/clarity)，不轰头。
# 改动(相对 v20260820)：
#  - sub 频段(30-60Hz)用 low-shelf 温和抬升，不再对全频段加法叠加；
#  - 新增 60-120Hz 轻度 bell 提升，强化鼓 body/punch；
#  - 新增瞬态强调(transient emphasize)单独强化鼓点起音；
#  - 饱和 drive 从固定 2.5 降到 1.6，仅作用于 sub+low-mid，低值不再重染色；
#  - 保留 RMS 门控(只控增强量) 与 混合后 -0.5dBTP 真峰值保护。
# 用法: python bass_enhance.py --bass bass.wav --in-mix premix.wav --out mix.wav [--sub-db 4] [--punch-db 2] [--sat 0.3] [--trans 0.3]
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal


def soft_clip(x, drive):
    """tanh 软限幅产生温和谐波"""
    return np.tanh(x * drive) / np.tanh(drive)


def _shelf(x, sr, fc, gain_db, q=0.7):
    """二阶 low-shelf：只改变 sub/low 频段，不污染全频段"""
    g = 10 ** (gain_db / 20.0)
    sos = signal.butter(2, fc, btype="lowpass", fs=sr, output="sos")
    # 用 low-pass 近似 shelf：原信号 + (低频部分 * (g-1))
    lp = signal.sosfiltfilt(sos, x, padlen=0)
    return x + lp * (g - 1.0)


def _bell(x, sr, fc, gain_db, q=1.2):
    """二阶 peaking（RBJ biquad）：只在中心频率附近按 gain_db 提升，
    频带外保持 0dB 平直——不能像 iirpeak 谐振器那样在带外深削。
    （旧实现用 iirpeak，其在 2-5kHz 是 -26~-35dB 的凹槽，会削掉
    bass stem 里的中频串音，造成混音中频空洞。）"""
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * q)
    A = 10 ** (gain_db / 40.0)
    b0, b1, b2 = 1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A
    a0, a1, a2 = 1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A
    b = np.array([b0, b1, b2]) / a0
    a = np.array([a0, a1, a2]) / a0
    return signal.lfilter(b, a, x)


def _transient(x, sr, amount, fc=120.0, q=0.7):
    """瞬态强调：提取高频包络并把攻击段单独放大，强化鼓点起音"""
    # 用高通突出瞬态，再做单边整流得到瞬态包络
    hp = signal.sosfiltfilt(
        signal.butter(2, fc, "highpass", fs=sr, output="sos"), x, padlen=0
    )
    env = np.abs(hp)
    # 快攻击慢释放，得到"只在起音出现"的瞬态权重
    attack = 1.0 - np.exp(-1.0 / (sr * 0.005))
    release = 1.0 - np.exp(-  1.0 / (sr * 0.08))
    w = np.empty_like(env)
    prev = 0.0
    for i in range(len(env)):
        a = attack if env[i] > prev else release
        prev = prev + a * (env[i] - prev)
        w[i] = max(0.0, env[i] - prev)  # 只取"超出慢包络"的快速变化 = 瞬态
    wmax = np.max(w)
    if wmax > 1e-9:
        w /= wmax
    else:
        w[:] = 0.0
    return x + hp * w * amount


def enhance_bass_stem(x, sr, sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.3,
                      drive=1.6, gate_db=-45.0, attack_ms=40.0, release_ms=250.0):
    """增强 bass stem。
    sub_db   : sub(30-60Hz) low-shelf 提升，给包裹感
    punch_db : 60-120Hz bell 提升，给鼓 body/punch
    sat      : 谐波饱和混入比例 0-1（仅作用于 sub+low-mid）
    trans    : 瞬态强调强度 0-1
    """
    x = x.astype(np.float64)
    if sub_db == 0 and punch_db == 0 and sat == 0 and trans == 0:
        return x.astype(np.float32)

    # ── 1. 包裹感：sub low-shelf（30Hz 起），仅低频，不污染全频段 ──
    x_warm = _shelf(x, sr, 30.0, sub_db)
    # ── 2. 鼓质感：60-120Hz bell 提升 ──
    x_warm = _bell(x_warm, sr, 90.0, punch_db, q=1.2)
    # ── 3. 谐波饱和：仅作用于 sub+low-mid（再低通一次避免高频染色） ──
    lp = signal.sosfiltfilt(
        signal.butter(2, 200.0, "lowpass", fs=sr, output="sos"), x_warm, padlen=0
    )
    x_sat = soft_clip(lp, drive)
    x_eff = (1.0 - sat) * lp + sat * x_sat
    # 高频部分(>200Hz)保持原貌，不参与饱和
    x_eff = x_eff + (x_warm - lp)
    # ── 4. 瞬态强调 ──
    x_eff = _transient(x_eff, sr, trans * 0.8)

    # ── 5. RMS 门控：bass 活跃段才应用增强，静音段保持干净 ──
    env = np.sqrt(np.convolve(x ** 2, np.ones(int(sr * 0.05)) / (sr * 0.05), mode="same"))
    env_db = 20 * np.log10(env + 1e-12)
    target = np.where(env_db > gate_db, 1.0, 0.0)
    a = 1.0 - np.exp(-1.0 / (sr * attack_ms / 1000.0))
    r = 1.0 - np.exp(-1.0 / (sr * release_ms / 1000.0))
    gate = np.empty_like(target)
    acc = 0.0
    for i in range(len(target)):
        alpha = a if target[i] >= acc else r
        acc = acc + alpha * (target[i] - acc)
        gate[i] = acc
    x_out = x + (x_eff - x) * gate

    return x_out.astype(np.float32)


def main():
    ap = argparse.ArgumentParser(description="Bass stem controlled enhancement (warmth + punch)")
    ap.add_argument("--bass", required=True, type=Path, help="分离出的 bass stem (wav)")
    ap.add_argument("--in-mix", required=True, type=Path,
                    help="上一阶段完整混音；通过 delta-add 保留分离残差")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sub-db", type=float, default=4.0, help="sub(30-60Hz) 提升 dB")
    ap.add_argument("--punch-db", type=float, default=2.0, help="60-120Hz 鼓 body 提升 dB")
    ap.add_argument("--sat", type=float, default=0.3, help="谐波饱和混入比例 0-1")
    ap.add_argument("--trans", type=float, default=0.3, help="瞬态强调强度 0-1")
    ap.add_argument("--bass-gain-db", type=float, default=0.0, help="bass 整体增益 dB")
    args = ap.parse_args()

    bass, sr = sf.read(args.bass, always_2d=True)
    in_mix, sr2 = sf.read(args.in_mix, always_2d=True)
    assert sr2 == sr, f"采样率不一致: {args.in_mix}"

    original_bass = bass.copy()
    if args.bass_gain_db:
        bass = bass * (10 ** (args.bass_gain_db / 20.0))

    out = np.zeros_like(bass)
    for c in range(bass.shape[1]):
        out[:, c] = enhance_bass_stem(
            bass[:, c], sr,
            sub_db=args.sub_db, punch_db=args.punch_db,
            sat=args.sat, trans=args.trans,
        )
    n = min(len(out), len(in_mix), len(original_bass))
    out = in_mix[:n] + (out[:n] - original_bass[:n])

    # 混合后样本峰值保护：增强 delta 与完整输入混音相加后统一留出母带余量。
    # 这里不是 true-peak limiter；真正的 4× true peak 检测由下游 Soren 完成。
    neutral = (args.sub_db == 0 and args.punch_db == 0 and args.sat == 0 and
               args.trans == 0 and args.bass_gain_db == 0)
    peak = np.max(np.abs(out))
    ceiling = 10 ** (-0.5 / 20.0)  # ≈ 0.944
    if not neutral and peak > ceiling:
        out *= ceiling / peak

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    print(f"Bass-enhanced mix done: {args.out} | sub={args.sub_db}dB punch={args.punch_db}dB "
          f"sat={args.sat} trans={args.trans} | {sr}Hz {bass.shape[1]}ch")


if __name__ == "__main__":
    main()
