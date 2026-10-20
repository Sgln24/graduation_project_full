"""Keeps every generated transcript and translation of a video so it can be chosen again."""
import hashlib
import json
import os
import re
import threading
import time
import uuid

from project_paths import SUMMARIES_DIR, TRANSCRIPTS_DIR

LANGUAGE_NAMES = {"de": "German", "en": "English", "ar": "Arabic", "pl": "Polish"}
_LOCK = threading.Lock()
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _versions_dir(video_id):
    if not _SAFE_ID.match(video_id or ""):
        raise ValueError("Invalid video id")
    return TRANSCRIPTS_DIR / "versions" / video_id


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short names: Windows paths are limited to 260 characters.
    temporary = path.with_name(f"~{uuid.uuid4().hex[:8]}.tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        # ASCII escapes keep the files readable by code that opens them with the platform encoding.
        json.dump(data, file)
    os.replace(temporary, path)


def _active_source_path(video_id):
    return TRANSCRIPTS_DIR / f"{video_id}_active_source.json"


def get_active_source(video_id):
    """Which transcript/chapter version (whisper, uploaded, or a translation) is
    currently active for this video, so reloading the page can restore it."""
    data = _read_json(_active_source_path(video_id), {})
    if isinstance(data, dict) and data.get("kind") in {"transcript", "translation"}:
        return data
    return {"kind": "transcript", "source": "whisper"}


def set_active_source(video_id, kind, *, source=None, language=None):
    """Record which version is active so it keeps being shown after a reload."""
    if kind not in {"transcript", "translation"}:
        return
    payload = {"kind": kind}
    if kind == "translation":
        payload["language"] = language
    else:
        payload["source"] = source or "whisper"
    _write_json(_active_source_path(video_id), payload)


def content_hash(segments):
    """Identify a transcript by its timing and wording only."""
    normalized = [
        [round(float(item.get("start", 0) or 0), 2), str(item.get("text", "")).strip(), item.get("speaker")]
        for item in segments or [] if isinstance(item, dict)
    ]
    return hashlib.sha1(json.dumps(normalized, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text_hash(segments):
    return hashlib.sha1(json.dumps(
        [[round(float(item.get("start", 0) or 0), 2), str(item.get("text", "")).strip()]
         for item in segments or [] if isinstance(item, dict)],
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _load_index(video_id):
    index = _read_json(_versions_dir(video_id) / "index.json", [])
    return index if isinstance(index, list) else []


def _model_label(model):
    try:
        from cloud_transcribe import CLOUD_TRANSCRIPTION_MODELS
        label = CLOUD_TRANSCRIPTION_MODELS.get(model, {}).get("label")
    except Exception:
        label = None
    return label or (model if ":" in model else f"Whisper {model}")


def _describe_original(entry):
    if entry["kind"] == "translation":
        language = LANGUAGE_NAMES.get(entry.get("language"), str(entry.get("language", "")).upper())
        label = f"{language} translation"
        if entry.get("model"):
            label += f" · {entry['model']}"
        return label
    source = entry.get("source")
    if source == "uploaded":
        return "Uploaded transcript"
    label = "Transcript"
    if entry.get("model"):
        label += f" · {_model_label(entry['model'])}"
    if entry.get("diarization"):
        label += " · speakers"
    return label


def _describe(entry):
    return _describe_original(entry) + (" · edited" if entry.get("edited") else "")


def save_version(video_id, kind, segments, *, source=None, language=None, model=None,
                 metadata=None, chapters=None, created_at=None, based_on=None, edited=False):
    """Store a transcript version; identical content updates the existing entry."""
    if kind not in {"transcript", "translation"} or not segments:
        return None
    digest = content_hash(segments)
    directory = _versions_dir(video_id)
    with _LOCK:
        index = _load_index(video_id)
        entry = next(
            (item for item in index if item.get("hash") == digest
             and item.get("kind") == kind and item.get("language") == language),
            None,
        )
        if entry is None:
            entry = {
                "id": f"{'t' if kind == 'transcript' else (language or 'x')}-{digest[:12]}",
                "kind": kind,
                "hash": digest,
                "text_hash": _text_hash(segments),
                "created_at": created_at or time.strftime("%Y-%m-%d %H:%M"),
            }
            index.append(entry)
        entry.update({
            "source": source,
            "language": language,
            "model": model or entry.get("model"),
            "segments": len(segments),
        })
        if based_on:
            entry["based_on"] = based_on
        if edited:
            entry["edited"] = True
        entry["label"] = _describe(entry)
        payload = {"transcript": segments}
        if metadata is not None:
            payload["metadata"] = metadata
        version_path = directory / f"{entry['id']}.json"
        stored = _read_json(version_path, {}) if version_path.is_file() else {}
        if chapters is not None:
            payload["chapters"] = chapters
        elif stored.get("chapters") is not None:
            payload["chapters"] = stored["chapters"]
        if stored.get("metadata") is not None and "metadata" not in payload:
            payload["metadata"] = stored["metadata"]
        if stored != payload:
            _write_json(version_path, payload)
        if _load_index(video_id) != index:
            _write_json(directory / "index.json", index)
        return dict(entry)


def find_transcript_label(video_id, segments):
    """Label of the stored transcript a translation was made from, if any."""
    digest = _text_hash(segments)
    for entry in _load_index(video_id):
        if entry.get("kind") == "transcript" and entry.get("text_hash") == digest:
            return entry.get("label")
    return None


def _file_time(path):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))


def sync_existing(video_id):
    """Capture the transcripts currently on disk (including ones made before versions existed)."""
    whisper = TRANSCRIPTS_DIR / f"{video_id}_whisper.json"
    if whisper.is_file():
        segments = _read_json(whisper, [])
        metadata = _read_json(TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json", {})
        if isinstance(segments, list) and segments:
            save_version(
                video_id, "transcript", segments, source="whisper",
                model=metadata.get("model") if isinstance(metadata, dict) else None,
                metadata=metadata if isinstance(metadata, dict) else None,
                created_at=(metadata or {}).get("generated_at") or _file_time(whisper),
            )
    uploaded = TRANSCRIPTS_DIR / f"{video_id}_uploaded.json"
    if uploaded.is_file():
        segments = _read_json(uploaded, [])
        if isinstance(segments, list) and segments:
            save_version(video_id, "transcript", segments, source="uploaded", created_at=_file_time(uploaded))
    for path in TRANSCRIPTS_DIR.glob(f"{video_id}_translated_*.json"):
        language = path.stem.rsplit("_", 1)[-1]
        segments = _read_json(path, [])
        if language in LANGUAGE_NAMES and isinstance(segments, list) and segments:
            chapters = _read_json(SUMMARIES_DIR / f"{video_id}_summary_{language}.json", None)
            save_version(
                video_id, "translation", segments, language=language,
                chapters=chapters if isinstance(chapters, list) else None,
                created_at=_file_time(path),
            )


def copy_speakers_to_translations(video_id, segments, speaker_names=None):
    """Give translated transcripts the speakers of the transcript they were translated from."""
    names = speaker_names or {}
    by_start = {}
    for item in segments:
        if isinstance(item, dict) and item.get("speaker"):
            by_start.setdefault(round(float(item.get("start", 0) or 0), 2), item["speaker"])
    if not by_start:
        return 0
    updated = 0
    for path in TRANSCRIPTS_DIR.glob(f"{video_id}_translated_*.json"):
        translated = _read_json(path, [])
        if not isinstance(translated, list) or not translated:
            continue
        same_length = len(translated) == len(segments)
        changed = False
        for index, item in enumerate(translated):
            if not isinstance(item, dict):
                continue
            speaker = by_start.get(round(float(item.get("start", 0) or 0), 2))
            if speaker is None and same_length and isinstance(segments[index], dict):
                speaker = segments[index].get("speaker")
            if speaker and item.get("speaker") != speaker:
                item["speaker"] = speaker
                changed = True
            if speaker and names.get(speaker) and item.get("speaker_name") != names[speaker]:
                item["speaker_name"] = names[speaker]
                changed = True
        if changed:
            _write_json(path, translated)
            updated += 1
    return updated


def list_versions(video_id):
    sync_existing(video_id)
    whisper = _read_json(TRANSCRIPTS_DIR / f"{video_id}_whisper.json", [])
    active = content_hash(whisper) if isinstance(whisper, list) and whisper else None
    versions = []
    for entry in sorted(_load_index(video_id), key=lambda item: item.get("created_at", ""), reverse=True):
        public = {key: value for key, value in entry.items() if key not in {"hash", "text_hash"}}
        public["label"] = _describe(entry)
        public["active_whisper"] = entry.get("kind") == "transcript" and entry.get("hash") == active
        versions.append(public)
    return versions


def _find(video_id, version_id):
    entry = next((item for item in _load_index(video_id) if item.get("id") == version_id), None)
    if entry is None:
        raise KeyError(version_id)
    return entry


def read_version(video_id, version_id):
    """Read a saved transcript without changing the active analysis."""
    entry = _find(video_id, version_id)
    path = _versions_dir(video_id) / f"{entry['id']}.json"
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return dict(entry), payload["transcript"]


def activate_version(video_id, version_id):
    """Make a stored version the current transcript used by summaries and exports."""
    entry = _find(video_id, version_id)
    payload = _read_json(_versions_dir(video_id) / f"{version_id}.json", {})
    segments = payload.get("transcript") or []
    if not segments:
        raise KeyError(version_id)
    if entry["kind"] == "translation":
        language = entry["language"]
        _write_json(TRANSCRIPTS_DIR / f"{video_id}_translated_{language}.json", segments)
        if payload.get("chapters"):
            _write_json(SUMMARIES_DIR / f"{video_id}_summary_{language}.json", payload["chapters"])
        else:
            # Chapters of another translation must not be shown or exported with this one.
            (SUMMARIES_DIR / f"{video_id}_summary_{language}.json").unlink(missing_ok=True)
        set_active_source(video_id, "translation", language=language)
    elif entry.get("source") == "uploaded":
        _write_json(TRANSCRIPTS_DIR / f"{video_id}_uploaded.json", segments)
        set_active_source(video_id, "transcript", source="uploaded")
    else:
        _write_json(TRANSCRIPTS_DIR / f"{video_id}_whisper.json", segments)
        if payload.get("metadata") is not None:
            _write_json(TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json", payload["metadata"])
        set_active_source(video_id, "transcript", source="whisper")
    if entry["kind"] == "transcript" and payload.get("chapters") is not None:
        _write_json(SUMMARIES_DIR / f"{video_id}.json", payload["chapters"])
    public = {key: value for key, value in entry.items() if key not in {"hash", "text_hash"}}
    public["label"] = _describe(entry)
    return {"version": public, "transcript": segments, "chapters": payload.get("chapters"),
            "metadata": payload.get("metadata")}


def delete_version(video_id, version_id):
    with _LOCK:
        index = _load_index(video_id)
        entry = next((item for item in index if item.get("id") == version_id), None)
        if entry is None:
            raise KeyError(version_id)
        if entry["kind"] == "translation":
            current = TRANSCRIPTS_DIR / f"{video_id}_translated_{entry.get('language')}.json"
        elif entry.get("source") == "uploaded":
            current = TRANSCRIPTS_DIR / f"{video_id}_uploaded.json"
        else:
            current = TRANSCRIPTS_DIR / f"{video_id}_whisper.json"
        if current.is_file() and content_hash(_read_json(current, [])) == entry.get("hash"):
            if entry["kind"] == "transcript" and entry.get("source") != "uploaded":
                raise ValueError("This is the current transcript. Choose another version before deleting it.")
            # Otherwise sync_existing would immediately store it again.
            current.unlink(missing_ok=True)
            if entry["kind"] == "translation":
                (SUMMARIES_DIR / f"{video_id}_summary_{entry.get('language')}.json").unlink(missing_ok=True)
                active = get_active_source(video_id)
                if active.get("kind") == "translation" and active.get("language") == entry.get("language"):
                    # The active translation was just deleted; fall back to the Whisper transcript.
                    set_active_source(video_id, "transcript", source="whisper")
        remaining = [item for item in index if item.get("id") != version_id]
        _write_json(_versions_dir(video_id) / "index.json", remaining)
        (_versions_dir(video_id) / f"{version_id}.json").unlink(missing_ok=True)
