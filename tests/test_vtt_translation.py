import ast
import asyncio
import tempfile
import unittest
import zipfile
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.testclient import TestClient
from export_jobs import ExportJobStore
from processors.summary_processor import SummaryProcessor
from vtt_translation import (
    parse_vtt, translated_vtt, translate_vtt_batch, MAX_FILES,
    MAX_FILE_BYTES, MAX_BATCH_BYTES, TRANSLATION_MODELS,
)


VTT = b"""WEBVTT

STYLE
::cue { color: white; }

REGION
id:bottom

NOTE Leave this note unchanged

cue-id
00:00:01.200 --> 00:00:03.456 align:start position:10%
<v Speaker><b>Hello &amp; welcome</b></v>
Second line

00:05.000 --> 00:07.000
<00:05.200>Hello again
"""


class VttTranslationTests(unittest.TestCase):
    def test_zip_translates_plain_fragments_without_model_controlled_markers(self):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.model = True
        processor.model_name = "gemini-test"
        processor._refresh_clients = lambda: None
        processor._provider_chain = lambda *args, **kwargs: [("gemini", "gemini-test")]
        requests = []
        def complete(provider, model, prompt, *args):
            payload = json.loads(prompt.split("\n", 1)[1])
            requests.append(payload["lines"])
            return json.dumps({"translations": [
                {"id": item["id"], "text": re.sub(r"\[\[VTT_[0-9]+\]\]", "", item["text"])
                 .replace("Hello", "Hallo").replace("Second", "Zweite")}
                for item in payload["lines"]
            ]})
        processor._complete = complete
        with tempfile.TemporaryDirectory() as temporary:
            response = asyncio.run(translate_vtt_batch(
                [("source.vtt", parse_vtt(VTT))], "de", "gemini-test", processor, temporary, lambda _: None))
            with zipfile.ZipFile(response.path) as archive:
                output = archive.read("source_DE.vtt").decode()
                self.assertIn("<v Speaker><b>Hallo &amp; welcome</b></v>\nZweite line", output)
                self.assertEqual(parse_vtt(output.encode())["cues"], 2)
                report = json.loads(archive.read("translation-report.json"))[0]
                self.assertEqual(report["usage"]["requests"], 1)
                self.assertEqual(report["translation_items"], 3)
        self.assertEqual(len(requests), 1)
        self.assertTrue(all("[[VTT_" not in item["text"] for item in requests[0]))

    def test_plain_fragment_batches_preserve_cues_and_formatting(self):
        content = b"WEBVTT\n\n" + b"\n\n".join(
            f"cue-{index}\n00:{index // 60:02d}:{index % 60:02d}.000 --> "
            f"00:{(index + 1) // 60:02d}:{(index + 1) % 60:02d}.000\n"
            "<b>Hello</b>\nWorld".encode() for index in range(80)
        )
        parsed = parse_vtt(content)
        self.assertEqual(len(parsed["segments"]), 160)
        before = SummaryProcessor._translation_batches(parsed["segments"], 30, 2500)
        after = SummaryProcessor._translation_batches(parsed["segments"], 60, 2500)
        self.assertEqual(len(before), 6)
        self.assertEqual(len(after), 3)
        translated = [{"text": item["text"].replace("Hello", "Hallo").replace("World", "Welt")}
                      for item in parsed["segments"]]
        output = translated_vtt(parsed, translated)
        self.assertEqual(parse_vtt(output.encode())["cues"], 80)
        self.assertEqual(output.count("<b>Hallo</b>\nWelt"), 80)
        self.assertEqual(content.decode().replace("Hello", "Hallo").replace("World", "Welt") + "\n", output)

    def test_preserves_metadata_timestamps_markup_and_newlines(self):
        parsed = parse_vtt(b"\xef\xbb\xbf" + VTT.replace(b"\n", b"\r\n"))
        self.assertEqual(parsed["cues"], 2)
        self.assertEqual([s["text"] for s in parsed["segments"]],
                         ["Hello & welcome", "Second line", "Hello again"])
        self.assertEqual(parsed["segments"][0]["start"], 1.2)
        output = translated_vtt(parsed, [{"text": "Hallo & willkommen"}, {"text": "Zweite Zeile"},
                                        {"text": "Hallo nochmals"}])
        self.assertIn("<v Speaker><b>Hallo &amp; willkommen</b></v>\nZweite Zeile", output)
        self.assertIn("<00:05.200>Hallo nochmals", output)
        for text in ("STYLE\n::cue { color: white; }", "REGION\nid:bottom",
                     "NOTE Leave this note unchanged", "cue-id",
                     "00:00:01.200 --> 00:00:03.456 align:start position:10%",
                     "00:05.000 --> 00:07.000"):
            self.assertIn(text, output)
        self.assertEqual(parse_vtt(output.encode())["cues"], 2)

    def test_preserves_timing_whitespace(self):
        timing = "00:01.000\t -->   00:02.000 align:start"
        parsed = parse_vtt(f"WEBVTT\n\n{timing}\nHello".encode())
        output = translated_vtt(parsed, [{"text": "Hallo"}])
        self.assertIn(timing, output)

    def test_rejects_invalid_inputs_and_incomplete_translation(self):
        for content in (b"", b"1\n00:00:01,000 --> 00:00:02,000\nHi",
                        b"WEBVTT\n\nbad\ntext", b"WEBVTT\n\n00:00.000 --> 00:00.000\nHi",
                        b"WEBVTT\n\n00:99.000 --> 01:00.000\nHi",
                        b"WEBVTT\n\nNOTE only metadata", b"WEBVTT\n\n00:00.000 --> 00:01.000",
                        b"WEBVTT\n\n00:00.000 --> 00:01.000\n<b></b>",
                        b"WEBVTT\n\n00:00.000 --> " + b"9" * 500 + b":00:00.000\nHi"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_vtt(content)
        with self.assertRaises(UnicodeDecodeError):
            parse_vtt(b"WEBVTT\n\n\xff")
        for items in ([], [{"text": ""}], [{"text": 123}], [None]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                translated_vtt(parse_vtt(b"WEBVTT\n\n00:00.000 --> 00:01.000\nHi"), items)
        output = translated_vtt(parse_vtt(b"WEBVTT\n\n00:00.000 --> 00:01.000\nHi"),
                                [{"text": "<script>\n\n00:01.000 --> 00:02.000"}])
        self.assertNotIn("<script>", output)
        self.assertEqual(parse_vtt(output.encode())["cues"], 1)

    def test_batch_zip_names_unicode_progress_and_failure(self):
        calls = []
        async def translate(segments, target, model, on_progress, allow_fallback=True):
            calls.append((segments, target, model))
            on_progress({"completed_segments": len(segments)})
            return {"transcript": [{"text": "\u0645\u0631\u062d\u0628\u0627"} for _ in segments]}
        processor = SimpleNamespace(translate_transcript=translate)
        content = b"WEBVTT\n\n00:00.000 --> 00:02.000\nHi\n"
        with tempfile.TemporaryDirectory() as temporary:
            steps = []
            response = asyncio.run(translate_vtt_batch(
                [(name, parse_vtt(content)) for name in ("Lecture.vtt", "lecture.vtt", "../Other.vtt")],
                "ar", "", processor, temporary, steps.append))
            with zipfile.ZipFile(response.path) as archive:
                self.assertEqual(archive.namelist(),
                                 ["Lecture_AR.vtt", "lecture_AR_2.vtt", "Other_AR.vtt", "translation-report.json"])
                for name in archive.namelist()[:-1]:
                    self.assertIn("\u0645\u0631\u062d\u0628\u0627", archive.read(name).decode("utf-8"))
                    self.assertIn("00:00.000 --> 00:02.000", archive.read(name).decode("utf-8"))
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(call[2] is None for call in calls))
            self.assertTrue(any("File 3/3" in step for step in steps))
            failing = SimpleNamespace(translate_transcript=AsyncMock(side_effect=RuntimeError("Provider unavailable")))
            with self.assertRaisesRegex(RuntimeError, "Lecture.vtt: Provider unavailable"):
                asyncio.run(translate_vtt_batch([("Lecture.vtt", parse_vtt(content))],
                            "en", "", failing, temporary, steps.append))

    def test_upload_route_needs_no_video_and_snapshots_files(self):
        source = Path(__file__).resolve().parents[1] / "main.py"
        node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_vtt_translation_job")
        app = FastAPI()
        store = SimpleNamespace(start=Mock(return_value="a" * 32))
        renderer = AsyncMock()
        processor = object()
        namespace = dict(app=app, File=File, Form=Form, UploadFile=UploadFile,
                         HTTPException=HTTPException, MAX_FILES=MAX_FILES, MAX_FILE_BYTES=MAX_FILE_BYTES,
                         MAX_BATCH_BYTES=MAX_BATCH_BYTES, TRANSLATION_MODELS=TRANSLATION_MODELS,
                         parse_vtt=parse_vtt, export_jobs=store, translate_vtt_batch=renderer,
                         summary_processor=processor)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        client = TestClient(app)
        data = {"target_language": "de", "model": "groq:openai/gpt-oss-120b"}
        files = [("files", ("One.vtt", VTT, "text/vtt")), ("files", ("Two.vtt", VTT, "text/vtt"))]
        response = client.post("/vtt_translation_jobs", data=data, files=files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_id"], "a" * 32)
        self.assertEqual(store.start.call_args.args[0], "vtt-batch")
        for service in ("libretranslate", "microsoft-translator"):
            self.assertEqual(client.post("/vtt_translation_jobs",
                data={"target_language": "de", "model": service, "allow_fallback": "false"},
                files=files).status_code, 200)
        response = client.post("/vtt_translation_jobs", data=data, files=files)
        generate = store.start.call_args.args[2]
        asyncio.run(generate("vtt-batch", {}, print, None, Path("workspace")))
        args = renderer.call_args.args
        self.assertEqual([name for name, parsed in args[0]], ["One.vtt", "Two.vtt"])
        self.assertEqual(args[1:4], ("de", data["model"], processor))
        for bad in ({"target_language": "unknown"}, {"target_language": "en", "model": "unknown"}):
            self.assertEqual(client.post("/vtt_translation_jobs", data=bad, files=files).status_code, 400)
        self.assertEqual(client.post("/vtt_translation_jobs",
                         data={"target_language": "en", "allow_fallback": "false"}, files=files).status_code, 400)
        self.assertEqual(client.post("/vtt_translation_jobs", data=data,
                                    files=[("files", ("x.txt", VTT))]).status_code, 400)
        self.assertEqual(client.post("/vtt_translation_jobs", data=data,
                                    files=[("files", ("x.vtt", b"invalid"))]).status_code, 400)
        self.assertEqual(client.post("/vtt_translation_jobs", data=data,
                                    files=[("files", ("x.vtt", b"x" * (MAX_FILE_BYTES + 1)))]).status_code, 413)
        self.assertEqual(client.post("/vtt_translation_jobs", data=data,
                                    files=files * 16).status_code, 400)
        with tempfile.TemporaryDirectory() as temporary:
            store = ExportJobStore(Path(temporary) / "jobs")
            async def translate(segments, target, model, on_progress, allow_fallback=True):
                return {"transcript": [{"text": item["text"].replace("Hello", "Translated")}
                                       for item in segments]}
            namespace["export_jobs"] = store
            namespace["summary_processor"] = SimpleNamespace(translate_transcript=translate)
            namespace["translate_vtt_batch"] = translate_vtt_batch
            response = client.post("/vtt_translation_jobs", data=data, files=files)
            self.assertEqual(response.status_code, 200)
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = store.read(job_id)
                if job["status"] in {"complete", "error"}:
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "complete", job)
            persisted = store.file(job_id, job["download_file"])
            with zipfile.ZipFile(persisted) as archive:
                self.assertEqual(archive.namelist(), ["One_DE.vtt", "Two_DE.vtt", "translation-report.json"])
                self.assertIn(b"Translated", archive.read("One_DE.vtt"))
            self.assertEqual(ExportJobStore(store.root).read(job_id)["status"], "complete")
        namespace["MAX_FILE_BYTES"] = 100
        namespace["MAX_BATCH_BYTES"] = 120
        small = b"WEBVTT\n\n00:00.000 --> 00:01.000\n" + b"a" * 40
        response = client.post("/vtt_translation_jobs", data=data,
                               files=[("files", ("x.vtt", small)), ("files", ("y.vtt", small))])
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
