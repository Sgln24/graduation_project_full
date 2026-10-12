import ast
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from caption_exports import caption_text, caption_filename, validate_captions, render_captioned_video, video_duration
from export_jobs import ExportJobStore
from ffmpeg_tools import media_executable


class CaptionTests(unittest.TestCase):
    def test_formats_timing_unicode_and_missing_durations(self):
        captions = validate_captions([
            {"start": 2, "duration": 20, "text": "Last & <text>"},
            {"start": 0, "text": "\u0645\u0631\u062d\u0628\u0627"},
            {"start": 1.2345, "duration": 0, "text": "Second"},
            {"start": 10, "duration": 1, "text": "Outside"},
        ])
        srt = caption_text(captions, 4)
        self.assertIn("00:00:00,000 --> 00:00:01,234", srt)
        self.assertIn("\u0645\u0631\u062d\u0628\u0627", srt)
        self.assertIn("00:00:02,000 --> 00:00:04,000", srt)
        self.assertIn("Last &amp; &lt;text&gt;", srt)
        self.assertNotIn("Outside", srt)
        vtt = caption_text(captions, 4, "vtt")
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:01.234 --> 00:00:02.000", vtt)
        self.assertNotIn("00:00:01,234", vtt)

    def test_validation_rejects_invalid_input_and_outside_cues(self):
        for transcript in (
            None, [], [{}], [{"start": 0, "text": " "}],
            [{"start": -1, "text": "Bad"}], [{"start": float("nan"), "text": "Bad"}],
            [{"start": True, "text": "Bad"}], [{"start": "0", "text": "Bad"}],
            [{"start": 0, "duration": -2, "text": "Bad"}],
        ):
            with self.subTest(transcript=transcript), self.assertRaises(ValueError):
                validate_captions(transcript)
        with self.assertRaisesRegex(ValueError, "within"):
            caption_text(validate_captions([{"start": 10, "text": "Outside"}]), 3)
        with self.assertRaisesRegex(ValueError, "format"):
            caption_text(validate_captions([{"start": 0, "text": "Text"}]), 3, "txt")
        self.assertNotIn("/", caption_filename("../lecture.mp4"))
        self.assertNotIn("\\", caption_filename(r"..\lecture.mp4"))

    def test_caption_routes_capture_snapshot_without_activating_version(self):
        app = FastAPI()
        source = Path(__file__).resolve().parents[1] / "main.py"
        functions = [
            node for node in ast.parse(source.read_text(encoding="utf-8")).body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"start_caption_video_job", "download_caption_file"}
        ]
        captured = []

        class Jobs:
            def start(self, video_id, options, generate):
                captured.append((video_id, options, generate))
                return "a" * 32

        async def duration(path):
            return 3

        async def render(*args):
            return args

        namespace = {
            "app": app, "Request": Request, "HTTPException": HTTPException,
            "StreamingResponse": StreamingResponse, "validate_captions": validate_captions,
            "caption_text": caption_text, "caption_filename": caption_filename,
            "video_duration": duration, "render_captioned_video": render,
            "video_processor": SimpleNamespace(get_video_path=lambda video_id: "/original.mp4" if video_id == "video" else None),
            "export_jobs": Jobs(),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        client = TestClient(app)
        payload = {"transcript": [{"start": 0, "duration": 1, "text": "Chosen translation"}],
                   "filename": "lecture.mp4", "transcript_label": "German"}
        response = client.post("/caption_video_jobs/video", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "a" * 32)
        args = asyncio.run(captured[0][2]("video", {}, lambda message: None, None, Path("workspace")))
        self.assertEqual(args[1][0]["text"], "Chosen translation")
        payload["transcript"][0]["text"] = "Changed later"
        self.assertEqual(args[1][0]["text"], "Chosen translation")
        for format in ("srt", "vtt"):
            response = client.post(f"/caption_file/video/{format}", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"lecture.{format}", response.headers["content-disposition"])
            self.assertIn("Changed later", response.text)
        self.assertEqual(client.post("/caption_file/video/txt", json=payload).status_code, 400)
        self.assertEqual(client.post("/caption_file/missing/srt", json=payload).status_code, 404)
        self.assertEqual(client.post("/caption_video_jobs/video", json={"transcript": []}).status_code, 400)
        self.assertEqual(client.post("/caption_video_jobs/video", json=[]).status_code, 400)

    def test_real_video_burns_captions_preserves_original_and_saves_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp4"
            ffmpeg = media_executable("ffmpeg")
            subprocess.run([
                ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10:d=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(original),
            ], check=True, capture_output=True)
            digest = hashlib.sha256(original.read_bytes()).digest()
            captions = validate_captions([
                {"start": 0, "duration": 1.5, "text": "{\\an8}Chosen captions"},
                {"start": 1.5, "duration": 0.5, "text": "\u0645\u0631\u062d\u0628\u0627"},
            ])
            store = ExportJobStore(root / "jobs")

            async def generate(video_id, options, step, artifacts, workspace):
                return await render_captioned_video(original, captions, "lecture.mp4", workspace, step)

            job_id = store.start("video", {"document_title": "Captioned video - Chosen"}, generate)
            for _ in range(300):
                state = store.read(job_id)
                if state["status"] in {"complete", "error"}:
                    break
                time.sleep(0.1)
            self.assertEqual(state["status"], "complete", state)
            output = store.file(job_id, state["download_file"])
            self.assertAlmostEqual(asyncio.run(video_duration(output)), 3, delta=0.3)
            self.assertEqual(hashlib.sha256(original.read_bytes()).digest(), digest)

            def frame(at):
                result = subprocess.run([
                    ffmpeg, "-v", "error", "-ss", str(at), "-i", str(output),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
                ], check=True, capture_output=True)
                return result.stdout

            self.assertGreater(sum(value > 100 for value in frame(0.5)), 100)
            self.assertEqual(sum(value > 100 for value in frame(0.5)[:320 * 60]), 0,
                             "Transcript text must not inject subtitle alignment overrides")
            self.assertGreater(sum(value > 100 for value in frame(1.7)), 30)
            self.assertEqual(sum(value > 100 for value in frame(2.5)), 0)
            probe = subprocess.run([
                media_executable("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(output),
            ], capture_output=True, text=True, check=True)
            self.assertIn("audio", [stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]])
            restarted = ExportJobStore(root / "jobs")
            self.assertEqual(restarted.read(job_id)["status"], "complete")
            self.assertTrue(restarted.file(job_id, state["download_file"]).is_file())

    def test_encoder_failure_is_explicit(self):
        async def duration(path):
            return 3

        class FailedProcess:
            returncode = 1
            stdout = SimpleNamespace(readline=None)

            async def wait(self):
                return 1

        async def empty_line():
            return b""

        FailedProcess.stdout.readline = empty_line

        async def start(*args, **kwargs):
            kwargs["stderr"].write(b"No such filter: subtitles")
            return FailedProcess()

        with tempfile.TemporaryDirectory() as directory, \
                patch("caption_exports.video_duration", duration), \
                patch("caption_exports.media_executable", return_value="ffmpeg"), \
                patch("caption_exports.asyncio.create_subprocess_exec", start):
            with self.assertRaisesRegex(RuntimeError, "No such filter"):
                asyncio.run(render_captioned_video(
                    "source.mp4", validate_captions([{"start": 0, "text": "Caption"}]),
                    "lecture", directory, lambda message: None,
                ))


if __name__ == "__main__":
    unittest.main()
