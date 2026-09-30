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
from noise_profile import (NOISE_MODES, adaptive_denoise, constrain_noise_delta,
                           validate_noise_options)

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
    # 短输入限制重叠比例：noverlap=nperseg-1 会让 hop=1、帧数随样本数线性膨胀。
    noverlap = min(3072, nperseg * 3 // 4, nperseg - 1)
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


# 宽度保护（2026-09-30 裁决，与 mastering/soundstage.py widen() 的相位 guard
# 同语义）：增长量由用户的宽度请求授权（constrain_width_delta 的 growth_db）；
# 唯一固定保护是**相位翻转边界**——分析带内 Side 能量不得被本次处理新推到超过
# Mid 能量（占比 0.50，等价于局部 correlation 被推负）。已越过边界的素材
# 原样保留（部分歌曲本就存在单声道内容/超宽混音），不收窄也不再增宽。
# 旧分带单声道兼容占比上限（0.40/0.60/0.75/0.70）同日废弃：mono 兼容性
# 不是缺陷，保护不得抑制用户的调整意志。
WIDTH_BANDS = ((120, 300, 0.50), (300, 2000, 0.50), (2000, 8000, 0.50), (8000, 24000, 0.50))


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


def constrain_width_delta(mix, delta, sr, growth_db=6.0):
    """Bound added Side against the final mix in overlapping time/frequency windows.

    growth_db is the user-authorized Side energy growth (10**(growth_db/10)),
    not a fixed cap: the request itself is what admits headroom. The only
    fixed guard is the per-band phase-inversion boundary in WIDTH_BANDS
    (side energy must not be newly pushed above mid energy; already-inverted
    content is retained untouched). Caps are analysis-domain budgets, not
    perceptual width units. Reconstruction can change window energies
    slightly. The returned delta is side-only by construction: any mid
    component of the input delta is discarded, and bands whose existing mix
    side is silent admit no new width.
    """
    if not np.isfinite(growth_db) or not 0.0 <= growth_db <= 12.0:
        raise ValueError("growth_db must be finite and within [0, 12]")
    n = len(mix)
    report = {'bands': [], 'analysis': '4096 Hann / 75% overlap',
              'authorized_growth_db': float(growth_db)}
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
    growth = 10.0 ** (growth_db / 10.0)
    for lo, hi, cap in WIDTH_BANDS:
        mask = (f >= lo) & (f < hi)
        if not np.any(mask):
            continue
        em = np.sum(abs(m[mask]) ** 2, axis=0)
        es = np.sum(abs(s[mask]) ** 2, axis=0)
        ed = np.sum(abs(d[mask]) ** 2, axis=0)
        cross = np.sum((s[mask].conj() * d[mask]).real, axis=0)
        maximum = np.minimum(em * cap / (1 - cap), es * growth)
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
                                'max_side_fraction': cap, 'max_growth_db': growth_db,
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


# ── 空间去拥挤（开发规格 v2 §7.4，P3-04）────────────────────────────────
# 声场强度 a 可授权对 other 轨 200-700Hz 的持续性伴奏重叠做小幅整理：
#   实际衰减 ≤ 1.5 × a dB，且只在"有明确证据"时动作（频段持续占用 + 相对
#   全曲该频段能量足够高），无证据不动——不为使声场旋钮"永远有效"而强行
#   挖空伴奏。与人声去掩蔽划分频段所有权：人声主导区（≥1kHz 掩蔽带）的
#   伴奏退让由 vocal_adjust 的 1.5/3kHz 掩蔽 EQ 负责，本模块只动 200-700Hz
#   的中低频背景重叠，不重复做第二套 duck。a=0 → 精确恒等；独立于宽度
#   （W=0、a>0 时允许轻微去拥挤）。
SPATIAL_UNMASK_LO_HZ = 200.0
SPATIAL_UNMASK_HI_HZ = 700.0
SPATIAL_UNMASK_DEPTH_DB_AT_SPACE_ONE = 1.5
SPATIAL_UNMASK_FRAME_MS = 100.0      # 证据帧长：持续占用须跨多帧
SPATIAL_UNMASK_HOLD_MS = 400.0       # 证据保持：短暂起伏不闪烁
SPATIAL_UNMASK_RELEASE_MS = 800.0    # 证据释放：缓慢退出，不做节奏门
SPATIAL_UNMASK_EVIDENCE_RATIO_DB = 3.0   # 帧能量高于基准 3dB 起算证据
SPATIAL_UNMASK_EVIDENCE_KNEE_DB = 9.0    # 软转折宽度（3→12dB 线性上升）
SPATIAL_UNMASK_REF_PCT = 25.0            # 基准 = P25（局部稳健统计）：整曲多数
                                         # 时间都拥挤（无对比）→ 基准即拥挤电平
                                         # → 无"局部堆积"证据，符合 §7.4"无明确
                                         # 问题不动作"
SPATIAL_UNMASK_MIN_SECONDS = 1.0         # 更短不下判断
SPATIAL_UNMASK_EPS = 1e-24


def _spatial_unmask_gain_curve(band_power, sr):
    """200-700Hz 帧能量 → 证据包络 ∈ [0,1]（帧域，全曲联动）。"""
    frames = len(band_power)
    if frames == 0:
        return np.zeros(0)
    ref = float(np.percentile(band_power, SPATIAL_UNMASK_REF_PCT))
    if ref <= SPATIAL_UNMASK_EPS:
        # 基准静音（频段几乎全空）而局部有能量：仍以相对量评估，
        # 用非零帧的 P25 兜底，避免全曲中位≈0 时把任何瞬时当堆积。
        nonzero = band_power[band_power > SPATIAL_UNMASK_EPS]
        if not len(nonzero):
            return np.zeros(frames)
        ref = float(np.percentile(nonzero, SPATIAL_UNMASK_REF_PCT))
    ratio_db = 10.0 * np.log10((band_power + SPATIAL_UNMASK_EPS) / (ref + SPATIAL_UNMASK_EPS))
    evidence = np.clip((ratio_db - SPATIAL_UNMASK_EVIDENCE_RATIO_DB)
                       / SPATIAL_UNMASK_EVIDENCE_KNEE_DB, 0.0, 1.0)
    hop_s = SPATIAL_UNMASK_FRAME_MS / 1000.0
    hold = max(1, int(SPATIAL_UNMASK_HOLD_MS / SPATIAL_UNMASK_FRAME_MS))
    release = 1.0 - np.exp(-hop_s / (SPATIAL_UNMASK_RELEASE_MS / 1000.0))
    out = np.empty(frames)
    acc = 0.0
    held = np.zeros(frames, dtype=int)
    countdown = 0
    for i in range(frames):
        if evidence[i] > acc:
            countdown = hold          # 重置保持窗
            acc = evidence[i]
        elif countdown > 0:
            countdown -= 1
        else:
            acc += release * (evidence[i] - acc)
        held[i] = countdown
        out[i] = acc
    return out


def spatial_unmask_other(stem, sr, space_amount, report=None):
    """other 轨 200-700Hz 有界去拥挤（声场强度 a 授权，规格 §7.4）。

    仅缩放带通分量（零相位提取后精确重组，带外逐样本不变）；衰减深度
    ≤ 1.5 × a dB 且乘证据包络（持续占用才动作）。a<=0 或无证据 → 精确
    返回原数组（位级不变，dtype 保留）。
    """
    amount = 0.0 if space_amount is None else float(space_amount)
    if amount <= 0.0 or len(stem) == 0:
        if report is not None:
            report.update(applied=False, auth=round(amount, 4),
                          depth_cap_db=round(
                              SPATIAL_UNMASK_DEPTH_DB_AT_SPACE_ONE * amount, 4),
                          reason="disabled" if amount <= 0.0 else "empty")
        return stem
    if len(stem) < int(sr * SPATIAL_UNMASK_MIN_SECONDS):
        if report is not None:
            report.update(applied=False, auth=round(amount, 4),
                          depth_cap_db=round(
                              SPATIAL_UNMASK_DEPTH_DB_AT_SPACE_ONE * amount, 4),
                          reason="insufficient_duration")
        return stem
    x = np.asarray(stem, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    depth_cap = SPATIAL_UNMASK_DEPTH_DB_AT_SPACE_ONE * amount
    # 带内能量（多声道平均，反相不抵消）→ 帧证据
    band = _bandpass_time(x, sr, SPATIAL_UNMASK_LO_HZ, SPATIAL_UNMASK_HI_HZ)
    hop = max(1, int(sr * SPATIAL_UNMASK_FRAME_MS / 1000.0))
    n = x.shape[0]
    pad = (-n) % hop
    power = band * band
    p = np.mean(power, axis=1) if power.ndim == 2 else power
    if pad:
        p = np.concatenate((p, np.zeros(pad)))
    frame_power = p.reshape(-1, hop).mean(axis=1)
    evidence = _spatial_unmask_gain_curve(frame_power, sr)
    max_ev = float(np.max(evidence)) if len(evidence) else 0.0
    if max_ev <= 0.0:
        if report is not None:
            report.update(applied=False, auth=round(amount, 4),
                          depth_cap_db=round(depth_cap, 4), reason="no_evidence")
        return stem
    duck_db = np.clip(depth_cap * evidence, 0.0, depth_cap)
    centers = (np.arange(len(duck_db)) + 0.5) * hop
    gain = np.interp(np.arange(n), centers, 10.0 ** (-duck_db / 20.0))
    out = x + (gain[:, None] - 1.0) * band
    out = out.astype(stem.dtype if stem.dtype in (np.float32, np.float64)
                     else np.float32)
    if report is not None:
        active = duck_db > 0.01
        report.update(
            applied=bool(np.any(active)),
            auth=round(amount, 4),
            band_hz=[SPATIAL_UNMASK_LO_HZ, SPATIAL_UNMASK_HI_HZ],
            depth_cap_db=round(depth_cap, 4),
            evidence_p50=round(float(np.percentile(evidence, 50)), 4),
            evidence_p95=round(float(np.percentile(evidence, 95)), 4),
            duck_db_p95=round(float(np.percentile(duck_db, 95)), 4),
            duck_db_max=round(float(np.max(duck_db)), 4),
            coverage=round(float(np.mean(active)), 4),
            reason="applied")
    if stem.ndim == 1:
        return out[:, 0]
    return out


def _bandpass_time(x, sr, lo, hi):
    """二阶 butter 带通 + sosfiltfilt（沿时间轴）。"""
    sos = signal.butter(2, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64), padlen=0, axis=0)


def main():
    ap = argparse.ArgumentParser(description="delta-add 声场重塑（管线内阶段：stems 由 stage_demucs 预先产出）")
    ap.add_argument("--in-mix", required=True, type=Path, help="输入混音（管线上一阶段产物或原曲）")
    ap.add_argument("--out-wav", required=True, type=Path)
    ap.add_argument("--stems-dir", required=True, type=Path, help="demucs 4-stem 输出目录（含 drums/other.wav）")
    ap.add_argument("--mode", choices=list(MODES), default="broadband",
                    help="broadband=纯side增益（默认，音色最保真）| shelf3k | shelf-air | dynamic")
    ap.add_argument("--side-gain-db", type=float, default=None,
                    help="宽度上限 dB：覆盖模式预设的 side 增益（other=设定值，drums=一半）")
    ap.add_argument("--wet", type=float, default=1.0, help="拓宽干湿比 0-1；0=不拓宽，降噪仍由独立 amount 控制")
    ap.add_argument("--space-amount", type=float, default=0.0,
                    help="声场强度授权 0-1（规格 §7.4）：缩放宽度差值并授权 other 轨 "
                         "200-700Hz 有界去拥挤（≤1.5×a dB，证据门控）；0=去拥挤关闭。"
                         "与 --wet 相互独立：wet 缩放宽度差值，本参数不重复缩放宽度")
    ap.add_argument("--other-denoise-amount", type=float, default=0.0,
                    help="other 轨 ≥fc 噪声地板降噪量 0-1（Audition 降噪量语义，贴地板 -amount*100%%）")
    ap.add_argument("--other-denoise-fc", type=float, default=10000.0)
    ap.add_argument("--noise-mode", choices=NOISE_MODES, default="other",
                    help="other=兼容降噪；adaptive_all=对可用分轨做置信度降噪")
    ap.add_argument("--noise-low-hz", type=float, default=8000.0)
    ap.add_argument("--noise-high-hz", type=float, default=20000.0)
    ap.add_argument("--noise-max-attenuation-db", type=float, default=6.0)
    ap.add_argument("--stem-scale", type=float, default=1.0, help="累计上游混音缩放")
    ap.add_argument("--report-json", type=Path, default=None)
    args = ap.parse_args()

    if not np.isfinite(args.stem_scale) or not 0.0 <= args.stem_scale <= 1.0:
        ap.error("--stem-scale must be finite and within [0, 1]")
    if not np.isfinite(args.wet) or not 0.0 <= args.wet <= 1.0:
        ap.error("--wet must be finite and within [0, 1]")
    if not np.isfinite(args.space_amount) or not 0.0 <= args.space_amount <= 1.0:
        ap.error("--space-amount must be finite and within [0, 1]")
    if args.side_gain_db is not None and (
            not np.isfinite(args.side_gain_db) or not 0.0 <= args.side_gain_db <= 12.0):
        ap.error("--side-gain-db must be finite and within [0, 12]")
    if not np.isfinite(args.other_denoise_amount) or not 0.0 <= args.other_denoise_amount <= 1.0:
        ap.error("--other-denoise-amount must be finite and within [0, 1]")
    if not np.isfinite(args.other_denoise_fc) or not 0.0 < args.other_denoise_fc < SR / 2.0:
        ap.error(f"--other-denoise-fc must be finite and within (0, {SR / 2:.0f})")

    if args.noise_mode == "adaptive_all":
        try:
            validate_noise_options(SR, args.other_denoise_amount, args.noise_low_hz,
                                   args.noise_high_hz, args.noise_max_attenuation_db)
        except ValueError as exc:
            ap.error(str(exc))

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
    unmask_report = {"applied": False}
    noise_report = {"mode": args.noise_mode, "amount": args.other_denoise_amount,
                    "stems": {}, "applied": False}
    wet = args.wet
    # 空间去拥挤（规格 §7.4）：声场强度 a 授权的 other 轨 200-700Hz 有界
    # 整理，先于宽度施加（整理后的 other 作为宽度的输入——同一轨按串联
    # 处理，不是两套独立差值相加）。a=0 或无证据 → 精确透传。
    other_stem_processed = None
    if args.space_amount > 0:
        other_path = stem_dir / "other.wav"
        other_raw, o_sr = sf.read(other_path, always_2d=True, dtype="float64")
        if o_sr != sr:
            raise SystemExit(f"other sample rate {o_sr} != {sr}")
        try:
            validate_audio_pair(other_raw, mix, o_sr, primary_name="other",
                                secondary_name="in-mix", require_sr=sr)
        except ValueError as exc:
            raise SystemExit(str(exc))
        other_raw = other_raw * args.stem_scale
        other_stem_processed = spatial_unmask_other(
            other_raw, sr, args.space_amount, report=unmask_report)
        if unmask_report.get("applied"):
            # 去拥挤差值并入混音基底（其它阶段的差值计算都基于当前基底）
            unmask_delta = (other_stem_processed - other_raw) * 1.0
            out = out + unmask_delta
            print(f"  other: 空间去拥挤 200-700Hz ≤{unmask_report['depth_cap_db']:.2f}dB "
                  f"(auth={unmask_report['auth']:.2f}, coverage={unmask_report['coverage']:.0%})")
        else:
            print(f"  other: 空间去拥挤未动作（{unmask_report.get('reason')}）")
    # 各 stem 的 working 基底（审计 P0-4，2026-09-30）：同轨处理必须真串联
    # denoise → width。旧实现里 adaptive_all 的降噪 delta 与宽度 delta 都基于
    # RAW stem 独立计算后相加——宽度 delta 会把降噪刚削掉的高频噪声重新放大
    # 带回混音。现在降噪先行，宽度 delta 在降噪后的 working stem 上计算；
    # 去拥挤后的 other 作为降噪输入（unmask → denoise → width 同轨串联）。
    # 残差设计不变：最终仍是 mix += Σ(各 delta)，Demucs 解释不了的残差保留。
    working_stems = {}
    if other_stem_processed is not None and unmask_report.get("applied"):
        working_stems["other"] = other_stem_processed
    if args.noise_mode == "adaptive_all":
        for name in ("vocals", "drums", "bass", "other", "guitar", "piano"):
            path = stem_dir / f"{name}.wav"
            if not path.is_file():
                noise_report["stems"][name] = {"status": "unavailable", "applied": False,
                                                "reason": "missing_stem"}
                continue
            if args.other_denoise_amount == 0:
                noise_report["stems"][name] = {"status": "not_applied", "applied": False,
                                                "reason": "disabled"}
                continue
            base_stem = working_stems.get(name)
            if base_stem is None:
                stem, s_sr = sf.read(path, always_2d=True, dtype="float64")
                validate_audio_pair(stem, mix, s_sr, primary_name=name,
                                    secondary_name="in-mix", require_sr=sr)
                base_stem = stem * args.stem_scale
            stats = {}
            # 人声轨参与降噪（AI 人声的嘶声烙在人声内容里，分离后主要落在
            # vocals 轨），但最大衰减减半：气声/齿音由瞬态与谐波保护负责，
            # 稳定嘶声仍会被处理，真空气最多损失 cap/2，不设排除性豁免。
            cap = args.noise_max_attenuation_db * (0.5 if name == "vocals" else 1.0)
            cleaned = adaptive_denoise(base_stem, sr, args.other_denoise_amount,
                                       args.noise_low_hz, args.noise_high_hz,
                                       cap, report=stats)
            denoise_delta += cleaned - base_stem
            working_stems[name] = cleaned
            noise_report["stems"][name] = stats
        denoise_delta, noise_report["mix_budget"] = constrain_noise_delta(
            out, denoise_delta, sr, args.noise_low_hz, args.noise_high_hz,
            args.noise_max_attenuation_db * args.other_denoise_amount)
        noise_report["applied"] = noise_report["mix_budget"]["applied"]
    # wet=0 宽度整块旁路（审计 P1-1，2026-09-30）：不加载 stem、不做
    # reshape / 鼓包络 / delta 低频保护，width_delta 恒为零。只保留 legacy
    # other 降噪（noise_mode=="other" 且 amount>0）所需的最小 other 轨路径；
    # adaptive_all 降噪已在上方独立完成，denoise / unmask 语义不受影响。
    legacy_other_denoise = (args.noise_mode == "other"
                            and args.other_denoise_amount > 0)
    for name, (db, fc, gain) in params.items():
        if wet == 0 and not (name == "other" and legacy_other_denoise):
            continue
        stem, s_sr = sf.read(stem_dir / f"{name}.wav", always_2d=True, dtype="float64")
        if s_sr != sr:
            raise SystemExit(f"{name} sample rate {s_sr} != {sr}")
        try:
            validate_audio_pair(stem, out, s_sr, primary_name=name, secondary_name="in-mix")
        except ValueError as exc:
            raise SystemExit(str(exc))
        stem = stem * args.stem_scale
        working = working_stems.get(name, stem)
        if wet > 0:
            if args.mode == "dynamic" and db > 0:
                reshaped = _dynamic_side_shelf(working, sr, fc, db)
            else:
                reshaped = _reshape_stem(working, sr, db, fc, gain)
            delta = (reshaped - working) * wet
            if name == 'drums':
                delta *= drum_width_envelope(working, sr)[:, None]
            delta = _protect_widen_delta(delta, sr)
            width_delta += delta
            processed = working + delta
        else:
            processed = working
        if legacy_other_denoise and name == "other":
            cleaned = _spectral_denoise(processed, sr, args.other_denoise_fc,
                                       args.other_denoise_amount)
            denoise_delta += cleaned - processed
            print(f"  other: ≥{args.other_denoise_fc:.0f}Hz 噪声地板降噪 {args.other_denoise_amount*100:.0f}%")
        if wet > 0:
            if args.mode == "dynamic" and db > 0:
                # 动态分支不施加静态 side 增益，打印不得谎报已生效的参数。
                print(f"  {name}: 动态门 side shelf +{db}dB@{fc:.0f}Hz"
                      "（包络控制；静态 side 增益在此模式不适用）")
            else:
                print(f"  {name}: shelf +{db}dB@{fc:.0f}Hz, side gain +{gain}dB")
    if args.noise_mode != "adaptive_all":
        noise_report.update(applied=bool(np.any(denoise_delta)),
                            reason="legacy_other" if args.other_denoise_amount else "disabled")
    base = out + denoise_delta
    # 增长授权 = 本阶段实际请求的最大 Side 增益（static: other 轨增益、drums
    # 自动减半；dynamic: side shelf 提升量），用户请求本身就是预算。
    growth_db = max((db if args.mode == "dynamic" and db > 0 else gain)
                    for db, _fc, gain in params.values())
    accepted, budget = constrain_width_delta(base, width_delta, sr, growth_db=growth_db)
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
                     input_path=args.in_mix, output_path=args.out_wav,
                     extra={"noise": noise_report, "width": budget,
                            "spatial_unmask": unmask_report})
    print(f"  输出: {args.out_wav}")

    print("宽度报告:")
    width_report(mix, out, sr)


if __name__ == "__main__":
    main()
