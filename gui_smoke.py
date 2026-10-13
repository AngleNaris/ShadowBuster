"""Exercise the frozen GUI, including the WebEngine renderer and bundled UI."""
import json
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, qVersion
from PySide6.QtWidgets import QApplication


def run(window_factory, report_path, version):
    app = QApplication(["ShadowBuster GUI check"])
    app.setApplicationName("ShadowBuster")
    settings = QSettings("AngleNaris", "ShadowBuster")
    geometry = {key: settings.value(key) for key in ("window/size", "window/pos")}
    window = window_factory()
    finished = False

    def finish(ok, page):
        nonlocal finished
        if finished:
            return
        finished = True
        report = {"status": "pass" if ok else "fail", "version": version,
                  "qt_version": qVersion(), "page": page,
                  "bridge_version": window.bridge.appVersion()}
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        app.exit(0 if ok else 1)

    def inspect_page(raw):
        page = json.loads(raw) if raw else {}
        ok = (isinstance(page, dict) and page.get("title") == "ShadowBuster"
              and page.get("readyState") == "complete" and page.get("elements", 0) > 0
              and page.get("bridgeReady") is True)
        finish(ok, page)

    def loaded(ok):
        if not ok:
            finish(False, {"error": "UI failed to load"})
            return
        QTimer.singleShot(500, lambda: window.view.page().runJavaScript(
            "JSON.stringify({title:document.title,readyState:document.readyState,"
            "elements:document.body.querySelectorAll('*').length,"
            "bridgeReady:!!window.pyApi && typeof window.pyApi.appVersion==='function'})", inspect_page))

    window.view.loadFinished.connect(loaded)
    QTimer.singleShot(30000, lambda: finish(False, {"error": "GUI startup timed out"}))
    window.show()
    try:
        return app.exec()
    finally:
        window.hide()
        for key, value in geometry.items():
            if value is None:
                settings.remove(key)
            else:
                settings.setValue(key, value)
        settings.sync()
