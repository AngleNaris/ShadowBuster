"""ShadowBuster — 母带工坊（Qt WebView 壳）
PySide6 + QWebEngineView + QWebChannel，加载 ui/index.html（KFL 合规 DAW 插件界面）。
管线在后台线程执行，进度经 Signal 推送到前端。
"""
import os
import sys
import threading
import traceback
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

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._cancel_flag = threading.Event()
        self._thread = None
        self._gpu_cancel = threading.Event()
        self._gpu_thread = None

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
        threading.Thread(target=self._gpu_check_worker, daemon=True).start()

    def _gpu_check_worker(self):
        try:
            if not getattr(sys, "frozen", False):
                self._gpu_emit({"type": "state", "dev": True})
                return
            import gpu_env as ge
            payload = {"type": "state", "installed": ge.installed_info(),
                       "manifest": None, "error": None}
            try:
                m = ge.load_manifest(backend.APP_VERSION)
                payload["manifest"] = {
                    "version": m["version"],
                    "totalSize": m["totalSize"],
                    "sha256": m["sha256"],
                    "parts": [{"name": p["name"], "size": p["size"]} for p in m["parts"]],
                }
            except Exception as e:
                payload["error"] = str(e)[:200]
            self._gpu_emit(payload)
        except Exception as e:   # 兜底
            self._gpu_emit({"type": "error", "msg": f"内部错误: {e}"[:200]})

    @Slot(result=bool)
    def startGpuInstall(self):
        """启动 GPU 环境下载安装（后台线程，防重入）。"""
        if self._gpu_thread and self._gpu_thread.is_alive():
            return False
        self._gpu_cancel.clear()
        self._gpu_thread = threading.Thread(target=self._gpu_install_worker, daemon=True)
        self._gpu_thread.start()
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

        def prog(phase, cur, total):
            t = _time.monotonic()
            if t - state["last"][0] >= 0.2:
                state["last"][0] = t
                self._gpu_emit({"type": "progress", "phase": phase,
                                "cur": int(cur), "total": int(total)})

        try:
            self._gpu_emit({"type": "progress", "phase": "info", "cur": 0, "total": 1})
            m = ge.load_manifest(backend.APP_VERSION)
            total = m["totalSize"]
            # 磁盘预检：下载期存在 分卷 + 组装 zip 的峰值（约 2 倍包体积）
            free = _shutil.disk_usage(ge.user_base_dir()).free
            if free < total * 2 + (1 << 30):
                raise RuntimeError(f"磁盘空间不足：需要约 {max(1, (total*2 + (1<<30)) >> 30)} GB，"
                                   f"当前可用 {free >> 30} GB")
            work = ge.dl_dir(m["version"])
            work.mkdir(parents=True, exist_ok=True)
            base = 0
            parts = []
            n = len(m["parts"])
            for i, p in enumerate(m["parts"], 1):
                if cancel.is_set():
                    raise ge.DownloadCancelled("下载已取消")
                self._gpu_emit({"type": "progress", "phase": "download",
                                "cur": base, "total": total,
                                "part": f"{i}/{n}"})
                dest = work / p["name"]
                ge.download_part(p["url"], dest, p["size"],
                                 cancel=lambda: cancel.is_set(),
                                 progress=lambda off, b=base: prog("download", b + off, total))
                prog("verify", base, total)   # 单卷校验（几秒）
                sha = ge.sha256_of(dest, cancel=lambda: cancel.is_set())
                if sha != p["sha256"]:
                    raise RuntimeError(f"分卷 {p['name']} SHA-256 校验失败，请重新下载")
                base += p["size"]
                parts.append({**p, "local": str(dest)})
            zpath = work / f"gpu-env-{m['version']}.zip"
            prog("assemble", 0, total)
            ge.assemble_zip(parts, zpath, cancel=lambda: cancel.is_set(),
                            progress=lambda c, t: prog("assemble", c, t))
            zsize = zpath.stat().st_size
            prog("verify", 0, zsize)
            h = ge.sha256_of(zpath, cancel=lambda: cancel.is_set(),
                             progress=lambda c, t: prog("verify", c, t))
            if h != m["sha256"]:
                raise RuntimeError("GPU 环境包 SHA-256 校验失败，请重新下载")
            for p in parts:
                _P(p["local"]).unlink(missing_ok=True)   # 校验通过才释放分卷
            # 解压空间预检（此时能拿到真实解压体积）
            with _zipfile.ZipFile(zpath) as zf:
                extracted = sum(i.file_size for i in zf.infolist() if not i.is_dir())
            free = _shutil.disk_usage(ge.user_base_dir()).free
            if free < extracted + (1 << 30):
                raise RuntimeError(f"解压空间不足：需要约 {max(1, (extracted + (1<<30)) >> 30)} GB，"
                                   f"当前可用 {free >> 30} GB，请清理后重试")
            staging = ge.runner_dir() / ".staging"
            _shutil.rmtree(staging, ignore_errors=True)
            prog("extract", 0, extracted)
            ge.extract_zip(zpath, staging, cancel=lambda: cancel.is_set(),
                           progress=lambda c, t: prog("extract", c, t))
            prog("verify", 0, 1)
            ge.validate_runtime(staging / "python.exe")
            ge.swap_env(staging, m["version"], m["sha256"], runtime_validated=True)
            zpath.unlink(missing_ok=True)
            self._gpu_emit({"type": "done", "version": m["version"]})
        except ge.DownloadCancelled:
            self._gpu_emit({"type": "cancelled"})
        except Exception as e:
            self._gpu_emit({"type": "error", "msg": str(e)[:300]})

    @Slot()
    def probeDevice(self):
        """探测推理设备（子进程），结果经 gpuStatus 回传。"""
        threading.Thread(target=self._probe_worker, daemon=True).start()

    def _probe_worker(self):
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
        self._cancel_flag.clear()
        p = dict(params or {})
        self._thread = threading.Thread(
            target=self._run_batch, args=(p,), daemon=True)
        self._thread.start()
        return True

    @Slot()
    def cancel(self):
        self._cancel_flag.set()

    @Slot(str)
    def openFolder(self, path):
        import subprocess
        subprocess.Popen(["explorer", "/select,", str(Path(path).resolve())])

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

            # 旋钮映射：Sub 直接等位传递（前端 0–12 即 0–12 dB，刻度不被放大）；
            # sat / trans / denoise 是 0–10 的比例档，折算到 0–1；punch 直接等位传递 0–10 dB；
            # space（声场干湿比）同样 0–10 折算 0–1；space_width 为宽度上限 0–12 档折算 0–6 dB；
            # vocal 为人声增益 -12~12 档折算 -6~+6 dB（0 = 直通）。
            sub_db = float(params.get("sub", 6))            # 0–12 dB（数值 = 显示）
            sat = float(params.get("sat", 3)) / 10.0        # 0–1.0
            punch_db = float(params.get("punch", 2))        # 0–10 dB（数值 = 显示）
            trans = float(params.get("trans", 3)) / 10.0    # 0–1.0
            space_wet = float(params.get("space", 6)) / 10.0      # 0–1.0，默认 0.6
            space_denoise = float(params.get("denoise", 2)) / 10.0  # 0–1.0，默认 0.2
            space_width_db = float(params.get("space_width", 12)) / 2.0  # 0–12 dB，默认 6.0
            vocal_gain_db = float(params.get("vocal", 0)) / 2.0   # -6~+6 dB，默认 0
            # bypass：面板开关关闭的阶段名集合
            bypass = [b for b in (params.get("bypass") or []) if b]
            bypass = [b for b in bypass
                      if b in ("lew", "vocals", "bass", "drums", "reshape", "soren")]
            self.logLine.emit(f"── 开始批处理（{len(files)} 个文件）──", "")
            results = backend.run_batch(
                files, params["output"],
                sub_db=sub_db,
                sat=sat,
                punch_db=punch_db,
                trans=trans,
                space_wet=space_wet,
                space_denoise=space_denoise,
                space_width_db=space_width_db,
                vocal_gain_db=vocal_gain_db,
                bypass=bypass,
                quality=int(params.get("quality", 1)),
                guidance=float(params.get("guidance", 1.5)),
                genre=params.get("genre", "Pop"),
                loudness=params.get("loudness", "normal"),
                eq_profile=params.get("eq", "Neutral"),
                reference=params.get("reference") or None,
                device=backend.auto_device(),
                progress=progress,
                file_finished=file_finished,
                cancel=lambda: self._cancel_flag.is_set(),
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
            self.logLine.emit(f"── 完成：成功 {ok}/{len(results)} ──", "")
            err_text = "\n".join(err_lines)
            self.done.emit(str(Path(params["output"]).resolve()), ok, fail, err_text)
        except backend.PipelineError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"内部错误: {e}\n{traceback.format_exc()[-400:]}")


class StudioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ShadowBuster — 击碎暗影 · 重塑声浪")
        self.setMinimumSize(*MIN_WINDOW)
        self._restore_or_default_geometry()
        self.setWindowIcon(QIcon(str(ROOT / "ui" / "logo.ico")))

        self.view = DropAwareWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)

        # 显式使用命名 Profile 并开启持久存储，
        # 让前端 localStorage 在关闭后仍能保留参数。
        profile = QWebEngineProfile("ShadowBusterProfile", self.view)
        profile.setPersistentStoragePath(str(ROOT / "webview_storage"))
        page = QWebEnginePage(profile, self.view)
        self.view.setPage(page)

        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
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
        self.bridge.cancel()
        backend.terminate_all()   # 兜底：杀掉仍在跑的推理子进程树
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ShadowBuster")
    win = StudioWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
