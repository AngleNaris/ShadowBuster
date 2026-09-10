# soundstage_reshape.py — 声场重塑（管线阶段版：broadband delta-add 分轨空间处理）
# 架构：out = in_mix + wet × Σ (processed_i − stem_i)
#   - 以输入混音为基底，每条轨只把自己的"处理差值"加回；
#   - wet=0 时仅旁路声场拓宽；降噪为 0 时输出才与输入逐样本一致；
#   - 分离残差 (mix − Σstems) 随基底原样保留，不丢"胶感"。
# 新增宽度按频段/时间窗约束，原始 Mid/Side 保留；分离伪影仍需试听检查。
# drums 的新增宽度在攻击段收紧，other 可独立进行高频降噪。
# bass/vocals 不直接拓宽；低频不生成新增宽度。
# 用法（stems 由主管线 stage_demucs 预先产出）:
#   python soundstage_reshape.py --in-mix premix.wav --out-wav out.wav \
#       --stems-dir <htdemucs 输出下含 drums/other 的目录> \
#       [--wet 0.6] [--other-denoise-amount 0.2]
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal, ndimage

from audio_validation import validate_audio, validate_audio_pair, finite_range
from stage_metadata import write_report

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
    if side_high_db == 0 and side_gain_db == 0:
        return x.copy()
    left, right = x[:, 0], x[:, 1]
    mid, side = (left + right) / 2.0, (left - right) / 2.0
    side = _high_shelf(side, sr, side_high_fc, side_high_db)
    if side_gain_db:
        side = side * (10 ** (side_gain_db / 20.0))
    return np.column_stack((mid + side, mid - side))


def _spectral_denoise(x, sr, fc=10000.0, amount=0.2, thr_ratio=1.5):
    """频带降噪（Audition 噪声采样式，噪声地板自动估计）。

    每个 bin 沿时间轴取 P15 = 噪声地板估计（稳态沙沙一直存在所以贴地板，
    音乐瞬态偶尔出现所以分位数不受影响）。门限曲线与 Audition 对齐：
    幅度 ≤ 地板（纯噪声）→ 衰减满 amount（20% ≈ -1.9dB）；
    幅度 ≥ 1.5×地板（音乐瞬态）→ 原样通过。仅作用 fc 以上频段。
    amount=0 恒等。
    """
    if amount <= 0:
        return x
    if not np.isfinite(amount) or not 0.0 <= amount <= 1.0:
        raise ValueError("denoise amount must be finite and within [0, 1]")
    if not np.isfinite(fc) or not 0.0 < fc < sr / 2.0:
        raise ValueError("denoise cutoff must be finite and below Nyquist")
    if not np.isfinite(thr_ratio) or thr_ratio <= 1.0:
        raise ValueError("denoise threshold ratio must be finite and greater than 1")
    if len(x) == 0:
        return x.copy()

    def _local_percentile_floor(magnitude, window_frames):
        """在稀疏时间锚点估计局部 P15，再线性插值到每个 STFT 帧。"""
        frame_count = magnitude.shape[1]
        if frame_count == 1:
            return magnitude.copy()
        step = max(1, window_frames // 2)
        anchors = np.arange(0, frame_count, step, dtype=int)
        if anchors[-1] != frame_count - 1:
            anchors = np.append(anchors, frame_count - 1)
        half = window_frames // 2
        anchor_floors = np.column_stack([
            np.percentile(
                magnitude[:, max(0, anchor - half):min(frame_count, anchor + half + 1)],
                15,
                axis=1,
            )
            for anchor in anchors
        ])
        frames = np.arange(frame_count)
        right = np.searchsorted(anchors, frames, side="left")
        right = np.clip(right, 0, len(anchors) - 1)
        left = np.maximum(right - 1, 0)
        span = anchors[right] - anchors[left]
        weight = np.divide(
            frames - anchors[left],
            span,
            out=np.zeros(frame_count, dtype=float),
            where=span > 0,
        )
        return (
            anchor_floors[:, left] * (1.0 - weight)
            + anchor_floors[:, right] * weight
        )

    nperseg = min(4096, len(x))
    if nperseg < 2:
        return x.copy()
    noverlap = min(3072, nperseg - 1)
    hop = nperseg - noverlap
    channels = [
        x[:, c] for c in range(x.shape[1])
    ] if x.ndim == 2 else [x]
    first_f, _, first_spectrum = signal.stft(
        channels[0], sr, nperseg=nperseg, noverlap=noverlap)
    spectra = [first_spectrum] + [
        signal.stft(ch, sr, nperseg=nperseg, noverlap=noverlap)[2]
        for ch in channels[1:]
    ]
    f = first_f
    high = f >= fc
    if not high.any():
        return x.copy()

    linked_mag = np.sqrt(np.mean([np.abs(Z) ** 2 for Z in spectra], axis=0))
    frame_rate = sr / hop
    floor_frames = max(3, int(round(3.0 * frame_rate)))
    sub = linked_mag[high]
    floor = _local_percentile_floor(sub, floor_frames)
    ratio = sub / (floor + 1e-12)
    likelihood = np.clip((thr_ratio - ratio) / (thr_ratio - 1.0), 0.0, 1.0)
    gain = 1.0 - amount * likelihood
    smooth_fc = min(8.0, frame_rate * 0.45)
    if gain.shape[1] > 1 and smooth_fc > 0:
        sos = signal.butter(2, smooth_fc, "lowpass", fs=frame_rate, output="sos")
        zi = signal.sosfilt_zi(sos)[:, None, :] * gain[:, 0][None, :, None]
        gain, _ = signal.sosfilt(sos, gain, axis=1, zi=zi)
    gain = np.clip(gain, 1.0 - amount, 1.0)

    outs = []
    for Z in spectra:
        Zg = Z.copy()
        Zg[high] *= gain
        _, channel = signal.istft(Zg, sr, nperseg=nperseg, noverlap=noverlap)
        if len(channel) < len(x):
            channel = np.pad(channel, (0, len(x) - len(channel)))
        outs.append(channel[:len(x)])
    y = np.column_stack(outs) if x.ndim == 2 else outs[0]

    if not np.isfinite(y).all():
        raise RuntimeError("spectral denoise produced non-finite samples")
    return y.astype(x.dtype)


def _reshape_bass(x, sr, sub_db, sub_fc):
    """bass 轨低频补偿：全频段低 shelf（包在 delta-add 里，归零恒等）。"""
    return _low_shelf(x, sr, sub_fc, sub_db)


# 拓宽增量（delta）side 的低频保护：只在最底部温和收束新增宽度，保留鼓/Bass低中频包裹感
DELTA_SIDE_LF_PROTECT_FC = 70.0
DELTA_SIDE_LF_PROTECT_DB = -2.0


def _protect_widen_delta(delta, sr, fc=None, shelf_db=None):
    """对新增（delta）side 做温和低频保护：低架衰减 fc 以下的拓宽增量。

    只过滤处理差值的 side 分量（mid 增量原样保留，纯 reshape 路径恒为 0），
    原始 side 不参与；delta 全零（wet=0 或宽度=0）时精确恒等。
    """
    fc = DELTA_SIDE_LF_PROTECT_FC if fc is None else fc
    shelf_db = DELTA_SIDE_LF_PROTECT_DB if shelf_db is None else shelf_db
    if not np.any(delta):
        return delta
    delta_mid = (delta[:, 0] + delta[:, 1]) / 2.0
    delta_side = (delta[:, 0] - delta[:, 1]) / 2.0
    delta_side = _low_shelf(delta_side, sr, fc, shelf_db)
    return np.column_stack((delta_mid + delta_side, delta_mid - delta_side))


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


WIDTH_BANDS = ((120, 300, 0.10, 1.0), (300, 2000, 0.35, 2.0),
               (2000, 8000, 0.55, 3.0), (8000, 24000, 0.45, 2.0))


def drum_width_envelope(stem, sr):
    """Linked attack protection; an envelope estimate, not room-source separation."""
    if len(stem) == 0:
        return np.zeros(0)
    power = np.mean(np.asarray(stem, dtype=np.float64) ** 2, axis=1)
    def envelope(ms):
        pole = np.exp(-1.0 / (sr * ms / 1000))
        return signal.lfilter([1 - pole], [1, -pole], power)
    ratio = 10 * np.log10((envelope(4) + 1e-14) / (envelope(70) + 1e-14))
    amount = np.clip((ratio - 3) / 9, 0, 1)
    gain = 1 - 0.75 * amount
    return ndimage.uniform_filter1d(gain, max(1, int(sr * .003)), mode='nearest')


def constrain_width_delta(mix, delta, sr):
    """Bound added Side against the final mix in overlapping time/frequency windows.

    Caps are analysis-domain budgets, not perceptual width units. Reconstruction
    can change window energies slightly; existing over-wide content is retained.
    """
    n = len(mix)
    report = {'bands': [], 'analysis': '4096 Hann / 75% overlap'}
    if n < 32 or not np.any(delta):
        return np.zeros_like(delta), report
    size = min(4096, n)
    overlap = size * 3 // 4
    mid = np.asarray(mix, dtype=np.float64).mean(axis=1)
    side = (mix[:, 0] - mix[:, 1]) * .5
    added = (delta[:, 0] - delta[:, 1]) * .5
    def stft(x):
        return signal.stft(x, sr, nperseg=size, noverlap=overlap,
                           boundary='zeros', padded=True)
    f, _, m = stft(mid)
    _, _, s = stft(side)
    _, _, d = stft(added)
    # No generated width below 120 Hz; cosine transition avoids a hard step.
    highpass = .5 - .5 * np.cos(np.pi * np.clip((f - 120) / 120, 0, 1))
    d *= highpass[:, None]
    gain = np.ones_like(d.real)
    for lo, hi, cap, relative_db in WIDTH_BANDS:
        mask = (f >= lo) & (f < hi)
        if not np.any(mask):
            continue
        em = np.sum(abs(m[mask]) ** 2, axis=0)
        es = np.sum(abs(s[mask]) ** 2, axis=0)
        ed = np.sum(abs(d[mask]) ** 2, axis=0)
        cross = np.sum((s[mask].conj() * d[mask]).real, axis=0)
        maximum = np.minimum(em * cap / (1 - cap), es * 10 ** (relative_db / 10))
        budget = np.maximum(maximum - es, 0)
        root = np.sqrt(np.maximum(cross * cross + ed * budget, 0))
        alpha = np.clip((-cross + root) / np.maximum(ed, 1e-30), 0, 1)
        alpha[(es >= em * cap / (1 - cap)) | (es < 1e-16)] = 0
        alpha[ed < 1e-30] = 0
        # Conservative look-around smoothing never exceeds a frame's safe gain.
        limited = ndimage.minimum_filter1d(alpha, size=5, mode='nearest')
        alpha = np.minimum(alpha, ndimage.uniform_filter1d(limited, size=5, mode='nearest'))
        gain[mask] = alpha
        report['bands'].append({'low_hz': lo, 'high_hz': min(hi, sr / 2),
                                'max_side_fraction': cap, 'max_growth_db': relative_db,
                                'mean_admitted': float(np.mean(alpha))})
    _, restored = signal.istft(d * gain, sr, nperseg=size,
                               noverlap=overlap, input_onesided=True, boundary=True)
    restored = restored[:n]
    return np.column_stack((restored, -restored)), report


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
    if len(mo) != len(mo2):
        raise ValueError("width report length mismatch")
    dr = 10 * np.log10(((mo2 ** 2).mean() + 1e-12) / ((mo ** 2).mean() + 1e-12))
    corr = np.corrcoef(mo, mo2)[0, 1]
    print(f"  mono fold-down 能量变化 {dr:+.2f}dB（相位抵消检查），与原 mono 相关 {corr:.4f}")


def resolve_side_gains(mode, override_db=None):
    """解析各轨 (shelf_db, shelf_fc, side_gain_db)；override_db 只替换 side 增益。

    宽度上限语义：other 轨取设定值，drums 轨固定取一半（与 broadband 预设的
    3.0/1.5 比例一致），避免鼓被拉散。shelf 参数仍来自模式预设。
    """
    preset = MODES[mode]
    params = {"other": preset["other"], "drums": preset["drums"]}
    if override_db is not None:
        if not np.isfinite(override_db) or not 0.0 <= override_db <= 12.0:
            raise ValueError("side gain must be finite and within [0, 12] dB")
        override_db = float(override_db)
        params["other"] = (preset["other"][0], preset["other"][1], override_db)
        params["drums"] = (preset["drums"][0], preset["drums"][1], override_db * 0.5)
    return params


def main():
    ap = argparse.ArgumentParser(description="delta-add 声场重塑（管线内阶段：stems 由 stage_demucs 预先产出）")
    ap.add_argument("--in-mix", required=True, type=Path, help="输入混音（管线上一阶段产物或原曲）")
    ap.add_argument("--out-wav", required=True, type=Path)
    ap.add_argument("--stems-dir", required=True, type=Path, help="demucs 4-stem 输出目录（含 drums/other.wav）")
    ap.add_argument("--mode", choices=list(MODES), default="broadband",
                    help="broadband=纯side增益（默认，音色最保真）| shelf3k | shelf-air | dynamic")
    ap.add_argument("--side-gain-db", type=float, default=None,
                    help="宽度上限 dB：覆盖模式预设的 side 增益（other=设定值，drums=一半）")
    ap.add_argument("--wet", type=float, default=1.0, help="干湿比 0-1：缩放全部处理差值，0=与输入逐样本一致")
    ap.add_argument("--other-denoise-amount", type=float, default=0.0,
                    help="other 轨 ≥fc 噪声地板降噪量 0-1（Audition 降噪量语义，贴地板 -amount*100%%）")
    ap.add_argument("--other-denoise-fc", type=float, default=10000.0)
    ap.add_argument("--report-json", type=Path, default=None)
    args = ap.parse_args()

    if not np.isfinite(args.wet) or not 0.0 <= args.wet <= 1.0:
        ap.error("--wet must be finite and within [0, 1]")
    if args.side_gain_db is not None and (
            not np.isfinite(args.side_gain_db) or not 0.0 <= args.side_gain_db <= 12.0):
        ap.error("--side-gain-db must be finite and within [0, 12]")
    if not np.isfinite(args.other_denoise_amount) or not 0.0 <= args.other_denoise_amount <= 1.0:
        ap.error("--other-denoise-amount must be finite and within [0, 1]")
    if not np.isfinite(args.other_denoise_fc) or not 0.0 < args.other_denoise_fc < SR / 2.0:
        ap.error(f"--other-denoise-fc must be finite and within (0, {SR / 2:.0f})")

    in_mix = args.in_mix.resolve()
    stem_dir = Path(args.stems_dir).resolve()
    missing = [n for n in ("drums.wav", "other.wav") if not (stem_dir / n).exists()]
    if missing:
        raise SystemExit(f"stems 缺失: {missing} (stems-dir={stem_dir})")

    mix, sr = sf.read(in_mix, always_2d=True, dtype="float64")
    try:
        validate_audio(mix, name="in-mix", sr=sr, require_sr=SR, channels=2)
    except ValueError as exc:
        ap.error(str(exc))

    params = resolve_side_gains(args.mode, args.side_gain_db)
    out = mix.copy()
    width_delta = np.zeros_like(mix)
    denoise_delta = np.zeros_like(mix)
    wet = args.wet
    for name, (db, fc, gain) in params.items():
        stem, s_sr = sf.read(stem_dir / f"{name}.wav", always_2d=True, dtype="float64")
        if s_sr != sr:
            raise SystemExit(f"{name} sample rate {s_sr} != {sr}")
        try:
            validate_audio_pair(stem, out, s_sr, primary_name=name, secondary_name="in-mix")
        except ValueError as exc:
            raise SystemExit(str(exc))
        n = len(stem)
        if args.mode == "dynamic" and db > 0:
            reshaped = _dynamic_side_shelf(stem, sr, fc, db)
        else:
            reshaped = _reshape_stem(stem, sr, db, fc, gain)
        delta = (reshaped - stem) * wet
        if name == 'drums':
            delta *= drum_width_envelope(stem, sr)[:, None]
        delta = _protect_widen_delta(delta, sr)
        width_delta += delta
        processed = stem + delta
        if name == "other" and args.other_denoise_amount > 0:
            cleaned = _spectral_denoise(processed, sr, args.other_denoise_fc,
                                       args.other_denoise_amount)
            denoise_delta += cleaned - processed
            print(f"  other: ≥{args.other_denoise_fc:.0f}Hz 噪声地板降噪 {args.other_denoise_amount*100:.0f}%")
        print(f"  {name}: shelf +{db}dB@{fc:.0f}Hz, side gain +{gain}dB"
              + ("  [动态门]" if args.mode == "dynamic" and db > 0 else ""))
    base = mix + denoise_delta
    accepted, budget = constrain_width_delta(base, width_delta, sr)
    out = base + accepted
    print(f"  自适应宽度预算: {budget}")
    if wet != 1.0:
        print(f"  声场干湿比 wet={wet:.2f} (0=不拓宽, 1=全量)")

    if not np.isfinite(out).all():
        raise RuntimeError("soundstage reshape produced non-finite samples")

    # 峰值保护：与 bass/drum_enhance 同约定，-0.5dB 给母带留余量
    peak = np.abs(out).max() if out.size else 0.0
    if peak > 0.999:
        scale = 0.999 / peak
        out *= scale
        print(f"  全局峰值缩放: {20*np.log10(scale):+.2f}dB（静态缩放，不改变同一 mix 的相对比例；不是自动 limiter）")
    sf.write(args.out_wav, out.astype(np.float32), sr, subtype="FLOAT")
    if args.report_json:
        write_report(args.report_json, stage="reshape", scale=float(scale) if peak > 0.999 else 1.0,
                     input_path=args.in_mix, output_path=args.out_wav)
    print(f"  输出: {args.out_wav}")

    print("宽度报告:")
    width_report(mix, out, sr)


if __name__ == "__main__":
    main()
