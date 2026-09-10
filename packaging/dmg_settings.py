# -*- coding: utf-8 -*-
# ShadowBuster DMG 安装镜像布局（dmgbuild 设置文件）。
# 以 CWD = dist/ShadowBuster-1.6.6-macOS-arm64 运行；背景图等经环境变量传入。
import os

app = "ShadowBuster.app"
readme = "首次打开必读.txt"

format = "UDZO"                     # 压缩只读镜像
files = [app, readme]
symlinks = {"Applications": "/Applications"}
background = os.environ["SB_DMG_BG"]

# dmgbuild 自动估算的镜像尺寸会低估 .app 实际占用（1.9G 被截到 ~700M，
# 写入时 ENOSPC）；显式给 3G。注意必须是 hdiutil 格式的字符串，int 会崩。
size = "3g"

window_rect = ((100, 100), (1340, 820))
icon_size = 128
icon_text_size = 14

icon_locations = {
    app: (260, 400),
    "Applications": (1060, 400),
    readme: (660, 700),
}
