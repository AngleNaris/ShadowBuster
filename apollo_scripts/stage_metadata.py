"""Fresh, strict metadata reports emitted by enhancement stages."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path
import soundfile as sf

SCHEMA_VERSION = 1

def audio_metadata(path):
    p = Path(path).resolve()
    info = sf.info(str(p))
    st = p.stat()
    return {"path": str(p), "sr": int(info.samplerate), "frames": int(info.frames),
            "channels": int(info.channels), "size_bytes": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns)}

def write_report(path, *, stage, scale, input_path, output_path):
    if not (isinstance(scale, (int, float)) and scale > 0 and scale <= 1):
        raise ValueError("scale must be finite and within (0, 1]")
    payload = {"schema_version": SCHEMA_VERSION, "stage": str(stage),
               "scale": float(scale), "input": audio_metadata(input_path),
               "output": audio_metadata(output_path)}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def read_report(path, *, stage, input_path, output_path):
    p = Path(path)
    if not p.is_file(): raise ValueError(f"missing {stage} metadata report: {p}")
    try: data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc: raise ValueError(f"invalid {stage} metadata report: {p}") from exc
    if data.get("schema_version") != SCHEMA_VERSION or data.get("stage") != stage:
        raise ValueError(f"invalid {stage} metadata report")
    scale = data.get("scale")
    if not isinstance(scale, (int, float)) or not 0 < scale <= 1:
        raise ValueError(f"invalid {stage} scale")
    for key, actual in (("input", input_path), ("output", output_path)):
        expected = data.get(key, {})
        fresh = audio_metadata(actual)
        for field in ("path", "sr", "frames", "channels", "size_bytes", "mtime_ns"):
            if expected.get(field) != fresh[field]:
                raise ValueError(f"stale or invalid {stage} {key} metadata")
    return float(scale)
