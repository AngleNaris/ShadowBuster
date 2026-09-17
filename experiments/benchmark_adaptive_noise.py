"""Offline excerpt benchmark using existing stems; no separation or model downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audio_metrics import measure_audio, _json_safe


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=20)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    cases = json.loads(args.manifest.read_text(encoding='utf-8'))
    for case_index, case in enumerate(cases):
        mix_path, stems_path = Path(case['mix']), Path(case['stems'])
        info = sf.info(mix_path)
        frames = min(info.frames, round(args.seconds * info.samplerate))
        sources = {'mix': {'path': str(mix_path), 'sha256': sha256(mix_path)}}
        paths = {name: stems_path / (name + '.wav') for name in ('vocals', 'drums', 'bass', 'other')}
        for name, path in paths.items():
            stem_info = sf.info(path)
            if (stem_info.frames, stem_info.samplerate, stem_info.channels) != (info.frames, info.samplerate, info.channels):
                raise ValueError(f'Unaligned stem: {path}')
            sources[name] = {'path': str(path), 'sha256': sha256(path)}
        for part, fraction in (('start', 0), ('middle', .5), ('end', 1)):
            start = round((info.frames - frames) * fraction)
            work = args.output / f'{case_index + 1}-{part}'
            stems = work / 'stems'
            stems.mkdir(parents=True)
            x, sr = sf.read(mix_path, start=start, frames=frames, always_2d=True, dtype='float64')
            src, dst, report_path = work / 'input.wav', work / 'adaptive.wav', work / 'stage.json'
            sf.write(src, x, sr, subtype='FLOAT')
            for name, path in paths.items():
                stem, _ = sf.read(path, start=start, frames=frames, always_2d=True, dtype='float64')
                sf.write(stems / (name + '.wav'), stem, sr, subtype='FLOAT')
            command = [sys.executable, str(ROOT / 'apollo_scripts/soundstage_reshape.py'),
                       '--in-mix', str(src), '--out-wav', str(dst), '--stems-dir', str(stems),
                       '--wet', '0', '--other-denoise-amount', '.5', '--noise-mode', 'adaptive_all',
                       '--report-json', str(report_path)]
            began = time.perf_counter()
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
            (work / 'command.log').write_text(' '.join(command) + '\n' + process.stdout + process.stderr, encoding='utf-8')
            if process.returncode:
                raise RuntimeError(f'Benchmark failed: {work}')
            y, _ = sf.read(dst, always_2d=True)
            stage = json.loads(report_path.read_text(encoding='utf-8'))
            records.append({'song': case['label'], 'part': part, 'start_seconds': start / sr,
                            'duration_seconds': frames / sr, 'sources': sources,
                            'elapsed_seconds': time.perf_counter() - began,
                            'input_metrics': measure_audio(x, sr), 'output_metrics': measure_audio(y, sr),
                            'noise': stage['extra']['noise'], 'stage_scale': stage['scale'],
                            'delta_rms': float(np.sqrt(np.mean((y - x) ** 2))), 'output': str(dst)})
            print(f'{case["label"]} {part}: {records[-1]["noise"]["mix_budget"]}', flush=True)
    payload = {'schema_version': 1, 'scope': 'two-song excerpt diagnostic; no ABX or full-pipeline quality claim',
               'settings': {'amount': .5, 'max_attenuation_db': 6, 'low_hz': 8000, 'high_hz': 20000,
                            'wet': 0},
               'code_sha256': {name: sha256(ROOT / name) for name in
                               ('apollo_scripts/noise_profile.py', 'apollo_scripts/soundstage_reshape.py', 'audio_metrics.py')},
               'cases': records}
    (args.output / 'results.json').write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
