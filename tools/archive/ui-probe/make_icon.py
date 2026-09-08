"""从生成的 logo 原图裁切去背景，产出 ui/logo.png 与 ui/logo.ico。"""
from pathlib import Path
from PIL import Image, ImageDraw

SRC = Path(__file__).parent / "visual" / "vis_20260820_204824_663d93c0" / "ShadowBuster应用图标.png"
UI = Path(__file__).parent / "ui"


def main():
    im = Image.open(SRC).convert("RGBA")
    w, h = im.size
    c = 700
    left, top = (w - c) // 2, (h - c) // 2
    im = im.crop((left, top, left + c, top + c))
    # 从四角洪泛填充为背景透明，保留主体、裁掉水印区域外的杂边
    for xy in [(0, 0), (c - 1, 0), (0, c - 1), (c - 1, c - 1)]:
        ImageDraw.floodfill(im, xy, (0, 0, 0, 0), thresh=48)
    UI.mkdir(exist_ok=True)
    im.resize((96, 96), Image.LANCZOS).save(UI / "logo.png")
    im.save(UI / "logo.ico",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("ok", (UI / "logo.png").stat().st_size, (UI / "logo.ico").stat().st_size)


if __name__ == "__main__":
    main()
