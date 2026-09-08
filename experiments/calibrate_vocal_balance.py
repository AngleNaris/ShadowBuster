"""Offline, equal-category calibration of a fixed vocal Mid balance target."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apollo_scripts'))
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HOME'] = str(ROOT / 'packaging/stage/runtime_gpu/hf_home')

import numpy as np
import soundfile as sf

TRACKS = {
    'pop': ['Marry You - Bruno Mars.mp3', '匆匆那年 - 王菲.mp3', '成都 - 赵雷.mp3'],
    'rock': ['夜空中最亮的星 - 逃跑计划.mp3', '存在 - 汪峰.mp3', '暁の砂時計 - 10-FEET.mp3'],
    'electronic': ['bad guy - Billie Eilish.mp3', 'Solid Dance - STOLEN秘密行动.mp3', 'As Alive As You Need Me To Be - Nine Inch Nails.mp3'],
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def separate(source, output):
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    torch.manual_seed(0)
    model = get_model('htdemucs').eval().to('cuda')
    records = []
    for category, names in TRACKS.items():
        for index, name in enumerate(names):
            path = source / name
            sha = digest(path)
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-of', 'json', str(path)]))
            duration = float(info['format']['duration'])
            track = {'category': category, 'file': str(path), 'sha256': sha, 'duration_seconds': duration, 'clips': []}
            for clip_index, fraction in enumerate((0.22, 0.58)):
                start = min(duration - 25, duration * fraction)
                folder = output / f'{category}_{index}_{clip_index}'
                folder.mkdir(parents=True, exist_ok=False)
                mix_path = folder / 'mix.wav'
                subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(start), '-i', str(path), '-t', '25', '-ar', '44100', '-ac', '2', '-c:a', 'pcm_f32le', str(mix_path)], check=True)
                audio, sr = sf.read(mix_path, dtype='float32', always_2d=True)
                tensor = torch.from_numpy(audio.T.copy())
                reference = tensor.mean(0)
                mean, std = reference.mean(), reference.std()
                if std < 1e-8:
                    raise ValueError(f'silent reference: {path}')
                with torch.inference_mode():
                    stems = apply_model(model, ((tensor - mean) / std)[None], device='cuda', shifts=1, overlap=.25, progress=False)[0].cpu()
                stems = stems * std + mean
                vocal = stems[model.sources.index('vocals')].numpy().T
                sf.write(folder / 'vocals.wav', vocal, sr, subtype='FLOAT')
                track['clips'].append({'start_seconds': start, 'duration_seconds': len(audio)/sr, 'folder': str(folder)})
                print(f'{category}: {name} @ {start:.1f}s', flush=True)
            if digest(path) != sha:
                raise ValueError(f'source changed: {path}')
            records.append(track)
            (output / 'manifest.json').write_text(json.dumps({'model': 'htdemucs', 'seed': 0, 'tracks': records}, ensure_ascii=False, indent=2), encoding='utf-8')


def measure(output):
    from vocal_adjust import mid_rms_windows
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    categories = {}
    for track in manifest['tracks']:
        values = []
        for clip in track['clips']:
            folder = Path(clip['folder'])
            mix, sr = sf.read(folder / 'mix.wav', always_2d=True)
            vocal, _ = sf.read(folder / 'vocals.wav', always_2d=True)
            _, vr = mid_rms_windows(vocal, sr)
            _, ar = mid_rms_windows(mix - vocal, sr)
            active = vr >= max(1e-6, float(np.percentile(vr, 95)) * 0.10)
            valid = active & (ar > np.maximum(1e-6, vr * 0.01))
            ratio = np.full(len(vr), np.nan)
            good = valid & (vr > 1e-12) & (ar > 1e-12)
            ratio[good] = 20 * np.log10(vr[good] / ar[good])
            accepted = ratio[good]
            clip['valid_coverage'] = float(good.mean())
            clip['valid_seconds'] = float(good.sum() * .05)
            clip['ratio_p20_p50_p80_db'] = np.percentile(accepted, [20, 50, 80]).tolist() if len(accepted) else None
            if good.sum() >= 100:
                values.extend(accepted.tolist())
        if not values:
            raise ValueError(f'insufficient active reference: {track["file"]}')
        track['median_ratio_db'] = float(np.median(values))
        categories.setdefault(track['category'], []).append(track['median_ratio_db'])
    centers = {name: float(np.median(values)) for name, values in categories.items()}
    target = round(float(np.mean(list(centers.values()))) * 2) / 2
    manifest.update({'category_medians_db': centers, 'fixed_target_db': target,
                     'method': 'Per-track valid-window median; per-category track median; equal-weight mean of three category medians, rounded to 0.5 dB.',
                     'limitations': ['Nine local recordings; category assignments are broad and overlapping, not verified metadata.', 'Two fixed-position 25-second samples per track, not manually annotated verse/chorus.', 'Activity is an energy/leakage heuristic, not semantic singing recognition.', 'Demucs residual estimates, not isolated studio stems or a universal mixing standard.']})
    (ROOT / 'experiments' / 'results' / 'vocal_balance_calibration_20260905').mkdir(parents=True, exist_ok=True)
    (ROOT / 'experiments' / 'results' / 'vocal_balance_calibration_20260905' / 'calibration.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'category_medians_db': centers, 'fixed_target_db': target}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('C:/_kasidia/creamplayer-win32-x64/downloads'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--measure-only', action='store_true')
    args = parser.parse_args()
    if args.measure_only:
        measure(args.output)
    else:
        args.output.mkdir(parents=True, exist_ok=False)
        separate(args.source, args.output)
