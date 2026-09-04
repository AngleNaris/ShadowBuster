"""GPU worker concurrency guards without starting a real download."""
import threading
import unittest
from unittest import mock

import main


class GpuConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.bridge = main.Bridge(None)
        self.events = []
        self.bridge.gpuStatus.connect(self.events.append)

    def tearDown(self):
        self.bridge._gpu_cancel.set()
        if self.bridge._gpu_lock.locked():
            self.bridge._gpu_lock.release()

    def test_check_reports_busy_while_gpu_operation_holds_lock(self):
        self.bridge._gpu_lock.acquire()
        self.bridge.checkGpuEnv()
        self.assertEqual(self.events, ['{"type": "busy", "op": "check"}'])

    def test_install_reports_busy_while_check_holds_lock(self):
        self.bridge._gpu_lock.acquire()
        self.assertFalse(self.bridge.startGpuInstall())
        self.assertEqual(self.events, ['{"type": "busy", "op": "install"}'])

    def test_processing_reports_busy_while_gpu_operation_holds_lock(self):
        self.bridge._gpu_lock.acquire()
        self.assertFalse(self.bridge.process({"files": ["input.wav"], "output": "out"}))
        self.assertEqual(self.events, ['{"type": "busy", "op": "process"}'])

    def test_processing_holds_lock_until_batch_finishes(self):
        started = threading.Event()
        release = threading.Event()

        def fake_run_batch(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return []

        with mock.patch.object(main.backend, "run_batch", side_effect=fake_run_batch), \
             mock.patch.object(main.backend, "auto_device", return_value="cpu"):
            self.assertTrue(self.bridge.process({"files": ["input.wav"], "output": "out"}))
            self.assertTrue(started.wait(1))
            self.assertFalse(self.bridge.startGpuInstall())
            release.set()
            self.bridge._thread.join(2)

        self.assertFalse(self.bridge._gpu_lock.locked())

    def test_check_releases_lock_when_thread_start_fails(self):
        class BrokenThread:
            def __init__(self, target, daemon=True):
                pass

            def start(self):
                raise RuntimeError("thread unavailable")

        with mock.patch.object(main.threading, "Thread", BrokenThread):
            self.bridge.checkGpuEnv()
        self.assertFalse(self.bridge._gpu_lock.locked())
        self.assertIn("检查启动失败", self.events[0])

    def test_check_releases_lock_when_worker_finishes(self):
        class InlineThread:
            def __init__(self, target, daemon=True):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(main.threading, "Thread", InlineThread):
            self.bridge.checkGpuEnv()
        self.assertFalse(self.bridge._gpu_lock.locked())
        self.assertIn('"type": "state"', self.events[0])


if __name__ == "__main__":
    unittest.main()
