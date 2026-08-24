"""量化面板拉丝纹理 / 硬件质感可见度。

用法: python tex_measure.py <screenshot.png> [--rect l,t,r,b]
只统计给定区域内的亮度微对比：逐对相邻扫描线（纹理周期 3px）亮度差的幅度，
同时统计像素级高频能量，供"质感是否更明显"做前后对比。
"""
import sys
import numpy as np
from PIL import Image


def main():
    src = sys.argv[1]
    rect = None
    if "--rect" in sys.argv:
        i = sys.argv.index("--rect")
        rect = [int(x) for x in sys.argv[i + 1].split(",")]

    im = Image.open(src).convert("L")
    a = np.asarray(im, dtype=np.float64)
    if rect:
        l, t, r, b = rect
        a = a[t:b, l:r]

    h, w = a.shape

    # 1) 行间微对比：相邻扫描线亮度差（纹理为周期 3px，相邻行必然一亮一暗）
    row_diffs = np.abs(a[0 : h - 1] - a[1:])
    line_contrast = float(row_diffs.mean())

    # 2) 相位幅度：按 phase%3 抽行，各行均值跨度 = 纯周期纹理的真实振幅
    phases = {p: a[p::3] for p in range(3)}
    phase_means = {p: float(np.mean(v)) for p, v in phases.items()}
    phase_amp = max(phase_means.values()) - min(phase_means.values())

    # 3) 全像素高频能量（相邻像素差），衡量表面 rich 程度
    hpix = np.concatenate([np.abs(np.diff(a, axis=1).ravel()),
                           np.abs(np.diff(a, axis=0).ravel())])
    highfreq = float(hpix.mean())

    # 4) 表面整体亮度与标准差
    surf_sd = float(a.std()) if a.size else 0.0
    surf_mean = float(a.mean()) if a.size else 0.0

    print(f"region={rect or 'full'} size={w}x{h}")
    print(f"line_contrast={line_contrast:.4f}  phase_amp={phase_amp:.4f}  "
          f"highfreq={highfreq:.4f} surf_sd={surf_sd:.4f} surf_mean={surf_mean:.2f}")
    print(f"phase_means={ {p: round(m, 3) for p, m in phase_means.items()} }")


if __name__ == "__main__":
    main()
