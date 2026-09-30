"""Content-addressed cache for the expensive reference tonal target (audit §9.8).

`candidate()` (Matchering) → `matching_curve()` depends ONLY on the (source,
reference) audio — never on loudness / final width / EQ. The outer mastering stage
cache keys on all of those knobs, so moving any one of them re-ran Matchering. This
inner cache is keyed solely on the two inputs' content, so the broad tonal target
survives downstream changes; the final guard still recomputes against the (unchanged)
candidate curve, exactly as the no-cache path does.

Cache `None` computes in memory and returns the raw `matching_curve` arrays — bit-for-bit
the pre-cache behaviour. A cache hit restores the same float64 arrays (JSON float
round-trips exactly), so warm output equals cold output.
"""
import json
from pathlib import Path

import numpy as np

from .guard import matching_curve

# Bumped when the (frequencies, db, metadata) semantics or serialization change.
TONAL_TARGET_VERSION = "reference-tonal-target-v1"


def _encode(frequencies, db, metadata):
    return {"version": TONAL_TARGET_VERSION,
            "frequencies": [float(v) for v in frequencies],
            "db": [float(v) for v in db],
            "metadata": metadata}


def _decode(payload):
    return (np.asarray(payload["frequencies"], dtype=np.float64),
            np.asarray(payload["db"], dtype=np.float64), payload["metadata"])


def tonal_target(candidate_fn, source_path, reference_path, *,
                 temp_parent=None, cache=None, artifact=None):
    """Return ``(frequencies, db, metadata)`` for the (source, reference) pair.

    ``candidate_fn(source, reference, temp_parent=...)`` is injected so the caller
    keeps its monkeypatchable symbol. ``cache=None`` skips the cache entirely.
    """
    def compute():
        source, raw, metadata = candidate_fn(source_path, reference_path,
                                              temp_parent=temp_parent)
        frequencies, db = matching_curve(source, raw, metadata["reference_bandwidth_hz"])
        return frequencies, db, metadata

    if cache is None:
        return compute()

    def write_artifact():
        frequencies, db, metadata = compute()
        Path(artifact).write_text(
            json.dumps(_encode(frequencies, db, metadata), allow_nan=False),
            encoding="utf-8")

    cache.run("reference_tonal_target", [source_path, reference_path], {},
              [artifact], write_artifact)
    return _decode(json.loads(Path(artifact).read_text(encoding="utf-8")))


def identity(mastering_dir, python):
    """StageCache identity: version + code that produces the curve.

    Only ``candidate`` (matchering_adapter) and ``matching_curve`` (guard) feed the
    tonal target, so those plus this module are the invalidation set; downstream
    pipeline / finalizer edits must not evict the expensive candidate.
    """
    import pipeline_cache
    code = [mastering_dir / name
            for name in ("reference_tone.py", "guard.py", "matchering_adapter.py")]
    return {"reference_tonal_target": 1, "version": TONAL_TARGET_VERSION,
            "python": str(python), "code": [pipeline_cache.md5(p) for p in code]}
