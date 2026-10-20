import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import library
from export_jobs import ExportJobStore


class LibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("VIDEO_DIR", "TRANSCRIPTS_DIR", "SCENES_DIR", "SUMMARIES_DIR", "THUMBNAILS_DIR",
                     "FULLSIZE_IMAGES_DIR", "DETECTIONS_DIR", "OCR_RESULTS_DIR"):
            path = self.root / name
            path.mkdir()
            patch = mock.patch.object(library, name, path)
            patch.start()
            self.addCleanup(patch.stop)
        self.store = ExportJobStore(self.root / "exports")
        patch = mock.patch.object(library, "stores", {"exports": self.store})
        patch.start()
        self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(library.router)
        self.client = TestClient(app)
        (library.VIDEO_DIR / "parent.mp4").write_bytes(b"video")
        (library.VIDEO_DIR / "child.mp4").write_bytes(b"independent child")
        (library.VIDEO_DIR / "metadata.json").write_text(json.dumps({"parent": {"filename": "Parent"},
                                                                   "child": {"filename": "Child"}}))
        (library.TRANSCRIPTS_DIR / "parent_whisper.json").write_text("[]")
        (library.SUMMARIES_DIR / "parent.json").write_text("[]")
        (library.THUMBNAILS_DIR / "parent").mkdir()
        (library.THUMBNAILS_DIR / "parent" / "0.jpg").write_bytes(b"image")
        self.job = "a" * 32
        self.store._write(self.job, {"id": self.job, "video_id": "parent", "instance": self.store.instance,
                                    "status": "complete", "created_at": 1, "title": "Export"})
        (self.store._directory(self.job) / "download.zip").write_bytes(b"saved")

    def test_listing_and_confirmed_export_deletion(self):
        listed = self.client.get("/library").json()
        self.assertEqual(len(listed["videos"]), 2)
        self.assertEqual(len(listed["exports"]), 1)
        url = f"/library/exports/exports/{self.job}"
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.assertEqual(self.client.delete(url + "?confirm=true").status_code, 200)
        self.assertTrue((library.VIDEO_DIR / "parent.mp4").exists())
        self.assertFalse(self.store._directory(self.job).exists())

    def test_upload_deletion_can_keep_or_remove_exports_and_never_deletes_child(self):
        for include in (False, True):
            with self.subTest(include=include):
                (library.VIDEO_DIR / "parent.mp4").write_bytes(b"video")
                response = self.client.delete(f"/library/videos/parent?confirm=true&include_exports={str(include).lower()}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.store._directory(self.job).exists(), not include)
                self.assertTrue((library.VIDEO_DIR / "child.mp4").exists())
                self.assertFalse((library.THUMBNAILS_DIR / "parent").exists())
                self.assertNotIn("parent", json.loads((library.VIDEO_DIR / "metadata.json").read_text()))

    def test_running_jobs_confirmation_and_traversal_are_guarded(self):
        self.assertEqual(self.client.delete("/library/videos/parent").status_code, 400)
        with mock.patch.object(library, "busy", return_value=True):
            self.assertEqual(self.client.delete("/library/videos/parent?confirm=true").status_code, 409)
        state = self.store.read(self.job)
        state["status"] = "running"
        self.store._write(self.job, state)
        self.assertEqual(self.client.delete(f"/library/exports/exports/{self.job}?confirm=true").status_code, 409)
        self.assertEqual(self.client.delete("/library/videos/parent?confirm=true").status_code, 409)
        self.assertTrue((library.VIDEO_DIR / "parent.mp4").exists())
        with self.assertRaises(library.HTTPException):
            library.delete_upload("..", confirm=True)
        self.assertTrue((library.VIDEO_DIR / "child.mp4").exists())


if __name__ == "__main__":
    unittest.main()
