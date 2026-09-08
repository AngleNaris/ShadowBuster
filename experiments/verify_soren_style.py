"""Verify historical input provenance and additive default-mode preservation."""
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from experiments.compare_soren_styles import DEST,CORE
from experiments.capture_soren_baseline import digest
sys.path.insert(0,str(CORE.parent))


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
    return m


def main():
    baseline=json.loads((DEST/'baseline.json').read_text(encoding='utf-8'))
    history_path=ROOT/'experiments/results/analysis_vocal_reference_20260906/assessment.json'
    history=json.loads(history_path.read_text(encoding='utf-8'))
    checked={}
    for source in baseline['inputs']:
        source=Path(source)
        work=source.parent
        name=work.name.removeprefix('.work_')
        for filename in ('original.wav','lew.wav','bass.wav','drums.wav','shape.wav'):
            old=history_path.parent/name/filename
            expected=next(v for k,v in history['hashes'].items() if Path(k)==old)
            assert digest(old)==expected and digest(work/filename)==expected
            checked[str(work/filename)]=expected
        assert digest(source)==baseline['inputs'][str(source)]
        checked[str(source)]=digest(source)
        for p in (work/'comparison_mask.npz',work/'reference_vocal.vocal.json'):
            checked[str(p)]=digest(p)
    before=next(p for p in DEST.glob('core_before_*.py') if digest(p)=='3104340f58306080495750de36605af69fe258ae3a2037a9bd224a39b202af74')
    old=load('before_style_core',before);new=load('after_style_core',CORE)
    rng=np.random.default_rng(15);audio=rng.normal(0,.08,(2,44100));reference=rng.normal(0,.09,(2,44100))
    results=[]
    for c in (old,new):
        c.apply_dither=lambda x:x
        with contextlib.redirect_stdout(io.StringIO()):
            results.append(c.process_audio(audio.copy(),reference.copy(),5,c.Config(),{'genre':'Pop','lufs':-12,'initial_rms':.1}))
    np.testing.assert_array_equal(results[0],results[1])
    (DEST/'provenance_and_default.json').write_text(json.dumps({'historical_upstream_verified':checked,'premaster_note':'Existing previous-run premaster; current hash pinned before processing. Prior comparison records dimensions and measured ratios but did not store premaster hash, so no claim of historical byte-hash identity for this file.','styled_default_bit_identical_with_dither_disabled':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Provenance verified; styled DSP bit-identical without random dither')

if __name__=='__main__':main()
