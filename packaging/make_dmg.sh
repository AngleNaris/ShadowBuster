#!/usr/bin/env bash
# ShadowBuster — macOS DMG 安装镜像（拖拽安装，Mac 常见分发形态）。
# 产物: dist/ShadowBuster-<ver>-macOS-arm64.dmg
# 前置: dist/ShadowBuster.app（先跑 build_macos.sh）、.venv-shell（PySide6/dmgbuild）。
# 已知坑: dmgbuild 自动估算镜像尺寸会低估 .app 实际占用，dmg_settings.py 里
# 显式 size="3g"（hdiutil 格式字符串，int 会崩）。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$REPO_ROOT"

APP="dist/ShadowBuster.app"
[[ -d "$APP" ]] || { echo "[dmg] 缺少 ${APP}（先跑 build_macos.sh）" >&2; exit 1; }
SHELL_PY="$REPO_ROOT/.venv-shell/bin/python"
[[ -x "$SHELL_PY" ]] || { echo "[dmg] 缺少 $SHELL_PY" >&2; exit 1; }
command -v dmgbuild >/dev/null 2>&1 || PATH="$REPO_ROOT/.venv-shell/bin:$PATH"
"$SHELL_PY" -c "import dmgbuild" 2>/dev/null || \
    { echo "[dmg] 需要 dmgbuild：uv pip install --python $SHELL_PY dmgbuild" >&2; exit 1; }

VERSION=$("$SHELL_PY" -c "import re; print(re.search(r'APP_VERSION = \"([\d.]+)\"', open('studio_backend.py', encoding='utf-8').read()).group(1))")
STAGE="$REPO_ROOT/dist/ShadowBuster-$VERSION-macOS-arm64"
DMG="$REPO_ROOT/dist/ShadowBuster-$VERSION-macOS-arm64.dmg"
BG="$REPO_ROOT/packaging/stage/dmg_background.png"

echo "[dmg] 生成安装背景图 ..."
"$SHELL_PY" - "$BG" "$APP/ui/logo.png" <<'PYEOF'
import sys
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QFont, QPainterPath
from PySide6.QtWidgets import QApplication

bg_path, logo_path = sys.argv[1], sys.argv[2]
app = QApplication([])
img = QImage(1320, 800, QImage.Format_ARGB32)
img.fill(QColor(0x14, 0x12, 0x18))
painter = QPainter(img)
painter.setRenderHint(QPainter.Antialiasing)
painter.setRenderHint(QPainter.SmoothPixmapTransform)
painter.save()
clip = QPainterPath()
clip.addRoundedRect(QRectF(120, 260, 280, 280), 0, 0)
painter.setClipPath(clip)
painter.drawImage(QRectF(120, 260, 280, 280), QImage(logo_path))
painter.restore()
painter.setPen(QColor(0xF1, 0xF0, 0xF1))
painter.setFont(QFont("PingFang SC", 44, QFont.Bold))
painter.drawText(480, 340, "ShadowBuster")
painter.setPen(QColor(0xFF, 0x54, 0x81))
painter.setFont(QFont("PingFang SC", 26))
painter.drawText(480, 420, "拖动 ShadowBuster 到右侧的 Applications")
painter.setPen(QColor(0x9A, 0x93, 0xA3))
painter.setFont(QFont("PingFang SC", 18))
painter.drawText(480, 480, "安装完成后可推出本镜像")
painter.end()
img.save(bg_path, "PNG")
PYEOF

echo "[dmg] 暂存安装内容 ..."
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/ShadowBuster.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/首次打开必读.txt" <<'EOF'
ShadowBuster（macOS / Apple Silicon）

安装：
1. 把 ShadowBuster.app 拖入右侧的 Applications 文件夹
2. 首次打开遇到安全提示属正常现象（开源软件未做付费开发者签名）：
   · 提示"无法验证开发者"：打开 系统设置 → 隐私与安全性 → 点"仍要打开"
   · 提示"已损坏，无法打开"：打开终端执行以下命令后再打开：
       xattr -cr /Applications/ShadowBuster.app

系统要求：
· Apple Silicon（M1/M2/M3/M4）Mac
· 无需安装 Python / ffmpeg / 任何模型，全部已内置，离线可用
· 推理自动使用 Apple GPU（MPS）加速
EOF

echo "[dmg] 构建 DMG ..."
# dmg_settings.py 的 files 是相对路径，必须在 stage 目录内运行
(
    cd "$STAGE"
    SB_DMG_BG="$BG" "$SHELL_PY" -m dmgbuild \
        -s "$REPO_ROOT/packaging/dmg_settings.py" \
        "ShadowBuster $VERSION" "$DMG"
)

echo "[dmg] 完成: $DMG"
