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
    assert json.loads(events[-1])['source']=='app'
    assert not bridge._gpu_lock.locked()


def test_check_falls_back_to_legacy_user_dir_without_network():
    bridge=main.Bridge(None); events=[];bridge.gpuStatus.connect(events.append)
    installed={'version':main.backend.GPU_ENV_VERSION,'runtimeValidated':True}
    with mock.patch.object(main.sys,'frozen',True,create=True), \
         mock.patch.object(gpu_env,'installed_info',return_value=None), \
         mock.patch.object(gpu_env,'legacy_installed_info',return_value=installed) as legacy, \
         mock.patch.object(gpu_env,'load_manifest',side_effect=AssertionError('no network')):
        bridge._gpu_lock.acquire();bridge._gpu_check_worker()
    legacy.assert_called_once_with(expected_version=main.backend.GPU_ENV_VERSION)
    payload=json.loads(events[-1])
    assert payload['installed']==installed and payload['source']=='system'
    assert not bridge._gpu_lock.locked()


def test_check_emits_scanning_phases_before_manifest():
    bridge=main.Bridge(None); events=[];bridge.gpuStatus.connect(events.append)
    with mock.patch.object(main.sys,'frozen',True,create=True), \
         mock.patch.object(gpu_env,'installed_info',return_value=None), \
         mock.patch.object(gpu_env,'legacy_installed_info',return_value=None), \
         mock.patch.object(gpu_env,'load_manifest',side_effect=RuntimeError('offline')):
        bridge._gpu_lock.acquire();bridge._gpu_check_worker()
    phases=[json.loads(e) for e in events]
    assert phases[0]=={'type':'scanning','phase':'app'}
    assert phases[1]=={'type':'scanning','phase':'system'}
    assert phases[-1]['type']=='error'
    assert not bridge._gpu_lock.locked()


def test_check_reports_unwritable_app_dir_in_state():
    bridge=main.Bridge(None); events=[];bridge.gpuStatus.connect(events.append)
    manifest={'version':'1.5.0','totalSize':1,'sha256':'a'*64,'parts':[]}
    with mock.patch.object(main.sys,'frozen',True,create=True), \
         mock.patch.object(gpu_env,'installed_info',return_value=None), \
         mock.patch.object(gpu_env,'legacy_installed_info',return_value=None), \
         mock.patch.object(gpu_env,'load_manifest',return_value=manifest), \
         mock.patch.object(gpu_env,'app_writable',return_value=False) as writable, \
         mock.patch.object(gpu_env,'nvidia_driver_present',return_value=True), \
         mock.patch.object(gpu_env,'migrate_legacy_runtime',side_effect=AssertionError('no migrate')):
        bridge._gpu_lock.acquire();bridge._gpu_check_worker()
    writable.assert_called()
    payload=json.loads(events[-1])
    assert payload['installed'] is None and payload['writable'] is False
    assert payload['manifest']==manifest
    assert not bridge._gpu_lock.locked()


def test_install_refuses_when_app_dir_unwritable():
    bridge=main.Bridge(None); events=[];bridge.gpuStatus.connect(events.append)
    with mock.patch.object(gpu_env,'installed_info',return_value=None), \
         mock.patch.object(gpu_env,'app_writable',return_value=False), \
         mock.patch.object(gpu_env,'load_manifest',side_effect=AssertionError('no network')), \
         mock.patch.object(gpu_env,'download_part',side_effect=AssertionError('no download')):
        bridge._gpu_lock.acquire();bridge._gpu_install_worker()
    payload=json.loads(events[-1])
    assert payload['type']=='error' and '不可写' in payload['msg']
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
