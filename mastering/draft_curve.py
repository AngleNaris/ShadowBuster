"""Analysis-only bounded reference curve for the live draft player."""
import json
from pathlib import Path
import sys

import numpy as np
from .guard import matching_curve
from .matchering_adapter import candidate, UnsuitableReference


def main():
    source, reference, output = sys.argv[1:]
    try:
        x, y, metadata = candidate(source, reference, temp_parent=Path(output).parent)
        f, db = matching_curve(x, y, metadata['reference_bandwidth_hz'])
        centers = np.array([100, 250, 630, 1600, 4000, 10000, 16000])
        curve = list(zip(centers.tolist(), np.interp(centers, f, db).tolist()))
    except UnsuitableReference:
        curve = []
    Path(output).write_text(json.dumps(curve), encoding='utf-8')


if __name__ == '__main__':
    main()
