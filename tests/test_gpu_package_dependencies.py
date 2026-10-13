"""A legacy CUDA archive cannot supply the new reference mastering path."""
import importlib.util
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gpu_package", ROOT / "packaging/make_gpu_env_package.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)

ENTRIES = (
    "python.exe",
    "Lib/site-packages/numpy/__init__.py",
    "Lib/site-packages/numpy/core.pyd",
    "Lib/site-packages/soundfile.py",
    "Lib/site-packages/torch-2.7.1+cu128.dist-info/METADATA",
    "Lib/site-packages/torchaudio-2.7.1+cu128.dist-info/METADATA",
    "Lib/site-packages/matchering/__init__.py",
    "Lib/site-packages/resampy/__init__.py",
)


@pytest.mark.parametrize("missing", ("matchering", "resampy"))
def test_cuda_archive_without_reference_dependency_is_rejected(tmp_path, missing):
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name in ENTRIES:
            if f"/{missing}/" not in name:
                stream.writestr(name, b"test fixture")
    with pytest.raises(SystemExit, match=missing):
        PACKAGE.required_zip_entries(archive)


def test_complete_cuda_archive_is_accepted(tmp_path):
    archive = tmp_path / "complete.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name in ENTRIES:
            stream.writestr(name, b"test fixture")
    assert PACKAGE.required_zip_entries(archive) == len(ENTRIES)
