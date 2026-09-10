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
# v20260909: sat 低中频预算（无新 UI/参数，不改 sub/punch/trans/auto-clarity；
# 量值为启发式，未经听感验证）：
#  - 仅对 sat*(soft_clip(lp)-lp) 的 180-500Hz 增量（tanh 奇对称→奇次谐波为主）
#    施加 0..-3dB 静态衰减（有界非硬预算），立体声联动同一因子，sat 保持单调；
#  - 原始 stem 与 <120Hz 的 sub/punch 增益不作 EQ；带通非 brickwall，<120Hz
#    仅残留裙边泄漏量级的改变（测试按实测容差断言）；
#  - 预算 a_allow = 0.15*a_low(30-120Hz) - 0.2*a_lm(180-500Hz)（原始 stem 幅度，
#    能量 dB 一律 10*log10）；sosfiltfilt 幅频 = |H|²（无 +3dB 补偿），
#    预算比值两侧用同一滤波器近似抵消。
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


SAT_BUDGET_LOW_HZ = 180.0
SAT_BUDGET_HIGH_HZ = 500.0
SAT_BUDGET_LOWBAND_LO_HZ = 30.0
SAT_BUDGET_LOWBAND_HI_HZ = 120.0
# 以下量值为启发式（未听感验证）：floor 允许新增低中频至低频幅度的 15%，
# 原始低中频每 1 单位幅度扣减 0.2；衰减上限 3dB。
SAT_BUDGET_FLOOR_FRAC = 0.15
SAT_BUDGET_DENSITY_PENALTY = 0.2
SAT_BUDGET_MAX_REDUCTION_DB = 3.0
SAT_BUDGET_MIN_SECONDS = 0.25


def _warm_stage(x, sr, sub_db, punch_db):
    """sub low-shelf + punch bell（与主链同序；主链与预算分析共用）"""
    return _bell(_shelf(x, sr, 30.0, sub_db), sr, 90.0, punch_db, q=1.2)


def _lp200(x, sr):
    return signal.sosfiltfilt(
        signal.butter(2, 200.0, "lowpass", fs=sr, output="sos"), x, padlen=0
    )


def _bandpass(x, sr, lo, hi):
    """二阶 butter 带通 + sosfiltfilt。等效幅频 = |H(f)|^2：带中心增益 1.0、
    带边约 -6dB，这里**不做任何 +3dB 补偿**；预算比较两侧用同一滤波器，
    滤波器增益在比值中近似抵消。"""
    sos = signal.butter(2, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64), padlen=0)


def _band_mean_square(x, sr, lo, hi):
    """带通后每样本均方能量（能量域；dB 一律用 10*log10 转换）"""
    y = _bandpass(x, sr, lo, hi)
    return float(np.mean(y * y))


def compute_sat_budget_gain(x, sr, sub_db=4.0, punch_db=2.0, sat=0.3, drive=1.6,
                            auto_clarity_gains=None,
                            floor_frac=SAT_BUDGET_FLOOR_FRAC,
                            density_penalty=SAT_BUDGET_DENSITY_PENALTY,
                            return_stats=False):
    """立体声联动的饱和低中频预算：返回静态幅度因子 g ∈ [10^(-3/20), 1]。

    仅约束 sat 新增的 180-500Hz 增量（tanh 奇对称 → 主要是 60-120Hz 基频的
    3/5 次奇次谐波）相对原始 stem 的密度：
        a_allow = floor_frac*a_low - density_penalty*a_lm      (截到 >=0)
        trim_db = 10*log10(e_gen / a_allow²)  （= 20*log10 振幅比，能量用
                  10*log10 转换），截到 [0, 3]dB；g = 10^(-trim_db/20)
    a_low/a_lm 为**原始** stem 的 30-120Hz / 180-500Hz 幅度；多声道按能量
    平均（避免反相抵消），全曲共用一个 g。原低中频越浓允许新增越少；
    纯 sub（低中频稀疏）stem 基本不触发。**有界非硬预算**：最多 -3dB，
    不保证把增量压到预算以内。静音/过短(<0.25s)/非有限/sat<=0 一律返回
    1.0（不动）。量值为启发式、未经听感验证。return_stats=True 时返回
    (g, stats) 供测试/诊断，纯函数无副作用。
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    stats = {"a_gen": 0.0, "a_allow": 0.0, "a_low": 0.0, "a_lm": 0.0,
             "trim_db": 0.0, "channels": int(x.shape[1])}
    if (sat <= 0 or len(x) < int(sr * SAT_BUDGET_MIN_SECONDS)
            or not np.isfinite(x).all()):
        return (1.0, stats) if return_stats else 1.0
    e_gen = e_lm = e_low = 0.0
    for c in range(x.shape[1]):
        xc = x[:, c]
        if auto_clarity_gains is not None:
            mud_db, clar_db = (float(auto_clarity_gains[0]), float(auto_clarity_gains[1]))
            if mud_db != 0.0 or clar_db != 0.0:
                xc = apply_auto_clarity_eq(xc, sr, mud_db, clar_db)
        lp = _lp200(_warm_stage(xc, sr, sub_db, punch_db), sr)
        delta = sat * (soft_clip(lp, drive) - lp)
        e_gen += _band_mean_square(delta, sr, SAT_BUDGET_LOW_HZ, SAT_BUDGET_HIGH_HZ)
        e_lm += _band_mean_square(xc, sr, SAT_BUDGET_LOW_HZ, SAT_BUDGET_HIGH_HZ)
        e_low += _band_mean_square(xc, sr, SAT_BUDGET_LOWBAND_LO_HZ, SAT_BUDGET_LOWBAND_HI_HZ)
    n = x.shape[1]
    a_gen = float(np.sqrt(e_gen / n))
    a_low = float(np.sqrt(e_low / n))
    a_lm = float(np.sqrt(e_lm / n))
    a_allow = max(floor_frac * a_low - density_penalty * a_lm, 0.0)
    if a_gen <= 0.0:
        g = 1.0
    elif a_allow <= 0.0:
        g = float(10 ** (-SAT_BUDGET_MAX_REDUCTION_DB / 20.0))
    else:
        trim_db = float(np.clip(10.0 * np.log10((a_gen / a_allow) ** 2),
                                0.0, SAT_BUDGET_MAX_REDUCTION_DB))
        g = float(10 ** (-trim_db / 20.0))
    stats.update(a_gen=a_gen, a_allow=a_allow, a_low=a_low, a_lm=a_lm,
                 trim_db=round(-20.0 * float(np.log10(max(g, 1e-12))), 4))
    return (g, stats) if return_stats else g


def enhance_bass_stem(x, sr, sub_db=4.0, punch_db=2.0, sat=0.3, trans=0.3,
                      drive=1.6, gate_db=-45.0, attack_ms=40.0, release_ms=250.0,
                      auto_clarity_gains=None, sat_budget_gain=None):
    """增强 bass stem。
    sub_db   : sub(30-60Hz) low-shelf 提升，给包裹感
    punch_db : 60-120Hz bell 提升，给鼓 body/punch
    sat      : 谐波饱和混入比例 0-1（仅作用于 sub+low-mid）
    trans    : 瞬态强调强度 0-1
    auto_clarity_gains : None=关闭（默认，行为与旧版位级一致）；否则为
        analyze_auto_clarity() 返回的 (mud_db, clarity_db)，在 sub 增强
        **之前**施加同一组宽频 EQ。立体声联动：两声道必须传同一组增益。
    sat_budget_gain : 饱和低中频预算因子，须 finite 且在 [10^(-3/20), 1]
        （越界截断，非有限抛 ValueError）。None=按本通道信号自动计算（单声道/
        测试用）；float=外部传入的立体声联动因子（main 对两声道传同一
        compute_sat_budget_gain() 结果）。sat=0 或因子=1.0 时与旧版位级一致；
        <1 时仅衰减 sat 新增的 180-500Hz 增量（≤-3dB，非硬预算），原 stem
        不作 EQ，<120Hz 仅裙边泄漏量级改变（带通非 brickwall）。
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

    # ── 1+2. 包裹感与鼓质感（与旧版同序：sub low-shelf + punch bell） ──
    x_warm = _warm_stage(x, sr, sub_db, punch_db)
    # ── 3. 谐波饱和：仅作用于 sub+low-mid（再低通一次避免高频染色） ──
    lp = _lp200(x_warm, sr)
    x_sat = soft_clip(lp, drive)
    x_eff = (1.0 - sat) * lp + sat * x_sat
    # ── 3b. 饱和低中频预算（启发式/未听感验证）：仅衰减 sat 新增的 180-500Hz
    # 增量，静态因子 ≤-3dB（非硬预算），立体声联动时两声道同一 g；g==1.0 或
    # sat=0 完全走旧路径（位级一致）。
    if sat > 0:
        g = sat_budget_gain
        if g is None:
            g = compute_sat_budget_gain(x, sr, sub_db=sub_db, punch_db=punch_db,
                                        sat=sat, drive=drive)
        else:
            g = float(g)
            if not np.isfinite(g):
                raise ValueError(f"sat_budget_gain must be finite, got {sat_budget_gain!r}")
            g = min(max(g, 10 ** (-SAT_BUDGET_MAX_REDUCTION_DB / 20.0)), 1.0)
        if g < 1.0:
            delta_lm = _bandpass(sat * (x_sat - lp), sr,
                                 SAT_BUDGET_LOW_HZ, SAT_BUDGET_HIGH_HZ)
            x_eff = x_eff - (1.0 - g) * delta_lm
    # 高频部分(>200Hz)保持原貌，不参与饱和
    x_eff = x_eff + (x_warm - lp)
    # ── 4. 瞬态强调 ──
    x_eff = _transient(x_eff, sr, trans * 0.8)

    # ── 5. RMS 门控：bass 活跃段才应用增强，静音段保持干净 ──
    win = min(int(sr * 0.05), len(x))  # 短于窗长时截短核，避免 convolve same 变长
    env = np.sqrt(np.convolve(x ** 2, np.ones(win) / (sr * 0.05), mode="same"))
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

    # 饱和低中频预算（v20260909）：全曲只算一次，两声道共用同一静态因子 g
    # （立体声联动，绝不做 per-channel 独立计算）；sat=0 时不启用。
    # 量值经 stdout 报告（启发式、未听感验证）；stage_metadata 接口不变。
    sat_budget = None
    if args.sat > 0:
        sat_budget = compute_sat_budget_gain(
            bass, sr, sub_db=args.sub_db, punch_db=args.punch_db,
            sat=args.sat, auto_clarity_gains=clarity_gains,
        )

    out = np.zeros_like(bass)
    for c in range(bass.shape[1]):
        out[:, c] = enhance_bass_stem(
            bass[:, c], sr,
            sub_db=args.sub_db, punch_db=args.punch_db,
            sat=args.sat, trans=args.trans,
            auto_clarity_gains=clarity_gains,
            sat_budget_gain=sat_budget,
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
    if sat_budget is not None:
        trim_db = min(SAT_BUDGET_MAX_REDUCTION_DB,
                      max(0.0, -20.0 * float(np.log10(max(sat_budget, 1e-12)))))
        trim_txt = f"{trim_db:.2f}dB"
    else:
        trim_txt = "off"
    print(f"Bass-enhanced mix done: {args.out} | sub={args.sub_db}dB punch={args.punch_db}dB "
          f"sat={args.sat} trans={args.trans} "
          f"sat_lmid_trim={trim_txt} (heuristic, not listening-verified) "
          f"auto_clarity={clarity_gains if clarity_gains is not None else 'off'} "
          f"| {sr}Hz {bass.shape[1]}ch")


if __name__ == "__main__":
    main()
