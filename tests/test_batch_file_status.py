from __future__ import annotations

import unittest
from unittest.mock import patch

import studio_backend


class BatchFileStatusTests(unittest.TestCase):
    def test_reports_each_file_result_immediately_and_continues_after_failure(self):
        progress_events = []
        finished_events = []

        def fake_pipeline(source, output_dir, *, progress, **kwargs):
            progress(0, 0.25, "高频重建")
            if source.name == "broken.wav":
                raise studio_backend.PipelineError("测试失败")
            progress(3, 1.0, "Soren 母带完成")
            return f"{output_dir}/{source.stem}_shadowbuster.wav"

        with patch.object(studio_backend, "run_pipeline", side_effect=fake_pipeline):
            results = studio_backend.run_batch(
                ["first.wav", "broken.wav", "last.wav"],
                "output",
                progress=lambda *args: progress_events.append(args),
                file_finished=lambda *args: finished_events.append(args),
            )

        self.assertEqual(
            finished_events,
            [
                (0, 3, "first.wav", True, ""),
                (1, 3, "broken.wav", False, "测试失败"),
                (2, 3, "last.wav", True, ""),
            ],
        )
        self.assertEqual([event[0] for event in progress_events], [0, 0, 0, 1, 1, 2, 2, 2])
        self.assertEqual([result[2] for result in results], [None, "测试失败", None])

    def test_cancelled_file_is_not_reported_as_failed(self):
        finished_events = []
        cancel_checks = iter((False, True))

        with patch.object(
            studio_backend,
            "run_pipeline",
            side_effect=studio_backend.PipelineError("用户取消"),
        ):
            with self.assertRaisesRegex(studio_backend.PipelineError, "用户取消"):
                studio_backend.run_batch(
                    ["cancelled.wav"],
                    "output",
                    file_finished=lambda *args: finished_events.append(args),
                    cancel=lambda: next(cancel_checks),
                )

        self.assertEqual(finished_events, [])

    def test_reports_unexpected_exception_as_failed_file(self):
        finished_events = []

        with patch.object(studio_backend, "run_pipeline", side_effect=ValueError("bad input")):
            results = studio_backend.run_batch(
                ["broken.wav"],
                "output",
                file_finished=lambda *args: finished_events.append(args),
            )

        self.assertEqual(
            finished_events,
            [(0, 1, "broken.wav", False, "ValueError: bad input")],
        )
        self.assertEqual(results[0][2], "ValueError: bad input")


if __name__ == "__main__":
    unittest.main()
