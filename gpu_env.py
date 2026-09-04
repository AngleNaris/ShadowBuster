"""GPU 环境下载与安装（v1.5）。

CPU 版安装包不带 CUDA 运行时；需要 GPU 加速的用户在「设置 → GPU 环境」
里下载安装。运行时包以分卷资产发布在 GitHub Release 上（1 个清单 JSON +
N 个 <2GiB 的分卷），本模块负责：拉取清单 → 断点续传下载分卷 →
逐卷校验 → 组装 zip → 全包 SHA-256 校验 → 解压到用户目录并原子切换。

解压目标 LOCALAPPDATA\\ShadowBuster\\runtime-gpu\\env（不写 Program Files，
应用非提权运行也能安装）；studio_backend._resolve_runtime() 优先采用该环境，
重启应用后 CUDA 加速生效。

网络层收敛在 load_manifest / download_part 两个函数，其余逻辑无 I/O，
便于单元测试（tests/test_gpu_env.py）。
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

REPO = "AngleNaris/ShadowBuster"
PART_NAME_RE = re.compile(r"^gpu-env-[\w.-]+\.part\d{1,2}of\d{1,2}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
READ_CHUNK = 1 << 20  # 1 MiB


class DownloadCancelled(Exception):
    """用户取消下载/安装（进度保留，可续传）。"""


def user_base_dir():
    """用户数据根目录（下载、GPU 环境都放这里）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or "") or (Path.home() / "AppData" / "Local")
    return base / "ShadowBuster"


def runner_dir():
    """GPU 环境安装位置（env 与 gpu-env.json 标记都在这里）。"""
    return user_base_dir() / "runtime-gpu"


def env_dir():
    return runner_dir() / "env"


def marker_path():
    return runner_dir() / "gpu-env.json"


def dl_dir(version):
    """分卷下载工作目录。"""
    return user_base_dir() / "gpu_dl" / version


# ── 清单：结构校验（纯函数）+ 网络拉取 ──

def validate_manifest(data):
    """校验清单结构，非法输入抛 ValueError。返回规范化 dict。"""
    if not isinstance(data, dict):
        raise ValueError("清单不是 JSON 对象")
    version = data.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError(f"清单 version 非法: {version!r}")
    sha = data.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
        raise ValueError("清单 sha256 非法")
    total = data.get("totalSize")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        raise ValueError(f"清单 totalSize 非法: {total!r}")
    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("清单 parts 缺失或为空")
    if len(parts) > 32:
        raise ValueError("清单 parts 过多")
    seen = set()
    size_sum = 0
    out = []
    for p in parts:
        if not isinstance(p, dict):
            raise ValueError("part 不是 JSON 对象")
        name, psize, psha = p.get("name"), p.get("size"), p.get("sha256")
        if not isinstance(name, str) or not PART_NAME_RE.fullmatch(name):
            raise ValueError(f"part 名称非法: {name!r}")
        if not isinstance(psize, int) or isinstance(psize, bool) or psize <= 0:
            raise ValueError(f"part 大小非法: {name}")
        if not isinstance(psha, str) or not SHA256_RE.fullmatch(psha):
            raise ValueError(f"part sha256 非法: {name}")
        if name in seen:
            raise ValueError(f"part 重复: {name}")
        seen.add(name)
        size_sum += psize
        out.append({"name": name, "size": psize, "sha256": psha.lower()})
    if size_sum != total:
        raise ValueError(f"parts 大小总和 {size_sum} != totalSize {total}")
    return {"version": version, "sha256": sha.lower(), "totalSize": total, "parts": out}


def match_part_assets(parts, url_by_name):
    """为每个分卷匹配 GitHub 资产下载地址；缺失抛 LookupError。纯函数。"""
    out = []
    for p in parts:
        url = url_by_name.get(p["name"])
        if not url:
            raise LookupError(f"Release 缺少分卷资产: {p['name']}")
        out.append({**p, "url": url})
    return out


def pick_release(latest, version):
    """latest release 对象是否就是目标版本（tag 匹配即用）。

    新发布 release 的 tags/v{ver}/assets 匿名索引可能滞后（404），而
    releases/latest 返回的 id 型 assets_url 最稳定，故命中即用。"""
    if isinstance(latest, dict) and str(latest.get("tag_name", "")).lstrip("v") == str(version):
        return latest
    return None


def load_manifest(version, repo=REPO, timeout=10):
    """从 GitHub 拉取 v{version} 的资产列表，找到 gpu-env-{version}.json
    清单，校验后附上每个分卷的浏览器下载地址。网络错误/未发布抛异常。

    优先走 releases/latest 的 id 型 assets_url（匿名缓存最稳，新发布
    release 的 tags 路径索引可能滞后 404）；仅当最新 release 不是目标
    版本时才回退 tags/v{version} 路径。"""
    import urllib.request

    def _get(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": "ShadowBuster", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    rel = pick_release(_get(f"https://api.github.com/repos/{repo}/releases/latest"), version)
    if rel is None:
        rel = _get(f"https://api.github.com/repos/{repo}/releases/tags/v{version}")
    assets = _get(rel["assets_url"])
    url_by_name = {}
    for a in assets:
        name = str(a.get("name", ""))
        url = str(a.get("browser_download_url", ""))
        if name and url:
            url_by_name[name] = url
    mname = f"gpu-env-{version}.json"
    murl = url_by_name.get(mname)
    if not murl:
        raise LookupError(f"v{version} 未发布 GPU 环境（缺少清单 {mname}）")
    req = urllib.request.Request(murl, headers={"User-Agent": "ShadowBuster"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        manifest = validate_manifest(json.loads(r.read().decode("utf-8")))
    manifest["parts"] = match_part_assets(manifest["parts"], url_by_name)
    return manifest


# ── 下载：断点续传 ──

def resume_offset(part_path, expected_size):
    """续传起始偏移：0=未下载，expected=已完整（跳过），中间=可续传。"""
    if not Path(part_path).exists():
        return 0
    size = Path(part_path).stat().st_size
    if size > expected_size:
        raise ValueError(f"分卷文件损坏（{size} > 期望 {expected_size}）")
    return size


def download_part(url, dest, expected_size, cancel=None, progress=None, timeout=30):
    """下载单个分卷到 dest（已存在部分则 RANGE 续传）。
    cancel: 每次读块后检查，为真则抛 DownloadCancelled（保留已下部分）。
    progress(bytes_done): 每个读块回调一次（bytes_done 为本分卷内偏移）。
    返回实际写入的字节数；完成后校验分卷大小，不符抛 ValueError。"""
    import urllib.request
    off = resume_offset(dest, expected_size)
    if off == expected_size:
        return 0
    headers = {"User-Agent": "ShadowBuster"}
    if off > 0:
        headers["Range"] = f"bytes={off}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "ab" if off > 0 else "wb") as f:
        while True:
            if cancel and cancel():
                raise DownloadCancelled("下载已取消")
            chunk = r.read(READ_CHUNK)
            if not chunk:
                break
            f.write(chunk)
            off += len(chunk)
            if progress:
                progress(off)
    actual = Path(dest).stat().st_size
    if actual != expected_size:
        raise ValueError(f"分卷 {Path(dest).name} 大小不符（{actual} != {expected_size}）")
    return off


def sha256_of(path, cancel=None, progress=None):
    """流式计算文件 SHA-256；progress(cur,total) 每块回调。"""
    h = hashlib.sha256()
    total = Path(path).stat().st_size
    cur = 0
    with open(path, "rb") as f:
        while True:
            if cancel and cancel():
                raise DownloadCancelled("校验已取消")
            chunk = f.read(READ_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            cur += len(chunk)
            if progress:
                progress(cur, total)
    return h.hexdigest()


def assemble_zip(parts, dest_zip, cancel=None, progress=None):
    """按 order 拼接本地分卷为完整 zip。progress(cur,total)。"""
    total = sum(p["size"] for p in parts)
    cur = 0
    with open(dest_zip, "wb") as out:
        for p in parts:
            src = Path(p["local"])
            if not src.is_file():
                raise ValueError(f"缺少本地分卷: {src.name}")
            if src.stat().st_size != p["size"]:
                raise ValueError(f"本地分卷 {src.name} 大小不符")
            with open(src, "rb") as f:
                while True:
                    if cancel and cancel():
                        raise DownloadCancelled("组装已取消")
                    chunk = f.read(READ_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    cur += len(chunk)
                    if progress:
                        progress(cur, total)
    return dest_zip


def extract_zip(zip_path, dest_dir, cancel=None, progress=None):
    """解压 zip 到 dest_dir（不存在则创建）。按成员解压字节回报进度。"""
    import zipfile
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        total = sum(i.file_size for i in members if not i.is_dir())
        cur = 0
        for info in members:
            if cancel and cancel():
                raise DownloadCancelled("解压已取消")
            target = dest_dir / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, READ_CHUNK)
            cur += info.file_size
            if progress:
                progress(cur, total)
    return dest_dir


def installed_info():
    """读本地 GPU 环境标记；env 缺失视为未安装。返回 dict 或 None。"""
    try:
        data = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (env_dir() / "python.exe").is_file():
        return None
    return data


def swap_env(staging_dir, version, sha256):
    """校验 staging 完整后原子切换 env 并写标记文件。返回 env 目录。"""
    staging = Path(staging_dir)
    env = env_dir()
    if not (staging / "python.exe").is_file():
        raise ValueError("解压产物缺少 env\\python.exe，安装中止")
    runner_dir().mkdir(parents=True, exist_ok=True)
    old = runner_dir() / "env.old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if env.exists():
        os.rename(env, old)
    try:
        os.rename(staging, env)
    except Exception:
        if old.exists() and not env.exists():
            os.rename(old, env)  # 回滚旧环境
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    marker_path().write_text(json.dumps({
        "version": version,
        "sha256": sha256,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return env