"""Read-only album inputs; isolated EQ/reference renders, measurements and listening pack."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import psutil
import soundfile as sf
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audio_metrics import measure_audio


def peak_memory(process):
    # Windows venv python may be a launcher: include its actual interpreter.
    total = 0
    try:
        parent = psutil.Process(process.pid)
        for child in [parent, *parent.children(recursive=True)]:
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    return total


def inventory(folder):
    result = {}
    for path in sorted(folder.iterdir()):
        if path.is_file():
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            result[path.name] = {'sha256': digest, 'bytes': path.stat().st_size,
                                 'mtime_ns': path.stat().st_mtime_ns}
    return result


def prepare(source, output, start=0, duration=None):
    info = sf.info(source)
    x, sr = sf.read(source, start=round(start * info.samplerate),
                    frames=-1 if duration is None else round(duration * info.samplerate),
                    always_2d=True)
    if sr != 44100:
        divisor = math.gcd(sr, 44100)
        x = signal.resample_poly(x, 44100 // divisor, sr // divisor, axis=0,
                                 window=('kaiser', 8.6))
    sf.write(output, x, 44100, subtype='FLOAT')
    return len(x)


def render(source, reference, output, eq, strength):
    cmd = [sys.executable, '-m', 'mastering', str(source), str(output),
           '--loudness', 'normal', '--eq-profile', eq, '--space-wet', '.6', '--width-db', '6']
    if strength is not None:
        cmd += ['--reference', str(reference), '--strength', str(strength), '--match-source', str(source)]
    start = time.perf_counter()
    peak = 0
    with output.with_suffix('.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        while proc.poll() is None:
            peak = max(peak, peak_memory(proc))
            time.sleep(.25)
        if proc.returncode:
            raise RuntimeError(f'Render failed: {output.with_suffix(".log")}')
    stats = json.loads(Path(str(output) + '.mastering.json').read_text(encoding='utf-8'))
    x, sr = sf.read(output, always_2d=True)
    metrics = measure_audio(x, sr)
    return {'seconds': time.perf_counter() - start, 'peak_rss_mib': peak / 2**20,
            'mastering': stats, 'metrics': metrics, 'file': str(output)}


def listening_files(runs):
    level = min(row['metrics']['integrated_lufs'] - max(0, row['metrics']['true_peak_4x_dbtp'] + 1)
                for row in runs.values())
    for row in runs.values():
        path = Path(row['file'])
        x, sr = sf.read(path, always_2d=True)
        x *= 10 ** ((level - row['metrics']['integrated_lufs']) / 20)
        dst = path.with_name(path.stem + '_equal_loudness.wav')
        sf.write(dst, x, sr, subtype='FLOAT')
        row['equal_loudness_file'] = str(dst)
        row['equal_loudness_target_lufs'] = level


def summarize(report_path):
    """Gain-independent spectral comparisons from actual written outputs."""
    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    summary = {'source_unchanged': report['source_unchanged'], 'samples': {}}

    def bands(path):
        x, sr = sf.read(path, always_2d=True)
        f, p = signal.welch(x, sr, nperseg=8192, axis=0)
        p = p.sum(axis=1)
        def energy(lo, hi):
            return p[(f >= lo) & (f < hi)].sum()
        body = energy(500, 1500)
        return np.array([10 * np.log10(energy(40, 180) / body),
                         10 * np.log10(energy(8000, 16000) / body)])

    lines = ['# 万象婆娑：EQ / 参考强度测试', '',
             '原始目录只读，所有成品/中间文件均在本目录。测试范围为独立母带，不含上游 AI 修复。',
             '片段：原曲 45% 时间点起的 12 秒；参考为该曲旧处理成品的对应时间片段。',
             '试听请选 `_equal_loudness.wav`：同一组已对齐响度，避免更响被误认为更好。',
             'off 为无参考；ref_025/050/100 为参考强度；后缀为用户 EQ。', '',
             '| 组 | 歌曲 | Warm 低频变化 dB | Bright 高频变化 dB | 有参考 Bright 高频变化 dB |',
             '|---|---|---:|---:|---:|']
    for key, sample in report['samples'].items():
        runs = sample['runs']
        base = bands(runs['off_Neutral']['file'])
        row = {'source': sample['source'], 'scope': sample['scope'], 'runs': {}}
        for label, run in runs.items():
            stats, ref = run['mastering'], run['mastering']['reference']
            row['runs'][label] = {
                'lufs': stats['actual_lufs'], 'true_peak_dbtp': stats['true_peak_dbtp'],
                'target_status': stats['target_status'], 'reference_status': ref['status'],
                'requested_strength': ref['requested_strength'], 'accepted_strength': ref['accepted_strength'],
                'reason': ref.get('reason'), 'seconds': run['seconds'],
                'peak_rss_mib': run['peak_rss_mib'],
                'low_high_vs_neutral_db': (bands(run['file']) - base).tolist(),
            }
        if sample['scope'] == 'clip':
            a, _ = sf.read(runs['off_Neutral']['file'])
            b, _ = sf.read(runs['ref_000_Neutral']['file'])
            row['zero_strength_max_sample_difference'] = float(np.max(np.abs(a - b)))
            row['reference_bright_high_delta_db'] = float((bands(runs['ref_050_Bright']['file']) -
                                                           bands(runs['ref_050_Neutral']['file']))[1])
            low = row['runs']['off_Warm']['low_high_vs_neutral_db'][0]
            high = row['runs']['off_Bright']['low_high_vs_neutral_db'][1]
            lines.append(f'| {key} | {Path(sample["source"]).stem} | {low:.3f} | {high:.3f} | '
                         f'{row["reference_bright_high_delta_db"]:.3f} |')
        summary['samples'][key] = row
    lines += ['', '完整数据见 report.json；精简比较见 summary.json。',
              '指标只能确认功能与保护约束，未作主观听感验收。',
              f'源目录文件内容及修改时间核对：{"完全未变" if report["source_unchanged"] else "存在变化"}。']
    report_path.with_name('summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    report_path.with_name('试听说明.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    source_dir, out = args.source_dir.resolve(), args.output_dir.resolve()
    if out == source_dir or source_dir in out.parents:
        raise ValueError('Test output must be outside the source directory')
    out.mkdir(parents=True, exist_ok=False)
    before = inventory(source_dir)
    report = {'source_directory': str(source_dir), 'input_inventory': before,
              'scope': 'mastering only; no upstream AI separation/restoration', 'samples': {}}
    report_path = out / 'report.json'

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

    sources = [p for p in sorted(source_dir.glob('*.wav'))
               if '_shadowbuster' not in p.stem and (source_dir / (p.stem + '_shadowbuster.wav')).is_file()]
    variants = [('off_' + eq, eq, None) for eq in ('Neutral', 'Warm', 'Bright', 'Fusion')]
    variants += [(f'ref_{int(s*100):03d}_Neutral', 'Neutral', s) for s in (0, .25, .5, 1)]
    variants += [('ref_050_Bright', 'Bright', .5)]
    jobs = [(p, 'clip', variants) for p in sources]
    jobs += [(max(sources, key=lambda p: sf.info(p).duration), 'full',
              [('off_Neutral', 'Neutral', None), ('off_Bright', 'Bright', None), ('ref_050_Bright', 'Bright', .5)])]
    save()
    for index, (source, scope, settings) in enumerate(jobs, 1):
        reference = source_dir / (source.stem + '_shadowbuster.wav')
        folder = out / f'{index:02d}_{scope}'
        folder.mkdir()
        start = sf.info(source).duration * .45 if scope == 'clip' else 0
        duration = 12 if scope == 'clip' else None
        inp, ref = folder/'input.wav', folder/'reference.wav'
        frames = prepare(source, inp, start, duration)
        prepare(reference, ref, start, duration)
        row = {'source': str(source), 'reference': str(reference), 'start_seconds': start,
               'scope': scope, 'frames': frames, 'runs': {}}
        report['samples'][folder.name] = row
        for label, eq, strength in settings:
            print(f'START {folder.name} {source.name} {label}', flush=True)
            result = render(inp, ref, folder/(label + '.wav'), eq, strength)
            row['runs'][label] = result
            assert result['mastering']['output_frames'] == frames
            assert result['mastering']['output_subtype'] == 'PCM_24'
            assert result['metrics']['true_peak_4x_dbtp'] <= -.4
            assert result['mastering']['eq_profile'] == eq
            save()
            r = result['mastering']['reference']
            print(f'DONE {label} accepted={r["accepted_strength"]} status={r["status"]} '
                  f'LUFS={result["metrics"]["integrated_lufs"]:.3f} '
                  f'time={result["seconds"]:.1f}s RSS={result["peak_rss_mib"]:.0f}MiB', flush=True)
        listening_files(row['runs'])
        save()
    report['source_unchanged'] = inventory(source_dir) == before
    save()
    assert report['source_unchanged'], 'Source directory changed during test'
    summarize(report_path)
    print('COMPLETE: all source files unchanged', flush=True)


if __name__ == '__main__':
    main()
