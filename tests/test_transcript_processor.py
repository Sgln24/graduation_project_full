import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

fastapi_module = sys.modules.setdefault("fastapi", ModuleType("fastapi"))
fastapi_responses_module = sys.modules.setdefault(
    "fastapi.responses", ModuleType("fastapi.responses")
)

class FakeJSONResponse:
    def __init__(self, content):
        self.content = content


fastapi_responses_module.JSONResponse = FakeJSONResponse
fastapi_module.responses = fastapi_responses_module

youtube_module = ModuleType("youtube_transcript_api")
youtube_module.YouTubeTranscriptApi = object
sys.modules.setdefault("youtube_transcript_api", youtube_module)

transcribe_module = ModuleType("transcribe")
transcribe_module.transcribe_audio = lambda *args, **kwargs: ()
transcribe_module.WHISPER_MODELS = ("turbo", "large-v3", "medium", "small", "base")
transcribe_module.get_whisper_device_config = lambda: ("cuda", "float16")
transcribe_module.describe_transcription_method = lambda device: "batched (batch size 16)"
sys.modules.setdefault("transcribe", transcribe_module)

from processors import transcript_processor as transcript_module
from processors.transcript_processor import TranscriptProcessor


class TranscriptProcessorReuseTests(unittest.TestCase):
    def test_cancel_restores_previous_transcript_and_clears_progress(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)), \
                mock.patch.object(TranscriptProcessor, "_terminate_whisper_process", return_value=True):
            root = Path(directory)
            segments = [{"text": "Previous", "start": 0}]
            (root / "vid_whisper_before_speakers.json").write_text(json.dumps(segments), encoding="utf-8")
            (root / "vid_whisper_progress.txt").write_text("100", encoding="utf-8")
            response = asyncio.run(TranscriptProcessor().cancel_whisper_generation("vid"))
            self.assertTrue(response.content["success"])
            self.assertEqual(response.content["transcript"], segments)
            self.assertFalse((root / "vid_whisper_progress.txt").exists())

    def test_interrupted_speaker_job_restores_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            with mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", working_dir):
                segments = [{"start": 0, "duration": 1, "text": "Hallo"}]
                (working_dir / "vid_whisper_before_speakers.json").write_text(json.dumps(segments), encoding="utf-8")
                (working_dir / "vid_whisper_progress.txt").write_text("100", encoding="utf-8")
                (working_dir / "kept_whisper.json").write_text("[]", encoding="utf-8")
                (working_dir / "kept_whisper_before_speakers.json").write_text("[]", encoding="utf-8")
                TranscriptProcessor().restore_interrupted_speaker_jobs()
                self.assertEqual(json.loads((working_dir / "vid_whisper.json").read_text(encoding="utf-8")), segments)
                self.assertFalse((working_dir / "vid_whisper_progress.txt").exists())
                self.assertFalse((working_dir / "vid_whisper_before_speakers.json").exists())
                self.assertFalse((working_dir / "kept_whisper_before_speakers.json").exists())

    def test_constructing_worker_preserves_active_speaker_job(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)):
            working_dir = Path(directory)
            backup = working_dir / "vid_whisper_before_speakers.json"
            progress = working_dir / "vid_whisper_progress.txt"
            backup.write_text('[{"text": "Hallo"}]', encoding="utf-8")
            progress.write_text("100", encoding="utf-8")
            with mock.patch.object(TranscriptProcessor, "_process_whisper_transcript_sync") as process:
                transcript_module._run_whisper_worker(
                    "vid", "video.mp4", str(working_dir / "vid_whisper.json"),
                    True, [{"text": "Hallo"}],
                )
                process.assert_called_once()
            self.assertTrue(backup.exists())
            self.assertEqual(progress.read_text(), "100")
            self.assertFalse((working_dir / "vid_whisper.json").exists())

    def test_heartbeat_does_not_recreate_completed_job_marker(self):
        class StopAfterOneHeartbeat:
            def wait(self, timeout):
                return False

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)):
            TranscriptProcessor()._heartbeat_whisper_job("video", StopAfterOneHeartbeat())
            self.assertFalse((Path(directory) / "video_whisper_progress.txt").exists())

    def test_status_handles_progress_removed_after_existence_check(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)):
            progress = str(Path(directory) / "video_whisper_progress.txt")
            original_exists = os.path.exists
            checks = []

            def exists(path):
                if str(path) == progress:
                    checks.append(path)
                    return len(checks) == 1
                return original_exists(path)

            with mock.patch.object(transcript_module.os.path, "exists", side_effect=exists):
                response = asyncio.run(TranscriptProcessor().get_whisper_status("video"))
            self.assertEqual(response.content["status"], "queued")
            self.assertTrue(response.content["success"])

    def test_heartbeat_refreshes_progress_marker(self):
        class StopAfterOneHeartbeat:
            def __init__(self):
                self.calls = 0

            def wait(self, timeout):
                self.calls += 1
                return self.calls > 1

        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            working_dir = Path(directory)
            transcript_module.TRANSCRIPTS_DIR = working_dir
            try:
                progress_path = working_dir / "video_whisper_progress.txt"
                progress_path.write_text("0", encoding="utf-8")
                old_time = time.time() - 120
                os.utime(progress_path, (old_time, old_time))
                processor = TranscriptProcessor()

                processor._heartbeat_whisper_job(
                    "video", StopAfterOneHeartbeat()
                )

                self.assertLess(time.time() - progress_path.stat().st_mtime, 5)
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir

    def test_forced_retry_reclaims_an_old_progress_marker(self):
        class BackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, function, *args):
                self.tasks.append((function, args))

        with tempfile.TemporaryDirectory() as directory:
            original_video_dir = transcript_module.VIDEO_DIR
            original_transcript_dir = transcript_module.TRANSCRIPTS_DIR
            working_dir = Path(directory)
            transcript_module.VIDEO_DIR = working_dir
            transcript_module.TRANSCRIPTS_DIR = working_dir
            try:
                (working_dir / "video.mp4").write_bytes(b"video")
                progress_path = working_dir / "video_whisper_progress.txt"
                progress_path.write_text("0", encoding="utf-8")
                old_time = time.time() - 120
                os.utime(progress_path, (old_time, old_time))
                background_tasks = BackgroundTasks()
                processor = TranscriptProcessor()
                processor.manual_retry_stale_seconds = 90

                response = asyncio.run(
                    processor.generate_whisper_transcript(
                        "video", background_tasks, force=True
                    )
                )

                self.assertEqual(response.content["phase"], "starting")
                self.assertEqual(len(background_tasks.tasks), 1)
                self.assertLess(time.time() - progress_path.stat().st_mtime, 5)
            finally:
                transcript_module.VIDEO_DIR = original_video_dir
                transcript_module.TRANSCRIPTS_DIR = original_transcript_dir

    def test_matching_model_and_prompt_allow_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            transcript_module.TRANSCRIPTS_DIR = Path(directory)
            try:
                metadata_path = Path(directory) / "video_whisper_meta.json"
                metadata_path.write_text(
                    json.dumps({
                        "model": "turbo",
                    }),
                    encoding="utf-8",
                )
                processor = TranscriptProcessor()

                self.assertTrue(
                    processor._can_reuse_whisper_transcript("video")
                )
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir

    def test_existing_transcript_skips_asr_and_runs_diarization(self):
        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            original_transcribe = transcript_module.transcribe_audio
            transcript_module.TRANSCRIPTS_DIR = Path(directory)
            transcript_module.transcribe_audio = self._unexpected_transcription
            try:
                processor = TranscriptProcessor()
                processor.apply_diarization = lambda path, transcript, **kwargs: (
                    [{**item, "speaker": "SPEAKER_00"} for item in transcript],
                    {"SPEAKER_00": "Speaker 00"},
                )
                output_path = Path(directory) / "video_whisper.json"
                existing = [{"text": "Hello", "start": 0, "duration": 1}]
                (Path(directory) / "video_whisper_progress.txt").write_text(
                    "100", encoding="utf-8"
                )

                processor._process_whisper_transcript_sync(
                    "video",
                    "video.mp4",
                    str(output_path),
                    diarization=True,
                    existing_transcript=existing,
                )

                result = json.loads(output_path.read_text(encoding="utf-8"))
                self.assertEqual(result[0]["speaker"], "SPEAKER_00")
                self.assertFalse(
                    (Path(directory) / "video_whisper_progress.txt").exists()
                )
                self.assertFalse(
                    (Path(directory) / "video_whisper_phase.txt").exists()
                )
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir
                transcript_module.transcribe_audio = original_transcribe

    def test_failed_regeneration_restores_previous_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            original_transcribe = transcript_module.transcribe_audio
            transcript_module.TRANSCRIPTS_DIR = Path(directory)
            previous = [{"text": "Previously saved", "start": 0, "duration": 2}]
            output_path = Path(directory) / "video_whisper.json"
            backup_path = Path(directory) / "video_whisper_before_speakers.json"
            backup_path.write_text(json.dumps(previous), encoding="utf-8")

            def failed_transcription(*args, **kwargs):
                raise RuntimeError("decoder failed")

            transcript_module.transcribe_audio = failed_transcription
            try:
                TranscriptProcessor()._process_whisper_transcript_sync(
                    "video", "video.mp4", str(output_path)
                )

                self.assertEqual(
                    json.loads(output_path.read_text(encoding="utf-8")), previous
                )
                self.assertFalse(backup_path.exists())
                self.assertIn(
                    "decoder failed",
                    (Path(directory) / "video_whisper_error.txt").read_text(
                        encoding="utf-8"
                    ),
                )
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir
                transcript_module.transcribe_audio = original_transcribe

    def test_previous_transcript_is_versioned_before_forced_regeneration(self):
        class BackgroundTasks:
            def add_task(self, function, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as directory:
            original_video_dir = transcript_module.VIDEO_DIR
            original_transcript_dir = transcript_module.TRANSCRIPTS_DIR
            working_dir = Path(directory)
            transcript_module.VIDEO_DIR = working_dir
            transcript_module.TRANSCRIPTS_DIR = working_dir
            output_path = working_dir / "video_whisper.json"
            output_path.write_text(
                json.dumps([{"text": "Existing transcript", "start": 0}]),
                encoding="utf-8",
            )

            def verify_existing_transcript(video_id):
                self.assertEqual(
                    json.loads(output_path.read_text(encoding="utf-8"))[0]["text"],
                    "Existing transcript",
                )

            try:
                with mock.patch.object(
                    transcript_module.transcript_versions,
                    "sync_existing",
                    side_effect=verify_existing_transcript,
                ) as sync_existing:
                    response = asyncio.run(
                        TranscriptProcessor().generate_whisper_transcript(
                            "video", BackgroundTasks(), force=True
                        )
                    )

                self.assertTrue(response.content["success"])
                sync_existing.assert_called_once_with("video")
                self.assertTrue(
                    (working_dir / "video_whisper_before_speakers.json").exists()
                )
            finally:
                transcript_module.VIDEO_DIR = original_video_dir
                transcript_module.TRANSCRIPTS_DIR = original_transcript_dir

    @staticmethod
    def _unexpected_transcription(*args, **kwargs):
        raise AssertionError("ASR should not run when reusing a transcript")

    def test_selected_model_is_used_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            original_transcribe = transcript_module.transcribe_audio
            transcript_module.TRANSCRIPTS_DIR = Path(directory)
            calls = []

            def fake_transcribe(path, model_name="turbo", prompt=None, on_step=None):
                calls.append((model_name, prompt))
                on_step("Loading Whisper")
                yield "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n", 100

            transcript_module.transcribe_audio = fake_transcribe
            try:
                processor = TranscriptProcessor()
                output_path = Path(directory) / "video_whisper.json"
                processor._process_whisper_transcript_sync(
                    "video", "video.mp4", str(output_path), model="large-v3",
                    prompt="SAP lecture",
                )

                metadata = processor._public_whisper_metadata("video")
                self.assertEqual(calls, [("large-v3", "SAP lecture")])
                steps, elapsed = transcript_module.read_transcript_steps("video")
                messages = [step["message"] for step in steps]
                self.assertIn("Loading Whisper", messages)
                self.assertTrue(any(m.startswith("Transcription finished") for m in messages))
                self.assertTrue(any(m.startswith("Saving the transcript") for m in messages))
                self.assertIsNotNone(elapsed)
                self.assertEqual(metadata["model"], "large-v3")
                self.assertEqual(metadata["prompt"], "SAP lecture")
                self.assertEqual(metadata["device"], "cuda")
                self.assertFalse(metadata["diarization"])
                self.assertTrue(
                    processor._can_reuse_whisper_transcript("video", "large-v3", "SAP lecture")
                )
                self.assertFalse(
                    processor._can_reuse_whisper_transcript("video", "large-v3")
                )
                self.assertFalse(
                    processor._can_reuse_whisper_transcript("video", "turbo", "SAP lecture")
                )
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir
                transcript_module.transcribe_audio = original_transcribe

    def test_unsupported_model_is_rejected(self):
        response = asyncio.run(
            TranscriptProcessor().generate_whisper_transcript(
                "video", None, model="unknown"
            )
        )
        self.assertFalse(response.content["success"])

    def test_online_model_without_key_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            response = asyncio.run(
                TranscriptProcessor().generate_whisper_transcript(
                    "video", None, model="groq:whisper-large-v3-turbo"
                )
            )
        self.assertFalse(response.content["success"])
        self.assertIn("GROQ_API_KEY", response.content["error"])

    def test_online_model_is_used_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            transcript_module.TRANSCRIPTS_DIR = Path(directory)

            def fake_cloud(path, model, prompt=None, on_step=None):
                yield [{"text": "Hallo.", "start": 0.0, "duration": 1.5}], 100.0

            try:
                with mock.patch.object(transcript_module, "transcribe_audio_cloud", fake_cloud), \
                        mock.patch.object(transcript_module, "transcribe_audio",
                                          self._unexpected_transcription):
                    processor = TranscriptProcessor()
                    output_path = Path(directory) / "video_whisper.json"
                    processor._process_whisper_transcript_sync(
                        "video", "video.mp4", str(output_path),
                        model="gemini:gemini-2.5-flash",
                    )
                    metadata = processor._public_whisper_metadata("video")
                    transcript = processor._read_transcript("video")
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir

        self.assertEqual(transcript[0]["text"], "Hallo.")
        self.assertEqual(metadata["model"], "gemini:gemini-2.5-flash")
        self.assertEqual(metadata["device"], "cloud")
        self.assertIn("Gemini API", metadata["transcription_method"])


class TranscriptProcessorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_worker_crash_becomes_status_error(self):
        class FailedProcess:
            exitcode = -9

            def start(self):
                pass

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        class ProcessContext:
            def Process(self, **kwargs):
                return FailedProcess()

        with tempfile.TemporaryDirectory() as directory:
            original_dir = transcript_module.TRANSCRIPTS_DIR
            original_get_context = transcript_module.multiprocessing.get_context
            transcript_module.TRANSCRIPTS_DIR = Path(directory)
            transcript_module.multiprocessing.get_context = (
                lambda method: ProcessContext()
            )
            try:
                progress_path = Path(directory) / "video_whisper_progress.txt"
                progress_path.write_text("0", encoding="utf-8")
                previous = [{"text": "Previously saved", "start": 0, "duration": 2}]
                backup_path = Path(directory) / "video_whisper_before_speakers.json"
                backup_path.write_text(json.dumps(previous), encoding="utf-8")
                processor = TranscriptProcessor()

                await processor.process_whisper_transcript(
                    "video", "video.mp4", "transcript.json"
                )
                response = await processor.get_whisper_status("video")

                self.assertEqual(response.content["status"], "error")
                self.assertIn("exit code -9", response.content["error"])
                self.assertFalse(progress_path.exists())
                self.assertEqual(
                    json.loads(
                        (Path(directory) / "video_whisper.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    previous,
                )
                self.assertFalse(backup_path.exists())
            finally:
                transcript_module.TRANSCRIPTS_DIR = original_dir
                transcript_module.multiprocessing.get_context = original_get_context

    def test_forced_retry_can_terminate_active_worker(self):
        class RunningProcess:
            def __init__(self):
                self.alive = True
                self.terminated = False

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated = True
                self.alive = False

            def join(self, timeout=None):
                pass

        process = RunningProcess()
        transcript_module._ACTIVE_WHISPER_PROCESSES["video"] = process

        TranscriptProcessor._terminate_whisper_process("video")

        self.assertTrue(process.terminated)
        self.assertNotIn(
            "video", transcript_module._ACTIVE_WHISPER_PROCESSES
        )

    async def test_replaced_worker_cannot_overwrite_new_job_status(self):
        class ReplacedProcess:
            exitcode = -15

            def start(self):
                pass

            def join(self):
                TranscriptProcessor._terminate_whisper_process("video")
                transcript_module._ACTIVE_WHISPER_PROCESSES["video"] = replacement

            def is_alive(self):
                return False

        replacement = object()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)), \
                mock.patch.object(transcript_module.multiprocessing, "get_context") as context:
            context.return_value.Process.return_value = ReplacedProcess()
            progress = Path(directory) / "video_whisper_progress.txt"
            progress.write_text("42", encoding="utf-8")
            processor = TranscriptProcessor()
            processor._write_whisper_phase("video", "transcribing")
            try:
                await processor.process_whisper_transcript("video", "video.mp4", "output.json")
                self.assertEqual(progress.read_text(), "42")
                self.assertTrue((Path(directory) / "video_whisper_phase.txt").exists())
                self.assertFalse((Path(directory) / "video_whisper_error.txt").exists())
                self.assertIs(transcript_module._ACTIVE_WHISPER_PROCESSES["video"], replacement)
            finally:
                transcript_module._ACTIVE_WHISPER_PROCESSES.pop("video", None)

    async def test_forced_retry_starts_immediately_after_terminating_worker(self):
        class BackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, *args):
                self.tasks.append(args)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "VIDEO_DIR", Path(directory)), \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)):
            (Path(directory) / "video.mp4").write_bytes(b"video")
            progress = Path(directory) / "video_whisper_progress.txt"
            progress.write_text("42", encoding="utf-8")
            process = mock.Mock()
            process.is_alive.side_effect = [True, False]
            transcript_module._ACTIVE_WHISPER_PROCESSES["video"] = process
            tasks = BackgroundTasks()
            response = await TranscriptProcessor().generate_whisper_transcript(
                "video", tasks, force=True
            )
            process.terminate.assert_called_once()
            self.assertEqual(len(tasks.tasks), 1)
            self.assertEqual(response.content["phase"], "starting")
            self.assertEqual(progress.read_text(), "0")

    async def test_worker_start_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)), \
                mock.patch.object(transcript_module.multiprocessing, "get_context") as context:
            context.return_value.Process.return_value.start.side_effect = OSError(
                "Cannot start the worker"
            )
            await TranscriptProcessor().process_whisper_transcript(
                "video", "video.mp4", "output.json"
            )
            message = (Path(directory) / "video_whisper_error.txt").read_text()
            self.assertIn("Cannot start the worker", message)
            self.assertNotIn("video", transcript_module._ACTIVE_WHISPER_PROCESSES)

    def test_external_sigterm_has_specific_guidance(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(transcript_module, "TRANSCRIPTS_DIR", Path(directory)):
            TranscriptProcessor()._record_whisper_process_failure("video", -15)
            message = (Path(directory) / "video_whisper_error.txt").read_text()
            self.assertIn("SIGTERM", message)
            self.assertIn("restarted", message)


class HuggingFaceTokenTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: os.environ.pop(name, None)
            for name in ("HUGGINGFACE_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
        }

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def test_hf_token_environment_variable_is_detected(self):
        os.environ["HF_TOKEN"] = " hf_example "
        self.assertEqual(transcript_module.get_huggingface_token(), "hf_example")

    def test_saved_hub_login_is_used_when_environment_is_empty(self):
        hub_module = ModuleType("huggingface_hub")
        hub_module.get_token = lambda: "hf_saved"
        original = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = hub_module
        try:
            self.assertEqual(transcript_module.get_huggingface_token(), "hf_saved")
        finally:
            if original is None:
                sys.modules.pop("huggingface_hub", None)
            else:
                sys.modules["huggingface_hub"] = original


class PyannoteCompatibilityTests(unittest.TestCase):
    def test_uses_token_argument_for_pyannote_4(self):
        calls = []

        class Pipeline4:
            @staticmethod
            def from_pretrained(name, token=None):
                calls.append((name, token))
                return "pipeline"

        self.assertEqual(
            TranscriptProcessor._load_pyannote_pipeline(Pipeline4, "hf"), "pipeline"
        )
        self.assertEqual(calls, [("pyannote/speaker-diarization-3.1", "hf")])

    def test_falls_back_to_use_auth_token_for_pyannote_3(self):
        class Pipeline3:
            @staticmethod
            def from_pretrained(name, use_auth_token=None):
                return f"{name}:{use_auth_token}"

        self.assertEqual(
            TranscriptProcessor._load_pyannote_pipeline(Pipeline3, "hf"),
            "pyannote/speaker-diarization-3.1:hf",
        )


if __name__ == "__main__":
    unittest.main()
