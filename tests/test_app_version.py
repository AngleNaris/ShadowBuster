"""版本号单一来源校验：studio_backend.APP_VERSION 必须与安装器 MyAppVersion 一致。"""
import re
import unittest
from pathlib import Path

import studio_backend

ROOT = Path(studio_backend.__file__).parent


class AppVersionTests(unittest.TestCase):
    def test_installer_version_matches_backend(self):
        iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        m = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss)
        self.assertIsNotNone(m, "installer.iss 缺少 MyAppVersion 定义")
        self.assertEqual(m.group(1), studio_backend.APP_VERSION)

    def test_version_format(self):
        self.assertRegex(studio_backend.APP_VERSION, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
