import ast
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import video_summary


class VideoSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(video_summary, "SUMMARIES_DIR", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.processor = SimpleNamespace(generate_video_summary=mock.AsyncMock(return_value={
            "summary": "A short paragraph covering the entire lecture.", "language": "en",
            "provider": "Groq", "model": "test",
        }))
        names = {"load_video_summary", "generate_video_summary"}
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        namespace = {
            "HTTPException": HTTPException, "video_summary": video_summary,
            "summary_processor": self.processor, "logging": logging,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
        app = FastAPI()
        for name, operation in [("load_video_summary", "load"), ("generate_video_summary", "generate")]:
            app.post(f"/video_summary/{{video_id}}/{operation}")(namespace[name])
        self.client = TestClient(app)
        self.body = {"transcript": [{"start": 0, "text": "Opening"}, {"start": 50, "text": "Ending"}],
                     "transcript_language": "en", "model": "groq:test"}

    def post(self, operation, body=None):
        return self.client.post(f"/video_summary/video/{operation}", json=body or self.body)

    def test_generated_paragraph_is_persistent_and_does_not_replace_chapters(self):
        chapters = self.root / "video.json"
        chapters.write_text('[{"title":"Keep chapters"}]', encoding="utf-8")
        self.assertFalse(self.post("load").json()["exists"])
        response = self.post("generate")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])
        self.processor.generate_video_summary.assert_awaited_once_with(
            self.body["transcript"], "groq:test", "en",
        )
        saved = self.post("load").json()
        self.assertTrue(saved["exists"])
        self.assertEqual(saved["summary"], response.json()["summary"])
        self.assertEqual(json.loads(chapters.read_text()), [{"title": "Keep chapters"}])

    def test_summary_follows_transcript_content_and_language_not_speaker_names(self):
        self.post("generate")
        renamed = {**self.body, "transcript": [{**row, "speaker": "A", "speaker_name": "Alex"}
                                             for row in self.body["transcript"]]}
        self.assertTrue(self.post("load", renamed).json()["exists"])
        changed = {**self.body, "transcript": [{"text": "Different transcript"}]}
        self.assertFalse(self.post("load", changed).json()["exists"])
        self.assertFalse(self.post("load", {**self.body, "transcript_language": "de"}).json()["exists"])

    def test_failed_generation_keeps_previous_summary_and_surfaces_error(self):
        self.post("generate")
        self.processor.generate_video_summary.side_effect = RuntimeError("Provider busy")
        response = self.post("generate")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Provider busy", response.json()["detail"])
        self.assertTrue(self.post("load").json()["exists"])

    def test_invalid_input_and_corrupt_saved_content_are_explicit_errors(self):
        self.assertEqual(self.client.post("/video_summary/video/generate", content="{broken",
                                         headers={"Content-Type": "application/json"}).status_code, 422)
        self.assertEqual(self.client.post("/video_summary/video/load", json=["not an object"]).status_code, 422)
        for body in ({"transcript": []}, {"transcript": [{"text": ""}]},
                     {"transcript": [{"text": 12}]}, {**self.body, "transcript_language": "../de"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("generate", body).status_code, 400)
        self.assertEqual(self.client.post("/video_summary/bad.id/load", json=self.body).status_code, 400)
        path, _, _ = video_summary.summary_context("video", self.body)
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        response = self.post("load")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Could not read", response.json()["detail"])
