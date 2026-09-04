"""SorenStudio 后端管线：Lew 高频 → Demucs 4-stem 分离 → bass 增强 → drums 增强 → Soren 母带
所有阶段调用已验证的命令行工具（subprocess），支持进度回调与取消。
"""
import os
import sys
import time
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path

# 应用版本号（单一来源）：设置界面显示 / 打包与安装器读取。
# 与 packaging/installer.iss 的 MyAppVersion 保持一致（tests/test_app_version.py 有同步校验）。
APP_VERSION = "1.5.0"

if getattr(sys, "frozen", False):
    # PyInstaller 冻结后 __file__ 在 _internal 里，exe 同级才是安装根目录
    ROOT = Path(sys.executable).parent
else:
    ROOT = Path(__file__).parent

DEV_PYTHON = r"D:/_3.AI/audio_upscale/UniverSR/.venv/Scripts/python.exe"
DEV_APOLLO = Path(r"D:/_3.AI/audio_upscale/Apollo")
DEV_SOREN = Path(r"D:/_3.AI/audio_upscale/Soren_src")

# 无控制台的 GUI 进程里，子进程默认会新建可见控制台（安装版弹 python 黑框）；
# CREATE_NO_WINDOW 让推理 / ffmpeg 子进程全程无窗。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _user_gpu_py():
    """用户级 GPU 环境（设置内下载的 CUDA 运行时，v1.5）。

    装在 LOCALAPPDATA\\ShadowBuster\\runtime-gpu\\env（应用非提权运行也能写），
    仅替换解释器；Apollo/Soren/权重仍读安装目录。返回解释器路径或 None。
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        import gpu_env as ge
        info = ge.installed_info(expected_version=APP_VERSION)
    except (ImportError, OSError, ValueError):
        return None
    if info is None:
        return None
    return ge.env_dir() / "python.exe"


def _resolve_runtime():
    """解析工具链根目录。

    部署包：优先取 exe 同级 runtime/（安装器就地放下，无需环境变量）；
    其次 SB_ASSETS 环境变量；都未设置时回退到开发机布局。
    用户级 GPU 环境（LOCALAPPDATA\\ShadowBuster\\runtime-gpu）仅覆盖解释器。
    """
    upy = _user_gpu_py()
    if upy is not None:
        root = ROOT / "runtime"
        apollo, soren = root / "Apollo", root / "Soren_src"
        if apollo.is_dir() and soren.is_dir():
            return upy, apollo, soren, root
    for root in (ROOT / "runtime",
                 Path(os.environ.get("SB_ASSETS", ".")) if os.environ.get("SB_ASSETS") else None):
        if root is None:
            continue
        apollo, soren = root / "Apollo", root / "Soren_src"
        if apollo.is_dir() and soren.is_dir():
            # 便携 Python 布局 env\python.exe；兼容旧 venv 布局 env\Scripts\python.exe
            py = root / "env" / "python.exe"
            if not py.exists():
                py = root / "env" / "Scripts" / "python.exe"
            return (py if py.exists() else sys.executable), apollo, soren, root
    return (
        Path(os.environ.get("SB_PYTHON", DEV_PYTHON)),
        Path(os.environ.get("SB_APOLLO", DEV_APOLLO)),
        Path(os.environ.get("SB_SOREN", DEV_SOREN)),
        None,
    )


PYTHON, APOLLO_DIR, SOREN_DIR, ASSETS = _resolve_runtime()


def auto_device():
    """推理设备：通过子进程探测 CUDA，不在 UI 进程中 import torch。
    （QtWebEngine + 同进程 torch CUDA 初始化会卡死，内存缓涨无资源占用）
    开发版与部署版统一用 PYTHON（带 torch 的运行时）做子进程探测：
    开发版 GUI 解释器（如 C:/Python314）通常没有 torch，进程内 import 必失败，
    若在此分支探测会误判为 CPU，故一律走 PYTHON 子进程。"""
    _tr("auto_device: start")
    py = PYTHON
    try:
        probe = subprocess.run(
            [str(py), "-c", "import torch;print('1' if torch.cuda.is_available() else '0')"],
            capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW,
        )
        _tr(f"auto_device: probe({py}) -> {probe.stdout.strip()} err={probe.stderr[-200:]!r}")
        return "cuda" if ("1" in probe.stdout) else "cpu"
    except Exception as e:
        _tr(f"auto_device: probe err {e!r}")
        return "cpu"


def ffmpeg_bin():
    """ffmpeg 可执行：优先 runtime 内置 → 环境变量 → PATH。"""
    if ASSETS is not None:
        cand = ASSETS / "ffmpeg" / "bin" / "ffmpeg.exe"
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
    worker 等孙进程会残留继续占用 CPU/GPU，必须 taskkill /T 连根拔。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW,
        )
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
        creationflags=_NO_WINDOW,
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


def ffmpeg_convert(src, dst, sr=44100):
    # -c:a pcm_s16le：统一输出 16-bit PCM，兼容 stdlib wave 与下游工具
    cmd = [ffmpeg_bin(), "-y", "-v", "quiet", "-i", str(src),
           "-ar", str(sr), "-ac", "2", "-c:a", "pcm_s16le", str(dst)]
    _run(cmd, cwd=ROOT)


# 质量档 → (chunk 秒数, overlap 秒数)：精细档用更短的块 + 更多重叠，
# 拼接接缝更平滑、质量更高，耗时更长；快速档反之。
QUALITY_CHUNKS = {0: (20.0, 1.0), 1: (15.0, 2.0), 2: (10.0, 3.0)}


def mix_wet_dry(dry, wet, out, wet_ratio):
    """按 wet_ratio 加权混合 Lew 重建结果与干信号（重建强度）。

    wet_ratio ∈ [0, 1]；0 = 保留原信号，1 = 完全重建。写回 16-bit PCM。
    lew 的 FLOAT 输出与 ffmpeg 的 pcm_s16le 输入都可读（soundfile 优先，
    无 soundfile 时退回 stdlib wave + ffmpeg 转 PCM16）。
    """
    import numpy as np
    import wave

    try:
        import soundfile as sf
        a, fr = sf.read(str(dry), dtype="float64", always_2d=True)
        b, _ = sf.read(str(wet), dtype="float64", always_2d=True)
    except (ImportError, RuntimeError, ValueError):
        a, fr = _read_wav_any(dry)
        b, _ = _read_wav_any(wet)

    n = min(a.shape[0], b.shape[0])
    nc = min(a.shape[1], b.shape[1])
    mix = a[:n, :nc] * (1.0 - wet_ratio) + b[:n, :nc] * wet_ratio
    peak = float(np.abs(mix).max()) if mix.size else 0.0
    if peak > 0.999:
        mix *= 0.999 / peak
    out_i = np.clip(mix * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(int(nc))
        w.setsampwidth(2)
        w.setframerate(int(fr))
        w.writeframes(out_i.tobytes())


def _read_wav_pcm16(path):
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        nc = w.getnchannels()
        fr = w.getframerate()
        a = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, nc).astype(np.float64) / 32768.0
    return a, fr


def _read_wav_any(path):
    """读取波形：优先 soundfile；不可用时用 ffmpeg 转系统临时 PCM16 后立即读取并删除。
    临时文件只存在于系统临时目录，用完即删，绝不污染原始文件目录。"""
    import wave
    try:
        import soundfile as sf
        return sf.read(str(path), dtype="float64", always_2d=True)
    except (ImportError, RuntimeError, ValueError):
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sb_pcm16_")
        os.close(fd)
        try:
            ffmpeg_convert(path, tmp)
            return _read_wav_pcm16(tmp)
        finally:
            os.unlink(tmp)


def _ensure_pcm16(path):
    """若文件非 stdlib-wave 可读（如 lew 的 FLOAT 输出），用 ffmpeg 转成 PCM16。
    临时文件落在系统临时目录（用完由调用方删除），不再写在原始文件目录下。"""
    import wave
    try:
        with wave.open(str(path), "rb"):
            return Path(path)
    except Exception:
        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="sb_pcm16_")
        os.close(fd)
        ffmpeg_convert(path, tmp)
        return Path(tmp)


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


def stage_demucs(input_wav, out_dir, progress=None, cancel=None):
    if progress:
        progress(0.0, "Demucs 四轨分离")
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
    cmd = [PYTHON, "-m", "demucs", "--float32", "--clip-mode=none", "-n", "htdemucs",
           "-o", str(out_dir), str(input_wav)]
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


def stage_bass(stem_dir, in_mix, out_wav, sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
               bass_gain_db=0.0, progress=None, cancel=None):
    if progress:
        progress(0.0, "贝斯增强")
    bass = stem_dir / "bass.wav"
    if not bass.exists():
        raise PipelineError(f"分离产物缺失: {stem_dir}")
    cmd = [PYTHON, str(APOLLO_DIR / "bass_enhance.py"),
           "--bass", str(bass), "--in-mix", str(in_mix), "--out", str(out_wav),
           "--sub-db", str(sub_db), "--sat", str(sat), "--punch-db", str(punch_db),
           "--trans", str(trans), "--bass-gain-db", str(bass_gain_db)]
    _run_stream(cmd, APOLLO_DIR, cancel=cancel)
    if progress:
        progress(1.0, "贝斯增强完成")


def stage_drums(stem_dir, rest_wav, out_wav, punch_db=2.0, trans=0.3,
                drums_gain_db=0.0, progress=None, cancel=None):
    """鼓增强：punch/瞬态处理施加到鼓所在的轨上（4-stem 的 drums 轨）。"""
    if progress:
        progress(0.0, "鼓增强")
    stem_dir = Path(stem_dir)
    drums = stem_dir / "drums.wav"
    if not drums.exists():
        # 旧 two-stems 产物没有 drums 轨：跳过增强，透传贝斯阶段结果
        shutil.copyfile(rest_wav, out_wav)
    else:
        cmd = [PYTHON, str(APOLLO_DIR / "drum_enhance.py"),
               "--drums", str(drums), "--in-mix", str(rest_wav), "--out", str(out_wav),
               "--punch-db", str(punch_db), "--trans", str(trans),
               "--drums-gain-db", str(drums_gain_db)]
        _run_stream(cmd, APOLLO_DIR, cancel=cancel)
    if progress:
        progress(1.0, "鼓增强完成")


def stage_vocals(stem_dir, in_mix, out_wav, gain_db=0.0, progress=None, cancel=None):
    """人声整体增益（delta-add 保留分离残差）：0 dB 位级透传，不启动子进程。"""
    if progress:
        progress(0.0, "人声调整")
    stem_dir = Path(stem_dir)
    vocals = stem_dir / "vocals.wav"
    if gain_db == 0 or not vocals.exists():
        shutil.copyfile(in_mix, out_wav)
    else:
        cmd = [PYTHON, str(APOLLO_DIR / "vocal_adjust.py"),
               "--vocals", str(vocals), "--in-mix", str(in_mix), "--out", str(out_wav),
               "--vocal-gain-db", str(gain_db)]
        _run_stream(cmd, APOLLO_DIR, cancel=cancel)
    if progress:
        progress(1.0, "人声调整完成")


def stage_reshape(in_mix, stems_dir, out_wav, wet=1.0, denoise=0.0, width_db=6.0,
                  progress=None, cancel=None):
    """声场重塑（broadband delta-add）：wet 缩放全部处理差值，可附带 ≥10kHz 噪声地板降噪。

    width_db 为宽度上限（other 轨 side 增益 dB，drums 自动取一半），wet 决定向该
    宽度目标混合的比例。wet≤0 且 denoise≤0，或缺少 drums/other stems 时直接透传
    （位级不变）。denoise>0 时即使 wet=0 也会运行，以允许单独使用高频降噪。
    """
    if progress:
        progress(0.0, "声场重塑")
    stems_dir = Path(stems_dir)
    if (wet <= 0 and denoise <= 0) or not (stems_dir / "drums.wav").exists() or not (stems_dir / "other.wav").exists():
        shutil.copyfile(in_mix, out_wav)
    else:
        cmd = [PYTHON, str(APOLLO_DIR / "soundstage_reshape.py"),
               "--in-mix", str(in_mix), "--out-wav", str(out_wav),
               "--stems-dir", str(stems_dir),
               "--mode", "broadband", "--wet", str(wet),
               "--side-gain-db", str(width_db)]
        if denoise > 0:
            cmd += ["--other-denoise-amount", str(denoise)]
        _run_stream(cmd, APOLLO_DIR, cancel=cancel)
    if progress:
        progress(1.0, "声场重塑完成")


def stage_soren(input_wav, out_wav, genre="Pop", loudness="normal",
                eq_profile="Neutral", reference=None, lowpass_cutoff=None,
                progress=None, cancel=None):
    if progress:
        progress(0.0, f"Soren 母带（{genre or '自定义参考'} / {loudness} / {eq_profile}）")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOREN_DIR)
    cmd = [PYTHON, str(SOREN_DIR / "core_decrypted.py"),
           str(input_wav), str(out_wav),
           "--loudness", loudness, "--eq-profile", eq_profile]
    if reference:
        cmd += ["--reference", str(reference)]
    else:
        cmd += ["--genre", genre]
    if lowpass_cutoff:
        cmd += ["--lowpass-cutoff", str(lowpass_cutoff)]
    _run_stream(cmd, SOREN_DIR, env=env, cancel=cancel,
                on_progress=(lambda f: progress(f * 0.999, "Soren 母带")) if progress else None)
    if progress:
        progress(1.0, "Soren 母带完成")


def run_pipeline(input_wav, output_dir, *, sub_db=6.0, sat=0.3, punch_db=2.0, trans=0.3,
                 bass_gain_db=0.0, vocal_gain_db=0.0, genre="Pop", loudness="normal",
                 eq_profile="Neutral", reference=None, quality=1, guidance=1.5,
                 device="cuda", progress=None, cancel=None, work_dir=None,
                 lowpass_cutoff=None, space_wet=0.0, space_denoise=0.0,
                 space_width_db=6.0, bypass=()):
    """执行单文件完整链路。progress(stage_idx, frac, label)。

    bypass: 可迭代的阶段名（lew/vocals/bass/drums/reshape/soren），命中的阶段位级跳过。
    分离阶段不可 bypass（后续阶段依赖 stems 产物）。
    """
    input_wav = Path(input_wav)
    if not input_wav.exists():
        raise PipelineError(f"输入文件不存在: {input_wav}")
    if not output_dir:
        raise PipelineError("未指定输出目录，请在界面选择输出目录后再处理")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    work = Path(work_dir) if work_dir else output_dir / ".sorenstudio_work"
    work.mkdir(parents=True, exist_ok=True)

    bypass = set(bypass)
    if not bypass <= {"lew", "vocals", "bass", "drums", "reshape", "soren"}:
        raise PipelineError(f"未知 bypass 阶段: {sorted(bypass)}")

    stem = input_wav.stem
    out_final = output_dir / f"{stem}_shadowbuster.wav"
    lew_out = work / f"{stem}_lew.wav"
    stems_out = work / "stems"
    bass_out = work / f"{stem}_bassmix.wav"
    drum_out = work / f"{stem}_drummix.wav"
    vocal_out = work / f"{stem}_vocalmix.wav"
    shape_out = work / f"{stem}_shapemix.wav"

    def cb(i):
        def inner(frac, label):
            if cancel and cancel():
                raise PipelineError("用户取消")
            if progress:
                progress(i, frac, label)
        return inner

    try:
        if "lew" in bypass:
            # 直接以原输入进入 Demucs；Demucs 会在解码时适配其模型采样率。
            lew_src = input_wav
            if progress:
                cb(0)(1.0, "高频旁路")
        else:
            stage_lew(input_wav, lew_out, device=device, progress=cb(0),
                      quality=quality, guidance=guidance, cancel=cancel)
            lew_src = lew_out
        stage_demucs(lew_src, stems_out, progress=cb(1), cancel=cancel)
        stem_dir = stems_out / "htdemucs" / lew_src.stem
        if "bass" in bypass:
            shutil.copyfile(lew_src, bass_out)
            if progress:
                cb(2)(1.0, "贝斯旁路")
        else:
            stage_bass(stem_dir, lew_src, bass_out, sub_db=sub_db, sat=sat,
                       punch_db=0.0, trans=0.0, bass_gain_db=bass_gain_db,
                       progress=cb(2), cancel=cancel)
        if "drums" in bypass:
            shutil.copyfile(bass_out, drum_out)
            if progress:
                cb(3)(1.0, "鼓旁路")
        else:
            stage_drums(stem_dir, bass_out, drum_out, punch_db=punch_db, trans=trans,
                        progress=cb(3), cancel=cancel)
        vocal_src = drum_out
        if "vocals" in bypass or vocal_gain_db == 0:
            # 人声 0dB 或面板旁路：位级透传，不启动子进程
            shutil.copyfile(drum_out, vocal_out)
            if progress:
                cb(4)(1.0, "人声旁路" if "vocals" in bypass else "人声 0dB")
        else:
            stage_vocals(stem_dir, drum_out, vocal_out, gain_db=vocal_gain_db,
                         progress=cb(4), cancel=cancel)
        if "reshape" in bypass:
            shutil.copyfile(vocal_src, shape_out)
            if progress:
                cb(4)(1.0, "声场旁路")
        else:
            stage_reshape(vocal_src, stem_dir, shape_out, wet=space_wet,
                          denoise=space_denoise, width_db=space_width_db,
                          progress=cb(4), cancel=cancel)
        if "soren" in bypass:
            shutil.copyfile(shape_out, out_final)
            if progress:
                cb(5)(1.0, "母带旁路")
        else:
            stage_soren(shape_out, out_final, genre=genre, loudness=loudness,
                        eq_profile=eq_profile, reference=reference,
                        lowpass_cutoff=lowpass_cutoff, progress=cb(5),
                        cancel=cancel)
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
    # CLI 测试模式
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", required=True)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--sub-db", type=float, default=6.0)
    ap.add_argument("--sat", type=float, default=0.3)
    ap.add_argument("--genre", default="Pop")
    ap.add_argument("--loudness", default="normal")
    ap.add_argument("--eq-profile", default="Neutral")
    ap.add_argument("--reference", default=None)
    ap.add_argument("--lowpass-cutoff", type=float, default=None)
    ap.add_argument("--space-wet", type=float, default=0.0)
    ap.add_argument("--space-denoise", type=float, default=0.0)
    ap.add_argument("--space-width-db", type=float, default=6.0,
                    help="声场宽度上限 dB（other 轨 side 增益，drums 自动取一半）")
    ap.add_argument("--vocal-gain-db", type=float, default=0.0,
                    help="人声整体增益 dB（0 = 直通）")
    ap.add_argument("--bypass", default="", help="逗号分隔的旁路阶段名")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    def p(i, frac, label):
        print(f"  [{label}] {frac*100:.0f}%", flush=True)

    t0 = time.time()
    out = run_pipeline(args.input, args.output, sub_db=args.sub_db, sat=args.sat,
                       genre=args.genre, loudness=args.loudness,
                       eq_profile=args.eq_profile, reference=args.reference,
                       lowpass_cutoff=args.lowpass_cutoff,
                       space_wet=args.space_wet, space_denoise=args.space_denoise,
                       space_width_db=args.space_width_db,
                       vocal_gain_db=args.vocal_gain_db,
                       bypass=[b.strip() for b in args.bypass.split(",") if b.strip()],
                       device="cpu" if args.cpu else "cuda", progress=p)
    print(f"完成: {out}（{time.time()-t0:.0f}s）")
