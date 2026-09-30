# premaster_hygiene.py — 母带前卫生滤波：40 Hz 低切 + 20 kHz 低通（引擎无关）
#
# 背景（2026-09-22）：AI 生成素材与上游带宽扩展会在两端造出听感上拿不到回报、
# 却真实占用限制器余量的能量——<40Hz 的次低频隆隆声与 20–22kHz 的合成"空气"。
# 混音工程惯例是进场就把这两端收掉。本阶段在**任何母带引擎之前**做这件事，
# 因此两条链路（无风格/参考独立母带、Soren 风格母带）行为一致。
#
# 两个滤波器的选择：
#   ① 低切：2 阶巴特沃斯高通 + filtfilt（零相位）。次低频的听感价值几乎为零，
#      但峰值得靠限制器压，收掉它等于把余量还给可听频段。零相位会把幅度响应
#      平方（等效 4 阶 24dB/oct），因此设计转角取 0.8014×请求值，使 --low-cut-hz
#      就是**实际 −3dB 点**：40Hz 处 −3dB、25Hz 约 −11dB、20Hz 约 −18dB，而
#      50–100Hz 的贝斯基频只掉 ≤1.4dB（不像 fc=40 的直给会让 50Hz 掉 5dB）。
#      报告里的响应按平方后的实际值记录，不留"名义值好看、实际砍掉一半"的缝。
#   ② 低通：1025 抽头线性相位 FIR（Kaiser 8.6 ≈ 80dB 阻带），20kHz 处 −6dB、
#      19.3kHz 以内 ≤0.05dB、21kHz 以下 ≥60dB。44.1kHz 交付里 20kHz 以上没有
#      合法内容（都是上游生成的），用平顶+陡降把这段合成能量整体摘掉，而不是
#      用一阶斜坡让 21–22kHz 继续漏出去。线性相位、群延迟恰为 (taps−1)/2 个
#      样本，按反射填充后有效卷积补偿，输出与输入样本对齐（不做时移）。
#
# 用法: python premaster_hygiene.py --in premaster.wav --out premaster_hygiene.wav
#         [--low-cut-hz 40] [--lowpass-hz 20000] [--report-json r.json]
import argparse
import math

import numpy as np
import soundfile as sf
from scipy import signal

from audio_validation import finite_range, validate_audio
from stage_metadata import write_report

LOW_CUT_ORDER = 2
# filtfilt 把 2 阶幅度响应平方；(−3dB 点 / 巴特沃斯转角) = (10^0.15 − 1)^0.25
ZERO_PHASE_CORNER_RATIO = 0.8014
LOWPASS_TAPS = 1025
KAISER_BETA = 8.6
LOW_CUT_PROBE_HZ = (20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 100.0)
LOWPASS_PROBE_HZ = (16_000.0, 19_000.0, 20_000.0, 21_000.0, 22_000.0)

# ── 素材自适应低切（2026-09-30，审计 P0-3）─────────────────────────────────
# "低于请求截止"≠"无用能量"：EDM sub / cinematic bass / synth bass 的 31.5Hz
# 可能就是音乐本体。低切点由素材证据裁决，请求值（默认 40Hz）只是上限：
#   ① 能量占比 sub_ratio_db：次声带（10Hz–请求值）相对紧邻基频带（请求值–
#      4×请求值）的能量——占比高说明该段是音乐主体而非残余隆隆声，也反映
#      其对限制器余量的真实占用；
#   ② 包络联动 envelope_correlation：两带 100ms 帧包络（dB 域）的 Pearson
#      相关——音乐性 sub 随编曲起伏，rumble 与音乐无关（≈0）；
#   ③ 静默段泄漏 silent_leak_db：基频带静默帧里次声带相对活跃帧的中位电平
#      ——rumble 整曲漂移（泄漏≈0dB），音乐性 sub 随音乐消失（深负值）。
# 裁决档位（自上而下取第一个满足的；证据不足/素材过短 → 请求值）：
LOW_CUT_MIN_ANALYSIS_SECONDS = 2.0
LOW_CUT_FRAME_SECONDS = 0.1
LOW_CUT_HOP_SECONDS = 0.05
LOW_CUT_ACTIVE_RANGE_DB = 40.0    # 基频带峰值下 40dB 内视为"音乐活跃"帧
LOW_CUT_MIN_ACTIVE_FRAMES = 8
LOW_CUT_MIN_ENVELOPE_STD_DB = 0.5  # 活跃帧包络 std 低于此值视为恒定电平：
                                   # 无起伏可谈"联动"（合成音/持续 drone/数值抖动）
LOW_CUT_MUSICAL_TIERS = (
    # (最小联动, 最小能量占比 dB, 泄漏上限 dB(None=不查), 裁决 Hz；None=不处理)
    (0.50, -20.0, -6.0, None),    # 强音乐性：次声带完整保留，不加滤波
    (0.35, -26.0, -3.0, 27.5),    # 音乐性：25–30Hz 档，31.5Hz 内容 ≤1.2dB 损失
    (0.25, -30.0, None, 35.0),    # 弱联动：30–35Hz 档
)


def low_cut_sos(sample_rate, hz):
    """hz 是请求的 −3dB 点（零相位实际响应），内部换算成巴特沃斯转角。"""
    return signal.butter(LOW_CUT_ORDER, float(hz) * ZERO_PHASE_CORNER_RATIO,
                         btype="highpass", fs=sample_rate, output="sos")


def lowpass_kernel(sample_rate, hz, taps=LOWPASS_TAPS):
    """线性相位低通核；截止不高于 Nyquist 时才存在。"""
    if float(hz) >= sample_rate / 2.0:
        return None
    return signal.firwin(int(taps), float(hz), window=("kaiser", KAISER_BETA),
                         fs=sample_rate)


def _response_db(coefficients, probes_hz, sample_rate, *, sos=False,
                 zero_phase=False):
    """按实际滤波器系数在探针频点求响应（dB）。coefficients=None → 全 None。

    zero_phase=True 表示该滤波器经 filtfilt 施加（幅度响应平方 → 每点翻倍）。"""
    if coefficients is None:
        return {f"{hz:g}": None for hz in probes_hz}
    probes = list(probes_hz)
    evaluate = signal.sosfreqz if sos else signal.freqz
    frequencies, response = evaluate(coefficients, worN=probes, fs=sample_rate)
    factor = 2.0 if zero_phase else 1.0
    return {f"{hz:g}": factor * float(20 * math.log10(max(float(abs(value)), 1e-12)))
            for hz, value in zip(frequencies, response)}


def _frame_envelope_db(mono, sample_rate, sos):
    """带通后按帧取 RMS 并转 dB（100ms 帧 / 50ms 跳）。"""
    pad = min(max(0, len(mono) - 1), int(round(0.5 * sample_rate)))
    filtered = signal.sosfiltfilt(sos, mono, padlen=pad)
    frame = max(1, int(round(LOW_CUT_FRAME_SECONDS * sample_rate)))
    hop = max(1, int(round(LOW_CUT_HOP_SECONDS * sample_rate)))
    starts = range(0, max(0, len(filtered) - frame + 1), hop)
    rms = np.fromiter((np.sqrt(np.mean(filtered[i:i + frame] ** 2)) for i in starts),
                      dtype=np.float64, count=len(starts) if starts else 0)
    if rms.size == 0:
        rms = np.array([np.sqrt(np.mean(filtered ** 2))]) if len(filtered) else np.zeros(1)
    return 20 * np.log10(np.maximum(rms, 1e-12))


def analyze_low_band(audio, sample_rate, requested_hz):
    """次声带（10Hz–请求值）相对基频带（请求值–4×请求值）的音乐性证据。"""
    data = np.asarray(audio, dtype=np.float64)
    mono = data.mean(axis=1) if data.ndim == 2 else data
    nyquist = sample_rate / 2.0 - 1.0
    sub_sos = signal.butter(2, [10.0, min(float(requested_hz), nyquist)],
                            btype="bandpass", fs=sample_rate, output="sos")
    ref_sos = signal.butter(2, [min(float(requested_hz), nyquist),
                                min(4.0 * float(requested_hz), nyquist)],
                            btype="bandpass", fs=sample_rate, output="sos")
    sub_db = _frame_envelope_db(mono, sample_rate, sub_sos)
    ref_db = _frame_envelope_db(mono, sample_rate, ref_sos)
    pad = min(max(0, len(mono) - 1), int(round(0.5 * sample_rate)))
    sub_full = signal.sosfiltfilt(sub_sos, mono, padlen=pad)
    ref_full = signal.sosfiltfilt(ref_sos, mono, padlen=pad)
    sub_energy = float(np.mean(sub_full ** 2))
    ref_energy = float(np.mean(ref_full ** 2))
    analysis = {
        "requested_hz": float(requested_hz),
        "sub_ratio_db": (10 * math.log10(sub_energy / ref_energy)
                         if sub_energy > 1e-30 and ref_energy > 1e-30 else -300.0),
        "envelope_correlation": None,
        "silent_leak_db": None,
        "active_frames": 0,
        "silent_frames": 0,
        "duration_seconds": float(len(mono) / sample_rate),
    }
    if ref_db.size < LOW_CUT_MIN_ACTIVE_FRAMES:
        return analysis
    active = ref_db > float(np.max(ref_db)) - LOW_CUT_ACTIVE_RANGE_DB
    analysis["active_frames"] = int(np.count_nonzero(active))
    analysis["silent_frames"] = int(np.count_nonzero(~active))
    if analysis["active_frames"] >= LOW_CUT_MIN_ACTIVE_FRAMES:
        a, b = sub_db[active], ref_db[active]
        if (float(np.std(a)) >= LOW_CUT_MIN_ENVELOPE_STD_DB
                and float(np.std(b)) >= LOW_CUT_MIN_ENVELOPE_STD_DB):
            analysis["envelope_correlation"] = float(np.corrcoef(a, b)[0, 1])
        else:
            # 恒定电平（合成测试音/持续 drone）：无起伏证据，联动记 0
            analysis["envelope_correlation"] = 0.0
    if analysis["silent_frames"] >= 4 and analysis["active_frames"] >= 4:
        analysis["silent_leak_db"] = (float(np.median(sub_db[~active]))
                                      - float(np.median(sub_db[active])))
    return analysis


def choose_low_cut_hz(audio, sample_rate, requested_hz):
    """素材自适应低切裁决：返回 (chosen_hz, analysis)。

    chosen ∈ {0.0(不处理), 27.5, 35.0, requested}，恒 ≤ requested；
    0.0 为假值，与 apply_hygiene / CLI 的"未启用"语义直接兼容。
    素材过短或频率分辨不足时保守沿用请求值。
    """
    data = np.asarray(audio, dtype=np.float64)
    frames = data.shape[0] if data.ndim >= 1 else 0
    if not requested_hz or frames / sample_rate < LOW_CUT_MIN_ANALYSIS_SECONDS:
        return float(requested_hz or 0.0), {"skipped": "insufficient_material",
                                            "requested_hz": float(requested_hz or 0.0)}
    analysis = analyze_low_band(data, sample_rate, requested_hz)
    chosen = float(requested_hz)
    correlation = analysis["envelope_correlation"]
    leak = analysis["silent_leak_db"]
    if correlation is not None:
        for min_corr, min_ratio_db, max_leak_db, tier_hz in LOW_CUT_MUSICAL_TIERS:
            if correlation < min_corr or analysis["sub_ratio_db"] < min_ratio_db:
                continue
            if max_leak_db is not None and (leak is None or leak > max_leak_db):
                continue
            chosen = 0.0 if tier_hz is None else min(float(tier_hz), float(requested_hz))
            analysis["tier"] = {"min_correlation": min_corr, "chosen_hz": chosen}
            break
    analysis["chosen_hz"] = chosen
    return chosen, analysis


def apply_hygiene(audio, sample_rate, *, low_cut_hz=None, lowpass_hz=None):
    """返回处理后的 [frames, channels] float64；未启用的那一路原样保留。"""
    result = np.asarray(audio, dtype=np.float64)
    if low_cut_hz:
        sos = low_cut_sos(sample_rate, low_cut_hz)
        # 40Hz 滤波器的冲激响应长度远超 filtfilt 默认填充，填充不足会在文件
        # 首尾留下阶跃瞬态；按半秒反射填充把瞬态推到数据之外。
        pad = min(max(0, len(result) - 1), int(round(0.5 * sample_rate)))
        result = signal.sosfiltfilt(sos, result, axis=0, padlen=pad)
    if lowpass_hz:
        kernel = lowpass_kernel(sample_rate, lowpass_hz)
        if kernel is not None:
            delay = (len(kernel) - 1) // 2
            padded = np.pad(result, ((delay, len(kernel) - 1 - delay), (0, 0)),
                            mode="reflect")
            result = signal.fftconvolve(padded, kernel[:, None], mode="valid", axes=0)
    return result


def main():
    parser = argparse.ArgumentParser(description="母带前卫生滤波：低切 + 低通")
    parser.add_argument("--in", dest="in_wav", required=True)
    parser.add_argument("--out", dest="out_wav", required=True)
    parser.add_argument("--low-cut-hz", type=float, default=None,
                        help="低切 -3dB 点（Hz，零相位 2 阶巴特沃斯）；省略=不做低切")
    parser.add_argument("--lowpass-hz", type=float, default=None,
                        help="低通截止（Hz，线性相位 FIR）；省略=不做低通")
    parser.add_argument("--report-json")
    args = parser.parse_args()

    audio, sr = sf.read(args.in_wav, dtype="float64", always_2d=True)
    validate_audio(audio, name="premaster", sr=sr, channels=2)
    requested_low_cut = (finite_range(args.low_cut_hz, "low_cut_hz", 10.0, 500.0)
                         if args.low_cut_hz is not None else None)
    lowpass_hz = (finite_range(args.lowpass_hz, "lowpass_hz", 1000.0, sr / 2.0 - 1.0)
                  if args.lowpass_hz is not None else None)
    # 素材自适应：请求值只是上限，音乐性次声带证据可以把低切降级或不处理
    low_cut_hz, low_cut_analysis = (choose_low_cut_hz(audio, sr, requested_low_cut)
                                    if requested_low_cut else (None, None))

    out = apply_hygiene(audio, sr, low_cut_hz=low_cut_hz, lowpass_hz=lowpass_hz)
    if not np.isfinite(out).all():
        raise RuntimeError("premaster hygiene produced non-finite samples")

    peak = float(np.abs(out).max()) if out.size else 0.0
    scale = 1.0
    if peak > 0.999:
        scale = 0.999 / peak
        out = out * scale
        print(f"  全局峰值缩放: {20 * math.log10(scale):+.2f}dB（静态缩放，保持相对比例）")

    sf.write(args.out_wav, out.astype(np.float32), sr, subtype="FLOAT")
    if low_cut_hz:
        suffix = (f"（素材裁决：请求 {requested_low_cut:g} → {low_cut_hz:g}）"
                  if low_cut_analysis and low_cut_hz != requested_low_cut else "")
        print(f"  低切: {low_cut_hz:g} Hz（-3dB 点，零相位 2 阶巴特沃斯）{suffix}")
    elif requested_low_cut:
        print(f"  低切: 素材裁决不处理（请求 {requested_low_cut:g} Hz，音乐性次声带完整保留）")
    else:
        print("  低切: 未启用")
    print(f"  低通: {lowpass_hz:g} Hz（{LOWPASS_TAPS} 抽头线性相位 FIR）" if lowpass_hz
          else "  低通: 未启用")
    if args.report_json:
        sos = low_cut_sos(sr, low_cut_hz) if low_cut_hz else None
        kernel = lowpass_kernel(sr, lowpass_hz) if lowpass_hz else None
        write_report(args.report_json, stage="hygiene", scale=scale,
                     input_path=args.in_wav, output_path=args.out_wav,
                     extra={
                         "low_cut": {"hz": low_cut_hz, "requested_hz": requested_low_cut,
                                     "order": LOW_CUT_ORDER,
                                     "zero_phase": True,
                                     "butter_corner_hz": (low_cut_hz * ZERO_PHASE_CORNER_RATIO
                                                          if low_cut_hz else None),
                                     "applied": sos is not None,
                                     "analysis": low_cut_analysis,
                                     "response_db": _response_db(sos, LOW_CUT_PROBE_HZ, sr,
                                                                 sos=True, zero_phase=True)},
                         "lowpass": {"hz": lowpass_hz, "taps": LOWPASS_TAPS,
                                     "applied": kernel is not None,
                                     "response_db": _response_db(kernel, LOWPASS_PROBE_HZ, sr)},
                         "note": ("引擎无关的母带前滤波；响应按实际施加方式（含零相位平方）计算；"
                                  "低切点为素材自适应裁决，requested_hz 为请求上限"),
                     })
    print(f"  输出: {args.out_wav}")


if __name__ == "__main__":
    main()
