#!/usr/bin/env bash
# 从矢量源 ui/shadowbuster.svg 生成 ui/logo.icns（macOS .app 图标）。
# 图标设计以 SVG（红色渐变边框版）为唯一权威来源，与 Windows 的 logo.ico
# 同一设计；logo.png 是旧版紫色设计，仅作最后回退。
# 依赖 macOS 自带 qlmanage/sips/iconutil，无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")/.."

SVG="ui/shadowbuster.svg"
ICO="ui/logo.ico"
PNG="ui/logo.png"
OUT="ui/logo.icns"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MASTER="$WORK/master.png"

if [[ -f "$SVG" ]]; then
    # qlmanage 渲染 SVG 矢量 → 1024px 位图（白底 由样式矩形自带）
    qlmanage -t -s 1024 -o "$WORK" "$SVG" >/dev/null 2>&1 || true
    RENDERED="$WORK/$(basename "$SVG").png"
    if [[ -s "$RENDERED" ]]; then
        mv "$RENDERED" "$MASTER"
    fi
fi
if [[ ! -s "$MASTER" && -f "$ICO" ]]; then
    # 回退：从 ico 里最大的内嵌图（256px）放大
    sips -s format png "$ICO" --out "$MASTER" >/dev/null
fi
[[ -s "$MASTER" ]] || { echo "[icns] 无可用图标源（$SVG / $ICO / $PNG 均失败）" >&2; exit 1; }

# ── Apple 图标模板预制：绕过 Dock 的系统圆角遮罩 ─────────────────────
# Dock 只对"满幅不透明方形"图标自动套系统圆角；把图标内缩到 824/1024
# 居中（Apple 模板网格，与系统裁切后的视觉尺寸一致）并烘焙透明四角后，
# Dock 按原样绘制、不再加工——圆角半径由本脚本决定而非系统。
# SB_ICON_RADIUS 可自定义圆角半径（默认 0 = 完全直角，忠实原设计）。
SHELL_PY="$(cd "$(dirname "$0")/.." && pwd -P)/.venv-shell/bin/python"
if [[ -x "$SHELL_PY" ]]; then
    "$SHELL_PY" - "$MASTER" "$WORK/template.png" "${SB_ICON_RADIUS:-0}" <<'PYEOF'
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath

src, dst, radius = QImage(sys.argv[1]), sys.argv[2], float(sys.argv[3])
canvas = QImage(1024, 1024, QImage.Format_ARGB32)
canvas.fill(Qt.transparent)
body = 824
margin = (1024 - body) // 2
painter = QPainter(canvas)
painter.setRenderHint(QPainter.Antialiasing)
clip = QPainterPath()
clip.addRoundedRect(QRectF(margin, margin, body, body), radius, radius)
painter.setClipPath(clip)
painter.drawImage(margin, margin, src.scaled(
    body, body, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
painter.end()
canvas.save(dst, "PNG")
PYEOF
    if [[ -s "$WORK/template.png" ]]; then
        mv "$WORK/template.png" "$MASTER"
    else
        echo "[icns] ⚠ 模板预制失败（缺 PySide6?），回退满幅图标：Dock 将套用系统圆角" >&2
    fi
fi

ICONSET="$WORK/logo.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$MASTER" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    s2=$((s * 2))
    sips -z "$s2" "$s2" "$MASTER" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$OUT"
echo "[icns] 生成 ${OUT}"
