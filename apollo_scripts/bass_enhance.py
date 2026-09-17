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
# v20260912: 新增第2批次默认关闭的 Kick/Bass 有界侧链（--sidechain-*，默认
# amount=0 关闭，关闭时与旧路径位级一致；不改 stage_metadata 接口）：
#  - detect_kick_envelope(): drums 35-140Hz thump 起音 × 1-3kHz attack 起音 ×
#    频段均衡三条件联合置信——持续低频（无起音/无 click）、snare/hat（低频
#    仅裙边）拿不到置信；包络一阶有界平滑（无过冲）；
#  - apply_bass_sidechain(): 仅 duck bass 20-180Hz 带通分量（零相位提取后
#    精确重组，中高频逐样本不变），全曲/全声道共用同一条 duck 曲线，
#    duck ≤ max_duck_db 且按置信等比缩小；
#  - main 集成仅在 --sidechain-amount > 0 且提供 --drums 时生效；delta-add
#    与既有参数默认完全不变。
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_validation import validate_audio_pair, finite_range
from stage_metadata import write_report
from scipy import ndimage, signal


def soft_clip(x, drive):
    """tanh 软限幅产生温和谐波"""
    return np.tanh(x * drive) / np.tanh(drive)


SUB_SHELF_FC = 60.0
# 低频侧链受控溢出（v20260910）：Side 的 shelf 新增幅度 = Mid 新增幅度 ×
# 10^(-6/20)（一半）。不做黑胶刻盘式完全单声道化——数字混音的低频保留
# 有限立体声（高相关但非全同）。
SUB_SHELF_SIDE_RELATIVE_DB = -6.0


def side_shelf_db(sub_db, relative_db=SUB_SHELF_SIDE_RELATIVE_DB):
    """Mid shelf 提升 sub_db（>0）时 Side 允许的受控溢出增益（dB）。

    按"Side 新增幅度 = Mid 新增幅度 × k"定义（k=10^(relative_db/20)，
    默认一半），而不是把 shelf 增益整体平移 relative_db：平移会把
    sub_db<|relative_db| 时的 Side 变成衰减，等于变相单声道化。
    sub_db<=0（削减低频）时 Side 对称同幅削减；sub_db=0 精确回 0。
    """
    if sub_db <= 0.0:
        return sub_db
    k = 10.0 ** (relative_db / 20.0)
    added = k * (10.0 ** (sub_db / 20.0) - 1.0)
    return float(20.0 * np.log10(1.0 + added))


def _shelf(x, sr, fc, gain_db, q=0.7071):
    """真 low-shelf（模拟原型 + 双线性变换 + filtfilt 零相位）。

    为什么不用 RBJ biquad：RBJ shelf 在低 fc/小增益组合下会失稳（实测
    fc=70Hz/sr=44100/+3dB 极点半径 1.39）。模拟原型
        H(s) = (s² + G^¼·ω0/q·s + √G·ω0²) / (s² + G^−¼·ω0/q·s + G^−½·ω0²)
    对任意 G>0 极点恒在左半平面，双线性（含 fc 预畸变）后恒稳定。
    filtfilt 幅频取平方，故单次按 gain_db/2 设计（G=10^(gain_db/40)），
    总提升精确等于 gain_db；fc 为总提升的 dB 域中点频率（30-60Hz sub
    接近满增益、90Hz+ 快速退出，不与 punch bell 重叠）。gain_db=0 精确恒等。
    """
    if gain_db == 0.0:
        return np.asarray(x, dtype=np.float64).copy()
    G = 10 ** (gain_db / 40.0)
    w0 = 2.0 * sr * np.tan(np.pi * fc / sr)
    g4 = G ** 0.25
    num = [1.0, g4 * w0 / q, np.sqrt(G) * w0 * w0]
    den = [1.0, w0 / (g4 * q), w0 * w0 / np.sqrt(G)]
    b, a = signal.bilinear(num, den, fs=sr)
    return signal.filtfilt(b, a, np.asarray(x, dtype=np.float64), padlen=0)


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
    """Apply the shared low-shelf and punch bell stage."""
    return _bell(_shelf(x, sr, SUB_SHELF_FC, sub_db), sr, 90.0, punch_db, q=1.2)


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


# ── Kick/Bass 有界侧链（v20260912，第2批次，默认关闭） ─────────────────────
# 只 duck bass 的 20-180Hz 带通分量：零相位 sosfiltfilt 提取低频分量，按同一
# 条 duck 增益曲线缩放后与原信号相加精确重组——中高频逐样本不变（仅滤波裙边
# 量级的数值残差），全曲/全声道共用同一条曲线。kick 检测是能量启发式、非音
# 色分类器：低频 thump 起音、attack 起音、频段均衡三个条件联合，拿不到联合
# 置信就按比例缩小 duck（拿不准少动、拿不准不动）。所有滤波沿用 padlen=0
# （短音频不会触发 filtfilt 长度异常）。
KICK_LOW_LO_HZ = 35.0
KICK_LOW_HI_HZ = 140.0
KICK_ATTACK_LO_HZ = 1000.0
KICK_ATTACK_HI_HZ = 3000.0
KICK_ONSET_FAST_MS = 1.0      # 起音检测的快包络
KICK_ONSET_SLOW_MS = 25.0     # 起音检测的慢包络（快-慢之差 = 瞬态成分）
KICK_FAST_RELEASE_MS = 60.0
KICK_SLOW_RELEASE_MS = 200.0
KICK_ENV_RELEASE_MS = 90.0    # kick 联合包络的平滑释放
# 频段均衡（平滑包络幅度比）：kick 的低频包络 >> 1-3kHz 包络；比 attack 弱
# 3dB 记 0 分、强 9dB 记满分。snare/hat 的能量以 1-3kHz 为主（35-140Hz 只有
# 裙边）→ 均衡分≈0，不误判。
KICK_BALANCE_OFFSET_DB = 3.0
KICK_BALANCE_RANGE_DB = 12.0
# attack 重合度（起音幅度比，绝对值）：1-3kHz 起音允许比低频起音弱 18dB 仍
# 记部分分、弱 6dB 以内记满分。无 click 的持续低频/软起音音符（attack 起音
# 绝对值远小于低频起音）→ 重合度≈0，不误判。重合度还乘以低频起音存在门
# （onset_n）：静音/包络拖尾里两个起音都≈0，eps 比值会退化成 0dB（假满分），
# 必须用存在门归零；也不做 release 平滑——零相位滤波的 pre-ring 会让低频
# 包络略超前 attack 到达，release 平滑会把这段"有低频、无 attack"的窗口填满。
KICK_COINC_FLOOR_DB = 18.0
KICK_COINC_RANGE_DB = 12.0
KICK_COINC_WINDOW_S = 0.04   # 重合度比值取 ±该窗口内的起音峰值（容忍 click
                             # 与 thump 峰的毫秒级错位，双向）
KICK_HIT_THRESHOLD = 0.3      # hit 候选：包络局部极大须超过该值
KICK_HIT_GAP_S = 0.05         # 相邻 hit 最小间隔
KICK_MIN_HITS = 3             # 少于 3 个 hit = 证据不足，孤立瞬态不下判断
KICK_CONF_OFFSET = 0.4        # 平均 kickness 低于该值 → 置信 0
KICK_CONF_SCALE = 0.4
KICK_MIN_SECONDS = 0.25       # 更短的 drums 不下判断
KICK_EDGE_FADE_S = 0.01       # 分析前两端淡入淡出，抑制 filtfilt 边界假起音
DUCK_BAND_LO_HZ = 20.0
DUCK_BAND_HI_HZ = 180.0
DUCK_AMOUNT_RANGE = (0.0, 1.0)
DUCK_ATTACK_MS_RANGE = (1.0, 50.0)
DUCK_RELEASE_MS_RANGE = (20.0, 500.0)
DUCK_MAX_DB_RANGE = (0.0, 12.0)


def _follower(level, sr, attack_ms, release_ms):
    """一阶 attack/release 包络跟随：上升用 attack、下降用 release 系数。

    单调、有界（输出恒在 [min(level), max(level)] 内）、无过冲无振铃——
    相比 filtfilt 平滑不会在瞬态后反向过冲（"包络有界平滑"）。
    attack_ms == release_ms 时退化为普通一阶平滑。
    """
    a = 1.0 - np.exp(-1000.0 / (sr * max(float(attack_ms), 1e-6)))
    r = 1.0 - np.exp(-1000.0 / (sr * max(float(release_ms), 1e-6)))
    out = np.empty(len(level), dtype=np.float64)
    acc = 0.0
    for i in range(len(level)):
        v = level[i]
        acc += (a if v >= acc else r) * (v - acc)
        out[i] = acc
    return out


def _normalize_p99(y):
    """按 p99 归一到 [0,1]；p99 退化（极稀疏事件）时回退 max；全 0 → 全 0。"""
    y = np.asarray(y, dtype=np.float64)
    peak = float(np.max(y)) if len(y) else 0.0
    if peak <= 1e-12:
        return np.zeros_like(y)
    ref = float(np.percentile(y, 99.0))
    if ref <= 1e-12:
        ref = peak
    return np.clip(y / ref, 0.0, 1.0)


def _hit_peaks(env, threshold, min_gap):
    """包络局部极大（高于 threshold、间隔 >= min_gap），返回升序索引列表。"""
    if len(env) < 3:
        return []
    candidates = np.where((env[1:-1] > threshold)
                          & (env[1:-1] >= env[:-2])
                          & (env[1:-1] >= env[2:]))[0] + 1
    picks = []
    last = -min_gap - 1
    for i in candidates:
        if i - last >= min_gap:
            picks.append(int(i))
            last = int(i)
    return picks


def _bandpass_time(x, sr, lo, hi):
    """_bandpass 的 (n, ch) 版本：显式沿时间轴（axis=0）滤波。

    （sosfiltfilt 默认 axis=-1，对 (样本, 声道) 布局的 2D 数组会错误地沿
    长度为声道数的轴滤波——现有 _bandpass 调用方全部传 1D，此处必须显式
    指定 axis=0。）
    """
    sos = signal.butter(2, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64), padlen=0, axis=0)


def _bandpass_causal(x, sr, lo, hi):
    """因果（单次 sosfilt）带通，用于 kick 检测的包络提取。

    零相位 filtfilt 的 pre-ring 会让低频包络比 1-3kHz 包络早 ~10ms 出现
    （"有低频、无 attack"的假 kick 窗口）；因果滤波无 pre-ring，且低频
    thump 在 click 之后到达，与真实时序一致。检测不需要零相位。
    """
    sos = signal.butter(2, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfilt(sos, np.asarray(x, dtype=np.float64), axis=0)


def detect_kick_envelope(drums, sr):
    """检测 drums stem 中的 kick（底鼓）：返回 (env, confidence)。

    env : (len(drums),) float64，每样本 kick 可能性包络，有界 ∈ [0,1]
        （一阶 attack/release 有界平滑，无过冲；峰值保持绝对标度、不重归一）。
    confidence : float ∈ [0,1]，全曲联合置信。三个条件联合判定：
        1) 35-140Hz 存在 thump 起音（快包络超出慢包络 → 持续低频无起音即落空）；
        2) 起音处 1-3kHz attack 以可比的**绝对强度**同时出现（起音幅度比 →
           无 click / 软起音的持续低频、808 式音符落空）；
        3) 低频包络与 1-3kHz 包络同量级（频段均衡 → 能量以 1-3kHz 为主、
           35-140Hz 只有裙边的 snare / hat 落空）。
        另要求 hit 数 >= KICK_MIN_HITS：孤立瞬态证据不足，不下判断。
    静音 / 过短(< KICK_MIN_SECONDS) / 非有限 / 无低频 / 无起音 → (全 0, 0.0)。
    能量启发式、非音色分类器；不确定的内容一律给低置信（下游按置信等比
    缩小 duck 量）。
    """
    x = np.asarray(drums, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    n = len(x)
    if (n == 0 or not np.isfinite(x).all() or n < int(sr * KICK_MIN_SECONDS)
            or KICK_LOW_HI_HZ >= 0.45 * sr):
        return np.zeros(n, dtype=np.float64), 0.0
    x = x.copy()
    # filtfilt（padlen=0）在首尾有边界瞬态，会制造假起音：分析前对副本两端
    # 做 KICK_EDGE_FADE_S 淡入淡出（只影响分析，不改调用方数据）。
    fade = min(int(KICK_EDGE_FADE_S * sr), n // 2)
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, endpoint=False)
        x[:fade] *= ramp[:, None]
        x[-fade:] *= ramp[::-1][:, None]
    # 多声道按能量平均（避免反相抵消在分析时抵消），与 analyze_auto_clarity 同口径。
    low = _bandpass_causal(x, sr, KICK_LOW_LO_HZ, KICK_LOW_HI_HZ)
    low_e = np.sqrt(np.mean(low * low, axis=1))
    if float(np.max(low_e)) <= 1e-9:
        return np.zeros(n, dtype=np.float64), 0.0      # 无低频内容（纯 hat 等）
    att_hi = min(KICK_ATTACK_HI_HZ, 0.45 * sr)
    att = _bandpass_causal(x, sr, min(KICK_ATTACK_LO_HZ, 0.5 * att_hi), att_hi)
    att_e = np.sqrt(np.mean(att * att, axis=1))
    if float(np.max(att_e)) <= 1e-9:
        return np.zeros(n, dtype=np.float64), 0.0      # 无 1-3kHz attack 成分
    low_fast = _follower(low_e, sr, KICK_ONSET_FAST_MS, KICK_FAST_RELEASE_MS)
    low_slow = _follower(low_e, sr, KICK_ONSET_SLOW_MS, KICK_SLOW_RELEASE_MS)
    att_fast = _follower(att_e, sr, KICK_ONSET_FAST_MS, KICK_FAST_RELEASE_MS)
    att_slow = _follower(att_e, sr, KICK_ONSET_SLOW_MS, KICK_SLOW_RELEASE_MS)
    low_onset = np.clip(low_fast - low_slow, 0.0, None)
    att_onset = np.clip(att_fast - att_slow, 0.0, None)
    if float(np.max(low_onset)) <= 1e-9 or float(np.max(att_onset)) <= 1e-9:
        return np.zeros(n, dtype=np.float64), 0.0      # 持续低频 / 无瞬态起音
    onset_n = _normalize_p99(low_onset)
    # attack 重合度：1-3kHz 起音与低频起音的**绝对幅度比**。p99 归一化会让
    # 任意微弱的 attack 残余拿满分，从而把无 click 的持续低频误判成 kick；
    # 绝对比值才能识别"低频起音强、attack 起音可有可无"的软起音内容。
    # 比值两侧各取 ±KICK_COINC_WINDOW_S 窗口内的起音峰值（click 与 thump 峰
    # 有毫秒级错位，逐样本相除会被低估）；再乘 onset 存在门——起音都≈0 时
    # eps 比值退化成 0dB 假满分。
    win = max(int(KICK_COINC_WINDOW_S * sr) | 1, 3)
    att_on_w = ndimage.maximum_filter1d(att_onset, win)
    low_on_w = ndimage.maximum_filter1d(low_onset, win)
    rel_db = 20.0 * np.log10((att_on_w + 1e-9) / (low_on_w + 1e-9))
    coincide = (np.clip((rel_db + KICK_COINC_FLOOR_DB) / KICK_COINC_RANGE_DB,
                        0.0, 1.0)
                * ndimage.maximum_filter1d(onset_n, win))
    # 频段均衡（平滑包络幅度比，逐样本）：kick 低频 >> attack；snare/hat 反之。
    balance_db = 20.0 * np.log10((low_fast + 1e-9) / (att_fast + 1e-9))
    balance = np.clip((balance_db + KICK_BALANCE_OFFSET_DB) / KICK_BALANCE_RANGE_DB,
                      0.0, 1.0)
    joint = np.clip(onset_n * coincide * balance, 0.0, 1.0)
    env = np.clip(_follower(joint, sr, KICK_ONSET_FAST_MS, KICK_ENV_RELEASE_MS),
                  0.0, 1.0)
    peaks = _hit_peaks(env, KICK_HIT_THRESHOLD, int(sr * KICK_HIT_GAP_S))
    if not peaks:
        return env, 0.0
    conf = float(np.clip((np.mean([float(env[i]) for i in peaks]) - KICK_CONF_OFFSET)
                         / KICK_CONF_SCALE, 0.0, 1.0))
    conf *= float(np.clip((len(peaks) - 1) / (KICK_MIN_HITS - 1), 0.0, 1.0))
    return env, round(float(np.clip(conf, 0.0, 1.0)), 4)


def apply_bass_sidechain(bass, sr, drums, amount=0, attack_ms=5, release_ms=150,
                         max_duck_db=6, report=None):
    """Kick 触发的 bass 低频有界 duck（默认关闭：amount=0 精确透传）。

    仅缩放 bass 的 20-180Hz 带通分量（零相位提取后精确重组），中高频逐样本
    不变；全曲/全声道共用同一条 duck 增益曲线（声道联动，绝不做 per-channel
    独立曲线）。每样本 duck 深度 =
        max_duck_db × amount × confidence × env(t)
    经 attack_ms/release_ms 一阶平滑后截到 [0, max_duck_db]（有界，永不超
    上限）；置信为 0（没检测到可信 kick）时完全不动作。

    amount=0/None、drums 为 None/空/非有限/过短、或置信为 0 时**精确返回
    原信号**（同一对象、位级不变）；实际施加时返回与输入同 dtype 的数组。
    amount 须有限且 ∈ [0,1]（否则 ValueError）；attack_ms/release_ms/
    max_duck_db 须有限（否则 ValueError），越界截断到 [1,50]/[20,500]/[0,12]。
    drums 与 bass 长度不必一致：duck 曲线按 drums 计算，截断/补零对齐 bass
    （drums 覆盖不到的尾部不 duck）。短音频安全：所有滤波 padlen=0。

    report 传入 dict 时就地写入：applied、confidence、duck_db_p50、
    duck_db_p95、duck_db_max（整条 duck 曲线的百分位/最大值，dB）、
    attack_ms、release_ms、max_duck_db。
    """
    amount = 0.0 if amount is None else float(amount)
    if (not np.isfinite(amount)
            or not DUCK_AMOUNT_RANGE[0] <= amount <= DUCK_AMOUNT_RANGE[1]):
        raise ValueError(f"sidechain amount must be finite within [0, 1], got {amount!r}")
    attack_ms = float(attack_ms)
    release_ms = float(release_ms)
    max_duck_db = float(max_duck_db)
    for value, name, rng in ((attack_ms, "attack_ms", DUCK_ATTACK_MS_RANGE),
                             (release_ms, "release_ms", DUCK_RELEASE_MS_RANGE),
                             (max_duck_db, "max_duck_db", DUCK_MAX_DB_RANGE)):
        if not np.isfinite(value):
            raise ValueError(f"{name} must be finite, got {value!r}")
    attack_ms = float(np.clip(attack_ms, *DUCK_ATTACK_MS_RANGE))
    release_ms = float(np.clip(release_ms, *DUCK_RELEASE_MS_RANGE))
    max_duck_db = float(np.clip(max_duck_db, *DUCK_MAX_DB_RANGE))

    def _finish(result, applied, confidence=0.0, duck=None):
        if report is not None:
            if duck is not None and len(duck):
                p50 = float(np.percentile(duck, 50))
                p95 = float(np.percentile(duck, 95))
                dmax = float(np.max(duck))
            else:
                p50 = p95 = dmax = 0.0
            report.update(applied=bool(applied), confidence=float(confidence),
                          duck_db_p50=round(p50, 4), duck_db_p95=round(p95, 4),
                          duck_db_max=round(dmax, 4), attack_ms=attack_ms,
                          release_ms=release_ms, max_duck_db=max_duck_db)
        return result

    arr = np.asarray(bass)
    if amount <= 0.0 or drums is None or len(arr) == 0:
        return _finish(arr.copy(), False)
    x = np.asarray(drums, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if (len(x) == 0 or not np.isfinite(x).all()
            or DUCK_BAND_HI_HZ >= 0.45 * sr):
        return _finish(arr.copy(), False)
    env, conf = detect_kick_envelope(x, sr)
    if conf <= 0.0 or float(np.max(env)) <= 0.0:
        return _finish(arr, False, conf)
    curve = np.zeros(len(arr), dtype=np.float64)
    m = min(len(arr), len(env))
    curve[:m] = env[:m]
    duck = _follower(max_duck_db * amount * conf * curve, sr, attack_ms, release_ms)
    duck = np.clip(duck, 0.0, max_duck_db)
    gain = 10.0 ** (-duck / 20.0)
    x2 = arr.astype(np.float64)
    if x2.ndim == 1:
        x2 = x2[:, None]
    low = _bandpass_time(x2, sr, DUCK_BAND_LO_HZ, DUCK_BAND_HI_HZ)
    out = x2 + (gain[:, None] - 1.0) * low
    if arr.ndim == 1:
        out = out[:, 0]
    out_dtype = arr.dtype if arr.dtype in (np.float32, np.float64) else np.float32
    return _finish(out.astype(out_dtype), True, conf, duck)


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


def _enhance_stereo(bass, sr, sub_db, punch_db, sat, trans,
                    auto_clarity_gains, sat_budget):
    """M/S 域立体声处理：Mid 全额 sub shelf，Side 按 side_shelf_db 受控溢出。

    punch bell / 饱和 / 瞬态 / 门限对 M、S 用同一参数；门限与瞬态包络按
    M、S 自身电平计算，近单声道的 S 会被门限自然冻结（无侧链内容可放大，
    物理上合理）。立体声联动：M、S 传入同一 sat_budget 与 clarity gains。
    """
    m = bass.mean(axis=1)
    s = (bass[:, 0] - bass[:, 1]) * 0.5
    out_m = enhance_bass_stem(
        m, sr, sub_db=sub_db, punch_db=punch_db, sat=sat, trans=trans,
        auto_clarity_gains=auto_clarity_gains, sat_budget_gain=sat_budget)
    out_s = enhance_bass_stem(
        s, sr, sub_db=side_shelf_db(sub_db), punch_db=punch_db, sat=sat,
        trans=trans, auto_clarity_gains=auto_clarity_gains,
        sat_budget_gain=sat_budget)
    return np.column_stack((out_m + out_s, out_m - out_s))


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
    ap.add_argument("--sidechain-drums", type=Path, default=None,
                    help="可选 drums stem；用于 kick 触发的受控 bass 低频让位")
    ap.add_argument("--sidechain-amount", type=float, default=0.0,
                    help="Kick/Bass 侧链强度 0-1（第2批次，默认 0=关闭，关闭时位级透传）")
    ap.add_argument("--sidechain-attack-ms", type=float, default=5.0,
                    help="侧链 duck 攻击时间 ms（1-50）")
    ap.add_argument("--sidechain-release-ms", type=float, default=150.0,
                    help="侧链 duck 释放时间 ms（20-500）")
    ap.add_argument("--sidechain-max-duck-db", type=float, default=6.0,
                    help="侧链最大 duck 深度 dB（0-12）")
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
    for value, name, lo, hi in (
            (args.sidechain_amount, "sidechain-amount", 0.0, 1.0),
            (args.sidechain_attack_ms, "sidechain-attack-ms", 1.0, 50.0),
            (args.sidechain_release_ms, "sidechain-release-ms", 20.0, 500.0),
            (args.sidechain_max_duck_db, "sidechain-max-duck-db", 0.0, 12.0)):
        try: finite_range(value, name, lo, hi)
        except ValueError as exc: ap.error(str(exc))
    if args.sidechain_amount > 0 and args.sidechain_drums is None:
        ap.error("--sidechain-amount > 0 requires --drums (drums stem)")

    drums = None
    if args.sidechain_drums is not None:
        drums, sr3 = sf.read(args.sidechain_drums, always_2d=True)
        if sr3 != sr:
            ap.error(f"sample rate mismatch: {args.sidechain_drums} is {sr3}, expected {sr}")

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

    if bass.shape[1] == 2:
        out = _enhance_stereo(bass, sr, sub_db=args.sub_db,
                              punch_db=args.punch_db, sat=args.sat,
                              trans=args.trans,
                              auto_clarity_gains=clarity_gains,
                              sat_budget=sat_budget)
    else:
        out = np.zeros_like(bass)
        for c in range(bass.shape[1]):
            out[:, c] = enhance_bass_stem(
                bass[:, c], sr,
                sub_db=args.sub_db, punch_db=args.punch_db,
                sat=args.sat, trans=args.trans,
                auto_clarity_gains=clarity_gains,
                sat_budget_gain=sat_budget,
            )
    # Kick/Bass 有界侧链（v20260912）：仅当 --sidechain-amount > 0 且提供了
    # --drums 时启用；duck 施加在增强后的 bass、delta-add 之前，使混音中的
    # bass 低频在 kick 时让位。amount=0 时完全跳过（与旧路径位级一致）。
    sc_report = None
    if args.sidechain_amount > 0 and drums is not None:
        sc_report = {}
        out = apply_bass_sidechain(
            out, sr, drums, amount=args.sidechain_amount,
            attack_ms=args.sidechain_attack_ms,
            release_ms=args.sidechain_release_ms,
            max_duck_db=args.sidechain_max_duck_db, report=sc_report)
    out = in_mix + (out - original_bass)

    # 混音后样本峰值保护：增强 delta 与完整输入混音相加后统一留出母带余量。
    # original_bass 是 clarity EQ **之前**的 delta 基准（整段处理差值都被保留），
    # 而 peak/scale 在施加 clarity EQ 之后的完整混音上测量——报告里的 scale
    # 因此已计入 clarity 提升带来的静态电平变化。
    # 这里不是 true-peak limiter；真正的 4× true peak 检测由下游 Soren 完成。
    neutral = (args.sub_db == 0 and args.punch_db == 0 and args.sat == 0 and
               args.trans == 0 and args.bass_gain_db == 0 and
               (clarity_gains is None or clarity_gains == (0.0, 0.0)) and
               not (sc_report is not None and sc_report.get("applied", False)))
    peak = np.max(np.abs(out))
    ceiling = 10 ** (-0.5 / 20.0)  # ≈ 0.944
    if not neutral and peak > ceiling:
        out *= ceiling / peak

    sf.write(args.out, out.astype(np.float32), sr, subtype="FLOAT")
    if args.report_json:
        write_report(args.report_json, stage="bass", scale=float(ceiling / peak) if (not neutral and peak > ceiling) else 1.0,
                     input_path=args.in_mix, output_path=args.out, extra={"sidechain": sc_report or {"applied": False, "reason": "disabled"}})
    if sat_budget is not None:
        trim_db = min(SAT_BUDGET_MAX_REDUCTION_DB,
                      max(0.0, -20.0 * float(np.log10(max(sat_budget, 1e-12)))))
        trim_txt = f"{trim_db:.2f}dB"
    else:
        trim_txt = "off"
    if sc_report is not None and sc_report.get("applied"):
        sc_txt = (f"sidechain=on conf={sc_report['confidence']:.2f} "
                  f"duck_p95={sc_report['duck_db_p95']:.2f}dB "
                  f"duck_max={sc_report['duck_db_max']:.2f}dB")
    elif sc_report is not None:
        sc_txt = "sidechain=requested_but_not_applied (no confident kicks)"
    else:
        sc_txt = "sidechain=off"
    print(f"Bass-enhanced mix done: {args.out} | sub={args.sub_db}dB punch={args.punch_db}dB "
          f"sat={args.sat} trans={args.trans} "
          f"sat_lmid_trim={trim_txt} (heuristic, not listening-verified) "
          f"auto_clarity={clarity_gains if clarity_gains is not None else 'off'} "
          f"{sc_txt} "
          f"| {sr}Hz {bass.shape[1]}ch")


if __name__ == "__main__":
    main()
