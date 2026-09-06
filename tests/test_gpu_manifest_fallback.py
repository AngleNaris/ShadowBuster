import io
import json
from unittest import mock
import gpu_env


def test_latest_failure_still_loads_pinned_gpu_release():
    sha='a'*64
    manifest={'version':'1.5.0','sha256':sha,'totalSize':1,'parts':[{'name':'gpu-env-1.5.0.part1of1','size':1,'sha256':sha}]}
    seen=[]
    def fetch(request,timeout=10):
        url=request.full_url;seen.append(url)
        if url.endswith('/latest'):raise TimeoutError('latest unavailable')
        if url.endswith('/tags/v1.5.0'):data={'assets_url':'https://test/assets'}
        elif url.endswith('/assets'):data=[{'name':'gpu-env-1.5.0.json','browser_download_url':'https://test/manifest'},{'name':'gpu-env-1.5.0.part1of1','browser_download_url':'https://test/part'}]
        else:data=manifest
        return io.BytesIO(json.dumps(data).encode())
    with mock.patch('urllib.request.urlopen',side_effect=fetch):
        result=gpu_env.load_manifest('1.5.0')
    assert result['version']=='1.5.0'
    assert any(url.endswith('/tags/v1.5.0') for url in seen)
