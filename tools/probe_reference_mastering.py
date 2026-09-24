"""Render protected references and equal-loudness comparisons on local music."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf
from audio_metrics import measure_audio
from mastering.pipeline import master_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', nargs='+', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {'reference_sha256': hashlib.sha256(args.reference.read_bytes()).hexdigest(), 'samples': {}}
    for source in args.input:
        name = source.parent.name
        row = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'runs': {}}
        variants = [('off', None, 0), ('reference_035', args.reference, .35),
                    ('reference_085', args.reference, .85), ('reference_100', args.reference, 1)]
        paths = []
        for label, ref, strength in variants:
            dst = args.output_dir / f'{name}_{label}.wav'
            start = time.perf_counter()
            stats = master_file(source, dst, 'normal', reference=ref, match_source=source,
                                strength=strength, space_wet=.6, width_db=6)
            elapsed = time.perf_counter() - start
            audio, sr = sf.read(dst, always_2d=True)
            metrics = measure_audio(audio, sr)
            row['runs'][label] = {'seconds': elapsed, 'mastering': stats, 'metrics': metrics}
            paths.append((dst, metrics))
        level = min(m['integrated_lufs'] - max(0, m['true_peak_4x_dbtp'] + 1) for _, m in paths)
        for dst, metrics in paths:
            audio, sr = sf.read(dst, always_2d=True)
            audio *= 10 ** ((level - metrics['integrated_lufs']) / 20)
            sf.write(dst.with_name(dst.stem + '_equal_loudness.wav'), audio, sr, subtype='FLOAT')
        report['samples'][name] = row
        print(f'Completed {name}', flush=True)
        (args.output_dir/'reference.json').write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                               allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
