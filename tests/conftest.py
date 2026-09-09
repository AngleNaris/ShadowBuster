"""Keep processing tests away from the user's persistent audio cache."""
import pytest


@pytest.fixture(autouse=True)
def isolated_processing_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_PROCESSING_CACHE_DIR', str(tmp_path / 'processing-cache'))
