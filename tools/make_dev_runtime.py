"""生成/维护开发态 canonical Soren 运行时：dev_runtime/Soren_src。

开发缺口：开发回退此前直接指向外部仓库 D:/_3.AI/audio_upscale/Soren_src，
其中 soren_original.py 是滞后的未跟踪旧稿，与打包权威
（packaging/soren_core.py、packaging/soren_original.py，经
packaging/runtime_sync.ps1 同步进 stage/安装包）行为不一致。

统一路由（studio_backend._dev_soren_dir → 本模块 ensure）：
  dev_runtime/Soren_src（canonical，缺失/陈旧/资源源变化时安全重建）
SB_SOREN 仅作为资源源（本脚本与 packaging/runtime_sync.ps1），绝不作为执行目录。

安全规则（重要）：
- 目标目录非空且无 owned manifest（或 manifest 键结构不符）→ 直接拒绝，绝不删除任何未知内容；
- 只覆盖 manifest 登记的 owned 文件，且当前哈希必须等于生成期哈希（用户改动 → 拒绝，
  --force 才允许且先备份）；
- 资源目录只保留或重建 junction 链接本身；copy 退化产物在资源源未变时原样保留，
  资源源变化时拒绝（未登记文件级哈希，不做 rmtree）；任何真实目录内容一律不删；
- 写入前完整预检（canonical/资源源/全部计划动作），通过后才执行；代码文件用
  临时文件 + os.replace 原子写入；被替换内容先备份到 dev_runtime/.backups/<UTC>/；
- SB_SOREN（资源根）与 manifest 不一致 → 判定 stale 并安全重建链接/资源文件；
- verify 无 manifest / manifest 损坏 / 键结构不符即失败。

用法：
    python tools/make_dev_runtime.py           # 生成/安全升级（幂等）
    python tools/make_dev_runtime.py --verify  # 仅校验
    python tools/make_dev_runtime.py --force   # 允许覆盖被改动的 owned 文件（先备份）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV_RUNTIME = ROOT / "dev_runtime" / "Soren_src"
MANAGED_CODE = {
    "core_decrypted.py": ROOT / "packaging" / "soren_core.py",
    "soren_original.py": ROOT / "packaging" / "soren_original.py",
}
MANAGED_RESOURCE_FILE = "test_model.py"
EXPECTED_MANAGED = frozenset(MANAGED_CODE) | {MANAGED_RESOURCE_FILE}
RESOURCE_DIRS = ("model", "profiles", "secured_genres")
EXTERNAL_SOREN = Path(r"D:/_3.AI/audio_upscale/Soren_src")
MANIFEST_NAME = "dev_runtime_manifest.json"
BACKUP_DIR = ".backups"
FORMAT = "soren-dev-runtime"
FORMAT_VERSION = 2
OWNER = "SorenStudio tools/make_dev_runtime.py"
LEGACY_KEYS = {"generated_utc", "code_canonical", "code_runtime_sha256",
               "resources_source", "resource_mode"}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class RefuseError(RuntimeError):
    """安全策略拒绝执行（未知内容/用户改动/不安全资源操作），绝不自动清理。"""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_resource_root() -> Path:
    """资源根：SB_SOREN 优先，默认外部 Soren_src；缺失即报错，不静默换源。"""
    override = os.environ.get("SB_SOREN")
    root = Path(override) if override else EXTERNAL_SOREN
    problems = []
    if not (root / MANAGED_RESOURCE_FILE).is_file():
        problems.append(MANAGED_RESOURCE_FILE)
    problems += [n for n in RESOURCE_DIRS if not (root / n).is_dir()]
    if problems:
        raise RefuseError(
            f"SB_SOREN 资源目录无效：{root}（缺少：{', '.join(problems)}）。"
            "请设 SB_SOREN 指向 Soren 源目录。")
    return root


def _is_reparse_dir(path: Path) -> bool:
    """junction/symlink 目录检测（os.path.islink 对 junction 返回 False）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _backup(target: Path, name: str) -> None:
    src = target / name
    if src.is_file():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = target.parent / BACKUP_DIR / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, backup_dir / name)


def _read_manifest(target: Path):
    """返回 ("current", data) / ("legacy", data) / (None, None)。

    current：format/owner 匹配，且 managed 恰为两个 code + test_model、
    resource_mode 恰为三个资源目录（缺漏/多出/路径任意 → 视为未知，拒绝）。
    legacy：仅接受首版精确键结构（两个 code 键），且文件哈希与记录一致。
    """
    path = target / MANIFEST_NAME
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    if (data.get("format") == FORMAT
            and data.get("format_version") == FORMAT_VERSION
            and data.get("owner") == OWNER):
        managed = data.get("managed")
        modes = data.get("resource_mode")
        if not isinstance(managed, dict) or set(managed) != EXPECTED_MANAGED:
            return None, None
        if not all(isinstance(m, dict) and isinstance(m.get("sha256"), str)
                   and isinstance(m.get("source"), str) for m in managed.values()):
            return None, None
        if not isinstance(modes, dict) or set(modes) != set(RESOURCE_DIRS) \
                or not all(v in ("junction", "copy") for v in modes.values()):
            return None, None
        return "current", data
    if set(data) == LEGACY_KEYS:
        code_hashes = data["code_runtime_sha256"]
        if set(code_hashes) != set(MANAGED_CODE):
            return None, None
        for name, recorded in code_hashes.items():
            file = target / name
            if not file.is_file() or sha256_file(file) != recorded:
                return None, None  # 与记录不符 → 按未知内容处理
        return "legacy", data
    return None, None


def validate(target: Path = DEV_RUNTIME):
    """返回 (state, detail)。state ∈ ok/stale/absent/empty/legacy/unowned/modified/broken。"""
    if not target.exists():
        return "absent", f"{target} 不存在"
    if not target.is_dir():
        return "unowned", f"{target} 不是目录"
    if not list(target.iterdir()):
        return "empty", f"{target} 为空"
    kind, manifest = _read_manifest(target)
    if manifest is None:
        return "unowned", (f"{target} 非空且无有效 owned manifest；"
                           "可能包含未知/用户内容，本工具拒绝处理，请人工检查")
    records = (manifest.get("managed", {}) if kind == "current"
               else manifest["code_runtime_sha256"])
    for name, meta in records.items():
        recorded = meta["sha256"] if kind == "current" else meta
        file = target / name
        if not file.is_file():
            return "modified", f"owned 文件缺失：{name}"
        if sha256_file(file) != recorded:
            return "modified", (f"owned 文件被改动：{name}（如确认可 "
                                "python tools/make_dev_runtime.py --force，将先备份）")
    source = Path(manifest["resources_source"])
    try:
        expected_root = resolve_resource_root()
    except RefuseError as exc:
        return "stale", f"资源源不可用，需重建：{exc}"
    if os.path.realpath(source) != os.path.realpath(expected_root):
        return "stale", (f"资源根已变化：manifest={source} 当前={expected_root}"
                         "（将安全重建链接/资源文件）")
    for name in RESOURCE_DIRS:
        mode = manifest["resource_mode"][name]
        link = target / name
        if mode == "junction":
            if link.is_dir() and not _is_reparse_dir(link) and not link.is_symlink():
                return "unowned", f"{name} 处出现真实目录（非 junction），含未知内容，拒绝处理"
            if not (link.is_dir()
                    and os.path.realpath(link) == os.path.realpath(source / name)):
                return "broken", f"junction 失效：{name}"
        elif not link.is_dir():
            return "modified", f"owned 资源目录缺失：{name}"
    for name, src in MANAGED_CODE.items():
        recorded = records[name]["sha256"] if kind == "current" else records[name]
        if sha256_file(src) != recorded:
            return "stale", f"canonical 已更新：{name}（将安全同步）"
    if kind == "current":
        resource_file = source / MANAGED_RESOURCE_FILE
        if resource_file.is_file() and sha256_file(resource_file) != records[MANAGED_RESOURCE_FILE]["sha256"]:
            return "stale", f"资源源已更新：{MANAGED_RESOURCE_FILE}（将安全同步）"
    if kind == "legacy":
        return "legacy", "检测到首版 legacy manifest（哈希已验证），将安全升级"
    return "ok", ""


def _make_junction(src: Path, dst: Path) -> str:
    try:
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
        return "junction"
    except Exception:
        shutil.copytree(src, dst)
        return "copy"


def build(target: Path = DEV_RUNTIME, force: bool = False) -> Path:
    """安全生成/升级：先完整预检，通过后仅写 owned 内容；未知内容永不删除。"""
    # ── 预检阶段（零写入/零删除）──────────────────────────────────────
    plan_code = {}
    for name, src in MANAGED_CODE.items():
        if not src.is_file():
            raise RefuseError(f"权威源缺失：{src}")
        plan_code[name] = src.read_bytes()
    resource_root = resolve_resource_root()
    plan_resource_file = (resource_root / MANAGED_RESOURCE_FILE).read_bytes()
    state, detail = validate(target)
    if state == "unowned":
        raise RefuseError(f"拒绝处理：{detail}")
    if state == "modified" and not force:
        raise RefuseError(f"拒绝覆盖：{detail}")
    _, old_manifest = _read_manifest(target)
    old_modes = old_manifest.get("resource_mode", {}) if old_manifest else {}
    old_source = Path(old_manifest["resources_source"]) if old_manifest else None

    target.mkdir(parents=True, exist_ok=True)
    actions = {}
    for name in RESOURCE_DIRS:
        dst = target / name
        src_dir = resource_root / name
        if (dst.is_dir() and _is_reparse_dir(dst)
                and os.path.realpath(dst) == os.path.realpath(src_dir)):
            actions[name] = "keep"
        elif dst.is_symlink() or _is_reparse_dir(dst):
            actions[name] = "relink"      # 仅摘链接本身，不触及目标内容（含悬空 junction）
        elif dst.is_file():
            raise RefuseError(f"{dst} 是未知文件（manifest 未登记），拒绝删除/覆盖")
        elif dst.is_dir():
            if (old_modes.get(name) == "copy" and old_source is not None
                    and os.path.realpath(old_source) == os.path.realpath(resource_root)):
                actions[name] = "keep-copy"   # 资源源未变：copy 产物原样保留，不校验不删除
            else:
                raise RefuseError(
                    f"{dst} 是真实目录且非本工具登记的产物（或资源源已变化），"
                    "未登记文件级哈希，拒绝删除；请人工确认后手动处理")
        elif dst.exists():
            raise RefuseError(f"{dst} 为未知条目，拒绝处理")
        else:
            actions[name] = "create"

    # ── 执行阶段（预检全部通过）────────────────────────────────────────
    for name, data in plan_code.items():
        file = target / name
        if not (file.is_file() and file.read_bytes() == data):
            _backup(target, name)
            _atomic_write(file, data)
    resource_file = target / MANAGED_RESOURCE_FILE
    if not (resource_file.is_file() and resource_file.read_bytes() == plan_resource_file):
        _backup(target, MANAGED_RESOURCE_FILE)
        _atomic_write(resource_file, plan_resource_file)

    modes = {}
    for name in RESOURCE_DIRS:
        dst = target / name
        action = actions[name]
        if action == "keep":
            modes[name] = "junction"
        elif action == "keep-copy":
            modes[name] = "copy"
        else:
            if action == "relink":
                os.rmdir(dst)
            modes[name] = _make_junction(resource_root / name, dst)

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "owner": OWNER,
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "managed": {
            **{n: {"sha256": hashlib.sha256(d).hexdigest(), "source": str(s)}
               for n, s, d in ((n, s, plan_code[n]) for n, s in MANAGED_CODE.items())},
            MANAGED_RESOURCE_FILE: {
                "sha256": hashlib.sha256(plan_resource_file).hexdigest(),
                "source": str(resource_root / MANAGED_RESOURCE_FILE)},
        },
        "resources_source": str(resource_root),
        "resource_mode": modes,
        "read_only_convention": "资源 junction 并非访问控制：约定只读，禁止经 dev_runtime 写外部源",
    }
    _atomic_write(target / MANIFEST_NAME,
                  json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    unknown = [e.name for e in target.iterdir()
               if e.name not in (*MANAGED_CODE, MANAGED_RESOURCE_FILE, MANIFEST_NAME,
                                 *RESOURCE_DIRS)]
    print(f"[make_dev_runtime] 就绪 {target}（原状态 {state}）")
    print(f"  代码 = packaging 权威；资源 = {resource_root}（{modes}）")
    if unknown:
        print(f"  未知条目保持原样（未删除）：{unknown}")
    return target


def ensure(target: Path = DEV_RUNTIME) -> Path:
    """供 studio_backend 调用：可用即返回；缺失/陈旧/资源源变化自动安全重建；不明内容拒绝。"""
    state, _ = validate(target)
    if state == "ok":
        return target
    if state in ("absent", "empty", "stale", "legacy", "broken"):
        return build(target)
    _, detail = validate(target)
    raise RefuseError(
        f"dev runtime 需人工处理（{state}）：{detail}。"
        "请运行 python tools/make_dev_runtime.py 查看详情；本工具绝不删除未知内容。")


def verify(target: Path = DEV_RUNTIME) -> int:
    state, detail = validate(target)
    if state == "ok":
        print(f"[verify] OK：{target} 与 packaging 权威、资源源及 manifest 一致")
        return 0
    print(f"[verify] FAILED（{state}）：{detail}")
    return 1


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="生成/校验开发态 canonical Soren 运行时")
    _parser.add_argument("--verify", action="store_true", help="仅校验（无/坏 manifest 即失败）")
    _parser.add_argument("--force", action="store_true",
                         help="允许覆盖被改动的 owned 文件（先备份）；未知内容仍然拒绝")
    _args = _parser.parse_args()
    try:
        sys.exit(verify() if _args.verify else 0 if build(force=_args.force) else 0)
    except RefuseError as exc:
        print(f"[make_dev_runtime] 拒绝：{exc}")
        sys.exit(2)
