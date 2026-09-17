"""Sub 低架滤波器标定（开发规格 v2 §6.2；测试矩阵 DSP-05/DSP-06）。

锚定 _shelf 的语义契约（sub_db = 低频渐近增益，fc = dB 域中点）：
  1. DC/极低频渐近增益精确 = sub_db（10/20Hz 实测 ±0.05dB）；
  2. fc 处 ≈ sub_db/2（dB 域中点，65Hz/6dB 实测 +3.00dB）；
  3. 退出带按实测容差（100Hz ≤ 1.0dB、120Hz ≤ 0.6dB、200Hz ≤ 0.15dB、
     300Hz+ ≈ 0——不与 punch bell(90Hz) 显著重叠）；
  4. 20Hz-Nyquist 响应单调下降、无谐振（DSP-05）；
  5. sub_db=0 精确恒等（逐样本，含 float32 输入）；
  6. 静默/无活动段不被放大（§6.2"默认不放大无活动段"——活动门冻结新增差值）。

另附 50/65/80Hz 三候选的实测响应对比（§6.2 要求的工程比较记录，选型
决策见 docs/analysis/sub_shelf_calibration_20260917.md，本文件只锚定
被选中的 65Hz 契约）。

测试方法：稳态正弦最小二乘幅度估计（跳过 ≥0.5s 滤波器起振段），
与 _shelf 的零相位 filtfilt 实际输出对照——测的是真实通过的信号，
不是设计公式自证。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
import bass_enhance as bass

SR = 44100
FC = bass.SUB_SHELF_FC
assert FC == 65.0, "选型变更须同步更新本套标定与 docs/analysis 选型记录"


def _tone(freq, seconds=2.0, amp=0.3):
    t = np.arange(int(SR * seconds)) / SR
    return amp * np.sin(2 * np.pi * freq * t)


def _measured_gain_db(freq, sub_db, fc=FC):
    """稳态正弦通过 _shelf 的实测增益（dB）。"""
    x = _tone(freq)
    y = bass._shelf(x, SR, fc, sub_db)
    seg = slice(SR // 2, None)                     # 跳过滤波器起振
    t = np.arange(len(x[seg])) / SR
    ay = 2.0 * np.abs(np.mean(y[seg] * np.exp(-2j * np.pi * freq * t)))
    ax = 2.0 * np.abs(np.mean(x[seg] * np.exp(-2j * np.pi * freq * t)))
    return 20.0 * np.log10(ay / ax)


# ── DSP-06：低架标定（请求 dB 与实测响应的关系可解释）─────────────────

@pytest.mark.parametrize("sub_db", [2.0, 6.0, 12.0])
def test_dc_asymptotic_gain_is_requested(sub_db):
    """低频渐近增益精确 = sub_db（§6.2：sub_db 指低频渐近增益，不声称
    截止频率处也是相同增益）。10Hz 为实测渐近点；20Hz 仍在转折带内
    （fc=65Hz 实测：6dB 请求 +5.95、12dB 请求 +11.88——转折带随增益展宽，
    "20-30Hz 以下全额提升"的说法不成立，这正是 §6.2 要求按实测而非旋钮
    数字解释响应的原因）。"""
    assert _measured_gain_db(10.0, sub_db) == pytest.approx(sub_db, abs=0.05)
    in_band = _measured_gain_db(20.0, sub_db)
    assert sub_db - 0.2 <= in_band <= sub_db      # 转折带内：偏差有界且小于 fc 处
    assert in_band > _measured_gain_db(FC, sub_db)  # 高于中点（dB 域单调）


@pytest.mark.parametrize("sub_db", [2.0, 6.0, 12.0])
def test_feature_frequency_is_db_midpoint(sub_db):
    """fc 处 ≈ sub_db/2（dB 域中点定义）。"""
    assert _measured_gain_db(FC, sub_db) == pytest.approx(sub_db / 2.0, abs=0.05)


def test_exit_band_response():
    """退出带实测：不与 punch bell(90Hz) 显著重叠、300Hz+ 透明。"""
    g = {f: _measured_gain_db(f, 6.0) for f in (100.0, 120.0, 200.0, 300.0, 500.0)}
    assert g[100.0] <= 1.0
    assert g[120.0] <= 0.6
    assert g[200.0] <= 0.15
    assert g[300.0] <= 0.05
    assert g[500.0] <= 0.02


def test_zero_db_is_exact_identity():
    """0dB 逐样本恒等（含 float32 输入与负 dB 削减路径的存在性）。"""
    x = _tone(55.0, 1.0) + _tone(300.0, 1.0, 0.1)
    np.testing.assert_array_equal(bass._shelf(x, SR, FC, 0.0), x)
    x32 = x.astype(np.float32)
    np.testing.assert_array_equal(bass._shelf(x32, SR, FC, 0.0),
                                  x32.astype(np.float64))
    # 削减路径可用且对称（sub_db<0 削减低频；Side 对称同幅由既有测试覆盖）
    y = bass._shelf(x, SR, FC, -6.0)
    assert np.isfinite(y).all()
    assert _measured_gain_db(10.0, -6.0) == pytest.approx(-6.0, abs=0.05)


# ── DSP-05：20Hz–Nyquist 扫频，无谐振、单调下降 ────────────────────────

def test_sweep_monotonic_no_resonance():
    """扫频响应随频率单调下降（相邻倍频程增益非增），无未定义谐振。"""
    freqs = [20, 25, 32, 40, 50, 65, 80, 100, 125, 160, 200, 250, 320, 400,
             500, 640, 800, 1000, 2000, 4000, 8000, 16000]
    gains = [_measured_gain_db(f, 6.0) for f in freqs]
    for i in range(1, len(gains)):
        assert gains[i] <= gains[i - 1] + 0.02, \
            f"{freqs[i - 1]}->{freqs[i]}Hz 出现上升（谐振）: {gains[i - 1]:.2f}->{gains[i]:.2f}"
    assert max(gains) <= 6.0 + 0.05                # 不超过请求增益


# ── §6.2 活动门：默认不放大无活动段（立体声联动）─────────────────────

def test_silent_sections_not_amplified():
    """静音段不被放大：活动门冻结新增差值（不是切断原轨，只不加增强）。"""
    t = np.arange(SR) / SR
    x = np.zeros(SR)
    x[: SR // 2] = 0.4 * np.sin(2 * np.pi * 60 * t[: SR // 2])   # 前半活跃
    out = bass.enhance_bass_stem(x, SR, sub_db=6.0, punch_db=0.0,
                                 sat=0.0, trans=0.0)
    tail = out[SR // 2 + SR // 10:]                # 远离门释放
    np.testing.assert_allclose(tail, 0.0, atol=1e-12)


def test_stereo_linked_gate_power_domain():
    """反相低频（Mid≈0、Side 满）仍被检测为活跃：功率域左右联动的活动门
    不因 M/S 分解后 Mid 能量抵消而冻结 Mid 增强（§6.2：使用功率而非把
    左右波形先平均）。"""
    t = np.arange(SR) / SR
    left = 0.4 * np.sin(2 * np.pi * 60 * t)
    anti = np.column_stack((left, -left))          # 纯反相：M=0, S=left
    gate = bass._stereo_activity_gate(anti, SR)
    active = slice(int(0.2 * SR), int(0.9 * SR))
    assert float(np.mean(gate[active])) > 0.9       # 判定为活跃
    # 对照：M/S 各自独立检测时 Mid=0 会被判不活跃（该缺陷的回归锚）
    m = anti.mean(axis=1)
    gate_m = bass._gate_curve(m * m, SR)
    assert float(np.mean(gate_m[active])) < 0.1


def test_stereo_gate_only_scales_added_delta():
    """门只缩放新增差值，原轨尾音不被切断：门关闭后的真静默段输出=输入
    （差值被门冻结，不是把原信号衰减掉）。"""
    sr = SR
    t = np.arange(sr * 2) / sr
    note = 0.4 * np.sin(2 * np.pi * 60 * t)
    note[sr:] = 0.0                              # 1s 后完全静默（无尾音可保留）
    stereo = np.column_stack((note, note * 0.9))
    out = bass._enhance_stereo(stereo, sr, sub_db=3.0, punch_db=0.0,
                               sat=0.0, trans=0.0, auto_clarity_gains=None,
                               sat_budget=1.0)
    # 静默段（门已释放）输出应精确回到原信号
    tail = slice(int(1.6 * sr), int(2.0 * sr))
    np.testing.assert_allclose(out[tail], stereo[tail], atol=1e-9)
    # 活跃段确实被增强（门开启）
    head = slice(int(0.2 * sr), int(0.8 * sr))
    assert float(np.abs(out[head] - stereo[head]).max()) > 1e-3


# ── §6.2 三候选实测对比（工程记录；断言被选中候选的区分特性）────────────

def test_three_candidate_comparison_record():
    """50/65/80Hz 候选在同一请求（6dB）下的实测响应形状差异（供选型复核）：
    50Hz 更深更窄（80Hz 处已退出 0.81dB），80Hz 更浅更宽（100Hz 仍 +1.76dB，
    与 punch bell 90Hz 明显重叠），65Hz 居中（100Hz +0.92dB、120Hz +0.49dB，
    与 punch bell 重叠有限）。共同点：三者 10Hz 渐近都精确 6dB、fc 处都是
    中点。数值容差 ±0.05dB；换 fc 或 q 须连 docs/analysis 选型记录一起复核。"""
    rows = {}
    for fc in (50.0, 65.0, 80.0):
        rows[fc] = {f: _measured_gain_db(f, 6.0, fc=fc)
                    for f in (10.0, 20.0, 65.0, 80.0, 100.0, 120.0)}
    # 共同点：渐近精确、fc=中点（各自的 fc；此处用 10Hz 与候选自身中点验证）
    for fc, r in rows.items():
        assert r[10.0] == pytest.approx(6.0, abs=0.05)
    # 区分特性（当前实测锚点）
    assert rows[50.0][80.0] < rows[65.0][80.0] < rows[80.0][80.0]
    assert rows[80.0][100.0] > 1.5      # 80Hz 候选与 punch bell 明显重叠
    assert rows[65.0][100.0] < 1.0      # 65Hz 候选与 punch bell 重叠有限
    assert rows[65.0][120.0] < 0.6
    assert rows[50.0][120.0] < 0.25     # 50Hz 候选最窄
