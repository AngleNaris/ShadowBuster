# bass_enhance.py — 贝斯 stem 受控增强（包裹感 + 鼓质感版）
# 目标：补 AI 音乐丢失的①低频包裹感(sub 存在感) ②鼓的质感(punch/clarity)，不轰头。
# 改动(相对 v20260820)：
#  - sub 频段(30-60Hz)用 low-shelf 温和抬升，不再对全频段加法叠加；
#  - 新增 60-120Hz 轻度 bell 提升，强化鼓 body/punch；
#  - 新增瞬态强调(transient emphasize)单独强化鼓点起音；
#  - 饱和 drive 从固定 2.5 降到 1.6，仅作用于 sub+low-mid，低值不再重染色；
#  - 保留 RMS 门控(只控增强量) 与 混合后 -0.5dBTP 真峰值保护。
# 用法: python bass_enhance.py --bass bass.wav --in-mix premix.wav --out mix.wav [--sub-db 4] [--punch-db 2] [--sat 0.3] [--trans 0.3] [--auto-clarity]
# v20260908: 新增 opt-in --auto-clarity（保守清晰度 EQ，默认关闭；关闭时位级透传）：
#  - 立体声联动分析（两声道合并，共用同一组 EQ），在 sub 增强之前施加；
#  - 检测到活跃谐波内容才动作：180-450Hz 泥浊削减 ≤2dB、~800Hz 清晰度提升 ≤2dB；
#  - 能量启发式（静音门 + 谱平坦度 + 频段相对能量 + 斜坡 + 硬上限），非音色分类器，
#    拿不准（静音/纯 sub/噪声样）一律不动。
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_validation import validate_audio_pair, finite_range
from stage_metadata import write_report
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


AUTO_CLARITY_MUD_CENTER_HZ = 285.0
AUTO_CLARITY_CLARITY_CENTER_HZ = 800.0


def analyze_auto_clarity(stereo, sr):
    """全曲联动的保守频谱启发式；不是音色或串音分类器。"""
    x = np.asarray(stereo, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if len(x) < sr * 0.25 or not np.isfinite(x).all():
        return (0.0, 0.0)
    if np.mean(x * x) < 10 ** (-55 / 10):
        return (0.0, 0.0)
    # 平均声道功率而非波形，避免反相内容在分析时抵消。
    f, p = signal.welch(x, sr, nperseg=min(len(x), 16384), axis=0)
    p = p.mean(axis=1)
    spectrum = np.maximum(p[(f >= 60) & (f <= 1500)], 1e-30)
    flatness = np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum)
    if flatness > 0.5:
        return (0.0, 0.0)
    def energy(lo, hi):
        return float(np.sum(p[(f >= lo) & (f < hi)]) * (f[1] - f[0]))
    bass, mud, clarity = energy(30, 160), energy(180, 450), energy(550, 1100)
    if min(bass, mud, clarity) < 1e-6:
        return (0.0, 0.0)
    ratio = 10 * np.log10(clarity / mud)
    # 既有谐波太弱不提噪声；已明亮的音色不再提亮。
    confidence = np.clip((ratio + 30) / 10, 0, 1)
    dullness = np.clip((-6 - ratio) / 12, 0, 1)
    cut = np.clip((10 * np.log10(mud / bass) + 18) / 12, 0, 1)
    return (round(float(-2 * cut * dullness * confidence), 2),
            round(float(2 * dullness * confidence), 2))


def apply_auto_clarity_eq(x, sr, mud_db, clarity_db):
    """对单通道信号施加 auto-clarity 宽频 EQ；增益为 0 的频段保持原样。"""
    if mud_db < 0.0:
        x = _bell(x, sr, AUTO_CLARITY_MUD_CENTER_HZ, mud_db, q=0.8)
    if clarity_db > 0.0:
        x = _bell(x, sr, AUTO_CLARITY_CLARITY_CENTER_HZ, clarity_db, q=0.9)
    return x


def enhance_bass_stem(x, sr, sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.3,
                      drive=1.6, gate_db=-45.0, attack_ms=40.0, release_ms=250.0,
                      auto_clarity_gains=None):
    """增强 bass stem。
    sub_db   : sub(30-60Hz) low-shelf 提升，给包裹感
    punch_db : 60-120Hz bell 提升，给鼓 body/punch
    sat      : 谐波饱和混入比例 0-1（仅作用于 sub+low-mid）
    trans    : 瞬态强调强度 0-1
    auto_clarity_gains : None=关闭（默认，行为与旧版位级一致）；否则为
        analyze_auto_clarity() 返回的 (mud_db, clarity_db)，在 sub 增强
        **之前**施加同一组宽频 EQ。立体声联动：两声道必须传同一组增益。
    """
    x = x.astype(np.float64)
    mud_db, clar_db = (0.0, 0.0)
    if auto_clarity_gains is not None:
        mud_db, clar_db = (float(auto_clarity_gains[0]), float(auto_clarity_gains[1]))
    if (sub_db == 0 and punch_db == 0 and sat == 0 and trans == 0
            and mud_db == 0.0 and clar_db == 0.0):
        return x.astype(np.float32)

    # ── 0. opt-in 清晰度 EQ：在 sub 增强之前（增益由立体声联动分析统一给出）──
    if mud_db != 0.0 or clar_db != 0.0:
        x = apply_auto_clarity_eq(x, sr, mud_db, clar_db)

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
    ap.add_argument("--auto-clarity", action="store_true",
                    help="opt-in 保守清晰度 EQ：能量启发式（非音色分类器）检测到活跃谐波"
                         "内容时，做 ≤2dB 的 180-450Hz 泥浊削减与 ~800Hz 清晰度提升；"
                         "静音/纯 sub/噪声样不动，默认关闭")
    ap.add_argument("--report-json", type=Path, default=None)
    args = ap.parse_args()

    bass, sr = sf.read(args.bass, always_2d=True)
    in_mix, sr2 = sf.read(args.in_mix, always_2d=True)
    if sr2 != sr:
        ap.error(f"sample rate mismatch: {args.in_mix} is {sr2}, expected {sr}")
    try:
        validate_audio_pair(bass, in_mix, sr, primary_name="bass", secondary_name="in-mix")
        finite_range(args.bass_gain_db, "bass-gain-db", -12.0, 12.0)
    except ValueError as exc:
        ap.error(str(exc))

    for value, name, lo, hi in ((args.sub_db, "sub-db", -12, 12), (args.punch_db, "punch-db", -12, 12), (args.sat, "sat", 0, 1), (args.trans, "trans", 0, 1)):
        try: finite_range(value, name, lo, hi)
        except ValueError as exc: ap.error(str(exc))

    original_bass = bass.copy()
    if args.bass_gain_db:
        bass = bass * (10 ** (args.bass_gain_db / 20.0))

    # 立体声联动：全曲只分析一次（两声道合并），两声道共用同一组 EQ 增益，
    # 绝不做 per-channel 独立分析（否则左右清晰度会不一致）。
    clarity_gains = analyze_auto_clarity(bass, sr) if args.auto_clarity else None

    out = np.zeros_like(bass)
    for c in range(bass.shape[1]):
        out[:, c] = enhance_bass_stem(
            bass[:, c], sr,
            sub_db=args.sub_db, punch_db=args.punch_db,
            sat=args.sat, trans=args.trans,
            auto_clarity_gains=clarity_gains,
        )
    out = in_mix + (out - original_bass)

    # 混音后样本峰值保护：增强 delta 与完整输入混音相加后统一留出母带余量。
    # original_bass 是 clarity EQ **之前**的 delta 基准（整段处理差值都被保留），
    # 而 peak/scale 在施加 clarity EQ 之后的完整混音上测量——报告里的 scale
    # 因此已计入 clarity 提升带来的静态电平变化。
    # 这里不是 true-peak limiter；真正的 4× true peak 检测由下游 Soren 完成。
    neutral = (args.sub_db == 0 and args.punch_db == 0 and args.sat == 0 and
               args.trans == 0 and args.bass_gain_db == 0 and
               (clarity_gains is None or clarity_gains == (0.0, 0.0)))
    peak = np.max(np.abs(out))
    ceiling = 10 ** (-0.5 / 20.0)  # ≈ 0.944
    if not neutral and peak > ceiling:
        out *= ceiling / peak

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    if args.report_json:
        write_report(args.report_json, stage="bass", scale=float(ceiling / peak) if (not neutral and peak > ceiling) else 1.0,
                     input_path=args.in_mix, output_path=args.out)
    print(f"Bass-enhanced mix done: {args.out} | sub={args.sub_db}dB punch={args.punch_db}dB "
          f"sat={args.sat} trans={args.trans} "
          f"auto_clarity={clarity_gains if clarity_gains is not None else 'off'} "
          f"| {sr}Hz {bass.shape[1]}ch")


if __name__ == "__main__":
    main()
