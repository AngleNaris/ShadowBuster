#!/usr/bin/env python3
"""prepare_listening_pack.py — 低频（bass/drums）DSP 试听校准包生成器。

开发规格 §6.7（低频专项验收）+ §13（试听校准）配套工具：
  1. 合成 4 类低频素材（30s / 44.1kHz / 立体声，峰值 ≤ −6 dBFS）：
       A_shortkick_sustained_bass  短 kick + 持续 bass 音符
       B_long808_dense_kick        长 808 滑音 + 密集 kick
       C_acoustic_bass_drums       原声质感 bass + 鼓组（kick/snare/hat）
       D_sub_only_electronic       纯 sub 正弦滑音 + 轻微 hat 节拍参照
  2. 每类素材经真实生产 DSP（apollo_scripts/bass_enhance.py + drum_enhance.py
     子进程，参数不做任何重实现）渲染 4 个变体：
       ref0 / default_v1610 / rc0 / strong
  3. 非 ref 变体统一响度匹配到本类 ref0（integrated LUFS，残差 ≤ 0.2 LU；
     需要削波余量时先整类统一衰减再匹配），写 float32 WAV。
  4. 真实歌曲（若 demucs + htdemucs 权重可用）：对 set_3 input.wav 分离出
     bass/drums，in_mix 用原始输入，渲染同样 4 变体并匹配。
  5. 每类（及真实歌曲）额外写 ref0/default_v1610/rc0 的 mono 折叠 (L+R)/2。
  6. 盲听命名（固定种子 20260917 洗牌），输出 MANIFEST.md / KEY.md /
     manifest.json。

可重复运行、确定性（固定 RNG 种子）。listening_pack/ 下 wav 由 *.wav
gitignore 规则自动忽略；本脚本与 manifest 文件可提交。

用法: python tools/prepare_listening_pack.py [--round {1,2}] [--skip-real] [--keep-work]

--round 1（默认）：第一轮全量校准包，行为与输出与历史版本完全一致。
--round 2：弹性隔离轮（第一轮结论：rc0 低频量合适但弹性不足）。
    sub 固定 2dB，弹性维度（punch/trans/sidechain/clarity 授权）递增：
    ref0 / rc0（第一轮锚点）/ rc1（rc0 量 + 默认弹性）/ rc1e（更高弹性）。
    仅 R（真实歌曲）+ A + B（弹性判别力最强的合成类），输出
    listening_pack/round2/，盲听种子 20260918，全部 4 变体均做 mono 折叠。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import audio_metrics  # noqa: E402  (repo 模块：integrated_lufs)

# ── 常量 ────────────────────────────────────────────────────────────────
SR = 44100
DUR_S = 30.0
BLIND_SEED = 20260917
PEAK_TARGET_DB = -6.5          # 合成素材峰值上限（规格 ≤ −6 dBFS）
CLIP_CEILING_DB = -1.0         # 响度匹配后的削波保护上限（样本峰值）
MATCH_TOL_LU = 0.2             # 响度匹配残差容差
PACK_DIR = REPO / "listening_pack"
WORK_DIR = PACK_DIR / "_work"

# 变体旋钮（低频专项）。bass 阶段 --punch-db 恒 0，punch 全部走 drum 阶段。
VARIANT_ORDER = ["ref0", "default_v1610", "rc0", "strong"]
VARIANTS: dict[str, dict] = {
    "ref0": dict(sub_db=0.0, sat=0.0, punch_db=0.0, trans=0.0,
                 sidechain=0.0, clarity=False, clarity_auth=0.0),
    "default_v1610": dict(sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
                          sidechain=0.5, clarity=True, clarity_auth=1.0),
    "rc0": dict(sub_db=2.0, sat=0.2, punch_db=1.5, trans=0.25,
                sidechain=0.25, clarity=True, clarity_auth=0.5),
    "strong": dict(sub_db=9.0, sat=0.5, punch_db=4.0, trans=0.5,
                   sidechain=0.5, clarity=True, clarity_auth=1.0),
}

MONO_VARIANTS = ["ref0", "default_v1610", "rc0"]

# ── Round 2（弹性隔离轮）：sub 固定 2dB，只有弹性维度递增。仅 R+A+B。─────
ROUND2_BLIND_SEED = 20260918
ROUND2_VARIANT_ORDER = ["ref0", "rc0", "rc1", "rc1e"]
ROUND2_VARIANTS: dict[str, dict] = {
    "ref0": dict(sub_db=0.0, sat=0.0, punch_db=0.0, trans=0.0,
                 sidechain=0.0, clarity=False, clarity_auth=0.0),
    "rc0": dict(sub_db=2.0, sat=0.2, punch_db=1.5, trans=0.25,
                sidechain=0.25, clarity=True, clarity_auth=0.5),
    "rc1": dict(sub_db=2.0, sat=0.2, punch_db=2.0, trans=0.3,
                sidechain=0.3333, clarity=True, clarity_auth=0.6667),
    "rc1e": dict(sub_db=2.0, sat=0.2, punch_db=3.0, trans=0.4,
                 sidechain=0.5, clarity=True, clarity_auth=1.0),
}
ROUND2_CLASSES = ["A", "B"]                # 弹性判别力最强的两组合成素材（同 round-1 种子）
ROUND2_LISTEN_QUESTIONS = [
    "低频量：rc1/rc1e 是否与 rc0 保持同一量级（sub 不变，低频量不应反弹变多）？",
    "弹性：相比 rc0，kick 起音 / bass 律动是否更有弹性？rc1 与 rc1e 哪个合适？",
    "过量检查：rc1e 是否开始出现低频偏多 / 抽吸（pumping）/ 低频变硬？",
    "mono 折叠（小音箱）下以上结论是否仍然成立？",
]

REAL_INPUT = Path(r"D:\_3.AI\audio_upscale\_e2e_tmp\inference_samples\set_3\input.wav")
REAL_CLASS = "R"

LISTEN_QUESTIONS = [
    "kick 起音是否更清楚（每个变体 vs 同类其它变体）？",
    "bass 音符是否更可辨（音高/音符边界）？",
    "长尾音（808/sub/持续 bass）是否连续、有无断裂或抖动？",
    "有无抽吸（pumping）/低频变薄（kick 落下瞬间 bass 被吃掉）？",
    "mono 折叠（小音箱）下以上结论是否仍然成立？",
]

MATERIAL_DESC = {
    "A": "A_shortkick_sustained_bass：短 kick（每 0.5s，~80ms 衰减，60Hz 体 + 2kHz click）"
         "叠持续 bass（55Hz 基频 + 二次谐波，每 2s 换音），~120 BPM。",
    "B": "B_long808_dense_kick：长 808 音符（40–55Hz 滑音，0.8s 指数衰减，每 1s 触发）"
         "+ 密集 kick（每 0.25s），~120 BPM。",
    "C": "C_acoustic_bass_drums：原声质感 bass（80–110Hz + 谐波至 ~500Hz，拨弦包络，每 1s 换音）"
         "+ 鼓组（kick 1&3 拍、snare 2&4 拍 180Hz+噪声、hat 8 分音 8kHz 噪声、hat 有宽度），120 BPM。",
    "D": "D_sub_only_electronic：纯 sub 正弦 30–60Hz 滑音（portamento，每 3s 换音）"
         "+ 轻微 hat（8kHz 噪声 tick，8 分音）作节拍参照。",
    "R": "真实歌曲（demucs htdemucs 四轨分离 bass/drums；in_mix = 原始输入，34.1s）。"
         "注意：真实素材包含分离残差与全频段内容，与合成素材互补。",
}


# ── 小工具 ──────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


def db_to_lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def peak_db(x: np.ndarray) -> float:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return float(20.0 * np.log10(max(m, 1e-12)))


def lufs_of(x: np.ndarray, sr: int) -> tuple[float, str]:
    # audio_metrics 约定 (n, ch)（frames × channels，见 tests/test_audio_metrics.py）
    lufs, status = audio_metrics.integrated_lufs(np.asarray(x).T, sr)
    return float(lufs), status


def write_wav(path: Path, data: np.ndarray, sr: int, subtype: str = "FLOAT") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(data, dtype=np.float64).T, sr, subtype=subtype)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), always_2d=True)
    return np.asarray(data, dtype=np.float64).T, sr  # (ch, n)


def _place(buf: np.ndarray, start: int, sig: np.ndarray) -> None:
    if start < 0 or start >= buf.shape[1]:
        return
    end = min(buf.shape[1], start + sig.shape[1])
    buf[:, start:end] += sig[:, : end - start]


def _tone(freq: float, n: int, amp: float = 1.0, phase0: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    return amp * np.sin(2 * np.pi * freq * t + phase0)


def _env_exp(n: int, tau_s: float) -> np.ndarray:
    return np.exp(-np.arange(n) / SR / tau_s)


def _fades(sig: np.ndarray, fade_s: float = 0.005) -> np.ndarray:
    nf = max(1, int(fade_s * SR))
    out = sig.copy()
    if out.shape[1] > 2 * nf:
        ramp = np.linspace(0.0, 1.0, nf)
        out[:, :nf] *= ramp
        out[:, -nf:] *= ramp[::-1]
    return out


def _bandpass_noise(n: int, lo: float, hi: float, rng: np.random.Generator,
                    order: int = 4) -> np.ndarray:
    """0–1 噪声经带通（但特filt，零相位）。返回单声道 (n,)。"""
    x = rng.standard_normal(n)
    nyq = SR / 2
    sos = __import__("scipy").signal.butter(order, [lo / nyq, hi / nyq],
                                            btype="band", output="sos")
    return __import__("scipy").signal.sosfiltfilt(sos, x)


def _normalize_pair(bass: np.ndarray, drums: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """各自归一到峰值以下，再保证 in_mix = bass+drums 峰值 ≤ PEAK_TARGET_DB。"""
    for arr in (bass, drums):
        g = db_to_lin(PEAK_TARGET_DB - 0.5 - peak_db(arr))
        arr *= g
    in_mix = bass + drums
    over = peak_db(in_mix) - PEAK_TARGET_DB
    if over > 0:
        g = db_to_lin(-over)
        bass *= g
        drums *= g
        in_mix *= g
    return bass, drums, in_mix


# ── 素材合成（固定种子，确定性）────────────────────────────────────────
def synth_class_A(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """短 kick（每 0.5s）+ 持续 bass（55Hz+2 次，每 2s 换音）。"""
    n = int(round(DUR_S * SR))
    notes = [55.0, 55.0, 65.41, 73.42, 55.0, 49.0, 65.41, 55.0,
             49.0, 58.27, 65.41, 55.0, 73.42, 65.41, 49.0]  # 15 × 2s = 30s
    bass = np.zeros((2, n))
    for i, f in enumerate(notes):
        start = int(i * 2.0 * SR)
        dur = int(2.0 * SR)
        t = np.arange(dur) / SR
        env = np.minimum(1.0, t / 0.008) * np.exp(-t / 1.6)   # 持续、轻衰减
        env[-int(0.01 * SR):] *= np.linspace(1.0, 0.0, int(0.01 * SR))
        seg = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 2 * f * t)) * env
        seg *= 0.92 + 0.08 * rng.uniform()                    # 轻微自然变化
        _place(bass, start, np.vstack([seg, seg]))

    drums = np.zeros((2, n))
    for i in range(int(DUR_S / 0.5)):                         # 每 0.5s 一个 kick
        start = int(i * 0.5 * SR)
        accent = 1.0 if i % 2 == 0 else 0.85
        nb = int(0.25 * SR)
        body = _tone(60.0, nb, _env_exp(nb, 0.035)) * accent  # ~80ms 衰减 (-20dB@80ms)
        nc = int(0.03 * SR)
        click = _tone(2000.0, nc, 0.5 * _env_exp(nc, 0.004))
        kick = body.copy()
        kick[:nc] += click
        _place(drums, start, np.vstack([kick, kick]))         # kick 居中
    return bass, drums


def synth_class_B(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """长 808（40–55Hz 滑音，0.8s 衰减）+ 密集 kick（每 0.25s）。"""
    n = int(round(DUR_S * SR))
    notes = [55.0, 49.0, 43.65, 40.0, 46.25, 55.0, 41.2, 49.0,
             36.71, 44.0, 55.0, 49.0, 43.65, 51.91, 40.0,
             46.25, 55.0, 49.0, 41.2, 43.65, 55.0, 40.0, 49.0,
             36.71, 46.25, 44.0, 55.0, 49.0, 43.65, 40.0]      # 30 × 1s = 30s
    bass = np.zeros((2, n))
    prev = notes[0]
    glide_s = 0.08
    for i, f in enumerate(notes):
        start = int(i * 1.0 * SR)
        dur = int(1.0 * SR)
        t = np.arange(dur) / SR
        # portamento：从 prev 滑到 f（cos 曲线），之后保持
        ng = min(dur, int(glide_s * SR))
        frac = np.ones(dur)
        if ng > 1:
            frac[:ng] = 0.5 - 0.5 * np.cos(np.pi * np.arange(ng) / max(1, ng - 1))
        freq = prev + (f - prev) * frac
        phase = 2 * np.pi * np.cumsum(freq) / SR
        env = _env_exp(dur, 0.30)                              # 0.8s 量级指数衰减
        env[-int(0.01 * SR):] *= np.linspace(1.0, 0.0, int(0.01 * SR))
        seg = (np.sin(phase) + 0.10 * np.sin(2 * phase)) * env
        _place(bass, start, np.vstack([seg, seg]))
        prev = f

    drums = np.zeros((2, n))
    for i in range(int(DUR_S / 0.25)):                         # 每 0.25s 密集 kick
        start = int(i * 0.25 * SR)
        accent = 1.0 if i % 4 == 0 else 0.8
        nb = int(0.15 * SR)
        body = _tone(50.0, nb, _env_exp(nb, 0.022)) * accent
        nc = int(0.02 * SR)
        click = _tone(1800.0, nc, 0.4 * _env_exp(nc, 0.003))
        kick = body.copy()
        kick[:nc] += click
        _place(drums, start, np.vstack([kick, kick]))
    return bass, drums


def synth_class_C(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """原声 bass（拨弦，谐波至 ~500Hz）+ 鼓组（kick/snare/hat，hat 宽度）。"""
    n = int(round(DUR_S * SR))
    notes = [82.41, 98.0, 110.0, 87.31, 82.41, 110.0, 98.0, 87.31,
             82.41, 98.0, 110.0, 98.0, 87.31, 110.0, 82.41]    # 15 × 2s，每 1s 换音
    bass = np.zeros((2, n))
    for i, f in enumerate(notes):
        start = int(i * 1.0 * SR)
        dur = int(1.0 * SR)
        t = np.arange(dur) / SR
        env = np.minimum(1.0, t / 0.004) * np.exp(-t / 0.35)   # 拨弦包络
        seg = np.zeros(dur)
        h = 1
        while f * h <= 500.0:                                  # 谐波至 500Hz
            seg += (1.0 / h) * np.sin(2 * np.pi * f * h * t + 0.3 * h)
            h += 1
        seg *= env
        seg *= 0.9 + 0.1 * rng.uniform()
        _place(bass, start, np.vstack([seg, seg]))             # bass 居中

    drums = np.zeros((2, n))
    bars = int(DUR_S / 2.0)                                    # 120 BPM → 1 小节 2s
    for b in range(bars):
        base = int(b * 2.0 * SR)
        for beat in (0.0, 1.0):                                # kick 1&3
            start = base + int(beat * SR)
            nb = int(0.3 * SR)
            body = _tone(55.0, nb, _env_exp(nb, 0.045))
            nc = int(0.02 * SR)
            click = _tone(1500.0, nc, 0.4 * _env_exp(nc, 0.003))
            kick = body.copy()
            kick[:nc] += click
            _place(drums, start, np.vstack([kick, kick]))
        for beat in (0.5, 1.5):                                # snare 2&4：180Hz + 噪声
            start = base + int(beat * SR)
            nt = int(0.25 * SR)
            tone = _tone(180.0, nt, 0.7 * _env_exp(nt, 0.04))
            noise = _bandpass_noise(nt, 400.0, 8000.0, rng) * _env_exp(nt, 0.05)
            snare = tone + 0.8 * noise
            _place(drums, start, np.vstack([snare, snare]))    # snare 居中
        for k in range(8):                                     # hat 8 分音，有宽度
            start = base + int(k * 0.25 * SR)
            nh = int(0.06 * SR)
            amp = 0.5 if k % 2 == 1 else 0.28                  # 反拍重音
            gl = _bandpass_noise(nh, 6000.0, 10500.0, rng) * _env_exp(nh, 0.008) * amp
            gr = _bandpass_noise(nh, 6000.0, 10500.0, rng) * _env_exp(nh, 0.008) * amp
            _place(drums, start, np.vstack([gl, gr]))          # 左右去相关 → 宽度
    return bass, drums


def synth_class_D(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """纯 sub 正弦 30–60Hz（portamento，每 3s 换音）+ 轻微 hat 参照。"""
    n = int(round(DUR_S * SR))
    notes = [55.0, 43.65, 49.0, 36.71, 55.0, 32.7, 44.0, 40.0, 49.0, 55.0]  # 10 × 3s
    freq = np.zeros(n)
    pos = 0
    glide_s = 0.3
    prev = notes[0]
    for i, f in enumerate(notes):
        seg_len = int(3.0 * SR) if i < len(notes) - 1 else n - pos
        seg_len = int(seg_len)
        ng = min(seg_len, int(glide_s * SR))
        idx = np.arange(seg_len)
        frac = np.ones(seg_len)
        if ng > 1:
            frac[:ng] = 0.5 - 0.5 * np.cos(np.pi * idx[:ng] / max(1, ng - 1))
        freq[pos:pos + seg_len] = prev + (f - prev) * frac
        pos += seg_len
        prev = f
    phase = 2 * np.pi * np.cumsum(freq) / SR
    sub = np.sin(phase)
    bass = np.vstack([sub, sub])                               # sub 居中

    drums = np.zeros((2, n))
    for k in range(int(DUR_S / 0.25)):                         # hat 8 分音，轻微
        start = int(k * 0.25 * SR)
        nh = int(0.04 * SR)
        amp = 0.12 if k % 2 == 1 else 0.07
        gl = _bandpass_noise(nh, 7000.0, 10000.0, rng) * _env_exp(nh, 0.006) * amp
        gr = _bandpass_noise(nh, 7000.0, 10000.0, rng) * _env_exp(nh, 0.006) * amp
        _place(drums, start, np.vstack([gl, gr]))
    return bass, drums


SYNTH_CLASSES = {
    "A": (synth_class_A, 26091701),
    "B": (synth_class_B, 26091702),
    "C": (synth_class_C, 26091703),
    "D": (synth_class_D, 26091704),
}


# ── DSP 子进程（真实生产脚本，不重实现）────────────────────────────────
def find_dsp_python() -> str:
    """优先用生产运行时 python（studio_backend.PYTHON），否则当前解释器。"""
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import studio_backend as b; print(b.PYTHON)"],
            cwd=str(REPO), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        cand = (r.stdout or "").strip().splitlines()[-1].strip() if r.returncode == 0 else ""
        if cand and Path(cand).exists():
            return cand
    except Exception:
        pass
    return sys.executable


def _run_dsp(cmd: list[str]) -> str:
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600,
                       env={**os.environ, "PYTHONPATH": str(REPO / "apollo_scripts")})
    if r.returncode != 0:
        tail = (r.stderr or "")[-2500:]
        raise RuntimeError(f"exit {r.returncode}: {cmd[:3]}...\n{tail}")
    return r.stdout or ""


def render_variant(dsp_py: str, work: Path, tag: str, bass_wav: Path,
                   drums_wav: Path, in_mix_wav: Path, v: dict) -> dict:
    """跑 bass_enhance → drum_enhance 两阶段，返回 {final, bass_stdout, drum_stdout}。"""
    vname = tag
    bass_out = work / f"bassout_{vname}.wav"
    final_out = work / f"final_{vname}.wav"

    cmd1 = [dsp_py, str(REPO / "apollo_scripts" / "bass_enhance.py"),
            "--bass", str(bass_wav), "--in-mix", str(in_mix_wav), "--out", str(bass_out),
            "--sub-db", str(v["sub_db"]), "--punch-db", "0",
            "--sat", str(v["sat"]), "--trans", "0"]
    if v["sidechain"] > 0:
        cmd1 += ["--sidechain-drums", str(drums_wav), "--sidechain-amount", str(v["sidechain"])]
    if v["clarity"]:
        cmd1 += ["--auto-clarity", "--clarity-auth", str(v["clarity_auth"])]
    bass_stdout = _run_dsp(cmd1)

    cmd2 = [dsp_py, str(REPO / "apollo_scripts" / "drum_enhance.py"),
            "--drums", str(drums_wav), "--in-mix", str(bass_out), "--out", str(final_out),
            "--punch-db", str(v["punch_db"]), "--trans", str(v["trans"])]
    drum_stdout = _run_dsp(cmd2)
    return {"final": final_out, "bass_stdout": bass_stdout.strip(),
            "drum_stdout": drum_stdout.strip(),
            "bass_cmd": " ".join(os.path.relpath(c, REPO) if c.startswith(str(REPO)) else c for c in cmd1),
            "drum_cmd": " ".join(os.path.relpath(c, REPO) if c.startswith(str(REPO)) else c for c in cmd2)}


# ── 真实歌曲分离 ────────────────────────────────────────────────────────
def htdemucs_4stems_available() -> bool:
    """检查 HF 缓存中的 4 轨 htdemucs 权重（大小写不敏感扫描快照目录）。"""
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    hub = hf_home / "hub"
    if not hub.is_dir():
        return False
    for d in hub.iterdir():
        if d.name.lower().startswith("models--adefossez--htdemucs-6s"):
            continue
        if d.name.lower() == "models--adefossez--htdemucs":
            for snap in (d / "snapshots").glob("*"):
                has_yaml = any(p.name.lower() == "htdemucs.yaml" for p in snap.iterdir())
                has_w = any(p.suffix in (".safetensors", ".th") for p in snap.iterdir())
                if has_yaml and has_w:
                    return True
    return False


def separate_real(dsp_py: str, work: Path) -> dict:
    """demucs htdemucs 分离真实歌曲，返回 {bass, drums, in_mix, note, device} 或 {error}。"""
    info: dict = {}
    try:
        r = subprocess.run([dsp_py, "-c", "import demucs; print('ok')"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"error": f"demucs import failed: {(r.stderr or '')[-500]}"}
    except Exception as exc:
        return {"error": f"demucs import failed: {exc}"}

    sep_dir = work / "real_separation"
    sep_dir.mkdir(parents=True, exist_ok=True)
    cmd = [dsp_py, "-m", "demucs", "--float32", "--clip-mode=none",
           "-n", "htdemucs", "-o", str(sep_dir), str(REAL_INPUT)]
    log(f"    [real] demucs separating {REAL_INPUT.name} ...")
    try:
        _run_dsp(cmd)
    except Exception as exc:
        return {"error": f"demucs separation failed: {exc}"}
    stem_dir = sep_dir / "htdemucs" / REAL_INPUT.stem
    bass, drums = stem_dir / "bass.wav", stem_dir / "drums.wav"
    if not (bass.is_file() and drums.is_file()):
        return {"error": f"separation outputs missing under {stem_dir}"}

    in_mix, sr = read_wav(REAL_INPUT)
    b, sr1 = read_wav(bass)
    d, sr2 = read_wav(drums)
    if not (sr == sr1 == sr2):
        return {"error": f"sample rate mismatch after separation: {sr}/{sr1}/{sr2}"}
    # 防御：对齐到公共长度（demucs 偶尔有 1 样本级差）
    n = min(in_mix.shape[1], b.shape[1], d.shape[1])
    for name, arr in (("bass", b), ("drums", d), ("in_mix", in_mix)):
        if arr.shape[1] != n:
            info[f"trimmed_{name}_samples"] = int(arr.shape[1] - n)
    b, d, in_mix = b[:, :n], d[:, :n], in_mix[:, :n]
    write_wav(work / "real_bass.wav", b, sr)
    write_wav(work / "real_drums.wav", d, sr)
    write_wav(work / "real_inmix.wav", in_mix, sr)
    info.update({"bass": str(work / "real_bass.wav"), "drums": str(work / "real_drums.wav"),
                 "in_mix": str(work / "real_inmix.wav"), "sr": sr,
                 "duration_s": round(n / sr, 3)})
    return info


# ── 响度匹配 + 写出 ─────────────────────────────────────────────────────
def process_class(cls_key: str, stems: dict, sr: int, dsp_py: str,
                  work: Path) -> dict:
    """渲染 4 变体 → 响度匹配 → 返回 per-variant 记录（未盲命名）。"""
    rec: dict = {"sr": sr, "variants": {}}
    bass_wav, drums_wav, in_mix_wav = stems["bass"], stems["drums"], stems["in_mix"]

    for vname in VARIANT_ORDER:
        tag = f"{cls_key}_{vname}"
        log(f"    [dsp] {cls_key}/{vname} ...")
        try:
            out = render_variant(dsp_py, work, tag, bass_wav, drums_wav, in_mix_wav,
                                 VARIANTS[vname])
            arr, srv = read_wav(out["final"])
            if srv != sr:
                raise RuntimeError(f"render sr mismatch: {srv} != {sr}")
            if not np.isfinite(arr).all():
                raise RuntimeError("render contains non-finite samples")
            rec["variants"][vname] = {**out, "array": arr,
                                      "render_lufs": lufs_of(arr, sr)[0],
                                      "error": None}
        except Exception as exc:
            log(f"    [dsp] {cls_key}/{vname} FAILED: {exc}")
            rec["variants"][vname] = {"final": None, "error": str(exc),
                                      "bass_stdout": "", "drum_stdout": ""}

    ok = [v for v in VARIANT_ORDER
          if rec["variants"][v].get("array") is not None]
    if "ref0" not in ok:
        rec["match_error"] = "ref0 render failed; loudness matching skipped"
        return rec

    ref = rec["variants"]["ref0"]["array"]
    ref_lufs = rec["variants"]["ref0"]["render_lufs"]
    rec["ref_lufs_render"] = ref_lufs

    # 每变体一个固定增益 + 削波余量检查（必要时整类统一衰减，含 ref0）
    gains = {}
    headroom_need = -120.0
    for v in ok:
        if v == "ref0":
            gains[v] = 0.0
            continue
        g = ref_lufs - rec["variants"][v]["render_lufs"]
        gains[v] = g
        headroom_need = max(headroom_need, peak_db(rec["variants"][v]["array"]) + g - CLIP_CEILING_DB)
    atten = float(max(0.0, headroom_need))
    rec["class_atten_db"] = atten
    if atten > 1e-6:
        log(f"    [match] {cls_key}: applying class-wide attenuation -{atten:.2f} dB for headroom")
        for v in ok:
            rec["variants"][v]["array"] *= db_to_lin(-atten)
        ref_lufs -= atten
    for v in ok:
        if v != "ref0":
            rec["variants"][v]["array"] *= db_to_lin(gains[v])
    rec["match_gains_db"] = gains
    rec["ref_lufs_matched"] = ref_lufs
    return rec


# ── 盲命名 + manifest ───────────────────────────────────────────────────
def fmt_knobs(v: dict) -> str:
    return (f"sub={v['sub_db']}dB sat={v['sat']} punch={v['punch_db']}dB trans={v['trans']} "
            f"sidechain={v['sidechain']} auto_clarity={'on(auth=%s)' % v['clarity_auth'] if v['clarity'] else 'off'}")


def write_manifests(packed: list[dict], recs: dict, real_info: dict,
                    dsp_py: str, repover: str, round_num: int = 1) -> None:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    purpose = ("低频弹性隔离实验（Round 2）：sub 固定 2dB，弹性（punch/trans/sidechain/"
               "clarity 授权）递增，rc0 为第一轮锚点") if round_num == 2 else \
              "低频（bass/drums）DSP 试听校准（开发规格 §6.7 / §13）"
    machine = {
        "schema": "sorenstudio_listening_pack/1",
        "round": round_num,
        "purpose": purpose,
        "generated": now,
        "repo_version": repover,
        "blind_seed": BLIND_SEED,
        "dsp_python": dsp_py,
        "lufs_method": "audio_metrics.integrated_lufs (repo module)",
        "loudness_match": {"reference": "ref0 per class", "tolerance_lu": MATCH_TOL_LU,
                           "clip_ceiling_dbfs": CLIP_CEILING_DB},
        "variants": {k: {kk: vv for kk, vv in v.items()} for k, v in VARIANTS.items()},
        "separation": real_info,
        "notes": [
            "合成素材为程序化合成，不包含 AI 分离残差（separation artifacts 不在本包覆盖范围内）。",
            "真实歌曲素材（若包含）包含分离残差，用 demucs htdemucs 四轨分离。",
            "所有非 ref0 变体按 integrated LUFS 匹配到同素材 ref0（残差 ≤ 0.2 LU）。",
            "mono 文件为匹配后立体声文件的 (L+R)/2 折叠（双声道等电平写出）。",
            f"盲名由固定种子 {BLIND_SEED} 洗牌，映射仅见 KEY.md（先听后读）。",
        ],
        "files": packed,
        "dsp_diagnostics": {},
    }
    if round_num == 2:
        machine["notes"].append(
            "Round 2 假设：rc1/rc1e 的响度匹配增益应小于第一轮 default（R 组 +3.00dB）——"
            "sub=2 抬升峰值更少，-0.5dBTP 静态天花板引发的匹配缩放更小。实际值见 files[].match_gain_db。")

    # 诊断（按 素材 → 变体）
    diag = {}
    for cls_key, rec in recs.items():
        diag[cls_key] = {}
        for vname, vr in rec["variants"].items():
            diag[cls_key][vname] = {
                "bass_stdout": vr.get("bass_stdout", ""),
                "drum_stdout": vr.get("drum_stdout", ""),
                "error": vr.get("error"),
            }
            if vr.get("bass_cmd"):
                diag[cls_key][vname]["bass_cmd"] = vr["bass_cmd"]
                diag[cls_key][vname]["drum_cmd"] = vr["drum_cmd"]
    machine["dsp_diagnostics"] = diag

    (PACK_DIR / "manifest.json").write_text(
        json.dumps(machine, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── MANIFEST.md ──
    L: list[str] = []
    if round_num == 2:
        L.append("# ShadowBuster 低频试听校准包 Round 2（弹性隔离：sub 固定 2dB，弹性递增）\n")
    else:
        L.append("# ShadowBuster 低频试听校准包（§6.7 低频专项验收 / §13 试听校准）\n")
    L.append(f"- 生成时间：{now}　版本：{repover}　盲听种子：{BLIND_SEED}")
    L.append(f"- DSP：真实生产脚本 apollo_scripts/bass_enhance.py + drum_enhance.py（子进程原样调用）")
    L.append(f"- 响度匹配：integrated LUFS（audio_metrics），非 ref 变体 → 同素材 ref0，残差 ≤ {MATCH_TOL_LU} LU；"
             f"削波余量不足时整类统一衰减后再匹配。文件均为 32-bit float WAV。")
    L.append(f"- **合成素材不含分离残差**：separation artifacts 不在本包覆盖范围内；"
             f"真实歌曲素材（如有）包含分离残差与全频段内容。\n")
    L.append("## 听音时回答的问题（§6.7）\n")
    for q in LISTEN_QUESTIONS:
        L.append(f"- {q}")
    L.append("")
    L.append("## 比对协议（§13.4）\n")
    L.append("- 所有变体已响度匹配（≤ 0.2 LU），用**外部播放器**以舒适音量逐对比较，不要用浏览器/混音软件做判断。")
    L.append("- **偏好与损伤分开评**：哪个更好听（preference）与 是否出现抽吸/变薄/断裂/染色（damage）分别记录。")
    if round_num == 2:
        L.append("- **隔离实验**：非 ref 变体的 sub/sat 完全相同（低频量维度固定），仅弹性维度"
                 "（punch/trans/sidechain/clarity 授权）递增；rc0 为第一轮锚点（低频量对、弹性不足）。")
    else:
        L.append("- **强设置应仍能表达用户意图**：strong 档若听起来与 default 无差别或反而更弱，属于校准问题，请记录。")
    L.append("- 同素材类内比较（A 组内比 A、B 组内比 B …）；mono 文件用于小音箱/单声道检验。")
    L.append("- ref0 是无处理恒等参考（响度匹配后），可作为“原样”基线。\n")

    for cls_key, rec in recs.items():
        L.append(f"## 素材 {cls_key} — {MATERIAL_DESC[cls_key]}\n")
        L.append("| 盲名 | 时长(s) | LUFS(匹配后) | 增益(dB, 相对渲染) | 备注 |")
        L.append("|---|---|---|---|---|")
        for p in packed:
            if p["class"] != cls_key or p.get("mono"):
                continue
            note = "恒等参考" if p["variant"] == "ref0" else ""
            if p.get("class_atten_db", 0) > 1e-6:
                note = (note + "；" if note else "") + f"整类统一衰减 -{p['class_atten_db']:.2f}dB"
            L.append(f"| {p['file']} | {p['duration_s']:.2f} | {p['lufs']:.2f} | "
                     f"{p['match_gain_db']:+.2f} | {note} |")
        mono_rows = [p for p in packed if p["class"] == cls_key and p.get("mono")]
        if mono_rows:
            L.append("")
            L.append("mono 折叠 (L+R)/2（双声道等电平；折叠后 LUFS 与立体声版本不同属预期）：")
            L.append("")
            L.append("| 盲名 | 时长(s) | LUFS |")
            L.append("|---|---|---|")
            for p in mono_rows:
                L.append(f"| {p['file']} | {p['duration_s']:.2f} | {p['lufs']:.2f} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("# ⚠️ 以下为 DSP 诊断信息（含变体身份剧透，完成听音前勿往下读）\n")
    for cls_key, rec in recs.items():
        L.append(f"## 素材 {cls_key} 诊断\n")
        for vname in VARIANT_ORDER:
            vr = rec["variants"].get(vname, {})
            if vr.get("error"):
                L.append(f"- {vname}: **渲染失败** — {vr['error'][:400]}")
                continue
            L.append(f"- {vname}（旋钮：{fmt_knobs(VARIANTS[vname])}）")
            for line in (vr.get("bass_stdout") or "").splitlines():
                L.append(f"  - bass: {line}")
            for line in (vr.get("drum_stdout") or "").splitlines():
                L.append(f"  - drums: {line}")
        L.append("")
    L.append("盲名 ↔ 变体映射见 **KEY.md**（先完成听音再读）。\n")
    (PACK_DIR / "MANIFEST.md").write_text("\n".join(L), encoding="utf-8")

    # ── KEY.md（剧透，放最后）──
    K: list[str] = []
    K.append("# KEY — 盲名映射（剧透）\n")
    K.append("> **先完成听音再读本文件。**\n")
    K.append("| 盲名 | 素材 | 变体 | 旋钮 | 匹配增益(dB) |")
    K.append("|---|---|---|---|---|")
    for p in packed:
        if p.get("mono"):
            continue
        K.append(f"| {p['file']} | {p['class']} | {p['variant']} | "
                 f"{fmt_knobs(VARIANTS[p['variant']])} | {p['match_gain_db']:+.2f} |")
    K.append("")
    K.append("注：mono 文件与其立体声源同名 + `_mono` 后缀（如 `A_v2.wav` → `A_v2_mono.wav`），"
             "变体一致。\n")
    (PACK_DIR / "KEY.md").write_text("\n".join(K), encoding="utf-8")


# ── 主流程 ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--round", type=int, default=1, choices=(1, 2),
                    help="1=第一轮全量校准包（默认，行为不变）；2=弹性隔离轮（R+A+B，输出 round2/）")
    ap.add_argument("--skip-real", action="store_true", help="跳过真实歌曲分离/渲染")
    ap.add_argument("--keep-work", action="store_true", help="保留 _work 中间产物")
    args = ap.parse_args()

    if args.round == 2:
        global VARIANT_ORDER, VARIANTS, MONO_VARIANTS, BLIND_SEED, PACK_DIR, WORK_DIR, LISTEN_QUESTIONS
        VARIANT_ORDER = list(ROUND2_VARIANT_ORDER)
        VARIANTS = dict(ROUND2_VARIANTS)
        MONO_VARIANTS = list(ROUND2_VARIANT_ORDER)      # round 2：全部 4 变体做 mono 折叠
        BLIND_SEED = ROUND2_BLIND_SEED
        PACK_DIR = REPO / "listening_pack" / "round2"
        WORK_DIR = PACK_DIR / "_work"
        LISTEN_QUESTIONS = list(ROUND2_LISTEN_QUESTIONS)

    log(f"listening pack -> {PACK_DIR}")
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    dsp_py = find_dsp_python()
    log(f"dsp python: {dsp_py}")

    try:
        repover = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO),
                                 capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        repover = ""
    repover = f"v1.6.10{(' @ ' + repover) if repover else ''}"

    recs: dict[str, dict] = {}
    packed: list[dict] = []

    # 1) 合成素材类
    for cls_key, (fn, seed) in SYNTH_CLASSES.items():
        if args.round == 2 and cls_key not in ROUND2_CLASSES:
            continue
        log(f"[class {cls_key}] synthesizing (seed {seed}) ...")
        rng = np.random.default_rng(seed)
        bass, drums = fn(rng)
        bass, drums, in_mix = _normalize_pair(bass, drums)
        stems_dir = WORK_DIR / f"class_{cls_key}"
        stems_dir.mkdir(parents=True, exist_ok=True)
        write_wav(stems_dir / "bass.wav", bass, SR)
        write_wav(stems_dir / "drums.wav", drums, SR)
        write_wav(stems_dir / "in_mix.wav", in_mix, SR)
        log(f"    peaks: bass {peak_db(bass):.2f} dBFS, drums {peak_db(drums):.2f} dBFS, "
            f"in_mix {peak_db(in_mix):.2f} dBFS")
        recs[cls_key] = process_class(cls_key,
                                      {"bass": stems_dir / "bass.wav",
                                       "drums": stems_dir / "drums.wav",
                                       "in_mix": stems_dir / "in_mix.wav"},
                                      SR, dsp_py, stems_dir)
        recs[cls_key]["n_samples"] = int(bass.shape[1])

    # 2) 真实歌曲（若可用）
    real_info: dict = {"available": False, "note": "skipped by --skip-real" if args.skip_real else ""}
    if not args.skip_real:
        log("[real] checking demucs + htdemucs weights ...")
        try:
            r = subprocess.run([dsp_py, "-c",
                                "import studio_backend as b; print(b.six_stem_weights_available())"],
                               cwd=str(REPO), capture_output=True, text=True, timeout=180,
                               env={**os.environ, "PYTHONPATH": str(REPO)})
            six_ok = r.stdout.strip().splitlines()[-1] == "True" if r.returncode == 0 else False
        except Exception:
            six_ok = False
        four_ok = htdemucs_4stems_available()
        real_info.update({"six_stem_weights": bool(six_ok), "htdemucs_4stem_weights": four_ok})
        if not four_ok:
            real_info.update({"available": False,
                              "note": "4-stem htdemucs weights not found in HF cache; skipped"})
            log("    real-music renders skipped: 4-stem htdemucs weights unavailable")
        elif not REAL_INPUT.is_file():
            real_info.update({"available": False, "note": f"input missing: {REAL_INPUT}"})
            log(f"    real-music renders skipped: {REAL_INPUT} missing")
        else:
            sep = separate_real(dsp_py, WORK_DIR)
            real_info.update(sep)
            if "error" in sep or "bass" not in sep:
                real_info["available"] = False
                log(f"    real-music renders skipped: {sep.get('error', sep)}")
            else:
                real_info["available"] = True
                log(f"    separation ok ({sep['duration_s']}s)")
                recs[REAL_CLASS] = process_class(
                    REAL_CLASS,
                    {"bass": Path(sep["bass"]), "drums": Path(sep["drums"]),
                     "in_mix": Path(sep["in_mix"])},
                    sep["sr"], dsp_py, WORK_DIR / "class_R" if False else WORK_DIR)
                recs[REAL_CLASS]["n_samples"] = int(sep["duration_s"] * sep["sr"])
    else:
        real_info["htdemucs_4stem_weights"] = htdemucs_4stems_available()

    # 3) 盲命名 + 写出匹配后文件 + mono
    class_order = [c for c in ["A", "B", "C", "D", REAL_CLASS] if c in recs]
    blind_rng = np.random.default_rng(BLIND_SEED)
    failures: list[str] = []
    log("")
    log(f"writing matched outputs (blind naming, seed {BLIND_SEED}) ...")
    summary_rows: list[tuple] = []

    for cls_key in class_order:
        rec = recs[cls_key]
        sr = rec["sr"]
        present = [v for v in VARIANT_ORDER if rec["variants"][v].get("array") is not None]
        missing = [v for v in VARIANT_ORDER if v not in present]
        for v in missing:
            failures.append(f"{cls_key}/{v}: render failed — {rec['variants'][v].get('error', '')[:200]}")
        if not present:
            continue
        perm = blind_rng.permutation(len(present))
        codes = {v: f"{cls_key}_v{perm[i] + 1}" for i, v in enumerate(present)}
        ref_arr = rec["variants"]["ref0"]["array"] if "ref0" in present else None
        ref_len = ref_arr.shape[1] if ref_arr is not None else None

        ref_out_path = None
        for v in present:
            arr = rec["variants"][v]["array"]
            lufs_m, lufs_status = lufs_of(arr, sr)
            code = codes[v]
            out_path = PACK_DIR / f"{code}.wav"
            write_wav(out_path, arr, sr)
            if v == "ref0":
                ref_out_path = out_path
            # mono 折叠（匹配后）
            mono_path = None
            if v in MONO_VARIANTS:
                mono = (arr[0] + arr[1]) / 2.0
                mono = np.vstack([mono, mono])
                mono_path = PACK_DIR / f"{code}_mono.wav"
                write_wav(mono_path, mono, sr)

            # 校验：有限、长度 vs ref0、LUFS 残差
            chk, _ = read_wav(out_path)
            finite = bool(np.isfinite(chk).all())
            len_ok = True if ref_len is None else chk.shape[1] == ref_len
            if v == "ref0":
                residual = 0.0
            else:
                residual = lufs_m - rec["ref_lufs_matched"]
            ok = finite and len_ok and (v == "ref0" or abs(residual) <= MATCH_TOL_LU)
            if not ok:
                failures.append(f"{out_path.name}: finite={finite} len_ok={len_ok} residual={residual:.3f}")

            gain = rec["match_gains_db"].get(v, 0.0) if "match_gains_db" in rec else 0.0
            entry = {"file": out_path.name, "class": cls_key, "variant": v, "mono": False,
                     "sr": sr, "duration_s": round(chk.shape[1] / sr, 3),
                     "samples": int(chk.shape[1]), "lufs": round(lufs_m, 2),
                     "lufs_status": lufs_status,
                     "match_gain_db": round(gain, 2),
                     "match_residual_lu": round(residual, 3),
                     "class_atten_db": round(rec.get("class_atten_db", 0.0), 2),
                     "knobs": VARIANTS[v], "finite": finite, "length_ok": len_ok,
                     "verified": ok}
            packed.append(entry)
            summary_rows.append((out_path.name, cls_key, v, entry["duration_s"],
                                 entry["lufs"], entry["match_gain_db"],
                                 entry["match_residual_lu"], ok))

            if mono_path is not None:
                mchk, msr = read_wav(mono_path)
                mlufs, mstatus = lufs_of(mchk, msr)
                mfinite = bool(np.isfinite(mchk).all())
                mlen_ok = mchk.shape[1] == chk.shape[1]
                packed.append({"file": mono_path.name, "class": cls_key, "variant": v,
                               "mono": True, "sr": msr,
                               "duration_s": round(mchk.shape[1] / msr, 3),
                               "samples": int(mchk.shape[1]), "lufs": round(mlufs, 2),
                               "lufs_status": mstatus, "match_gain_db": round(gain, 2),
                               "match_residual_lu": None,
                               "knobs": VARIANTS[v], "finite": mfinite,
                               "length_ok": mlen_ok, "verified": mfinite and mlen_ok})
                summary_rows.append((mono_path.name, cls_key, f"{v} mono",
                                     packed[-1]["duration_s"], packed[-1]["lufs"],
                                     packed[-1]["match_gain_db"], float("nan"),
                                     mfinite and mlen_ok))

        rec["blind_codes"] = {v: codes[v] for v in present}
        log(f"  class {cls_key}: " + ", ".join(f"{codes[v]}={v}" for v in present))

    # 4) manifest
    write_manifests(packed, recs, real_info, dsp_py, repover, round_num=args.round)

    # 5) 汇总表
    log("")
    log("=" * 96)
    log(f"{'file':<18}{'cls':<5}{'variant':<16}{'dur(s)':>7}{'LUFS':>8}{'gain(dB)':>10}"
        f"{'resid(LU)':>11}  ok")
    log("-" * 96)
    for name, cls_key, v, dur, lufs, gain, resid, ok in summary_rows:
        rs = "  -" if np.isnan(resid) else f"{resid:9.3f}"
        log(f"{name:<18}{cls_key:<5}{v:<16}{dur:>7.2f}{lufs:>8.2f}{gain:>+10.2f}{rs:>11}  "
            f"{'OK' if ok else 'FAIL'}")
    log("=" * 96)

    if failures:
        log("\nFAILURES:")
        for f in failures:
            log(f"  - {f}")
    if not args.keep_work:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        log(f"\nwork dir cleaned ({PACK_DIR / '_work'} removed); re-run regenerates deterministically")
    log(f"\nmanifest: {PACK_DIR / 'MANIFEST.md'} | key: {PACK_DIR / 'KEY.md'} | "
        f"json: {PACK_DIR / 'manifest.json'}")
    log("先完成听音再读 KEY.md。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
