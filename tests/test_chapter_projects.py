import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import chapter_projects
from export_jobs import ExportJobStore
from processors import transcript_versions


class ChapterProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("VIDEO_DIR", "TRANSCRIPTS_DIR", "SUMMARIES_DIR", "SCENES_DIR",
                     "THUMBNAILS_DIR", "FULLSIZE_IMAGES_DIR"):
            directory = self.root / name
            directory.mkdir()
            setattr(self, name, directory)
            patch = mock.patch.object(chapter_projects, name, directory)
            patch.start()
            self.addCleanup(patch.stop)
            if hasattr(transcript_versions, name):
                patch = mock.patch.object(transcript_versions, name, directory)
                patch.start()
                self.addCleanup(patch.stop)
        patch = mock.patch.object(chapter_projects, "jobs", ExportJobStore(self.root / "jobs"))
        patch.start()
        self.addCleanup(patch.stop)
        self.chapters = [{"timestamp": "00:02", "title": "Chapter one"}, {"timestamp": "00:06", "title": "Chapter two"}]
        self.transcript = [
            {"start": 0, "duration": 1, "text": "Before."},
            {"start": 1, "duration": 3, "end": 4, "text": "Overlapping.", "speaker": "A",
             "words": [{"start": 1, "end": 2.5, "word": "Overlapping."}]},
            {"start": 4, "duration": 4, "text": "Ends later.", "speaker": "B"},
            {"start": 8, "duration": 2, "text": "After."},
        ]
        self.scenes = [
            {"time_seconds": 0, "timestamp": "00:00", "duration": 4, "title": "Carry-in",
             "yolo_detections": {"success": True, "detections": [{"ocr_text": "Saved OCR"}]}},
            {"time_seconds": 4, "timestamp": "00:04", "duration": 2, "title": "Next"},
            {"time_seconds": 6, "timestamp": "00:06", "duration": 4, "title": "Outside"},
        ]
        self.write(self.SUMMARIES_DIR / "parent.json", self.chapters)
        self.write(self.TRANSCRIPTS_DIR / "parent_whisper.json", self.transcript)
        self.write(self.TRANSCRIPTS_DIR / "parent_whisper_meta.json", {"model": "turbo", "speaker_names": {"A": "Alice"}})
        self.write(self.SCENES_DIR / "parent.json", self.scenes)
        (self.VIDEO_DIR / "parent.mp4").write_bytes(b"source video")
        for directory in (self.THUMBNAILS_DIR, self.FULLSIZE_IMAGES_DIR):
            (directory / "parent").mkdir()
            for index in range(3):
                Image.new("RGB", (16, 9), "white").save(directory / "parent" / f"{index}.jpg")
        self.data = {"chapter_index": 0, "timestamp": "00:02", "title": "Chapter one", "next_timestamp": "00:06"}
        self.commands = []

        async def encode(command, step, label, duration):
            self.commands.append(command)
            Path(command[-1]).write_bytes(b"encoded chapter")
        self.encode = encode
        self.real_extract = chapter_projects.extract_caption_tracks
        patch = mock.patch.object(chapter_projects, "extract_caption_tracks", new=mock.AsyncMock(return_value=[]))
        patch.start()
        self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(chapter_projects.router)
        self.app = app

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, data=None, duration=10):
        snapshot = chapter_projects._snapshot("parent", data or self.data)
        with mock.patch.object(chapter_projects, "video_duration", new=mock.AsyncMock(return_value=duration)), \
                mock.patch.object(chapter_projects, "encode_export_clip", side_effect=self.encode), \
                mock.patch.object(chapter_projects, "media_executable", return_value="ffmpeg"):
            return asyncio.run(chapter_projects.create_project(snapshot, "child", lambda _: None))

    def test_project_is_independent_with_relative_transcript_slides_and_ocr(self):
        originals = {path: path.read_bytes() for directory in (
            self.TRANSCRIPTS_DIR, self.SUMMARIES_DIR, self.SCENES_DIR, self.VIDEO_DIR,
        ) for path in directory.iterdir() if path.is_file()}
        result = self.create()
        self.assertEqual(result["duration"], 4)
        self.assertEqual(result["slides"], 2)
        self.assertEqual(result["transcript_segments"], 2)
        cues = self.read(self.TRANSCRIPTS_DIR / "child_whisper.json")
        self.assertEqual([(item["start"], item["duration"]) for item in cues], [(0, 2), (2, 2)])
        self.assertEqual(cues[0]["end"], 2)
        self.assertEqual(cues[0]["speaker"], "A")
        self.assertEqual(cues[0]["words"][0]["start"], 0)
        self.assertEqual(cues[0]["words"][0]["end"], 0.5)
        slides = self.read(self.SCENES_DIR / "child.json")
        self.assertEqual([scene["time_seconds"] for scene in slides], [0, 2])
        self.assertEqual([scene["duration"] for scene in slides], [2, 2])
        self.assertEqual(slides[0]["yolo_detections"], self.scenes[0]["yolo_detections"])
        self.assertEqual(slides[1]["fullsize"], "/fullsize_images/child/1.jpg")
        self.assertTrue((self.FULLSIZE_IMAGES_DIR / "child" / "0.jpg").is_file())
        self.assertFalse((self.SUMMARIES_DIR / "child.json").exists(), "New project can generate fresh chapters")
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original, f"Parent content changed: {path}")
        metadata = self.read(self.VIDEO_DIR / "metadata.json")["child"]
        self.assertEqual(metadata["chapter_project"]["parent_video_id"], "parent")
        self.assertEqual(metadata["filename"], "Chapter one.mp4")
        self.assertTrue((self.VIDEO_DIR / "child.mp4").is_file())
        command = self.commands[0]
        self.assertEqual(command[command.index("-ss") + 1], "2.0")
        self.assertEqual(command[command.index("-t") + 1], "4.0")
        self.assertIn("0:a?", command)
        self.assertFalse((self.VIDEO_DIR / "child.pending").exists())

    def test_last_chapter_ends_at_video_duration(self):
        data = {"chapter_index": 1, "timestamp": "00:06", "title": "Chapter two", "next_timestamp": None}
        result = self.create(data)
        self.assertEqual(result["duration"], 4)
        slides = self.read(self.SCENES_DIR / "child.json")
        self.assertEqual([scene["title"] for scene in slides], ["Outside"])

    def test_caption_copy_is_the_project_source_and_subtitle_streams_are_preserved(self):
        store = ExportJobStore(self.root / "exports" / "jobs")
        job = "b" * 32
        store._write(job, {"id": job, "video_id": "parent", "instance": store.instance, "status": "complete",
                          "created_at": 1, "title": "Captioned video - English",
                          "filename": "English.mp4", "download_file": "download.mp4"})
        captioned = store._directory(job) / "download.mp4"
        captioned.write_bytes(b"captioned copy")
        with mock.patch.object(chapter_projects, "EXPORTS_DIR", self.root / "exports"):
            snapshot = chapter_projects._snapshot("parent", {**self.data, "media_export_id": job})
            self.assertEqual(snapshot["video_path"], captioned)
            self.create({**self.data, "media_export_id": job})
        self.assertIn(str(captioned), self.commands[0])
        self.assertIn("0:s?", self.commands[0])
        self.assertIn("mov_text", self.commands[0])
        self.assertEqual(chapter_projects.chapter_captions("child")["tracks"], [])
        with mock.patch.object(chapter_projects, "EXPORTS_DIR", self.root / "exports"):
            state = store.read(job)
            state["video_id"] = "unrelated"
            store._write(job, state)
            with self.assertRaises(chapter_projects.HTTPException):
                chapter_projects._snapshot("parent", {**self.data, "media_export_id": job})

    def test_cancellation_stops_encoder_and_cleans_pending_video(self):
        async def exercise():
            started = asyncio.Event()
            async def encode(command, step, label, duration):
                Path(command[-1]).write_bytes(b"incomplete")
                started.set()
                await asyncio.Event().wait()
            class Request:
                async def json(inner):
                    return self.data
            with mock.patch.object(chapter_projects, "video_duration", new=mock.AsyncMock(return_value=10)), \
                    mock.patch.object(chapter_projects, "encode_export_clip", side_effect=encode), \
                    mock.patch.object(chapter_projects, "media_executable", return_value="ffmpeg"):
                response = await chapter_projects.start_chapter_project("parent", Request())
                await started.wait()
                job = response["job_id"]
                task = chapter_projects._active_jobs[job][0]
                self.assertEqual((await chapter_projects.cancel_chapter_project(job))["status"], "cancelling")
                await task
                self.assertEqual(chapter_projects.chapter_project_status(job)["status"], "cancelled")
                self.assertEqual(list(self.VIDEO_DIR.glob("*.pending")), [])
                self.assertTrue((self.VIDEO_DIR / "parent.mp4").exists())
        asyncio.run(exercise())

    def test_active_translation_and_original_language_both_survive(self):
        translated = [{**item, "text": "Translated " + item["text"]} for item in self.transcript]
        self.write(self.TRANSCRIPTS_DIR / "parent_translated_de.json", translated)
        self.write(self.SUMMARIES_DIR / "parent_summary_de.json", self.chapters)
        result = self.create({**self.data, "language": "de"})
        self.assertEqual(result["video_id"], "child")
        self.assertEqual(transcript_versions.get_active_source("child"), {"kind": "translation", "language": "de"})
        original = self.read(self.TRANSCRIPTS_DIR / "child_whisper.json")
        translation = self.read(self.TRANSCRIPTS_DIR / "child_translated_de.json")
        self.assertEqual(original[0]["text"], "Overlapping.")
        self.assertEqual(translation[0]["text"], "Translated Overlapping.")

    def test_video_without_transcript_or_slides_can_be_a_project(self):
        (self.TRANSCRIPTS_DIR / "parent_whisper.json").unlink()
        (self.SCENES_DIR / "parent.json").unlink()
        result = self.create()
        self.assertEqual((result["transcript_segments"], result["slides"]), (0, 0))
        self.assertTrue((self.VIDEO_DIR / "child.mp4").exists())

    def test_failure_cleans_partial_project_and_preserves_parent(self):
        (self.FULLSIZE_IMAGES_DIR / "parent" / "1.jpg").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.create()
        self.assertFalse((self.VIDEO_DIR / "child.mp4").exists())
        self.assertFalse((self.VIDEO_DIR / "child.pending").exists())
        self.assertFalse((self.THUMBNAILS_DIR / "child").exists())
        self.assertTrue((self.VIDEO_DIR / "parent.mp4").exists())
        self.assertFalse((self.VIDEO_DIR / "metadata.json").exists())

    def test_invalid_or_stale_requests_rejected(self):
        with TestClient(self.app) as client:
            for data in ({**self.data, "chapter_index": True}, {**self.data, "language": []},
                         {**self.data, "source": "../etc"}, {**self.data, "chapter_index": -1},
                         *({**self.data, "caption_track_index": value} for value in (True, -1, 32, "1"))):
                self.assertEqual(client.post("/chapter_projects/parent", json=data).status_code, 400)
            for data in ({**self.data, "timestamp": "00:01"}, {**self.data, "title": "Old title"},
                         {**self.data, "next_timestamp": "00:07"}, {**self.data, "chapter_index": 9}):
                self.assertEqual(client.post("/chapter_projects/parent", json=data).status_code, 409)
            self.assertEqual(client.post("/chapter_projects/bad.id", json=self.data).status_code, 400)
            self.assertEqual(client.post("/chapter_projects/parent", content="{").status_code, 400)
            self.assertEqual(client.get("/chapter_project_jobs/bad").status_code, 404)

    def test_duration_outside_video_does_not_encode(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.create(duration=3)
        self.assertEqual(self.commands, [])

    def test_job_persists_result_and_can_be_read_after_restart(self):
        async def project(snapshot, project_id, step):
            return {"video_id": project_id, "filename": "Chapter.mp4", "slides": 0}
        with mock.patch.object(chapter_projects, "create_project", side_effect=project), TestClient(self.app) as client:
            response = client.post("/chapter_projects/parent", json=self.data)
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            for _ in range(20):
                state = client.get(f"/chapter_project_jobs/{job_id}").json()
                if state["status"] == "complete":
                    break
            self.assertEqual(state["status"], "complete")
            self.assertIn("project", state)
        restarted = ExportJobStore(chapter_projects.jobs.root)
        self.assertEqual(restarted.read(job_id)["project"], state["project"])

    def test_encoding_error_is_reported_by_job(self):
        with mock.patch.object(chapter_projects, "create_project", new=mock.AsyncMock(side_effect=RuntimeError("Encoder failed"))), \
                TestClient(self.app) as client:
            response = client.post("/chapter_projects/parent", json=self.data)
            job_id = response.json()["job_id"]
            for _ in range(20):
                state = client.get(f"/chapter_project_jobs/{job_id}").json()
                if state["status"] == "error":
                    break
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["error"], "Encoder failed")

    def test_real_encoder_produces_precise_playable_chapter_with_audio(self):
        from ffmpeg_tools import media_executable
        subprocess.run([
            media_executable("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=160x90:r=12:d=10", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(self.VIDEO_DIR / "parent.mp4"),
        ], check=True, capture_output=True)
        snapshot = chapter_projects._snapshot("parent", self.data)
        result = asyncio.run(chapter_projects.create_project(snapshot, "child", lambda _: None))
        self.assertEqual(result["duration"], 4)
        output = self.VIDEO_DIR / "child.mp4"
        self.assertAlmostEqual(asyncio.run(chapter_projects.video_duration(output)), 4, delta=0.12)
        probe = subprocess.run([
            media_executable("ffprobe"), "-v", "error", "-show_entries", "stream=codec_name",
            "-of", "json", str(output),
        ], check=True, capture_output=True, text=True)
        self.assertEqual({stream["codec_name"] for stream in json.loads(probe.stdout)["streams"]}, {"h264", "aac"})

    def test_real_nested_projects_keep_english_captions_and_browser_tracks(self):
        from ffmpeg_tools import media_executable
        source = self.VIDEO_DIR / "parent.mp4"
        plain = self.root / "plain.mp4"
        subtitles = self.root / "english.srt"
        subtitles.write_text("1\n00:00:01,000 --> 00:00:08,000\nEnglish captions stay visible.\n", encoding="utf-8")
        german = self.root / "german.srt"
        german.write_text("1\n00:00:01,000 --> 00:00:08,000\nDeutsche Untertitel.\n", encoding="utf-8")
        subprocess.run([media_executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=blue:s=160x90:r=12:d=10", "-c:v", "libx264", str(plain)],
                       check=True, capture_output=True)
        subprocess.run([media_executable("ffmpeg"), "-v", "error", "-y", "-i", str(plain),
                        "-i", str(german), "-i", str(subtitles),
                        "-map", "0:v", "-map", "1:0", "-map", "2:0", "-c:v", "copy", "-c:s", "mov_text",
                        "-metadata:s:s:0", "language=deu", "-metadata:s:s:1", "language=eng", str(source)],
                       check=True, capture_output=True)
        with mock.patch.object(chapter_projects, "extract_caption_tracks", new=self.real_extract):
            snapshot = chapter_projects._snapshot("parent", {**self.data, "caption_track_index": 1})
            asyncio.run(chapter_projects.create_project(snapshot, "child", lambda _: None))
            tracks = chapter_projects.chapter_captions("child")["tracks"]
            self.assertEqual([track["language"] for track in tracks], ["deu", "eng"])
            self.assertEqual([track["default"] for track in tracks], [False, True])
            self.assertIn("English captions stay visible", (self.TRANSCRIPTS_DIR / "child_caption_1.vtt").read_text())
            self.write(self.SUMMARIES_DIR / "child.json", [{"timestamp": "00:01", "title": "Nested"}])
            nested = chapter_projects._snapshot("child", {
                "chapter_index": 0, "timestamp": "00:01", "title": "Nested", "next_timestamp": None,
            })
            asyncio.run(chapter_projects.create_project(nested, "grandchild", lambda _: None))
            tracks = chapter_projects.chapter_captions("grandchild")["tracks"]
            self.assertEqual([track["default"] for track in tracks], [False, True])
            self.assertEqual(tracks[1]["language"], "eng")
            text = (self.TRANSCRIPTS_DIR / "grandchild_caption_1.vtt").read_text()
            self.assertIn("English captions stay visible", text)
            self.assertIn("00:00.000", text)
            self.assertAlmostEqual(asyncio.run(chapter_projects.video_duration(self.VIDEO_DIR / "grandchild.mp4")), 3, delta=.15)

    def test_single_chapter_video_download_is_an_mp4_not_a_project(self):
        captured = {}
        def start(video_id, options, generate):
            captured["generate"] = generate
            return "d" * 32
        class Request:
            async def json(inner):
                return self.data
        with mock.patch.object(chapter_projects.video_jobs, "start", side_effect=start):
            result = asyncio.run(chapter_projects.start_chapter_video("parent", Request()))
        self.assertEqual(result["job_id"], "d" * 32)
        workspace = self.root / "download"
        workspace.mkdir()
        with mock.patch.object(chapter_projects, "video_duration", new=mock.AsyncMock(return_value=10)), \
                mock.patch.object(chapter_projects, "encode_export_clip", side_effect=self.encode), \
                mock.patch.object(chapter_projects, "media_executable", return_value="ffmpeg"):
            response = asyncio.run(captured["generate"]("parent", {}, lambda _: None, None, workspace))
        self.assertEqual(Path(response.path).suffix, ".mp4")
        self.assertEqual(Path(response.path).read_bytes(), b"encoded chapter")
        self.assertFalse((self.VIDEO_DIR / "metadata.json").exists(), "A download must not create another upload")


if __name__ == "__main__":
    unittest.main()
