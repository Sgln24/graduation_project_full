import ast
import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from fastapi.testclient import TestClient

from caption_exports import (
    caption_text, caption_filename, validate_captions, render_captioned_video, video_duration,
    validate_caption_style, caption_filter, burn_caption_chunks, render_caption_preview,
)
from PIL import Image
from io import BytesIO
from export_jobs import ExportJobStore, export_workspace
from ffmpeg_tools import media_executable


class CaptionTests(unittest.TestCase):
    def test_style_validation_and_two_line_chunks_preserve_words_and_time(self):
        for options in ([], {"preset": "unknown"}, {"position": "middle"}, {"size": 3},
                        {"preset": "box',FontSize=999"}, {"size": "xl"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_caption_style(options)
        self.assertEqual(validate_caption_style(), {"preset": "box", "size": "medium", "position": "bottom"})
        text = " ".join(f"word{index}" for index in range(100))
        captions = validate_captions([{"start": 2, "duration": 20, "text": text}])
        for size in ("small", "medium", "large"):
            chunks = burn_caption_chunks(captions, 30, validate_caption_style({"size": size}))
            self.assertEqual(" ".join(chunk["text"].replace("\n", " ") for chunk in chunks), text)
            self.assertTrue(all(len(chunk["text"].splitlines()) <= 2 for chunk in chunks))
            self.assertEqual(chunks[0]["start"], 2)
            self.assertAlmostEqual(chunks[-1]["start"] + chunks[-1]["duration"], 22)
            for before, after in zip(chunks, chunks[1:]):
                self.assertAlmostEqual(before["start"] + before["duration"], after["start"])
        self.assertIn("Alignment=6", caption_filter(validate_caption_style({"position": "top"})))
        self.assertIn("PrimaryColour=&H0000FFFF", caption_filter(validate_caption_style({"preset": "yellow"})))
        self.assertIn("BorderStyle=1", caption_filter(validate_caption_style({"preset": "outline"})))
        self.assertIn("BorderStyle=3", caption_filter(validate_caption_style({"preset": "box"})))

    def test_burned_and_sidecar_cues_share_missing_duration_handling(self):
        captions = validate_captions([
            {"start": 0, "text": "First"},
            {"start": 0, "duration": 0, "text": "Same time"},
            {"start": 2, "text": "Last"},
        ])
        chunks = burn_caption_chunks(captions, 4, validate_caption_style())
        self.assertEqual([item["text"] for item in chunks], ["First", "Same time", "Last"])
        self.assertEqual([item["duration"] for item in chunks], [2, 2, 2])
        self.assertEqual(caption_text(captions, 4), caption_text(chunks, 4))

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
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name in {"start_caption_video_job", "download_caption_file", "caption_preview", "saved_caption_videos", "detect_transcript_language"}
        ]
        captured = []

        class Jobs:
            def list(self):
                return [
                    {"title": "Captioned video - German", "video_id": "other", "status": "complete", "download_url": "/export_jobs/old/download"},
                    {"title": "Chapter export", "video_id": "video", "status": "complete"},
                    {"title": "Captioned video - English", "video_id": "video", "status": "running"},
                ]

            def start(self, video_id, options, generate):
                captured.append((video_id, options, generate))
                return "a" * 32

        async def duration(path):
            return 3

        async def render(*args):
            return args

        async def preview(*args):
            return b"preview PNG"

        namespace = {
            "app": app, "Request": Request, "HTTPException": HTTPException,
            "StreamingResponse": StreamingResponse, "validate_captions": validate_captions,
            "Response": Response, "export_workspace": export_workspace, "render_caption_preview": preview,
            "caption_text": caption_text, "caption_filename": caption_filename,
            "video_duration": duration, "render_captioned_video": render,
            "validate_caption_style": validate_caption_style,
            "re": re,
            "video_processor": SimpleNamespace(get_video_path=lambda video_id: "/original.mp4" if video_id == "video" else None),
            "export_jobs": Jobs(),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        client = TestClient(app)
        history = client.get("/saved_caption_videos")
        self.assertEqual(history.status_code, 200)
        self.assertEqual([job["video_id"] for job in history.json()["exports"]], ["other", "video"])
        self.assertEqual(history.json()["exports"][0]["download_url"], "/export_jobs/old/download")
        payload = {"transcript": [{"start": 0, "duration": 1, "text": "Chosen translation"}],
                   "filename": "lecture.mp4", "transcript_label": "German"}
        response = client.post("/caption_video_jobs/video", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["job_id"], "a" * 32)
        args = asyncio.run(captured[0][2]("video", {}, lambda message: None, None, Path("workspace")))
        self.assertEqual(args[1][0]["text"], "Chosen translation")
        self.assertEqual(args[-1], validate_caption_style())
        self.assertEqual(args[2], "lecture_EN.mp4")
        for language in ("de", "EN", "ar", "pl", "fr"):
            response = client.post("/caption_video_jobs/video", json={**payload, "caption_language": language})
            self.assertEqual(response.status_code, 200)
            export_args = asyncio.run(captured[-1][2]("video", {}, lambda message: None, None, Path("workspace")))
            self.assertEqual(export_args[2], f"lecture_{language.upper()}.mp4")
        long_name = "a" * 140 + ".mp4"
        client.post("/caption_video_jobs/video", json={**payload, "filename": long_name, "caption_language": "de"})
        export_args = asyncio.run(captured[-1][2]("video", {}, lambda message: None, None, Path("workspace")))
        self.assertTrue(caption_filename(export_args[2]).endswith("_DE"))
        for invalid in ("German", "../de", 123, True):
            self.assertEqual(client.post("/caption_video_jobs/video", json={**payload, "caption_language": invalid}).status_code, 400)
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
        self.assertEqual(client.post("/caption_video_jobs/video", json={**payload, "style": {"preset": "invalid"}}).status_code, 400)
        response = client.post("/caption_preview/video", json={**payload, "at": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.content, b"preview PNG")
        self.assertEqual(client.post("/caption_preview/video", json={**payload, "at": -1}).status_code, 400)
        self.assertEqual(client.post("/caption_preview/missing", json=payload).status_code, 404)

    def test_real_video_burns_captions_preserves_original_and_saves_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp4"
            ffmpeg = media_executable("ffmpeg")
            subprocess.run([
                ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x000050:s=640x360:r=10:d=3",
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
                return await render_captioned_video(original, captions, "lecture_EN.mp4", workspace, step)

            job_id = store.start("video", {"document_title": "Captioned video - Chosen"}, generate)
            for _ in range(300):
                state = store.read(job_id)
                if state["status"] in {"complete", "error"}:
                    break
                time.sleep(0.1)
            self.assertEqual(state["status"], "complete", state)
            output = store.file(job_id, state["download_file"])
            self.assertEqual(state["filename"], "lecture_EN caption.mp4")
            self.assertAlmostEqual(asyncio.run(video_duration(output)), 3, delta=0.3)
            self.assertEqual(hashlib.sha256(original.read_bytes()).digest(), digest)

            def frame(at):
                result = subprocess.run([
                    ffmpeg, "-v", "error", "-ss", str(at), "-i", str(output),
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
                ], check=True, capture_output=True)
                return result.stdout

            self.assertGreater(sum(value > 100 for value in frame(0.5)), 100)
            self.assertEqual(sum(value > 100 for value in frame(0.5)[:640 * 120]), 0,
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
            preview_root = root / "preview"
            preview_root.mkdir()
            images = {}
            for preset in ("box", "outline", "yellow"):
                for position in ("top", "bottom"):
                    for size in ("small", "medium", "large"):
                        with self.subTest(preset=preset, position=position, size=size):
                            style = validate_caption_style({"preset": preset, "position": position, "size": size})
                            data = asyncio.run(render_caption_preview(original, captions, 0.5, style, preview_root))
                            image = Image.open(BytesIO(data)).convert("RGB")
                            self.assertEqual(image.size, (640, 360))
                            pixels = list(image.getdata())
                            top = sum(max(pixel) > 100 for pixel in pixels[:640 * 120])
                            bottom = sum(max(pixel) > 100 for pixel in pixels[640 * 240:])
                            self.assertGreater(top if position == "top" else bottom, 100)
                            self.assertEqual(bottom if position == "top" else top, 0)
                            images[preset, position, size] = data
                            if preset == "yellow":
                                self.assertGreater(sum(r > 100 and g > 100 and b < 70 for r, g, b in pixels), 50)
            self.assertTrue(images["box", "top", "large"] != images["outline", "top", "large"],
                            "Box must differ from outline on a colored frame")
            self.assertTrue(images["box", "top", "large"] != images["box", "top", "small"],
                            "Font size must affect actual rendered frames")

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
            self.assertEqual(args[args.index("-preset") + 1], "superfast")
            self.assertEqual(args[args.index("-crf") + 1], "20")
            self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")
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
