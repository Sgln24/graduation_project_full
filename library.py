"""Explicit deletion of saved uploads and exports, scoped to validated IDs."""
import json
import re
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException
import chapter_projects
from processors import transcript_versions
from project_paths import (
    VIDEO_DIR, TRANSCRIPTS_DIR, SCENES_DIR, SUMMARIES_DIR, THUMBNAILS_DIR,
    FULLSIZE_IMAGES_DIR, DETECTIONS_DIR, OCR_RESULTS_DIR,
)

router = APIRouter()
stores = {}
busy = lambda video_id: False


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise HTTPException(400, "Invalid video ID.")
    return value


def _remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        def retry_readonly(function, target, error):
            if not isinstance(error[1], PermissionError):
                raise error[1]
            Path(target).chmod(stat.S_IWRITE)
            function(target)
        shutil.rmtree(path, onerror=retry_readonly)


def _jobs(video_id=None):
    return [(kind, job) for kind, store in stores.items() for job in store.list(video_id)]


@router.get("/library")
def saved_library():
    try:
        metadata_path = VIDEO_DIR / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
        videos = [
            {"video_id": path.stem, "filename": metadata.get(path.stem, {}).get("filename", path.name)}
            for path in VIDEO_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi"}
        ]
        exports = [
            {"id": job["id"], "store": kind, "video_id": job["video_id"],
             "title": job["title"], "status": job["status"]}
            for kind, job in _jobs() if kind != "projects"
        ]
        return {"videos": videos, "exports": exports}
    except (OSError, ValueError) as error:
        raise HTTPException(500, f"Could not read the saved library: {error}") from error


@router.delete("/library/exports/{kind}/{job_id}")
def delete_export(kind: str, job_id: str, confirm: bool = False):
    if not confirm:
        raise HTTPException(400, "Confirm deletion first.")
    if kind not in stores or kind == "projects":
        raise HTTPException(400, "Invalid export type.")
    try:
        store = stores[kind]
        job = store.read(job_id)
        if job["status"] in {"running", "queued"}:
            raise HTTPException(409, "This export is still running. Wait for it to finish.")
        _remove(store._directory(job_id))
        return {"success": True}
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, "Export not found.") from error
    except OSError as error:
        raise HTTPException(500, f"Could not delete the export: {error}. Close any open downloads and retry.") from error


@router.delete("/library/videos/{video_id}")
def delete_upload(video_id: str, include_exports: bool = False, confirm: bool = False):
    _id(video_id)
    if not confirm:
        raise HTTPException(400, "Confirm deletion first.")
    try:
        jobs = _jobs(video_id)
        if busy(video_id) or any(job["status"] in {"running", "queued"} for _, job in jobs):
            raise HTTPException(409, "This video still has running work. Stop it or wait before deleting.")
        if (TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt").exists():
            raise HTTPException(409, "Transcription is still marked as running. Finish or stop it before deleting.")
        files = []
        for root in (VIDEO_DIR, TRANSCRIPTS_DIR, SCENES_DIR, SUMMARIES_DIR, DETECTIONS_DIR, OCR_RESULTS_DIR):
            if root.is_dir():
                files.extend(path for path in root.iterdir() if path.is_file()
                             and (path.stem == video_id or path.name.startswith(video_id + "_")))
        directories = [
            TRANSCRIPTS_DIR / "versions" / video_id, SUMMARIES_DIR / "overviews" / video_id,
            THUMBNAILS_DIR / video_id, FULLSIZE_IMAGES_DIR / video_id,
            DETECTIONS_DIR / video_id, OCR_RESULTS_DIR / video_id,
        ]
        metadata_path = VIDEO_DIR / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
        if not files and not any(path.exists() for path in directories) and video_id not in metadata:
            raise HTTPException(404, "Saved upload not found.")
        for path in files + directories:
            _remove(path)
        if include_exports:
            for kind, job in jobs:
                _remove(stores[kind]._directory(job["id"]))
        with chapter_projects._metadata_lock:
            metadata_path = VIDEO_DIR / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            metadata.pop(video_id, None)
            transcript_versions._write_json(metadata_path, metadata)
        return {"success": True, "exports_kept": not include_exports}
    except (OSError, ValueError) as error:
        raise HTTPException(500, f"Could not finish deleting this upload: {error}. Close open files and retry.") from error
