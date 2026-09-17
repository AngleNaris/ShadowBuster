"""ShadowBuster 后端管线：Lew 高频 → Demucs 4-stem 分离 → bass 增强 → drums 增强 → Soren 母带
所有阶段调用已验证的命令行工具（subprocess），支持进度回调与取消。
"""
import json
import math
import os
import signal
import sys
import time
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path

from apollo_scripts.stage_metadata import (SCHEMA_VERSION, audio_metadata,
                                           read_report, write_report)
from apollo_scripts.vocal_config import REFERENCE_MODE
from audio_metrics import (quality_report_path, quality_summary, read_quality_report,
                           write_quality_report)
# 应用版本号（单一来源）：设置界面显示 / 打包与安装器读取。
# 与 packaging/installer.iss 的 MyAppVersion 保持一致（tests/test_app_version.py 有同步校验）。
APP_VERSION = "1.6.9"
GPU_ENV_VERSION = "1.5.0"

if getattr(sys, "frozen", False):
    # PyInstaller 冻结后 __file__ 在 _internal 里，exe 同级才是安装根目录
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent

DEV_PYTHON = r"D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe"
DEV_APOLLO = Path(r"D:/_3.AI/audio_upscale/Apollo")
DEV_DSP = Path(__file__).parent / "apollo_scripts"
DEV_SOREN = Path(r"D:/_3.AI/audio_upscale/Soren_src")
# 开发态 canonical Soren 运行时（tools/make_dev_runtime.py 生成）：
# 代码与打包权威逐字节一致，资源经 junction 引用 SB_SOREN/外部，绝不写外部。
DEV_SOREN_RUNTIME = Path(__file__).parent / "dev_runtime" / "Soren_src"


_DEV_RUNTIME_TOOL = None


def _load_dev_runtime_tool():
    """按路径加载 tools/make_dev_runtime.py（纯标准库，导入无副作用）。"""
    global _DEV_RUNTIME_TOOL
    if _DEV_RUNTIME_TOOL is None:
        import importlib.util
        tool_path = Path(__file__).parent / "tools" / "make_dev_runtime.py"
        spec = importlib.util.spec_from_file_location("make_dev_runtime", tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["make_dev_runtime"] = module
        spec.loader.exec_module(module)
        _DEV_RUNTIME_TOOL = module
    return _DEV_RUNTIME_TOOL


def _dev_soren_dir():
    """开发态 Soren 目录（惰性）：一律返回 canonical dev runtime 路径。

    import 时不生成、不校验、不读任何外部资源——新 checkout 无 Soren 资源时
    仍可导入本模块、跑 CLI --help 与全部非 Soren 测试。实际执行 Soren 前
    必须经 _ensure_dev_runtime() 校验/生成；绝不回退执行外部旧 DSP。
    """
    return DEV_SOREN_RUNTIME


def _ensure_dev_runtime():
    """执行 Soren / 计算指纹前调用：校验/生成 canonical dev runtime（仅开发态）。

    打包态（frozen 或 runtime/SB_ASSETS 已解析、ASSETS 非 None）直接返回已解析
    的 SOREN_DIR，绝不触发 dev 工具。开发态缺失/陈旧/资源源（SB_SOREN）变化时
    经 tools/make_dev_runtime.py 安全重建；含未知内容或用户改动时拒绝并指向
    工具，绝不删除未知文件。
    """
    if getattr(sys, "frozen", False) or ASSETS is not None:
        return Path(SOREN_DIR)
    try:
        return _load_dev_runtime_tool().ensure(target=DEV_SOREN_RUNTIME)
    except Exception as exc:
        raise RuntimeError(
            "开发态 Soren runtime 不可用且无法安全生成（拒绝回退执行外部旧 DSP）。"
            "请运行 python tools/make_dev_runtime.py（资源源经 SB_SOREN 指定，"
            f"默认 D:/_3.AI/audio_upscale/Soren_src）。原因：{exc}") from exc

# 无控制台的 GUI 进程里，子进程默认会新建可见控制台（安装版弹 python 黑框）；
# CREATE_NO_WINDOW 让推理 / ffmpeg 子进程全程无窗。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _user_gpu_py():
    """GPU 环境（设置内下载的 CUDA 运行时，v1.5，Windows 专属）。

    新版装在应用目录 runtime-gpu\\env；旧版用户目录
    LOCALAPPDATA\\ShadowBuster\\runtime-gpu\\env 仍被识别使用。
    两处任一可用即采用，仅替换解释器，Apollo/Soren/权重仍读安装目录。
    macOS 无 CUDA 运行时下载机制（MPS/CPU 随系统内置），直接返回 None。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    try:
        import gpu_env as ge
        return ge.usable_python(expected_version=GPU_ENV_VERSION)
    except (ImportError, OSError, ValueError):
        return None


def _resolve_runtime():
    """解析工具链根目录。

    部署包：优先取 exe 同级 runtime/（安装器就地放下，无需环境变量）；
    其次 SB_ASSETS 环境变量；都未设置时回退到开发机布局（Windows 硬编码
    DEV_* 仅是兜底，mac 上请设置 SB_PYTHON/SB_APOLLO/SB_SOREN，见 start.sh）；
    macOS .app 的 runtime 也可能随 PyInstaller datas 落在 _internal/runtime。
    GPU 环境（应用目录 runtime-gpu 或旧版用户目录）仅覆盖解释器。
    """
    upy = _user_gpu_py()
    if upy is not None:
        root = ROOT / "runtime"
        apollo, soren = root / "Apollo", root / "Soren_src"
        if apollo.is_dir() and soren.is_dir():
            return upy, apollo, soren, root
    roots = [ROOT / "runtime"]
    if os.name != "nt":
        # PyInstaller 冻结后 datas 在 _internal 下；开发布局不存在该目录，自然跳过
        roots.append(ROOT / "_internal" / "runtime")
    if os.environ.get("SB_ASSETS"):
        roots.append(Path(os.environ["SB_ASSETS"]))
    for root in roots:
        apollo, soren = root / "Apollo", root / "Soren_src"
        if apollo.is_dir() and soren.is_dir():
            # 便携 Python 布局：Windows env\python.exe（旧 venv env\Scripts\python.exe）；
            # POSIX env/bin/python
            if os.name == "nt":
                py = root / "env" / "python.exe"
                if not py.exists():
                    py = root / "env" / "Scripts" / "python.exe"
            else:
                py = root / "env" / "bin" / "python"
            return (py if py.exists() else sys.executable), apollo, soren, root
    return (
        Path(os.environ.get("SB_PYTHON", DEV_PYTHON)),
        Path(os.environ.get("SB_APOLLO", DEV_APOLLO)),
        _dev_soren_dir(),
        None,
    )


PYTHON, APOLLO_DIR, SOREN_DIR, ASSETS = _resolve_runtime()
# APOLLO_DIR remains the model/tool root (Lew script and weights). DSP_DIR is
# independent so development uses repository DSP sources while packaged runtime
# uses the synchronized runtime Apollo scripts.
DSP_DIR = (APOLLO_DIR if ASSETS is not None or getattr(sys, "frozen", False)
           else Path(os.environ.get("SB_DSP", DEV_DSP)))


def auto_device():
    """推理设备：通过子进程探测 MPS / CUDA，不在 UI 进程中 import torch。
    （QtWebEngine + 同进程 torch CUDA 初始化会卡死，内存缓涨无资源占用）
    SB_DEVICE=mps/cuda/cpu 可显式覆盖（如 MPS 与某模型不兼容时强制 CPU）。
    探测优先级：macOS 的 Apple Silicon MPS → CUDA → CPU，与 torch 的
    平台语义一致：macOS 上 torch.cuda.is_available() 恒为 False。
    开发版 GUI 解释器通常没有 torch，故一律用 PYTHON（带 torch 的运行时）
    做子进程探测，避免在无 torch 的分支误判为 CPU。"""
    override = os.environ.get("SB_DEVICE", "").strip().lower()
    if override in ("mps", "cuda", "cpu"):
        _tr(f"auto_device: override SB_DEVICE={override}")
        return override
    _tr("auto_device: start")
    py = PYTHON
    probe = ("import torch;"
             "_mps = getattr(torch.backends, 'mps', None) is not None "
             "and torch.backends.mps.is_available();"
             "print('mps' if _mps else ('cuda' if torch.cuda.is_available() else 'cpu'))")
    try:
        probe_run = subprocess.run(
            [str(py), "-c", probe],
            capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
        )
        dev = probe_run.stdout.strip().splitlines()[-1] if probe_run.stdout.strip() else ""
        _tr(f"auto_device: probe({py}) -> {dev!r} err={probe_run.stderr[-200:]!r}")
        return dev if dev in ("mps", "cuda", "cpu") else "cpu"
    except Exception as e:
        _tr(f"auto_device: probe err {e!r}")
        return "cpu"


def ffmpeg_bin():
    """ffmpeg 可执行：优先 runtime 内置 → 环境变量 → PATH。"""
    if ASSETS is not None:
        exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        cand = ASSETS / "ffmpeg" / "bin" / exe_name
        if cand.exists():
            return str(cand)
    env = os.environ.get("SB_FFMPEG")
    if env and Path(env).exists():
        return env
    which = shutil.which("ffmpeg")
    return which if which else "ffmpeg"


GENRES = ["Pop", "EDM", "Rock", "Dance", "Hiphop", "Ambient", "Chillout", "Orchestral", "Speech", "Piano"]
LOUDNESS = ["soft", "dynamic", "normal", "loud"]
EQ_PROFILES = ["Neutral", "Warm", "Bright", "Fusion"]


def _tr(msg):
    """SB_TRACE=1 时把诊断打点写文件（QWebEngine 应用里 stdout 不可靠）。"""
    if not os.environ.get("SB_TRACE"):
        return
    try:
        with open(os.environ.get("SB_TRACE_FILE", "/tmp/sb_trace.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


class PipelineError(Exception):
    pass


# 活跃子进程注册表：窗口关闭 / 全局停止时兜底清树，防止后台残留继续跑
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def _kill_tree(proc):
    """终止整棵进程树。proc.kill() 只杀直接子进程，demucs 的 DataLoader
    worker 等孙进程会残留继续占用 CPU/GPU。Windows 用 taskkill /T 连根拔；
    POSIX 下管线子进程以独立进程组启动（见 _run_stream 的 start_new_session），
    killpg(pgid=pid) 一次带走整棵树。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    for kill in (proc.kill, proc.wait):
        try:
            kill()
        except Exception:
            pass


def terminate_all():
    """杀掉所有仍活跃的管线子进程（UI 关闭时调用）。"""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        _kill_tree(p)


def _run(cmd, cwd, timeout=None, env=None):
    """运行子进程并返回 stdout；非零退出码抛错。"""
    if os.environ.get("SB_TRACE"):
        print(f"[trace] _run: {' '.join(map(str, cmd[:6]))} (cwd={cwd})", flush=True)
    if env is None:
        env = os.environ.copy()
    env = dict(env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
    )
    if os.environ.get("SB_TRACE"):
        print(f"[trace] _run done rc={proc.returncode}", flush=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise PipelineError(f"命令失败（{proc.returncode}）: {' '.join(map(str, cmd[:3]))}…\n{tail}")
    return proc.stdout


def _run_stream(cmd, cwd, env=None, on_progress=None, cancel=None):
    """流式运行子进程：实时解析进度行（LEW_PROGRESS / tqdm %），支持取消。

    读管道放在独立线程，主循环带超时轮询——否则子进程长时间无输出时
    （如 Soren 静默计算段）read1 会一直阻塞，取消永远得不到检查。
    on_progress(pct01) 只在进度前进时回调；返回捕获的全部输出文本。
    """
    import queue as _queue
    import re as _re
    if env is None:
        env = os.environ.copy()
    env = dict(env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # POSIX 下让子进程自成进程组，取消/退出时 killpg 连孙进程一起清；
        # Windows 上该参数被忽略，杀树走 taskkill /T。
        start_new_session=(os.name != "nt"),
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)

    q: "_queue.Queue" = _queue.Queue()

    def _pump(stream, q):
        try:
            while True:
                chunk = stream.read1(8192)
                if not chunk:
                    break
                q.put(chunk)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True).start()

    buf, tail = "", ""
    last_pct = -1.0
    try:
        while True:
            if cancel and cancel():
                _kill_tree(proc)
                raise PipelineError("用户取消")
            try:
                chunk = q.get(timeout=0.2)
            except _queue.Empty:
                continue
            if chunk is None:
                break
            buf += chunk.decode("utf-8", errors="replace")
            parts = _re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for line in parts:
                # 累积最近输出（含多行 traceback），失败时据此显示真实原因
                tail = (tail + "\n" + line)[-4000:]
                if not on_progress:
                    continue
                m = _re.search(r"LEW_PROGRESS\s+([\d.]+)", line) or _re.search(r"(\d{1,3})%\|", line)
                if m:
                    pct = min(100.0, max(0.0, float(m.group(1))))
                    if pct > last_pct:
                        last_pct = pct
                        on_progress(pct / 100.0)
        proc.wait()
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    if proc.returncode != 0:
        raise PipelineError(f"命令失败（{proc.returncode}）: {' '.join(map(str, cmd[:3]))}…\n{tail}")
    return buf


# 内部音频格式版本：所有 FFmpeg 中转、Lew 干湿混合、参考分离产物统一 44.1k/双声道/
# 32-bit float WAV；最终量化只发生在 Soren 的 PCM24 输出。版本号进入缓存身份，
# 旧 PCM16 时代的缓存产物不与本版本产物混用。
AUDIO_FORMAT_VERSION = "f32-internal-1"


def ffmpeg_convert(src, dst, sr=44100, subtype="FLOAT"):
    """内部中转统一解码/重采样为 32-bit float WAV（浮点无量化损失）。

    生产链内部不再产生 PCM16 中间件；输出后校验采样率/声道/可读性，
    失败即报错——绝不静默回退 PCM16 进入生产路径（外部兼容场景需显式传参）。
    """
    encoding = {"FLOAT": "pcm_f32le", "PCM_16": "pcm_s16le"}.get(subtype)
    if encoding is None:
        raise PipelineError(f"不支持的内部音频格式: {subtype!r}")
    cmd = [ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(src),
           "-ar", str(sr), "-ac", "2", "-c:a", encoding, str(dst)]
    _run(cmd, cwd=ROOT)
    try:
        import soundfile as sf
        info = sf.info(str(dst))
        if info.samplerate != int(sr) or info.channels != 2:
            raise RuntimeError(
                f"converted file mismatch: sr={info.samplerate} ch={info.channels}")
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        raise PipelineError(f"音频转换校验失败: {dst} ({exc!r})") from exc


# 快速档限制单块长度以降低峰值显存；少量重叠控制重复计算。
# 标准与精细档保留原分块策略，档位不改变模型精度。
QUALITY_CHUNKS = {0: (6.0, 0.5), 1: (15.0, 2.0), 2: (10.0, 3.0)}


def mix_wet_dry(dry, wet, out, wet_ratio):
    """按 wet_ratio 加权混合 Lew 重建结果与干信号（重建强度）。

    wet_ratio ∈ [0, 1]；0 = 保留原信号，1 = 完全重建。float64 计算、float32
    写出（FLOAT WAV）——中间不再有 16-bit 截断台阶，量化只发生在最终 Soren
    PCM24 输出。峰值保护沿用 0.999 上限；写盘依赖 soundfile（生产运行时必装）。
    """
    import numpy as np
    import soundfile as sf
    a, fr = sf.read(str(dry), dtype="float64", always_2d=True)
    b, fr_wet = sf.read(str(wet), dtype="float64", always_2d=True)
    if fr != fr_wet:
        raise PipelineError(f"sample rate mismatch: {dry} is {fr}, {wet} is {fr_wet}")
    n = min(a.shape[0], b.shape[0])
    nc = min(a.shape[1], b.shape[1])
    mix = a[:n, :nc] * (1.0 - wet_ratio) + b[:n, :nc] * wet_ratio
    peak = float(np.abs(mix).max()) if mix.size else 0.0
    if peak > 0.999:
        mix *= 0.999 / peak
    sf.write(str(out), mix.astype(np.float32), int(fr), subtype="FLOAT")


def _detect_sample_rate(path):
    """返回文件采样率；探测不到（非音频、缺解码依赖）时返回 None。

    None 不在此处报错，按原样透传，交由下游阶段报出真实错误。"""
    try:
        import soundfile as sf
        return sf.info(str(path)).samplerate
    except (ImportError, RuntimeError, ValueError, OSError):
        pass
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            return w.getframerate()
    except Exception:
        return None


def stage_lew(input_wav, out_wav, device="cuda", progress=None, quality=1, guidance=1.5,
              cancel=None):
    if progress:
        progress(0.0, "Lew 高频重建")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(APOLLO_DIR)
    # lew_upscale 需要 44.1k 输入，先用临时目录转一份 44k；
    # 临时产物一律落在系统临时目录，绝不污染原始文件目录，并在结束时强制清理。
    tmpdir = tempfile.mkdtemp(prefix="sb_lew_")
    try:
        tmp_44k = Path(tmpdir) / (input_wav.stem + "_44k_tmp.wav")
        ffmpeg_convert(input_wav, tmp_44k)
        chunk_s, overlap_s = QUALITY_CHUNKS.get(int(quality), (15.0, 2.0))
        cmd = [PYTHON, str(APOLLO_DIR / "lew_upscale.py"),
               "--in_wav", str(tmp_44k), "--out_wav", str(out_wav),
               "--chunk-seconds", str(chunk_s), "--overlap-seconds", str(overlap_s),
               "--device", device]
        # 流式解析 LEW_PROGRESS → 真实阶段进度
        _run_stream(cmd, APOLLO_DIR, env=env, cancel=cancel,
                    on_progress=(lambda f: progress(f, "Lew 高频重建")) if progress else None)
        # 重建引导：按权重把 Lew 重建结果与干信号混合（0=保留原声，2=完全重建）
        wet = max(0.0, min(1.0, float(guidance) / 2.0))
        if wet < 1.0:
            mixed = Path(tmpdir) / (out_wav.stem + "_mix_tmp.wav")
            mix_wet_dry(tmp_44k, out_wav, mixed, wet)
            # 注意：mixed 落在系统临时目录（可能与输出目录分属不同磁盘），
            # os.replace 跨盘会抛 WinError 17，必须用 shutil.move（跨盘拷贝+删除）。
            shutil.move(str(mixed), str(out_wav))
    finally:
        # 无论成功/失败/取消，清理临时目录，避免遗留垃圾文件
        shutil.rmtree(tmpdir, ignore_errors=True)
    if progress:
        progress(1.0, "Lew 完成")


def stage_demucs(input_wav, out_dir, model="htdemucs", progress=None, cancel=None, device=None):
    if progress:
        progress(0.0, f"Demucs 分离（{model}）")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)   # demucs 的 -o 目录需先存在（cwd 也要可进入）
    env = os.environ.copy()
    if ASSETS is not None:
        # 权重已随 runtime 预置 → 定向 TORCH_HOME，保证离线可用
        torch_home = ASSETS / "torch_home"
        hf_home = ASSETS / "hf_home"
        torch_home.mkdir(parents=True, exist_ok=True)
        hf_home.mkdir(parents=True, exist_ok=True)
        env["TORCH_HOME"] = str(torch_home)
        env["HF_HOME"] = str(hf_home)
        env["HF_HUB_OFFLINE"] = "1"
    # 4-stem 全轨输出（vocals/drums/bass/other，求和≈原曲）：鼓的 punch/瞬态处理
    # 需要作用在鼓所在的轨上（实测 kick 起音 ~100% 落在 drums 轨），两轨模式拿不到 drums
    # htdemucs_6s（opt-in）额外输出 guitar/piano；产物目录以模型名命名（见 run_pipeline）。
    cmd = [PYTHON, "-m", "demucs", "--float32", "--clip-mode=none", "-n", model,
           "-o", str(out_dir), str(input_wav)]
    # macOS Apple Silicon：demucs CLI 缺省按 cuda→cpu 选择，MPS 永远选不上，
    # 显式传 -d mps 走 GPU（实测分轨与 CPU 数值一致、约 2 倍速）；CUDA 保持
    # demucs 自带探测，Windows 行为不变。
    if device == "mps":
        cmd += ["-d", "mps"]
    # 流式解析 demucs 的 tqdm 百分比 → 真实阶段进度
    _run_stream(cmd, out_dir, env=env, cancel=cancel,
                on_progress=(lambda f: progress(f, "Demucs 四轨分离")) if progress else None)
    if progress:
        progress(1.0, "分离完成")


def _rest_files(stem_dir):
    """4-stem 输出的"其余轨"：vocals + other；兼容旧 two-stems 输出（minus_bass/no_bass）。"""
    stem_dir = Path(stem_dir)
    if (stem_dir / "vocals.wav").exists() and (stem_dir / "other.wav").exists():
        return [stem_dir / "vocals.wav", stem_dir / "other.wav"]
    for name in ("minus_bass.wav", "no_bass.wav"):
        p = stem_dir / name
        if p.exists():
            return [p]
    raise PipelineError(f"分离产物缺失（需要 vocals/other 或 no_bass）: {stem_dir}")


def _run_reported(cmd, in_mix, out_wav, report_json, stage, cancel):
    if report_json is None:
        _run_stream(cmd, DSP_DIR, cancel=cancel)
        return 1.0
    if Path(in_mix).resolve() == Path(out_wav).resolve():
        raise PipelineError("Reported stages require distinct input/output files")
    Path(report_json).unlink(missing_ok=True)
    Path(out_wav).unlink(missing_ok=True)
    cmd += ["--report-json", str(report_json)]
    _run_stream(cmd, DSP_DIR, cancel=cancel)
    try:
        return read_report(report_json, stage=stage, input_path=in_mix, output_path=out_wav)
    except (ValueError, OSError, RuntimeError, TypeError, AttributeError) as exc:
        raise PipelineError(f"{stage} metadata validation failed: {exc}") from exc


def stage_bass(stem_dir, in_mix, out_wav, sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
               bass_gain_db=0.0, auto_clarity=False, sidechain_amount=0.0,
               sidechain_attack_ms=5.0, sidechain_release_ms=150.0,
               sidechain_max_duck_db=6.0, progress=None, cancel=None, report_json=None):
    """贝斯增强。auto_clarity: opt-in 保守清晰度 EQ（--auto-clarity），
    默认关闭；关闭时命令行与旧行为完全一致（位级透传）。sidechain_amount 默认 0，
    仅在 drums.wav 存在且显式开启时对 Bass 低频应用有界 Kick 侧链。"""
    if progress:
        progress(0.0, "贝斯增强")
    bass = stem_dir / "bass.wav"
    if not bass.exists():
        raise PipelineError(f"分离产物缺失: {stem_dir}")
    cmd = [PYTHON, str(DSP_DIR / "bass_enhance.py"),
           "--bass", str(bass), "--in-mix", str(in_mix), "--out", str(out_wav),
           "--sub-db", str(sub_db), "--sat", str(sat), "--punch-db", str(punch_db),
           "--trans", str(trans), "--bass-gain-db", str(bass_gain_db),
           "--sidechain-amount", str(sidechain_amount),
           "--sidechain-attack-ms", str(sidechain_attack_ms),
           "--sidechain-release-ms", str(sidechain_release_ms),
           "--sidechain-max-duck-db", str(sidechain_max_duck_db)]
    drums = Path(stem_dir) / "drums.wav"
    if drums.exists():
        cmd += ["--sidechain-drums", str(drums)]
    if auto_clarity:
        cmd.append("--auto-clarity")
    scale = _run_reported(cmd, in_mix, out_wav, report_json, "bass", cancel)
    if progress:
        progress(1.0, "贝斯增强完成")
    return scale


def stage_drums(stem_dir, rest_wav, out_wav, punch_db=2.0, trans=0.3,
                drums_gain_db=0.0, progress=None, cancel=None, report_json=None):
    """鼓增强：punch/瞬态处理施加到鼓所在的轨上（4-stem 的 drums 轨）。"""
    if progress:
        progress(0.0, "鼓增强")
    stem_dir = Path(stem_dir)
    scale = 1.0
    drums = stem_dir / "drums.wav"
    if not drums.exists():
        # 旧 two-stems 产物没有 drums 轨：跳过增强，透传贝斯阶段结果。
        # 同样补一份本次运行的"未施加"报告，避免读取上次运行遗留文件。
        shutil.copyfile(rest_wav, out_wav)
        if report_json is not None:
            write_report(report_json, stage="drums", scale=1.0,
                         input_path=rest_wav, output_path=out_wav)
    else:
        cmd = [PYTHON, str(DSP_DIR / "drum_enhance.py"),
               "--drums", str(drums), "--in-mix", str(rest_wav), "--out", str(out_wav),
               "--punch-db", str(punch_db), "--trans", str(trans),
               "--drums-gain-db", str(drums_gain_db)]
        scale = _run_reported(cmd, rest_wav, out_wav, report_json, "drums", cancel)
    if progress:
        progress(1.0, "鼓增强完成")
    return scale


def stage_vocals(stem_dir, in_mix, out_wav, gain_db=0.0, reference_mix=None,
                 reference_vocals=None, vocal_scale=1.0, progress=None, cancel=None,
                 balance_target_db=None, balance_mode=None, comp_amount=0.0,
                 air_db=None):
    """人声平衡；显式固定目标下 0 dB 仍执行自动平衡。

    comp_amount: 有界宽带人声压缩（0=关，命令行与旧行为一致）。
    air_db: 人声空气高架（只加不削）；None=关。UI 映射按母带 8kHz 高架衰减
    （SOREN_HIGH_SHELF_MID_DB）镜像补偿，保证人声空气感穿过母带不丢失。"""
    if progress:
        progress(0.0, "人声调整")
    stem_dir = Path(stem_dir)
    vocals = stem_dir / "vocals.wav"
    if balance_mode is None and balance_target_db is None and (gain_db == 0 or not vocals.exists()):
        shutil.copyfile(in_mix, out_wav)
    elif not vocals.exists():
        shutil.copyfile(in_mix, out_wav)
    else:
        cmd = [PYTHON, str(DSP_DIR / "vocal_adjust.py"),
               "--vocals", str(vocals), "--in-mix", str(in_mix), "--out", str(out_wav),
               "--vocal-gain-db", str(gain_db)]
        cmd += ["--vocal-scale", str(vocal_scale)]
        if comp_amount:
            cmd += ["--vocal-comp-amount", str(comp_amount)]
        if air_db is not None:
            cmd += ["--vocal-air-db", str(air_db)]
        if balance_mode is not None:
            cmd += ["--balance-mode", balance_mode]
        if balance_target_db is not None:
            cmd += ["--balance-target-db", str(balance_target_db)]
        if reference_mix is not None:
            cmd += ["--reference-mix", str(reference_mix),
                    "--reference-vocals", str(reference_vocals or vocals)]
        _run_stream(cmd, DSP_DIR, cancel=cancel)
    if progress:
        progress(1.0, "人声调整完成")


def stage_reshape(in_mix, stems_dir, out_wav, wet=1.0, denoise=0.0, width_db=6.0,
                  progress=None, cancel=None, report_json=None,
                  noise_mode="other", noise_low_hz=8000.0, noise_high_hz=20000.0,
                  noise_max_attenuation_db=6.0):
    """声场重塑（broadband delta-add）：wet 缩放全部处理差值，可附带 ≥10kHz 噪声地板降噪。

    width_db 为宽度上限（other 轨 side 增益 dB，drums 自动取一半），wet 决定向该
    宽度目标混合的比例。wet≤0 且 denoise≤0，或缺少 drums/other stems 时直接透传
    （位级不变）。denoise>0 时即使 wet=0 也会运行，以允许单独使用高频降噪。

    noise_mode="other"（默认）为兼容降噪路径：命令行与旧行为完全一致，四个
    noise_* 参数不进入子进程命令。noise_mode="adaptive_all" 为 Stage3 可选
    自适应降噪：把 --noise-mode/--noise-low-hz/--noise-high-hz/
    --noise-max-attenuation-db 原样传给 soundstage_reshape.py（other-denoise-amount
    仍是共同降噪强度）。范围与 apollo_scripts/noise_profile.py 一致。
    """
    validate_noise_options(noise_mode, noise_low_hz, noise_high_hz,
                           noise_max_attenuation_db)
    if progress:
        progress(0.0, "声场重塑")
    scale = 1.0
    stems_dir = Path(stems_dir)
    if (wet <= 0 and denoise <= 0) or not (stems_dir / "drums.wav").exists() or not (stems_dir / "other.wav").exists():
        shutil.copyfile(in_mix, out_wav)
        if report_json is not None:
            # 透传也产出本次运行的"未施加"报告：报告永远与成品同批生成，
            # 绝不给过期 extras 留位（writes_stage_report 缓存契约要求产物在）。
            write_report(report_json, stage="reshape", scale=1.0,
                         input_path=in_mix, output_path=out_wav,
                         extra={"noise": {"mode": str(noise_mode),
                                          "amount": float(denoise),
                                          "stems": {}, "applied": False,
                                          "reason": "passthrough"},
                                "width": {"bands": [],
                                          "analysis": "passthrough (no reshape applied)"}})
    else:
        cmd = [PYTHON, str(DSP_DIR / "soundstage_reshape.py"),
               "--in-mix", str(in_mix), "--out-wav", str(out_wav),
               "--stems-dir", str(stems_dir),
               "--mode", "broadband", "--wet", str(wet),
               "--side-gain-db", str(width_db)]
        if denoise > 0:
            cmd += ["--other-denoise-amount", str(denoise)]
        if noise_mode == "adaptive_all":
            cmd += ["--noise-mode", str(noise_mode),
                    "--noise-low-hz", str(noise_low_hz),
                    "--noise-high-hz", str(noise_high_hz),
                    "--noise-max-attenuation-db", str(noise_max_attenuation_db)]
        scale = _run_reported(cmd, in_mix, out_wav, report_json, "reshape", cancel)
    if progress:
        progress(1.0, "声场重塑完成")
    return scale


# Stage3 自适应降噪（opt-in）可选参数范围：与 apollo_scripts/noise_profile.py 的
# validate_noise_options 保持一致（Nyquist 上限由脚本按实际采样率校验）。
NOISE_MODES = ("other", "adaptive_all")
NOISE_LOW_HZ_RANGE = (8000.0, 20000.0)
NOISE_HIGH_HZ_RANGE = (8000.0, 22000.0)
NOISE_MAX_ATTENUATION_DB_RANGE = (0.0, 6.0)


def validate_noise_options(noise_mode, noise_low_hz, noise_high_hz,
                           noise_max_attenuation_db):
    """校验自适应降噪可选参数；非法模式一律拒绝，数值范围仅在 adaptive_all 生效。

    other（默认兼容路径）不消费这些数值，保持旧行为零变化；adaptive_all 在
    进入任何阶段前拒绝越界值与 low>=high 的无效频带（提前失败，不跑推理）。
    抛 PipelineError：GUI/批处理按用户可读错误处理。
    """
    if noise_mode not in NOISE_MODES:
        raise PipelineError(
            f"未知降噪模式: {noise_mode!r}（可选: {'/'.join(NOISE_MODES)}）")
    if noise_mode != "adaptive_all":
        return
    for name, value, (lo, hi) in (
            ("noise_low_hz", noise_low_hz, NOISE_LOW_HZ_RANGE),
            ("noise_high_hz", noise_high_hz, NOISE_HIGH_HZ_RANGE),
            ("noise_max_attenuation_db", noise_max_attenuation_db,
             NOISE_MAX_ATTENUATION_DB_RANGE)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise PipelineError(f"{name} 必须是数值，收到: {value!r}") from None
        if not math.isfinite(number) or not lo <= number <= hi:
            raise PipelineError(f"{name} 必须在 {lo:g}..{hi:g} 范围内，收到: {value!r}")
    if not float(noise_low_hz) < float(noise_high_hz):
        raise PipelineError(
            f"adaptive_all 要求 noise_low_hz < noise_high_hz（收到 "
            f"{noise_low_hz} >= {noise_high_hz}）")


# ── 六轨可选分轨（guitar / synth 合并组）有界增强（opt-in，默认中性）────
# htdemucs_6s（第 4 批次）额外输出 guitar/piano。产品拓扑把 other + piano 求和成
# 一条"合成器/键盘层"（synth：合成器、铺底、效果器、钢琴等和声性内容），
# guitar 保持独立——很多歌曲没有钢琴，独立 piano 轨常为空。这是同模型分轨的
# 精确数学合并，不是合成器专用分离器。全部控制默认 0 dB → 位级透传；缺任一源
# 分轨时由 DSP 脚本透传并报告 unavailable，绝不把别的轨冒充本组。六轨残差比
# 四轨大（见 docs/analysis/six_stem_eval_20260913.md），听感验证未完成前不宣称音质。
DEMUCS_MODELS = ("htdemucs", "htdemucs_6s")
STEM_KINDS = ("guitar", "synth")
STEM_SOURCES = {"guitar": ("guitar",), "synth": ("other", "piano")}
STEM_GAIN_DB_RANGE = (-6.0, 6.0)   # 全轨增益（正负皆可）
STEM_EQ_DB_RANGE = (0.0, 6.0)      # mud 削减 / presence / harsh 收敛 / width（只增/只减方向固定）


def validate_demucs_model(demucs_model):
    """分离模型在跑任何推理前校验；模型名同时决定 stem 产物拓扑与缓存身份。"""
    if demucs_model not in DEMUCS_MODELS:
        raise PipelineError(
            f"未知分离模型: {demucs_model!r}（可选: {'/'.join(DEMUCS_MODELS)}）")


def validate_stem_controls(kind, controls):
    """可选分轨控制进阶段前校验（镜像 validate_noise_options 的提前失败策略）。"""
    if kind not in STEM_KINDS:
        raise PipelineError(f"未知可选分轨类型: {kind!r}")
    for name, value in controls.items():
        lo, hi = STEM_GAIN_DB_RANGE if name == "gain_db" else STEM_EQ_DB_RANGE
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise PipelineError(
                f"{kind}.{name} 必须是数值，收到: {value!r}") from None
        if not math.isfinite(number) or not lo <= number <= hi:
            raise PipelineError(
                f"{kind}.{name} 必须在 {lo:g}..{hi:g} 范围内，收到: {value!r}")


def six_stem_weights_available():
    """检查 htdemucs_6s 权重是否已在本地 HF 缓存（离线可用）；不加载模型。

    打包环境优先检查预置的 ASSETS/hf_home；开发环境检查 HF_HOME/默认缓存。
    快照目录存在且含权重文件（safetensors/th）即视为可用。"""
    if ASSETS is not None:
        hf_home = Path(ASSETS) / "hf_home"
    else:
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    model_dir = hf_home / "hub" / "models--adefossez--HTDemucs-6s"
    if not model_dir.is_dir():
        return False
    return any(model_dir.rglob("*.safetensors")) or any(model_dir.rglob("*.th"))


def _make_stage_stem(kind):
    """生成 guitar/synth 各自的独立阶段函数（缓存名/报告 stage/替换点都按名区分）。

    synth 组的源分轨是 other+piano（STEM_SOURCES），DSP 端先求和再处理。
    """

    def stage_stem(stem_dir, in_mix, out_wav, gain_db=0.0, mud_cut_db=0.0,
                   presence_db=0.0, harsh_cut_db=0.0, width_db=0.0,
                   progress=None, cancel=None, report_json=None):
        if progress:
            progress(0.0, f"{kind} 增强")
        sources = STEM_SOURCES[kind]
        cmd = [PYTHON, str(DSP_DIR / "stem_enhance.py"),
               "--stem", str(Path(stem_dir) / f"{sources[0]}.wav")]
        for extra_src in sources[1:]:
            cmd += ["--stem2", str(Path(stem_dir) / f"{extra_src}.wav")]
        cmd += ["--in-mix", str(in_mix), "--out", str(out_wav),
                "--kind", kind,
                "--gain-db", str(gain_db), "--mud-cut-db", str(mud_cut_db),
                "--presence-db", str(presence_db), "--harsh-cut-db", str(harsh_cut_db),
                "--width-db", str(width_db)]
        scale = _run_reported(cmd, in_mix, out_wav, report_json, kind, cancel)
        if progress:
            progress(1.0, f"{kind} 增强完成")
        return scale

    stage_stem.__name__ = f"stage_{kind}"
    stage_stem.writes_stage_report = True
    stage_stem.report_input_arg = "in_mix"
    return stage_stem


stage_guitar = _make_stage_stem("guitar")
stage_synth = _make_stage_stem("synth")


# 母带链对人声 Mid 的 8kHz 高架固定衰减（镜像自 soren_original.py 的
# high_shelf_gain_db_mid；改动那边时必须同步这里）。UI 映射用人声空气高架
# 镜像补偿该衰减，使人声空气感穿过母带不丢失（只补不削，封顶 2dB）。
SOREN_HIGH_SHELF_MID_DB = -1.5


# 缓存包装器据此把阶段 metadata 报告（含降噪/宽度诊断 extras）与成品一起入缓存：
# 缓存命中时报告文件一并恢复，否则命中只回成品、报告缺失，最终质量报告在
# 缓存命中的运行里就拿不到当次（同缓存身份）的 bass/reshape 诊断数据。
# report_input_arg 指明报告 "input" 段对应的混音参数（恢复后重绑用）。
stage_bass.writes_stage_report = True
stage_bass.report_input_arg = "in_mix"
stage_drums.writes_stage_report = True
stage_drums.report_input_arg = "rest_wav"
stage_reshape.writes_stage_report = True
stage_reshape.report_input_arg = "in_mix"


# ── 母带统计旁车（<out>.mastering.json）────────────────────────────────
# 引擎（core_decrypted）成功写出成品后会把 last_mastering_stats 原样写到
# <out>.mastering.json（目标/实测 LUFS、真峰值、target_status 等）。旁车与成品
# 一起进入阶段缓存：缓存命中也能恢复统计，GUI 既有进度日志可回放
# 目标/实测/达标状态；最终复制为 <最终wav>.mastering.json。
# 旁车缺失/损坏/未产出（母带旁路、测试替身）时一律视为无统计，绝不编造。
MASTERING_REPORT_SUFFIX = ".mastering.json"


def _mastering_report_path(wav_path):
    return Path(str(wav_path) + MASTERING_REPORT_SUFFIX)


def read_mastering_stats(wav_path):
    """读取引擎写在 <out>.mastering.json 的母带统计；缺失/损坏返回 None。

    json.loads 保留 NaN/Infinity 字面量，不丢弃引擎产出的任何数值。"""
    try:
        stats = json.loads(_mastering_report_path(wav_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return stats if isinstance(stats, dict) and stats else None


def _format_metric(value):
    """进度日志里的数值格式：有限值保留 1 位小数，NaN/Inf 原样标注不掩饰。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+inf" if number > 0 else "-inf"
    return f"{number:.1f}"


_TARGET_STATUS_LABELS = {"met": "达标", "below_target": "低于目标", "above_target": "高于目标"}


def _mastering_log_label(stats):
    """把母带统计压成一行进度日志文本（目标/实测/达标状态，缺啥省啥）。"""
    if not isinstance(stats, dict):
        return None
    parts = []
    if stats.get("target_lufs") is not None:
        parts.append(f"目标 {_format_metric(stats['target_lufs'])} LUFS")
    if stats.get("actual_lufs") is not None:
        parts.append(f"实测 {_format_metric(stats['actual_lufs'])} LUFS")
    status = stats.get("target_status")
    if isinstance(status, str):
        parts.append(_TARGET_STATUS_LABELS.get(status, status))
    elif isinstance(stats.get("target_met"), bool):
        parts.append("达标" if stats["target_met"] else "未达标")
    if not parts:
        return None
    return "母带统计（" + " / ".join(parts) + "）"


def _emit_mastering_log(report_cb, mastered_path):
    """有统计时把目标/实测/状态写进既有进度回调（缓存命中恢复后同样展示）。"""
    label = _mastering_log_label(read_mastering_stats(mastered_path))
    if label:
        report_cb(1.0, label)


def stage_soren(input_wav, out_wav, genre="Pop", loudness="normal",
                eq_profile="Neutral", reference=None, lowpass_cutoff=None,
                progress=None, cancel=None, style_mode="styled",
                upstream_delta_path=None, upstream_delta_hash=None,
                style_blend=0.85):
    if style_mode not in ("styled", "off", "eq_only"):
        raise ValueError(f"Unknown Soren style mode: {style_mode}")
    if progress:
        progress(0.0, f"Soren 母带（{genre or '自定义参考'} / {loudness} / {eq_profile}）")
    soren_dir = _ensure_dev_runtime()   # 执行前确保 canonical dev runtime（无资源时清晰报错）
    env = os.environ.copy()
    env["PYTHONPATH"] = str(soren_dir)
    cmd = [PYTHON, str(soren_dir / "core_decrypted.py"),
           str(input_wav), str(out_wav),
           "--loudness", loudness, "--eq-profile", eq_profile]
    if style_mode != "styled":
        cmd += ["--style-mode", style_mode]
    if reference:
        cmd += ["--reference", str(reference)]
    else:
        cmd += ["--genre", genre]
    if lowpass_cutoff:
        cmd += ["--lowpass-cutoff", str(lowpass_cutoff)]
    if upstream_delta_path:
        cmd += ["--upstream-delta", str(upstream_delta_path)]
    if style_mode == "styled":
        cmd += ["--style-blend", str(min(max(float(style_blend), 0.0), 1.0))]
    _run_stream(cmd, soren_dir, env=env, cancel=cancel,
                on_progress=(lambda f: progress(f * 0.999, "Soren 母带")) if progress else None)
    if progress:
        progress(1.0, "Soren 母带完成")
    # 引擎在写出成品后把统计写在 <out>.mastering.json：原样透传（返回 dict 供
    # 直接调用方使用；旁车文件本身交由 run_pipeline / 阶段缓存接管）。引擎没有
    # 产出统计（如测试替身）时返回 None，绝不编造。
    return read_mastering_stats(out_wav)


# 缓存包装器据此把 <out>.mastering.json 与成品一起入缓存（测试替身无此标记，
# 保持单产物旧合同；Mock 的自动属性也不会 `is True`）。
stage_soren.writes_mastering_report = True


# ── 上游意图感知（v20260910）：styled 频谱匹配的目标改为"流派曲线 × 上游
# 等效频谱 delta"。delta 在管线内测量（母带输入 vs 原始输入），M/S 分列：
# 形状曲线（1/3 倍频程平滑、限幅 ±9dB、扣除宽带 rms 比例防二次计入）+
# M/S 宽带 rms 比例（用于缩放参考的电平匹配，保持上游建立的中/侧平衡）。
# 纯 numpy 实现（外壳环境无 scipy）。
_DELTA_MIN_HZ = 20.0
_DELTA_MAX_HZ = 20500.0
_DELTA_CLAMP_DB = 9.0
_DELTA_FLOOR_RATIO = 1e-10   # 原信号该频点能量低于峰值 1e-10 时 delta 记 0


def _welch_psd(x, sr, nfft=8192):
    """Hann 窗 50% 重叠平均周期图（只取相对形状，不做密度归一）。"""
    import numpy as np
    x = np.asarray(x, dtype=np.float64)
    if len(x) < nfft:
        x = np.pad(x, (0, nfft - len(x)))
    n_seg = max(1, (len(x) - nfft) // (nfft // 2) + 1)
    idx = np.arange(nfft)[None, :] + (nfft // 2) * np.arange(n_seg)[:, None]
    win = np.hanning(nfft)
    seg = x[idx] * win
    psd = (np.abs(np.fft.rfft(seg, axis=1)) ** 2).mean(axis=0)
    return np.fft.rfftfreq(nfft, 1.0 / sr), psd


def _octave_smooth_db(freqs, db_vals, valid_mask, points_per_octave=12.0,
                      window_octaves=1.0 / 3.0):
    """重采样到 1/12 倍频程对数网格并用 ±1/6 倍频程窗平均；限幅。"""
    import numpy as np
    n = int(np.floor(points_per_octave * np.log2(_DELTA_MAX_HZ / _DELTA_MIN_HZ))) + 1
    grid = _DELTA_MIN_HZ * 2.0 ** (np.arange(n) / points_per_octave)
    out = np.zeros(len(grid))
    half = window_octaves / 2.0
    for i, f in enumerate(grid):
        lo, hi = f * 2.0 ** (-half), f * 2.0 ** half
        mask = (freqs >= lo) & (freqs <= hi) & valid_mask
        out[i] = db_vals[mask].mean() if mask.any() else 0.0
    return grid, np.clip(out, -_DELTA_CLAMP_DB, _DELTA_CLAMP_DB)


def _upstream_delta_curves(original_path, processed_path):
    """计算 styled 母带的"上游等效频谱意图"payload（无有效 delta 时返回 None）。

    processed（母带输入）相对 original（管线输入）的 M/S 频谱 delta：
    1/3 倍频程平滑、限幅 ±9dB、扣除宽带 rms 比例（电平域由 rms 缩放承担，
    防止形状曲线与电平缩放二次计入）；同时给出 M/S 宽带 rms 比例。"""
    import numpy as np
    import soundfile as sf
    orig, sr_o = sf.read(original_path, dtype="float64", always_2d=True)
    proc, sr_p = sf.read(processed_path, dtype="float64", always_2d=True)
    curves, ratios = {}, {}
    for name, mix in (("mid", lambda a: a.mean(axis=1)),
                      ("side", lambda a: (a[:, 0] - a[:, 1]) * 0.5)):
        o, p = mix(orig), mix(proc)
        rms_o = float(np.sqrt(np.mean(o * o)))
        rms_p = float(np.sqrt(np.mean(p * p)))
        ratios[name] = rms_p / rms_o if rms_o > 1e-9 and rms_p > 1e-12 else 1.0
        if rms_o <= 1e-9 or rms_p <= 1e-12:
            curves[name] = None
            continue
        f_o, psd_o = _welch_psd(o, sr_o)
        f_p, psd_p = _welch_psd(p, sr_p)
        if len(f_o) != len(f_p) or not np.allclose(f_o, f_p):
            psd_o = np.interp(f_p, f_o, psd_o)
        valid = psd_o >= psd_o.max() * _DELTA_FLOOR_RATIO
        delta_db = 10.0 * np.log10(np.maximum(psd_p, 1e-30)) \
            - 10.0 * np.log10(np.maximum(psd_o, 1e-30))
        delta_db = np.where(valid, delta_db, 0.0)
        delta_db -= 20.0 * np.log10(max(ratios[name], 1e-9))
        curves[name] = _octave_smooth_db(f_p, delta_db, valid)
    if all(v is None for v in curves.values()):
        return None
    grid = curves["mid"][0] if curves["mid"] is not None else curves["side"][0]

    def curve(name):
        c = curves[name]
        return [0.0] * len(grid) if c is None else [round(float(v), 3) for v in c[1]]

    return {"version": 1,
            "freqs": [round(float(v), 2) for v in grid],
            "mid_db": curve("mid"), "side_db": curve("side"),
            "rms_mid": round(min(max(ratios["mid"], 0.25), 4.0), 4),
            "rms_side": round(min(max(ratios["side"], 0.25), 4.0), 4)}


def _write_upstream_delta(input_wav, soren_input, work_dir):
    """计算并把上游意图 JSON 写入工作目录；返回 (路径, 内容哈希) 或 (None, None)。

    delta 是可选增强：计算或写盘失败（如解码/环境抖动）时告警并降级为
    无 delta（母带退回旧匹配行为），不中止整条管线。"""
    try:
        payload = _upstream_delta_curves(input_wav, soren_input)
        if payload is None:
            return None, None
        import hashlib
        import json
        text = json.dumps(payload, sort_keys=True)
        path = Path(work_dir) / "upstream_delta.json"
        path.write_text(text, encoding="utf-8")
    except Exception as exc:
        print(f"上游意图 delta 计算失败，本曲退回无感知匹配: {exc!r}")
        return None, None
    return path, hashlib.md5(text.encode("utf-8")).hexdigest()


# 单次运行的临时工作目录名（v1.6.5 及之前为 .sorenstudio_work，遗留目录会被收编）。
WORK_DIR_NAME = ".shadowbuster_work"
LEGACY_WORK_DIR_NAME = ".sorenstudio_work"


def _resolve_work_dir(output_dir: Path, work_dir) -> Path:
    """显式 work_dir 优先；否则用新目录名并原子收编旧名遗留目录。"""
    if work_dir:
        return Path(work_dir)
    work = output_dir / WORK_DIR_NAME
    legacy = output_dir / LEGACY_WORK_DIR_NAME
    if legacy.exists() and not work.exists():
        try:
            legacy.rename(work)
        except OSError:
            return legacy  # 被占用（另一实例在跑/文件锁）时沿用旧目录
    return work


def _stage_report_extra(report_path, stage):
    """读取本次运行阶段报告里的 extra 诊断载荷；缺失/损坏/无 extra 返回 None。

    调用时机保证 report_path 属于本次运行：非旁路报告阶段在调用前已删掉可能
    的遗留文件，其内容要么刚经 read_report 全量校验重算（_run_reported），
    要么从同一缓存条目与成品一起按哈希恢复（writes_stage_report 产物）；阶段
    透传（不产报告）时文件保持缺失。旁路阶段一律不读取。这里只做结构校验。
    """
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION \
            or data.get("stage") != stage:
        return None
    extra = data.get("extra")
    return extra if isinstance(extra, dict) else None


def _rebind_stage_report(report_path, input_path, output_path):
    """把阶段报告的 input/output 元数据重绑到本次运行的文件（缓存恢复后调用）。

    StageCache 按内容哈希恢复产物：恢复出的报告记录的是入缓存时的路径与
    mtime_ns。跨输出目录命中时路径不同；即使同目录命中，成品 mtime 也会变。
    重绑后报告与本次恢复的成品一一对应，read_report 的新鲜度校验重新成立。
    重算场景下元数据本就指向当前文件 → 内容相同 → 不重写（幂等）。重绑失败
    只损失诊断一致性，绝不阻断成品与缓存。
    """
    path = Path(report_path)
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        fresh = {"input": audio_metadata(input_path),
                 "output": audio_metadata(output_path)}
        if all(data.get(key) == value for key, value in fresh.items()):
            return
        data.update(fresh)
        text = json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (OSError, ValueError, TypeError):
        return


def run_pipeline(input_wav, output_dir, *, sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
                 bass_gain_db=0.0, bass_auto_clarity=False, sidechain_amount=0.0,
                 sidechain_attack_ms=5.0, sidechain_release_ms=150.0,
                 sidechain_max_duck_db=6.0, vocal_gain_db=0.0,
                 genre="Pop", loudness="normal",
                 eq_profile="Neutral", reference=None, quality=1, guidance=1.5,
                 device="cuda", progress=None, cancel=None, work_dir=None,
                 lowpass_cutoff=None, space_wet=0.0, space_denoise=0.0,
                 space_width_db=6.0, balance_target_db=None, bypass=(),
                 balance_mode=REFERENCE_MODE, style_mode="styled", style_blend=0.85,
                 cache_enabled=True, noise_mode="other", noise_low_hz=8000.0,
                 noise_high_hz=20000.0, noise_max_attenuation_db=6.0,
                 demucs_model="htdemucs", guitar_gain_db=0.0, guitar_mud_cut_db=0.0,
                 guitar_presence_db=0.0, guitar_harsh_cut_db=0.0, guitar_width_db=0.0,
                 synth_gain_db=0.0, synth_mud_cut_db=0.0, synth_presence_db=0.0,
                 synth_harsh_cut_db=0.0, synth_width_db=0.0,
                 vocal_comp_amount=0.0, vocal_air_db=None):
    """执行单文件完整链路。progress(stage_idx, frac, label)。

    bypass: 可迭代的阶段名（lew/vocals/bass/drums/reshape/soren），命中的阶段位级跳过。
    分离阶段在 bass/drums/reshape/vocals 全部旁路时自动跳过（分轨产物无人消费），
    任一分轨阶段启用时照常执行。旁路 Lew 时输入探测到非 44.1k 采样率会先统一
    采样率再进入后续阶段（Soren 只接受 44.1k；已是 44.1k 或探测不到采样率时
    原样透传；全链路旁路时无阶段消费产物，逐字节透传）。
    bass_auto_clarity: opt-in 贝斯清晰度 EQ（默认 False；关闭时贝斯阶段行为位级不变）。
    noise_mode="other"（默认）为兼容降噪；"adaptive_all" 为 Stage3 可选自适应
    高频降噪（opt-in），参数在进任何阶段前校验，且仅在 adaptive_all 时转发给
    声场重塑阶段——默认路径的子进程命令不变（DSP 代码变更仍按既有语义使
    缓存失效）。
    demucs_model="htdemucs"（默认四轨，行为与旧版一致）；"htdemucs_6s" 为第 4 批次
    opt-in 六轨：额外产出 guitar/piano，并把 other+piano 求和成"合成器/键盘层"
    （synth 组），路由两个默认中性的有界 delta-add 阶段（guitar → synth）。
    模型名进入 stem 产物目录与缓存身份；4 轨/6 轨产物不混用。缺源分轨由 DSP
    脚本透明降级并报告 unavailable。控制全零时阶段位级透传。
    母带统计旁车：引擎把 last_mastering_stats 写到母带输出旁的 <out>.mastering.json，
    与成品一起入阶段缓存（命中同样恢复）；处理成功后复制为输出目录的
    <最终wav>.mastering.json，并把目标/实测/达标状态写进既有进度日志。母带旁路或
    引擎未产出统计时不生成旁车，且移除同名的过期旁车（绝不编造统计）。
    """
    if style_mode not in ("styled", "off", "eq_only"):
        raise ValueError(f"Unknown Soren style mode: {style_mode}")
    # 新参数先于输入文件存在性校验：显式传参错误在跑任何阶段/推理前失败。
    validate_noise_options(noise_mode, noise_low_hz, noise_high_hz,
                           noise_max_attenuation_db)
    validate_demucs_model(demucs_model)
    stem_controls = {
        "guitar": dict(gain_db=guitar_gain_db, mud_cut_db=guitar_mud_cut_db,
                       presence_db=guitar_presence_db,
                       harsh_cut_db=guitar_harsh_cut_db, width_db=guitar_width_db),
        "synth": dict(gain_db=synth_gain_db, mud_cut_db=synth_mud_cut_db,
                      presence_db=synth_presence_db,
                      harsh_cut_db=synth_harsh_cut_db, width_db=synth_width_db),
    }
    for kind, controls in stem_controls.items():
        validate_stem_controls(kind, controls)
    input_wav = Path(input_wav)
    if not input_wav.exists():
        raise PipelineError(f"输入文件不存在: {input_wav}")
    if not output_dir:
        raise PipelineError("未指定输出目录，请在界面选择输出目录后再处理")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    work = _resolve_work_dir(output_dir, work_dir)
    work.mkdir(parents=True, exist_ok=True)

    bypass = set(bypass)
    if not bypass <= {"lew", "vocals", "bass", "drums", "reshape", "soren"}:
        raise PipelineError(f"未知 bypass 阶段: {sorted(bypass)}")

    # ── macOS TCC 加固 ──
    # 输入文件位于 Desktop/Downloads 等受保护目录时，拖放/文件对话框授予
    # 父进程的访问权不传递给推理子进程（ffmpeg 等），子进程读取会以 EPERM
    # 失败（现象：命令失败（1），Windows 上无此机制）。对策：父进程先把
    # 输入复制到系统临时目录、工作目录也放临时目录（子进程只碰临时目录），
    # 最终产物再由父进程写回输出目录（父进程持有对话框授予的访问权）。
    neutralize_tcc = sys.platform == "darwin" and work_dir is None
    if neutralize_tcc:
        try:
            tcc_tmp = Path(tempfile.mkdtemp(prefix="sb_tcc_"))
            staged = tcc_tmp / input_wav.name
            shutil.copy2(input_wav, staged)
            input_wav = staged
            work_dir = tcc_tmp
        except OSError as exc:
            raise PipelineError(
                f"无法读取输入文件 {input_wav}：{exc}。请将文件移出"
                "「桌面 / 下载」等受保护目录后重试，或在系统设置 → 隐私与"
                "安全性 → 文件与文件夹中允许 ShadowBuster 访问。") from exc

    stem = input_wav.stem
    out_final = output_dir / f"{stem}_shadowbuster.wav"
    lew_out = work / f"{stem}_lew.wav"
    stems_out = work / "stems"
    bass_out = work / f"{stem}_bassmix.wav"
    drum_out = work / f"{stem}_drummix.wav"
    vocal_out = work / f"{stem}_vocalmix.wav"
    shape_out = work / f"{stem}_shapemix.wav"
    guitar_out = work / f"{stem}_guitarmix.wav"
    synth_out = work / f"{stem}_synthmix.wav"
    bass_report = work / "bass.json"
    drums_report = work / "drums.json"
    reshape_report = work / "reshape.json"
    guitar_report = work / "guitar.json"
    synth_report = work / "synth.json"

    def cb(i, offset=0.0, span=1.0):
        def inner(frac, label):
            if cancel and cancel():
                raise PipelineError("用户取消")
            if progress:
                progress(i, offset + span * frac, label)
        return inner

    import inspect
    import pipeline_cache
    implementation = [Path(__file__), Path(pipeline_cache.__file__)]
    # 指纹前确保 canonical dev runtime（soren 参与处理时）。失败在执行任何阶段前
    # 中止：不执行、不缓存任何东西（绝不吞错，注释与行为一致）。
    soren_root = Path(SOREN_DIR) if "soren" in bypass else _ensure_dev_runtime()
    implementation += list(Path(DSP_DIR).glob("*.py")) + list(soren_root.glob("*.py"))
    # Lew 脚本在 APOLLO_DIR（模型/工具根）而非 DSP_DIR，必须单独入指纹：
    # 否则 lew_upscale.py 的算法变更（如计算精度）不会使旧阶段缓存失效。
    _lew_script = Path(APOLLO_DIR) / "lew_upscale.py"
    if _lew_script.is_file():
        implementation.append(_lew_script)
    # dev runtime 有效性入缓存身份：manifest 记录资源源与生成期哈希，资源切换或
    # 重建后旧缓存自动失效（dev runtime 下代码即 packaging canonical 字节）。
    _dev_manifest = soren_root / "dev_runtime_manifest.json"
    if _dev_manifest.is_file():
        implementation.append(_dev_manifest)
    runtime_files = [Path(PYTHON)]
    for folder in (Path(APOLLO_DIR), soren_root):
        for pattern in ("*.pt", "*.pth", "*.ckpt", "*.json", "*.yaml"):
            runtime_files.extend(folder.rglob(pattern))
    runtime_stamp = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in runtime_files if p.is_file()]
    identity = {"version": APP_VERSION, "runtime": str(PYTHON), "device": device,
                "audio_format": AUDIO_FORMAT_VERSION,
                "runtime_files": runtime_stamp, "input_md5": pipeline_cache.md5(input_wav),
                "code": [(str(p), pipeline_cache.md5(p)) for p in implementation if p.is_file()]}
    cache = pipeline_cache.StageCache(cache_enabled, identity)

    def cached(fn, output_arg, input_args, report_suffix=None):
        if not hasattr(fn, "__name__"):
            return fn
        signature = inspect.signature(fn)
        def invoke(*args, **kwargs):
            if cancel and cancel():
                raise PipelineError("用户取消")
            # Test doubles may not expose the production signature.
            bound = signature.bind(*args, **kwargs)
            values = dict(bound.arguments)
            values.update(values.pop("kwargs", {}))
            if output_arg not in values:
                return fn(*args, **kwargs)
            inputs = [values[k] for k in input_args if values.get(k) is not None]
            params = {k: v for k, v in values.items() if k not in input_args + [output_arg, "progress", "cancel", "report_json"]}
            if fn.__name__ == "stage_demucs":
                params["stem_name"] = Path(inputs[0]).stem
            # 母带统计旁车与成品一起入缓存：缓存命中后 run_pipeline 仍能读到
            # <out>.mastering.json 回放目标/实测指标。测试替身没有
            # writes_mastering_report 标记 → 单产物，旧缓存合同不变。统计 dict
            # 不能进缓存 result（数值校验只收有限数值），统计一律经旁车文件传递。
            outputs = [values[output_arg]]
            report_cacheable = (report_suffix is not None and
                                getattr(fn, "writes_mastering_report", None) is True)
            stage_report = None
            if report_cacheable:
                outputs.append(str(values[output_arg]) + report_suffix)
                compute = lambda: (fn(*args, **kwargs), None)[1]
            else:
                # 阶段 metadata 报告与成品一起入缓存：否则缓存命中只恢复成品、
                # 报告缺失（本次 compute 未执行），bass/reshape 的诊断 extras
                # 就无法进入本次质量报告。旧缓存条目只有单产物，命中校验失败
                # 会自然回退重算并补写报告。
                if getattr(fn, "writes_stage_report", None) is True and \
                        values.get("report_json") is not None:
                    outputs.append(values["report_json"])
                    stage_report = (values["report_json"],
                                    values.get(getattr(fn, "report_input_arg", None)))
                compute = lambda: fn(*args, **kwargs)
            result = cache.run(fn.__name__, inputs, params, outputs, compute)
            if stage_report is not None:
                # 恢复/重算出的报告重绑到本次文件：缓存恢复的报告元数据指向
                # 入缓存时的路径与 mtime，重绑后 read_report 的新鲜度校验对
                # 缓存命中同样成立（重算场景内容相同，幂等不重写）。
                _rebind_stage_report(stage_report[0], stage_report[1],
                                     values[output_arg])
            if kwargs.get("progress"):
                kwargs["progress"](1.0, "阶段完成（可复用缓存）")
            return result
        return invoke

    run_lew = cached(stage_lew, "out_wav", ["input_wav"])
    run_demucs = cached(stage_demucs, "out_dir", ["input_wav"])
    run_bass = cached(stage_bass, "out_wav", ["stem_dir", "in_mix"])
    run_drums = cached(stage_drums, "out_wav", ["stem_dir", "rest_wav"])
    run_reshape = cached(stage_reshape, "out_wav", ["in_mix", "stems_dir"])
    run_guitar = cached(stage_guitar, "out_wav", ["stem_dir", "in_mix"])
    run_synth = cached(stage_synth, "out_wav", ["stem_dir", "in_mix"])
    run_vocals = cached(stage_vocals, "out_wav", ["stem_dir", "in_mix", "reference_mix", "reference_vocals"])
    run_soren = cached(stage_soren, "out_wav",
                       ["input_wav", "reference", "upstream_delta_hash"],
                       report_suffix=MASTERING_REPORT_SUFFIX)
    run_convert = cached(ffmpeg_convert, "dst", ["src"])

    mastered = work / "mastered.wav"
    mastering_stats = None
    stage_extras = {}   # 本次运行 bass/drums/reshape/guitar/piano 诊断 extras（旁路阶段绝不收集）
    try:
        if "lew" in bypass:
            # Lew 正常运行时会把输入统一成 44.1k；旁路后这层保证消失，而 Demucs
            # 分轨保留输入采样率，48k 源会让全部中间产物保持 48k，Soren 母带只
            # 接受 44.1k。因此旁路 Lew 且探测到非 44.1k 采样率时先统一（已是
            # 44.1k 不重编码，探测不到按原样透传）；全链路旁路时无阶段消费
            # 产物，同样逐字节透传。
            rate = _detect_sample_rate(input_wav)
            if rate is None or rate == 44100 or \
                    bypass >= {"bass", "drums", "reshape", "vocals", "soren"}:
                lew_src = input_wav
            else:
                run_convert(input_wav, lew_out)
                lew_src = lew_out
            if progress:
                cb(0)(1.0, "高频旁路")
        else:
            run_lew(input_wav, lew_out, device=device, progress=cb(0),
                      quality=quality, guidance=guidance, cancel=cancel)
            lew_src = lew_out
        # 产物目录以模型名命名（demucs CLI 约定）：模型名同时进入缓存身份，
        # 4 轨/6 轨产物绝不混用。
        stem_dir = stems_out / demucs_model / lew_src.stem
        if bypass >= {"bass", "drums", "reshape", "vocals"}:
            # 四个分轨消费阶段全部旁路：分轨产物无人读取，跳过最重的分离计算。
            if progress:
                cb(1)(1.0, "分轨旁路")
        else:
            run_demucs(lew_src, stems_out, model=demucs_model, progress=cb(1), cancel=cancel,
                       device=device)
        if "bass" in bypass:
            shutil.copyfile(lew_src, bass_out)
            bass_scale = 1.0
            if progress:
                cb(2)(1.0, "贝斯旁路")
        else:
            # 先清掉上次失败运行可能遗留的报告：阶段重算会重写、缓存命中会按
            # 哈希恢复；透传分支不产报告时保持缺失，诊断绝不引用过期数据。
            bass_report.unlink(missing_ok=True)
            bass_scale = run_bass(stem_dir, lew_src, bass_out, sub_db=sub_db, sat=sat,
                       punch_db=0.0, trans=0.0, bass_gain_db=bass_gain_db,
                       auto_clarity=bass_auto_clarity,
                       sidechain_amount=sidechain_amount,
                       sidechain_attack_ms=sidechain_attack_ms,
                       sidechain_release_ms=sidechain_release_ms,
                       sidechain_max_duck_db=sidechain_max_duck_db,
                       progress=cb(2), cancel=cancel, report_json=bass_report)
            extra = _stage_report_extra(bass_report, "bass")
            if extra is not None:
                stage_extras["bass"] = extra
        if "drums" in bypass:
            shutil.copyfile(bass_out, drum_out)
            drums_scale = 1.0
            if progress:
                cb(3)(1.0, "鼓旁路")
        else:
            drums_report.unlink(missing_ok=True)
            drums_scale = run_drums(stem_dir, bass_out, drum_out, punch_db=punch_db, trans=trans,
                        progress=cb(3), cancel=cancel, report_json=drums_report)
            extra = _stage_report_extra(drums_report, "drums")
            if extra is not None:
                stage_extras["drums"] = extra
        if "reshape" in bypass:
            shutil.copyfile(drum_out, shape_out)
            reshape_scale = 1.0
            if progress:
                cb(4, 0.0, 0.5)(1.0, "声场旁路")
        else:
            # 仅 adaptive_all 转发新参数：默认路径的子进程命令保持旧样，
            # 既有测试替身 / 默认命令行为不受影响（算法代码变更仍会使既有
            # 缓存整体失效，这是缓存身份的既有语义）。
            reshape_kwargs = {}
            if noise_mode == "adaptive_all":
                reshape_kwargs = dict(noise_mode=noise_mode,
                                      noise_low_hz=noise_low_hz,
                                      noise_high_hz=noise_high_hz,
                                      noise_max_attenuation_db=noise_max_attenuation_db)
            reshape_report.unlink(missing_ok=True)
            reshape_scale = run_reshape(drum_out, stem_dir, shape_out, wet=space_wet,
                          denoise=space_denoise, width_db=space_width_db,
                          progress=cb(4, 0.0, 0.5), cancel=cancel, report_json=reshape_report,
                          **reshape_kwargs)
            extra = _stage_report_extra(reshape_report, "reshape")
            if extra is not None:
                stage_extras["reshape"] = extra
        # 六轨（opt-in）：reshape 之后、vocals 之前路由 guitar/piano 两个默认中性
        # 的有界 delta-add 阶段；缺 stem 由 DSP 脚本透传并报告 unavailable。
        # 分轨消费阶段全旁路时分离被跳过、stem 无人产出，这两个阶段一并跳过。
        extra_in = shape_out
        extra_scales = []
        if demucs_model == "htdemucs_6s" and \
                not bypass >= {"bass", "drums", "reshape", "vocals"}:
            for kind, out_wav, report_path, span in (
                    ("guitar", guitar_out, guitar_report, (0.4, 0.05)),
                    ("synth", synth_out, synth_report, (0.45, 0.05))):
                report_path.unlink(missing_ok=True)
                run_extra = run_guitar if kind == "guitar" else run_synth
                extra_scales.append(run_extra(
                    stem_dir, extra_in, out_wav,
                    progress=cb(4, *span), cancel=cancel,
                    report_json=report_path, **stem_controls[kind]))
                extra = _stage_report_extra(report_path, kind)
                if extra is not None:
                    stage_extras[kind] = extra
                extra_in = out_wav
        # Only original vocals base scaling; unscaled original-stem delta additions remain residual.
        vocal_scale = bass_scale * drums_scale * reshape_scale
        for extra_scale in extra_scales:
            vocal_scale *= extra_scale
        if "vocals" in bypass:
            shutil.copyfile(extra_in, vocal_out)
            cb(4, 0.5, 0.5)(1.0, "人声旁路")
        else:
            original_mix, original_vocals = lew_src, stem_dir / "vocals.wav"
            if balance_mode == REFERENCE_MODE and balance_target_db is None:
                original_mix = work / "original_reference.wav"
                original_stems = work / "original_reference_stems"
                # HTDemucs preserves sample origin; DSP rejects frame/rate mismatch.
                run_convert(input_wav, original_mix, sr=44100)
                run_demucs(original_mix, original_stems, model=demucs_model,
                           cancel=cancel, device=device)
                original_vocals = original_stems / demucs_model / original_mix.stem / "vocals.wav"
            run_vocals(stem_dir, extra_in, vocal_out, gain_db=vocal_gain_db,
                         balance_target_db=balance_target_db,
                         balance_mode=balance_mode if balance_target_db is None else None,
                         reference_mix=original_mix,
                         reference_vocals=original_vocals,
                         vocal_scale=vocal_scale,
                         comp_amount=vocal_comp_amount, air_db=vocal_air_db,
                         progress=cb(4, 0.5, 0.5), cancel=cancel)
        if "soren" in bypass:
            shutil.copyfile(vocal_out, mastered)
            if progress:
                cb(5)(1.0, "母带旁路")
        else:
            delta_path = delta_hash = None
            # 上游全部旁路且六轨附加阶段未产生实际差值时，母带输入即原始输入，
            # delta 恒为零，跳过计算；六轨附加控制非中性时母带输入已不同，
            # 仍需计算 delta（这些阶段没有旁路开关）。
            extras_active = demucs_model == "htdemucs_6s" and any(
                any(v != 0 for v in controls.values())
                for controls in stem_controls.values())
            if style_mode == "styled" and \
                    not (bypass >= {"lew", "bass", "drums", "reshape", "vocals"}
                         and not extras_active):
                delta_path, delta_hash = _write_upstream_delta(
                    input_wav, vocal_out, work)
            run_soren(vocal_out, mastered, genre=genre, loudness=loudness,
                        eq_profile=eq_profile, reference=reference,
                        lowpass_cutoff=lowpass_cutoff, progress=cb(5),
                        cancel=cancel, style_mode=style_mode,
                        upstream_delta_path=delta_path,
                        upstream_delta_hash=delta_hash,
                        style_blend=style_blend)
            # 母带统计（目标/实测 LUFS、达标状态）写进既有进度日志；缓存命中
            # 恢复的旁车同样展示（引擎本次没跑也能回放指标）。
            _emit_mastering_log(cb(5), mastered)
        if "soren" not in bypass:
            mastering_stats = read_mastering_stats(mastered)
        shutil.copyfile(mastered, out_final)
        # 最终统计旁车：只有真实统计才复制；本次无统计（母带旁路、兼容替身）
        # 时移除同名旧旁车，避免旧渲染的指标被误当作本次成品的数据。
        final_report = _mastering_report_path(out_final)
        if _mastering_report_path(mastered).is_file():
            shutil.copyfile(_mastering_report_path(mastered), final_report)
        else:
            final_report.unlink(missing_ok=True)
        quality_params = {
            "genre": genre, "loudness": loudness, "eq_profile": eq_profile,
            "style_mode": style_mode, "style_blend": style_blend,
            "bypass": sorted(bypass), "quality_level": quality, "guidance": guidance,
        }
        if noise_mode == "adaptive_all":
            # 仅 opt-in 运行记录噪声参数：默认路径的质量报告内容保持旧样。
            quality_params.update(
                noise_mode=noise_mode, noise_low_hz=noise_low_hz,
                noise_high_hz=noise_high_hz,
                noise_max_attenuation_db=noise_max_attenuation_db)
        if demucs_model == "htdemucs_6s":
            # 仅六轨（opt-in）记录模型与可选分轨控制：四轨默认报告内容保持旧样。
            quality_params.update(demucs_model=demucs_model, stems=stem_controls)
        quality_report = None
        try:
            import soundfile as _sf
            quality_params["output_subtype"] = _sf.info(str(out_final)).subtype
            quality_report = write_quality_report(
                input_wav, out_final, params=quality_params,
                mastering_stats=mastering_stats,
                stage_reports=stage_extras or None)
        except Exception as exc:
            # 质量报告是诊断附加物；不能让兼容输入或测试替身阻断已生成的音频。
            print(f"质量报告生成失败，本次保留音频输出: {exc!r}")
        if progress and quality_report:
            summary = quality_summary(quality_report) or {}
            progress(5, 1.0, f"质量检查（{summary.get('overall', 'unknown')}）")
    except PipelineError:
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return str(out_final)


def run_batch(input_files, output_dir, *, progress=None, file_finished=None, cancel=None, **kwargs):
    """批处理：逐个文件跑完整链路。
    progress(file_idx, file_total, stage_idx, frac, label, file_name)
    file_finished(file_idx, file_total, file_name, succeeded, error)
    """
    if os.environ.get("SB_TRACE"):
        print(f"[trace] run_batch start files={len(input_files)} kwargs={list(kwargs)}", flush=True)
    _tr(f"run_batch: start files={len(input_files)}")
    files = [Path(f) for f in input_files]
    results = []
    for idx, f in enumerate(files):
        _tr(f"run_batch: file {idx} {f.name}")
        if cancel and cancel():
            raise PipelineError("用户取消")

        def file_progress(stage_idx, frac, label):
            if cancel and cancel():
                raise PipelineError("用户取消")
            if progress:
                progress(idx, len(files), stage_idx, frac, label, f.name)

        if progress:
            progress(idx, len(files), 0, 0.0, "开始处理", f.name)

        try:
            out = run_pipeline(f, output_dir, progress=file_progress, cancel=cancel, **kwargs)
            results.append((str(f), str(out), None))
            if file_finished:
                file_finished(idx, len(files), f.name, True, "")
        except PipelineError as e:
            if cancel and cancel():
                raise
            error = str(e)
            results.append((str(f), None, error))
            if file_finished:
                file_finished(idx, len(files), f.name, False, error)
        except Exception as e:
            if os.environ.get("SB_TRACE"):
                import traceback
                traceback.print_exc()
            error = f"{type(e).__name__}: {e}"
            results.append((str(f), None, error))
            if file_finished:
                file_finished(idx, len(files), f.name, False, error)
    return results


if __name__ == "__main__":
    from processing_cli import main
    raise SystemExit(main())
