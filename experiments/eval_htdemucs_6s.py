"""Offline htdemucs_6s (six-stem) capability evaluation — batch 4 gate.

Downloads weights into $TORCH_HOME (caller sets it), separates the same clip
with the production 4-stem baseline and the 6-stem model on one device, and
reports wall time, GPU peak memory, stem energy shares, mixture residual and
guitar/piano cross-stem leakage. Results are measurements, not quality claims:
listening tests are still required before any product statement.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


def rel_db(a, b):
    na, nb = np.sum(a.astype(np.float64) ** 2), np.sum(b.astype(np.float64) ** 2)
    return None if na <= 0 or nb <= 0 else float(10 * np.log10(nb / na))


def pearson(a, b):
    if len(a) < 2:
        return None
    a, b = a - a.mean(), b - b.mean()
    denom = np.sqrt(np.sum(a * a) * np.sum(b * b))
    return None if denom <= 0 else float(np.sum(a * b) / denom)


def bandpass(x, sr, lo, hi):
    from scipy import signal
    sos = signal.butter(4, [lo, hi], btype="bandpass", fs=sr, output="sos")
    return signal.sosfilt(sos, x, axis=0)


def centroid(x, sr):
    spec = np.abs(np.fft.rfft(x[:, 0]))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    total = spec.sum()
    return None if total <= 0 else float(np.sum(f * spec) / total)


def side_ratio(x):
    mid = x.mean(axis=1)
    side = (x[:, 0] - x[:, 1]) / 2
    ms, ss = np.sum(mid ** 2), np.sum(side ** 2)
    return None if ms <= 0 else float(10 * np.log10(ss / ms))


def separate(model_name, mix, sr, device):
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    model = get_model(model_name)
    model.to(device)
    model.eval()
    if model.samplerate != sr:
        raise SystemExit(f"{model_name} expects {model.samplerate}Hz, clip is {sr}Hz")
    sources = list(model.sources)
    x = torch.from_numpy(mix.T[None]).to(device)
    if device == "cuda":
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    start = time.perf_counter()
    with torch.no_grad():
        out = apply_model(model, x, device=device, shifts=0, split=True,
                          overlap=0.25, progress=False)[0]
    if device == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - start
    peak_vram = torch.cuda.max_memory_allocated() / 2**30 if device == "cuda" else None
    stems = {name: out[i].cpu().numpy().T for i, name in enumerate(sources)}
    model.cpu()
    torch.cuda.empty_cache() if device == "cuda" else None
    return sources, stems, elapsed, peak_vram


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    mix, sr = sf.read(args.clip, always_2d=True, dtype="float32")
    results = {"clip": str(args.clip), "sr": sr, "seconds": round(len(mix) / sr, 2),
               "device": args.device, "models": {}}
    for name in ("htdemucs", "htdemucs_6s"):
        print(f"[eval] separating with {name} ...", flush=True)
        sources, stems, elapsed, peak_vram = separate(name, mix, sr, args.device)
        entry = {"sources": sources, "wall_s": round(elapsed, 2),
                 "gpu_peak_gb": round(peak_vram, 2) if peak_vram is not None else None}
        total = sum(stems.values())
        entry["residual_db"] = rel_db(mix, mix - total)
        entry["stem_energy_db"] = {s: rel_db(mix, stems[s]) for s in sources}
        for stem_dir in args.out_dir / name, :
            stem_dir.mkdir(exist_ok=True)
            for s, data in stems.items():
                sf.write(stem_dir / f"{s}.wav", data, sr, subtype="FLOAT")
        if "guitar" in stems:
            guitar, piano, other = stems["guitar"], stems["piano"], stems["other"]
            hf = lambda a: bandpass(a, sr, 2000, 12000)
            entry["guitar_vs_piano_corr"] = pearson(guitar.mean(axis=1), piano.mean(axis=1))
            entry["guitar_vs_other_corr_hf"] = pearson(hf(guitar).mean(axis=1), hf(other).mean(axis=1))
            entry["guitar_vs_vocals_corr_hf"] = pearson(
                hf(guitar).mean(axis=1), hf(stems["vocals"]).mean(axis=1))
            entry["guitar_centroid_hz"] = centroid(guitar, sr)
            entry["piano_centroid_hz"] = centroid(piano, sr)
            entry["guitar_side_ratio_db"] = side_ratio(guitar)
        results["models"][name] = entry
        print(json.dumps(entry, indent=2), flush=True)

    path = args.out_dir / "results.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] written {path}")


if __name__ == "__main__":
    sys.exit(main())
