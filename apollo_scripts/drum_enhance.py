# drum_enhance.py — 鼓 stem 受控增强（punch/attack 版）
# 背景：管线原先把 punch(90Hz bell) 与瞬态强调施加在 demucs 的 bass 轨上，但实测
# （4-stem 分离，kick 起音点低频能量中位 99.9% 在 drums 轨）鼓的起音根本不在 bass 轨
# —— bass 轨上的"鼓处理"实际只作用于贝斯。本脚本把同样的处理意图施加到鼓所在的轨：
#  ① kick body/punch：90Hz bell（60-120Hz），只抬鼓的低频体，不碰其他轨；
#  ② 全频段瞬态强调：kick beater click / snare crack / hat attack（HP>120Hz 快包络）；
#  ③ RMS 门控改快 attack(5ms)：跟上密集鼓点，起音第一击就有增强；release 150ms；
#  ④ 以完整输入混音为基底 delta-add 后做 -0.5dB 峰值保护，保留分离残差并给下游母带留 headroom。
# 用法: python drum_enhance.py --drums drums.wav --in-mix premix.wav --out out.wav [--punch-db 2] [--trans 0.3]
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf

from bass_enhance import _bell, _transient


def _gate(x, sr, gate_db=-45.0, attack_ms=5.0, release_ms=150.0):
    """鼓活跃段才应用增强：快 attack 让第一击就生效，慢 release 避免尾音闪烁。"""
    env = np.sqrt(np.convolve(x ** 2, np.ones(int(sr * 0.02)) / (sr * 0.02), mode="same"))
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
    return gate


def enhance_drum_stem(x, sr, punch_db=2.0, trans=0.3):
    """增强鼓 stem。punch_db: 90Hz bell 提升量; trans: 瞬态强调强度 0-1。"""
    x = x.astype(np.float64)
    if punch_db == 0 and trans == 0:
        return x.copy()
    # 1. kick body/punch：90Hz bell，只动 60-120Hz
    x_warm = _bell(x, sr, 90.0, punch_db, q=1.2)
    # 2. 全频段瞬态强调（_transient 用 HP>120Hz 快包络，覆盖 kick click/snare/hat 起音）
    x_eff = _transient(x_warm, sr, trans)
    # 3. 门控：鼓不响的段落保持原样
    gate = _gate(x, sr)
    return x + (x_eff - x) * gate


def main():
    ap = argparse.ArgumentParser(description="Drum stem controlled enhancement (punch + attack)")
    ap.add_argument("--drums", required=True, type=Path, help="分离出的 drums stem (wav)")
    ap.add_argument("--in-mix", required=True, type=Path,
                    help="上一阶段完整混音；通过 delta-add 保留分离残差")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--punch-db", type=float, default=2.0, help="90Hz 鼓 body 提升 dB")
    ap.add_argument("--trans", type=float, default=0.3, help="瞬态强调强度 0-1")
    ap.add_argument("--drums-gain-db", type=float, default=0.0, help="drums 整体增益 dB")
    args = ap.parse_args()

    drums, sr = sf.read(args.drums, always_2d=True)
    in_mix, sr2 = sf.read(args.in_mix, always_2d=True)
    assert sr2 == sr, f"采样率不一致: {args.in_mix}"

    original_drums = drums.copy()
    if args.drums_gain_db:
        drums = drums * (10 ** (args.drums_gain_db / 20.0))

    out = np.zeros_like(drums)
    for c in range(drums.shape[1]):
        out[:, c] = enhance_drum_stem(drums[:, c], sr, punch_db=args.punch_db, trans=args.trans)
    n = min(len(out), len(in_mix), len(original_drums))
    out = in_mix[:n] + (out[:n] - original_drums[:n])

    # 与 bass_enhance 相同的约定：混合后统一留 -0.5dB 母带余量（真峰值由下游 Soren 负责）
    neutral = (args.punch_db == 0 and args.trans == 0 and args.drums_gain_db == 0)
    peak = np.max(np.abs(out))
    ceiling = 10 ** (-0.5 / 20.0)
    if not neutral and peak > ceiling:
        out *= ceiling / peak

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    print(f"Drum-enhanced mix done: {args.out} | punch={args.punch_db}dB trans={args.trans} | {sr}Hz {drums.shape[1]}ch")


if __name__ == "__main__":
    main()
