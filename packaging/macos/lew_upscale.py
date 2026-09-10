# lew_upscale.py — Lew's Universal Super Resolution (Apollo uni, feature_dim=384)
# ─────────────────────────────────────────────────────────────
# macOS 权威副本：MPS 设备支持 + fp16 autocast + 分块缓存清理。
# 打包（runtime_sync_macos.sh）与开发运行均以本文件为准，覆盖组件
# 目录中的上游 fp32/CUDA 版本；改动需同步回上游组件。
# ─────────────────────────────────────────────────────────────
# 用法: python lew_upscale.py --in_wav in.wav --out_wav out.wav [--chunk-seconds 15] [--overlap-seconds 2] [--device cuda]
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torch.nn.functional as F

import look2hear.models  # 来自 JusperLee/Apollo 仓库（PYTHONPATH 需包含其父目录）

SAMPLE_RATE = 44_100
LEW_CKPT = Path(__file__).parent / "ckpts" / "lew" / "apollo_model_uni.ckpt"
FEATURE_DIM = 384   # Lew uni 架构
PRECISION = "auto"  # auto = MPS 上启用 fp16 autocast；fp32 = 全精度


def select_device(requested):
    if requested == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        # macOS Apple Silicon：CUDA 恒不可用，优先 MPS
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    return torch.device(requested)


def load_audio(file_path):
    audio, sample_rate = sf.read(file_path, dtype="float32", always_2d=True)
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"Apollo expects {SAMPLE_RATE} Hz audio, got {sample_rate} Hz.")
    audio = torch.from_numpy(np.ascontiguousarray(audio.T)).unsqueeze(0)  # [1, ch, T]
    return audio, sample_rate


def save_audio(file_path, audio, sample_rate):
    out = audio.detach().squeeze(0).to("cpu").numpy().T
    sf.write(file_path, out, sample_rate, subtype="FLOAT")


def resolve_chunking(chunk_seconds, overlap_seconds):
    if chunk_seconds is None:
        return None, 0
    chunk_samples = int(round(chunk_seconds * SAMPLE_RATE))
    overlap_samples = int(round(overlap_seconds * SAMPLE_RATE))
    if overlap_samples * 2 > chunk_samples:
        raise ValueError("Overlap must not exceed half the chunk duration.")
    return chunk_samples, overlap_samples


def chunk_starts(total_samples, chunk_samples, overlap_samples):
    hop = chunk_samples - overlap_samples
    starts = [0]
    while starts[-1] + chunk_samples < total_samples:
        starts.append(starts[-1] + hop)
    return starts


def crossfade_weights(length, overlap_samples, fade_in, fade_out, dtype):
    w = torch.ones(length, dtype=dtype)
    fade = min(overlap_samples, length)
    if fade:
        ramp = torch.linspace(0.0, 1.0, fade, dtype=dtype)
        if fade_in:
            w[:fade] = ramp
        if fade_out:
            w[-fade:] = torch.flip(ramp, dims=(0,))
    return w.view(1, 1, -1)


def _forward(model, batch, device, mps_fp16):
    """单次前向。MPS 上默认走 fp16 autocast：该模型多小算子，Metal 每算子
    调度开销高，fp16 实测 3.6x 提速、激活内存减半（对 fp32 输出 SNR≈58dB、
    相关 0.999999）；--precision fp32 可回到全精度。"""
    if mps_fp16:
        with torch.autocast("mps", dtype=torch.float16):
            return model(batch.to(device))
    return model(batch.to(device))


def run_model(model, audio, device, chunk_samples=None, overlap_samples=0, chunk_batch_size=1):
    total = audio.shape[-1]
    mps_fp16 = device.type == "mps" and PRECISION == "auto"
    if chunk_samples is None or total <= chunk_samples:
        out = _forward(model, audio, device, mps_fp16)
        return out.detach().to("cpu")

    out_sum = torch.zeros_like(audio, device="cpu")
    w_sum = torch.zeros((1, 1, total), dtype=audio.dtype)
    starts = chunk_starts(total, chunk_samples, overlap_samples)
    done = 0

    for bs in range(0, len(starts), chunk_batch_size):
        batch_starts = starts[bs : bs + chunk_batch_size]
        chunks, valid = [], []
        for s in batch_starts:
            e = min(s + chunk_samples, total)
            v = e - s
            c = audio[..., s:e]
            if v < chunk_samples:
                c = F.pad(c, (0, chunk_samples - v))
            chunks.append(c)
            valid.append(v)
        batch = torch.cat(chunks, dim=0)
        out = _forward(model, batch, device, mps_fp16).detach().to("cpu")
        for off, (s, v) in enumerate(zip(batch_starts, valid)):
            e = s + v
            co = out[off : off + 1, ..., :v]
            idx = bs + off
            w = crossfade_weights(v, overlap_samples, idx > 0, idx < len(starts) - 1, audio.dtype)
            out_sum[..., s:e] += co * w
            w_sum[..., s:e] += w
        # MPS 缓存分配器会保留已释放的块，长音频分块推理时逐块累积，
        # 在 16GB 统一内存机器上撑爆物理内存触发系统级换页（症状：内核态
        # CPU 远超用户态、耗时随音频长度超线性上涨）。每块后主动释放缓存。
        if device.type == "mps" and hasattr(torch, "mps"):
            torch.mps.empty_cache()
        done += sum(valid)
        # 进度上报：后端流式解析（格式: LEW_PROGRESS <pct>）
        print(f"\rLEW_PROGRESS {done / total * 100:.1f}", end="", flush=True)
    print()
    w_sum[w_sum == 0] = 1e-8
    return out_sum / w_sum


def main():
    ap = argparse.ArgumentParser(description="Lew's Universal Super Resolution (Apollo uni)")
    ap.add_argument("--in_wav", required=True, type=Path)
    ap.add_argument("--out_wav", required=True, type=Path)
    ap.add_argument("--checkpoint", type=Path, default=LEW_CKPT)
    ap.add_argument("--device", default="cuda", choices=["auto", "cuda", "cpu", "mps"])
    ap.add_argument("--precision", default="auto", choices=["auto", "fp32"],
                    help="auto = MPS 上启用 fp16 autocast（3.6x 提速，SNR≈58dB）")
    ap.add_argument("--chunk-seconds", type=float, default=15.0)
    ap.add_argument("--overlap-seconds", type=float, default=2.0)
    ap.add_argument("--chunk-batch-size", type=int, default=1)
    args = ap.parse_args()

    device = select_device(args.device)
    global PRECISION
    PRECISION = args.precision
    if not args.checkpoint.exists():
        raise FileNotFoundError(f"Lew checkpoint not found: {args.checkpoint}")
    if args.chunk_seconds and args.chunk_seconds <= 0:
        raise ValueError("chunk-seconds must be positive")

    chunk_samples, overlap_samples = resolve_chunking(args.chunk_seconds, args.overlap_seconds)
    audio, sr = load_audio(args.in_wav)

    model = look2hear.models.BaseModel.from_pretrain(
        str(args.checkpoint), sr=SAMPLE_RATE, win=20, feature_dim=FEATURE_DIM, layer=6
    ).to(device).eval()

    with torch.inference_mode():
        out = run_model(model, audio, device, chunk_samples, overlap_samples, args.chunk_batch_size)
    save_audio(args.out_wav, out, sr)
    print(f"Lew Universal done: {args.out_wav} on {device} | input {tuple(audio.shape)} -> {tuple(out.shape)}")


if __name__ == "__main__":
    main()
