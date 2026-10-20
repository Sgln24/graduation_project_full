import asyncio
import json
import sys
import unittest
import tempfile
import time
from pathlib import Path
from unittest import mock
from types import ModuleType
from types import SimpleNamespace


fastapi_module = ModuleType("fastapi")
fastapi_responses_module = ModuleType("fastapi.responses")
fastapi_responses_module.JSONResponse = object
fastapi_module.responses = fastapi_responses_module
sys.modules.setdefault("fastapi", fastapi_module)
sys.modules.setdefault("fastapi.responses", fastapi_responses_module)

dotenv_module = ModuleType("dotenv")
dotenv_module.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv_module)

google_module = ModuleType("google")
google_genai_module = ModuleType("google.genai")
google_genai_types_module = ModuleType("google.genai.types")
google_genai_types_module.GenerateContentConfig = lambda **kwargs: kwargs
google_genai_types_module.HttpOptions = lambda **kwargs: SimpleNamespace(**kwargs)
google_genai_types_module.HttpRetryOptions = lambda **kwargs: SimpleNamespace(**kwargs)
google_genai_module.types = google_genai_types_module
google_genai_module.Client = object
google_module.genai = google_genai_module
sys.modules.setdefault("google", google_module)
sys.modules.setdefault("google.genai", google_genai_module)
sys.modules.setdefault("google.genai.types", google_genai_types_module)

from processors.summary_processor import SummaryProcessor


class FakeGeminiModels:
    def __init__(self):
        self.payloads = []

    def generate_content(self, **kwargs):
        payload = json.loads(kwargs["contents"].split("\n", 1)[1])
        self.payloads.append(payload)
        lines = payload["lines"]
        if len(self.payloads) == 1:
            lines = lines[::2]
        translations = [
            {"id": item["id"], "text": f"translated: {item['text']}"}
            for item in lines
        ]
        return SimpleNamespace(text=json.dumps({"translations": translations}))


class CompleteFakeGeminiModels:
    def __init__(self):
        self.payloads = []

    def generate_content(self, **kwargs):
        payload = json.loads(kwargs["contents"].split("\n", 1)[1])
        self.payloads.append(payload)
        return SimpleNamespace(text=json.dumps({
            "translations": [
                {"id": item["id"], "text": f"translated: {item['text']}"}
                for item in payload["lines"]
            ]
        }))


class SummaryProcessorTranslationTests(unittest.TestCase):
    def test_unchanged_output_is_not_retained_as_resumable_translation(self):
        from translation_checkpoint import translate_with_resume
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._complete = lambda provider, model, prompt, *args: json.dumps({
            "translations": json.loads(prompt.split("\n", 1)[1])["lines"],
        })
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "original text unchanged"):
                asyncio.run(translate_with_resume(
                    processor, [{"start": 0, "text": "Hello"}], "de", "gemini-test",
                    checkpoint_dir=directory, allow_fallback=False,
                ))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_translation_resumes_real_engine_without_resubmitting_completed_lines(self):
        from translation_checkpoint import translate_with_resume
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        calls = []
        fail = True
        def complete(provider, model, prompt, *args):
            lines = json.loads(prompt.split("\n", 1)[1])["lines"]
            calls.append([line["id"] for line in lines])
            if fail and lines[0]["id"] == "segment-1":
                raise RuntimeError("429 daily quota")
            return json.dumps({"translations": [
                {"id": line["id"], "text": "translated " + line["text"]} for line in lines]})
        processor._complete = complete
        transcript = [{"start": index, "text": "x" * 4000} for index in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "1/3 lines are saved"):
                asyncio.run(translate_with_resume(
                    processor, transcript, "de", "gemini-test", checkpoint_dir=directory,
                    allow_fallback=False,
                ))
            self.assertEqual(calls, [["segment-0"], ["segment-1"]])
            calls.clear()
            fail = False
            result = asyncio.run(translate_with_resume(
                processor, transcript, "de", "gemini-test", checkpoint_dir=directory,
                allow_fallback=False,
            ))
            self.assertEqual(calls, [["segment-1"], ["segment-2"]])
            self.assertEqual(result["resumed_segments"], 1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_gemini_minute_limit_retries_same_request_with_requested_wait(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._complete = mock.Mock(side_effect=[
            RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerMinutePerProject-FreeTier "
                         "limit: 15 Please retry in 24.973435797s."),
            '{"translations":[{"id":"segment-0","text":"Hallo"}]}',
        ])
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            result = asyncio.run(processor.translate_transcript(
                [{"start": 0, "text": "Hello"}], "de", "gemini-test", allow_fallback=False,
            ))
        sleep.assert_awaited_once_with(25.973435797)
        self.assertEqual(processor._complete.call_args_list[0], processor._complete.call_args_list[1])
        self.assertEqual(result["transcript"][0]["text"], "Hallo")

    def test_gemini_minute_limit_is_bounded_and_falls_back_with_completed_fragments(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [
            ("gemini", "gemini-test"), ("openai", "gpt-test")]
        calls = []
        def complete(provider, model, prompt, *args):
            lines = json.loads(prompt.split("\n", 1)[1])["lines"]
            calls.append((provider, [item["id"] for item in lines]))
            if provider == "gemini" and lines[0]["id"] != "segment-0":
                raise RuntimeError("429 RESOURCE_EXHAUSTED requests per minute. Please retry in 25s")
            return json.dumps({"translations": [
                {"id": item["id"], "text": "translated " + item["text"]} for item in lines]})
        processor._complete = complete
        transcript = [{"start": index, "text": "x" * 4000} for index in range(3)]
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            result = asyncio.run(processor.translate_transcript(transcript, "de"))
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(calls, [
            ("gemini", ["segment-0"]), ("gemini", ["segment-1"]),
            ("gemini", ["segment-1"]), ("gemini", ["segment-1"]),
            ("openai", ["segment-1"]), ("openai", ["segment-2"]),
        ])
        self.assertEqual(len(result["transcript"]), 3)
        remaining_cooldown = processor.quota_exhausted_until[("gemini", "gemini-test")] - time.time()
        self.assertGreater(remaining_cooldown, 20)
        self.assertLessEqual(remaining_cooldown, 26)

    def test_gemini_retry_rejects_daily_zero_unknown_and_long_quotas(self):
        for message in (
            "429 RequestsPerDay Please retry in 25s",
            "429 RequestsPerMinute RequestsPerDay Please retry in 25s",
            "429 requests per minute daily quota Please retry in 25s",
            "429 requests per minute limit: 0 Please retry in 25s",
            "429 requests per minute 'quotaValue': '0' Please retry in 25s",
            "429 requests per minute Please retry in 100s",
            "429 requests per minute",
            "429 quota exhausted Please retry in 25s",
            "503 requests per minute Please retry in 25s",
        ):
            with self.subTest(message=message):
                self.assertIsNone(SummaryProcessor._gemini_translation_retry_delay(RuntimeError(message)))
        self.assertEqual(SummaryProcessor._gemini_translation_retry_delay(
            RuntimeError("429 TokensPerMinute 'retryDelay': '24s'")), 25)

    def test_translation_catalog_excludes_unavailable_groq_llama(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor.groq_client = object()
        options = {item["id"]: item["available"] for item in processor.translation_models()}
        self.assertNotIn("groq:llama-3.3-70b-versatile", options)
        self.assertTrue(options["groq:openai/gpt-oss-20b"])
        self.assertTrue(options["groq:openai/gpt-oss-120b"])

    def _gemini_processor(self, models):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.client = SimpleNamespace(models=models)
        processor.model = True
        processor.model_name = "gemini-test"
        processor.openai_client = None
        processor.openai_model_name = "gpt-test"
        processor._refresh_clients = lambda: None
        return processor

    def test_usage_counts_targeted_retries_and_marks_missing_usage(self):
        class MeteredModels(FakeGeminiModels):
            def generate_content(self, **kwargs):
                response = super().generate_content(**kwargs)
                response.usage_metadata = SimpleNamespace(
                    prompt_token_count=100, candidates_token_count=20,
                    thoughts_token_count=5, total_token_count=125,
                )
                return response
        transcript = [{"start": index, "duration": 1, "text": text}
                      for index, text in enumerate(["First", "Second"])]
        result = asyncio.run(self._gemini_processor(MeteredModels()).translate_transcript(
            transcript, "de", "gemini-test"))
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["usage"]["input_tokens"], 200)
        self.assertEqual(result["usage"]["output_tokens"], 50)
        self.assertEqual(result["usage"]["total_tokens"], 250)
        self.assertTrue(result["usage"]["complete"])
        missing = asyncio.run(self._gemini_processor(CompleteFakeGeminiModels()).translate_transcript(
            transcript, "de", "gemini-test"))
        self.assertFalse(missing["usage"]["complete"])
        self.assertEqual(missing["usage"]["reported_requests"], 0)

    def test_strict_model_never_falls_back(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._complete = mock.Mock(side_effect=RuntimeError("429 RESOURCE_EXHAUSTED"))
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "RESOURCE_EXHAUSTED"):
                asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    "gemini-test", allow_fallback=False))
        self.assertEqual({call.args[1] for call in processor._complete.call_args_list}, {"gemini-test"})
        with self.assertRaisesRegex(ValueError, "Choose a model"):
            asyncio.run(processor.translate_transcript([], "de", allow_fallback=False))

    def test_quota_overload_and_timeout_switch_once_without_retry_waits(self):
        for error in (RuntimeError("429 Rate limit: try again in 1m5s"),
                      RuntimeError("503 UNAVAILABLE high demand"),
                      asyncio.TimeoutError(),
                      RuntimeError("Request timed out")):
            with self.subTest(error=type(error).__name__, message=str(error)):
                processor = self._gemini_processor(CompleteFakeGeminiModels())
                processor._provider_chain = lambda *args, **kwargs: [
                    ("groq", "llama-3.3-70b-versatile"), ("gemini", "gemini-test")]
                def complete(provider, model, prompt, *args):
                    if provider == "groq":
                        raise error
                    payload = json.loads(prompt.split("\n", 1)[1])
                    return json.dumps({"translations": [
                        {"id": item["id"], "text": "Hallo"} for item in payload["lines"]]})
                processor._complete = mock.Mock(side_effect=complete)
                with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
                    result = asyncio.run(processor.translate_transcript(
                        [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                        "groq:llama-3.3-70b-versatile"))
                    sleep.assert_not_awaited()
                self.assertEqual(processor._complete.call_count, 2)
                self.assertEqual(result["usage"]["requests"], 2)
                self.assertEqual(result["provider"], "Gemini fallback")
                if isinstance(error, asyncio.TimeoutError):
                    self.assertIn("timed out after 30s", result["fallback_reason"])

    def test_explicit_groq_strict_mode_stops_at_first_rate_limit(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [
            ("groq", "llama-3.3-70b-versatile"), ("gemini", "gemini-test")]
        processor._complete = mock.Mock(side_effect=RuntimeError("429 Rate limit reached"))
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 1 attempt.*429"):
                asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    "groq:llama-3.3-70b-versatile", allow_fallback=False))
            sleep.assert_not_awaited()
        processor._complete.assert_called_once()
        self.assertEqual(processor._complete.call_args.args[0], "groq")

    def test_short_groq_token_wait_retries_missing_lines_on_same_model(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [
            ("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-test")]
        processor._complete = mock.Mock(side_effect=[
            '{"translations":[{"id":"segment-0","text":"Hallo"}]}',
            RuntimeError("429 tokens per minute (TPM): Please try again in 5.3325s. rate_limit_exceeded"),
            '{"translations":[{"id":"segment-1","text":"Welt"}]}',
        ])
        progress = []
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            result = asyncio.run(processor.translate_transcript([
                {"start": 0, "duration": 1, "text": "Hello"},
                {"start": 1, "duration": 1, "text": "World"}], "de",
                "groq:openai/gpt-oss-20b", allow_fallback=False, on_progress=progress.append))
            sleep.assert_awaited_once_with(6.3325)
        calls = processor._complete.call_args_list
        self.assertTrue(all(call.args[:2] == ("groq", "openai/gpt-oss-20b") for call in calls))
        self.assertEqual(calls[1].args[2], calls[2].args[2], "Retry the exact missing-line request")
        self.assertEqual([item["text"] for item in result["transcript"]], ["Hallo", "Welt"])
        self.assertEqual(result["usage"]["requests"], 3)
        self.assertTrue(any("once in 6.3s" in update["message"] for update in progress))

    def test_short_groq_rate_limit_is_retried_only_once(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [("groq", "openai/gpt-oss-20b")]
        processor._complete = mock.Mock(side_effect=RuntimeError(
            "429 tokens per minute: Please try again in 5.3325s. rate_limit_exceeded"))
        with mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 2 attempt.*429"):
                asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    "groq:openai/gpt-oss-20b", allow_fallback=False))
            sleep.assert_awaited_once_with(6.3325)
        self.assertEqual(processor._complete.call_count, 2)

    def test_invalid_json_splits_requests_and_preserves_caption_fields(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [("groq", "openai/gpt-oss-20b")]
        calls = []
        def complete(provider, model, prompt, *args):
            lines = json.loads(prompt.split("\n", 1)[1])["lines"]
            calls.append([item["id"] for item in lines])
            if len(lines) > 2:
                return '{"translations":[{"id":'
            return "```json\n" + json.dumps({"translations": [
                {"id": item["id"], "text": "Translated " + item["text"]} for item in lines]}) + "\n```"
        processor._complete = complete
        transcript = [{"start": index, "duration": 1, "text": f"Line {index}", "speaker": "A"}
                      for index in range(8)]
        progress = []
        result = asyncio.run(processor.translate_transcript(
            transcript, "de", "groq:openai/gpt-oss-20b",
            allow_fallback=False, on_progress=progress.append))
        self.assertEqual([len(call) for call in calls], [8, 4, 2, 2, 4, 2, 2])
        self.assertEqual(result["usage"]["requests"], 7)
        self.assertEqual(len(result["transcript"]), 8)
        for original, translated in zip(transcript, result["transcript"]):
            self.assertEqual(translated, {**original, "text": "Translated " + original["text"]})
        self.assertTrue(any("two smaller requests" in update["message"] for update in progress))

    def test_persistent_invalid_json_recovery_is_bounded(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._complete = mock.Mock(return_value='{"translations":[')
        with self.assertRaisesRegex(ValueError, "invalid JSON after bounded recovery"):
            asyncio.run(processor.translate_transcript(
                [{"start": index, "duration": 1, "text": f"Line {index}"} for index in range(8)],
                "de", "gemini-test", allow_fallback=False))
        self.assertEqual(processor._complete.call_count, 3)

    def test_invalid_response_shape_recovers_in_smaller_requests(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._complete = mock.Mock(side_effect=[
            '{"translations":null}',
            '{"translations":[{"id":"segment-0","text":"Hallo"}]}',
            '{"translations":[{"id":"segment-1","text":"Welt"}]}',
        ])
        result = asyncio.run(processor.translate_transcript([
            {"start": 0, "duration": 1, "text": "Hello"},
            {"start": 1, "duration": 1, "text": "World"}], "de", "gemini-test",
            allow_fallback=False))
        self.assertEqual([item["text"] for item in result["transcript"]], ["Hallo", "Welt"])
        self.assertEqual(result["usage"]["requests"], 3)

    def test_successful_split_is_kept_when_next_split_hits_quota(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [
            ("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-test")]
        calls = []
        def complete(provider, model, prompt, *args):
            lines = json.loads(prompt.split("\n", 1)[1])["lines"]
            calls.append((provider, [item["id"] for item in lines]))
            if provider == "groq":
                if len(lines) > 1:
                    return "malformed"
                if lines[0]["id"] == "segment-1":
                    raise RuntimeError("429 rate limit")
            return json.dumps({"translations": [
                {"id": item["id"], "text": "Translated " + item["text"]} for item in lines]})
        processor._complete = complete
        result = asyncio.run(processor.translate_transcript([
            {"start": 0, "duration": 1, "text": "Hello"},
            {"start": 1, "duration": 1, "text": "World"}], "de", "groq:openai/gpt-oss-20b"))
        self.assertEqual(calls[-1], ("gemini", ["segment-1"]))
        self.assertEqual(len(result["transcript"]), 2)

    def test_short_wait_policy_rejects_long_unknown_and_daily_quota_errors(self):
        for message in (
            "429 tokens per minute: try again in 1m5s",
            "429 tokens per minute: try again in 14.1s",
            "429 tokens per minute: try again in 65s",
            "429 tokens per minute: no retry time provided",
            "429 daily quota: try again in 5s",
            "503 UNAVAILABLE: try again in 5s",
        ):
            with self.subTest(message=message):
                self.assertIsNone(SummaryProcessor._short_translation_retry_delay(RuntimeError(message)))
        self.assertEqual(SummaryProcessor._short_translation_retry_delay(
            RuntimeError("429 rate_limit_exceeded: try again in 14s")), 15)

    def test_partial_batch_success_survives_failed_missing_line_retry(self):
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor._provider_chain = lambda *args, **kwargs: [
            ("groq", "llama-3.3-70b-versatile"), ("gemini", "gemini-test")]
        calls = []
        def complete(provider, model, prompt, *args):
            payload = json.loads(prompt.split("\n", 1)[1])
            lines = payload["lines"]
            calls.append((provider, [item["id"] for item in lines]))
            if provider == "groq":
                if len(calls) == 1:
                    return '{"translations":[{"id":"segment-0","text":"Hallo"}]}'
                raise RuntimeError("429 Rate limit")
            return json.dumps({"translations": [{"id": item["id"], "text": "Welt"} for item in lines]})
        processor._complete = complete
        result = asyncio.run(processor.translate_transcript([
            {"start": 0, "duration": 1, "text": "Hello"},
            {"start": 1, "duration": 1, "text": "World"}], "de",
            "groq:llama-3.3-70b-versatile"))
        self.assertEqual(calls, [
            ("groq", ["segment-0", "segment-1"]),
            ("groq", ["segment-1"]), ("gemini", ["segment-1"])])
        self.assertEqual([item["text"] for item in result["transcript"]], ["Hallo", "Welt"])
        self.assertEqual(result["usage"]["requests"], 3)

    def test_gemini_translation_bounds_sdk_requests_without_changing_chapters(self):
        models = CompleteFakeGeminiModels()
        models.generate_content = mock.Mock(wraps=models.generate_content)
        processor = self._gemini_processor(models)
        asyncio.run(processor.translate_transcript(
            [{"start": 0, "duration": 1, "text": "Hello"}], "de", "gemini-test"))
        config = models.generate_content.call_args.kwargs["config"]
        options = config["http_options"] if isinstance(config, dict) else config.http_options
        self.assertEqual(options.timeout, 30000)
        self.assertEqual(options.retry_options.attempts, 1)
        processor._complete("gemini", "gemini-test",
                            'Translate\n{"lines":[{"id":"test","text":"Hello"}]}', 0.2)
        config = models.generate_content.call_args.kwargs["config"]
        self.assertFalse(config.get("http_options") if isinstance(config, dict) else config.http_options)

    def test_groq_default_and_usage_are_retained_across_provider_fallback(self):
        class Completions:
            def create(self, **kwargs):
                payload = json.loads(kwargs["messages"][0]["content"].split("\n", 1)[1])
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                        "translations": [{"id": item["id"], "text": "Translated " + item["text"]}
                                         for item in payload["lines"]],
                    })))],
                    usage=SimpleNamespace(prompt_tokens=80, completion_tokens=20, total_tokens=100),
                )
        processor = self._gemini_processor(CompleteFakeGeminiModels())
        processor.groq_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        processor.groq_client.with_options = mock.Mock(return_value=processor.groq_client)
        processor.groq_model_name = "openai/gpt-oss-120b"
        transcript = [{"start": 0, "duration": 1, "text": "Hello"}]
        result = asyncio.run(processor.translate_transcript(transcript, "de"))
        processor.groq_client.with_options.assert_called_once_with(timeout=30, max_retries=0)
        self.assertEqual(result["provider"], "Groq")
        self.assertEqual(result["usage"]["total_tokens"], 100)
        self.assertTrue(result["usage"]["complete"])
        processor.groq_client.chat.completions.create = mock.Mock(side_effect=RuntimeError("404 model_not_found"))
        fallback = asyncio.run(processor.translate_transcript(transcript, "de"))
        self.assertEqual(fallback["provider"], "Gemini fallback")
        self.assertEqual(fallback["usage"]["requests"], 2)
        self.assertEqual(fallback["usage"]["models"], ["Gemini gemini-test", "Groq openai/gpt-oss-120b"])
        self.assertFalse(fallback["usage"]["complete"])

    def test_persistent_omissions_retry_only_unresolved_plain_fragment(self):
        class MissingFragment(CompleteFakeGeminiModels):
            def generate_content(self, **kwargs):
                response = super().generate_content(**kwargs)
                if len(self.payloads) < 3:
                    result = json.loads(response.text)
                    result["translations"] = [item for item in result["translations"] if item["id"] != "segment-0"]
                    response.text = json.dumps(result)
                return response
        models = MissingFragment()
        progress = []
        result = asyncio.run(self._gemini_processor(models).translate_transcript(
            [{"start": 0, "duration": 3, "text": "Hello"},
             {"start": 3, "duration": 1, "text": "Other"}], "de", "gemini-test",
            on_progress=progress.append))
        self.assertEqual(result["transcript"][0]["text"], "translated: Hello")
        self.assertEqual(result["transcript"][1]["text"], "translated: Other")
        self.assertEqual(len(models.payloads), 3)
        self.assertEqual([payload["lines"][0]["text"] for payload in models.payloads[2:]],
                         ["Hello"])
        self.assertTrue(all(len(payload["lines"]) == 1 for payload in models.payloads[2:]))
        self.assertEqual(result["usage"]["requests"], 3)
        self.assertTrue(any("separately" in update["message"] for update in progress))

    def test_individual_fragment_omissions_remain_explicit_errors(self):
        class EmptyModels:
            def generate_content(self, **kwargs):
                return SimpleNamespace(text='{"translations":[]}')
        with self.assertRaisesRegex(RuntimeError, "even when requested individually"):
            asyncio.run(self._gemini_processor(EmptyModels()).translate_transcript(
                [{"start": 0, "duration": 1, "text": "Hello"}],
                "de", "gemini-test"))

    def test_translation_retries_only_missing_ids_and_preserves_fields(self):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        models = FakeGeminiModels()
        processor.client = SimpleNamespace(models=models)
        processor.model = True
        processor.model_name = "gemini-test"
        processor.openai_client = None
        processor.openai_model_name = "gpt-test"
        processor._refresh_gemini_client = lambda: None
        processor._refresh_openai_client = lambda: None
        transcript = [
            {"start": 0, "duration": 2.2, "text": "First", "speaker": "SPEAKER_00"},
            {"start": 2.2, "duration": 3.1, "text": "Second", "speaker": "SPEAKER_01"},
            {"start": 5.3, "duration": 1.5, "text": "Third", "speaker": "SPEAKER_00"},
        ]

        result = asyncio.run(
            processor.translate_transcript(transcript, "de", "gemini-test")
        )

        self.assertEqual(
            [item["text"] for item in result["transcript"]],
            ["translated: First", "translated: Second", "translated: Third"],
        )
        self.assertEqual(
            [item["speaker"] for item in result["transcript"]],
            ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"],
        )
        self.assertEqual(
            [item["id"] for item in models.payloads[0]["lines"]],
            ["segment-0", "segment-1", "segment-2"],
        )
        self.assertEqual(
            [item["id"] for item in models.payloads[1]["lines"]],
            ["segment-1"],
        )
        self.assertEqual(models.payloads[0]["lines"][0]["seconds"], 2.2)

    def test_translation_batches_include_neighboring_context_only(self):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        models = CompleteFakeGeminiModels()
        processor.client = SimpleNamespace(models=models)
        processor.model = True
        processor.model_name = "gemini-test"
        processor.openai_client = None
        processor.openai_model_name = "gpt-test"
        processor._refresh_gemini_client = lambda: None
        processor._refresh_openai_client = lambda: None
        processor._translation_batches = lambda items: [items[:2], items[2:]]
        transcript = [
            {"start": 0, "duration": 1, "text": "First"},
            {"start": 1, "duration": 1, "text": "Second"},
            {"start": 2, "duration": 1, "text": "Third"},
        ]

        asyncio.run(
            processor.translate_transcript(transcript, "de", "gemini-test")
        )

        payloads = sorted(
            models.payloads,
            key=lambda payload: payload["lines"][0]["id"],
        )
        self.assertEqual(payloads[0]["context_before"], [])
        self.assertEqual(payloads[0]["context_after"], ["Third"])
        self.assertEqual(payloads[1]["context_before"], ["First", "Second"])
        self.assertEqual(payloads[1]["context_after"], [])

    def test_gemini_quota_error_falls_back_to_openai(self):
        class ExhaustedGemini:
            def generate_content(self, **kwargs):
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

        class FakeOpenAICompletions:
            def create(self, **kwargs):
                payload = json.loads(kwargs["messages"][0]["content"].split("\n", 1)[1])
                content = json.dumps({"translations": [
                    {"id": item["id"], "text": f"übersetzt: {item['text']}"}
                    for item in payload["lines"]
                ]})
                return SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=content))
                ])

        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.client = SimpleNamespace(models=ExhaustedGemini())
        processor.model = True
        processor.model_name = "gemini-test"
        processor.openai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeOpenAICompletions())
        )
        processor.openai_client.with_options = mock.Mock(return_value=processor.openai_client)
        processor.openai_model_name = "gpt-test"
        processor._refresh_gemini_client = lambda: None
        processor._refresh_openai_client = lambda: None
        original_sleep = asyncio.sleep

        async def no_sleep(seconds):
            return None

        asyncio.sleep = no_sleep
        try:
            result = asyncio.run(processor.translate_transcript(
                [{"start": 0, "duration": 1, "text": "Hello"}], "de"
            ))
        finally:
            asyncio.sleep = original_sleep

        self.assertEqual(result["provider"], "OpenAI fallback")
        self.assertEqual(result["model"], "gpt-test")
        self.assertEqual(result["transcript"][0]["text"], "übersetzt: Hello")

    def test_translation_batches_respect_item_and_character_limits(self):
        items = [
            {"id": "segment-0", "position": 0, "text": "abcd"},
            {"id": "segment-1", "position": 1, "text": "efgh"},
            {"id": "segment-2", "position": 2, "text": "ij"},
        ]

        batches = SummaryProcessor._translation_batches(
            items, maximum_items=2, maximum_characters=6
        )

        self.assertEqual(
            [[item["id"] for item in batch] for batch in batches],
            [["segment-0"], ["segment-1", "segment-2"]],
        )

    def test_fallback_keeps_finished_batches_and_reports_progress(self):
        calls = []

        def complete(provider, model, prompt, temperature, json_output=True, max_tokens=None, fast=False):
            payload = json.loads(prompt.split("\n", 1)[1])
            ids = [item["id"] for item in payload["lines"]]
            calls.append((provider, ids, fast))
            if provider == "gemini" and "segment-2" in ids:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
            return json.dumps({"translations": [
                {"id": item["id"], "text": f"{provider}: {item['text']}"}
                for item in payload["lines"]
            ]})

        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.model = True
        processor.model_name = "gemini-test"
        processor.openai_client = object()
        processor.openai_model_name = "gpt-test"
        processor._refresh_clients = lambda: None
        processor._complete = complete
        processor._translation_batches = lambda items, *args: [items[i:i + 2] for i in range(0, len(items), 2)]
        progress = []
        original_sleep = asyncio.sleep

        async def no_sleep(seconds):
            return None

        asyncio.sleep = no_sleep
        try:
            with mock.patch("processors.summary_processor.GEMINI_FALLBACK_MODELS", ()):
                result = asyncio.run(processor.translate_transcript(
                    [{"start": i, "duration": 1, "text": f"line {i}"} for i in range(4)],
                    "de", on_progress=progress.append,
                ))
        finally:
            asyncio.sleep = original_sleep

        self.assertEqual(
            [item["text"] for item in result["transcript"]],
            ["gemini: line 0", "gemini: line 1", "openai: line 2", "openai: line 3"],
        )
        self.assertEqual(result["provider"], "OpenAI fallback")
        openai_ids = [ids for provider, ids, _ in calls if provider == "openai"]
        self.assertEqual(openai_ids, [["segment-2", "segment-3"]], "Finished Gemini lines are not translated again")
        self.assertTrue(all(fast for _, _, fast in calls), "Translation asks for minimal reasoning")
        self.assertTrue(any("continuing the remaining 2 lines" in p["message"] for p in progress))
        self.assertEqual(progress[-1]["completed_segments"], 4)
        self.assertEqual(progress[-1]["total_segments"], 4)

    def test_fast_gemini_requests_disable_thinking_and_recover_if_rejected(self):
        configs = []

        class Models:
            def generate_content(self, **kwargs):
                configs.append(kwargs["config"])
                if len(configs) == 1:
                    raise RuntimeError("400 INVALID_ARGUMENT: thinking_level is not supported")
                return SimpleNamespace(text="{}")

        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.client = SimpleNamespace(models=Models())
        thinking = object()
        with mock.patch.object(SummaryProcessor, "_fast_thinking_config", staticmethod(lambda model: thinking)), \
                mock.patch("processors.summary_processor.types.GenerateContentConfig", lambda **kwargs: kwargs, create=True):
            self.assertEqual(processor._complete("gemini", "gemini-x", "p", 0.2, True, 100, True), "{}")
            processor._complete("gemini", "gemini-x", "p", 0.2, True, 100, True)

        self.assertIs(configs[0]["thinking_config"], thinking)
        self.assertNotIn("thinking_config", configs[1])
        self.assertNotIn("thinking_config", configs[2], "A model that rejects the setting is remembered")


class SummaryProviderFallbackTests(unittest.TestCase):
    def test_enhancement_keeps_every_takeaway_and_manual_section_time(self):
        chapters = [{
            "timestamp": "00:00", "title": "Workflow", "summary": "Overview",
            "sections": [
                {"timestamp": "00:05.250", "title": "Step 1", "point": "First detail.", "timestamp_edited": True},
                {"timestamp": "00:15", "title": "Step 2", "point": "Second detail."},
            ],
        }]
        processor = self._processor(None, None, None)
        processor._complete = mock.Mock(return_value=json.dumps({"chapters": [{
            "title": "Complete workflow", "summary": "Overview",
            "groups": [{"title": "Workflow", "indices": [0, 1]}],
        }]}))
        result = asyncio.run(processor.enhance_chapters(chapters, "groq", "test"))
        self.assertEqual(result[0]["timestamp"], "00:00")
        self.assertEqual(result[0]["sections"][0]["timestamp"], "00:05.250")
        self.assertTrue(result[0]["sections"][0]["timestamp_edited"])
        self.assertEqual(result[0]["sections"][0]["point"], "First detail. Second detail.")
        self.assertEqual(chapters[0]["sections"][1]["title"], "Step 2")

    def test_enhancement_rejects_omitted_duplicate_reordered_and_boolean_indices(self):
        units = [{"section": {"timestamp": "00:00"}, "chapter": {"timestamp": "00:00"},
                  "point": text} for text in ["One", "Two"]]
        for indices in ([0], [0, 0], [1, 0], [False, 1]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                SummaryProcessor._apply_outline_groups(units, {"chapters": [{
                    "title": "Topic", "summary": "", "groups": [{"title": "Section", "indices": indices}],
                }]})

    def test_two_pass_generation_reviews_outline_and_failure_does_not_save_draft(self):
        processor = self._processor(None, None, None)
        processor._chunked_chapters = mock.AsyncMock(return_value='[{"timestamp":"00:00","title":"Draft"}]')
        processor.enhance_chapters = mock.AsyncMock(return_value=[{"timestamp": "00:00", "title": "Reviewed"}])
        processor._save_chapters = mock.Mock()
        transcript = [{"start": 0, "duration": 10, "text": "A topic."}]
        result = asyncio.run(processor.generate_summary(transcript, "video", passes="two"))
        self.assertTrue(result.content["success"])
        self.assertEqual(result.content["chapters"][0]["title"], "Reviewed")
        processor.enhance_chapters.assert_awaited_once()
        processor._save_chapters.reset_mock()
        processor.enhance_chapters.side_effect = RuntimeError("Invalid review")
        result = asyncio.run(processor.generate_summary(transcript, "video", passes="two"))
        self.assertFalse(result.content["success"])
        processor._save_chapters.assert_not_called()

    def test_generation_language_detects_clear_english_despite_foreign_proper_names(self):
        text = "We use the German product. This is the report and you can edit the data with this tool."
        self.assertEqual(SummaryProcessor._detect_transcript_language([{"text": text}]), "en")

    def test_summary_language_defaults_to_transcript_and_supports_selection(self):
        self.assertIn("same language", SummaryProcessor._summary_language_instruction("transcript"))
        self.assertIn("German", SummaryProcessor._summary_language_instruction("de"))
        with self.assertRaisesRegex(ValueError, "Unsupported summary language"):
            SummaryProcessor._summary_language_instruction("invalid")

    def setUp(self):
        class FakeJSONResponse:
            def __init__(self, content):
                self.content = content

        from processors import summary_processor as summary_module
        self._module = summary_module
        self._original_response = summary_module.JSONResponse
        summary_module.JSONResponse = FakeJSONResponse

    def tearDown(self):
        self._module.JSONResponse = self._original_response

    def _processor(self, gemini_models, groq_completions, openai_completions):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.client = SimpleNamespace(models=gemini_models)
        processor.model = True
        processor.model_name = "gemini-3.6-flash"
        processor.groq_client = SimpleNamespace(chat=SimpleNamespace(completions=groq_completions))
        processor.groq_model_name = "groq-test"
        processor.openai_client = SimpleNamespace(chat=SimpleNamespace(completions=openai_completions))
        processor.openai_model_name = "gpt-test"
        processor._refresh_gemini_client = lambda: None
        processor._refresh_openai_client = lambda: None
        processor._refresh_groq_client = lambda: None
        processor._save_chapters = lambda *args: None
        return processor

    def test_summary_default_prefers_groq_and_preserves_explicit_choice(self):
        processor = self._processor(None, None, None)
        self.assertEqual(
            processor._provider_chain(prefer_groq=True)[0], ("groq", "groq-test")
        )
        self.assertEqual(
            processor._provider_chain("gemini-3.8-flash", prefer_groq=True)[0],
            ("gemini", "gemini-3.8-flash"),
        )
        processor.groq_client = None
        self.assertEqual(
            processor._provider_chain(prefer_groq=True)[0],
            ("gemini", "gemini-3.6-flash"),
        )

    def test_selected_language_is_sent_to_summary_provider(self):
        for language, expected in (("transcript", "same language"), ("de", "German")):
            with self.subTest(language=language):
                processor = self._processor(None, None, None)
                prompts = []

                def complete(provider, model, prompt, *args):
                    prompts.append(prompt)
                    return '[{"timestamp": "00:00", "title": "Introduction"}]'

                processor._complete = complete
                response = asyncio.run(processor.generate_summary(
                    [{"start": 0, "text": "Welcome"}], language=language
                ))
                self.assertTrue(response.content["success"], response.content)
                self.assertIn(expected, prompts[0])

    def test_unicode_summary_prompt_uses_utf8_on_windows_and_colab(self):
        processor = self._processor(None, None, None)
        processor._complete = lambda *args: '[{"timestamp": "00:00", "title": "Introduction"}]'
        real_open = open
        with tempfile.TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "debug.txt"

            def windows_open(filename, mode="r", **kwargs):
                if filename == "debug.txt":
                    self.assertEqual(kwargs.get("encoding"), "utf-8")
                    return real_open(prompt_path, mode, **kwargs)
                return real_open(filename, mode, **kwargs)

            text = "Arabic: \u0645\u0631\u062d\u0628\u0627; Chinese: \u4f60\u597d; emoji: \U0001f600"
            with mock.patch("builtins.open", side_effect=windows_open):
                response = asyncio.run(processor.generate_summary(
                    [{"start": 0, "text": text}]
                ))
            self.assertTrue(response.content["success"], response.content)
            self.assertIn(text, prompt_path.read_text(encoding="utf-8"))

    def test_chain_tries_other_gemini_models_then_groq_before_paid_openai(self):
        processor = self._processor(None, None, None)
        self.assertEqual(processor._provider_chain("gemini-2.5-flash"), [
            ("gemini", "gemini-2.5-flash"),
            ("gemini", "gemini-3.6-flash"),
            ("gemini", "gemini-3.8-flash"),
            ("gemini", "gemini-3.5-flash-lite"),
            ("groq", "groq-test"),
            ("openai", "gpt-test"),
        ])

    def test_retired_gemini_model_is_skipped_and_remembered(self):
        class RetiredModel:
            def __init__(self):
                self.models = []

            def generate_content(self, model, **kwargs):
                self.models.append(model)
                if model == "gemini-2.5-flash":
                    raise RuntimeError(
                        "404 NOT_FOUND. This model models/gemini-2.5-flash is no longer "
                        "available to new users."
                    )
                return SimpleNamespace(text='[{"timestamp": "00:00", "title": "Intro"}]')

        gemini = RetiredModel()
        processor = self._processor(gemini, None, None)
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 5, "text": "Welcome"}], "video", "gemini-2.5-flash"
        ))

        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(gemini.models, ["gemini-2.5-flash", "gemini-3.6-flash"])
        self.assertNotIn(("gemini", "gemini-2.5-flash"), processor._provider_chain())

    def test_retry_delay_uses_the_wait_the_api_requests(self):
        self.assertEqual(
            SummaryProcessor._retry_delay(RuntimeError("Please try again in 7.5s.")), 8.5
        )
        self.assertEqual(
            SummaryProcessor._retry_delay(RuntimeError("Please try again in 1m2.5s.")), 63.5
        )
        self.assertEqual(
            SummaryProcessor._retry_delay(RuntimeError("try again in 500ms")), 1.5
        )

    def test_long_transcript_is_split_into_parts_for_groq(self):
        from processors import summary_processor as module
        processor = self._processor(None, None, None)
        prompts = []

        def complete(provider, model, prompt, *args):
            self.assertEqual(provider, "groq")
            prompts.append(prompt)
            if len(prompt) > 3000 and len(prompts) == 1:
                raise RuntimeError("Error code: 413 Request too large for model on tokens per minute (TPM)")
            if len(prompts) == 3:
                raise RuntimeError("Error code: 429 rate_limit_exceeded. Please try again in 0.01s.")
            stamp = prompt.split("from ", 1)[1][:8]
            return '{"chapters": [{"timestamp": "%s", "title": "Part at %s"}]}' % (stamp, stamp)

        processor._complete = complete
        transcript = [{"start": index * 10, "text": "word " * 40} for index in range(60)]
        sleeps = []

        async def no_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(module, "GROQ_CHAPTER_CHUNK_CHARS", 6000), \
                mock.patch.object(module.asyncio, "sleep", no_sleep):
            response = asyncio.run(processor.generate_summary(transcript, "video"))

        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(response.content["model"], "groq-test")
        starts = [chapter["timestamp"] for chapter in response.content["chapters"]]
        self.assertEqual(starts[0], "00:00")
        self.assertEqual(len(starts), len(set(starts)))
        self.assertGreaterEqual(len(starts), 3)
        self.assertEqual(len(sleeps), 1)

    def test_chapters_preserve_short_distinct_topics_and_keep_options(self):
        processor = self._processor(None, None, None)
        prompts = []

        def complete(provider, model, prompt, *args):
            prompts.append(prompt)
            return json.dumps({"chapters": [
                {"timestamp": "00:00:00", "title": "Authentication", "summary": "Credentials identify users."},
                {"timestamp": "00:00:30", "title": "Gradient descent", "summary": "How models learn.",
                 "sections": [
                     {"timestamp": "00:00:40", "title": "Loss functions", "point": "Loss measures error."},
                     {"timestamp": "00:03:00", "title": "Step size", "point": "Small steps are stable."},
                 ]},
                {"timestamp": "00:06:00", "title": "Rate limits", "summary": "Quotas limit requests."},
                {"timestamp": "00:06:20", "title": "Evaluation", "sections": [
                    {"timestamp": "00:06:20", "title": "Test data", "point": "Test data stays unseen."},
                ]},
            ]})

        processor._complete = complete
        transcript = [{"start": index * 10, "duration": 10, "text": "word"} for index in range(300)]
        response = asyncio.run(processor.generate_summary(
            transcript, None, detail="balanced", instructions="Focus on   exam topics",
        ))
        self.assertTrue(response.content["success"], response.content)
        chapters = response.content["chapters"]
        self.assertEqual([chapter["title"] for chapter in chapters],
                         ["Authentication", "Gradient descent", "Rate limits", "Evaluation"])
        self.assertEqual(chapters[0]["timestamp"], "00:00")
        self.assertEqual(
            [section["title"] for section in chapters[1]["sections"]],
            ["Loss functions", "Step size"],
        )
        self.assertEqual(chapters[2]["timestamp"], "06:00")
        self.assertIn("Focus on exam topics", prompts[0])
        self.assertIn("sections", prompts[0])
        self.assertIn("One section per genuinely separate topic", prompts[0])
        self.assertNotIn("True sections", prompts[0])

    def test_topic_guidance_does_not_add_a_second_ai_pass_or_fixed_limit(self):
        processor = self._processor(None, None, None)
        sections = [
            {"timestamp": SummaryProcessor._stamp(index * 60), "title": f"Topic {index}",
             "point": f"Takeaway {index}."}
            for index in range(33)
        ]
        answers = iter([json.dumps({"chapters": [
            {"timestamp": "00:00:00", "title": "Main topic", "sections": sections},
        ]})])
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or next(answers)
        transcript = [{"start": index * 30, "duration": 30, "text": "word"} for index in range(100)]
        response = asyncio.run(processor.generate_summary(transcript, None, detail="balanced"))
        self.assertTrue(response.content["success"], response.content)
        result = response.content["chapters"][0]["sections"]
        self.assertEqual(len(prompts), 1, "No additional review call after generation")
        self.assertEqual(len(result), 33, "Distinct topics must not be arbitrarily capped")
        self.assertIn("actual topic changes, not a fixed quota", prompts[0])
        self.assertEqual(
            " ".join(section["point"] for section in result),
            " ".join(f"Takeaway {index}." for index in range(33)),
        )
        self.assertEqual([section["timestamp"] for section in result],
                         [f"{index:02d}:00" for index in range(33)])

    def test_transcript_continuations_do_not_trigger_an_extra_ai_review(self):
        processor = self._processor(None, None, None)
        sections = [
            {"timestamp": SummaryProcessor._stamp(index * 20), "title": f"Step {index}", "point": f"Fact {index}."}
            for index in range(12)
        ]
        responses = iter([
            json.dumps({"chapters": [{"timestamp": "00:00:00", "title": "Workflow", "sections": sections[:6]}]}),
            json.dumps({"continued_sections": sections[6:], "chapters": []}),
        ])
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or next(responses)
        transcript = [{"start": index * 10, "duration": 10, "text": "word " * 20} for index in range(24)]
        with mock.patch("processors.summary_processor.GROQ_CHAPTER_CHUNK_CHARS", 3500):
            response = asyncio.run(processor.generate_summary(transcript, None))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(len(prompts), 2)
        result = response.content["chapters"][0]["sections"]
        self.assertEqual(len(result), 12)
        self.assertEqual([section["point"] for section in result],
                         [section["point"] for section in sections])
        self.assertIn("group continued explanations", prompts[1])

    def test_same_subtopic_across_requests_extends_existing_section_in_one_pass(self):
        processor = self._processor(None, None, None)
        answers = iter([
            {"chapters": [{"timestamp": "00:00:00", "title": "Workflow", "sections": [
                {"timestamp": "00:00:00", "title": "Report editing", "point": "Choose the report."},
            ]}]},
            {"continued_point": "Adjust its fields and check the result.", "continued_sections": [],
             "chapters": []},
        ])
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or json.dumps(next(answers))
        transcript = [{"start": index * 10, "duration": 10, "text": "word " * 20} for index in range(24)]
        with mock.patch("processors.summary_processor.GROQ_CHAPTER_CHUNK_CHARS", 3500):
            response = asyncio.run(processor.generate_summary(transcript, None))
        self.assertTrue(response.content["success"], response.content)
        sections = response.content["chapters"][0]["sections"]
        self.assertEqual(len(sections), 1, "A request boundary must not create a new subsection")
        self.assertEqual(sections[0]["timestamp"], "00:00")
        self.assertEqual(sections[0]["point"], "Choose the report. Adjust its fields and check the result.")
        self.assertEqual(len(prompts), 2, "No extra AI review pass")
        self.assertIn('"current_section"', prompts[1])
        self.assertIn("Choose the report.", prompts[1])
        self.assertIn("continued_point", prompts[1])
        self.assertIn("multiple related sentences belong in this same point", prompts[0])
        self.assertIn("slide or transcript fragment is NOT by itself a topic change", prompts[0])

    def test_continuation_and_genuine_new_subtopic_are_kept_separate(self):
        chapters, continued = SummaryProcessor._parse_chapter_response(json.dumps({
            "continued_point": "Continue the same explanation.",
            "continued_sections": [{"timestamp": "01:10", "title": "Billing", "point": "Review charges."}],
            "chapters": [],
        }))
        self.assertEqual(chapters, [])
        self.assertTrue(continued[0]["continue_previous"])
        self.assertEqual(continued[1]["title"], "Billing")
        with self.assertRaises(json.JSONDecodeError):
            SummaryProcessor._parse_chapter_response('{"continued_point": [], "chapters": []}')

    def test_duplicate_adjacent_topics_merge_without_time_based_caps(self):
        chapters = SummaryProcessor._normalize_chapters([
            {"timestamp": "00:00", "title": "Report editing", "sections": [
                {"timestamp": "00:00", "title": "Fields", "point": "Choose fields."},
                {"timestamp": "00:10", "title": "Fields", "point": "Rename fields."},
            ]},
            {"timestamp": "00:20", "title": "Report editing", "sections": [
                {"timestamp": "00:20", "title": "Fields", "point": "Check fields."},
                {"timestamp": "00:30", "title": "Permissions", "point": "Choose access."},
            ]},
            {"timestamp": "00:40", "title": "Billing", "sections": []},
        ], [], end=50)
        self.assertEqual([chapter["title"] for chapter in chapters], ["Report editing", "Billing"])
        self.assertEqual([section["title"] for section in chapters[0]["sections"]], ["Fields", "Permissions"])
        self.assertEqual(chapters[0]["sections"][0]["point"], "Choose fields. Rename fields. Check fields.")
        self.assertEqual(chapters[1]["timestamp"], "00:40", "A genuine brief topic must remain")

    def test_flat_structure_and_detail_level_change_the_prompt(self):
        processor = self._processor(None, None, None)
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: (
            prompts.append(prompt) or '{"chapters": [{"timestamp": "00:00:00", "title": "Intro"}]}'
        )
        transcript = [{"start": index * 60, "duration": 60, "text": "word"} for index in range(36)]
        asyncio.run(processor.generate_summary(transcript, None, structure="flat", detail="brief"))
        asyncio.run(processor.generate_summary(transcript, None, structure="sections", detail="detailed"))
        self.assertIn("broad, clearly distinct subjects", prompts[0])
        self.assertNotIn('"sections"', prompts[0])
        self.assertIn("independently meaningful learning objectives", prompts[1])
        self.assertIn('"sections"', prompts[1])
        for prompt in prompts:
            self.assertNotRegex(prompt, r"about \d+ chapters|covers \d+ minutes")
            self.assertIn("not its duration", prompt)
            self.assertIn("Do not target a chapter count", prompt)
        response = asyncio.run(processor.generate_summary(transcript, None, detail="huge"))
        self.assertFalse(response.content["success"])

    def test_continued_sections_join_the_previous_chapter(self):
        from processors import summary_processor as module
        processor = self._processor(None, None, None)
        answers = iter([
            '{"chapters": [{"timestamp": "00:00:00", "title": "Basics", "sections": '
            '[{"timestamp": "00:00:00", "title": "Terms", "point": "Terms matter."}]}]}',
            '{"continued_sections": [{"timestamp": "00:05:00", "title": "More terms", "point": "More."}], '
            '"chapters": [{"timestamp": "00:08:00", "title": "Practice", "sections": []}]}',
        ])
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or next(answers)
        transcript = [{"start": index * 30, "duration": 30, "text": "word " * 40} for index in range(24)]
        with mock.patch.object(module, "GROQ_CHAPTER_CHUNK_CHARS", 3500):
            response = asyncio.run(processor.generate_summary(transcript, None))
        self.assertTrue(response.content["success"], response.content)
        self.assertIn('ended inside the chapter "Basics"', prompts[1])
        chapters = response.content["chapters"]
        self.assertEqual([section["title"] for section in chapters[0]["sections"]], ["Terms", "More terms"])
        self.assertEqual(chapters[1]["title"], "Practice")

    def test_invalid_json_is_retried_with_low_reasoning_then_split(self):
        from processors import summary_processor as module
        processor = self._processor(None, None, None)
        calls = []

        def complete(provider, model, prompt, temperature, json_output, max_tokens, fast=False):
            calls.append((len(prompt), fast))
            if len(calls) <= 2:
                raise RuntimeError("Error code: 400 json_validate_failed: Failed to generate JSON")
            stamp = prompt.split("from ", 1)[1][:8]
            return '{"chapters": [{"timestamp": "%s", "title": "Topic at %s"}]}' % (stamp, stamp)

        processor._complete = complete
        transcript = [{"start": index * 60, "text": "word " * 20} for index in range(20)]
        with mock.patch.object(module, "GROQ_CHAPTER_CHUNK_CHARS", 100000):
            response = asyncio.run(processor.generate_summary(transcript, "video"))
        self.assertTrue(response.content["success"], response.content)
        self.assertTrue(all(fast for _, fast in calls))
        # Two failures on the whole part, then the two halves succeed.
        self.assertEqual(len(calls), 4)
        self.assertLess(calls[2][0], calls[0][0])
        self.assertEqual(len(response.content["chapters"]), 2)

    def test_fenced_chapter_response_keeps_nested_sections_and_continuations(self):
        data = {
            "continued_sections": [{"timestamp": "04:00", "title": "Continuation", "point": "More."}],
            "chapters": [{
                "timestamp": "08:00", "title": "Practice",
                "sections": [{"timestamp": "08:00", "title": "Example", "point": "Use [x] and quotes \"like this\"."}],
            }],
        }
        chapters, continued = SummaryProcessor._parse_chapter_response(
            "```json\n" + json.dumps(data) + "\n```",
        )
        self.assertEqual(chapters, data["chapters"])
        self.assertEqual(continued, data["continued_sections"])
        with self.assertRaises(json.JSONDecodeError):
            SummaryProcessor._parse_chapter_response(json.dumps(data) + json.dumps(data))

    def test_extra_json_data_gets_one_corrective_retry(self):
        processor = self._processor(None, None, None)
        replies = iter([
            '{"chapters":[{"timestamp":"00:00","title":"Incomplete"}]}'
            '{"chapters":[{"timestamp":"02:00","title":"Extra"}]}',
            '{"chapters":[{"timestamp":"00:00","title":"Complete",'
            '"sections":[{"timestamp":"00:00","title":"Topic","point":"Full answer."}]}]}',
        ])
        prompts = []
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or next(replies)
        processor._save_chapters = mock.Mock()
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 180, "text": "Lecture"}], "video", "gemini-3.5-flash-lite",
        ))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(len(prompts), 2)
        self.assertIn("exactly ONE JSON object", prompts[1])
        self.assertEqual(response.content["chapters"][0]["title"], "Complete")
        processor._save_chapters.assert_called_once()

    def test_malformed_response_retries_then_splits_without_repeating_finished_parts(self):
        from processors import summary_processor as module
        processor = self._processor(None, None, None)
        prompts = []
        answers = iter([
            '{"chapters":[{"timestamp":"00:00","title":"First"}]}',
            '{"chapters":[]}{"chapters":[]}',
            '{"chapters":[]}{"chapters":[]}',
            '{"chapters":[{"timestamp":"02:00","title":"Second"}]}',
            '{"chapters":[{"timestamp":"03:00","title":"Third"}]}',
        ])
        processor._complete = lambda provider, model, prompt, *args: prompts.append(prompt) or next(answers)
        with mock.patch.object(module, "CHAPTER_CHUNK_CHARS", 60):
            response = asyncio.run(processor.generate_summary(
                [{"start": i * 60, "duration": 60, "text": "word " * 3} for i in range(4)],
                "video", "gemini-3.5-flash-lite",
            ))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(len(prompts), 5)
        self.assertEqual([chapter["title"] for chapter in response.content["chapters"]],
                         ["First", "Second", "Third"])

    def test_invalid_json_falls_back_and_never_replaces_saved_chapters_on_failure(self):
        processor = self._processor(None, None, None)
        calls = []
        processor._save_chapters = mock.Mock()

        def complete(provider, model, *args):
            calls.append(model)
            if model == "gemini-3.5-flash-lite":
                return '{"chapters":[]}{"chapters":[]}'
            return '{"chapters":[{"timestamp":"00:00","title":"Fallback"}]}'

        processor._complete = complete
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 60, "text": "Lecture"}], "video", "gemini-3.5-flash-lite",
        ))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(calls, ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash"])
        processor._save_chapters.assert_called_once()
        processor._save_chapters.reset_mock()
        processor._complete = lambda *args: '{"chapters":[]}{"chapters":[]}'
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 60, "text": "Lecture"}], "video", "gemini-3.5-flash-lite",
        ))
        self.assertFalse(response.content["success"])
        self.assertIn("Extra data", response.content["error"])
        processor._save_chapters.assert_not_called()

    def test_chapters_follow_the_open_translation_and_are_saved_with_it(self):
        processor = self._processor(None, None, None)
        saved, prompts = [], []
        processor._save_chapters = lambda *args: saved.append(args)

        def complete(provider, model, prompt, *args):
            prompts.append(prompt)
            return '{"chapters": [{"timestamp": "00:00:00", "title": "Einf\u00fchrung", "summary": "Die Lernenden werden in Python eingef\u00fchrt."}]}'

        processor._complete = complete
        transcript = [{"start": 0, "duration": 5, "text": "Willkommen"}]
        response = asyncio.run(processor.generate_summary(transcript, "video", transcript_language="de"))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(response.content["language"], "de")
        self.assertIn("German", prompts[0])
        self.assertTrue(prompts[0].rstrip().endswith("the language of the transcript."))
        self.assertEqual(saved[0][2], "de")
        response = asyncio.run(processor.generate_summary(transcript, "video", transcript_language="xx"))
        self.assertFalse(response.content["success"])

    def test_arabic_transcript_is_detected_and_fillers_are_removed(self):
        arabic = [{"start": 0, "text": "\u0645\u0631\u062d\u0628\u0627 \u0628\u0643\u0645 \u0641\u064a \u0627\u0644\u0645\u062d\u0627\u0636\u0631\u0629"}]
        self.assertEqual(SummaryProcessor._detect_transcript_language(arabic), "ar")
        self.assertIsNone(SummaryProcessor._detect_transcript_language([{"start": 0, "text": "Hello class"}]))
        self.assertIn("Arabic", SummaryProcessor._summary_language_instruction("ar"))
        for text, expected in (
            ("The learner will learn how to submit assignments.", "How to submit assignments."),
            ("You will be introduced to the concept of entropy.", "The concept of entropy."),
            ("This section covers grading rules.", "Grading rules."),
            ("Learners understand that tests matter.", "Tests matter."),
            ("Gradient descent minimizes the loss.", "Gradient descent minimizes the loss."),
        ):
            self.assertEqual(SummaryProcessor._filler_free(text), expected)
        chapters = SummaryProcessor._normalize_chapters([{
            "timestamp": "00:00", "title": "Intro", "summary": "The learner will learn the course rules.",
            "sections": [{"timestamp": "00:00", "title": "Rules", "point": "Students will learn that exams are oral."}],
        }], [])
        self.assertEqual(chapters[0]["summary"], "The course rules.")
        self.assertEqual(chapters[0]["sections"][0]["point"], "Exams are oral.")

    def test_gemini_timeout_moves_on_with_a_clear_reason(self):
        processor = self._processor(None, None, None)
        processor.groq_client = None
        calls = []

        def complete(provider, model, *args):
            calls.append(model)
            if model == "gemini-3.6-flash":
                raise asyncio.TimeoutError()
            return '[{"timestamp": "00:00", "title": "Intro"}]'

        processor._complete = complete
        response = asyncio.run(processor.generate_summary([{"start": 0, "text": "Welcome"}], "video"))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(calls[:2], ["gemini-3.6-flash", "gemini-3.8-flash"])

    def test_summary_falls_back_to_second_gemini_model(self):
        class QuotaOnFirstModel:
            def __init__(self):
                self.models = []

            def generate_content(self, model, **kwargs):
                self.models.append(model)
                if model == "gemini-3.6-flash":
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return SimpleNamespace(text='[{"timestamp": "00:00", "title": "Intro"}]')

        gemini = QuotaOnFirstModel()
        processor = self._processor(gemini, None, None)
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 5, "text": "Welcome"}], "video", "gemini-3.6-flash"
        ))

        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(gemini.models, ["gemini-3.6-flash", "gemini-3.8-flash"])
        self.assertEqual(response.content["provider"], "Gemini fallback")
        self.assertEqual(response.content["model"], "gemini-3.8-flash")

    def test_summary_reports_every_exhausted_provider(self):
        class Exhausted:
            def generate_content(self, **kwargs):
                raise RuntimeError("429 quota exceeded")

            def create(self, **kwargs):
                raise RuntimeError("Error code: 429 insufficient_quota")

        exhausted = Exhausted()
        processor = self._processor(exhausted, exhausted, exhausted)
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 5, "text": "Welcome"}], "video"
        ))

        self.assertFalse(response.content["success"])
        self.assertIn("Every configured AI model", response.content["error"])
        self.assertIn("Groq groq-test", response.content["error"])
        self.assertNotIn("add GROQ_API_KEY", response.content["error"])

    def test_daily_groq_quota_and_busy_gemini_fall_back_to_current_lite_model(self):
        processor = self._processor(None, None, None)
        calls = []
        processor._save_chapters = mock.Mock()

        def complete(provider, model, *args):
            calls.append((provider, model))
            if provider == "groq":
                raise RuntimeError(
                    "429 rate_limit_exceeded on tokens per day (TPD). Please try again in 22m0.192s."
                )
            if model != "gemini-3.5-flash-lite":
                raise RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand.")
            return '{"chapters": [{"timestamp": "00:00", "title": "Intro"}]}'

        processor._complete = complete
        with mock.patch("processors.summary_processor.asyncio.sleep", new_callable=mock.AsyncMock) as sleep:
            response = asyncio.run(processor.generate_summary(
                [{"start": 0, "duration": 5, "text": "Welcome"}], "video",
            ))
        self.assertTrue(response.content["success"], response.content)
        self.assertEqual(response.content["model"], "gemini-3.5-flash-lite")
        self.assertEqual(calls, [
            ("groq", "groq-test"),
            ("gemini", "gemini-3.6-flash"),
            ("gemini", "gemini-3.8-flash"),
            ("gemini", "gemini-3.5-flash-lite"),
        ])
        sleep.assert_not_called()
        processor._save_chapters.assert_called_once()

    def test_unavailable_providers_preserve_chapters_and_report_retry_time(self):
        processor = self._processor(None, None, None)
        processor._save_chapters = mock.Mock()

        def complete(provider, model, *args):
            raise RuntimeError("429 rate_limit_exceeded. Please try again in 22m0.192s.")

        processor._complete = complete
        response = asyncio.run(processor.generate_summary(
            [{"start": 0, "duration": 5, "text": "Welcome"}], "video",
        ))
        self.assertFalse(response.content["success"])
        self.assertIn("22m0.192s", response.content["error"])
        self.assertNotIn("add GROQ_API_KEY", response.content["error"])
        processor._save_chapters.assert_not_called()


if __name__ == "__main__":
    unittest.main()
