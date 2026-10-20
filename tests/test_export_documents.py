"""Exercise export helpers/endpoint without loading transcription or GPU models."""
import ast
import asyncio
from collections import Counter
import html
import os
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import zipfile

from docx import Document
from docx.shared import Inches
from docx.image.exceptions import UnrecognizedImageError
from docx.oxml import OxmlElement
from PIL import Image as PillowImage, UnidentifiedImageError
from reportlab.lib.pagesizes import letter
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import inch
from reportlab.platypus import Image as PdfImage, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
from export_jobs import ExportJobStore, export_workspace
from ffmpeg_tools import media_executable


ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def load_export_functions():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    names = {
        "build_outline_points", "parse_chapter_timestamp", "format_chapter_timestamp",
        "chapter_boundaries_from_topics", "chapter_boundaries_from_scenes", "export_slide_start",
        "export_sentences", "align_export_chapters", "export_key_points", "summarize_export_batch",
        "build_export_subparts", "export_archive_paths", "prepare_export_image",
        "concise_export_title", "export_thumbnail", "strip_narration", "clean_outline_title", "build_slides_pdf",
        "create_combined_chapter_documents", "collapse_word_heading", "export_chapters",
        "scene_slide_text", "chapter_slide_content", "add_word_slide_content", "saved_section_points",
        "is_rtl_text", "make_word_rtl", "pdf_rtl_support",
    }
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(globals(), Request=object, FileResponse=lambda path, **kwargs: SimpleNamespace(path=path, **kwargs))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
    return namespace


class ExportDocumentTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / (".export-tests-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.functions = load_export_functions()
        self.functions["media_executable"] = lambda name: name
        for name in ("EXPORTS_DIR", "TRANSCRIPTS_DIR", "SUMMARIES_DIR", "SCENES_DIR", "VIDEO_DIR", "FULLSIZE_IMAGES_DIR"):
            directory = self.directory / name
            directory.mkdir()
            self.functions[name] = directory
        self.functions["summary_processor"] = SimpleNamespace(
            _refresh_clients=lambda: None, _provider_chain=lambda: [],
        )
        self.functions["video_processor"] = SimpleNamespace(get_video_path=lambda _: self.directory / "lecture.mp4")
        self.transcript = [
            {"start": 0, "duration": 4, "text": "Neural networks learn"},
            {"start": 4, "duration": 4, "text": "from examples. Training adjusts weights."},
            {"start": 8, "duration": 4, "text": "Validation measures generalization."},
            {"start": 12, "duration": 4, "text": "Regularization prevents overfitting."},
        ]
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture.json").write_text(json.dumps(self.transcript), encoding="utf-8")
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": "Neural networks"},
            {"timestamp": "00:03", "title": "Training and evaluation"},
        ]), encoding="utf-8")
        self.functions["SCENES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": "Learning"},
            {"timestamp": "00:09", "title": "Validation diagram"},
        ]), encoding="utf-8")

    def run_export(self, **options):
        self.media_commands = []
        async def request_json():
            return options

        def run_media(command, **kwargs):
            self.media_commands.append(command)
            if command[0] == "ffprobe":
                return SimpleNamespace(stdout="16")
            target = Path(command[-1])
            if target.suffix == ".jpg":
                PillowImage.new("RGB", (32, 18), "white").save(target)
            else:
                if target.suffix == ".mp4":
                    self.assertEqual(command[command.index("-preset") + 1], "veryfast")
                    self.assertEqual(command[command.index("-crf") + 1], "23")
                target.write_bytes(b"test video")
            return SimpleNamespace(returncode=0)

        async def encode_clip(command, step, label, duration=None):
            run_media(command)
        with mock.patch.object(subprocess, "run", side_effect=run_media), \
                mock.patch("ffmpeg_tools.encode_export_clip", side_effect=encode_clip):
            return asyncio.run(self.functions["export_chapters"]("lecture", SimpleNamespace(json=request_json)))

    def test_sentence_alignment_preserves_every_word_once(self):
        sentences = self.functions["export_sentences"](self.transcript)
        self.assertEqual(sentences[0]["text"], "Neural networks learn from examples.")
        chapters = self.functions["align_export_chapters"]([
            {"timestamp": "00:00", "title": "First"},
            {"timestamp": "00:03", "title": "Second"},
        ], sentences)
        self.assertGreater(chapters[1]["start"], 3)
        self.assertEqual(chapters[1]["start"], sentences[1]["start"])
        chunks = [[item["text"] for item in sentences if item["start"] >= part["start"]
                   and (part["end"] is None or item["start"] < part["end"])] for part in chapters]
        self.assertEqual(" ".join(text for chunk in chunks for text in chunk),
                         " ".join(item["text"] for item in self.transcript))

    def test_manual_chapter_and_section_timestamps_are_not_sentence_aligned(self):
        boundaries = self.functions["chapter_boundaries_from_topics"]([
            {"timestamp": "00:01.500", "title": "Edited", "timestamp_edited": True,
             "sections": [{"timestamp": "00:02.500", "title": "Section", "point": "Text",
                           "timestamp_edited": True}]},
            {"timestamp": "00:07", "title": "Next", "timestamp_edited": True},
        ])
        sentences = self.functions["export_sentences"](self.transcript)
        chapters = self.functions["align_export_chapters"](boundaries, sentences)
        self.assertEqual([chapter["start"] for chapter in chapters], [1.5, 7])
        points = self.functions["saved_section_points"](chapters[0])
        parts = self.functions["build_export_subparts"](chapters[0], sentences, points, [], "points")
        self.assertTrue(points[0]["timestamp_edited"])
        self.assertEqual(points[0]["start"], 2.5)
        self.assertEqual([part["start"] for part in parts], [1.5, 2.5])
        self.assertEqual(chapters[0]["end"], 7)

    def test_exports_use_selected_uploaded_transcript(self):
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture_uploaded.json").write_text(
            json.dumps([{"start": 0, "text": "User corrected upload."}]), encoding="utf-8",
        )
        response = self.run_export(include_transcripts=True, transcript_source="uploaded")
        with zipfile.ZipFile(response.path) as archive:
            text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".txt"))
        self.assertIn("User corrected upload.", text)
        self.assertNotIn("Neural networks learn", text)

    def test_user_edited_titles_and_summaries_are_not_rewritten_for_export(self):
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": "My exact title", "summary": "My exact summary",
             "user_edited": True, "sections": []},
        ]), encoding="utf-8")
        response = self.run_export(include_outline=True)
        document = Document(response.path)
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertIn("My exact title", text)
        self.assertIn("My exact summary", text)

    def test_faster_clip_exports_preserve_selected_caption_stream_mapping(self):
        self.functions["caption_copy_path"] = lambda video_id, job_id: self.directory / "captioned.mp4"
        self.run_export(include_clips=True, media_export_id="selected-copy")
        commands = [command for command in self.media_commands if command[-1].endswith(".mp4")]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("0:s?", command)
            self.assertEqual(command[command.index("-c:s") + 1], "mov_text")
            self.assertEqual(command[command.index("-i") + 1], str(self.directory / "captioned.mp4"))

    def create_detected_images(self):
        directory = self.functions["FULLSIZE_IMAGES_DIR"] / "lecture"
        directory.mkdir()
        for index in range(2):
            PillowImage.new("RGB", (32, 18), "white").save(directory / f"{index}.jpg")

    def test_screenshot_transcript_only_downloads_one_html_without_ai_or_ffmpeg(self):
        self.create_detected_images()
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text("invalid saved summary")
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture.json").unlink()
        selected = [
            {"start": 0, "duration": 1, "text": "Currently displayed translation."},
            {"start": 9, "duration": 1, "text": "Second screenshot translation."},
        ]
        response = self.run_export(
            include_screenshot_transcript=True, screenshot_transcript=selected,
            document_title="Screenshots only", export_filename="my_lecture.zip",
        )
        self.assertEqual(response.path.suffix, ".html")
        self.assertEqual(response.filename, "my_lecture.html")
        self.assertEqual(response.media_type, "text/html")
        output = response.path.read_text(encoding="utf-8")
        self.assertIn("<h1 dir=\"auto\">Screenshots only</h1>", output)
        self.assertIn(selected[0]["text"], output)
        self.assertIn(selected[1]["text"], output)
        self.assertEqual(output.count('src="data:image/jpeg;base64,'), 2)
        self.assertNotIn("Neural networks", output)
        self.assertNotIn("<video", output)
        self.assertEqual(self.media_commands, [], "No clip encoding or duration probing")

    def test_screenshot_html_can_be_packaged_with_other_deliverables(self):
        self.create_detected_images()
        response = self.run_export(include_screenshot_transcript=True, include_transcripts=True)
        with zipfile.ZipFile(response.path) as archive:
            self.assertIn("screenshots_transcript.html", archive.namelist())
            self.assertIn("transcript_by_chapter.md", archive.namelist())
            self.assertNotIn("index.html", archive.namelist())
            self.assertFalse(any(name.endswith((".jpg", ".mp4")) for name in archive.namelist()))
            self.assertIn("Neural networks learn", archive.read("screenshots_transcript.html").decode())

    def test_screenshot_html_needs_detected_slides_and_transcript(self):
        self.create_detected_images()
        with self.assertRaises(HTTPException) as raised:
            self.run_export(include_screenshot_transcript=True, screenshot_transcript=[])
        self.assertEqual(raised.exception.status_code, 400)
        self.functions["SCENES_DIR"].joinpath("lecture.json").unlink()
        with self.assertRaises(HTTPException) as raised:
            self.run_export(include_screenshot_transcript=True)
        self.assertEqual(raised.exception.status_code, 404)

    def test_screenshot_html_job_download_and_open_link_survive_restart(self):
        self.create_detected_images()
        store = ExportJobStore(self.directory / "jobs")

        async def generate(video_id, options, step, artifacts, workspace):
            async def request_json():
                return options
            return await self.functions["export_chapters"](
                video_id, SimpleNamespace(json=request_json), on_step=step,
                on_artifacts=artifacts, export_root=workspace,
            )

        job_id = store.start("lecture", {"include_screenshot_transcript": True}, generate)
        for _ in range(100):
            state = store.read(job_id)
            if state["status"] in {"complete", "error"}:
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "complete", state.get("error"))
        restarted = ExportJobStore(store.root)
        saved = restarted.read(job_id)
        self.assertTrue(saved["filename"].endswith(".html"))
        self.assertTrue(saved["open_url"].endswith("/webpage/index.html"))
        downloaded = restarted.file(job_id, saved["download_file"]).read_text(encoding="utf-8")
        opened = restarted.file(job_id, "index.html", webpage=True).read_text(encoding="utf-8")
        self.assertEqual(downloaded, opened)
        self.assertEqual(opened.count('src="data:image/jpeg;base64,'), 2)

    def test_caption_copy_is_used_for_chapter_and_section_clips(self):
        copy = self.directory / "caption-copy.mp4"
        selected = []
        def resolve(video_id, job_id):
            selected.append((video_id, job_id))
            return copy
        self.functions["caption_copy_path"] = resolve
        self.run_export(media_export_id="copy-id", include_clips=True,
                        include_section_clips=True, subpart_mode="points")
        self.assertEqual(selected, [("lecture", "copy-id")])
        clips = [command for command in self.media_commands if command[-1].endswith(".mp4")]
        self.assertGreaterEqual(len(clips), 2)
        for command in clips:
            self.assertEqual(command[command.index("-i") + 1], str(copy))
            self.assertIn("0:s?", command)
            self.assertEqual(command[command.index("-c:s") + 1], "mov_text")

    def test_missing_ffmpeg_clip_export_reports_installation_guidance(self):
        def unavailable(name):
            raise RuntimeError("Install FFmpeg and restart the server")

        self.functions["media_executable"] = unavailable
        with self.assertRaisesRegex(HTTPException, "Install FFmpeg and restart"):
            self.run_export(include_webpage=True)

    def test_whisper_unpunctuated_phrases_join_until_pause_or_speaker_turn(self):
        phrases = [
            {"start": 0, "end": 2, "text": "neural networks learn"},
            {"start": 2.1, "end": 4, "text": "from examples", "speaker": "A"},
            {"start": 5.5, "duration": 2, "text": "training adjusts weights", "speaker": "A"},
            {"start": 7.6, "duration": 2, "text": "validation measures generalization", "speaker": "B"},
        ]
        sentences = self.functions["export_sentences"](phrases)
        self.assertEqual([item["text"] for item in sentences], [
            "neural networks learn from examples",
            "training adjusts weights",
            "validation measures generalization",
        ])
        parts = self.functions["align_export_chapters"]([
            {"timestamp": "00:00", "title": "Learning"},
            {"timestamp": "00:03", "title": "Evaluation"},
        ], sentences)
        self.assertEqual(parts[1]["start"], 5.5)
        self.assertEqual(" ".join(item["text"] for item in sentences),
                         " ".join(item["text"] for item in phrases))

    def test_unknown_caption_duration_does_not_invent_pauses(self):
        fragments = [
            {"start": 0, "text": "we learn"},
            {"start": 20, "text": "from examples"},
            {"start": 30, "text": "to improve predictions"},
        ]
        sentences = self.functions["export_sentences"](fragments)
        self.assertEqual(len(sentences), 1)
        self.assertEqual(sentences[0]["text"], "we learn from examples to improve predictions")

    def test_slide_content_option_adds_ocr_bullets_per_slide(self):
        def ocr(text, top):
            return {"class": "text", "bbox": [10, top, 200, top + 20], "ocr_text": text}
        self.functions["SCENES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "yolo_detections": {"success": True, "detections": [
                ocr("\u2022 Backpropagation\nGradient descent", 80), ocr("Neural Networks", 10),
                ocr("gradient descent", 120), ocr("|", 150),
            ]}},
            {"timestamp": "00:09", "surya_ocr": {"success": True, "results": [
                {"text": "Validation set", "bbox": [0, 5, 50, 20]},
                {"text": "Matched elsewhere", "bbox": [0, 30, 50, 40], "matched": True},
            ]}},
        ]), encoding="utf-8")
        response = self.run_export(include_webpage=True, include_word=True, include_pdf=True, include_outline=True, include_slide_text=True)
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
            word = Document(BytesIO(archive.read("chapter_document.docx")))
            outline = Document(BytesIO(archive.read("video_outline.docx")))
            self.assertGreater(len(archive.read("chapter_document.pdf")), 1000)
        self.assertIn("Validation set", [p.text for p in outline.paragraphs])
        self.assertEqual(webpage.count('<details class="slide-content"'), 2)
        self.assertIn("<ul><li>Neural Networks</li><li>Backpropagation</li><li>Gradient descent</li></ul>", webpage)
        self.assertIn("<li>Validation set</li>", webpage)
        self.assertNotIn("Matched elsewhere", webpage)
        bullets = [p.text for p in word.paragraphs if p.style.name == "List Bullet"]
        self.assertIn("Backpropagation", bullets)
        self.assertIn("Validation set", bullets)

        plain = self.run_export(include_webpage=True)
        with zipfile.ZipFile(plain.path) as archive:
            self.assertNotIn("slide-content\"", archive.read("index.html").decode())

    def test_slide_media_links_preserve_fractional_scene_time(self):
        self.functions["SCENES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:09", "time_seconds": 9.375, "title": "Validation"},
        ]), encoding="utf-8")
        response = self.run_export(include_webpage=True, subpart_mode="slides")
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
            self.assertIn("slide_0000009375.jpg", archive.namelist())
        self.assertIn("#t=4.775", webpage)

    def test_clip_overlap_adds_one_second_without_changing_transcript_boundaries(self):
        response = self.run_export(include_webpage=True, include_transcripts=True)
        clips = [command for command in self.media_commands if command[-1].endswith(".mp4")]
        starts = [float(command[command.index("-ss") + 1]) for command in clips]
        self.assertEqual(starts[0], 0)
        self.assertAlmostEqual(starts[1], 4.6)
        self.assertAlmostEqual(float(clips[0][clips[0].index("-t") + 1]), 5.6)
        self.assertNotIn("-t", clips[1], "The last chapter must continue to the video end")
        with zipfile.ZipFile(response.path) as archive:
            chapters = json.loads(archive.read("chapters.json"))
            self.assertAlmostEqual(chapters[1]["start"], 5.6)
            self.assertAlmostEqual(chapters[1]["clip_start"], 4.6)
            webpage = archive.read("index.html").decode()
            self.assertNotIn("overlap", webpage)
            self.assertNotIn("Clip starts", webpage)

    def test_clip_overlap_can_be_disabled(self):
        self.run_export(include_webpage=True, clip_overlap=False)
        clips = [command for command in self.media_commands if command[-1].endswith(".mp4")]
        self.assertAlmostEqual(float(clips[1][clips[1].index("-ss") + 1]), 5.6)

    def test_clip_overlap_is_clamped_at_video_start(self):
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"start": 0, "duration": 0.4, "text": "First sentence."},
            {"start": 0.4, "duration": 15.6, "text": "Second sentence."},
        ]), encoding="utf-8")
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": "First"},
            {"timestamp": "00:00.4", "title": "Second"},
        ]), encoding="utf-8")
        self.run_export(include_webpage=True)
        clips = [command for command in self.media_commands if command[-1].endswith(".mp4")]
        self.assertEqual(len(clips), 2)
        self.assertEqual(float(clips[1][clips[1].index("-ss") + 1]), 0)

    def test_html_archive_has_real_hierarchy_and_only_required_assets(self):
        response = self.run_export(include_webpage=True, subpart_mode="both")
        with zipfile.ZipFile(response.path) as archive:
            names = archive.namelist()
            self.assertIn("index.html", names)
            self.assertTrue(all(name == "index.html" or name.endswith((".jpg", ".mp4")) for name in names), names)
            webpage = archive.read("index.html").decode()
            self.assertIn('<h2 title="Neural networks">Part 1: Neural networks</h2>', webpage)
            self.assertGreaterEqual(webpage.count("<h3 "), 3)
            self.assertIn('<details class="chapter-subparts"', webpage)
            self.assertIn('<details class="subpart"', webpage)
            self.assertNotIn("AI-generated", webpage)
            self.assertNotIn("AI not configured", webpage)
            self.assertIn('<nav class="outline"', webpage)
            self.assertRegex(webpage, r'<details class="outline-chapter"><summary><a href="#part-\d+">')
            self.assertIn('class="outline-toggle"', webpage)
            self.assertIn('href="#part-2"', webpage)
            self.assertRegex(webpage, r'<figcaption title="Neural networks">1: Neural networks</figcaption>')
            self.assertNotIn("<summary>Key point", webpage)
            self.assertIn("2: 00:00:09", webpage)
            self.assertRegex(webpage, r"<h3[^>]*>[^<]+\(00:00:\d\d\)</h3>")
            for reference in re.findall(r'(?:src|href)="([^"#][^"]*)"', webpage):
                self.assertIn(html.unescape(reference).split("#")[0], names)
            transcript_blocks = re.findall(r"<pre>(.*?)</pre>", webpage, re.DOTALL)
            self.assertEqual(" ".join(html.unescape(block).replace("\n", " ") for block in transcript_blocks),
                             " ".join(item["text"] for item in self.transcript))
        print("HTML: timestamped chapter/subpart headings, complete sentences, valid slide links; ZIP =", names)

    def test_default_export_uses_content_chapters_without_slide_sections(self):
        response = self.run_export(include_webpage=True)
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
            names = archive.namelist()
        self.assertIn("Part 2: Training and evaluation", webpage)
        self.assertNotIn("Slide 2:", webpage)
        self.assertFalse(any(name.startswith("slide_") for name in names), names)

    def test_opening_speech_is_preserved_in_first_real_section(self):
        sentences = [
            {"start": 0, "text": "Hello everyone, welcome to this course."},
            {"start": 5, "text": "We will create a course."},
            {"start": 10, "text": "Next we upload a video."},
        ]
        chapter = {"start": 0, "end": 20, "title": "Course setup"}
        points = [
            {"start": 5, "title": "Create a course", "text": "Create the course."},
            {"start": 10, "title": "Upload a video", "text": "Upload the video."},
        ]
        parts = self.functions["build_export_subparts"](chapter, sentences, points, [], "points")
        self.assertEqual([part["title"] for part in parts], ["Create a course", "Upload a video"])
        self.assertEqual([part["start"] for part in parts], [0, 10])
        self.assertEqual([sentence for part in parts for sentence in part["sentences"]], sentences)
        self.assertEqual(parts[0]["points"], [points[0]])

    def test_unnamed_slide_sections_use_topic_not_transcript_fragment(self):
        chapter = {"start": 0, "end": 20, "title": "Course setup"}
        sentences = [{"start": 0, "text": "Hello everyone, welcome to this course."},
                     {"start": 10, "text": "Now let me show you this."}]
        parts = self.functions["build_export_subparts"](
            chapter, sentences, [], [{"timestamp": "00:10"}], "slides",
        )
        self.assertEqual([part["title"] for part in parts], ["Course setup", "Course setup"])
        self.assertEqual([part["start"] for part in parts], [0, 10])
        named = self.functions["build_export_subparts"](
            chapter, sentences, [], [{"timestamp": "00:10"}], "slides", {10: "Upload a video"},
        )
        self.assertEqual(named[1]["title"], "Upload a video")

    def test_html_warns_when_archive_media_cannot_load(self):
        response = self.run_export(include_webpage=True)
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn('id="media-help" role="alert" hidden', webpage)
        self.assertIn('use Extract All', webpage)
        self.assertIn("document.addEventListener('error'", webpage)
        self.assertIn("image.complete&&!image.naturalWidth", webpage)

    def test_explicit_transcripts_and_formats_are_preserved(self):
        response = self.run_export(include_webpage=True, include_transcripts=True,
                                   include_word=True, include_pdf=True, include_outline=True)
        with zipfile.ZipFile(response.path) as archive:
            names = archive.namelist()
            for name in ("chapter_document.docx", "chapter_document.pdf", "video_outline.docx",
                         "transcript_by_chapter.md", "chapters.json"):
                self.assertIn(name, names)
            self.assertTrue(any(name.endswith(".txt") for name in names))
            self.assertFalse(any(name.endswith(".png") for name in names))
        print("Mixed-format ZIP preserves explicitly selected documents and transcript files.")

    def test_no_format_selected_defaults_to_html(self):
        response = self.run_export()
        self.assertEqual(response.path.suffix, ".zip")
        with zipfile.ZipFile(response.path) as archive:
            self.assertIn("index.html", archive.namelist())
            self.assertFalse(any(name.endswith((".docx", ".txt", ".pdf")) for name in archive.namelist()))

    def test_selected_word_has_subpart_headings_and_full_sentences(self):
        response = self.run_export(include_word=True)
        self.assertEqual(response.path.suffix, ".docx")
        document = Document(response.path)
        headings = [paragraph.text for paragraph in document.paragraphs
                    if paragraph.style.name.startswith("Heading")]
        self.assertTrue(any("Part 1: Neural networks" in heading for heading in headings))
        self.assertTrue(any("Training adjusts weights" in heading for heading in headings))
        self.assertTrue(any("(00:00:" in heading for heading in headings))
        self.assertIn("Neural networks learn from examples.", [p.text for p in document.paragraphs])
        print("Word: verified actual document heading hierarchy =", headings)

    def test_pdf_has_actual_subpart_headings(self):
        with mock.patch.dict(self.functions, {"Paragraph": mock.Mock(wraps=Paragraph)}):
            response = self.run_export(include_pdf=True)
            paragraphs = self.functions["Paragraph"].call_args_list
        self.assertEqual(response.path.suffix, ".pdf")
        self.assertGreater(response.path.stat().st_size, 1000)
        self.assertTrue(any(call.args[1].name == "Heading2" for call in paragraphs))
        print("PDF: built", response.path.stat().st_size, "bytes with timestamped Heading2 subparts.")

    def test_slides_pdf_has_one_image_page_per_detected_slide(self):
        folder = self.functions["FULLSIZE_IMAGES_DIR"] / "lecture"
        folder.mkdir()
        for index, size in enumerate([(80, 40), (40, 80)]):
            PillowImage.new("RGB", size, "blue").save(folder / f"{index}.jpg")
        destination = self.directory / "slides.pdf"
        self.functions["build_slides_pdf"]("lecture", destination)
        pdf = destination.read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", pdf)), 2)
        self.assertEqual(len(re.findall(rb"/Subtype\s*/Image\b", pdf)), 2)
        self.assertRegex(pdf, rb"/MediaBox\s*\[\s*0 0 80 40\s*\]")
        self.assertRegex(pdf, rb"/MediaBox\s*\[\s*0 0 40 80\s*\]")
        (folder / "1.jpg").unlink()
        with self.assertRaises(HTTPException) as raised:
            self.functions["build_slides_pdf"]("lecture", destination)
        self.assertEqual(raised.exception.status_code, 409)

    def test_compact_export_thumbnails_and_full_title_on_hover(self):
        title = "A lengthy title describing neural networks and many learning examples"
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": title},
        ]), encoding="utf-8")
        response = self.run_export(include_webpage=True)
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn(f'title="{title}"', webpage)
        self.assertLessEqual(len(self.functions["concise_export_title"](title)), 36)
        self.assertIn('class="chapter-heading"', webpage)
        self.assertIn('class="subpart-heading"', webpage)
        self.assertIn('.export-thumbnail img{width:120px;height:68px', webpage)
        self.assertIn('target="_blank" rel="noopener" title="Enlarge:', webpage)

    def test_slides_pdf_http_download_and_temporary_cleanup(self):
        from fastapi import FastAPI, HTTPException as FastAPIException
        from fastapi.responses import FileResponse
        from fastapi.testclient import TestClient
        from starlette.background import BackgroundTask
        from export_jobs import cleanup_export_workspace
        folder = self.functions["FULLSIZE_IMAGES_DIR"] / "lecture"
        folder.mkdir()
        for index in range(2):
            PillowImage.new("RGB", (40, 20), "blue").save(folder / f"{index}.jpg")
        app = FastAPI()
        namespace = dict(self.functions, app=app, FileResponse=FileResponse,
                         HTTPException=FastAPIException, BackgroundTask=BackgroundTask,
                         cleanup_export_workspace=cleanup_export_workspace)
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        route = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == "download_slides_pdf")
        builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "build_slides_pdf")
        exec(compile(ast.Module(body=[builder, route], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
        with TestClient(app) as client:
            response = client.get("/download_slides_pdf/lecture")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF-"))
            self.assertEqual(client.get("/download_slides_pdf/missing").status_code, 404)

    def test_saved_slides_are_copied_not_reextracted(self):
        folder = self.functions["FULLSIZE_IMAGES_DIR"] / "lecture"
        folder.mkdir()
        for index in range(2):
            PillowImage.new("RGB", (40, 20), "blue").save(folder / f"{index}.jpg")
        response = self.run_export(include_webpage=True, subpart_mode="both")
        image_times = [float(command[command.index("-ss") + 1])
                       for command in self.media_commands if command[-1].endswith(".jpg")]
        self.assertNotIn(0, image_times)
        self.assertNotIn(9, image_times)
        self.assertEqual(image_times, [], "Chapter images reuse the slide on screen at the chapter start")
        with zipfile.ZipFile(response.path) as archive:
            self.assertEqual(archive.read("slide_0000000000.jpg"), (folder / "0.jpg").read_bytes())
            self.assertEqual(archive.read("slide_0000009000.jpg"), (folder / "1.jpg").read_bytes())
            self.assertEqual(archive.read("01_Neural_networks.jpg"), (folder / "0.jpg").read_bytes())
            self.assertEqual(archive.read("02_Training_and_evaluation.jpg"), (folder / "0.jpg").read_bytes())

    def test_missing_saved_images_are_extracted_only_once_per_timestamp(self):
        self.run_export(include_webpage=True, subpart_mode="both")
        image_times = [float(command[command.index("-ss") + 1])
                       for command in self.media_commands if command[-1].endswith(".jpg")]
        self.assertEqual(image_times.count(0), 1, "Chapter/slide images at the same time should share a capture")
        self.assertEqual(image_times.count(9), 1)

    def test_packaged_export_survives_locked_intermediate_files(self):
        paths = set()
        def locked(path):
            paths.add(Path(path))
            raise OSError(145, "The directory is not empty")
        try:
            with mock.patch("export_jobs.shutil.rmtree", side_effect=locked), \
                    mock.patch("export_jobs.time.sleep"), self.assertLogs("export_jobs", level="WARNING"):
                response = self.run_export(include_webpage=True)
            with zipfile.ZipFile(response.path) as archive:
                self.assertIn("index.html", archive.namelist())
                self.assertIsNone(archive.testzip())
        finally:
            for path in paths:
                shutil.rmtree(path)

    def test_ai_points_use_specific_titles_and_errors_are_not_hidden(self):
        processor = self.functions["summary_processor"]
        processor._provider_chain = lambda: [("gemini", "test-model")]
        calls = []

        def complete(*args):
            calls.append(args)
            return json.dumps({
                "summary": "Neural networks learn patterns from examples.",
                "points": [{
                    "sentence_id": 0, "title": "Learning from examples",
                    "text": "Neural networks learn patterns from examples.",
                }],
            })

        processor._complete = complete
        sentences = self.functions["export_sentences"](self.transcript)
        points, method, summary = asyncio.run(self.functions["export_key_points"](sentences))
        self.assertEqual(points[0]["title"], "Learning from examples")
        self.assertEqual(method, "AI-summarized key points")
        self.assertEqual(summary, "Neural networks learn patterns from examples.")
        self.assertEqual(len(calls), 1)
        processor._complete = lambda *args: json.dumps({
            "summary": "A valid short summary.",
            "points": [{"sentence_id": 0, "title": "Key point", "text": "Fake."}],
        })
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(self.functions["export_key_points"](sentences))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("generic", raised.exception.detail)

    def test_narration_fillers_are_removed_and_ai_titles_are_used(self):
        strip = self.functions["strip_narration"]
        self.assertEqual(strip("The lecture explains that gradient descent minimizes the loss."),
                         "Gradient descent minimizes the loss.")
        self.assertEqual(strip("The speaker points out that grades are weighted."), "Grades are weighted.")
        self.assertEqual(strip("In this section, the instructor discusses regularization methods for deep networks."),
                         "Regularization methods for deep networks.")
        self.assertEqual(strip("Validation measures generalization."), "Validation measures generalization.")
        self.assertEqual(self.functions["clean_outline_title"]("Navigating to assignments and ..."),
                         "Navigating to assignments and")
        processor = self.functions["summary_processor"]
        processor._provider_chain = lambda: [("gemini", "test-model")]
        prompts = []

        def complete(provider, model, prompt, temperature):
            prompts.append(prompt)
            return json.dumps({
                "title": "Learning From Examples",
                "summary": "The lecture explains that networks learn from examples.",
                "points": [{"sentence_id": 0, "title": "Learning", "text": "The speaker notes that data matters."}],
                "section_titles": {"2": "Validation Diagram"},
            })

        processor._complete = complete
        response = self.run_export(include_webpage=True, subpart_mode="both")
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn("Never mention the lecture, speaker", prompts[0])
        self.assertIn("Part 1: Learning From Examples</h2>", webpage)
        self.assertIn("Networks learn from examples.", webpage)
        self.assertIn("Data matters.", webpage)
        self.assertNotIn("The lecture explains", webpage)
        self.assertIn("Validation Diagram", webpage)

    def test_long_chapters_keep_bounded_batches_and_short_combined_summary(self):
        processor = self.functions["summary_processor"]
        processor._provider_chain = lambda: [("groq", "test-model")]
        calls = []

        def complete(*args):
            calls.append(args)
            return json.dumps({
                "summary": "The chapter explains learning.",
                "points": [{"sentence_id": 0, "title": "Learning", "text": "Learning uses examples."}],
            })

        processor._complete = complete
        sentences = [{"start": index, "text": "Examples " * 700 + "."} for index in range(3)]
        points, _, summary = asyncio.run(self.functions["export_key_points"](sentences))
        self.assertEqual(len(calls), 4)
        self.assertEqual([point["start"] for point in points], [0, 1, 2])
        self.assertEqual(summary, "The chapter explains learning.")
        self.assertTrue(all(len(call[2]) < 11000 for call in calls))

    def test_one_ai_request_per_chapter_supplies_html_word_and_pdf_summaries(self):
        processor = self.functions["summary_processor"]
        processor._provider_chain = lambda: [("gemini", "test-model")]
        calls = []

        def complete(*args):
            calls.append(args)
            return json.dumps({
                "summary": "This chapter teaches neural network fundamentals.",
                "points": [{
                    "sentence_id": 0,
                    "title": "Neural network fundamentals",
                    "text": "Neural networks learn patterns from examples.",
                }],
            })

        processor._complete = complete
        paragraph = mock.Mock(wraps=Paragraph)
        with mock.patch.dict(self.functions, {"Paragraph": paragraph}):
            response = self.run_export(
                include_webpage=True, include_word=True, include_pdf=True
            )

        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
            document = Document(BytesIO(archive.read("chapter_document.docx")))
        self.assertEqual(len(calls), 2)
        self.assertEqual(webpage.count("This chapter teaches neural network fundamentals."), 2)
        self.assertEqual(webpage.count('<details class="chapter-subparts"'), 2)
        document_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        self.assertEqual(
            document_text.count(
                "Summary: This chapter teaches neural network fundamentals."
            ),
            2,
        )
        self.assertTrue(any(
            "<b>Summary:</b> This chapter teaches neural network fundamentals."
            in str(call.args[0])
            for call in paragraph.call_args_list
        ))

    def test_saved_chapter_sections_are_reused_without_ai_calls(self):
        self.functions["SUMMARIES_DIR"].joinpath("lecture.json").write_text(json.dumps([
            {"timestamp": "00:00", "title": "Neural networks", "summary": "How networks learn from data.",
             "sections": [
                 {"timestamp": "00:00", "title": "Learning from examples",
                  "point": "The speaker explains that networks learn from examples."},
                 {"timestamp": "00:04", "title": "Weight updates", "point": "Training adjusts the weights."},
             ]},
            {"timestamp": "00:08", "title": "Evaluation", "summary": "Checking generalization.",
             "sections": [{"timestamp": "00:08", "title": "Validation", "point": "Validation measures generalization."}]},
        ]), encoding="utf-8")
        processor = self.functions["summary_processor"]
        processor._provider_chain = lambda: [("gemini", "test-model")]
        processor._complete = mock.Mock(side_effect=AssertionError("export must reuse saved sections"))
        response = self.run_export(include_webpage=True, include_word=True)
        processor._complete.assert_not_called()
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn("How networks learn from data.", webpage)
        self.assertIn("1.2 Weight updates", webpage)
        self.assertIn("2.1 Validation", webpage)
        self.assertIn("<li>Networks learn from examples.</li>", webpage)

    def write_sectioned_summary(self, name="lecture.json", chapters=None):
        self.functions["SUMMARIES_DIR"].joinpath(name).write_text(json.dumps(chapters or [
            {"timestamp": "00:00", "title": "Neural networks", "summary": "How networks learn from data.",
             "sections": [
                 {"timestamp": "00:00", "title": "Learning from examples", "point": "Networks learn from examples."},
                 {"timestamp": "00:04", "title": "Weight updates", "point": "Training adjusts the weights."},
             ]},
            {"timestamp": "00:08", "title": "Evaluation", "summary": "Checking generalization.",
             "sections": [{"timestamp": "00:08", "title": "Validation", "point": "Validation measures generalization."}]},
        ], ensure_ascii=False), encoding="utf-8")

    def test_section_clips_are_encoded_and_shown_under_each_section(self):
        self.write_sectioned_summary()
        response = self.run_export(include_webpage=True, include_section_clips=True)
        section_commands = [command for command in self.media_commands if re.search(r"_\d\d\.mp4$", command[-1])]
        self.assertEqual([Path(command[-1]).name for command in section_commands],
                         ["01_Neural_networks_01.mp4", "01_Neural_networks_02.mp4"])
        # The second section clip starts one second before its sentence (overlap).
        start = float(section_commands[1][section_commands[1].index("-ss") + 1])
        self.assertTrue(3.0 <= start < 5.0, start)
        with zipfile.ZipFile(response.path) as archive:
            names = archive.namelist()
            webpage = archive.read("index.html").decode()
        self.assertIn("01_Neural_networks_02.mp4", names)
        self.assertIn('class="section-video" controls preload="none" src="01_Neural_networks_02.mp4"', webpage)
        self.assertEqual(webpage.count('class="section-video"'), 2)

        response = self.run_export(include_section_clips=True)
        with zipfile.ZipFile(response.path) as archive:
            self.assertIn("01_Neural_networks_01.mp4", archive.namelist())

    def test_arabic_transcript_exports_right_to_left(self):
        arabic = "\u0627\u0644\u0634\u0628\u0643\u0627\u062a \u0627\u0644\u0639\u0635\u0628\u064a\u0629 \u062a\u062a\u0639\u0644\u0645 \u0645\u0646 \u0627\u0644\u0623\u0645\u062b\u0644\u0629."
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture_translated_ar.json").write_text(json.dumps([
            {"start": 0, "duration": 8, "text": arabic},
            {"start": 8, "duration": 8, "text": arabic},
        ], ensure_ascii=False), encoding="utf-8")
        self.write_sectioned_summary("lecture_summary_ar.json", [
            {"timestamp": "00:00", "title": "\u0627\u0644\u0634\u0628\u0643\u0627\u062a", "summary": arabic,
             "sections": [{"timestamp": "00:00", "title": "\u0627\u0644\u062a\u0639\u0644\u0645", "point": arabic}]},
        ])
        self.assertTrue(self.functions["is_rtl_text"](arabic))
        self.assertTrue(self.functions["is_rtl_text"]("Hello", "ar"))
        self.assertFalse(self.functions["is_rtl_text"]("Hello class"))
        response = self.run_export(
            include_webpage=True, include_word=True, include_pdf=True, include_outline=True, transcript_language="ar",
        )
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
            word_xml = zipfile.ZipFile(BytesIO(archive.read("chapter_document.docx"))).read("word/document.xml").decode()
            outline_xml = zipfile.ZipFile(BytesIO(archive.read("video_outline.docx"))).read("word/document.xml").decode()
            pdf = archive.read("chapter_document.pdf")
        self.assertIn('<html lang="ar" dir="rtl">', webpage)
        self.assertIn("padding-inline-start", webpage)
        for xml in (word_xml, outline_xml):
            self.assertIn("<w:bidi/>", xml)
            self.assertIn("<w:rtl/>", xml)
        self.assertTrue(pdf.startswith(b"%PDF"))
        if self.functions["pdf_rtl_support"]() is not None:
            self.assertIn(b"FontFile2", pdf)

    def test_translation_without_its_own_chapters_does_not_reuse_foreign_summaries(self):
        self.write_sectioned_summary()
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture_translated_de.json").write_text(json.dumps([
            {"start": 0, "duration": 8, "text": "Neuronale Netze lernen aus Beispielen."},
            {"start": 8, "duration": 8, "text": "Die Validierung misst die Generalisierung."},
        ]), encoding="utf-8")
        response = self.run_export(include_webpage=True, transcript_language="de")
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn('<html lang="de">', webpage)
        self.assertNotIn("How networks learn from data.", webpage)
        self.assertNotIn("Training adjusts the weights.", webpage)

    def test_learner_fillers_are_removed_from_export_text(self):
        strip = self.functions["strip_narration"]
        self.assertEqual(strip("The learner will learn how to plot data."), "How to plot data.")
        self.assertEqual(strip("You will be introduced to linear models."), "Linear models.")
        self.assertEqual(strip("Students understand that tests matter."), "Tests matter.")
        self.assertEqual(strip("Data has noise."), "Data has noise.")

    def test_scorm_contains_manifest_without_internal_transcript_files(self):
        response = self.run_export(include_scorm=True)
        with zipfile.ZipFile(response.path) as archive:
            names = archive.namelist()
            self.assertIn("imsmanifest.xml", names)
            self.assertIn("scorm_api.js", names)
            self.assertFalse(any(name.endswith((".txt", ".png", ".json")) for name in names))
            manifest = archive.read("imsmanifest.xml").decode()
            for reference in re.findall(r'<file href="([^"]+)"', manifest):
                self.assertIn(reference, names)

    def test_images_and_clips_work_without_any_transcript(self):
        self.functions["TRANSCRIPTS_DIR"].joinpath("lecture.json").unlink()
        response = self.run_export(include_images=True, include_clips=True)
        with zipfile.ZipFile(response.path) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith(".jpg") for name in names))
            self.assertTrue(any(name.endswith(".mp4") for name in names))
            self.assertTrue(all(name.endswith((".jpg", ".mp4")) for name in names))

    def test_fixed_intervals_and_part_timestamp_option_remain_selectable(self):
        response = self.run_export(include_webpage=True, interval_minutes=0.1,
                                   subpart_mode="slides", timestamp_mode="part")
        with zipfile.ZipFile(response.path) as archive:
            webpage = archive.read("index.html").decode()
        self.assertIn("Interval 2", webpage)
        self.assertIn("2: 00:00:09", webpage)
        self.assertNotRegex(webpage, r"<h3>[^<]+\(00:00:")


if __name__ == "__main__":
    unittest.main()
