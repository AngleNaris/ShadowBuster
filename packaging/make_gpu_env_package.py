"""GPU 环境包制作：把 GPU 版运行时 env 打成一个 zip，切成 <2GiB 分卷，
并生成清单 JSON（供应用内下载安装，见 gpu_env.load_manifest）。

用法:
    python make_gpu_env_package.py <env_dir> <out_dir> <version>

产物:
    out_dir/gpu-env-<version>.zip        （中间产物，校验后可用可删）
    out_dir/gpu-env-<version>.part1ofN   分卷（每个 < 2GiB，逐个上传 Release）
    out_dir/gpu-env-<version>.json       清单（应用通过它定位分卷与校验值）

幂等：同名 zip 已存在且 SHA-256 与上次一致时直接复用，只重切分卷。
"""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

PART_TARGET = 1400 * 1024 * 1024      # 目标分卷大小（~1.37 GiB，远低于 2 GiB 硬限）
MAX_PART = 2 * 1024 * 1024 * 1024     # GitHub Release 单资产硬上限
ZIP_COMPRESS = zipfile.ZIP_DEFLATED
ZIP_LEVEL = 1                        # 体积/速度平衡：模型权重本身不可压
READ = 8 * 1024 * 1024
EXPECTED_TORCH = "2.7.1+cu128"
EXPECTED_TORCHAUDIO = "2.7.1+cu128"


def required_zip_entries(zpath):
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
    required = {
        "python.exe": "便携 Python",
        "Lib/site-packages/numpy/__init__.py": "numpy",
    }
    for name, label in required.items():
        if name not in names:
            raise SystemExit(f"GPU ZIP 缺少 {label}: {name}")
    if not any(name.startswith("Lib/site-packages/numpy/") and name.endswith(".pyd") for name in names):
        raise SystemExit("GPU ZIP 缺少 numpy 原生扩展 (.pyd)")
    if "Lib/site-packages/soundfile.py" not in names:
        raise SystemExit("GPU ZIP 缺少 soundfile.py")
    if not any(name.startswith(f"Lib/site-packages/torch-{EXPECTED_TORCH}.dist-info/") for name in names):
        raise SystemExit(f"GPU ZIP 缺少 CUDA torch metadata: {EXPECTED_TORCH}")
    if not any(name.startswith(f"Lib/site-packages/torchaudio-{EXPECTED_TORCHAUDIO}.dist-info/") for name in names):
        raise SystemExit(f"GPU ZIP 缺少 CUDA torchaudio metadata: {EXPECTED_TORCHAUDIO}")
    return len(names)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(READ), b""):
            h.update(chunk)
    return h.hexdigest()


def make_zip(src: Path, zpath: Path):
    if zpath.exists():
        print(f"删除旧 zip，按当前 runtime 重建: {zpath.name}")
        zpath.unlink()
    files = sorted(p for p in src.rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"源目录为空: {src}")
    print(f"压缩 {len(files)} 个文件 → {zpath.name} ...")
    with zipfile.ZipFile(zpath, "w", ZIP_COMPRESS, compresslevel=ZIP_LEVEL) as zf:
        for i, p in enumerate(files, 1):
            zf.write(p, p.relative_to(src).as_posix())
            if i % 2000 == 0 or i == len(files):
                print(f"  {i}/{len(files)} 文件")
    print(f"zip 完成: {zpath.name} {zpath.stat().st_size} bytes")
    return zpath


def split_zip(zpath: Path, out_dir: Path, version: str):
    """切分 zip 为分卷，返回 (parts, zip_sha, total)。"""
    parts = []
    size = zpath.stat().st_size
    n_parts = max(1, (size + PART_TARGET - 1) // PART_TARGET)
    if PART_TARGET >= MAX_PART:
        raise SystemExit(f"分卷目标大小必须 < 2GiB（当前 {PART_TARGET}）")
    print(f"切分为 {n_parts} 卷（每卷 ≤ {PART_TARGET >> 20} MB）...")
    with open(zpath, "rb") as f:
        remaining = size
        for i in range(1, n_parts + 1):
            name = f"gpu-env-{version}.part{i}of{n_parts}"
            part = out_dir / name
            want = size if i == n_parts else PART_TARGET  # 末卷取剩余全部
            with open(part, "wb") as out:
                done = 0
                while done < want:
                    chunk = f.read(min(READ, want - done))
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    remaining -= len(chunk)
            if part.stat().st_size >= MAX_PART:
                raise SystemExit(f"分卷超限: {name}")
            parts.append({"name": name, "size": part.stat().st_size,
                          "sha256": sha256_of(part)})
            print(f"  {name} {part.stat().st_size} bytes")
    if remaining != 0:
        raise SystemExit(f"切分字节不完整（剩余 {remaining}）")
    return parts, sha256_of(zpath), size


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("env_dir")
    ap.add_argument("out_dir")
    ap.add_argument("version")
    args = ap.parse_args()
    src = Path(args.env_dir)
    out = Path(args.out_dir)
    if not (src / "python.exe").is_file():
        raise SystemExit(f"env 缺少 python.exe: {src}")
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"gpu-env-{args.version}.zip"
    for old in out.glob(f"gpu-env-{args.version}.part*"):
        old.unlink()
    (out / f"gpu-env-{args.version}.json").unlink(missing_ok=True)
    make_zip(src, zpath)
    print(f"检查 ZIP 内容: {required_zip_entries(zpath)} entries")
    parts, zip_sha, total = split_zip(zpath, out, args.version)
    manifest = {"version": args.version, "sha256": zip_sha,
                "totalSize": total, "parts": parts}
    mpath = out / f"gpu-env-{args.version}.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"清单: {mpath.name}")
    print("上传命令（每个资产 < 2GiB）:")
    for p in parts:
        print(f'  gh release upload v{args.version} "{out / p["name"]}"')
    print(f'  gh release upload v{args.version} "{mpath}"')


if __name__ == "__main__":
    main()