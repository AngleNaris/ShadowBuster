import json
from unittest import mock
import main
import gpu_env


def test_client_update_checks_existing_gpu_without_network():
    bridge=main.Bridge(None); events=[];bridge.gpuStatus.connect(events.append)
    installed={'version':main.backend.GPU_ENV_VERSION,'runtimeValidated':True}
    with mock.patch.object(main.sys,'frozen',True,create=True), \
         mock.patch.object(main.backend,'APP_VERSION','99.0.0'), \
         mock.patch.object(gpu_env,'installed_info',return_value=installed) as lookup, \
         mock.patch.object(gpu_env,'load_manifest',side_effect=AssertionError('no network')):
        bridge._gpu_lock.acquire();bridge._gpu_check_worker()
    lookup.assert_called_once_with(expected_version=main.backend.GPU_ENV_VERSION)
    assert json.loads(events[-1])['installed']==installed
    assert not bridge._gpu_lock.locked()


def test_install_request_reuses_valid_gpu_offline():
    bridge=main.Bridge(None);events=[];bridge.gpuStatus.connect(events.append)
    installed={'version':main.backend.GPU_ENV_VERSION,'runtimeValidated':True}
    with mock.patch.object(main.backend,'APP_VERSION','99.0.0'), \
         mock.patch.object(gpu_env,'installed_info',return_value=installed) as lookup, \
         mock.patch.object(gpu_env,'load_manifest',side_effect=AssertionError('no network')), \
         mock.patch.object(gpu_env,'download_part',side_effect=AssertionError('no download')):
        bridge._gpu_lock.acquire();bridge._gpu_install_worker()
    lookup.assert_called_once_with(expected_version=main.backend.GPU_ENV_VERSION)
    assert json.loads(events[-1])=={'type':'done','version':installed['version'],'reused':True}
    assert not bridge._gpu_lock.locked()
