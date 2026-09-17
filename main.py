"""ShadowBuster — 母带工坊（Qt WebView 壳）
PySide6 + QWebEngineView + QWebChannel，加载 ui/index.html（KFL 合规 DAW 插件界面）。
管线在后台线程执行，进度经 Signal 推送到前端。
"""
import os
import sys

if __name__ == "__main__" and any(arg in sys.argv[1:] for arg in ("--cli", "--help", "--version")):
    from processing_cli import main as cli_main
    raise SystemExit(cli_main())

import shutil
import threading
import traceback
import uuid
from pathlib import Path

# 使用 QtWebEngine 默认的 GPU 加速与合成路径。
# 不在全局强制软件渲染；特殊远程桌面或旧驱动环境应单独启用兼容模式。
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--disable-sandbox --no-sandbox --disable-dev-shm-usage")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PySide6.QtCore import QObject, Qt, Signal, Slot, QUrl, QEvent, QTimer, QPoint, QSize, QSettings
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile, QWebEnginePage

import studio_backend as backend


def map_ui_params(params, six_stem_available=None):
    """UI 旋钮 → 处理语义的固定映射（不新增旋钮；面向非专业用户）。

    - 高频降噪 fader > 0 → noise_mode="adaptive_all"（Stage3 跨分轨置信度降噪：
      只在高置信度识别到稳定嘶声时衰减，无嘶声的歌几乎不动；六轨运行同时覆盖
      guitar/synth 组）。fader=0 → 兼容路径 other。显式隐藏参数 noise_mode 优先。
    - 鼓身 punch（0–10）同时驱动 Kick/Bass 有界让位：amount = 0.5×punch/10
      （默认鼓身 2 → 0.1；上限 6dB、仅 20–180Hz，不会挖空低音）。
      显式隐藏参数 sidechain_amount 优先；attack/release/max_duck 固定默认。
    - 人声压缩默认 0.4（有界宽带、空气感不受频谱损失）；人声空气高架默认
      镜像补偿母带 8kHz 高架衰减（只补不削，封顶 2dB）——"无论如何不丢
      人声空气感"。显式隐藏参数 vocal_comp_amount / vocal_air_db 优先。
    - 吉他推子（声场面板，0–1，0=中性）：presence/mud/harsh/小幅电平联动；
      非零时需要六轨，权重缺失则明确提示回退（吉他调整不生效）。
      显式隐藏参数 guitar_*/synth_* 分项优先。
    返回 (mapping, notices)；mapping 可直接 ** 解包进 run_batch。
    """
    def explicit(key):
        value = params.get(key)
        return None if value is None else float(value)

    def with_default(key, default):
        value = explicit(key)
        return default if value is None else value

    denoise = float(params.get("denoise", 0.0) or 0.0)
    punch = float(params.get("punch", 2.0) or 0.0)
    guitar = float(params.get("guitar", 0.0) or 0.0)
    try:
        quality = int(params.get("quality", 1))
    except (TypeError, ValueError):
        quality = 1

    noise_mode = params.get("noise_mode") or (
        "adaptive_all" if denoise > 0 else "other")
    sidechain_amount = explicit("sidechain_amount")
    if sidechain_amount is None:
        sidechain_amount = round(0.5 * (punch / 10.0), 4)

    # 人声压缩默认随 UI 启用（有界、空气感不受频谱损失）；空气高架镜像补偿
    # 母带 8kHz 高架衰减（SOREN_HIGH_SHELF_MID_DB），只补不削、封顶 2dB。
    vocal_comp_amount = with_default("vocal_comp_amount", 0.4)
    vocal_air_db = with_default(
        "vocal_air_db", min(2.0, round(-backend.SOREN_HIGH_SHELF_MID_DB, 4)))
    # 自动清晰恒开（发闷必然有害；有界 ±2dB 且有置信度门控）。显式 False 优先。
    bass_auto_clarity = params.get("bass_auto_clarity")
    bass_auto_clarity = True if bass_auto_clarity is None else bool(bass_auto_clarity)

    # 吉他推子（0–1，0=中性）：更清楚（presence）+ 少浑浊（mud）+ 少毛刺
    # （harsh）+ 电平小幅跟随；非零时需要六轨分离（权重缺失则明确提示回退）。
    needs_six_stem = quality == 2 or guitar > 0
    demucs_model = params.get("demucs_model")
    notices = []
    if not demucs_model:
        if needs_six_stem:
            available = backend.six_stem_weights_available() if six_stem_available is None \
                else bool(six_stem_available())
            if available:
                demucs_model = "htdemucs_6s"
            else:
                demucs_model = "htdemucs"
                notices.append("六轨权重未预置，精细档回退四轨分离"
                               + ("，吉他调整本次不生效" if guitar > 0 else ""))
        else:
            demucs_model = "htdemucs"

    mapping = {
        "noise_mode": noise_mode,
        "noise_low_hz": with_default("noise_low_hz", 8000.0),
        "noise_high_hz": with_default("noise_high_hz", 20000.0),
        "noise_max_attenuation_db": with_default("noise_max_attenuation_db", 6.0),
        "sidechain_amount": sidechain_amount,
        "sidechain_attack_ms": with_default("sidechain_attack_ms", 5.0),
        "sidechain_release_ms": with_default("sidechain_release_ms", 150.0),
        "sidechain_max_duck_db": with_default("sidechain_max_duck_db", 6.0),
        "demucs_model": demucs_model,
        "vocal_comp_amount": vocal_comp_amount,
        "vocal_air_db": vocal_air_db,
        "bass_auto_clarity": bass_auto_clarity,
    }
    if guitar > 0:
        mapping.update(guitar_gain_db=round(1.5 * guitar - 0.75, 4),
                       guitar_mud_cut_db=round(2.5 * guitar, 4),
                       guitar_presence_db=round(4.0 * guitar, 4),
                       guitar_harsh_cut_db=round(1.5 * guitar, 4),
                       guitar_width_db=0.0)
    # 显式隐藏参数一律覆盖映射值（含 guitar/synth 各分项）。
    for key in ("guitar_gain_db", "guitar_mud_cut_db", "guitar_presence_db",
                "guitar_harsh_cut_db", "guitar_width_db",
                "synth_gain_db", "synth_mud_cut_db", "synth_presence_db",
                "synth_harsh_cut_db", "synth_width_db"):
        if params.get(key) is not None:
            mapping[key] = float(params[key])
    return mapping, notices


def _preview_cleanup(base):
    """删除预览临时目录（仅预览工作区，绝不触碰缓存/成品）。"""
    if base is not None:
        shutil.rmtree(base, ignore_errors=True)


def _spectrogram_payload(path, time_frames=800, freq_bins=160):
    """整文件频谱图显示数据：单声道下混 STFT 幅度 → dB 整数（-90..0）。

    仅用于界面显示，不参与任何音频处理；时间轴均匀分桶覆盖整曲。
    """
    import json as _json

    import numpy as _np
    import soundfile as _sf
    data, sr = _sf.read(path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.ndim == 2 else data
    n = len(mono)
    win = 2048
    hop = max(win // 2, n // max(1, time_frames))
    starts = _np.arange(0, max(1, n - win + 1), hop)
    if len(starts) > time_frames:
        starts = _np.linspace(0, len(starts) - 1, time_frames).astype(int)
    window = _np.hanning(win)
    spec = _np.zeros((freq_bins, len(starts)), dtype=_np.float32)
    for i, s0 in enumerate(starts):
        frame = mono[s0:s0 + win] * window
        mag = _np.abs(_np.fft.rfft(frame))[:freq_bins]
        spec[:, i] = mag
    db = 20.0 * _np.log10(spec + 1e-9)
    # 自归一：以本文件最高频谱电平为 0 参考向下 90dB 映射，避免未归一 FFT
    # 幅度整体饱和成一片亮色。
    db = db - (float(db.max()) if db.size else 0.0)
    db = _np.clip((db + 90.0) * (255.0 / 90.0), 0, 255).astype(int)
    return {"w": int(len(starts)), "h": int(freq_bins),
            "duration": round(n / sr, 3), "sr": int(sr),
            "data": db.T.ravel().tolist()}


def collect_pipeline_kwargs(params):
    """面板参数 → run_pipeline kwargs（批处理与预览共用同一组装与映射）。"""
    params = dict(params or {})
    mapping, notices = map_ui_params(params)

    def _stem_params(prefix):
        return {f"{prefix}_{name}": float(params.get(f"{prefix}_{name}", 0.0) or 0.0)
                for name in ("gain_db", "mud_cut_db", "presence_db",
                             "harsh_cut_db", "width_db")}

    kwargs = dict(
        sub_db=float(params.get("sub", 6)),
        sat=float(params.get("sat", 0.3)),
        punch_db=float(params.get("punch", 2)),
        trans=float(params.get("trans", 0.3)),
        space_wet=float(params.get("space", 0.6)),
        space_denoise=float(params.get("denoise", 0.2)),
        space_width_db=float(params.get("space_width", 6)),
        vocal_gain_db=float(params.get("vocal", 0)),
        bypass=[b for b in (params.get("bypass") or []) if b],
        quality=int(params.get("quality", 1)),
        guidance=float(params.get("guidance", 1.5)),
        genre=params.get("genre", "Pop"),
        style_mode=params.get("style_mode", "styled"),
        style_blend=float(params.get("style_blend", 0.85)),
        loudness=params.get("loudness", "normal"),
        eq_profile=params.get("eq", "Neutral"),
        reference=params.get("reference") or None,
        device=backend.auto_device(),
    )
    kwargs.update(mapping)
    kwargs.update(_stem_params("synth"))
    return kwargs, notices

ROOT = Path(__file__).parent
UI_INDEX = ROOT / "ui" / "index.html"

AUDIO_FILTER = "音频文件 (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;所有文件 (*.*)"
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

# 窗口尺寸契约：宽度下限 = 四面板旋钮排布不被挤压的最小值；
# 高度可在默认基础上伸缩，不足时前端内容区滚动、底部按钮固定。
# 默认 1000×860：紧凑布局内容（705px）+ 顶栏/底部/间距（148px）+ 余量。
MIN_WINDOW = (840, 600)
DEFAULT_WINDOW = (1000, 860)

# 主题对应的原生窗口底色（与 ui/style.css 的 --c-bg 一致）
THEME_WINDOW_COLOR = {"dark": "#141218", "light": "#f1f0f1"}


class DropAwareWebEngineView(QWebEngineView):
    """支持 OS 文件拖入的 WebEngine 视图。

    拖放事件落在 WebEngine 的内部渲染控件（focusProxy）上而不是视图本身，
    必须向该控件安装事件过滤器拦截；JS 侧拿不到拖入文件的完整路径，
    因此在 Qt 层取 url 列表后经 Signal 推给前端。
    """

    filesDropped = Signal(list)
    filesDroppedAt = Signal(list, int, int)
    dragHover = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._drop_installed = False

    def event(self, ev):
        # focusProxy 在页面首次渲染后才存在：子控件挂载时延迟安装一次
        if not self._drop_installed and ev.type() == QEvent.Type.ChildAdded:
            QTimer.singleShot(0, self._install_drop_filter)
        return super().event(ev)

    def _install_drop_filter(self):
        proxy = self.focusProxy()
        if proxy is not None and not self._drop_installed:
            proxy.installEventFilter(self)
            self._drop_installed = True

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if ev.mimeData().hasUrls():
                ev.acceptProposedAction()
                self.dragHover.emit(True)
                return True
        elif t == QEvent.Type.DragLeave:
            self.dragHover.emit(False)
        elif t == QEvent.Type.Drop:
            self.dragHover.emit(False)
            if ev.mimeData().hasUrls():
                paths = []
                for u in ev.mimeData().urls():
                    if not u.isLocalFile():
                        continue
                    p = Path(u.toLocalFile())
                    if p.is_dir() or p.suffix.lower() in AUDIO_EXTS:
                        paths.append(str(p))
                ev.acceptProposedAction()
                if paths:
                    pos = ev.position().toPoint()
                    # 音频文件进队列；目录与音频一起走落点路由（输出/参考框）
                    audio = [p for p in paths if Path(p).suffix.lower() in AUDIO_EXTS]
                    if audio:
                        self.filesDropped.emit(audio)
                    self.filesDroppedAt.emit(paths, pos.x(), pos.y())
                return True
        return super().eventFilter(obj, ev)


def apply_native_theme(window, mode):
    """原生窗口底色与标题栏跟随主题。

    WebView 内容覆盖整个客户区，原生底色只在边缘/露底时可见；标题栏颜色
    走 DWM：Win11 22000+ 支持 CAPTION_COLOR 精确着色，Win10 仅
    USE_IMMERSIVE_DARK_MODE（深色标题栏）生效，浅色标题栏跟随系统设置。
    """
    color = QColor(THEME_WINDOW_COLOR.get(mode, THEME_WINDOW_COLOR["dark"]))
    pal = window.palette()
    pal.setColor(QPalette.Window, color)
    window.setPalette(pal)
    try:
        window.view.page().setBackgroundColor(color)
    except Exception:
        pass
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    try:
        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
        dark = wintypes.BOOL(1 if mode == "dark" else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        cr = wintypes.DWORD(color.red() | (color.green() << 8) | (color.blue() << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cr), ctypes.sizeof(cr))  # CAPTION_COLOR
    except Exception:
        pass


class Bridge(QObject):
    """前端 JS 与 Python 的桥接对象。"""

    stageChanged = Signal(int, float, str)   # stage idx, frac, label
    fileProgress = Signal(int, int, str)     # file idx (0-based), total, file name
    fileFinished = Signal(int, int, str, bool, str)  # file idx, total, name, succeeded, error
    logLine = Signal(str, str)               # text, css class
    done = Signal(str, int, int, str)        # 输出路径, 成功数, 失败数, 失败详情
    failed = Signal(str)                     # 错误消息
    filesDropped = Signal(list)              # OS 拖入的音频文件路径列表
    dragHover = Signal(bool)                 # 文件正在拖入悬停（前端高亮队列）
    updateInfo = Signal(str)                 # 检查更新结果（JSON，后台线程回传）
    gpuStatus = Signal(str)                  # GPU 环境状态/进度（JSON，后台线程回传）
    cacheStatus = Signal(str)                # 处理缓存状态（JSON：info/cleared/busy/error）
    qualitySummary = Signal(str)             # 单个输出的质量摘要（JSON）
    previewPeaks = Signal(str)               # 预览波形峰值（JSON：duration/sr/peaks）
    previewDone = Signal(str)                # 预览渲染完成（JSON：output/quality/notices）
    previewFailed = Signal(str)              # 预览失败消息
    previewProgress = Signal(float)          # 预览渲染总体进度 0..1

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._cancel_flag = threading.Event()
        self._thread = None
        self._gpu_cancel = threading.Event()
        self._gpu_thread = None
        self._gpu_lock = threading.Lock()
        self._preview_thread = None
        # 缓存操作与批处理启动共用一把非阻塞闸门：任一方持有时另一方直接拒绝，
        # 避免清空/改容量与开始处理之间出现竞态（闸门本身不做长任务持有）。
        self._cache_gate = threading.Lock()

    # ── JS 可调用的方法 ──
    @Slot()
    def checkUpdate(self):
        """后台线程查询 GitHub Releases 最新版本，结果经 updateInfo 信号回传。"""
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        import json as _json
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/AngleNaris/ShadowBuster/releases/latest",
                headers={"User-Agent": "ShadowBuster",
                         "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read().decode("utf-8"))
            tag = str(data.get("tag_name", "") or "").lstrip("v")
            self.updateInfo.emit(_json.dumps({
                "ok": True,
                "current": backend.APP_VERSION,
                "latest": tag,
                "name": str(data.get("name", "") or ""),
                "body": str(data.get("body", "") or "")[:500],
                "url": str(data.get("html_url", "") or ""),
            }, ensure_ascii=False))
        except Exception as e:   # 网络不可用 / 仓库私有 / API 限流等
            self.updateInfo.emit(_json.dumps({
                "ok": False,
                "error": str(e)[:160],
            }, ensure_ascii=False))

    # ── GPU 环境：清单查询 / 下载安装 / 设备探测 / 重启（v1.5）──
    def _gpu_emit(self, d):
        import json as _json
        self.gpuStatus.emit(_json.dumps(d, ensure_ascii=False))

    @Slot()
    def checkGpuEnv(self):
        """后台线程查询 GPU 环境发布状态，结果经 gpuStatus 回传。"""
        if not self._gpu_lock.acquire(blocking=False):
            self._gpu_emit({"type": "busy", "op": "check"})
            return
        thread = threading.Thread(target=self._gpu_check_worker, daemon=True)
        try:
            thread.start()
        except Exception as e:
            self._gpu_lock.release()
            self._gpu_emit({"type": "error", "msg": f"GPU 环境检查启动失败：{e}"[:200]})

    def _gpu_check_worker(self):
        try:
            if os.name != "nt" or not getattr(sys, "frozen", False):
                # GPU 运行时下载是 Windows（NVIDIA CUDA）专属机制；macOS 走
                # 系统内置 MPS/CPU，开发模式用本地环境，都无需下载。
                self._gpu_emit({"type": "state", "dev": True,
                                "platform": sys.platform})
                return
            import gpu_env as ge
            # 检测顺序：应用目录（新安装位置）→ 旧版用户目录（系统环境）；
            # 任一可用即无需下载。scanning 事件供 UI 展示扫描进度提示。
            self._gpu_emit({"type": "scanning", "phase": "app"})
            current = ge.installed_info(expected_version=backend.GPU_ENV_VERSION)
            source = "app"
            if current is None:
                self._gpu_emit({"type": "scanning", "phase": "system"})
                current = ge.legacy_installed_info(expected_version=backend.GPU_ENV_VERSION)
                source = "system"
            if current is not None:
                self._gpu_emit({"type": "state", "installed": current, "source": source,
                                "manifest": None, "error": None})
                return
            m = ge.load_manifest(backend.GPU_ENV_VERSION)
            module_paths = (
                backend.ROOT / "runtime" / "Apollo",
                backend.ROOT / "runtime" / "Soren_src",
            )
            installed = ge.installed_info(
                expected_version=m["version"], expected_sha256=m["sha256"]
            )
            if installed is None:
                installed = ge.revalidate_existing(
                    m["version"], m["sha256"],
                    module_paths=module_paths,
                    torch_version="2.7.1+cu128",
                    torchaudio_version="2.7.1+cu128",
                )
            if installed is None and ge.app_writable():
                installed = ge.migrate_legacy_runtime(
                    backend.ROOT / "runtime" / "env",
                    m["version"], m["sha256"],
                    module_paths=module_paths,
                    torch_version="2.7.1+cu128",
                    torchaudio_version="2.7.1+cu128",
                )
            payload = {"type": "state", "installed": installed,
                       "source": "app" if installed else None,
                       "manifest": {
                           "version": m["version"],
                           "totalSize": m["totalSize"],
                           "sha256": m["sha256"],
                           "parts": [{"name": p["name"], "size": p["size"]} for p in m["parts"]],
                       },
                       "writable": ge.app_writable(),
                       "nvidiaDriver": ge.nvidia_driver_present(),
                       "error": None}
            self._gpu_emit(payload)
        except Exception as e:   # 兜底
            self._gpu_emit({"type": "error", "msg": f"内部错误: {e}"[:200]})
        finally:
            self._gpu_lock.release()

    @Slot(result=bool)
    def startGpuInstall(self):
        """启动 GPU 环境下载安装（后台线程，防重入）。"""
        if not self._gpu_lock.acquire(blocking=False):
            self._gpu_emit({"type": "busy", "op": "install"})
            return False
        if self._gpu_thread and self._gpu_thread.is_alive():
            self._gpu_lock.release()
            return False
        self._gpu_cancel.clear()
        thread = threading.Thread(target=self._gpu_install_worker, daemon=True)
        self._gpu_thread = thread
        try:
            thread.start()
        except Exception:
            self._gpu_thread = None
            self._gpu_lock.release()
            raise
        return True

    @Slot()
    def cancelGpuInstall(self):
        self._gpu_cancel.set()

    def _gpu_install_worker(self):
        import shutil as _shutil
        import time as _time
        import zipfile as _zipfile
        from pathlib import Path as _P
        import gpu_env as ge
        cancel = self._gpu_cancel
        state = {"last": [0.0]}
        staging = None
        swap_started = False

        def prog(phase, cur, total):
            t = _time.monotonic()
            if t - state["last"][0] >= 0.2:
                state["last"][0] = t
                self._gpu_emit({"type": "progress", "phase": phase,
                                "cur": int(cur), "total": int(total)})

        try:
            if os.name != "nt":
                raise RuntimeError(
                    "GPU 运行时下载仅支持 Windows；macOS 使用系统内置的 "
                    "Apple Silicon MPS / CPU 推理，无需下载运行时。")
            installed = ge.installed_info(expected_version=backend.GPU_ENV_VERSION)
            if installed is not None:
                self._gpu_emit({"type": "done", "version": installed["version"],
                                "reused": True})
                return
            # GPU 环境就地安装到应用目录；目录不可写（如 Program Files 非
            # 提权）时提前终止，避免白下载几个 GB 才失败。
            if not ge.app_writable():
                raise RuntimeError(
                    "应用安装目录不可写，无法安装 GPU 环境；请以管理员身份运行"
                    "应用，或将应用安装到当前用户可写的目录后重试")
            self._gpu_emit({"type": "progress", "phase": "info", "cur": 0, "total": 1})
            m = ge.load_manifest(backend.GPU_ENV_VERSION)
            expected_torch = "2.7.1+cu128"
            expected_torchaudio = "2.7.1+cu128"
            module_paths = (backend.ROOT / "runtime" / "Apollo",
                            backend.ROOT / "runtime" / "Soren_src")
            installed = ge.installed_info(
                expected_version=m["version"], expected_sha256=m["sha256"]
            )
            if installed is None:
                installed = ge.revalidate_existing(
                    m["version"], m["sha256"], module_paths=module_paths,
                    torch_version=expected_torch,
                    torchaudio_version=expected_torchaudio,
                )
            if installed is None:
                installed = ge.migrate_legacy_runtime(
                    backend.ROOT / "runtime" / "env", m["version"], m["sha256"],
                    module_paths=module_paths,
                    torch_version=expected_torch,
                    torchaudio_version=expected_torchaudio,
                )
            if installed is None:
                installed = ge.recover_extracted_runtime(m, cancel=lambda: cancel.is_set())
            if installed is not None:
                self._gpu_emit({"type": "done", "version": installed.get("version", m["version"]),
                                "reused": True})
                return

            total = m["totalSize"]
            ge.runner_dir().mkdir(parents=True, exist_ok=True)
            free = _shutil.disk_usage(ge.runner_dir()).free
            if free < total * 2 + (1 << 30):
                raise RuntimeError(f"磁盘空间不足：需要约 {max(1, (total*2 + (1<<30)) >> 30)} GB，"
                                   f"当前可用 {free >> 30} GB")
            work = ge.dl_dir(m["version"])
            work.mkdir(parents=True, exist_ok=True)
            zpath = work / f"gpu-env-{m['version']}.zip"
            zip_ready = ge.valid_cached_file(zpath, total, m["sha256"],
                                             cancel=lambda: cancel.is_set())
            if not zip_ready and zpath.exists():
                zpath.unlink(missing_ok=True)

            parts = []
            if not zip_ready:
                base = 0
                n = len(m["parts"])
                for i, p in enumerate(m["parts"], 1):
                    if cancel.is_set():
                        raise ge.DownloadCancelled("下载已取消")
                    self._gpu_emit({"type": "progress", "phase": "download",
                                    "cur": base, "total": total,
                                    "part": f"{i}/{n}"})
                    dest = work / p["name"]
                    try:
                        ge.download_part(p["url"], dest, p["size"],
                                         cancel=lambda: cancel.is_set(),
                                         progress=lambda off, b=base: prog("download", b + off, total))
                        prog("verify", base, total)
                        sha = ge.sha256_of(dest, cancel=lambda: cancel.is_set())
                        if sha != p["sha256"]:
                            dest.unlink(missing_ok=True)
                            raise RuntimeError(f"分卷 {p['name']} SHA-256 校验失败，请重试")
                    except ge.DownloadCancelled:
                        raise
                    except RuntimeError:
                        raise
                    except Exception:
                        raise
                    base += p["size"]
                    parts.append({**p, "local": str(dest)})
                prog("assemble", 0, total)
                ge.assemble_zip(parts, zpath, cancel=lambda: cancel.is_set(),
                                progress=lambda c, t: prog("assemble", c, t))
                prog("verify", 0, total)
                h = ge.sha256_of(zpath, cancel=lambda: cancel.is_set(),
                                 progress=lambda c, t: prog("verify", c, t))
                if h != m["sha256"]:
                    zpath.unlink(missing_ok=True)
                    raise RuntimeError("GPU 环境包 SHA-256 校验失败，请重试")

            with _zipfile.ZipFile(zpath) as zf:
                extracted = sum(i.file_size for i in zf.infolist() if not i.is_dir())
            free = _shutil.disk_usage(ge.runner_dir()).free
            if free < extracted + (1 << 30):
                raise RuntimeError(f"解压空间不足：需要约 {max(1, (extracted + (1<<30)) >> 30)} GB，"
                                   f"当前可用 {free >> 30} GB，请清理后重试")
            staging = ge.runner_dir() / f".staging-{uuid.uuid4().hex}"
            prog("extract", 0, extracted)
            ge.extract_zip(zpath, staging, cancel=lambda: cancel.is_set(),
                           progress=lambda c, t: prog("extract", c, t))
            prog("verify", 0, 1)
            ge.validate_runtime(
                staging / "python.exe",
                module_paths=(staging / "Apollo", staging / "Soren_src"),
                torch_version=expected_torch,
                torchaudio_version=expected_torchaudio,
            )
            ge.assert_runtime_files(staging / "python.exe")
            swap_started = True
            ge.swap_env(staging, m["version"], m["sha256"], runtime_validated=True)
            staging = None
            for p in m["parts"]:
                try:
                    (work / p["name"]).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                zpath.unlink(missing_ok=True)
            except OSError:
                pass
            self._gpu_emit({"type": "done", "version": m["version"]})
        except ge.DownloadCancelled:
            self._gpu_emit({"type": "cancelled"})
        except Exception as e:
            msg = str(e)
            if isinstance(e, PermissionError) or getattr(e, "winerror", None) in {5, 32, 33}:
                msg = (f"GPU 环境写入应用目录被 Windows 权限或文件占用阻止；"
                       f"现有环境和已下载缓存均已保留，请以管理员身份运行应用、"
                       f"关闭正在处理的任务后重试。{msg}")
            self._gpu_emit({"type": "error", "msg": msg[:300]})
        finally:
            if staging is not None and not swap_started and staging.exists():
                try:
                    _shutil.rmtree(staging, ignore_errors=True)
                except OSError:
                    pass
            self._gpu_lock.release()

    @Slot()
    def probeDevice(self):
        """探测推理设备（子进程），结果经 gpuStatus 回传。"""
        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self):
        with self._gpu_lock:
            try:
                dev = backend.auto_device()
            except Exception:
                dev = "cpu"
        self._gpu_emit({"type": "device", "device": dev})

    @Slot()
    def restartApp(self):
        """重启应用（GPU 环境安装完成后生效）。"""
        from PySide6.QtCore import QProcess, QCoreApplication
        QProcess.startDetached(str(Path(sys.executable).resolve()))
        QTimer.singleShot(400, QCoreApplication.instance().quit)

    # ── 处理缓存：状态 / 容量 / 清空 ──
    # 删除一律经由 pipeline_cache 助手（clear_cache），桥接层不做任何文件操作。
    def _cache_emit(self, d):
        import json as _json
        self.cacheStatus.emit(_json.dumps(d, ensure_ascii=False))

    @staticmethod
    def _cache_payload(pc):
        info = pc.get_info() or {}
        return {"capacity_gb": info.get("capacity_gb", 5),
                "used_bytes": int(info.get("used_bytes", 0) or 0),
                "entries": int(info.get("entries", 0) or 0)}

    @Slot()
    def refreshCacheInfo(self):
        """后台线程读取缓存容量与已用量，结果经 cacheStatus 回传。"""
        threading.Thread(target=self._cache_refresh_worker, daemon=True).start()

    def _cache_refresh_worker(self):
        try:
            import pipeline_cache as pc
            self._cache_emit({"type": "info", **self._cache_payload(pc)})
        except ModuleNotFoundError:
            self._cache_emit({"type": "error", "op": "info", "msg": "缓存组件未就绪"})
        except Exception as e:
            self._cache_emit({"type": "error", "op": "info", "msg": str(e)[:160]})

    @Slot("QVariant", result=bool)
    def setCacheCapacity(self, gb):
        """设置缓存容量（GiB，0=关闭）。处理进行中拒绝；结果经 cacheStatus 回传。"""
        return self._start_cache_op("capacity", gb)

    @Slot(result=bool)
    def clearCache(self):
        """清空处理缓存（后台线程）。处理进行中拒绝；结果经 cacheStatus 回传。"""
        return self._start_cache_op("clear")

    def _start_cache_op(self, op, gb=None):
        # 与批处理启动互斥：处理中拒绝；缓存操作已排队时同样拒绝（busy）。
        if not self._cache_gate.acquire(blocking=False):
            self._cache_emit({"type": "busy", "op": op})
            return False
        if self._thread and self._thread.is_alive():
            self._cache_gate.release()
            self._cache_emit({"type": "busy", "op": op})
            return False
        thread = threading.Thread(target=self._cache_op_worker, args=(op, gb), daemon=True)
        try:
            thread.start()
        except Exception:
            self._cache_gate.release()
            self._cache_emit({"type": "error", "op": op, "msg": "缓存操作启动失败"})
            return False
        return True

    def _cache_op_worker(self, op, gb):
        try:
            import pipeline_cache as pc
            if op == "clear":
                pc.clear_cache()
                self._cache_emit({"type": "cleared", **self._cache_payload(pc)})
            else:
                pc.set_capacity_gb(int(gb))
                self._cache_emit({"type": "info", **self._cache_payload(pc)})
        except ModuleNotFoundError:
            self._cache_emit({"type": "error", "op": op, "msg": "缓存组件未就绪"})
        except Exception as e:
            self._cache_emit({"type": "error", "op": op, "msg": str(e)[:160]})
        finally:
            self._cache_gate.release()

    @Slot(str)
    def openExternal(self, url):
        """用系统默认浏览器打开链接（如更新页面）。"""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(url))
    @Slot(list, int, int)
    def routeDrop(self, paths, x, y):
        """把拖入路径与页面坐标交给前端：由前端决定加入队列还是填入路径框。"""
        import json
        self._window.view.page().runJavaScript(
            "window.__sbDropAt && window.__sbDropAt("
            + json.dumps(list(paths)) + f", {int(x)}, {int(y)})")

    @Slot(result=str)
    def selectInput(self):
        path, _ = QFileDialog.getOpenFileName(
            self._window, "选择输入音频", "", AUDIO_FILTER)
        return path

    @Slot(result="QVariantList")
    def selectInputs(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self._window, "选择输入音频（可多选）", "", AUDIO_FILTER)
        return list(paths)

    @Slot(result=str)
    def selectReference(self):
        path, _ = QFileDialog.getOpenFileName(
            self._window, "选择 Soren 参考音频（风格基准）",
            "", "音频文件 (*.wav *.mp3 *.flac *.ogg *.m4a);;所有文件 (*.*)")
        return path

    @Slot(result=str)
    def selectOutput(self):
        path = QFileDialog.getExistingDirectory(self._window, "选择输出目录")
        return path

    @Slot(result=str)
    def debugPing(self):
        return "pong"

    @Slot(str, result=str)
    def debugEcho(self, s):
        return "echo:" + str(s)

    @Slot("QVariantMap", result=bool)
    def process(self, params):
        """启动批处理（后台线程）。params 是前端传来的 dict。"""
        backend._tr(f"process: slot invoked params_keys={list((params or {}).keys())} type={type(params).__name__}")
        if self._thread and self._thread.is_alive():
            return False
        if self._gpu_lock.locked():
            self._gpu_emit({"type": "busy", "op": "process"})
            return False
        if not self._cache_gate.acquire(blocking=False):
            # 缓存清空 / 改容量进行中：拒绝启动，避免与缓存删除竞态
            self._cache_emit({"type": "busy", "op": "process"})
            return False
        self._cancel_flag.clear()
        p = dict(params or {})
        self._gpu_lock.acquire()
        self._thread = threading.Thread(
            target=self._run_batch, args=(p,), daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self._gpu_lock.release()
            self._cache_gate.release()
            raise
        self._cache_gate.release()
        return True

    @Slot()
    def cancel(self):
        self._cancel_flag.set()

    # ── 报告：从输出目录扫描质量报告（跨会话可见，不依赖本次处理）──
    @Slot(str, result=str)
    def reportsScan(self, out_dir):
        """列出输出目录下成品旁的质量报告（新→旧），供报告页选择。"""
        import json as _json
        items = []
        try:
            d = Path(out_dir)
            if d.is_dir():
                reports = sorted(d.glob("*.quality.json"),
                                 key=lambda q: q.stat().st_mtime, reverse=True)
                for q in reports[:60]:
                    items.append({"name": q.name[: -len(".quality.json")],
                                  "path": str(q)})
        except (OSError, ValueError):
            pass
        return _json.dumps({"reports": items}, ensure_ascii=False)

    @Slot(str, result=str)
    def reportLoad(self, path):
        """读取单份质量报告全文（同步；报告为本地小 JSON）。"""
        import json as _json
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            return _json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ── 预览：波形峰值 + 片段快速渲染（与批处理互斥，GPU 锁保护）──
    @Slot(str)
    def previewLoad(self, path):
        p = Path(path)
        if not p.is_file():
            self.previewFailed.emit("文件不存在，无法预览")
            return
        if self._preview_thread and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(
            target=self._preview_load_worker, args=(str(p),), daemon=True)
        self._preview_thread.start()

    def _preview_load_worker(self, path):
        try:
            import json as _json

            import numpy as _np
            import soundfile as _sf
            info = _sf.info(path)
            buckets = 1200
            step = max(1, info.frames // buckets)
            peaks = []
            with _sf.SoundFile(path) as handle:
                while handle.tell() < info.frames:
                    data = handle.read(step, dtype="float32", always_2d=True)
                    peaks.append(float(_np.max(_np.abs(data))) if data.size else 0.0)
            payload = {"duration": round(info.frames / info.samplerate, 3),
                       "sr": info.samplerate, "peaks": peaks,
                       "spec": _spectrogram_payload(path)}
            self.previewPeaks.emit(_json.dumps(payload))
        except Exception as exc:
            self.previewFailed.emit(f"读取波形失败: {exc}")

    @Slot(str, float, float, "QVariantMap")
    def previewRender(self, path, start, end, params):
        if self._thread and self._thread.is_alive():
            self.previewFailed.emit("批处理进行中，预览暂不可用")
            return
        if self._preview_thread and self._preview_thread.is_alive():
            return
        p = Path(path)
        if not p.is_file() or not (0.0 <= start < end):
            self.previewFailed.emit("预览参数无效")
            return
        self._cancel_flag.clear()
        if not self._gpu_lock.acquire(blocking=False):
            self.previewFailed.emit("处理资源被占用，请稍后再试")
            return
        self._preview_thread = threading.Thread(
            target=self._preview_render_worker,
            args=(str(p), float(start), float(end), dict(params or {})),
            daemon=True)
        self._preview_thread.start()

    def _preview_render_worker(self, path, start, end, params):
        base = None
        try:
            import json as _json
            import shutil
            import tempfile

            import soundfile as _sf
            kwargs, notices = collect_pipeline_kwargs(params)
            base = Path(tempfile.mkdtemp(prefix="sb_preview_"))
            stem = Path(path).stem + f"_pv{int(round(start))}_{int(round(end))}"
            seg = base / (stem + ".wav")
            info = _sf.info(path)
            sr = info.samplerate
            with _sf.SoundFile(path) as handle:
                handle.seek(int(start * sr))
                data = handle.read(min(int((end - start) * sr),
                                       info.frames - int(start * sr)),
                                   always_2d=True)
            _sf.write(seg, data, sr, subtype="FLOAT")
            kwargs["work_dir"] = base / "work"

            def progress(stage_i, frac, label):
                self.previewProgress.emit(min(1.0, (stage_i + max(0.0, min(1.0, frac))) / 6.0))
                self.stageChanged.emit(stage_i, max(0.0, min(1.0, frac)), label)

            kwargs["progress"] = progress
            result = backend.run_pipeline(seg, base / "out", **kwargs)
            report = backend.read_quality_report(result)
            self.previewProgress.emit(0.95)
            spec_out = _spectrogram_payload(result)
            summary = backend.quality_summary(report) or {}
            previews = ROOT / "webview_storage" / "preview"
            previews.mkdir(parents=True, exist_ok=True)
            final = previews / Path(result).name
            shutil.move(str(result), str(final))
            payload = {"output": str(final), "quality": summary,
                       "mastering": report.get("mastering") or {},
                       "metrics": (report.get("output") or {}).get("metrics") or {},
                       "spec": spec_out, "notices": notices}
            self.previewDone.emit(_json.dumps(payload, ensure_ascii=False))
            self.previewProgress.emit(1.0)
        except Exception as exc:
            backend._tr(f"preview render failed: {exc!r}")
            self.previewFailed.emit(f"预览渲染失败: {exc}")
        finally:
            _preview_cleanup(base)
            if self._gpu_lock.locked():
                self._gpu_lock.release()

    @Slot(str, result=bool)
    def copyText(self, text):
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return clipboard.text() == text

    @Slot(str)
    def openFolder(self, path):
        import subprocess
        resolved = str(Path(path).resolve())
        if sys.platform == "darwin":
            # Finder 里定位该文件/目录（目录不存在时 open -R 回退打开父目录）
            subprocess.Popen(["open", "-R", resolved])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", resolved])
        else:
            subprocess.Popen(["xdg-open", resolved])

    @Slot()
    def help(self):
        self.logLine.emit(
            "链路: Lew 高频重建 → Demucs 四轨分离 → 贝斯增强 → 鼓增强 → 声场重塑 → Soren 母带。"
            "每个效果面板可独立开关（bypass）；旋钮双击复位；参考音频与流派二选一；处理中可点取消。", "")

    @Slot(result=str)
    def appVersion(self):
        """设置界面显示的应用版本号（单一来源：studio_backend.APP_VERSION）。"""
        return backend.APP_VERSION

    @Slot(str)
    def setNativeTheme(self, mode):
        """浅/深色模式切换时同步原生窗口底色与标题栏颜色。"""
        apply_native_theme(self._window, mode)

    # ── 后台管线 ──
    def _run_batch(self, params):
        try:
            backend._tr("_run_batch: start")
            files = list(params.get("files", []))
            if not files:
                self.failed.emit("未选择输入文件")
                return

            def progress(fi, ftotal, si, frac, label, fname):
                self.fileProgress.emit(fi, ftotal, fname)
                self.stageChanged.emit(si, frac, label)

            def file_finished(fi, ftotal, fname, succeeded, error):
                self.fileFinished.emit(fi, ftotal, fname, succeeded, error)


            def _stem_params(prefix):
                # 六轨 opt-in 可选分轨控制：UI 本阶段无新控件，参数存在时原样
                # 转发，缺省 0（中性）；范围校验在 run_pipeline 内做。
                return {f"{prefix}_{name}": float(params.get(f"{prefix}_{name}", 0.0) or 0.0)
                        for name in ("gain_db", "mud_cut_db", "presence_db",
                                     "harsh_cut_db", "width_db")}
            self.logLine.emit(f"── 开始批处理（{len(files)} 个文件）──", "")
            kwargs, mapping_notices = collect_pipeline_kwargs(params)
            for notice in mapping_notices:
                self.logLine.emit(notice, "")
            results = backend.run_batch(
                files, params["output"],
                progress=progress,
                file_finished=file_finished,
                cancel=lambda: self._cancel_flag.is_set(),
                **kwargs,
            )
            ok = sum(1 for _, _, err in results if err is None)
            fail = len(results) - ok
            err_lines = []
            for src, out, err in results:
                if err:
                    self.logLine.emit(f"✗ {Path(src).name}: {err}", "err")
                    err_lines.append(f"• {Path(src).name}: {err}")
                else:
                    self.logLine.emit(f"✓ {Path(src).name} → {out}", "ok")
                    try:
                        import json as _json
                        report = backend.read_quality_report(out)
                        summary = backend.quality_summary(report) or {}
                        # 发送完整报告（保留 output.metrics / mastering 嵌套结构）
                        # 再叠加摘要平铺字段：界面取数函数同时读两层
                        # （qualityMetric ← output.metrics，qualityMastering ←
                        # mastering）；把 output 覆盖成字符串路径会让采样峰值/
                        # 削波/目标响度全部退化为"无法判定"。
                        # 注意：report["input"] 是含 metrics 的对象，绝不能被
                        # 字符串路径覆盖（曾导致报告页"原始"值全部无法判定）。
                        payload = {**report, **summary,
                                   "filename": Path(src).name, "input_path": str(src)}
                        self.qualitySummary.emit(_json.dumps(payload, ensure_ascii=False))
                    except Exception:
                        pass
            self.logLine.emit(f"── 完成：成功 {ok}/{len(results)} ──", "")
            err_text = "\n".join(err_lines)
            self.done.emit(str(Path(params["output"]).resolve()), ok, fail, err_text)
        except backend.PipelineError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"内部错误: {e}\n{traceback.format_exc()[-400:]}")
        finally:
            if self._gpu_lock.locked():
                self._gpu_lock.release()


class StudioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShadowBuster — 击碎暗影 · 重塑声浪")
        self.setMinimumSize(*MIN_WINDOW)
        self._restore_or_default_geometry()
        # Windows 用 ico；macOS/Linux 优先 icns（红边新版设计，与 .app bundle
        # 图标同源，QIcon 可直接读取），无 icns 时回退 logo.png（旧版紫色设计）
        if os.name == "nt":
            icon_file = "logo.ico"
        else:
            icon_file = "logo.icns" if (ROOT / "ui" / "logo.icns").exists() else "logo.png"
        self.setWindowIcon(QIcon(str(ROOT / "ui" / icon_file)))

        self.view = DropAwareWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)

        # 显式使用命名 Profile 并开启持久存储，
        # 让前端 localStorage 在关闭后仍能保留参数。
        profile = QWebEngineProfile("ShadowBusterProfile", self.view)
        profile.setPersistentStoragePath(str(self._persistent_storage_dir()))
        page = QWebEnginePage(profile, self.view)
        self.view.setPage(page)

        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        # 预览视图用 <audio> 播放本地临时 WAV：file:// 页面需要显式允许文件互访。
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)

        self.channel = QWebChannel(self.view)
        self.bridge = Bridge(self)
        self.channel.registerObject("bridge", self.bridge)
        self.view.page().setWebChannel(self.channel)
        # OS 拖入文件 → 桥接信号 → 前端队列
        self.view.filesDropped.connect(self.bridge.filesDropped)
        self.view.filesDroppedAt.connect(self.bridge.routeDrop)
        self.view.dragHover.connect(self.bridge.dragHover)
        self.setCentralWidget(self.view)

        # 原生底色 + 标题栏跟随主题（默认深色；浅色由前端桥接触发切换）
        apply_native_theme(self, "dark")

        self.view.load(QUrl.fromLocalFile(str(UI_INDEX.resolve())))

    def _persistent_storage_dir(self):
        """WebView 持久存储目录。安装目录可写时随程序走（Windows 惯例）；
        macOS .app 只读且用户不应翻包内容，放 ~/Library/Application Support。"""
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            from PySide6.QtCore import QStandardPaths
            base = Path(QStandardPaths.writableLocation(
                QStandardPaths.AppDataLocation))
            (base / "webview_storage").mkdir(parents=True, exist_ok=True)
            return base / "webview_storage"
        return ROOT / "webview_storage"

    def _restore_or_default_geometry(self):
        """恢复上次窗口几何；首次启动按屏幕自适应默认尺寸并居中。

        默认高度取紧凑布局的内容高度，1080P 下不再占据近半屏；
        SB_WINDOW_GEOMETRY=WxH 仅供自动化视觉验证固定初始尺寸。
        """
        override = os.environ.get("SB_WINDOW_GEOMETRY", "").strip()
        if "x" in override:
            try:
                w, h = (int(v) for v in override.lower().split("x", 1))
                self.resize(max(w, MIN_WINDOW[0]), max(h, MIN_WINDOW[1]))
                return
            except ValueError:
                pass
        settings = QSettings("AngleNaris", "ShadowBuster")
        size = settings.value("window/size")
        pos = settings.value("window/pos")
        screen = self.screen().availableGeometry()
        if isinstance(size, QSize) and isinstance(pos, QPoint) \
                and any(s.availableGeometry().contains(pos) for s in QApplication.screens()):
            w = max(MIN_WINDOW[0], min(size.width(), screen.width()))
            h = max(MIN_WINDOW[1], min(size.height(), screen.height()))
            self.resize(w, h)
            self.move(pos)
            return
        w = max(MIN_WINDOW[0], min(DEFAULT_WINDOW[0], screen.width() - 32))
        h = max(MIN_WINDOW[1], min(DEFAULT_WINDOW[1], screen.height() - 32))
        self.resize(w, h)
        self.move(screen.center().x() - w // 2, screen.center().y() - h // 2)

    def closeEvent(self, event):
        settings = QSettings("AngleNaris", "ShadowBuster")
        settings.setValue("window/size", self.size())
        settings.setValue("window/pos", self.pos())
        # 只在关闭最后一个主窗口时才终止任务：任何原因产生的额外窗口
        # （系统重复激活、用户误开副本等）被关闭时，不应打断仍在进行的批处理
        others = [w for w in QApplication.topLevelWidgets()
                  if isinstance(w, StudioWindow) and w is not self]
        if not any(o.isVisible() for o in others):
            self.bridge.cancel()
            backend.terminate_all()   # 兜底：杀掉仍在跑的推理子进程树
        event.accept()


if sys.platform == "darwin":
    try:
        import objc
        from AppKit import NSCompositingOperationSourceOver, NSView
        from Foundation import NSMakeRect, NSZeroRect

        class _FillIconView(NSView):
            """按当前 bounds 绘制图标：内容占 ~80.5%（与其他应用图标的
            824/1024 模板内容区一致），Dock 任意缩放磁贴都自动适配。"""
            image = None

            def drawRect_(self, rect):
                if self.image is None:
                    return
                b = self.bounds()
                inset = b.size.width * 0.0975   # (1024-824)/2/1024，对齐系统模板边距
                self.image.drawInRect_fromRect_operation_fraction_(
                    NSMakeRect(b.origin.x + inset, b.origin.y + inset,
                               b.size.width - 2 * inset, b.size.height - 2 * inset),
                    NSZeroRect, NSCompositingOperationSourceOver, 1.0)
    except Exception:   # pyobjc 不可用时保持 _FillIconView 未定义，走回退
        _FillIconView = None
else:
    _FillIconView = None


def apply_custom_dock_icon(window):
    """macOS 26 (Tahoe) 把 .icns 静态图标强制模板化：满幅方形裁系统圆角，
    带透明边角则垫玻璃底板，且无任何开关。经 NSDockTile.contentView 放入
    自定义 NSView 可绕过模板——Dock 按原样绘制直角图标（见
    simonbs.dev "How To Bring Back Oddly Shaped App Icons on macOS 26 Tahoe"）。
    需要 pyobjc-framework-Cocoa；缺失或失败时静默回退系统模板化图标。"""
    if _FillIconView is None:
        return
    try:
        from Foundation import NSApplication, NSImage
        icon_path = ROOT / "ui" / "logo.png"   # 1024px 红边直角设计
        image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
        if image is None:
            backend._tr("dock tile: icon load failed")
            return
        tile = NSApplication.sharedApplication().dockTile()
        w = tile.size().width or 128
        h = tile.size().height or 128
        view = _FillIconView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        view.image = image
        tile.setContentView_(view)
        tile.display()
        backend._tr(f"dock tile: custom sharp-corner icon applied (tile {w}x{h})")
    except Exception as e:
        backend._tr(f"dock tile: override failed {e!r}")


_SINGLE_INSTANCE_KEY = "ShadowBuster.SingleInstance"


def _another_instance_running():
    """单实例检测：尝试连接已有实例的命名服务。连上即说明已有实例在跑，
    给它发一个 raise 消息让它把主窗口带到前台，然后调用方退出本进程。"""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(_SINGLE_INSTANCE_KEY)
    connected = sock.waitForConnected(300)
    if connected:
        sock.write(b"raise\n")
        sock.flush()
        # 对端收到 raise 后可能已断开，避免对已断开套接字调用 waitForBytesWritten
        if sock.state() == QLocalSocket.ConnectedState:
            sock.waitForBytesWritten(500)
    # 收尾必须优雅关闭并等真正断开：abort() 会直接丢弃写缓冲里未发出的数据；
    # Windows 的异步写还要靠本进程事件循环转圈才能完成排空——只
    # waitForDisconnected 会停在 ClosingState，socket 析构时消息随之丢失。
    sock.disconnectFromServer()
    for _ in range(100):
        if sock.state() == QLocalSocket.UnconnectedState:
            break
        QCoreApplication.processEvents()
        sock.waitForDisconnected(20)
    return connected


def _activate_existing_window(server, window):
    """收到二次启动实例的 raise 消息：把主窗口带到前台。"""
    conn = server.nextPendingConnection()
    if conn is None:
        return
    conn.readAll()
    conn.disconnectFromServer()
    window.show()
    window.raise_()
    window.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ShadowBuster")

    # 单实例守卫：DMG 副本 / Applications 副本 / 快速双击都可能产生第二个
    # 实例（macOS 不会按 bundle id 去重不同路径的启动），两个一模一样的窗口
    # 会让用户混淆窗口归属；二次启动一律唤起已有窗口后退出。
    from PySide6.QtNetwork import QLocalServer
    if _another_instance_running():
        return 0
    QLocalServer.removeServer(_SINGLE_INSTANCE_KEY)   # 清理崩溃残留的套接字
    server = QLocalServer()
    server.listen(_SINGLE_INSTANCE_KEY)

    win = StudioWindow()
    win.show()
    apply_custom_dock_icon(win)
    server.newConnection.connect(
        lambda: _activate_existing_window(server, win))
    ret = app.exec()
    server.close()
    return ret


if __name__ == "__main__":
    sys.exit(main())
