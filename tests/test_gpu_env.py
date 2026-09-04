"""gpu_env 模块单元测试：清单校验 / 地址匹配 / 断点续传 / 组装 / 哈希 /
解压 / 环境切换（全部纯逻辑，不访问网络）。"""
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import gpu_env as ge


def sample_manifest():
    return {
        "version": "1.5.0",
        "sha256": "a" * 64,
        "totalSize": 3000,
        "parts": [
            {"name": "gpu-env-1.5.0.part1of2", "size": 2000, "sha256": "b" * 64},
            {"name": "gpu-env-1.5.0.part2of2", "size": 1000, "sha256": "c" * 64},
        ],
    }


class GpuEnvTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["LOCALAPPDATA"] = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("LOCALAPPDATA", None)


class RuntimeValidationTests(GpuEnvTestCase):
    def test_missing_python_rejected(self):
        with self.assertRaises(ValueError):
            ge.validate_runtime(Path(self._tmp.name) / "missing.exe")

    def test_probe_failure_includes_child_output(self):
        py = Path(self._tmp.name) / "python.exe"
        py.write_bytes(b"py")
        failed = mock.Mock(returncode=1, stdout="", stderr="No module named numpy")
        with mock.patch("gpu_env.subprocess.run", return_value=failed) as run:
            with self.assertRaisesRegex(ValueError, "No module named numpy"):
                ge.validate_runtime(py, required=("numpy",))
        run.assert_called_once()

    def test_probe_adds_module_paths_to_child_environment(self):
        py = Path(self._tmp.name) / "python.exe"
        py.write_bytes(b"py")
        passed = mock.Mock(returncode=0, stdout="runtime imports OK 2.5.2\n", stderr="")
        module_path = Path(self._tmp.name) / "Apollo"
        with mock.patch("gpu_env.subprocess.run", return_value=passed) as run:
            ge.validate_runtime(py, required=("numpy",), module_paths=(module_path,))
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], str(module_path))


class ManifestTests(GpuEnvTestCase):
    def test_valid_manifest_normalizes(self):
        m = ge.validate_manifest(sample_manifest())
        self.assertEqual(m["version"], "1.5.0")
        self.assertEqual(m["sha256"], "a" * 64)
        self.assertEqual(len(m["parts"]), 2)
        self.assertEqual(sum(p["size"] for p in m["parts"]), m["totalSize"])

    def test_valid_manifest_uppercase_sha(self):
        d = sample_manifest()
        d["sha256"] = "A" * 64
        m = ge.validate_manifest(d)
        self.assertEqual(m["sha256"], "a" * 64)

    def test_missing_version(self):
        d = sample_manifest(); del d["version"]
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_bad_version(self):
        d = sample_manifest(); d["version"] = "1.5"
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_load_manifest_rejects_embedded_version_mismatch(self):
        latest = {"tag_name": "v1.5.0", "assets_url": "https://api.github.com/assets"}
        manifest = sample_manifest()
        manifest["version"] = "1.4.0"
        responses = {
            "https://api.github.com/repos/AngleNaris/ShadowBuster/releases/latest": latest,
            "https://api.github.com/assets": [
                {"name": "gpu-env-1.5.0.json", "browser_download_url": "https://x/manifest"},
                {"name": "gpu-env-1.5.0.part1of2", "browser_download_url": "https://x/p1"},
                {"name": "gpu-env-1.5.0.part2of2", "browser_download_url": "https://x/p2"},
            ],
        }

        class JsonResp(FakeResp):
            def __init__(self, value):
                super().__init__(json.dumps(value).encode())

        def get(req, timeout=10):
            url = req.full_url
            if url == "https://x/manifest":
                return JsonResp(manifest)
            return JsonResp(responses[url])

        with mock.patch("urllib.request.urlopen", side_effect=get):
            with self.assertRaisesRegex(ValueError, "版本与请求不一致"):
                ge.load_manifest("1.5.0")

    def test_bad_sha256(self):
        d = sample_manifest(); d["sha256"] = "xyz"
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_total_mismatch(self):
        d = sample_manifest(); d["totalSize"] = 9999
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_dup_part_name(self):
        d = sample_manifest()
        d["parts"].append(dict(d["parts"][0]))
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_path_traversal_part_name(self):
        d = sample_manifest()
        d["parts"][0]["name"] = "../gpu-env-1.5.0.part1of2"
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_non_dict_part(self):
        d = sample_manifest(); d["parts"][0] = "part1"
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_empty_parts(self):
        d = sample_manifest(); d["parts"] = []
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)

    def test_too_many_parts(self):
        d = sample_manifest()
        d["parts"] = [dict(d["parts"][0]) for _ in range(33)]
        with self.assertRaises(ValueError):
            ge.validate_manifest(d)


class MatchAssetsTests(GpuEnvTestCase):
    def test_all_matched(self):
        m = sample_manifest()
        out = ge.match_part_assets(m["parts"], {
            "gpu-env-1.5.0.part1of2": "https://x/p1",
            "gpu-env-1.5.0.part2of2": "https://x/p2",
            "ShadowBuster-Setup-1.5.0.exe": "https://x/setup",
        })
        self.assertEqual([p["url"] for p in out], ["https://x/p1", "https://x/p2"])

    def test_missing_part(self):
        m = sample_manifest()
        with self.assertRaises(LookupError):
            ge.match_part_assets(m["parts"], {
                "gpu-env-1.5.0.part1of2": "https://x/p1"})


class PickReleaseTests(GpuEnvTestCase):
    def test_matching_latest(self):
        latest = {"tag_name": "v1.5.0", "assets_url": "https://api.github.com/.../382454766/assets"}
        self.assertIsNotNone(ge.pick_release(latest, "1.5.0"))

    def test_latest_mismatch(self):
        latest = {"tag_name": "v1.6.0", "assets_url": "https://x"}
        self.assertIsNone(ge.pick_release(latest, "1.5.0"))

    def test_malformed_latest(self):
        self.assertIsNone(ge.pick_release({"tag_name": None}, "1.5.0"))
        self.assertIsNone(ge.pick_release("junk", "1.5.0"))


class PathAndCacheTests(GpuEnvTestCase):
    def test_local_appdata_fallback_is_absolute(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("gpu_env.Path.home", return_value=Path(self._tmp.name)):
                self.assertEqual(
                    ge.user_base_dir(),
                    Path(self._tmp.name) / "AppData" / "Local" / "ShadowBuster",
                )

    def test_installed_info_checks_expected_release_identity(self):
        ge.env_dir().mkdir(parents=True)
        (ge.env_dir() / "python.exe").write_bytes(b"py")
        numpy_dir = ge.env_dir() / "Lib" / "site-packages" / "numpy"
        numpy_dir.mkdir(parents=True)
        (numpy_dir / "__init__.py").write_bytes(b"numpy")
        (numpy_dir / "core.pyd").write_bytes(b"pyd")
        ge.marker_path().write_text(json.dumps({
            "version": "1.5.0", "sha256": "a" * 64, "runtimeValidated": True,
        }), encoding="utf-8")
        self.assertIsNotNone(ge.installed_info(expected_version="1.5.0", expected_sha256="a" * 64))
        self.assertIsNone(ge.installed_info(expected_version="1.6.0"))
        self.assertIsNone(ge.installed_info(expected_version="1.5.0", expected_sha256="b" * 64))

    def test_valid_cached_file_checks_hash(self):
        p = Path(self._tmp.name) / "cache.zip"
        p.write_bytes(b"cache")
        digest = hashlib.sha256(b"cache").hexdigest()
        self.assertTrue(ge.valid_cached_file(p, 5, digest))
        self.assertFalse(ge.valid_cached_file(p, 5, "0" * 64))
        self.assertFalse(ge.valid_cached_file(p, 6, digest))

    def test_assert_runtime_files_requires_native_numpy(self):
        py = Path(self._tmp.name) / "env" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_bytes(b"py")
        numpy_dir = py.parent / "Lib" / "site-packages" / "numpy"
        numpy_dir.mkdir(parents=True)
        (numpy_dir / "__init__.py").write_bytes(b"numpy")
        with self.assertRaisesRegex(ValueError, "原生扩展"):
            ge.assert_runtime_files(py)
        (numpy_dir / "core.pyd").write_bytes(b"pyd")
        ge.assert_runtime_files(py)

    def test_revalidate_old_marker_writes_current_identity(self):
        ge.env_dir().mkdir(parents=True)
        py = ge.env_dir() / "python.exe"
        py.write_bytes(b"py")
        numpy_dir = ge.env_dir() / "Lib" / "site-packages" / "numpy"
        numpy_dir.mkdir(parents=True)
        (numpy_dir / "__init__.py").write_bytes(b"numpy")
        (numpy_dir / "core.pyd").write_bytes(b"pyd")
        ge.marker_path().write_text(json.dumps({"version": "1.5.0"}), encoding="utf-8")
        with mock.patch.object(ge, "validate_runtime", return_value="ok"):
            info = ge.revalidate_existing("1.5.0", "c" * 64)
        self.assertEqual(info["sha256"], "c" * 64)
        self.assertTrue(info["runtimeValidated"])


class ResumeOffsetTests(GpuEnvTestCase):
    def test_missing(self):
        self.assertEqual(ge.resume_offset(Path(self._tmp.name) / "nope", 100), 0)

    def test_partial(self):
        p = Path(self._tmp.name) / "part"
        p.write_bytes(b"12345")
        self.assertEqual(ge.resume_offset(p, 100), 5)

    def test_complete(self):
        p = Path(self._tmp.name) / "part"
        p.write_bytes(b"x" * 42)
        self.assertEqual(ge.resume_offset(p, 42), 42)

    def test_oversize(self):
        p = Path(self._tmp.name) / "part"
        p.write_bytes(b"x" * 50)
        with self.assertRaises(ValueError):
            ge.resume_offset(p, 42)


class AssembleZipTests(GpuEnvTestCase):
    def test_assembly_and_progress(self):
        p1 = Path(self._tmp.name) / "p1"; p1.write_bytes(b"A" * 1000)
        p2 = Path(self._tmp.name) / "p2"; p2.write_bytes(b"B" * 500)
        parts = [{"name": "p1", "size": 1000, "local": str(p1)},
                 {"name": "p2", "size": 500, "local": str(p2)}]
        dest = Path(self._tmp.name) / "out.zip"
        seen = []
        ge.assemble_zip(parts, dest, progress=lambda c, t: seen.append((c, t)))
        self.assertEqual(dest.read_bytes(), b"A" * 1000 + b"B" * 500)
        self.assertEqual(seen[-1], (1500, 1500))
        self.assertTrue(monotonic(seen))
        self.assertTrue(p1.exists(), "组装不应删除分卷（校验通过后由调用方清理）")

    def test_cancel_during_assembly(self):
        p1 = Path(self._tmp.name) / "p1"; p1.write_bytes(b"A" * 1000)
        parts = [{"name": "p1", "size": 1000, "local": str(p1)}]
        dest = Path(self._tmp.name) / "out.zip"
        calls = {"n": 0}
        with self.assertRaises(ge.DownloadCancelled):
            ge.assemble_zip(parts, dest, cancel=lambda: (calls.__setitem__("n", calls["n"] + 1), calls["n"] >= 2)[1])

    def test_missing_local_part(self):
        dest = Path(self._tmp.name) / "out.zip"
        with self.assertRaises(ValueError):
            ge.assemble_zip([{"name": "x", "size": 1, "local": str(Path(self._tmp.name) / "ghost")}], dest)


def monotonic(events):
    prev = -1
    for c, _ in events:
        if c < prev:
            return False
        prev = c
    return True


class ShaTests(GpuEnvTestCase):
    def test_sha256_of(self):
        p = Path(self._tmp.name) / "f"
        data = os.urandom(3 * 1024 * 1024 + 7)
        p.write_bytes(data)
        seen = []
        h = ge.sha256_of(p, progress=lambda c, t: seen.append((c, t)))
        self.assertEqual(h, hashlib.sha256(data).hexdigest())
        self.assertEqual(seen[-1], (len(data), len(data)))


class ExtractZipTests(GpuEnvTestCase):
    def test_extract(self):
        zpath = Path(self._tmp.name) / "a.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("top.txt", "hello")
            zf.writestr("sub/nested.bin", b"\x01" * 2048)
        dest = Path(self._tmp.name) / "out"
        seen = []
        ge.extract_zip(zpath, dest, progress=lambda c, t: seen.append((c, t)))
        self.assertEqual((dest / "top.txt").read_text(), "hello")
        self.assertEqual((dest / "sub" / "nested.bin").read_bytes(), b"\x01" * 2048)
        self.assertEqual(seen[-1], (5 + 2048, 5 + 2048))


class SwapEnvTests(GpuEnvTestCase):
    def test_swap_and_marker(self):
        staging = Path(self._tmp.name) / "staging"
        (staging / "sub").mkdir(parents=True)
        (staging / "python.exe").write_bytes(b"py")
        (staging / "sub" / "x").write_bytes(b"x")
        ge.swap_env(staging, "1.5.0", "a" * 64, runtime_validated=True)
        env = ge.env_dir()
        self.assertTrue((env / "python.exe").is_file())
        marker = json.loads(ge.marker_path().read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], "1.5.0")
        self.assertTrue(marker["runtimeValidated"])
        # 再次切换应清掉 env.old
        staging2 = Path(self._tmp.name) / "staging2"
        staging2.mkdir()
        (staging2 / "python.exe").write_bytes(b"py2")
        ge.swap_env(staging2, "1.5.1", "b" * 64, runtime_validated=True)
        self.assertFalse((ge.runner_dir() / "env.old").exists())

    def test_swap_rename_failure_restores_previous_env_and_marker(self):
        old_env = ge.env_dir()
        old_env.mkdir(parents=True)
        (old_env / "python.exe").write_bytes(b"old")
        ge.marker_path().write_text(json.dumps({
            "version": "1.4.0", "sha256": "a" * 64, "runtimeValidated": True,
        }), encoding="utf-8")
        staging = Path(self._tmp.name) / "staging"
        staging.mkdir()
        (staging / "python.exe").write_bytes(b"new")
        original_rename = ge.os.rename
        calls = {"n": 0}

        def fail_second_rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("marker busy")
            return original_rename(src, dst)

        with mock.patch.object(ge.os, "rename", side_effect=fail_second_rename):
            with self.assertRaises(OSError):
                ge.swap_env(staging, "1.5.0", "b" * 64, runtime_validated=True)
        self.assertEqual((ge.env_dir() / "python.exe").read_bytes(), b"old")
        self.assertEqual(json.loads(ge.marker_path().read_text(encoding="utf-8"))["version"], "1.4.0")

    def test_swap_marker_write_failure_restores_previous_env(self):
        old_env = ge.env_dir()
        old_env.mkdir(parents=True)
        (old_env / "python.exe").write_bytes(b"old")
        ge.marker_path().write_text(json.dumps({
            "version": "1.4.0", "sha256": "a" * 64, "runtimeValidated": True,
        }), encoding="utf-8")
        staging = Path(self._tmp.name) / "marker-staging"
        staging.mkdir()
        (staging / "python.exe").write_bytes(b"new")
        with mock.patch.object(ge, "_write_marker", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                ge.swap_env(staging, "1.5.0", "b" * 64, runtime_validated=True)
        self.assertEqual((ge.env_dir() / "python.exe").read_bytes(), b"old")
        marker = json.loads(ge.marker_path().read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], "1.4.0")

    def test_swap_rejects_bad_staging(self):
        staging = Path(self._tmp.name) / "bad-staging"
        staging.mkdir()
        with self.assertRaises(ValueError):
            ge.swap_env(staging, "1.5.0", "a" * 64, runtime_validated=True)


class InstalledInfoTests(GpuEnvTestCase):
    def test_none_without_marker(self):
        self.assertIsNone(ge.installed_info())

    def test_none_without_python(self):
        ge.marker_path().parent.mkdir(parents=True)
        ge.marker_path().write_text(json.dumps({"version": "1.5.0"}), encoding="utf-8")
        self.assertIsNone(ge.installed_info())

    def test_ok(self):
        ge.env_dir().mkdir(parents=True)
        (ge.env_dir() / "python.exe").write_bytes(b"py")
        numpy_dir = ge.env_dir() / "Lib" / "site-packages" / "numpy"
        numpy_dir.mkdir(parents=True)
        (numpy_dir / "__init__.py").write_bytes(b"numpy")
        (numpy_dir / "core.pyd").write_bytes(b"pyd")
        ge.marker_path().write_text(json.dumps({"version": "1.5.0", "sha256": "a" * 64, "runtimeValidated": True}), encoding="utf-8")
        info = ge.installed_info()
        self.assertEqual(info["version"], "1.5.0")

    def test_none_without_validation_marker(self):
        ge.env_dir().mkdir(parents=True)
        (ge.env_dir() / "python.exe").write_bytes(b"py")
        numpy_dir = ge.env_dir() / "Lib" / "site-packages" / "numpy"
        numpy_dir.mkdir(parents=True)
        (numpy_dir / "__init__.py").write_bytes(b"numpy")
        ge.marker_path().write_text(json.dumps({"version": "1.5.0", "sha256": "a" * 64}), encoding="utf-8")
        self.assertIsNone(ge.installed_info())

    def test_none_for_non_object_marker(self):
        ge.env_dir().mkdir(parents=True)
        (ge.env_dir() / "python.exe").write_bytes(b"py")
        ge.marker_path().write_text("null", encoding="utf-8")
        self.assertIsNone(ge.installed_info())


class FakeResp:
    def __init__(self, data):
        self._data = data
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if not self._data:
            return b""
        chunk = self._data[:n] if n > 0 else self._data
        self._data = self._data[len(chunk):]
        return chunk


class DownloadPartTests(GpuEnvTestCase):
    def test_fresh_download(self):
        dest = Path(self._tmp.name) / "part"
        payload = b"0123456789"
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)) as m:
            written = ge.download_part("https://x/p", dest, len(payload))
        self.assertEqual(written, len(payload))
        self.assertEqual(dest.read_bytes(), payload)
        req = m.call_args[0][0]
        self.assertNotIn("Range", req.headers)

    def test_resume_with_range(self):
        dest = Path(self._tmp.name) / "part"
        dest.write_bytes(b"0123")
        payload = b"456789"
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(payload)) as m:
            written = ge.download_part("https://x/p", dest, 10)
        self.assertEqual(written, 10)
        self.assertEqual(dest.read_bytes(), b"0123456789")
        req = m.call_args[0][0]
        self.assertEqual(req.headers["Range"], "bytes=4-")

    def test_complete_skips_network(self):
        dest = Path(self._tmp.name) / "part"
        dest.write_bytes(b"x" * 8)
        with mock.patch("urllib.request.urlopen") as m:
            written = ge.download_part("https://x/p", dest, 8)
        self.assertEqual(written, 0)
        m.assert_not_called()

    def test_size_mismatch_raises(self):
        dest = Path(self._tmp.name) / "part"
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"ab")):
            with self.assertRaises(ValueError):
                ge.download_part("https://x/p", dest, 100)

    def test_cancel_keeps_partial(self):
        dest = Path(self._tmp.name) / "part"
        calls = {"n": 0}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"0123456789")):
            with self.assertRaises(ge.DownloadCancelled):
                ge.download_part("https://x/p", dest, 10,
                                 cancel=lambda: (calls.__setitem__("n", calls["n"] + 1), calls["n"] >= 2)[1])
        self.assertEqual(dest.read_bytes(), b"0123456789", "取消后应保留已下载部分以便续传")


if __name__ == "__main__":
    unittest.main()