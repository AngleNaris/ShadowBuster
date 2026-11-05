"""「密度」从界面到母带子进程的连线：UI 分段 → GUI 映射 → 后端命令 / CLI。"""
from pathlib import Path

import pytest

import processing_cli
import studio_backend as backend
from mastering.density import DENSITY_MODES

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")


def test_ui_offers_every_density_mode_with_light_default():
    for mode in DENSITY_MODES:
        assert f'data-density="{mode}"' in HTML
    assert 'data-density="light" aria-pressed="true"' in HTML
    assert 'loadValue("density", "light")' in APP
    # 进入处理参数与预设快照
    assert "density: getDensity()" in APP and "density: getDensity," in APP
    assert "density: setDensity," in APP


@pytest.mark.parametrize("argv, expected", [([], "light"), (["--density", "off"], "off")])
def test_cli_forwards_density(tmp_path, monkeypatch, argv, expected):
    assert backend.DEFAULTS["density"] == "light"
    seen = {}
    monkeypatch.setattr(backend, "run_batch", lambda inputs, out, **kw: seen.update(kw) or [])
    src = tmp_path / "a.wav"
    src.write_bytes(b"")
    processing_cli.main(["-i", str(src), "-o", str(tmp_path / "out")] + argv)
    assert seen["density"] == expected


@pytest.mark.parametrize("mode", DENSITY_MODES)
def test_independent_mastering_command_carries_density(monkeypatch, mode):
    calls = []
    monkeypatch.setattr(backend, "_run_stream", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(backend, "read_mastering_stats", lambda path: {})
    backend.stage_soren("in.wav", "out.wav", style_mode="off", density=mode)
    cmd = calls[-1]
    assert cmd[cmd.index("--density") + 1] == mode


def test_gui_mapping_forwards_density():
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    import main as app_main
    assert app_main.collect_pipeline_kwargs({})[0]["density"] == "light"
    assert app_main.collect_pipeline_kwargs({"density": "standard"})[0]["density"] == "standard"


def test_packaged_mastering_uses_bundled_runtime_not_gpu_env(tmp_path, monkeypatch):
    """GPU 环境只替换 PYTHON；母带子进程须用安装包内置运行时（含 pedalboard）。"""
    bundled = tmp_path / "env" / "python.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"")
    monkeypatch.setattr(backend, "ASSETS", tmp_path)
    monkeypatch.setattr(backend, "PYTHON", Path("C:/gpu-env/python.exe"))
    calls = []
    monkeypatch.setattr(backend, "_run_stream", lambda cmd, *a, **k: calls.append(cmd))
    monkeypatch.setattr(backend, "read_mastering_stats", lambda path: {})
    backend.stage_soren("in.wav", "out.wav", style_mode="off")
    assert Path(calls[-1][0]) == bundled
    monkeypatch.setattr(backend, "ASSETS", None)
    backend.stage_soren("in.wav", "out.wav", style_mode="off")
    assert Path(calls[-1][0]) == Path("C:/gpu-env/python.exe")
