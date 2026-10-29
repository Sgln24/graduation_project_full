import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fusion import (
    FusionValidationError,
    MAX_DOCUMENT_BYTES,
    MAX_VIDEO_SOURCES,
    ensure_local_model_available,
    run_local_fusion,
    validate_fusion_inputs,
)


class FakeUploadFile:
    def __init__(self, filename, payload):
        self.filename = filename
        self._payload = payload

    def read(self):
        return self._payload


class LocalFusionTests(unittest.TestCase):
    def _upload(self, filename, text):
        return FakeUploadFile(filename, text.encode("utf-8"))

    def test_five_video_limit_is_enforced(self):
        with self.assertRaisesRegex(FusionValidationError, "at most 5 video sources"):
            validate_fusion_inputs(video_sources=[f"video-{index}" for index in range(MAX_VIDEO_SOURCES + 1)])

    def test_aggregate_uploaded_document_limit_is_enforced(self):
        payload = b"x" * (MAX_DOCUMENT_BYTES + 1)
        file_obj = FakeUploadFile("large.txt", payload)
        with self.assertRaisesRegex(FusionValidationError, "20 MB|20 MB limit"):
            validate_fusion_inputs(uploaded_files=[file_obj])

    def test_unsupported_and_corrupted_document_is_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(b"not a real pdf")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(FusionValidationError, "Unsupported or corrupted document"):
                validate_fusion_inputs(uploaded_files=[FakeUploadFile(path.name, path.read_bytes())])
        finally:
            path.unlink(missing_ok=True)

    def test_local_model_unavailability_stops_without_cloud_fallback(self):
        class OfflineResponse:
            def __enter__(self):
                raise OSError("offline")

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("fusion.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(RuntimeError, "Ollama"):
                ensure_local_model_available("llama3.2:3b")

    def test_run_local_fusion_creates_pptx_and_pdf(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"models": [{"name": "llama3.2:3b"}]}).encode("utf-8")

        with patch("fusion.urlopen", return_value=FakeResponse()):
            with tempfile.TemporaryDirectory() as directory:
                payload = self._upload("notes.txt", "This lecture explains machine learning and neural networks.")
                result = run_local_fusion(
                    video_sources=["lecture-a", "lecture-b"],
                    uploaded_files=[payload],
                    model_name="llama3.2:3b",
                    output_dir=Path(directory),
                )
                pptx = Path(result["artifacts"]["pptx"])
                pdf = Path(result["artifacts"]["pdf"])
                self.assertTrue(pptx.exists())
                self.assertTrue(pdf.exists())
                self.assertGreater(len(result["relationships"]), 0)
                self.assertEqual(result["local_model"]["runtime"], "Ollama")


if __name__ == "__main__":
    unittest.main()
