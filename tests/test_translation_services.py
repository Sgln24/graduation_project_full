import asyncio
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock
from urllib.error import HTTPError, URLError

from translation_services import (
    NoRedirects, TranslationServiceError, character_count, service_batches, translate_texts,
)
from processors.summary_processor import SummaryProcessor
from vtt_translation import parse_vtt, translate_vtt_batch


class TranslationServicesTests(unittest.TestCase):
    def processor(self):
        processor = SummaryProcessor.__new__(SummaryProcessor)
        processor.model = None
        processor.model_name = "gemini-test"
        processor._refresh_clients = lambda: None
        return processor

    def response(self, data):
        return io.BytesIO(json.dumps(data).encode())

    def test_libretranslate_batches_arrays_and_optional_key(self):
        with mock.patch.dict(os.environ, {
            "LIBRETRANSLATE_URL": "http://127.0.0.1:5000", "LIBRETRANSLATE_API_KEY": "test-key",
        }), mock.patch("translation_services.build_opener") as opener:
            opener.return_value.open.return_value = self.response({"translatedText": ["Hallo", "Welt"]})
            self.assertEqual(translate_texts("libretranslate", ["Hello", "World"], "de"), ["Hallo", "Welt"])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:5000/translate")
            self.assertEqual(json.loads(request.data), {
                "q": ["Hello", "World"], "source": "auto", "target": "de",
                "format": "text", "api_key": "test-key",
            })
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 30)
            self.assertIsInstance(opener.call_args.args[0], NoRedirects)

    def test_microsoft_uses_v3_plain_text_auth_and_region(self):
        with mock.patch.dict(os.environ, {
            "MICROSOFT_TRANSLATOR_KEY": "test-key", "MICROSOFT_TRANSLATOR_REGION": "westeurope",
        }), mock.patch("translation_services.build_opener") as opener:
            opener.return_value.open.return_value = self.response([
                {"translations": [{"to": "de", "text": "Hallo"}]},
                {"translations": [{"to": "de", "text": "Welt"}]},
            ])
            self.assertEqual(translate_texts("microsoft", ["Hello", "World"], "de"), ["Hallo", "Welt"])
            request = opener.return_value.open.call_args.args[0]
            self.assertIn("api-version=3.0", request.full_url)
            self.assertIn("textType=plain", request.full_url)
            self.assertEqual(request.get_header("Ocp-apim-subscription-key"), "test-key")
            self.assertEqual(request.get_header("Ocp-apim-subscription-region"), "westeurope")
            self.assertEqual(json.loads(request.data), [{"Text": "Hello"}, {"Text": "World"}])

    def test_invalid_service_responses_never_silently_drop_lines(self):
        for provider, data in (
            ("libretranslate", {"translatedText": ["One"]}),
            ("libretranslate", {"translatedText": [None, "Two"]}),
            ("libretranslate", {"translatedText": ["", "Two"]}),
            ("microsoft", []),
            ("microsoft", [{"translations": [{"to": "ar", "text": "One"}]}] * 2),
        ):
            with self.subTest(provider=provider, data=data), mock.patch.dict(os.environ, {
                "MICROSOFT_TRANSLATOR_KEY": "test-key", "LIBRETRANSLATE_URL": "https://translate.example",
            }), mock.patch("translation_services.build_opener") as opener:
                opener.return_value.open.return_value = self.response(data)
                with self.assertRaises(TranslationServiceError):
                    translate_texts(provider, ["Hello", "World"], "de")

    def test_network_errors_are_explicit_and_do_not_expose_keys_or_text(self):
        for status in (400, 401, 403, 429, 500):
            error = HTTPError("https://example", status, "SECRET-TEXT", {}, io.BytesIO(b"SECRET-KEY"))
            with self.subTest(status=status), mock.patch.dict(os.environ, {
                "MICROSOFT_TRANSLATOR_KEY": "test-key",
            }), mock.patch("translation_services.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(TranslationServiceError) as raised:
                    translate_texts("microsoft", ["PRIVATE"], "de")
                self.assertEqual(raised.exception.transient, status == 429 or status >= 500)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("PRIVATE", str(raised.exception))
        with mock.patch.dict(os.environ, {"LIBRETRANSLATE_URL": "https://translate.example"}), \
                mock.patch("translation_services.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("SECRET")
            with self.assertRaisesRegex(TranslationServiceError, "could not connect"):
                translate_texts("libretranslate", ["Hello"], "de")

    def test_invalid_urls_are_rejected_before_sending_credentials(self):
        for url in ("http://public.example", "https://user:password@example.com",
                    "https://example.com?key=secret", "file:///private", ""):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"LIBRETRANSLATE_URL": url}), \
                    mock.patch("translation_services.build_opener") as opener:
                with self.assertRaises(ValueError):
                    translate_texts("libretranslate", ["Hello"], "de")
                opener.assert_not_called()

    def test_batches_respect_service_limits_and_unicode(self):
        items = [{"id": str(index), "text": "a" * 1000} for index in range(101)]
        self.assertEqual([len(batch) for batch in service_batches(items, "libretranslate")],
                         [10] * 10 + [1])
        self.assertEqual([len(batch) for batch in service_batches(items, "microsoft")], [50, 50, 1])
        self.assertEqual(character_count("\U0001f600"), 2)
        with self.assertRaises(ValueError):
            service_batches([{"text": "a" * 50001}], "microsoft")
        self.assertEqual([len(batch) for batch in service_batches(
            [{"text": "a"}] * 1001, "microsoft")], [1000, 1])

    def test_auto_order_and_explicit_selection_do_not_change_chapter_chain(self):
        processor = self.processor()
        llms = [("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-test")]
        processor._provider_chain = mock.Mock(return_value=llms)
        with mock.patch.dict(os.environ, {
            "LIBRETRANSLATE_URL": "http://localhost:5000", "MICROSOFT_TRANSLATOR_KEY": "test-key",
        }):
            self.assertEqual(processor._translation_chain(), llms + [
                ("libretranslate", "libretranslate"), ("microsoft", "microsoft-translator")])
            self.assertEqual(processor._translation_chain("libretranslate"), [
                ("libretranslate", "libretranslate"), *llms, ("microsoft", "microsoft-translator")])
            self.assertEqual(processor._translation_chain("microsoft-translator"), [
                ("microsoft", "microsoft-translator"), *llms, ("libretranslate", "libretranslate")])
        self.assertEqual(processor._provider_chain(), llms)

    def test_unconfigured_services_disabled_and_service_only_auto_supported(self):
        processor = self.processor()
        with mock.patch.dict(os.environ, {"LIBRETRANSLATE_URL": "", "MICROSOFT_TRANSLATOR_KEY": ""}):
            catalog = {entry["id"]: entry["available"] for entry in processor.translation_models()}
            self.assertFalse(catalog["libretranslate"])
            self.assertFalse(catalog["microsoft-translator"])
        with mock.patch.dict(os.environ, {
            "LIBRETRANSLATE_URL": "http://localhost:5000", "MICROSOFT_TRANSLATOR_KEY": "test-key",
        }):
            self.assertEqual(processor._translation_chain(), [
                ("libretranslate", "libretranslate"), ("microsoft", "microsoft-translator")])
            with mock.patch("processors.summary_processor.translate_texts", return_value=["Hallo"]):
                result = asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de"))
            self.assertEqual(result["provider"], "LibreTranslate")

    def test_service_translation_preserves_fields_and_reports_characters_not_tokens(self):
        processor = self.processor()
        transcript = [{"start": 1.2, "duration": 2, "text": "Hello", "speaker": "A"},
                      {"start": 3.2, "duration": 1, "text": "World", "speaker": "A"}]
        with mock.patch.dict(os.environ, {"LIBRETRANSLATE_URL": "http://localhost:5000"}), \
                mock.patch("processors.summary_processor.translate_texts", return_value=["Hallo", "Welt"]) as translate:
            result = asyncio.run(processor.translate_transcript(
                transcript, "de", "libretranslate", allow_fallback=False))
            translate.assert_called_once_with("libretranslate", ["Hello", "World"], "de")
        self.assertEqual(result["transcript"], [
            {**transcript[0], "text": "Hallo"}, {**transcript[1], "text": "Welt"}])
        self.assertEqual(result["usage"]["submitted_characters"], 10)
        self.assertEqual(result["usage"]["translated_characters"], 10)
        self.assertEqual(result["usage"]["llm_requests"], 0)
        self.assertEqual(result["usage"]["reported_requests"], 0)
        self.assertFalse(result["usage"]["complete"])

    def test_failed_service_falls_back_only_when_allowed_and_keeps_completed_batches(self):
        processor = self.processor()
        transcript = [{"start": index, "duration": 1, "text": "a" * 6000} for index in range(3)]
        for fallback in (False, True):
            with self.subTest(fallback=fallback), mock.patch(
                "processors.summary_processor.translate_texts", side_effect=[
                    ["Translated first"], TranslationServiceError("LibreTranslate HTTP 503", transient=True),
                    ["Translated second", "Translated third"],
                ]
            ) as translate:
                chain = [("libretranslate", "libretranslate"), ("microsoft", "microsoft-translator")]
                operation = processor.translate_transcript(transcript, "de", _chain=chain, allow_fallback=fallback)
                if fallback:
                    result = asyncio.run(operation)
                    self.assertEqual(translate.call_count, 3)
                    self.assertEqual(translate.call_args.args[1], ["a" * 6000, "a" * 6000])
                    self.assertEqual(result["transcript"][0]["text"], "Translated first")
                    self.assertEqual(result["usage"]["submitted_characters"], 24000)
                    self.assertEqual(result["usage"]["translated_characters"], 18000)
                    self.assertEqual(result["provider"], "Microsoft Translator fallback")
                else:
                    with self.assertRaises(TranslationServiceError):
                        asyncio.run(operation)
                    self.assertEqual(translate.call_count, 2)

    def test_authentication_failure_never_falls_back_and_unconfigured_selection_fails(self):
        processor = self.processor()
        with mock.patch("processors.summary_processor.translate_texts",
                        side_effect=TranslationServiceError("HTTP 401")) as translate:
            with self.assertRaises(TranslationServiceError):
                asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    _chain=[("microsoft", "microsoft-translator"), ("libretranslate", "libretranslate")]))
            translate.assert_called_once()
        with mock.patch.dict(os.environ, {"MICROSOFT_TRANSLATOR_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                asyncio.run(processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    "microsoft-translator", allow_fallback=False))

    def test_llm_failure_continues_with_open_source_before_microsoft(self):
        processor = self.processor()
        processor._provider_chain = lambda *args, **kwargs: [("groq", "openai/gpt-oss-20b")]
        processor._complete = mock.Mock(side_effect=RuntimeError("429 rate limit"))
        with mock.patch.dict(os.environ, {
            "LIBRETRANSLATE_URL": "http://localhost:5000", "MICROSOFT_TRANSLATOR_KEY": "test-key",
        }), mock.patch("processors.summary_processor.translate_texts", side_effect=[
            TranslationServiceError("LibreTranslate HTTP 503", transient=True), ["Hallo"],
        ]) as translate:
            result = asyncio.run(processor.translate_transcript(
                [{"start": 0, "duration": 1, "text": "Hello"}], "de"))
        self.assertEqual([call.args[0] for call in translate.call_args_list], ["libretranslate", "microsoft"])
        self.assertEqual(result["provider"], "Microsoft Translator fallback")
        self.assertEqual(result["usage"]["llm_requests"], 1)
        self.assertEqual(result["usage"]["character_requests"], 2)
        self.assertEqual(result["usage"]["translated_characters"], 5)
        self.assertFalse(result["usage"]["complete"])

    def test_llm_malformed_output_reaches_open_source_only_with_fallback_enabled(self):
        for fallback in (False, True):
            processor = self.processor()
            processor._provider_chain = lambda *args, **kwargs: [("groq", "openai/gpt-oss-20b")]
            processor._complete = mock.Mock(return_value="invalid JSON")
            with self.subTest(fallback=fallback), mock.patch.dict(os.environ, {
                "LIBRETRANSLATE_URL": "http://localhost:5000", "MICROSOFT_TRANSLATOR_KEY": "",
            }), mock.patch("processors.summary_processor.translate_texts", return_value=["Hallo"]) as translate, \
                    mock.patch("processors.summary_processor.asyncio.sleep", new=mock.AsyncMock()):
                operation = processor.translate_transcript(
                    [{"start": 0, "duration": 1, "text": "Hello"}], "de",
                    "groq:openai/gpt-oss-20b", allow_fallback=fallback)
                if fallback:
                    result = asyncio.run(operation)
                    self.assertEqual(result["provider"], "LibreTranslate fallback")
                    translate.assert_called_once()
                else:
                    with self.assertRaisesRegex(ValueError, "after bounded recovery"):
                        asyncio.run(operation)
                    translate.assert_not_called()

    def test_service_zip_keeps_vtt_markup_and_character_report(self):
        processor = self.processor()
        source = b"WEBVTT\n\nNOTE unchanged\n\ncue-1\n00:01.000 --> 00:03.000 align:start\n<b>Hello</b>\nWorld\n"
        with tempfile.TemporaryDirectory() as workspace, mock.patch.dict(os.environ, {
            "MICROSOFT_TRANSLATOR_KEY": "test-key",
        }), mock.patch("processors.summary_processor.translate_texts", return_value=["Hallo", "Welt"]):
            steps = []
            response = asyncio.run(translate_vtt_batch(
                [("source.vtt", parse_vtt(source))], "de", "microsoft-translator",
                processor, workspace, steps.append, allow_fallback=False))
            with zipfile.ZipFile(response.path) as archive:
                output = archive.read("source_DE.vtt").decode()
                self.assertIn("NOTE unchanged", output)
                self.assertIn("cue-1\n00:01.000 --> 00:03.000 align:start\n<b>Hallo</b>\nWelt", output)
                report = json.loads(archive.read("translation-report.json"))[0]
                self.assertEqual(report["usage"]["translated_characters"], 10)
                self.assertEqual(report["usage"]["llm_requests"], 0)
            self.assertTrue(any("no LLM tokens used" in step for step in steps))


if __name__ == "__main__":
    unittest.main()
