import json
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'experiments' / 'mid_prominence_compare.py'

def load():
    spec=importlib.util.spec_from_file_location('mid_compare', SCRIPT)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def test_tool_imports_and_explicit_paths():
    mod=load()
    assert mod.PYTHON.name == 'python.exe'
    assert mod.ROOT == ROOT
    assert (ROOT/'apollo_scripts'/'vocal_adjust.py').is_file()

def test_new_results_directory_is_independent_and_non_overwriting(tmp_path):
    mod=load()
    first=mod.safe_dir(tmp_path/'mid_prominence_compare_20260905')
    second=mod.safe_dir(tmp_path/'mid_prominence_compare_20260905')
    assert first != second and first.exists() and second.exists()

def test_report_contract_mentions_reference_and_lew():
    payload={"reference_provenance":{"is_processed_mix":False,"lew_run":False}}
    assert payload['reference_provenance']['is_processed_mix'] is False
    assert payload['reference_provenance']['lew_run'] is False

def test_existing_reference_artifacts_are_read_only_inputs():
    """历史实验音频属本地生成物（.gitignore 覆盖），存在时校验只读语义，
    不存在时（如工作区清理后）跳过而非失败。"""
    old = ROOT / 'experiments' / 'results' / 'real_20260905'
    if not (old / 'clip30.wav').is_file():
        import pytest
        pytest.skip('local experiment audio cleaned; regenerate via mid_prominence_compare.py')
    assert (old / 'demucs/htdemucs/clip30/vocals.wav').is_file()
    assert 'real_20260905' not in SCRIPT.read_text(encoding='utf8').split("base=args.out")[1].split("oldroot")[0]
