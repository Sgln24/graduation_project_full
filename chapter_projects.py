"""Create independent, persistent video projects from saved chapter ranges."""
import asyncio
import copy
import json
import logging
import math
import re
import shutil
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from caption_exports import video_duration
from export_jobs import ExportJobStore, cleanup_export_workspace
from ffmpeg_tools import encode_export_clip, media_executable
from processors import transcript_versions
from project_paths import (
    EXPORTS_DIR, FULLSIZE_IMAGES_DIR, SCENES_DIR, SUMMARIES_DIR, THUMBNAILS_DIR,
    TRANSCRIPTS_DIR, VIDEO_DIR,
)
from slide_exports import slide_seconds


router = APIRouter()
logger = logging.getLogger(__name__)
jobs = ExportJobStore(EXPORTS_DIR / "chapter-project-jobs")
_metadata_lock = threading.Lock()
_tasks = set()

def save_video_metadata(video_id, entry):
    with _metadata_lock:
        path = VIDEO_DIR / "metadata.json"
        metadata = _read(path, {})
        metadata[video_id] = {**metadata.get(video_id, {}), **entry}
        transcript_versions._write_json(path, metadata)


def _read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def _seconds(value):
    parts = str(value).split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid saved chapter timestamp.")
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid saved chapter timestamp.")
    return seconds


def _stamp(value):
    total = int(value)
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def clip_transcript(transcript, start, end):
    """Keep overlapping cues with clipped durations and chapter-relative timing."""
    ordered = sorted(transcript, key=lambda item: item["start"])
    result = []
    for index, item in enumerate(ordered):
        cue_start = float(item["start"])
        following = float(ordered[index + 1]["start"]) if index + 1 < len(ordered) else end
        duration = item.get("duration")
        cue_end = cue_start + float(duration) if duration else following
        if cue_start >= end or cue_end <= start:
            continue
        clipped_start, clipped_end = max(start, cue_start), min(end, cue_end)
        if clipped_end <= clipped_start:
            continue
        cue = {**item, "start": round(clipped_start - start, 6),
               "duration": round(clipped_end - clipped_start, 6)}
        if "end" in item:
            cue["end"] = round(clipped_end - start, 6)
        if isinstance(item.get("words"), list):
            cue["words"] = [
                {**word, "start": max(0, float(word["start"]) - start),
                 "end": min(end, float(word["end"])) - start}
                for word in item["words"]
                if float(word["start"]) < end and float(word["end"]) > start
            ]
        result.append(cue)
    return result


def copy_slides(video_id, project_id, scenes, start, end):
    ordered = sorted(enumerate(scenes), key=lambda item: slide_seconds(item[1]))
    active = [item for item in ordered if slide_seconds(item[1]) <= start]
    selected = (active[-1:] if active else []) + [
        item for item in ordered if start < slide_seconds(item[1]) < end
    ]
    result = []
    for index, (old_index, original) in enumerate(selected):
        scene = copy.deepcopy(original)
        scene.pop("index", None)
        relative = max(0, slide_seconds(scene) - start)
        scene.update(time_seconds=relative, timestamp=_stamp(relative))
        for key, directory, route in (
            ("thumbnail", THUMBNAILS_DIR, "thumbnails"),
            ("fullsize", FULLSIZE_IMAGES_DIR, "fullsize_images"),
        ):
            source = directory / video_id / f"{old_index}.jpg"
            if not source.is_file():
                raise ValueError(f"Screenshot for slide {old_index + 1} is missing. Reload or detect slides again.")
            target = directory / project_id / f"{index}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            scene[key] = f"/{route}/{project_id}/{index}.jpg"
        result.append(scene)
    for index, scene in enumerate(result):
        following = result[index + 1]["time_seconds"] if index + 1 < len(result) else end - start
        scene["duration"] = following - scene["time_seconds"]
    return result


def _snapshot(video_id, data):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
        raise HTTPException(400, "Invalid video ID.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Chapter project settings must be an object.")
    index = data.get("chapter_index")
    language, source = data.get("language", ""), data.get("source", "whisper")
    if (type(index) is not int or index < 0 or not isinstance(language, str)
            or language not in ("", "en", "de", "ar", "pl")
            or not isinstance(source, str) or source not in ("whisper", "uploaded", "youtube")):
        raise HTTPException(400, "Invalid chapter, language or transcript source.")
    chapter_language = data.get("chapter_language", language)
    if chapter_language not in ("", "en", "de", "ar", "pl"):
        raise HTTPException(400, "Invalid chapter language.")
    chapters = _read(SUMMARIES_DIR / (f"{video_id}_summary_{chapter_language}.json" if chapter_language else f"{video_id}.json"), [])
    if not chapters and chapter_language:
        chapters = _read(SUMMARIES_DIR / f"{video_id}.json", [])
    if chapters and isinstance(chapters[0], list):
        chapters = [chapter for group in chapters for chapter in group]
    if not isinstance(chapters, list) or index >= len(chapters):
        raise HTTPException(409, "Chapters changed. Reload them before opening a chapter project.")
    chapter = chapters[index]
    start = _seconds(chapter["timestamp"])
    end = _seconds(chapters[index + 1]["timestamp"]) if index + 1 < len(chapters) else None
    if (data.get("timestamp") != chapter["timestamp"] or data.get("title") != chapter["title"]
            or data.get("next_timestamp") != (chapters[index + 1]["timestamp"] if end is not None else None)):
        raise HTTPException(409, "Chapter boundaries changed. Reload them before creating a project.")
    videos = [path for path in VIDEO_DIR.glob(f"{video_id}.*") if path.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".avi")]
    if not videos:
        raise HTTPException(404, "Source video not found.")
    transcript_path = TRANSCRIPTS_DIR / (
        f"{video_id}_translated_{language}.json" if language else f"{video_id}_{source}.json"
    )
    transcript = _read(transcript_path)
    if transcript is None and source == "youtube" and not language:
        transcript = _read(TRANSCRIPTS_DIR / f"{video_id}.json", [])
    transcript = transcript or []
    if not isinstance(transcript, list):
        raise ValueError("Saved transcript has an invalid format.")
    for item in transcript:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("Saved transcript has an invalid format.")
        for key in ("start", "duration"):
            value = item.get(key, 0) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("Saved transcript timing is invalid.")
    version_id = data.get("transcript_version_id", "")
    if not isinstance(version_id, str):
        raise HTTPException(400, "Invalid transcript version.")
    if version_id:
        entry, saved = transcript_versions.read_version(video_id, version_id)
        if (entry.get("language") or "") != language or transcript_versions.content_hash(saved) != transcript_versions.content_hash(transcript):
            raise HTTPException(409, "The selected transcript version changed. Reload it before creating a project.")
    scenes = _read(SCENES_DIR / f"{video_id}.json", [])
    if not isinstance(scenes, list) or any(not isinstance(scene, dict) for scene in scenes):
        raise ValueError("Saved slides have an invalid format.")
    metadata = _read(TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json", {})
    baseline = _read(TRANSCRIPTS_DIR / f"{video_id}_whisper.json", []) if language else transcript
    return {
        "video_id": video_id, "video_path": videos[0], "start": start, "end": end,
        "title": chapter["title"], "transcript": transcript, "language": language,
        "source": source, "scenes": scenes, "metadata": metadata, "baseline": baseline or transcript,
    }


async def create_project(snapshot, project_id, step):
    video_id = snapshot["video_id"]
    start = snapshot["start"]
    duration = await video_duration(snapshot["video_path"])
    end = snapshot["end"] if snapshot["end"] is not None else duration
    if not 0 <= start < end <= duration + 0.1:
        raise ValueError("Chapter timestamps are outside the source video's duration.")
    end = min(end, duration)
    video_path = VIDEO_DIR / f"{project_id}.pending"
    published_path = VIDEO_DIR / f"{project_id}.mp4"
    committed = False
    try:
        command = [
            media_executable("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(start), "-i", str(snapshot["video_path"]), "-t", str(end - start),
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", "-f", "mp4", str(video_path),
        ]
        step("Creating an independent chapter video")
        await encode_export_clip(command, step, "Chapter project", end - start)
        step("Copying transcript, speakers and available slides")
        transcript = clip_transcript(snapshot["transcript"], start, end)
        baseline = clip_transcript(snapshot["baseline"], start, end)
        scenes = await asyncio.to_thread(
            copy_slides, video_id, project_id, snapshot["scenes"], start, end,
        )
        provenance = {
            "parent_video_id": video_id, "start": start, "end": end,
            "title": snapshot["title"], "source": snapshot["source"], "language": snapshot["language"],
        }
        if baseline or transcript:
            baseline = baseline or transcript
            metadata = {**snapshot["metadata"], "chapter_project": provenance}
            transcript_versions._write_json(TRANSCRIPTS_DIR / f"{project_id}_whisper.json", baseline)
            transcript_versions._write_json(TRANSCRIPTS_DIR / f"{project_id}_whisper_meta.json", metadata)
            transcript_versions.save_version(
                project_id, "transcript", baseline, source="whisper",
                model=metadata.get("model") or "chapter:excerpt", metadata=metadata,
            )
            language = snapshot["language"]
            if language and transcript:
                transcript_versions._write_json(TRANSCRIPTS_DIR / f"{project_id}_translated_{language}.json", transcript)
                transcript_versions.save_version(project_id, "translation", transcript, language=language)
                transcript_versions.set_active_source(project_id, "translation", language=language)
            else:
                transcript_versions.set_active_source(project_id, "transcript", source="whisper")
        transcript_versions._write_json(SCENES_DIR / f"{project_id}.json", scenes)
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", snapshot["title"]).strip(" .")[:100] or "Chapter"
        filename += ".mp4"
        video_path.replace(published_path)
        save_video_metadata(project_id, {"filename": filename, "chapter_project": provenance})
        committed = True
        return {"video_id": project_id, "filename": filename, "duration": end - start,
                "transcript_segments": len(transcript), "slides": len(scenes)}
    finally:
        if not committed:
            video_path.unlink(missing_ok=True)
            published_path.unlink(missing_ok=True)
            for path in TRANSCRIPTS_DIR.glob(f"{project_id}_*.json"):
                path.unlink()
            (SCENES_DIR / f"{project_id}.json").unlink(missing_ok=True)
            for directory in (TRANSCRIPTS_DIR / "versions", THUMBNAILS_DIR, FULLSIZE_IMAGES_DIR):
                target = directory / project_id
                if target.is_dir():
                    cleanup_export_workspace(target)


@router.post("/chapter_projects/{video_id}")
async def start_chapter_project(video_id: str, request: Request):
    try:
        snapshot = _snapshot(video_id, await request.json())
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, f"Could not read chapter project content: {error}") from error
    job_id, project_id = uuid.uuid4().hex, uuid.uuid4().hex
    state = {
        "id": job_id, "video_id": video_id, "instance": jobs.instance, "status": "queued",
        "created_at": time.time(), "steps": [], "title": snapshot["title"],
    }

    def step(message):
        state["steps"].append({"elapsed": round(time.time() - state["created_at"], 1), "message": str(message)})
        jobs._write(job_id, state)

    async def work():
        try:
            state["status"] = "running"
            step("Chapter project queued for encoding")
            state["project"] = await create_project(snapshot, project_id, step)
            state["status"] = "complete"
            step("Chapter project ready")
        except Exception as error:
            logger.exception("Chapter project creation failed")
            state["status"], state["error"] = "error", str(error)
        finally:
            state["finished_at"] = time.time()
            jobs._write(job_id, state)

    jobs._write(job_id, state)
    task = asyncio.create_task(work())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job_id": job_id}


@router.get("/chapter_project_jobs/{job_id}")
def chapter_project_status(job_id: str):
    try:
        state = jobs.read(job_id)
        if state["status"] == "error" and state.get("instance") != jobs.instance:
            state["error"] = "Chapter project creation was interrupted by a server restart. Create it again."
        return state
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "Chapter project job not found.") from error
