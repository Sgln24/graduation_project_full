"""Grounded visual lecture explanation: builds a hierarchical concept map from a transcript.

Every node's references point at real transcript segments (and the slide active at that time),
so timestamps are never invented. An optional LLM (local Ollama first, Groq only with explicit
consent) rewrites the explanations; without one, an extractive fallback is used.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from embeddings import extract_text_from_ocr
from project_paths import SCENES_DIR, TRANSCRIPTS_DIR
from tutor import (
    LOCAL_TUTOR_HOST,
    LOCAL_TUTOR_MODEL,
    OpenAI,
    TutorError,
    _format_timestamp,
    _load_json,
    _local_ollama_available,
    load_transcript_segments,
)

MAX_SECTIONS = 8
MAX_CONCEPTS = 4
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STOPWORDS = set(
    "a about above after again all also an and any are as at be because been before being below between both but by "
    "can could did do does doing down during each few for from further had has have having he her here hers him his how "
    "i if in into is it its just let like me more most my no nor not now of off on once one only or other our out over own "
    "really right same she should so some such than that the their them then there these they thing things this those "
    "through to too under until up very was we were what when where which while who whom why will with would you your "
    "going gonna okay yeah actually basically look want know see say said get got make way well lot kind sort"
    .split()
)
EXAMPLE_CUE = re.compile(r"\b(for example|for instance|such as|e\.g\.|consider|imagine|suppose)\b", re.I)
FORMULA_PATTERN = re.compile(r"[A-Za-z0-9_)\]]\s*(=|≈|≤|≥|\^|∑|∫|√|±)\s*[A-Za-z0-9_(\[√]")
LLM_SYSTEM = (
    "You explain one section of a lecture for students. Use ONLY the supplied lecture text; do not add outside facts. "
    "Reply with a single JSON object: {\"title\": short topic title, \"summary\": 1-3 simple sentences, "
    "\"key_points\": up to 4 short strings, \"examples\": up to 2 strings taken from the text, "
    "\"formulas\": formulas copied verbatim from the text}."
)


def local_only() -> bool:
    return os.getenv("TUTOR_LOCAL_ONLY", "false").lower() not in {"0", "false", "no"}


def mindmap_path(video_id: str):
    if not SAFE_ID.match(video_id or ""):
        raise TutorError("Invalid video id.")
    return TRANSCRIPTS_DIR / f"{video_id}_mindmap.json"


def load_cached(video_id: str) -> Optional[Dict[str, Any]]:
    data = _load_json(mindmap_path(video_id), None)
    return data if isinstance(data, dict) and data.get("root") else None


def load_scenes(video_id: str) -> List[Dict[str, Any]]:
    data = _load_json(SCENES_DIR / f"{video_id}.json", [])
    scenes = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    return sorted(scenes, key=lambda s: float(s.get("time_seconds", 0) or 0))


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z\-']{2,}", (text or "").lower()) if t not in STOPWORDS and len(t) > 3]


def split_sections(segments: List[Dict[str, Any]], scenes: List[Dict[str, Any]], max_sections: int = MAX_SECTIONS) -> List[List[Dict[str, Any]]]:
    """Group segments by slide boundaries when available, otherwise into equal windows."""
    if not segments:
        return []
    times = sorted({float(s.get("time_seconds", 0) or 0) for s in scenes})
    groups: List[List[Dict[str, Any]]] = []
    if len(times) >= 2:
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for segment in segments:
            buckets.setdefault(max(0, bisect.bisect_right(times, segment["start"]) - 1), []).append(segment)
        groups = [buckets[key] for key in sorted(buckets)]
        merged: List[List[Dict[str, Any]]] = []
        for group in groups:
            if merged and len(group) < 3:
                merged[-1].extend(group)
            else:
                merged.append(list(group))
        groups = merged
        while len(groups) > max_sections:
            index = min(range(len(groups) - 1), key=lambda i: len(groups[i]) + len(groups[i + 1]))
            groups[index:index + 2] = [groups[index] + groups[index + 1]]
    if len(groups) < 2:
        count = max(1, min(max_sections, math.ceil(len(segments) / 8)))
        size = math.ceil(len(segments) / count)
        groups = [segments[i:i + size] for i in range(0, len(segments), size)]
    return groups


def _slide_for(time_seconds: float, scenes: List[Dict[str, Any]]) -> Optional[int]:
    index = None
    for i, scene in enumerate(scenes):
        if float(scene.get("time_seconds", 0) or 0) <= time_seconds:
            index = i
        else:
            break
    return index


def _reference(segment: Dict[str, Any], scenes: List[Dict[str, Any]]) -> Dict[str, Any]:
    start = float(segment["start"])
    return {
        "seconds": start,
        "timestamp": _format_timestamp(start),
        "text": segment["text"][:200],
        "slide_index": _slide_for(start, scenes),
        "source": segment.get("source", "transcript"),
    }


def _keywords(sections: List[List[Dict[str, Any]]]) -> List[List[Tuple[str, float]]]:
    counters = [Counter(t for seg in section for t in _tokens(seg["text"])) for section in sections]
    document_frequency = Counter(term for counter in counters for term in counter)
    total = len(sections)
    ranked = []
    for counter in counters:
        scored = [(term, count * (math.log((1 + total) / (1 + document_frequency[term])) + 1)) for term, count in counter.items()]
        ranked.append(sorted(scored, key=lambda item: (-item[1], item[0])))
    return ranked


def _slide_text(scenes: List[Dict[str, Any]], start: float, end: float) -> List[str]:
    try:
        entries = extract_text_from_ocr(scenes)
    except Exception:
        return []
    return [e["text"].strip() for e in entries if start <= float(e.get("start", 0) or 0) <= end and e["text"].strip()]


def _extractive_section(index: int, section: List[Dict[str, Any]], keywords: List[Tuple[str, float]], scenes: List[Dict[str, Any]]) -> Dict[str, Any]:
    top = [term for term, _ in keywords[:MAX_CONCEPTS]]
    weights = dict(keywords)
    ranked = sorted(section, key=lambda s: -sum(weights.get(t, 0) for t in set(_tokens(s["text"]))))
    key_units = sorted(ranked[:3], key=lambda s: s["start"])
    end = section[-1]["start"] + float(section[-1].get("duration", 0) or 0)
    formulas = [
        {"text": seg["text"][:160], "reference": _reference(seg, scenes)}
        for seg in section if FORMULA_PATTERN.search(seg["text"])
    ][:3]
    for line in _slide_text(scenes, section[0]["start"], end):
        if FORMULA_PATTERN.search(line) and len(formulas) < 5:
            formulas.append({"text": line[:160], "reference": _reference(section[0], scenes)})
    examples = [
        {"text": seg["text"][:200], "reference": _reference(seg, scenes)}
        for seg in section if EXAMPLE_CUE.search(seg["text"])
    ][:2]
    concepts = []
    for term in top:
        containing = [s for s in section if term in s["text"].lower()]
        if not containing:
            continue
        best = max(containing, key=lambda s: sum(weights.get(t, 0) for t in set(_tokens(s["text"]))))
        concepts.append({
            "id": f"s{index}-c{len(concepts)}",
            "title": term.title(),
            "kind": "concept",
            "explanation": best["text"],
            "key_points": [],
            "examples": [],
            "formulas": [],
            "references": [_reference(best, scenes)],
            "children": [],
        })
    return {
        "id": f"s{index}",
        "title": " & ".join(t.title() for t in top[:2]) or f"Section {index + 1}",
        "kind": "section",
        "explanation": " ".join(seg["text"] for seg in key_units[:2]),
        "key_points": [{"text": seg["text"], "reference": _reference(seg, scenes)} for seg in key_units],
        "examples": examples,
        "formulas": formulas,
        "references": [_reference(section[0], scenes)],
        "start": section[0]["start"],
        "end": end,
        "keywords": top,
        "method": "extractive",
        "children": concepts,
    }


def _ollama_json(prompt: str) -> Optional[Dict[str, Any]]:
    if not _local_ollama_available():
        return None
    payload = {"model": LOCAL_TUTOR_MODEL, "prompt": f"{LLM_SYSTEM}\n\n{prompt}", "stream": False, "format": "json",
               "options": {"temperature": 0.2}}
    request = Request(f"{LOCAL_TUTOR_HOST.rstrip('/')}/api/generate", method="POST",
                      headers={"Content-Type": "application/json"}, data=json.dumps(payload).encode("utf-8"))
    try:
        with urlopen(request, timeout=90) as response:
            return _parse_json(json.loads(response.read().decode("utf-8", "replace")).get("response", ""))
    except Exception:
        return None


def _groq_json(prompt: str) -> Optional[Dict[str, Any]]:
    if not os.getenv("GROQ_API_KEY") or OpenAI is None:
        return None
    try:
        client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
        reply = client.chat.completions.create(
            model=os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b"),
            messages=[{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return _parse_json(reply.choices[0].message.content or "")
    except Exception:
        return None


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _strings(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [v.strip()[:240] for v in value if isinstance(v, str) and v.strip()][:limit]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def apply_llm(node: Dict[str, Any], section: List[Dict[str, Any]], data: Dict[str, Any], method: str, scenes: List[Dict[str, Any]]) -> None:
    """Merge validated LLM output into a section node; unverifiable formulas are dropped."""
    source_text = _norm(" ".join(seg["text"] for seg in section) + " " + " ".join(_slide_text(scenes, node["start"], node["end"])))
    title = data.get("title")
    summary = data.get("summary")
    if isinstance(title, str) and title.strip():
        node["title"] = title.strip()[:80]
    if isinstance(summary, str) and summary.strip():
        node["explanation"] = summary.strip()[:600]
    points = _strings(data.get("key_points"), 4)
    if points:
        anchor = node["references"][0]
        node["key_points"] = [{"text": p, "reference": anchor} for p in points]
    examples = _strings(data.get("examples"), 2)
    if examples:
        anchor = node["references"][0]
        node["examples"] = [{"text": e, "reference": anchor} for e in examples]
    verified = [f for f in _strings(data.get("formulas"), 4) if _norm(f) in source_text]
    if verified:
        node["formulas"] = [{"text": f, "reference": node["references"][0]} for f in verified]
    node["method"] = method


def build_relationships(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    relationships = []
    for i, left in enumerate(sections):
        for right in sections[i + 1:]:
            shared = [k for k in left["keywords"] if k in right["keywords"]]
            if shared:
                relationships.append({"source": left["id"], "target": right["id"], "type": "related", "label": ", ".join(shared[:3])})
    for left, right in zip(sections, sections[1:]):
        relationships.append({"source": left["id"], "target": right["id"], "type": "next", "label": "followed by"})
    return relationships[:60]


def build_revision(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    revision = []
    for node in sections:
        bullets = [p["text"] for p in node["key_points"][:3]] or [node["explanation"]]
        revision.append({
            "node_id": node["id"],
            "title": node["title"],
            "bullets": bullets,
            "formulas": [f["text"] for f in node["formulas"]],
            "terms": [c["title"] for c in node["children"]],
            "timestamp": node["references"][0]["timestamp"],
            "seconds": node["references"][0]["seconds"],
        })
    return revision


def generate_mindmap(video_id: str, *, use_llm: bool = True, allow_cloud_fallback: bool = False) -> Dict[str, Any]:
    mindmap_path(video_id)
    segments = load_transcript_segments(video_id)
    if not segments:
        raise TutorError("No transcript is available for this lecture yet. Generate or load a transcript first.")
    scenes = load_scenes(video_id)
    sections = split_sections(segments, scenes)
    keywords = _keywords(sections)
    nodes = [_extractive_section(i, sec, keywords[i], scenes) for i, sec in enumerate(sections)]

    if local_only():
        allow_cloud_fallback = False
    methods, notices = set(), []
    if use_llm:
        for node, section in zip(nodes, sections):
            prompt = "Lecture text:\n" + "\n".join(seg["text"] for seg in section)[:6000]
            data, method = _ollama_json(prompt), "llm-local"
            if data is None and allow_cloud_fallback:
                data, method = _groq_json(prompt), "llm-groq"
                if data is not None and not any("Groq" in n for n in notices):
                    notices.append("Lecture text for some sections was sent to Groq because you allowed cloud fallback.")
            if data is not None:
                apply_llm(node, section, data, method, scenes)
            methods.add(node["method"])
    if "extractive" in methods or not methods:
        notices.append("Some explanations are extracted directly from the transcript rather than AI-written.")
    result = {
        "video_id": video_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": "Lecture concept map",
        "root": {"id": "root", "title": "Lecture", "kind": "root", "explanation": f"{len(nodes)} main topics covered in this lecture.", "children": nodes},
        "relationships": build_relationships(nodes),
        "revision": build_revision(nodes),
        "methods": sorted(methods or {"extractive"}),
        "cloud_used": "llm-groq" in methods,
        "local_only": local_only(),
        "segments_used": len(segments),
        "slides_available": len(scenes),
        "notices": notices,
    }
    path = mindmap_path(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
