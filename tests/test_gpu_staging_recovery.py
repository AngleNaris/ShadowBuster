import json
import os
from unittest import mock
import gpu_env as ge


def _patch_paths(root):
    # runner_dir 与 marker_path 一并重定向，避免测试写入真实应用目录
    return [mock.patch.object(ge, 'runner_dir', return_value=root),
            mock.patch.object(ge, 'marker_path', return_value=root / 'gpu-env.json')]


def test_first_install_can_activate_verified_directory_when_rename_denied(tmp_path):
    root=tmp_path/'runtime-gpu';root.mkdir()
    staging=root/('.staging-'+'a'*32);staging.mkdir();(staging/'python.exe').write_bytes(b'python')
    error=PermissionError('locked');error.winerror=5
    patches=_patch_paths(root)
    with patches[0],patches[1],mock.patch.object(ge,'SWAP_RETRIES',1),mock.patch.object(ge.os,'rename',side_effect=error):
        assert ge.swap_env(staging,'1.5.0','b'*64,runtime_validated=True)==staging
        assert ge.env_dir()==staging
        assert json.loads(ge.marker_path().read_text())['directory']==staging.name
    assert (staging/'python.exe').read_bytes()==b'python'


def test_unvalidated_directory_never_activated(tmp_path):
    import pytest
    root=tmp_path/'runtime-gpu';root.mkdir()
    staging=root/('.staging-'+'a'*32);staging.mkdir();(staging/'python.exe').write_bytes(b'python')
    error=PermissionError('locked');error.winerror=5
    patches=_patch_paths(root)
    with patches[0],patches[1],mock.patch.object(ge,'SWAP_RETRIES',1),mock.patch.object(ge.os,'rename',side_effect=error):
        with pytest.raises(PermissionError):ge.swap_env(staging,'1.5.0','b'*64)
        assert not ge.marker_path().exists()


def test_marker_cannot_redirect_outside_runtime(tmp_path):
    patches=_patch_paths(tmp_path)
    with patches[0],patches[1]:
        ge.marker_path().write_text(json.dumps({'directory':'../elsewhere'}))
        assert ge.env_dir()==tmp_path/'env'
