"""Validated, conflict-checked edits to saved lecture content."""
import hashlib
import json
import re
import threading

from fastapi import APIRouter, HTTPException, Request
from processors import transcript_versions
from project_paths import SCENES_DIR, SUMMARIES_DIR, TRANSCRIPTS_DIR


router = APIRouter()
_lock = threading.Lock()


def _read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (OSError, ValueError) as error:
        raise HTTPException(500, "Could not read saved lecture content.") from error


def _text(value, label, *, blank=False, limit=20000):
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise HTTPException(400, f"{label} must be text{' (not empty)' if not blank else ''}, at most {limit} characters.")
    return value.strip()


def _seconds(timestamp):
    if not isinstance(timestamp, str) or not re.fullmatch(r"(?:\d+:)?\d+:[0-5]\d(?:\.\d{1,3})?", timestamp):
        raise HTTPException(400, "Use MM:SS or HH:MM:SS timestamps (optional milliseconds).")
    parts = timestamp.split(":")
    if len(parts) == 3 and int(parts[1]) >= 60:
        raise HTTPException(400, "Minutes in HH:MM:SS must be below 60.")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _snapshot(video_id, kind, language, source, version_id, transcript_language=None):
    selected_language = language if transcript_language is None else transcript_language
    if not all(isinstance(value, str) for value in (video_id, kind, language, source, version_id)):
        raise HTTPException(400, "Content type, source, language and version must be strings.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        raise HTTPException(400, "Invalid video ID.")
    if kind not in {"chapters", "slides", "transcript"}:
        raise HTTPException(400, "Invalid content type.")
    if (language not in {"", "en", "de", "ar", "pl"} or not isinstance(selected_language, str)
            or selected_language not in {"", "en", "de", "ar", "pl"} or source not in {"whisper", "uploaded", "youtube"}):
        raise HTTPException(400, "Invalid transcript source or language.")
    if kind == "slides":
        scenes = _read(SCENES_DIR / f"{video_id}.json", [])
        _rows(scenes)
        return scenes, None
    entry = None
    if version_id:
        try:
            entry, transcript = transcript_versions.read_version(video_id, version_id)
        except (KeyError, FileNotFoundError) as error:
            raise HTTPException(409, "The selected transcript version no longer exists. Reload it.") from error
        except (OSError, ValueError) as error:
            raise HTTPException(500, "Could not read the selected transcript version.") from error
        if (entry.get("language") or "") != selected_language:
            raise HTTPException(409, "The selected transcript language changed. Reopen the editor.")
        if not selected_language and entry.get("source") != source:
            raise HTTPException(409, "The selected transcript source changed. Reopen the editor.")
        active_path = (TRANSCRIPTS_DIR / f"{video_id}_translated_{selected_language}.json" if selected_language
                       else TRANSCRIPTS_DIR / f"{video_id}_{source}.json")
        if _read(active_path) != transcript:
            raise HTTPException(409, "Another transcript version is now active. Reload it before editing.")
    else:
        path = (TRANSCRIPTS_DIR / f"{video_id}_translated_{selected_language}.json" if selected_language
                else TRANSCRIPTS_DIR / f"{video_id}_{source}.json")
        transcript = _read(path)
        if transcript is None and source == "youtube" and not selected_language:
            transcript = _read(TRANSCRIPTS_DIR / f"{video_id}.json", [])
    if not isinstance(transcript, list) or not transcript:
        raise HTTPException(409, "Load a saved transcript before editing this content.")
    _rows(transcript)
    if kind == "transcript":
        return transcript, entry
    path = SUMMARIES_DIR / (f"{video_id}_summary_{language}.json" if language else f"{video_id}.json")
    chapters = _read(path)
    if chapters is None and language:
        chapters = _read(SUMMARIES_DIR / f"{video_id}.json", [])
    if chapters is None:
        chapters = []
    if isinstance(chapters, list) and chapters and isinstance(chapters[0], list):
        if not all(isinstance(group, list) for group in chapters):
            raise HTTPException(500, "Saved chapters have an invalid format.")
        chapters = [chapter for group in chapters for chapter in group]
    _rows(chapters)
    for chapter in chapters:
        _rows(chapter.get("sections") or [])
    return chapters, (entry, transcript, path)


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HTTPException(500, "Saved content has an invalid format.")


@router.get("/editable_content/{video_id}")
def editable_content(video_id: str, kind: str, language: str = "", source: str = "whisper", version_id: str = "",
                     transcript_language: str | None = None):
    with _lock:
        content, _ = _snapshot(video_id, kind, language, source, version_id, transcript_language)
        return {"content": content, "revision": _revision(content)}


def _chapters(original, edits):
    if not isinstance(edits, list) or len(edits) != len(original):
        raise HTTPException(400, "The chapter count must not change.")
    result = []
    for old, edit in zip(original, edits):
        if not isinstance(edit, dict):
            raise HTTPException(400, "Invalid chapter edit.")
        timestamp = _text(edit.get("timestamp"), "Chapter timestamp", limit=30)
        _seconds(timestamp)
        chapter = {**old, "title": _text(edit.get("title"), "Chapter title", limit=500),
                   "timestamp": timestamp, "summary": _text(edit.get("summary", ""), "Chapter summary", blank=True),
                   "user_edited": True}
        if timestamp != old.get("timestamp"):
            chapter["timestamp_edited"] = True
        sections = old.get("sections") or []
        changed = edit.get("sections", [])
        if not isinstance(changed, list) or len(changed) != len(sections):
            raise HTTPException(400, "The section count must not change.")
        chapter["sections"] = []
        for section, change in zip(sections, changed):
            if not isinstance(change, dict):
                raise HTTPException(400, "Invalid section edit.")
            stamp = _text(change.get("timestamp"), "Section timestamp", limit=30)
            _seconds(stamp)
            chapter["sections"].append({
                **section, "timestamp": stamp, "title": _text(change.get("title"), "Section title", limit=500),
                "point": _text(change.get("point", ""), "Section text", blank=True),
                **({"timestamp_edited": True} if stamp != section.get("timestamp") else {}),
            })
        result.append(chapter)
    starts = [_seconds(chapter["timestamp"]) for chapter in result]
    if any(right <= left for left, right in zip(starts, starts[1:])):
        raise HTTPException(400, "Chapter timestamps must be in strictly increasing order.")
    for index, chapter in enumerate(result):
        times = [_seconds(section["timestamp"]) for section in chapter["sections"]]
        end = starts[index + 1] if index + 1 < len(starts) else float("inf")
        if any(time < starts[index] or time >= end for time in times) or any(
            right <= left for left, right in zip(times, times[1:])
        ):
            raise HTTPException(400, "Sections must be ordered and inside their chapter's time range.")
    return result


def persist_chapters(video_id, content, context, language, selected_language, source):
    entry, transcript, path = context
    version = transcript_versions.save_version(
        video_id, "translation" if selected_language else "transcript", transcript,
        source=(entry or {}).get("source") or ("uploaded" if source == "uploaded" else "whisper"),
        language=selected_language or None, chapters=content,
    ) if language == selected_language else entry
    transcript_versions._write_json(path, content)
    return version


@router.post("/editable_content/{video_id}")
async def save_content_edits(video_id: str, request: Request):
    try:
        data = await request.json()
    except ValueError as error:
        raise HTTPException(400, "Edits must be valid JSON.") from error
    if not isinstance(data, dict):
        raise HTTPException(400, "Edits must be an object.")
    kind, language = data.get("kind"), data.get("language", "")
    source, version_id = data.get("source", "whisper"), data.get("version_id", "")
    with _lock:
        selected_language = data.get("transcript_language", language)
        original, context = _snapshot(video_id, kind, language, source, version_id, selected_language)
        if data.get("revision") != _revision(original):
            raise HTTPException(409, "This content changed since editing started. Reopen the editor to avoid overwriting changes.")
        edits = data.get("edits")
        if not isinstance(original, list) or not original:
            raise HTTPException(400, "There is no saved content to edit.")
        try:
            if kind == "slides":
                if not isinstance(edits, list) or len(edits) != len(original):
                    raise HTTPException(400, "The slide count must not change.")
                content = []
                for scene, value in zip(original, edits):
                    title = _text(value, "Slide title", blank=True, limit=500)
                    if language:
                        content.append({**scene, "title_translations": {
                            **(scene.get("title_translations") or {}), language: title,
                        }})
                    else:
                        content.append({**scene, "title": title, "title_edited": bool(title)})
                transcript_versions._write_json(SCENES_DIR / f"{video_id}.json", content)
                return {"content": content, "revision": _revision(content)}
            transcript_versions.sync_existing(video_id)
            if kind == "chapters":
                content = _chapters(original, edits)
                version = persist_chapters(video_id, content, context, language, selected_language, source)
            else:
                if not isinstance(edits, list) or len(edits) != len(original):
                    raise HTTPException(400, "The transcript segment count must not change.")
                content = [{**item, "text": _text(text, "Transcript text")}
                           for item, text in zip(original, edits)]
                if transcript_versions.content_hash(content) == transcript_versions.content_hash(original):
                    raise HTTPException(400, "No transcript text has changed. Use Cancel to keep the original.")
                path = SUMMARIES_DIR / (f"{video_id}_summary_{language}.json" if language else f"{video_id}.json")
                chapters = _read(path, [])
                entry = context or {}
                stored = (_read(transcript_versions._versions_dir(video_id) / f"{entry['id']}.json", {})
                          if entry else {})
                metadata = stored.get("metadata")
                if metadata is None and not language and source == "whisper":
                    metadata = _read(TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json")
                # Save a backup of the displayed version before creating corrections.
                transcript_versions.save_version(
                    video_id, "translation" if language else "transcript", original,
                    source=entry.get("source") or ("uploaded" if source == "uploaded" else "whisper"),
                    language=language or None, chapters=chapters, metadata=metadata,
                )
                version = transcript_versions.save_version(
                    video_id, "translation" if language else "transcript", content,
                    source=entry.get("source") or ("uploaded" if source == "uploaded" else "whisper"),
                    language=language or None, model=entry.get("model"), chapters=chapters, edited=True,
                    metadata=metadata, based_on=entry.get("based_on"),
                )
                transcript_versions.activate_version(video_id, version["id"])
            return {"content": content, "revision": _revision(content), "version": version}
        except OSError as error:
            raise HTTPException(500, "Could not save edits. Check file permissions and try again.") from error
