import os
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import ffmpeg_tools


class MediaExecutableTests(unittest.TestCase):
    def test_current_path_takes_precedence(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ffmpeg_tools.shutil, "which", return_value="installed-ffmpeg") as which:
            self.assertEqual(ffmpeg_tools.media_executable("ffmpeg"), "installed-ffmpeg")
            which.assert_called_once_with("ffmpeg")

    def test_windows_installed_path_is_used_when_process_path_is_stale(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ffmpeg_tools, "_windows_install_path", return_value="updated-install-path"), \
                mock.patch.object(ffmpeg_tools.shutil, "which", side_effect=[None, "resolved-ffmpeg"]) as which:
            self.assertEqual(ffmpeg_tools.media_executable("ffmpeg"), "resolved-ffmpeg")
            self.assertIn("updated-install-path", which.call_args.kwargs["path"])

    def test_explicit_executable_is_used(self):
        with mock.patch.dict(os.environ, {"FFMPEG_BINARY": "custom-ffmpeg"}, clear=True), \
                mock.patch.object(ffmpeg_tools.shutil, "which", return_value="custom-path") as which:
            self.assertEqual(ffmpeg_tools.media_executable("ffmpeg"), "custom-path")
            which.assert_called_once_with("custom-ffmpeg")

    def test_invalid_override_does_not_silently_fall_back(self):
        with mock.patch.dict(os.environ, {"FFMPEG_BINARY": "missing"}, clear=True), \
                mock.patch.object(ffmpeg_tools.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "FFMPEG_BINARY does not point"):
                ffmpeg_tools.media_executable("ffmpeg")

    def test_missing_tool_has_actionable_error(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ffmpeg_tools, "_windows_install_path", return_value=""), \
                mock.patch.object(ffmpeg_tools.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Install FFmpeg.*FFPROBE_BINARY"):
                ffmpeg_tools.media_executable("ffprobe")

    def test_unsupported_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            ffmpeg_tools.media_executable("other")

    def test_non_windows_launch_does_not_access_windows_registry(self):
        with mock.patch.object(ffmpeg_tools.sys, "platform", "linux"):
            self.assertEqual(ffmpeg_tools._windows_install_path(), "")


class ExportClipTests(unittest.TestCase):
    def test_progress_failure_and_unknown_duration(self):
        for duration in (100, None):
            with self.subTest(duration=duration), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "clip.mp4"
                output.write_bytes(b"video")
                process = SimpleNamespace(
                    stdout=SimpleNamespace(readline=mock.AsyncMock(side_effect=[
                        b"out_time_us=50000000\n", b"out_time_us=N/A\n", b"",
                    ])),
                    wait=mock.AsyncMock(return_value=0), returncode=0,
                )
                steps = []
                with mock.patch.object(ffmpeg_tools.asyncio, "create_subprocess_exec",
                                       new=mock.AsyncMock(return_value=process)) as spawn, \
                        mock.patch.object(ffmpeg_tools, "monotonic", side_effect=[0, 11, 11]):
                    asyncio.run(ffmpeg_tools.encode_export_clip(
                        ["ffmpeg", "-y", str(output)], steps.append, "Part 1", duration,
                    ))
                self.assertIn("50%" if duration else "0.8 minutes", steps[0])
                self.assertIn("complete", steps[-1])
                self.assertIn("-nostdin", spawn.call_args.args)
                self.assertIn("pipe:1", spawn.call_args.args)

    def test_ffmpeg_errors_and_missing_output_are_explicit(self):
        for code in (0, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                process = SimpleNamespace(
                    stdout=SimpleNamespace(readline=mock.AsyncMock(return_value=b"")),
                    wait=mock.AsyncMock(return_value=code), returncode=code,
                )
                with mock.patch.object(ffmpeg_tools.asyncio, "create_subprocess_exec",
                                       new=mock.AsyncMock(return_value=process)):
                    with self.assertRaisesRegex(RuntimeError, "did not produce" if code == 0 else "failed"):
                        asyncio.run(ffmpeg_tools.encode_export_clip(
                            ["ffmpeg", str(Path(directory) / "clip.mp4")], lambda _: None, "Part 1",
                        ))

    def test_cancelled_encoder_is_terminated_and_reaped(self):
        process = SimpleNamespace(
            stdout=SimpleNamespace(readline=mock.AsyncMock(side_effect=asyncio.CancelledError)),
            wait=mock.AsyncMock(), returncode=None, kill=mock.Mock(),
        )
        with mock.patch.object(ffmpeg_tools.asyncio, "create_subprocess_exec",
                               new=mock.AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(ffmpeg_tools.encode_export_clip(["ffmpeg", "clip.mp4"], lambda _: None, "Part 1"))
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
