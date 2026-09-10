#!/usr/bin/env bash
# ShadowBuster — macOS（Apple Silicon）一键打包：.app 外壳 + 内置推理 runtime。
# 产物: dist/ShadowBuster.app
#   Contents/MacOS/ShadowBuster        PyInstaller 外壳（QtWebEngine UI）
#   Contents/MacOS/_internal/ui        前端界面
#   Contents/MacOS/runtime/            env + Apollo + Soren_src + ffmpeg + 权重
# 前置: packaging/runtime_sync_macos.sh --runtime 的产物（不存在时自动装配），
#       ui/logo.icns（不存在时由 make_icns.sh 生成），.venv-shell（PyInstaller）。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$REPO_ROOT"

SHELL_PY="$REPO_ROOT/.venv-shell/bin/python"
[[ -x "$SHELL_PY" ]] || { echo "[build] 缺少 ${SHELL_PY}（先建 .venv-shell 装 PySide6/PyInstaller）" >&2; exit 1; }

STAGE="$REPO_ROOT/packaging/stage/runtime"
if [[ ! -x "$STAGE/env/bin/python" ]]; then
    echo "[build] runtime 未装配，先执行 runtime_sync_macos.sh --runtime ..."
    bash "$REPO_ROOT/packaging/runtime_sync_macos.sh" --runtime
fi
[[ -f "$REPO_ROOT/ui/logo.icns" ]] || bash "$REPO_ROOT/packaging/make_icns.sh"

echo "[build] PyInstaller 打包外壳 ..."
"$SHELL_PY" -m PyInstaller --noconfirm --clean "$REPO_ROOT/ShadowBuster_macos.spec"

APP="$REPO_ROOT/dist/ShadowBuster.app"
[[ -d "$APP" ]] || { echo "[build] PyInstaller 未产出 $APP" >&2; exit 1; }

# PyInstaller 对 PySide6 6.11 的 QtWebEngine hook 有 gap：收集的
# QtWebEngineCore.framework 被拍平（Versions/A 结构丢失，Helpers 里的
# QtWebEngineProcess.app 缺失），启动即报 "could not find QtWebEngineProcess"。
# 用 venv 里的原版框架整体替换（ditto 保留软链与内嵌 .app 结构；框架内
# rpath 均为相对路径，位置不变即解析不变），开发/打包布局保持一致。
FW_SRC="$("$SHELL_PY" -c 'import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).parent / "Qt" / "lib" / "QtWebEngineCore.framework")')"
FW_DST="$APP/Contents/Frameworks/PySide6/Qt/lib/QtWebEngineCore.framework"
if [[ -d "$FW_SRC" && -d "$FW_DST" && ! -d "$FW_DST/Versions/A/Helpers" ]]; then
    echo "[build] 修复 QtWebEngine 辅助进程（原样替换 QtWebEngineCore.framework）..."
    rm -rf "$FW_DST"
    ditto "$FW_SRC" "$FW_DST"
fi

echo "[build] 拷入推理 runtime（约 1.5 GB，稍候）..."
rm -rf "$APP/Contents/MacOS/runtime"
cp -R "$STAGE" "$APP/Contents/MacOS/runtime"

# 开源项目：不做 Developer ID 签名与公证。各二进制（PyInstaller 产物、
# Qt/ffmpeg 动态库）自带 ad-hoc 签名，本机与普通用户机器可直接运行；
# 外层 bundle 签名只在对外分发时按需补做。

echo "[build] 完成: $APP"
echo "试运行: open $APP   或   $APP/Contents/MacOS/ShadowBuster"
