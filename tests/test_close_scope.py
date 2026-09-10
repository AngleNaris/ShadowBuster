"""closeEvent 任务终止作用域：只有最后一个主窗口关闭才打断批处理。"""
from types import SimpleNamespace

from PySide6.QtCore import QSize, QPoint

import main


class FakeStudio:
    """closeEvent 里 isinstance(w, StudioWindow) 的替身：
    测试时把 main.StudioWindow monkeypatch 成本类。"""

    def __init__(self, visible=True):
        self._visible = visible

    def isVisible(self):
        return self._visible


def run_close_event(monkeypatch, other_windows):
    """以 other_windows 作为 topLevelWidgets，对替身 self 执行真实的
    StudioWindow.closeEvent，返回 (bridge, terminated)。"""
    real_close = main.StudioWindow.closeEvent   # 先取真实函数再打补丁
    bridge = SimpleNamespace(cancel=lambda: bridge.__setattr__("cancelled", True))
    bridge.cancelled = False
    terminated = {"v": False}
    fake_self = SimpleNamespace(
        size=lambda: QSize(1000, 860),
        pos=lambda: QPoint(0, 0),
        bridge=bridge,
    )
    event = SimpleNamespace(accept=lambda: None)

    monkeypatch.setattr(main, "StudioWindow", FakeStudio)
    monkeypatch.setattr(main.QApplication, "topLevelWidgets",
                        staticmethod(lambda: list(other_windows) + [fake_self]))
    monkeypatch.setattr(main, "QSettings",
                        lambda *a, **k: SimpleNamespace(setValue=lambda *a2: None))
    monkeypatch.setattr(main.backend, "terminate_all",
                        lambda: terminated.__setitem__("v", True))

    real_close(fake_self, event)
    return bridge, terminated["v"]


def test_closing_extra_window_keeps_task(monkeypatch):
    """还存在其他可见主窗口时关闭当前窗口：不取消、不杀任务。"""
    bridge, terminated = run_close_event(monkeypatch, [FakeStudio(visible=True)])
    assert bridge.cancelled is False
    assert terminated is False


def test_closing_last_window_stops_task(monkeypatch):
    """关闭最后一个主窗口（其余已隐藏）：取消任务并杀子进程树。"""
    bridge, terminated = run_close_event(monkeypatch, [FakeStudio(visible=False)])
    assert bridge.cancelled is True
    assert terminated is True


def test_closing_only_window_stops_task(monkeypatch):
    """单窗口（正常使用）关闭：取消任务并杀子进程树。"""
    bridge, terminated = run_close_event(monkeypatch, [])
    assert bridge.cancelled is True
    assert terminated is True
