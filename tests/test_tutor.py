import json
import os
import unittest
from unittest.mock import patch

import tutor


class TutorBehaviorTests(unittest.TestCase):
    def test_retrieve_relevant_segments_returns_timestamps_and_sources(self):
        segments = [
            {
                "text": "The central theorem explains gradient descent and optimization.",
                "start": 125.0,
                "duration": 18.0,
                "speaker": "Instructor",
                "source": "lecture.json",
            },
            {
                "text": "The lecture then covers a different practical example.",
                "start": 300.0,
                "duration": 12.0,
                "speaker": "Instructor",
                "source": "lecture.json",
            },
        ]

        evidence = tutor.retrieve_relevant_segments("What is the central theorem?", segments, limit=3)

        self.assertTrue(evidence)
        self.assertEqual(evidence[0]["start"], 125.0)
        self.assertIn("source", evidence[0])

    def test_answer_question_tracks_history_and_marks_insufficient_evidence(self):
        with patch("tutor.load_transcript_segments", return_value=[]):
            history = []
            result = tutor.answer_question(
                "demo-video",
                "Where is the proof discussed?",
                session_id="demo-session",
                history=history,
            )

        self.assertTrue(result["evidence_insufficient"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIn("lecture transcript does not contain enough direct evidence", result["answer"])

    def test_local_only_mode_blocks_cloud_fallback(self):
        with patch.dict(os.environ, {"TUTOR_LOCAL_ONLY": "true", "GROQ_API_KEY": "secret"}, clear=False):
            result = tutor._build_lecture_answer(
                "What is the definition of the objective?",
                [],
                [],
                allow_cloud_fallback=True,
            )

        self.assertFalse(result["used_cloud_fallback"])
        self.assertIn("Cloud fallback is disabled", result["cloud_fallback_notice"])

    def test_external_sources_are_distinct_from_lecture_evidence(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=10):
            url = request.full_url
            if "action=query" in url:
                return FakeResponse({"query": {"search": [{"title": "Machine learning", "snippet": "Machine learning is a field."}]}})
            return FakeResponse({
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Machine_learning"}},
                "extract": "Machine learning studies systems that improve from data.",
            })

        with patch("tutor.urlopen", side_effect=fake_urlopen):
            sources = tutor.retrieve_external_sources("machine learning", limit=1)

        self.assertTrue(sources)
        self.assertEqual(sources[0]["source"], "wikipedia")
        self.assertIn("Machine learning", sources[0]["title"])


if __name__ == "__main__":
    unittest.main()
