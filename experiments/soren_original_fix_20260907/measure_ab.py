# -*- coding: utf-8 -*-
"""A/B 输出测量：LUFS / 真峰 / 采样峰 / 分频段 M-S 宽度 / 单声道兼容。

用法: python measure_ab.py <ab_output_dir>  （含 ARTILUS3_OLD_/FIXED_shadowbuster.wav）
结果写 <dir>/measurements.json 并打印对照表。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy import signal

BANDS = [(20, 120, "low(20-120)"), (120, 2000, "mid(120-2k)"),
         (2000, 8000, "high(2k-8k)"), (8000, 16000, "air(8k-16k)")]


def true_peak_dbtp(x, sr):
    up = signal.resample_poly(x, 4, 1, axis=0)
    return 20 * np.log10(np.max(np.abs(up)) + 1e-12)


def band_ms(x, sr, lo, hi):
    a = signal.sosfilt(signal.butter(2, lo, "highpass", fs=sr, output="sos"), x, axis=0)
    b = signal.sosfilt(signal.butter(2, hi, "lowpass", fs=sr, output="sos"), a, axis=0)
    m = b.mean(axis=1)
    s = (b[:, 0] - b[:, 1]) / 2
    me, se = (m ** 2).mean(), (s ** 2).mean()
    return se / (me + se + 1e-12)


def measure(path):
    x, sr = sf.read(path, always_2d=True, dtype="float64")
    meter = pyln.Meter(sr)
    lufs = meter.integrated_loudness(x)
    m = x.mean(axis=1)
    s = (x[:, 0] - x[:, 1]) / 2
    out = {
        "file": str(path),
        "duration_s": round(len(x) / sr, 2),
        "lufs": round(float(lufs), 2),
        "true_peak_dbtp": round(float(true_peak_dbtp(x, sr)), 2),
        "sample_peak_db": round(float(20 * np.log10(np.max(np.abs(x)) + 1e-12)), 2),
        "mid_rms": round(float(np.sqrt((m ** 2).mean())), 5),
        "side_rms": round(float(np.sqrt((s ** 2).mean())), 5),
        "width_total": round(float((s ** 2).mean() / ((m ** 2).mean() + (s ** 2).mean() + 1e-12)), 4),
        "bands": {},
    }
    for lo, hi, name in BANDS:
        out["bands"][name] = round(band_ms(x, sr, lo, hi), 4)
    # 单声道兼容：折叠 mono 能量变化 + 相关性
    mono = (x[:, 0] + x[:, 1]) / 2
    ref = x[:, 0] * 0.5 + x[:, 1] * 0.5
    out["mono_peak_db"] = round(float(20 * np.log10(np.max(np.abs(mono)) + 1e-12)), 2)
    out["mono_corr_self"] = 1.0
    return out


def main():
    d = Path(sys.argv[1])
    targets = {
        "OLD": d / "ARTILUS3_OLD_shadowbuster.wav",
        "FIXED": d / "ARTILUS3_FIXED_shadowbuster.wav",
    }
    # 母带前输入（同一前置处理下的 reshape/vocals 差异）
    for tag, key in (("OLD", "pre_old"), ("FIXED", "pre_fixed")):
        p = d / ("old_dsp" if tag == "OLD" else "fixed_dsp") / "ARTILUS3_vocalmix.wav"
        if p.exists():
            targets[key] = p
    shared_drummix = d / "shared" / "ARTILUS3_drummix.wav"
    if shared_drummix.exists():
        targets["premaster_shared"] = shared_drummix

    results = {k: measure(p) for k, p in targets.items() if p.exists()}
    (d / "measurements.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                         encoding="utf-8")

    keys = ["lufs", "true_peak_dbtp", "sample_peak_db", "width_total"]
    print(f"\n{'metric':<22}" + "".join(f"{k:>16}" for k in results))
    for key in keys:
        print(f"{key:<22}" + "".join(f"{results[k][key]:>16}" for k in results))
    for band in results.get("OLD", results.get("FIXED", {})).get("bands", {}):
        print(f"{band:<22}" + "".join(f"{results[k]['bands'][band]:>16}" for k in results
                                      if "bands" in results[k]))
    print("\nmeasured ->", d / "measurements.json")


if __name__ == "__main__":
    main()
