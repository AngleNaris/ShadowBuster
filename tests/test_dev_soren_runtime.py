"""dev Soren 路由统一性测试（不触碰外部仓库内容、不改 DSP）。

原则：
- 一切可能写入的 build/升级测试只用 tmp_path fixture，绝不写真实 dev runtime；
- 对真实 dev runtime 只做只读一致性检查（缺失则 skip，可选）；
- 证明开发链路使用 canonical（而非外部旧稿）、资源路由正确、打包路由不变；
- 延迟 ensure：import/路由不依赖外部资源，真正执行 Soren 才报清晰错误。
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANON_CORE = ROOT / "packaging/soren_core.py"
CANON_ORIGINAL = ROOT / "packaging/soren_original.py"
DEV_RUNTIME = ROOT / "dev_runtime/Soren_src"
EXTERNAL = Path(r"D:/_3.AI/audio_upscale/Soren_src")
_MANAGED_FILES = ("core_decrypted.py", "soren_original.py", "test_model.py",
                  "dev_runtime_manifest.json")

skip_no_dev_runtime = pytest.mark.skipif(
    not (DEV_RUNTIME / "core_decrypted.py").is_file(),
    reason="dev runtime 未生成：python tools/make_dev_runtime.py")


def _load_tool():
    import sys
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_dev_runtime_test", ROOT / "tools/make_dev_runtime.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_owned_fixture(tmp_path, monkeypatch):
    tool = _load_tool()
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in tool.RESOURCE_DIRS:
        (sources / name).mkdir()
    (sources / "test_model.py").write_text("# fixture\n", encoding="utf-8")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    code = {}
    for name in tool.MANAGED_CODE:
        code[name] = canonical / name
        code[name].write_text("# canonical fixture\n", encoding="utf-8")
    monkeypatch.setattr(tool, "MANAGED_CODE", code)
    monkeypatch.setenv("SB_SOREN", str(sources))
    target = tmp_path / "Soren_src"
    tool.build(target=target)
    return tool, target


# ── import / 路由（延迟 ensure：不依赖任何外部资源）───────────────────────

def test_imported_backend_uses_canonical_dev_runtime():
    import studio_backend as backend
    assert backend.SOREN_DIR == DEV_RUNTIME   # 惰性：import 不 ensure、不读外部
    if not (DEV_RUNTIME / "core_decrypted.py").is_file():
        pytest.skip("dev runtime 未生成：python tools/make_dev_runtime.py")
    assert (DEV_RUNTIME / "core_decrypted.py").read_bytes() == CANON_CORE.read_bytes()
    assert (DEV_RUNTIME / "soren_original.py").read_bytes() == CANON_ORIGINAL.read_bytes()


def test_import_and_routing_work_without_resources(monkeypatch, tmp_path):
    """无 dev runtime / 外部资源：import 与 _resolve_runtime 仍可用。"""
    import studio_backend as backend
    fake = tmp_path / "absent" / "Soren_src"
    monkeypatch.setattr(backend, "DEV_SOREN_RUNTIME", fake)
    monkeypatch.delenv("SB_SOREN", raising=False)
    python, apollo, soren, assets = backend._resolve_runtime()
    assert soren == fake
    assert assets is None


def test_stage_soren_errors_clearly_without_resources(monkeypatch, tmp_path):
    """真正执行 Soren 时才 ensure；资源缺失 → 指向工具的清晰报错。"""
    import studio_backend as backend
    monkeypatch.setattr(backend, "DEV_SOREN_RUNTIME", tmp_path / "absent")
    empty_source = tmp_path / "empty_source"
    empty_source.mkdir()
    monkeypatch.setenv("SB_SOREN", str(empty_source))   # 资源源无效
    with pytest.raises(RuntimeError, match="make_dev_runtime"):
        backend.stage_soren("in.wav", "out.wav")


def test_sb_soren_is_resource_source_only(monkeypatch, tmp_path):
    """SB_SOREN 只是资源源，绝不作为执行目录（防止跑到外部旧 DSP）。"""
    import studio_backend as backend
    monkeypatch.setenv("SB_SOREN", str(tmp_path / "elsewhere"))
    assert backend._dev_soren_dir() == backend.DEV_SOREN_RUNTIME


def test_packaged_routing_unchanged(monkeypatch, tmp_path):
    """SB_ASSETS 打包布局分支优先于 dev 回退；dev runtime 不干扰打包路由。"""
    import studio_backend as backend
    assets = tmp_path / "assets"
    (assets / "Apollo").mkdir(parents=True)
    (assets / "Soren_src").mkdir()
    (assets / "Soren_src" / "core_decrypted.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("SB_ASSETS", str(assets))
    monkeypatch.delenv("SB_SOREN", raising=False)
    python, apollo, soren, root = backend._resolve_runtime()
    assert root == assets
    assert soren == assets / "Soren_src"


def test_packaged_stage_soren_bypasses_dev_tool(monkeypatch, tmp_path):
    """打包态（ASSETS 已解析）真实调用 stage_soren：不得触发 dev runtime 工具。"""
    import studio_backend as backend
    fake_runtime = tmp_path / "runtime" / "Soren_src"
    fake_runtime.mkdir(parents=True)
    (fake_runtime / "core_decrypted.py").write_text("# packaged stub\n", encoding="utf-8")
    monkeypatch.setattr(backend, "ASSETS", tmp_path / "runtime")
    monkeypatch.setattr(backend, "SOREN_DIR", fake_runtime)
    calls = []
    monkeypatch.setattr(backend, "_run_stream", lambda cmd, *a, **k: calls.append(cmd))

    def _boom():
        raise AssertionError("打包态不得调用 dev runtime 工具")

    monkeypatch.setattr(backend, "_load_dev_runtime_tool", _boom)
    backend.stage_soren("in.wav", "out.wav")
    assert len(calls) == 1 and calls[0][1] == str(fake_runtime / "core_decrypted.py")


# ── 真实 dev runtime：只读一致性检查（可选）────────────────────────────

@skip_no_dev_runtime
def test_dev_runtime_code_is_canonical():
    """只读：dev runtime 代码与 packaging 权威逐字节一致，manifest(v2) 记录吻合。"""
    core_bytes = (DEV_RUNTIME / "core_decrypted.py").read_bytes()
    original_bytes = (DEV_RUNTIME / "soren_original.py").read_bytes()
    assert core_bytes == CANON_CORE.read_bytes()
    assert original_bytes == CANON_ORIGINAL.read_bytes()
    manifest = json.loads((DEV_RUNTIME / "dev_runtime_manifest.json").read_text(encoding="utf-8"))
    managed = manifest["managed"]
    assert managed["core_decrypted.py"]["sha256"] == hashlib.sha256(core_bytes).hexdigest()
    assert managed["soren_original.py"]["sha256"] == hashlib.sha256(original_bytes).hexdigest()


@skip_no_dev_runtime
def test_dev_runtime_never_serves_external_stale_original():
    """只读：若外部旧稿与 canonical 不同，dev runtime 必须与外部不同。"""
    external = EXTERNAL / "soren_original.py"
    if external.is_file() and external.read_bytes() != CANON_ORIGINAL.read_bytes():
        assert (DEV_RUNTIME / "soren_original.py").read_bytes() != external.read_bytes()


@skip_no_dev_runtime
def test_dev_runtime_resources_resolved():
    """只读：资源目录与 test_model.py 就位。"""
    for name in ("model", "profiles", "secured_genres"):
        assert (DEV_RUNTIME / name).is_dir(), name
    assert (DEV_RUNTIME / "test_model.py").is_file()


@skip_no_dev_runtime
def test_dev_runtime_resources_match_manifest_source():
    """只读：资源目录来自 manifest 记录的 SB_SOREN 源（junction 用 realpath 证明）。"""
    data = json.loads((DEV_RUNTIME / "dev_runtime_manifest.json").read_text(encoding="utf-8"))
    source = Path(data["resources_source"])
    assert source.is_dir()
    for name in ("model", "profiles", "secured_genres"):
        assert (DEV_RUNTIME / name).is_dir(), name
        if data.get("resource_mode", {}).get(name) == "junction":
            assert Path(os.path.realpath(DEV_RUNTIME / name)) == \
                Path(os.path.realpath(source / name))


# ── build/升级语义：全部在 tmp fixture 上验证，绝不写真实 dev runtime ───

def test_rebuild_keeps_unknown_entries(tmp_path, monkeypatch):
    tool, target = _make_owned_fixture(tmp_path, monkeypatch)
    (target / "unknown_extra.txt").write_text("keep me", encoding="utf-8")
    tool.build(target=target)
    assert (target / "unknown_extra.txt").read_text(encoding="utf-8") == "keep me"


def test_rebuild_refuses_user_modified_owned_file(tmp_path, monkeypatch):
    tool, target = _make_owned_fixture(tmp_path, monkeypatch)
    owned = target / "soren_original.py"
    data = owned.read_bytes()
    owned.write_bytes(data + b"\n# local edit\n")
    with pytest.raises(RuntimeError):
        tool.build(target=target)
    assert owned.read_bytes() == data + b"\n# local edit\n"


def test_ensure_is_stable_and_detects_canonical_changes(tmp_path, monkeypatch):
    tool, target = _make_owned_fixture(tmp_path, monkeypatch)
    manifest = (target / tool.MANIFEST_NAME).read_bytes()
    assert tool.validate(target)[0] == "ok"
    assert tool.ensure(target) == target
    assert (target / tool.MANIFEST_NAME).read_bytes() == manifest
    source = tool.MANAGED_CODE["core_decrypted.py"]
    source.write_text("# revised canonical\n", encoding="utf-8")
    assert tool.validate(target)[0] == "stale"
    tool.ensure(target)
    assert (target / "core_decrypted.py").read_bytes() == source.read_bytes()
    assert tool.validate(target)[0] == "ok"


def test_ensure_rejects_unowned_directory(monkeypatch, tmp_path):
    """目标非空且无 owned manifest → ensure 拒绝且绝不删除未知内容。"""
    import studio_backend as backend
    fake = tmp_path / "Soren_src"
    fake.mkdir()
    (fake / "mystery.txt").write_text("user data", encoding="utf-8")
    monkeypatch.setattr(backend, "DEV_SOREN_RUNTIME", fake)
    with pytest.raises(RuntimeError):
        backend._ensure_dev_runtime()
    assert (fake / "mystery.txt").read_text(encoding="utf-8") == "user data"
