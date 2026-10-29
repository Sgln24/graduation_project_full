import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mindmap
from tutor import TutorError

SEGMENTS = [
    {"text": "Gradient descent minimizes the loss function by following gradients.", "start": 5.0, "duration": 5.0},
    {"text": "The learning rate controls how large each gradient step becomes.", "start": 12.0, "duration": 5.0},
    {"text": "For example, a large learning rate can overshoot the minimum.", "start": 20.0, "duration": 5.0},
    {"text": "The update rule is w = w - lr * grad.", "start": 28.0, "duration": 5.0},
    {"text": "Overfitting happens when a model memorizes training data.", "start": 60.0, "duration": 5.0},
    {"text": "Regularization reduces overfitting by penalizing large weights.", "start": 68.0, "duration": 5.0},
    {"text": "Cross validation estimates how a model generalizes to unseen data.", "start": 75.0, "duration": 5.0},
]
SCENES = [{"time_seconds": 0}, {"time_seconds": 55}]


def _segments():
    return [dict(s, source="t.json") for s in SEGMENTS]


class MindmapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            patch.object(mindmap, "TRANSCRIPTS_DIR", Path(self.tmp.name)),
            patch.object(mindmap, "load_transcript_segments", side_effect=lambda _id: _segments()),
            patch.object(mindmap, "load_scenes", return_value=SCENES),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_sections_follow_slide_boundaries(self):
        groups = mindmap.split_sections(_segments(), SCENES)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1][0]["start"], 60.0)

    def test_extractive_map_is_grounded_in_real_timestamps(self):
        result = mindmap.generate_mindmap("vid1", use_llm=False)
        valid = {s["start"] for s in SEGMENTS}
        sections = result["root"]["children"]
        self.assertEqual(len(sections), 2)

        def refs(node):
            yield from node.get("references", [])
            for item in node.get("key_points", []) + node.get("examples", []) + node.get("formulas", []):
                yield item["reference"]
            for child in node.get("children", []):
                yield from refs(child)

        all_refs = list(refs(result["root"]))
        self.assertTrue(all_refs)
        for ref in all_refs:
            self.assertIn(ref["seconds"], valid)
        self.assertEqual(sections[1]["references"][0]["slide_index"], 1)
        self.assertTrue(any("w = w" in f["text"] for f in sections[0]["formulas"]))
        self.assertTrue(any("For example" in e["text"] for e in sections[0]["examples"]))
        self.assertTrue(sections[0]["children"])
        self.assertEqual(result["methods"], ["extractive"])

    def test_relationships_revision_and_cache(self):
        result = mindmap.generate_mindmap("vid1", use_llm=False)
        self.assertTrue(any(r["type"] == "next" for r in result["relationships"]))
        self.assertEqual(len(result["revision"]), 2)
        self.assertTrue(all(item["bullets"] for item in result["revision"]))
        self.assertEqual(mindmap.load_cached("vid1")["video_id"], "vid1")

    def test_llm_output_is_validated_and_fake_formulas_dropped(self):
        llm = {"title": "Optimization", "summary": "Gradient descent lowers loss.", "key_points": ["Step along the gradient", 5],
               "formulas": ["w = w - lr * grad", "E = mc^2"]}
        with patch.object(mindmap, "_ollama_json", return_value=llm):
            result = mindmap.generate_mindmap("vid1")
        node = result["root"]["children"][0]
        self.assertEqual(node["title"], "Optimization")
        self.assertEqual(node["method"], "llm-local")
        self.assertEqual([f["text"] for f in node["formulas"]], ["w = w - lr * grad"])
        self.assertEqual(len(node["key_points"]), 1)
        self.assertFalse(result["cloud_used"])

    def test_cloud_requires_consent_and_respects_local_only(self):
        with patch.object(mindmap, "_ollama_json", return_value=None), \
                patch.object(mindmap, "_groq_json", return_value={"summary": "x"}) as groq:
            result = mindmap.generate_mindmap("vid1")
            groq.assert_not_called()
            self.assertFalse(result["cloud_used"])
            with patch.dict(os.environ, {"TUTOR_LOCAL_ONLY": "true"}):
                mindmap.generate_mindmap("vid1", allow_cloud_fallback=True)
            groq.assert_not_called()
            result = mindmap.generate_mindmap("vid1", allow_cloud_fallback=True)
            self.assertTrue(groq.called)
            self.assertTrue(result["cloud_used"])
            self.assertTrue(any("Groq" in n for n in result["notices"]))

    def test_local_failure_falls_back_to_extractive(self):
        with patch.object(mindmap, "_ollama_json", return_value=None):
            result = mindmap.generate_mindmap("vid1")
        self.assertEqual(result["methods"], ["extractive"])

    def test_errors(self):
        with self.assertRaises(TutorError):
            mindmap.generate_mindmap("../bad")
        with patch.object(mindmap, "load_transcript_segments", return_value=[]):
            with self.assertRaises(TutorError):
                mindmap.generate_mindmap("vid1")

    def test_parse_json_handles_noise(self):
        self.assertEqual(mindmap._parse_json('sure: {"a": 1} done'), {"a": 1})
        self.assertIsNone(mindmap._parse_json("no json"))


if __name__ == "__main__":
    unittest.main()
