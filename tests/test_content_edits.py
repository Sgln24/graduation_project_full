import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import content_edits
from processors import transcript_versions


class ContentEditsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("TRANSCRIPTS_DIR", "SCENES_DIR", "SUMMARIES_DIR"):
            directory = self.root / name
            directory.mkdir()
            setattr(self, name, directory)
            patch = mock.patch.object(content_edits, name, directory)
            patch.start()
            self.addCleanup(patch.stop)
            if hasattr(transcript_versions, name):
                patch = mock.patch.object(transcript_versions, name, directory)
                patch.start()
                self.addCleanup(patch.stop)
        self.transcript = [
            {"start": 0, "duration": 4, "text": "First", "speaker": "A", "extra": 1},
            {"start": 3723, "duration": 4, "text": "Second", "speaker": "B"},
        ]
        self.chapters = [
            {"timestamp": "00:00", "title": "Opening", "summary": "Intro", "extra": 2,
             "sections": [{"timestamp": "00:00", "title": "Part", "point": "Point"}]},
            {"timestamp": "01:02:03", "title": "Next"},
        ]
        self.write(self.TRANSCRIPTS_DIR / "vid_whisper.json", self.transcript)
        self.write(self.SUMMARIES_DIR / "vid.json", self.chapters)
        self.write(self.SCENES_DIR / "vid.json", [{"time_seconds": 0, "image": "keep.jpg"}])
        app = FastAPI()
        app.include_router(content_edits.router)
        self.client = TestClient(app)

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def get(self, kind, **context):
        response = self.client.get("/editable_content/vid", params={"kind": kind, **context})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, kind, edits, status=200, revision=None, **context):
        revision = revision or self.get(kind, **context)["revision"]
        response = self.client.post("/editable_content/vid", json={
            "kind": kind, "revision": revision, "edits": edits, **context,
        })
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_chapters_preserve_metadata_and_exact_manual_times_on_reload(self):
        edits = copy.deepcopy(self.chapters)
        edits[0].update(title="My title", summary="My text")
        edits[0]["sections"][0]["timestamp"] = "00:01.500"
        edits[1]["timestamp"] = "01:02:04"
        saved = self.save("chapters", edits)["content"]
        self.assertEqual(saved[0]["extra"], 2)
        self.assertTrue(saved[1]["timestamp_edited"])
        self.assertTrue(saved[0]["sections"][0]["timestamp_edited"])
        self.assertEqual(saved, self.get("chapters")["content"])
        self.assertNotIn("timestamp_edited", saved[0])

    def test_summary_reload_does_not_normalize_away_user_edits(self):
        from processors import summary_processor
        edits = copy.deepcopy(self.chapters)
        edits[0]["sections"][0]["timestamp"] = "00:01.500"
        saved = self.save("chapters", edits)["content"]
        processor = summary_processor.SummaryProcessor.__new__(summary_processor.SummaryProcessor)
        with mock.patch.object(summary_processor, "SUMMARIES_DIR", self.SUMMARIES_DIR), \
                mock.patch.object(summary_processor, "JSONResponse", side_effect=lambda data: data), \
                mock.patch.object(processor, "_normalize_chapters", side_effect=AssertionError("Must preserve edits")):
            result = asyncio.run(processor.get_summary("vid"))
        self.assertEqual(result["chapters"], saved)

    def test_write_failure_is_reported_without_success_response(self):
        with mock.patch.object(transcript_versions, "_write_json", side_effect=PermissionError("read only")):
            response = self.client.post("/editable_content/vid", json={
                "kind": "slides", "revision": self.get("slides")["revision"], "edits": ["Title"],
            })
        self.assertEqual(response.status_code, 500)
        self.assertIn("Could not save", response.json()["detail"])

    def test_transcript_save_preserves_original_timing_speakers_and_metadata(self):
        metadata = {"model": "turbo", "speakers": {"A": "Alice"}}
        self.write(self.TRANSCRIPTS_DIR / "vid_whisper_meta.json", metadata)
        transcript_versions.sync_existing("vid")
        original = transcript_versions.list_versions("vid")[0]
        result = self.save("transcript", ["Corrected", "Second"], version_id=original["id"])
        self.assertTrue(result["version"]["edited"])
        self.assertIn("edited", result["version"]["label"])
        expected = copy.deepcopy(self.transcript)
        expected[0]["text"] = "Corrected"
        self.assertEqual(result["content"], expected)
        self.assertEqual(self.get("transcript")["content"], expected)
        restored = transcript_versions.activate_version("vid", original["id"])
        self.assertEqual(restored["transcript"], self.transcript)
        self.assertEqual(restored["chapters"], self.chapters)
        edited = transcript_versions.activate_version("vid", result["version"]["id"])
        self.assertEqual(edited["metadata"], metadata)

    def test_translation_and_uploaded_edits_keep_provenance(self):
        for source, language in (("uploaded", ""), ("whisper", "de")):
            filename = f"vid_translated_{language}.json" if language else "vid_uploaded.json"
            self.write(self.TRANSCRIPTS_DIR / filename, self.transcript)
            if language:
                self.write(self.SUMMARIES_DIR / "vid_summary_de.json", self.chapters)
            result = self.save("transcript", ["Changed", "Second"], source=source, language=language)
            self.assertEqual(result["version"]["language"], language or None)
            self.assertEqual(self.get("transcript", source=source, language=language)["content"][0]["text"], "Changed")
            active = transcript_versions.get_active_source("vid")
            self.assertEqual(active["kind"], "translation" if language else "transcript")
            if not language:
                self.assertEqual(active["source"], "uploaded")

    def test_slide_title_edit_and_clear_preserve_other_fields(self):
        content = self.save("slides", ["<My title>"])["content"]
        self.assertEqual(content, [{"time_seconds": 0, "image": "keep.jpg", "title": "<My title>", "title_edited": True}])
        self.assertEqual(self.save("slides", [""])["content"][0]["title"], "")

    def test_stale_revision_rejected(self):
        revision = self.get("transcript")["revision"]
        self.save("transcript", ["Changed", "Second"])
        self.save("transcript", ["Lost update", "Second"], revision=revision, status=409)

    def outline(self):
        chapters = [
            {"timestamp": "00:00", "title": "First", "summary": "One", "extra": 7, "sections": [
                {"timestamp": "00:00", "title": "A", "point": "Alpha", "extra": 9},
                {"timestamp": "00:10", "title": "B", "point": "Beta"},
                {"timestamp": "00:20", "title": "C", "point": "Gamma"}]},
            {"timestamp": "00:30", "title": "Second", "summary": "Two", "sections": [
                {"timestamp": "00:30", "title": "D", "point": "Delta"},
                {"timestamp": "00:40", "title": "E", "point": "Epsilon"}]},
        ]
        self.write(self.SUMMARIES_DIR / "vid.json", chapters)
        return chapters

    def test_outline_grouping_saves_atomically_and_retains_text_and_metadata(self):
        for operation in [
            {"action": "merge_section", "chapter": 0, "section": 0},
            {"action": "merge_chapter", "chapter": 0},
            {"action": "promote", "chapter": 0, "section": 1},
            {"action": "move_section", "chapter": 0, "section": 2, "target": 1},
            {"action": "move_section", "chapter": 1, "section": 0, "target": 0},
        ]:
            original = self.outline()
            changed = content_edits.outline_operations(original, [operation])
            changed[0]["title"] = "Inline title"
            saved = self.save("chapters", changed, operations=[operation])["content"]
            self.assertEqual(saved[0]["extra"], 7)
            self.assertEqual(saved[0]["title"], "Inline title")
            for word in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
                self.assertIn(word, json.dumps(saved))
            self.assertEqual(self.get("chapters")["content"], saved)
            if operation["action"] == "merge_section":
                self.assertEqual(saved[0]["sections"][0]["extra"], 9)
                self.assertEqual(saved[0]["sections"][0]["timestamp"], "00:00")
                self.assertEqual(saved[0]["sections"][0]["title"], "A")

    def test_invalid_structure_never_overwrites_saved_outline(self):
        original = self.outline()
        for op in [
            {"action": "move_section", "chapter": 0, "section": 1, "target": 1},
            {"action": "move_section", "chapter": 0, "section": 2, "target": 2},
            {"action": "promote", "chapter": 0, "section": 0},
            {"action": "merge_section", "chapter": 0, "section": 2},
            {"action": "merge_chapter", "chapter": True},
        ]:
            self.save("chapters", original, status=400, operations=[op])
            self.assertEqual(self.get("chapters")["content"], original)

    def test_slide_inline_timestamps_keep_images_and_translation(self):
        self.write(self.SCENES_DIR / "vid.json", [
            {"timestamp": "00:00", "time_seconds": 0, "image": "first.jpg"},
            {"timestamp": "00:10", "time_seconds": 10, "image": "second.jpg"},
        ])
        result = self.save("slides", [{"timestamp": "00:01.500", "title": "One"},
                                      {"timestamp": "00:12", "title": "Two"}], language="en")["content"]
        self.assertEqual(result[0]["image"], "first.jpg")
        self.assertEqual(result[0]["time_seconds"], 1.5)
        self.assertEqual(result[0]["duration"], 10.5)
        self.assertEqual(result[0]["title_translations"]["en"], "One")
        self.save("slides", [{"timestamp": "00:15", "title": "No"},
                             {"timestamp": "00:12", "title": "Two"}], status=400)

    def test_invalid_edits_do_not_change_files(self):
        edits = copy.deepcopy(self.chapters)
        for timestamp in ("bad", "-01:00", "01:60:00", "00:00"):
            edits[1]["timestamp"] = timestamp
            self.save("chapters", edits, status=400)
        edits = copy.deepcopy(self.chapters)
        edits[0]["sections"][0]["timestamp"] = "02:00:00"
        self.save("chapters", edits, status=400)
        self.save("transcript", ["", "Second"], status=400)
        self.save("slides", ["One", "Two"], status=400)
        self.assertEqual(self.get("chapters")["content"], self.chapters)
        self.assertEqual(self.get("transcript")["content"], self.transcript)

    def test_unchanged_transcript_does_not_relabel_original_as_edited(self):
        self.save("transcript", ["First", "Second"], status=400)
        self.assertFalse(any(version.get("edited") for version in transcript_versions.list_versions("vid")))

    def test_old_version_cannot_overwrite_active_corrections(self):
        transcript_versions.sync_existing("vid")
        original = transcript_versions.list_versions("vid")[0]
        self.save("transcript", ["Changed", "Second"], version_id=original["id"])
        response = self.client.get("/editable_content/vid", params={"kind": "transcript", "version_id": original["id"]})
        self.assertEqual(response.status_code, 409)

    def test_invalid_request_types_and_malformed_json(self):
        for key in ("kind", "source", "language", "version_id"):
            data = {"kind": "slides", key: []}
            self.assertEqual(self.client.post("/editable_content/vid", json=data).status_code, 400)
        self.assertEqual(self.client.post("/editable_content/vid", content="{").status_code, 400)
        self.assertEqual(self.client.get("/editable_content/bad.id", params={"kind": "slides"}).status_code, 400)
        self.write(self.SCENES_DIR / "vid.json", ["bad"])
        self.assertEqual(self.client.get("/editable_content/vid", params={"kind": "slides"}).status_code, 500)

    def test_version_language_and_source_mismatch_rejected(self):
        transcript_versions.sync_existing("vid")
        version = transcript_versions.list_versions("vid")[0]["id"]
        for context in ({"language": "de"}, {"source": "uploaded"}):
            response = self.client.get("/editable_content/vid", params={
                "kind": "transcript", "version_id": version, **context,
            })
            self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
