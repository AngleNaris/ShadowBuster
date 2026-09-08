"""Capture immutable Soren baselines before this experiment changes runtime files."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

def digest(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

if __name__ == '__main__':
    dest = ROOT / 'experiments/results' / ('soren_style_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    dest.mkdir(exist_ok=False)
    paths = [ROOT.parent/'Soren_src/core_decrypted.py', *ROOT.glob('packaging/**/Soren_src/core_decrypted.py')]
    manifest = {'cores': {}, 'inputs': {}, 'baseline': 'CPU/GPU staged core; existing presence protection retained in styled mode'}
    for i, p in enumerate(paths):
        manifest['cores'][str(p)] = digest(p)
        shutil.copyfile(p, dest / f'core_before_{i}.py')
    source = Path(r'D:\_4.Projects\_MY\中二病晚期患者\SorenPresenceFix_20260906_184851_330919')
    for p in source.glob('.work_*/reference_vocal.wav'):
        manifest['inputs'][str(p)] = digest(p)
    (dest/'baseline.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(dest)
