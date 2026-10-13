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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from typing import Optional

from caption_exports import render_selectable_captions, validate_captions, caption_filename
from export_jobs import ExportJobStore
from ffmpeg_tools import media_executable
from processors import transcript_versions


class CaptionTrackTests(unittest.TestCase):
    def test_job_persists_browser_tracks_after_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ExportJobStore(Path(temporary) / "jobs")
            workspaces = []
            async def generate(video_id, options, step, artifacts, workspace):
                workspaces.append(workspace)
                output = workspace / "copy.mp4"
                output.write_bytes(b"video")
                (workspace / "track-0.vtt").write_text("WEBVTT\n\n")
                return FileResponse(output)
            job_id = store.start("video", {}, generate, caption_tracks=[{"label": "English"}])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = store.read(job_id)
                if job["status"] in {"complete", "error"}:
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "complete", job)
            self.assertEqual(job["caption_tracks"][0]["label"], "English")
            self.assertEqual(store.file(job_id, "track-0.vtt").read_text(), "WEBVTT\n\n")
            self.assertFalse(workspaces[0].exists())

    def test_real_mux_preserves_media_tracks_and_clip_subtitles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            subprocess.run([
                media_executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=320x180:r=10:d=3", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=3", "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(source),
            ], check=True)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            tracks = [
                {"label": "English transcript", "language": "eng",
                 "captions": validate_captions([{"start": 0, "duration": 3, "text": "Hello"}])},
                {"label": "German translation", "language": "deu",
                 "captions": validate_captions([{"start": 0, "duration": 3, "text": "Hallo"}])},
            ]
            workspace = root / "workspace"
            workspace.mkdir()
            response = asyncio.run(render_selectable_captions(
                source, tracks, "Lecture_EN_DE", workspace, lambda message: None))

            def probe(path):
                return json.loads(subprocess.run([
                    media_executable("ffprobe"), "-v", "error", "-show_streams",
                    "-show_format", "-of", "json", str(path),
                ], check=True, capture_output=True, text=True).stdout)

            result = probe(response.path)
            subtitles = [stream for stream in result["streams"] if stream["codec_type"] == "subtitle"]
            self.assertEqual([stream["codec_name"] for stream in subtitles], ["mov_text", "mov_text"])
            self.assertEqual([stream["tags"]["language"] for stream in subtitles], ["eng", "deu"])
            self.assertEqual([stream["tags"]["handler_name"] for stream in subtitles],
                             [track["label"] for track in tracks])
            self.assertEqual([stream["disposition"]["default"] for stream in subtitles], [1, 0])
            self.assertAlmostEqual(float(result["format"]["duration"]), 3, delta=0.1)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
            for stream in ("v:0", "a:0"):
                def media_hash(path):
                    return subprocess.run([
                        media_executable("ffmpeg"), "-v", "error", "-i", str(path),
                        "-map", stream, "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
                    ], check=True, capture_output=True).stdout
                self.assertEqual(media_hash(source), media_hash(response.path))
            self.assertIn("WEBVTT", (workspace / "track-1.vtt").read_text())
            clip = root / "clip.mp4"
            subprocess.run([
                media_executable("ffmpeg"), "-v", "error", "-ss", "1", "-i", str(response.path),
                "-t", "1", "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                "-c:v", "libx264", "-c:a", "aac", "-c:s", "mov_text", str(clip),
            ], check=True)
            clip_probe = probe(clip)
            self.assertAlmostEqual(float(clip_probe["format"]["duration"]), 1, delta=0.15)
            clipped = [s for s in clip_probe["streams"] if s["codec_type"] == "subtitle"]
            self.assertEqual([s["tags"]["language"] for s in clipped], ["eng", "deu"])

    def test_routes_snapshot_versions_and_reject_other_video_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = ExportJobStore(root / "jobs")
            captured = []
            with patch.object(transcript_versions, "TRANSCRIPTS_DIR", root / "transcripts"), \
                    patch.object(transcript_versions, "SUMMARIES_DIR", root / "summaries"):
                one = transcript_versions.save_version("video", "transcript",
                    [{"start": 0, "duration": 2, "text": "Original"}], source="uploaded")
                two = transcript_versions.save_version("video", "translation",
                    [{"start": 0, "duration": 2, "text": "Hallo"}], language="de")
                transcript_versions.set_active_source("video", "transcript", source="uploaded")
                app = FastAPI()
                source = Path(__file__).resolve().parents[1] / "main.py"
                names = {"start_caption_track_job", "caption_copy_path", "stream_caption_copy",
                         "caption_copy_track", "read_export_job"}
                functions = [node for node in ast.parse(source.read_text(encoding="utf-8")).body
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]

                async def renderer(*args):
                    captured.append(args)

                namespace = dict(app=app, Request=Request, Header=Header, Optional=Optional,
                    HTTPException=HTTPException, FileResponse=FileResponse,
                    video_processor=SimpleNamespace(get_video_path=lambda value: "source.mp4" if value == "video" else None),
                    transcript_versions=transcript_versions, validate_captions=validate_captions,
                    caption_filename=caption_filename, detect_transcript_language=lambda captions: "en",
                    render_selectable_captions=renderer, export_jobs=jobs)
                exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
                client = TestClient(app)
                payload = {"version_ids": [one["id"], two["id"]], "filename": "Lecture.mp4"}
                with patch.object(jobs, "start", return_value="a" * 32) as start:
                    response = client.post("/caption_track_jobs/video", json=payload)
                    self.assertEqual(response.status_code, 200)
                    generate = start.call_args.args[2]
                    asyncio.run(generate("video", {}, lambda message: None, None, root))
                    self.assertEqual([track["language"] for track in captured[0][1]], ["eng", "deu"])
                    self.assertEqual(captured[0][2], "Lecture_EN_DE")
                    self.assertEqual(transcript_versions.get_active_source("video")["source"], "uploaded")
                    for ids in ([], [one["id"], one["id"]], ["missing"], [True], ["x"] * 17):
                        self.assertEqual(client.post("/caption_track_jobs/video",
                            json={"version_ids": ids}).status_code, 400)
                job_id = "b" * 32
                directory = jobs._directory(job_id)
                directory.mkdir(parents=True)
                (directory / "download.mp4").write_bytes(b"video")
                (directory / "track-0.vtt").write_text("WEBVTT\n\n")
                state = dict(id=job_id, video_id="video", status="complete", instance=jobs.instance,
                             created_at=time.time(), title="Captioned video - Tracks", filename="copy.mp4",
                             download_file="download.mp4", caption_tracks=[{"language": "eng"}])
                jobs._write(job_id, state)
                self.assertEqual(namespace["caption_copy_path"]("video", job_id), directory / "download.mp4")
                self.assertEqual(client.get(f"/caption_copy/video/{job_id}/tracks/0").status_code, 200)
                self.assertEqual(client.get(f"/caption_copy/other/{job_id}/tracks/0").status_code, 400)
                self.assertEqual(client.get(f"/caption_copy/video/{job_id}/tracks/1").status_code, 404)
                state["status"] = "running"
                jobs._write(job_id, state)
                with self.assertRaises(HTTPException):
                    namespace["caption_copy_path"]("video", job_id)


if __name__ == "__main__":
    unittest.main()
