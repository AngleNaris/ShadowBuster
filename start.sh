#!/usr/bin/env bash
# ShadowBuster — macOS 启动脚本（对应 Windows 的 start.bat）。
# 前置条件（详见 MIGRATION.md / docs/BUILD_MACOS.md）：
#   1) .venv-infer / .venv-shell 两个 venv 已建好并装好依赖
#   2) Apollo / Soren 组件目录就位（默认取仓库同级的 component_* 目录）
# SB_PYTHON / SB_APOLLO / SB_SOREN / SB_FFMPEG / SB_DEVICE 环境变量可覆盖默认值。
set -euo pipefail
cd "$(dirname "$0")"
ROOT_DIR="$(pwd -P)"

export SB_PYTHON="${SB_PYTHON:-$ROOT_DIR/.venv-infer/bin/python}"
export SB_APOLLO="${SB_APOLLO:-$(dirname "$ROOT_DIR")/component_Apollo}"
export SB_SOREN="${SB_SOREN:-$(dirname "$ROOT_DIR")/component_Soren_src}"

if [[ ! -x "$SB_PYTHON" ]]; then
    echo "[start] 缺少推理解释器: $SB_PYTHON" >&2
    echo "        先创建 .venv-infer 并按 notes/requirements-macos-arm64.txt 装依赖" >&2
    exit 1
fi
if [[ ! -d "$SB_APOLLO" || ! -d "$SB_SOREN" ]]; then
    echo "[start] 缺少组件目录: SB_APOLLO=$SB_APOLLO SB_SOREN=$SB_SOREN" >&2
    exit 1
fi

# Soren 装配件由 runtime_sync_macos.sh 落位（幂等，已同步则跳过）
if [[ ! -f "$SB_SOREN/soren_original.py" ]]; then
    echo "[start] Soren 目录缺少装配文件，执行 runtime_sync_macos.sh 补齐 ..."
    bash "$ROOT_DIR/packaging/runtime_sync_macos.sh"
fi

SHELL_PY="$ROOT_DIR/.venv-shell/bin/python"
if [[ ! -x "$SHELL_PY" ]]; then
    echo "[start] 缺少桌面外壳解释器: ${SHELL_PY}（先创建 .venv-shell 并安装 PySide6）" >&2
    exit 1
fi
exec "$SHELL_PY" "$ROOT_DIR/main.py"
