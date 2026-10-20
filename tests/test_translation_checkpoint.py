import asyncio
import ast
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from translation_checkpoint import translate_with_resume, TranslationCheckpointError


class TranslationCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.transcript = [{"start": 0, "text": "Hello"}, {"start": 10, "text": "World"}]

    def run_translation(self, translate, transcript=None, target="de", **kwargs):
        return asyncio.run(translate_with_resume(
            SimpleNamespace(translate_transcript=translate),
            self.transcript if transcript is None else transcript, target,
            checkpoint_dir=self.directory, **kwargs,
        ))

    def save_partial(self):
        async def fail(transcript, target, model, _done, **kwargs):
            _done.update({"segment-0": "Hallo"})
            raise RuntimeError("429 daily quota")
        with self.assertRaisesRegex(RuntimeError, "1/2 lines are saved"):
            self.run_translation(fail)

    def test_new_attempt_loads_saved_fragments_and_clears_after_success(self):
        self.save_partial()
        saved = list(self.directory.glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text()), {"segment-0": "Hallo"})
        async def resume(transcript, target, model, _done, **kwargs):
            self.assertEqual(_done, {"segment-0": "Hallo"})
            _done.update({"segment-1": "Welt"})
            return {"transcript": [{**item, "text": _done[f"segment-{index}"]}
                                   for index, item in enumerate(transcript)]}
        result = self.run_translation(resume, model="other-model")
        self.assertEqual(result["resumed_segments"], 1)
        self.assertEqual([item["text"] for item in result["transcript"]], ["Hallo", "Welt"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_input_target_timing_speaker_and_chapter_changes_do_not_reuse(self):
        self.save_partial()
        async def fresh(transcript, target, model, _done, **kwargs):
            self.assertEqual(_done, {})
            return {"transcript": transcript}
        for transcript, target in (
            ([{**self.transcript[0], "text": "Changed"}, self.transcript[1]], "de"),
            ([{**self.transcript[0], "start": 1}, self.transcript[1]], "de"),
            ([{**self.transcript[0], "speaker": "A"}, self.transcript[1]], "de"),
            ([*self.transcript, {"start": 0, "text": "Chapter title"}], "de"),
            (self.transcript, "pl"),
        ):
            self.run_translation(fresh, transcript, target)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)

    def test_corrupt_checkpoint_and_write_failure_are_explicit(self):
        self.save_partial()
        path = next(self.directory.glob("*.json"))
        path.write_text('{"segment-999": "Bad"}')
        with self.assertRaisesRegex(TranslationCheckpointError, "Could not read"):
            self.run_translation(mock.AsyncMock())
        path.unlink()
        async def translate(transcript, target, model, _done, **kwargs):
            _done.update({"segment-0": "Hallo"})
        with mock.patch("translation_checkpoint.os.replace", side_effect=PermissionError("Locked")):
            with self.assertRaisesRegex(TranslationCheckpointError, "Could not save"):
                self.run_translation(translate)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_concurrent_identical_job_is_rejected_and_lock_is_released(self):
        async def exercise():
            async def translate(transcript, target, model, _done, **kwargs):
                with self.assertRaisesRegex(TranslationCheckpointError, "already being translated"):
                    await translate_with_resume(
                        SimpleNamespace(translate_transcript=translate), transcript, target,
                        checkpoint_dir=self.directory,
                    )
                return {"transcript": transcript}
            for _ in range(2):
                await translate_with_resume(
                    SimpleNamespace(translate_transcript=translate), self.transcript, "de",
                    checkpoint_dir=self.directory,
                )
        asyncio.run(exercise())

    def test_completed_file_can_be_retained_until_batch_archive_succeeds(self):
        async def translate(transcript, target, model, _done, **kwargs):
            _done.update({"segment-0": "Hallo", "segment-1": "Welt"})
            return {"transcript": transcript}
        self.run_translation(translate, clear_on_success=False)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        result = self.run_translation(translate)
        self.assertEqual(result["resumed_segments"], 2)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_transcript_endpoint_resumes_transcript_and_chapter_fields(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "translate_transcript")
        node.decorator_list = []
        class HTTPException(Exception):
            def __init__(self, status_code, detail):
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)
        summaries = self.directory / "summaries"
        transcripts = self.directory / "transcripts"
        summaries.mkdir()
        transcripts.mkdir()
        (summaries / "video.json").write_text(json.dumps([{
            "title": "Chapter", "summary": "Summary", "sections": [{"title": "Section"}],
        }]))
        fail = True
        seen = []
        async def translate(transcript, target, model, _done, on_progress, **kwargs):
            seen.append(dict(_done))
            for index, item in enumerate(transcript):
                identity = f"segment-{index}"
                if identity not in _done:
                    _done.update({identity: "translated " + item["text"]})
                if fail:
                    raise RuntimeError("429 daily quota")
            return {
                "transcript": [{**item, "text": _done[f"segment-{index}"]}
                               for index, item in enumerate(transcript)],
                "provider": "Test", "model": "test", "elapsed_seconds": 1, "batch_count": 1,
            }
        namespace = {
            "Request": object, "HTTPException": HTTPException, "json": json, "time": time,
            "asyncio": asyncio, "TRANSLATION_MODELS": {""}, "translation_progress": {},
            "EXPORTS_DIR": self.directory, "TRANSCRIPTS_DIR": transcripts, "SUMMARIES_DIR": summaries,
            "summary_processor": SimpleNamespace(translate_transcript=translate),
            "transcript_versions": SimpleNamespace(
                sync_existing=mock.Mock(), save_version=mock.Mock(),
                find_transcript_label=mock.Mock(return_value="Original"), set_active_source=mock.Mock(),
            ),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        request = SimpleNamespace(json=mock.AsyncMock(return_value={
            "transcript": self.transcript, "target_language": "de",
        }))
        with self.assertRaises(HTTPException) as failed:
            asyncio.run(namespace["translate_transcript"]("video", request))
        self.assertEqual(failed.exception.status_code, 502)
        self.assertIn("1/5 lines are saved", str(failed.exception))
        fail = False
        result = asyncio.run(namespace["translate_transcript"]("video", request))
        self.assertEqual(result["resumed_segments"], 1)
        self.assertEqual(seen, [{}, {"segment-0": "translated Hello"}])
        self.assertEqual(result["chapters"][0]["title"], "translated Chapter")
        self.assertEqual(result["chapters"][0]["sections"][0]["title"], "translated Section")
        self.assertEqual(json.loads((transcripts / "video_translated_de.json").read_text()),
                         result["transcript"])
        namespace["translation_progress"]["video:de"] = {"status": "running"}
        with self.assertRaises(HTTPException) as duplicate:
            asyncio.run(namespace["translate_transcript"]("video", request))
        self.assertEqual(duplicate.exception.status_code, 409)
