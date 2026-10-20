import ast
import asyncio
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import content_edits
from content_languages import scene_title
from processors import transcript_versions
from tests import test_content_edits


class ChapterToolsTests(unittest.TestCase):
    write = staticmethod(test_content_edits.ContentEditsTests.write)

    def setUp(self):
        test_content_edits.ContentEditsTests.setUp(self)
        self.processor = SimpleNamespace(
            translate_transcript=mock.AsyncMock(side_effect=self.translate),
            enhance_chapters=mock.AsyncMock(side_effect=lambda chapters, *args: copy.deepcopy(chapters)),
            _refresh_clients=mock.Mock(), _provider_chain=lambda *args, **kwargs: [("groq", "test")],
            _chapter_settings=lambda *args: {}, _summary_language_instruction=lambda language: language,
        )
        names = {"chapter_tools", "generate_summary", "detect_transcript_language", "parse_chapter_timestamp"}
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.namespace = {
            "Request": Request, "HTTPException": HTTPException, "content_edits": content_edits,
            "summary_processor": self.processor, "SUMMARIES_DIR": self.SUMMARIES_DIR,
            "SCENES_DIR": self.SCENES_DIR, "transcript_versions": transcript_versions,
            "copy": copy, "asyncio": asyncio, "scene_title": scene_title,
            "logging": logging,
            "re": __import__("re"),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), self.namespace)
        app = FastAPI()
        app.include_router(content_edits.router)
        app.post("/chapter_tools/{video_id}")(self.namespace["chapter_tools"])
        app.post("/generate_summary")(self.namespace["generate_summary"])
        self.client = TestClient(app)

    @staticmethod
    async def translate(rows, target, model):
        return {"transcript": [{**row, "text": f"{target}: {row['text']}"} for row in rows]}

    def action(self, status=200, **data):
        response = self.client.post("/chapter_tools/vid", json={
            "kind": "chapters", "operation": "translate", "target_language": "en", **data,
        })
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_translate_chapters_preserves_structure_metadata_times_and_original(self):
        result = self.action()["content"]
        self.assertEqual(result[0]["title"], "en: Opening")
        self.assertEqual(result[0]["sections"][0]["point"], "en: Point")
        self.assertEqual(result[1]["timestamp"], "01:02:03")
        self.assertEqual(result[0]["extra"], 2)
        self.assertEqual(json.loads((self.SUMMARIES_DIR / "vid.json").read_text()), self.chapters)
        self.assertEqual(json.loads((self.SUMMARIES_DIR / "vid_summary_en.json").read_text()), result)

    def test_translation_can_follow_open_transcript_or_choose_independent_language(self):
        self.write(self.TRANSCRIPTS_DIR / "vid_translated_en.json", self.transcript)
        self.assertEqual(self.action(transcript_language="en", target_language="transcript")["language"], "en")
        self.assertEqual(self.action(transcript_language="en", target_language="de")["language"], "de")
        self.assertEqual(self.processor.translate_transcript.await_args.args[1], "de")
        self.assertTrue((self.SUMMARIES_DIR / "vid_summary_de.json").exists())

    def test_slide_titles_translate_topic_derived_labels_and_keep_ocr_and_images(self):
        scene = {"timestamp": "00:00", "time_seconds": 0, "image": "keep.jpg", "ocr_text": "Original OCR"}
        self.write(self.SCENES_DIR / "vid.json", [scene])
        result = self.action(kind="slides", target_language="de")["content"][0]
        self.assertEqual(result["title_translations"], {"de": "de: Part"})
        self.assertEqual(result["ocr_text"], scene["ocr_text"])
        self.assertEqual(result["image"], scene["image"])
        self.assertEqual(result["timestamp"], scene["timestamp"])

    def test_slide_translation_follows_original_transcript_language(self):
        result = self.action(kind="slides", target_language="transcript")
        self.assertEqual(result["language"], "en")

    def test_stale_source_and_target_are_not_overwritten(self):
        async def change(rows, target, model):
            self.write(self.SUMMARIES_DIR / "vid_summary_en.json", [{"title": "Newer edit"}])
            return await self.translate(rows, target, model)
        self.processor.translate_transcript.side_effect = change
        self.action(status=409)
        self.assertEqual(json.loads((self.SUMMARIES_DIR / "vid_summary_en.json").read_text()), [{"title": "Newer edit"}])

    def test_failed_or_incomplete_translation_preserves_existing_content(self):
        self.processor.translate_transcript.return_value = {"transcript": []}
        self.processor.translate_transcript.side_effect = None
        self.action(status=502)
        self.assertFalse((self.SUMMARIES_DIR / "vid_summary_en.json").exists())
        self.assertEqual(json.loads((self.SUMMARIES_DIR / "vid.json").read_text()), self.chapters)

    def test_enhance_uses_existing_outline_not_transcript_regeneration(self):
        self.action(operation="enhance")
        self.processor.enhance_chapters.assert_awaited_once()
        self.processor.translate_transcript.assert_not_awaited()
        self.assertTrue(json.loads((self.SUMMARIES_DIR / "vid.json").read_text())[0]["user_edited"])

    def test_editor_can_edit_independent_chapter_and_slide_languages(self):
        self.action(target_language="de")
        snapshot = self.client.get("/editable_content/vid", params={
            "kind": "chapters", "language": "de", "transcript_language": "",
        }).json()
        edited = copy.deepcopy(snapshot["content"])
        edited[0]["title"] = "Edited German title"
        response = self.client.post("/editable_content/vid", json={
            "kind": "chapters", "language": "de", "transcript_language": "",
            "revision": snapshot["revision"], "edits": edited,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(json.loads((self.SUMMARIES_DIR / "vid.json").read_text()), self.chapters)
        snapshot = self.client.get("/editable_content/vid", params={"kind": "slides", "language": "de"}).json()
        response = self.client.post("/editable_content/vid", json={
            "kind": "slides", "language": "de", "revision": snapshot["revision"], "edits": ["German title"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"][0]["title_translations"]["de"], "German title")

    def test_generate_endpoint_forwards_open_transcript_language_and_passes(self):
        self.processor.generate_summary = mock.AsyncMock(return_value={"success": True})
        response = self.client.post("/generate_summary", json={
            "video_id": "vid", "transcript": self.transcript, "transcript_language": "en", "passes": "two",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.processor.generate_summary.await_args.kwargs["transcript_language"], "en")
        self.assertEqual(self.processor.generate_summary.await_args.kwargs["passes"], "two")
