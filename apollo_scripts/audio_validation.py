"""Shared validation helpers for Apollo audio stage scripts."""
from pathlib import Path
import numpy as np


def validate_audio_pair(primary, secondary, sr, *, primary_name="audio", secondary_name="in-mix", require_sr=None):
    if primary.ndim != 2 or secondary.ndim != 2:
        raise ValueError(f"{primary_name} must be a 2-D channel array")
    if primary.shape[1] != secondary.shape[1]:
        raise ValueError(f"channel count mismatch: {primary_name} has {primary.shape[1]}, {secondary_name} has {secondary.shape[1]}")
    if len(primary) == 0 or len(secondary) == 0:
        raise ValueError(f"empty audio: {primary_name if len(primary) == 0 else secondary_name}")
    if len(primary) != len(secondary):
        raise ValueError(f"length mismatch: {primary_name} has {len(primary)} samples, {secondary_name} has {len(secondary)}")
    if not np.isfinite(primary).all() or not np.isfinite(secondary).all():
        raise ValueError(f"non-finite audio samples in {primary_name if not np.isfinite(primary).all() else secondary_name}")
    if require_sr is not None and sr != require_sr:
        raise ValueError(f"{primary_name} sample rate {sr} != required {require_sr}")


def validate_audio(data, *, name="audio", sr=None, require_sr=None, channels=None):
    if data.ndim != 2:
        raise ValueError(f"{name} must be a 2-D channel array")
    if len(data) == 0:
        raise ValueError(f"empty audio: {name}")
    if channels is not None and data.shape[1] != channels:
        raise ValueError(f"channel count mismatch: {name} has {data.shape[1]}, expected {channels}")
    if not np.isfinite(data).all():
        raise ValueError(f"non-finite audio samples in {name}")
    if require_sr is not None and sr != require_sr:
        raise ValueError(f"{name} sample rate {sr} != required {require_sr}")


def finite_range(value, name, lo, hi):
    if not np.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{name} must be finite and within [{lo}, {hi}]")
    return float(value)
