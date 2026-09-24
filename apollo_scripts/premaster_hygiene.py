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
    low_cut_hz = (finite_range(args.low_cut_hz, "low_cut_hz", 10.0, 500.0)
                  if args.low_cut_hz is not None else None)
    lowpass_hz = (finite_range(args.lowpass_hz, "lowpass_hz", 1000.0, sr / 2.0 - 1.0)
                  if args.lowpass_hz is not None else None)

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
    print(f"  低切: {low_cut_hz:g} Hz（-3dB 点，零相位 2 阶巴特沃斯）" if low_cut_hz
          else "  低切: 未启用")
    print(f"  低通: {lowpass_hz:g} Hz（{LOWPASS_TAPS} 抽头线性相位 FIR）" if lowpass_hz
          else "  低通: 未启用")
    if args.report_json:
        sos = low_cut_sos(sr, low_cut_hz) if low_cut_hz else None
        kernel = lowpass_kernel(sr, lowpass_hz) if lowpass_hz else None
        write_report(args.report_json, stage="hygiene", scale=scale,
                     input_path=args.in_wav, output_path=args.out_wav,
                     extra={
                         "low_cut": {"hz": low_cut_hz, "order": LOW_CUT_ORDER,
                                     "zero_phase": True,
                                     "butter_corner_hz": (low_cut_hz * ZERO_PHASE_CORNER_RATIO
                                                          if low_cut_hz else None),
                                     "applied": sos is not None,
                                     "response_db": _response_db(sos, LOW_CUT_PROBE_HZ, sr,
                                                                 sos=True, zero_phase=True)},
                         "lowpass": {"hz": lowpass_hz, "taps": LOWPASS_TAPS,
                                     "applied": kernel is not None,
                                     "response_db": _response_db(kernel, LOWPASS_PROBE_HZ, sr)},
                         "note": "引擎无关的母带前滤波；响应按实际施加方式（含零相位平方）计算",
                     })
    print(f"  输出: {args.out_wav}")


if __name__ == "__main__":
    main()
