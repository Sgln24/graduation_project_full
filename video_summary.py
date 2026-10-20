"""Store whole-video paragraphs separately from chapter outlines."""
import hashlib
import json
import re

from fastapi import HTTPException
from processors.transcript_versions import _write_json
from project_paths import SUMMARIES_DIR


def summary_context(video_id, data):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        raise HTTPException(400, "Invalid video ID.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected summary options.")
    transcript = data.get("transcript")
    language = data.get("transcript_language", "")
    if not isinstance(language, str) or language not in ("", "en", "de", "ar", "pl"):
        raise HTTPException(400, "Unsupported transcript language.")
    if (not isinstance(transcript, list) or not transcript
            or any(not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in transcript)):
        raise HTTPException(400, "Load a transcript before generating a summary.")
    texts = [item["text"].strip() for item in transcript if item["text"].strip()]
    if not texts:
        raise HTTPException(400, "The open transcript contains no text.")
    digest = hashlib.sha256(json.dumps([language, texts], ensure_ascii=False).encode("utf-8")).hexdigest()
    return SUMMARIES_DIR / "overviews" / video_id / f"{digest[:32]}.json", transcript, language


def load_summary(path):
    if not path.is_file():
        return {"exists": False, "summary": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
            raise ValueError("Invalid saved paragraph.")
        return {**data, "exists": True}
    except (OSError, ValueError) as error:
        raise HTTPException(500, f"Could not read the saved video summary: {error}") from error


def save_summary(path, result):
    _write_json(path, result)
