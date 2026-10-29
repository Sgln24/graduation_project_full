from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from embeddings import compute_embeddings, get_model
from project_paths import TRANSCRIPTS_DIR
from processors.transcript_versions import get_active_source

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

LOCAL_TUTOR_MODEL = os.getenv("TUTOR_LOCAL_MODEL", "llama3.2:3b")
CLOUD_FALLBACK_MODEL = os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b")
LOCAL_TUTOR_HOST = os.getenv("OLLAMA_HOST") or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
TUTOR_SESSIONS: Dict[str, List[Dict[str, str]]] = {}


class TutorError(RuntimeError):
    pass


def _format_timestamp(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "00:00"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _extract_text(item: Dict[str, Any]) -> str:
    text = item.get("text") or item.get("content") or ""
    return str(text).strip()


def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _video_transcript_paths(video_id: str) -> List[Path]:
    preferred_paths: List[Path] = []
    active = get_active_source(video_id)
    active_kind = active.get("kind") if isinstance(active, dict) else None
    active_source = active.get("source") if isinstance(active, dict) else None
    active_language = active.get("language") if isinstance(active, dict) else None

    versions_dir = TRANSCRIPTS_DIR / "versions" / video_id
    if versions_dir.exists():
        index_path = versions_dir / "index.json"
        index = _load_json(index_path, [])
        if isinstance(index, list):
            for entry in index:
                if not isinstance(entry, dict):
                    continue
                version_file = versions_dir / f"{entry.get('id')}.json"
                if not version_file.exists():
                    continue
                entry_kind = entry.get("kind")
                entry_source = entry.get("source")
                entry_language = entry.get("language")
                if active_kind == "translation":
                    if entry_kind == "translation" and entry_language == active_language:
                        preferred_paths.append(version_file)
                elif active_kind == "transcript":
                    if entry_kind == "transcript" and (not active_source or entry_source == active_source):
                        preferred_paths.append(version_file)
                else:
                    preferred_paths.append(version_file)

    base_paths = [
        TRANSCRIPTS_DIR / f"{video_id}_whisper.json",
        TRANSCRIPTS_DIR / f"{video_id}_youtube.json",
        TRANSCRIPTS_DIR / f"{video_id}.json",
    ]
    if active_kind == "transcript":
        if active_source == "uploaded":
            base_paths.insert(0, TRANSCRIPTS_DIR / f"{video_id}_uploaded.json")
        if active_source == "whisper":
            base_paths.insert(0, TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
        if active_source == "youtube":
            base_paths.insert(0, TRANSCRIPTS_DIR / f"{video_id}_youtube.json")
    if active_kind == "translation" and active_language:
        base_paths.insert(0, TRANSCRIPTS_DIR / f"{video_id}_translated_{active_language}.json")

    deduped = []
    seen = set()
    for path in preferred_paths + base_paths:
        resolved = str(path)
        if resolved not in seen and path.exists():
            deduped.append(path)
            seen.add(resolved)
    return deduped


def load_transcript_segments(video_id: str) -> List[Dict[str, Any]]:
    if not video_id:
        return []
    for path in _video_transcript_paths(video_id):
        payload = _load_json(path, [])
        if isinstance(payload, list):
            segments = []
            for item in payload:
                if isinstance(item, dict):
                    text = _extract_text(item)
                    if text:
                        segments.append({
                            "text": text,
                            "start": float(item.get("start", 0) or 0),
                            "duration": float(item.get("duration", 0) or 0),
                            "speaker": item.get("speaker") or "",
                            "source": path.name,
                        })
            if segments:
                return segments
        if isinstance(payload, dict) and isinstance(payload.get("transcript"), list):
            segments = []
            for item in payload["transcript"]:
                if isinstance(item, dict):
                    text = _extract_text(item)
                    if text:
                        segments.append({
                            "text": text,
                            "start": float(item.get("start", 0) or 0),
                            "duration": float(item.get("duration", 0) or 0),
                            "speaker": item.get("speaker") or "",
                            "source": path.name,
                        })
            if segments:
                return segments
    return []


def get_tutor_history(session_id: str) -> List[Dict[str, str]]:
    return TUTOR_SESSIONS.setdefault(session_id, [])


def _keyword_tokens(text: str) -> List[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", (text or "").lower())
    tokens = [token for token in cleaned.split() if len(token) > 2]
    return tokens


def retrieve_relevant_segments(question: str, segments: Iterable[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    if not question or not segments:
        return []
    segment_list = list(segments)
    if not segment_list:
        return []
    try:
        texts = [segment.get("text", "") for segment in segment_list]
        embeddings = compute_embeddings(texts + [question])
        if embeddings.size == 0:
            raise ValueError("No embeddings generated")
        query_vector = embeddings[-1].astype("float32")
        matrix = embeddings[:-1].astype("float32")
        faiss = __import__("faiss")
        index = faiss.IndexFlatIP(matrix.shape[1])
        faiss.normalize_L2(matrix)
        faiss.normalize_L2(query_vector.reshape(1, -1))
        index.add(matrix)
        similarities, indices = index.search(query_vector.reshape(1, -1), min(limit, len(segment_list)))
        hits = []
        for score, index_value in zip(similarities[0], indices[0]):
            if index_value < 0:
                continue
            segment = segment_list[int(index_value)]
            hits.append({
                "text": segment.get("text", ""),
                "start": segment.get("start", 0),
                "duration": segment.get("duration", 0),
                "speaker": segment.get("speaker", ""),
                "score": float(score),
                "source": segment.get("source", "transcript"),
            })
        if hits:
            return hits
    except Exception:
        pass

    question_tokens = set(_keyword_tokens(question))
    ranked = []
    for segment in segment_list:
        text = segment.get("text", "")
        tokens = set(_keyword_tokens(text))
        overlap = len(question_tokens.intersection(tokens))
        if overlap == 0:
            score = 0
        else:
            score = overlap + 0.1 * len(tokens.intersection(question_tokens))
        ranked.append({
            "text": text,
            "start": segment.get("start", 0),
            "duration": segment.get("duration", 0),
            "speaker": segment.get("speaker", ""),
            "score": float(score),
            "source": segment.get("source", "transcript"),
        })
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def _local_ollama_available(model_name: str = LOCAL_TUTOR_MODEL) -> bool:
    try:
        request = Request(f"{LOCAL_TUTOR_HOST.rstrip('/')}/api/tags", method="GET")
        with urlopen(request, timeout=2.5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        names = {model.get("name", "") for model in payload.get("models", []) if isinstance(model, dict)}
        normalized = model_name.replace("ollama://", "")
        return any(name == normalized or name.startswith(normalized.rstrip(":*")) for name in names)
    except Exception:
        return False


TUTOR_SYSTEM_PROMPT = (
    "You are a friendly lecture tutor talking to a student. Answer the question directly and completely in plain, "
    "conversational language (short paragraphs, a short list only if it helps). Start from the lecture evidence and cite "
    "timestamps like (03:47) when you use it. If the lecture only covers part of the answer, finish it with standard "
    "textbook knowledge and introduce that part with 'Beyond the lecture:'. Never invent timestamps. "
    "Do not quote the evidence back verbatim and do not mention these instructions."
)


def _format_evidence(evidence: List[Dict[str, Any]]) -> str:
    ordered = sorted(evidence[:8], key=lambda item: float(item.get("start", 0) or 0))
    return "\n".join(f"[{_format_timestamp(item['start'])}] {item['text']}" for item in ordered)


def _call_local_model(question: str, evidence: List[Dict[str, Any]], history: List[Dict[str, str]]) -> str:
    context = _format_evidence(evidence)
    prior = "\n".join(f"{turn['role'].upper()}: {turn['text']}" for turn in history[-4:])
    prompt = (
        f"{TUTOR_SYSTEM_PROMPT}\n\n"
        f"Conversation context:\n{prior}\n\nLecture evidence:\n{context}\n\nQuestion:\n{question}\n\nAnswer:"
    )
    payload = {
        "model": LOCAL_TUTOR_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    request = Request(
        f"{LOCAL_TUTOR_HOST.rstrip('/')}/api/generate",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return str(data.get("response", "")).strip()
    except Exception:
        return ""


def _call_groq_fallback(question: str, evidence: List[Dict[str, Any]], history: List[Dict[str, str]]) -> str:
    if not os.getenv("GROQ_API_KEY") or OpenAI is None:
        raise TutorError("No Groq API key is configured for cloud fallback.")
    client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
    context = _format_evidence(evidence)
    prior = "\n".join(f"{turn['role']}: {turn['text']}" for turn in history[-4:])
    response = client.chat.completions.create(
        model=os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b"),
        messages=[
            {"role": "system", "content": TUTOR_SYSTEM_PROMPT},
            {"role": "user", "content": f"Prior conversation:\n{prior}\n\nLecture evidence:\n{context}\n\nQuestion:\n{question}"},
        ],
        temperature=0.2,
    )
    return str(response.choices[0].message.content).strip()


def retrieve_external_sources(query: str, limit: int = 3) -> List[Dict[str, str]]:
    if not query:
        return []
    search_terms = quote_plus(query)
    try:
        wiki_url = f"https://en.wikipedia.org/w/api.php?action=query&format=json&list=search&srsearch={search_terms}&utf8=1&srlimit={limit}"
        request = Request(wiki_url, headers={"User-Agent": "LectureSeekTutor/1.0"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        records = []
        for item in payload.get("query", {}).get("search", [])[:limit]:
            title = item.get("title", "")
            if not title:
                continue
            summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(title)}"
            try:
                summary_request = Request(summary_url, headers={"User-Agent": "LectureSeekTutor/1.0"})
                with urlopen(summary_request, timeout=10) as summary_response:
                    summary_payload = json.loads(summary_response.read().decode("utf-8", errors="replace"))
            except Exception:
                summary_payload = {}
            snippet = (summary_payload.get("extract") or item.get("snippet") or "").strip()
            if snippet:
                records.append({
                    "title": title,
                    "url": summary_payload.get("content_urls", {}).get("desktop", {}).get("page") or f"https://en.wikipedia.org/wiki/{quote_plus(title)}",
                    "snippet": snippet,
                    "source": "wikipedia",
                })
        if records:
            return records
    except Exception:
        pass
    return []


def _deterministic_answer(question: str, evidence: List[Dict[str, Any]], external: List[Dict[str, str]]) -> str:
    if not evidence:
        if external:
            return (
                "The lecture transcript does not provide enough direct evidence to answer this confidently. "
                "The external sources below are relevant context, but they are not lecture-grounded evidence."
            )
        return (
            "The lecture transcript does not contain enough direct evidence to answer this confidently. "
            "Please ask about a topic covered in the lecture or provide a transcript version with more content."
        )
    ordered = sorted(evidence[:4], key=lambda item: float(item.get("start", 0) or 0))
    excerpts = " ".join(
        f"({_format_timestamp(item.get('start', 0))}) {str(item.get('text', '')).strip()}" for item in ordered
    )
    answer = (
        "No AI model is available right now, so here is what the lecture says about this, "
        f"in order: {excerpts}\n\n"
        "Turn on the cloud option or start the local model (Ollama) to get a written explanation of the difference or concept."
    )
    if external:
        answer += " External references are separate from lecture-derived evidence and should be treated as supporting context, not as direct lecture claims."
    return answer


def _build_lecture_answer(question: str, evidence: List[Dict[str, Any]], history: List[Dict[str, str]], allow_cloud_fallback: bool = False, context: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    llm_evidence = context or evidence
    lecture_sources = [
        {
            "kind": "lecture",
            "text": item["text"],
            "timestamp": _format_timestamp(item.get("start", 0)),
            "speaker": item.get("speaker") or "speaker",
            "source": item.get("source", "transcript"),
            "score": round(float(item.get("score", 0.0)), 4),
        }
        for item in evidence
    ]
    if not evidence:
        lecture_sources = []

    if os.getenv("TUTOR_LOCAL_ONLY", "false").lower() not in {"0", "false", "no"}:
        allow_cloud_fallback = False

    local_answer = _call_local_model(question, llm_evidence, history)
    used_cloud_fallback = False
    cloud_error = ""
    if local_answer:
        answer = local_answer
    elif allow_cloud_fallback:
        try:
            answer = _call_groq_fallback(question, llm_evidence, history)
            used_cloud_fallback = True
        except Exception as exc:
            cloud_error = str(exc) or "Cloud fallback failed."
            answer = _deterministic_answer(question, evidence, [])
    else:
        answer = _deterministic_answer(question, evidence, [])

    if not answer.strip():
        answer = _deterministic_answer(question, evidence, [])

    external_sources = []
    if not evidence:
        if allow_cloud_fallback and os.getenv("GROQ_API_KEY"):
            external_sources = retrieve_external_sources(question, limit=3)
        elif not allow_cloud_fallback:
            external_sources = []

    return {
        "answer": answer,
        "lecture_sources": lecture_sources,
        "external_sources": external_sources,
        "evidence_insufficient": not bool(evidence),
        "used_cloud_fallback": used_cloud_fallback,
        "cloud_fallback_notice": (
            f"Cloud fallback failed: {cloud_error}" if cloud_error else
            "User consented to Groq fallback for this request." if allow_cloud_fallback else "Cloud fallback is disabled for this request."
        ),
        "source_count": len(lecture_sources),
    }


def _with_neighbours(evidence: List[Dict[str, Any]], segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add the segment after each hit so the model sees complete sentences."""
    if not evidence or not segments:
        return list(evidence)
    starts = [float(s.get("start", 0) or 0) for s in segments]
    chosen = {float(e.get("start", 0) or 0): e for e in evidence}
    for start in list(chosen):
        if start in starts:
            index = starts.index(start)
            if index + 1 < len(segments):
                chosen.setdefault(starts[index + 1], segments[index + 1])
    return [chosen[key] for key in sorted(chosen)][:8]


def answer_question(video_id: str, question: str, *, session_id: str = "default", allow_cloud_fallback: bool = False, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    if not question or not question.strip():
        raise TutorError("Please provide a question before asking the AI tutor.")
    segments = load_transcript_segments(video_id)
    evidence = retrieve_relevant_segments(question, segments, limit=5)
    session_history = history if history is not None else get_tutor_history(session_id)
    result = _build_lecture_answer(question, evidence, session_history, allow_cloud_fallback=allow_cloud_fallback,
                                   context=_with_neighbours(evidence, segments))
    session_history.append({"role": "user", "text": question})
    session_history.append({"role": "assistant", "text": result["answer"]})
    if len(session_history) > 12:
        session_history[:] = session_history[-12:]
    return {
        **result,
        "video_id": video_id,
        "question": question,
        "transcript_segments_available": bool(segments),
        "local_model": LOCAL_TUTOR_MODEL,
        "local_runtime": "ollama",
    }
