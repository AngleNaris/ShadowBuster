from pathlib import Path
import studio_backend as backend


def test_client_reuses_existing_gpu_release():
    assert backend.APP_VERSION == '1.6.3'
    assert backend.GPU_ENV_VERSION == '1.5.0'
    main=(Path(__file__).resolve().parents[1]/'main.py').read_text(encoding='utf-8')
    assert 'ge.load_manifest(backend.APP_VERSION)' not in main
    assert main.count('ge.load_manifest(backend.GPU_ENV_VERSION)') == 2
    assert 'ge.installed_info(expected_version=backend.GPU_ENV_VERSION)' in main
