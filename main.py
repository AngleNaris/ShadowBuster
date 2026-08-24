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

from PySide6.QtCore import QObject, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile, QWebEnginePage

import studio_backend as backend

ROOT = Path(__file__).parent
UI_INDEX = ROOT / "ui" / "index.html"

AUDIO_FILTER = "音频文件 (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;所有文件 (*.*)"


class Bridge(QObject):
    """前端 JS 与 Python 的桥接对象。"""

    stageChanged = Signal(int, float, str)   # stage idx, frac, label
    fileProgress = Signal(int, int, str)     # file idx (0-based), total, file name
    logLine = Signal(str, str)               # text, css class
    done = Signal(str, int, int, str)        # 输出路径, 成功数, 失败数, 失败详情
    failed = Signal(str)                     # 错误消息

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._cancel_flag = threading.Event()
        self._thread = None

    # ── JS 可调用的方法 ──
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
            "链路: Lew 高频重建 → Demucs 贝斯分离 → 贝斯增强 → Soren 母带。"
            "旋钮双击复位；参考音频与流派二选一；处理中可点取消。", "")

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

            # 旋钮映射：Sub 直接等位传递（前端 0–12 即 0–12 dB，刻度不被放大）；
            # sat / trans 是 0–10 的比例档，折算到 0–1；punch 直接等位传递 0–10 dB。
            sub_db = float(params.get("sub", 6))            # 0–12 dB（数值 = 显示）
            sat = float(params.get("sat", 3)) / 10.0        # 0–1.0
            punch_db = float(params.get("punch", 2))        # 0–10 dB（数值 = 显示）
            trans = float(params.get("trans", 3)) / 10.0    # 0–1.0
            self.logLine.emit(f"── 开始批处理（{len(files)} 个文件）──", "")
            results = backend.run_batch(
                files, params["output"],
                sub_db=sub_db,
                sat=sat,
                punch_db=punch_db,
                trans=trans,
                quality=int(params.get("quality", 1)),
                guidance=float(params.get("guidance", 1.5)),
                genre=params.get("genre", "Pop"),
                loudness=params.get("loudness", "normal"),
                eq_profile=params.get("eq", "Neutral"),
                reference=params.get("reference") or None,
                device=backend.auto_device(),
                progress=progress,
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
        self.resize(1020, 820)
        self.setFixedSize(1020, 820)
        self.setWindowIcon(QIcon(str(ROOT / "ui" / "logo.ico")))

        # 暗色窗口底色
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("#141218"))
        self.setPalette(pal)

        self.view = QWebEngineView(self)
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
        self.setCentralWidget(self.view)

        self.view.load(QUrl.fromLocalFile(str(UI_INDEX.resolve())))

    def closeEvent(self, event):
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
