from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks, UploadFile, File, APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import json
import hashlib
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import glob
import subprocess
import tempfile
import zipfile
import shutil
import traceback
import yt_dlp
from docx import Document
from docx.shared import Inches
from docx.image.exceptions import UnrecognizedImageError
from docx.oxml import OxmlElement
from reportlab.lib.pagesizes import letter
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as PdfImage, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
from youtube_transcript_api import YouTubeTranscriptApi
import re
import time
import html
from collections import Counter
from PIL import Image as PillowImage, UnidentifiedImageError
from dotenv import load_dotenv
from export_jobs import ExportJobStore, export_workspace, cleanup_export_workspace
from starlette.background import BackgroundTask
from ffmpeg_tools import media_executable
from caption_exports import validate_captions, render_captioned_video, video_duration, caption_text, caption_filename, validate_caption_style, render_caption_preview

from project_paths import STATIC_DIR, VIDEO_DIR, TRANSCRIPTS_DIR, SCENES_DIR, THUMBNAILS_DIR, FULLSIZE_IMAGES_DIR, SUMMARIES_DIR, EXPORTS_DIR, DETECTIONS_DIR, OCR_RESULTS_DIR, ensure_app_directories

# Import processing modules
from processors.video_processor import VideoProcessor
from processors.scene_processor import SceneProcessor
from processors.ocr_processor import OCRProcessor
from processors.transcript_processor import TranscriptProcessor, get_huggingface_token
from cloud_transcribe import cloud_provider_status, recommended_transcription_model
from transcribe import get_whisper_device_config
from processors.embedding_processor import EmbeddingProcessor
from processors.summary_processor import SummaryProcessor
from processors import transcript_versions

load_dotenv()

# Initialize FastAPI app
@asynccontextmanager
async def lifespan(app):
    transcript_processor.restore_interrupted_speaker_jobs()
    yield


app = FastAPI(lifespan=lifespan)

ensure_app_directories()

@app.middleware("http")
async def disable_frontend_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

@app.get("/yolo_status")
async def yolo_status():
    """Return the current availability of the YOLO model."""
    try:
        OCRProcessor.initialize_models()

        loaded = OCRProcessor.yolo_model is not None

        return JSONResponse({
            "loaded": loaded,
            "error": None if loaded else "YOLOv8 model not loaded"
        })

    except Exception as e:
        return JSONResponse({
            "loaded": False,
            "error": str(e)
        })

@app.get("/runtime_status")
async def runtime_status():
    """Report the compute device used by Whisper and available CUDA GPU."""
    summary_processor._refresh_clients()
    default_chain = summary_processor._provider_chain(prefer_groq=True)
    translation_status = {
        "translation_default_provider": (
            {"gemini": "Gemini", "groq": "Groq", "openai": "OpenAI"}[default_chain[0][0]]
            if default_chain else None
        ),
        "translation_default_model": default_chain[0][1] if default_chain else None,
        "ai_fallback_chain": [f"{provider}:{model}" for provider, model in default_chain],
        "gemini_configured": bool(summary_processor.model),
        "openai_configured": bool(summary_processor.openai_client),
        "groq_configured": bool(summary_processor.groq_client),
        "huggingface_token_configured": bool(get_huggingface_token()),
        "transcription_providers": cloud_provider_status(),
    }
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
        device_warnings = []
        whisper_device, whisper_compute_type = get_whisper_device_config(
            on_step=device_warnings.append
        )
        return JSONResponse({
            "cuda_available": cuda_available,
            "gpu_name": gpu_name,
            "cuda_version": torch.version.cuda,
            "whisper_device": whisper_device,
            "whisper_compute_type": whisper_compute_type,
            "whisper_device_warning": device_warnings[0] if device_warnings else None,
            "colab_gpu": os.getenv("COLAB_GPU") or None,
            "recommended_transcription_model": recommended_transcription_model(whisper_device == "cuda"),
            **translation_status,
        })
    except Exception as error:
        return JSONResponse({
            "recommended_transcription_model": recommended_transcription_model(False),
            "cuda_available": False,
            "gpu_name": None,
            "cuda_version": None,
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "colab_gpu": os.getenv("COLAB_GPU") or None,
            "error": str(error),
            **translation_status,
        })

# Mount static directory
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

# Initialize processors
video_processor = VideoProcessor()
scene_processor = SceneProcessor()
transcript_processor = TranscriptProcessor()
embedding_processor = EmbeddingProcessor()
summary_processor = SummaryProcessor()

def parse_uploaded_transcript(text, suffix):
    if suffix == ".json":
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("segments", [])
    lines = text.replace("\r\n", "\n").split("\n")
    result = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" in line:
            start = line.split("-->")[0].strip().replace(",", ".")
            parts = start.split(":")
            seconds = sum(float(value) * (60 ** position) for position, value in enumerate(reversed(parts)))
            index += 1
            text_lines = []
            while index < len(lines) and lines[index].strip():
                text_lines.append(lines[index].strip())
                index += 1
            if text_lines:
                result.append({"start": seconds, "duration": 0, "text": " ".join(text_lines)})
        elif line and not line.isdigit():
            result.append({"start": result[-1]["start"] + 1 if result else 0, "duration": 0, "text": line})
        index += 1
    return result

def detect_transcript_language(transcript):
    sample = " ".join(item.get("text", "") for item in transcript[:30])
    if re.search(r"[\u0600-\u06ff]", sample):
        return "ar"
    words = set(re.findall(r"[A-Za-zÀ-ÿ]+", sample.lower()))
    if words & {"der", "die", "das", "und", "ist"}:
        return "de"
    if words & {"jest", "nie", "oraz", "dla"}:
        return "pl"
    return "en"

def build_outline_points(transcript, max_points=4):
    """Select concise, timestamped key points for the Word outline."""
    candidates = []
    for item in transcript:
        text = " ".join(str(item.get("text", "")).split())
        if not text:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            words = re.findall(r"[A-Za-zÀ-ÿ\u0600-\u06ff]{3,}", sentence.lower())
            if sentence and words:
                title_words = sentence.split()[:6]
                candidates.append({
                    "start": float(item.get("start", 0)),
                    "text": sentence,
                    "title": " ".join(title_words).rstrip(".,;:!?") or "Key point",
                    "words": words,
                })
    if not candidates:
        return []
    frequencies = Counter(
        word for candidate in candidates for word in candidate["words"]
    )
    for candidate in candidates:
        candidate["score"] = sum(
            frequencies[word] for word in set(candidate["words"])
        ) / max(1, len(candidate["words"]))
    selected = sorted(
        candidates,
        key=lambda candidate: (-candidate["score"], candidate["start"]),
    )[:max_points]
    return sorted(selected, key=lambda candidate: candidate["start"])

@app.post("/upload_transcript/{video_id}")
async def upload_transcript(video_id: str, transcript: UploadFile = File(...)):
    text = (await transcript.read()).decode("utf-8-sig")
    transcript_data = parse_uploaded_transcript(text, Path(transcript.filename or "").suffix.lower())
    if not transcript_data:
        raise HTTPException(status_code=400, detail="No transcript segments found")
    transcript_versions.sync_existing(video_id)
    output_path = TRANSCRIPTS_DIR / f"{video_id}_uploaded.json"
    output_path.write_text(json.dumps(transcript_data, ensure_ascii=False), encoding="utf-8")
    transcript_versions.save_version(video_id, "transcript", transcript_data, source="uploaded")
    return {"success": True, "transcript": transcript_data, "source_language": detect_transcript_language(transcript_data)}

translation_progress = {}


@app.get("/translation_progress/{video_id}/{target}")
async def get_translation_progress(video_id: str, target: str):
    return translation_progress.get(f"{video_id}:{target}") or {"status": "idle"}


@app.post("/translate_transcript/{video_id}")
async def translate_transcript(video_id: str, request: Request):
    data = await request.json()
    transcript = data.get("transcript", [])
    target = data.get("target_language")
    requested_model = data.get("model") or None
    if target not in {"de", "en", "ar", "pl"}:
        raise HTTPException(status_code=400, detail="Unsupported translation language")
    if requested_model not in {
        None, "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
        "gemini-2.5-flash", "gemini-2.5-flash-lite",
        "groq:openai/gpt-oss-120b", "gpt-4.1-mini",
    }:
        raise HTTPException(status_code=400, detail="Unsupported translation model")
    if (
        not isinstance(transcript, list)
        or not transcript
        or any(not isinstance(item, dict) for item in transcript)
    ):
        raise HTTPException(status_code=400, detail="No transcript segments were provided")

    chapters = []
    summary_path = SUMMARIES_DIR / f"{video_id}.json"
    if summary_path.is_file():
        try:
            chapters = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=500,
                detail=f"Could not read chapter summary: {error}",
            )
        if not isinstance(chapters, list):
            chapters = []
        if chapters and isinstance(chapters[0], list):
            chapters = [
                chapter
                for chapter_group in chapters
                if isinstance(chapter_group, list)
                for chapter in chapter_group
            ]
    chapter_fields = []
    for chapter_index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        for field in ("title", "summary"):
            if str(chapter.get(field) or "").strip():
                chapter_fields.append((chapter_index, None, field, str(chapter[field])))
        for section_index, section in enumerate(chapter.get("sections") or []):
            if not isinstance(section, dict):
                continue
            for field in ("title", "point"):
                if str(section.get(field) or "").strip():
                    chapter_fields.append((chapter_index, section_index, field, str(section[field])))
    chapter_segments = [
        {"start": index, "duration": 0, "text": text}
        for index, (_, _, _, text) in enumerate(chapter_fields)
    ]
    progress_key = f"{video_id}:{target}"
    translation_progress[progress_key] = {
        "status": "running",
        "completed_segments": 0,
        "total_segments": len(transcript) + len(chapter_segments),
        "message": "Starting translation...",
        "updated_at": time.time(),
    }

    def on_progress(update):
        translation_progress[progress_key] = {
            **update, "status": "running", "updated_at": time.time(),
        }

    try:
        translation = await summary_processor.translate_transcript(
            [*transcript, *chapter_segments], target, requested_model, on_progress=on_progress
        )
    except (RuntimeError, ValueError, asyncio.TimeoutError, json.JSONDecodeError) as error:
        translation_progress[progress_key] = {
            "status": "error", "message": str(error), "updated_at": time.time(),
        }
        raise HTTPException(status_code=502, detail=str(error)) from error
    except Exception as error:
        translation_progress[progress_key] = {
            "status": "error", "message": str(error), "updated_at": time.time(),
        }
        raise
    translation_progress[progress_key] = {
        "status": "complete",
        "completed_segments": len(translation["transcript"]),
        "total_segments": len(translation["transcript"]),
        "message": f"Translated with {translation['provider']} {translation['model']}",
        "elapsed_seconds": translation["elapsed_seconds"],
        "updated_at": time.time(),
    }
    translated = translation["transcript"][:len(transcript)]
    translated_title_segments = translation["transcript"][len(transcript):]
    transcript_versions.sync_existing(video_id)
    (TRANSCRIPTS_DIR / f"{video_id}_translated_{target}.json").write_text(
        json.dumps(translated, ensure_ascii=False), encoding="utf-8"
    )
    text_by_index = {
        int(item.get("start", index)): str(item.get("text", "")).strip()
        for index, item in enumerate(translated_title_segments)
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    }
    copies = json.loads(json.dumps(chapters, ensure_ascii=False))
    for index, (chapter_index, section_index, field, _) in enumerate(chapter_fields):
        if index in text_by_index:
            target_item = copies[chapter_index] if section_index is None else copies[chapter_index]["sections"][section_index]
            target_item[field] = text_by_index[index]
    translated_chapters = [chapter for chapter in copies if isinstance(chapter, dict)]
    if translated_chapters:
        (SUMMARIES_DIR / f"{video_id}_summary_{target}.json").write_text(
            json.dumps(translated_chapters, ensure_ascii=False), encoding="utf-8"
        )
    transcript_versions.save_version(
        video_id, "translation", translated, language=target,
        model=translation["model"], chapters=translated_chapters,
        based_on=transcript_versions.find_transcript_label(video_id, transcript),
    )
    # Keep showing this translation (transcript, timeline, and scene titles) after a reload.
    transcript_versions.set_active_source(video_id, "translation", language=target)
    return {
        "success": True,
        "transcript": translated,
        "chapters": translated_chapters,
        "language": target,
        "provider": translation["provider"],
        "model": translation["model"],
        "batch_count": translation["batch_count"],
        "elapsed_seconds": translation["elapsed_seconds"],
    }

@app.get("/transcript_versions/{video_id}")
async def get_transcript_versions(video_id: str):
    try:
        return {"versions": await asyncio.to_thread(transcript_versions.list_versions, video_id)}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/transcript_versions/{video_id}/{version_id}/activate")
async def activate_transcript_version(video_id: str, version_id: str):
    try:
        return await asyncio.to_thread(transcript_versions.activate_version, video_id, version_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Transcript version not found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/transcript_versions/{video_id}/{version_id}")
async def delete_transcript_version(video_id: str, version_id: str):
    try:
        await asyncio.to_thread(transcript_versions.delete_version, video_id, version_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Transcript version not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"success": True}

# Create thread pool for background tasks
executor = ThreadPoolExecutor(max_workers=2)

# Dictionary to store SSE clients by video_id
sse_clients = {}

async def send_sse_update(video_id, event_data):
    """Send an SSE update to all clients for a specific video.

    OCR runs in worker threads, so each queue is fed through its own event loop.
    """
    data_str = json.dumps(event_data)
    for queue, loop in list(sse_clients.get(video_id, [])):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, data_str)
        except Exception as e:
            print(f"Error sending SSE update: {str(e)}")

# Initialize OCR processor with SSE update function
ocr_processor = OCRProcessor(send_sse_update=send_sse_update)

# Routes

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    response = templates.TemplateResponse(
    request=request,
    name="index.html"
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.post("/upload_video")
async def upload_video(background_tasks: BackgroundTasks, video: UploadFile = File(...)):
    temporary_path = None
    try:
        # Hash and persist the upload in chunks to avoid holding large videos in RAM.
        hasher = hashlib.sha256()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(VIDEO_DIR),
            prefix=".upload-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                temporary_file.write(chunk)

        video_hash = hasher.hexdigest()
        video_path = str(VIDEO_DIR / f"{video_hash}.mp4")
        metadata_path = VIDEO_DIR / "metadata.json"
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
        metadata[video_hash] = {"filename": video.filename or f"{video_hash}.mp4"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        # Check if video exists
        if os.path.exists(video_path):
            print("Video exists: ", os.path.exists(video_path))
            return await video_processor.handle_existing_video(video_hash, video_path, background_tasks)

        # Move the completed upload into its content-addressed location.
        os.replace(str(temporary_path), video_path)
        temporary_path = None

        return await video_processor.process_new_video(video_hash, video_path, background_tasks)

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

@app.get("/uploaded_videos")
async def uploaded_videos():
    metadata_path = VIDEO_DIR / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        metadata = {}
    videos = []
    for video_path in sorted(VIDEO_DIR.glob("*"), key=lambda path: path.stat().st_mtime, reverse=True):
        if (
            not video_path.is_file()
            or video_path.name == "metadata.json"
            or video_path.name.endswith("_video_quality.json")
        ):
            continue
        videos.append({
            "video_id": video_path.stem,
            "filename": metadata.get(video_path.stem, {}).get("filename", video_path.name),
            "size_bytes": video_path.stat().st_size,
            "modified_at": video_path.stat().st_mtime
        })
    return {"success": True, "videos": videos}

@app.get("/uploaded_videos/{video_id}")
async def load_uploaded_video(video_id: str, background_tasks: BackgroundTasks):
    video_path = video_processor.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Uploaded video not found")
    return await video_processor.handle_existing_video(video_id, video_path, background_tasks)

@app.get("/video/{video_id}")
async def stream_video(video_id: str, range: Optional[str] = Header(None)):
    try:
        video_path = video_processor.get_video_path(video_id)
        if not video_path:
            raise HTTPException(status_code=404, detail="Video not found")
        return await video_processor.stream_video(video_path, range)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/scenes/{video_id}")
async def get_scenes(video_id: str):
    return await scene_processor.get_scenes(video_id)

@app.post("/detect_scenes/{video_id}")
async def detect_scenes(video_id: str, request: Request, background_tasks: BackgroundTasks):
    video_path = video_processor.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    data = await request.json()
    mode = data.get("mode", "content")
    try:
        options = {
            "adaptive_detail": str(data.get("adaptive_detail", "balanced")),
            "content_threshold": float(data.get("content_threshold", 27)),
            "minimum_slide_duration": float(data.get("minimum_slide_duration", 10)),
            "minimum_slide_duration_percent": (
                float(data["minimum_slide_duration_percent"])
                if data.get("minimum_slide_duration_percent") is not None else None
            ),
            "maximum_slides_per_hour": int(data.get("maximum_slides_per_hour", 60)),
            "merge_similar_slides": bool(data.get("merge_similar_slides", True)),
            "include_chapter_boundaries": bool(data.get("include_chapter_boundaries", True)),
            "screenshot_height": int(data.get("screenshot_height", 720)),
        }
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid scene detection options")
    if mode not in {"adaptive", "content", "chapters"}:
        raise HTTPException(status_code=400, detail="Invalid scene detection mode")
    if options["adaptive_detail"] not in {"fewer", "balanced", "more"}:
        raise HTTPException(status_code=400, detail="Invalid adaptive detail level")
    if not 10 <= options["content_threshold"] <= 60:
        raise HTTPException(status_code=400, detail="Content-cut threshold must be between 10 and 60")
    if options["minimum_slide_duration_percent"] is not None:
        if options["minimum_slide_duration_percent"] not in {0.25, 0.5, 1, 2}:
            raise HTTPException(status_code=400, detail="Invalid minimum slide duration percentage")
    elif options["minimum_slide_duration"] not in {5, 10, 20, 30}:
        raise HTTPException(status_code=400, detail="Invalid minimum slide duration")
    if options["maximum_slides_per_hour"] not in {0, 40, 60, 90}:
        raise HTTPException(status_code=400, detail="Invalid maximum slides per hour")
    if options["screenshot_height"] not in {480, 720, 1080}:
        raise HTTPException(status_code=400, detail="Invalid screenshot quality")
    print(f"Starting scene detection for {video_id}: mode={mode}, options={options}")
    await scene_processor.start_scene_detection(video_id, video_path, background_tasks, mode, options)
    return JSONResponse({"success": True, "message": "Scene detection started"})

@app.get("/scene_detections/{video_id}/{scene_index}")
async def get_scene_detections(video_id: str, scene_index: int):
    return await scene_processor.get_scene_detections(video_id, scene_index)

@app.get("/thumbnails/{video_id}/{filename}")
async def get_thumbnail(video_id: str, filename: str):
    base_dir = THUMBNAILS_DIR.resolve()
    thumbnail_path = (base_dir / video_id / filename).resolve()
    if not thumbnail_path.is_relative_to(base_dir) or not thumbnail_path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(thumbnail_path)

@app.get("/fullsize_images/{video_id}/{filename}")
async def get_fullsize_image(video_id: str, filename: str):
    base_dir = FULLSIZE_IMAGES_DIR.resolve()
    image_path = (base_dir / video_id / filename).resolve()
    if not image_path.is_relative_to(base_dir) or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(image_path)

@app.get("/download_scene_screenshots/{video_id}")
async def download_scene_screenshots(video_id: str):
    scene_path = SCENES_DIR / f"{video_id}.json"
    if not scene_path.is_file():
        raise HTTPException(status_code=404, detail="Run slide detection before downloading screenshots")

    try:
        scenes = json.loads(scene_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"Could not read detected slides: {error}")
    if not isinstance(scenes, list) or not scenes:
        raise HTTPException(status_code=400, detail="No detected slide screenshots are available")

    image_dir = FULLSIZE_IMAGES_DIR / video_id
    with tempfile.TemporaryDirectory(dir=EXPORTS_DIR) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        manifest = []
        image_count = 0
        for index, scene in enumerate(scenes):
            source = image_dir / f"{index}.jpg"
            if not source.is_file():
                continue
            filename = f"slide_{index + 1:03d}_{scene.get('timestamp', 'unknown').replace(':', '-')}.jpg"
            (temp_dir / filename).write_bytes(source.read_bytes())
            manifest.append({
                "slide": index + 1,
                "timestamp": scene.get("timestamp"),
                "time_seconds": scene.get("time_seconds"),
                "filename": filename
            })
            image_count += 1

        if not image_count:
            raise HTTPException(status_code=400, detail="Detected slides have no generated screenshots")

        (temp_dir / "screenshots.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        zip_path = EXPORTS_DIR / f"{video_id}_slide_screenshots.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file_path in temp_dir.iterdir():
                archive.write(file_path, file_path.name)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{video_id}_slide_screenshots.zip"
    )

# Slide downloads are independent of chapter clip/transcript export jobs.
@app.get("/download_slide/{video_id}/{scene_index}")
async def download_slide(video_id: str, scene_index: int):
    from slide_exports import SlideExportError, load_download_slides, slide_image_path, slide_filename
    try:
        scenes = load_download_slides(video_id, SCENES_DIR)
        source = slide_image_path(video_id, scene_index, scenes, FULLSIZE_IMAGES_DIR)
        filename = slide_filename(scene_index, scenes[scene_index])
    except SlideExportError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    return FileResponse(source, media_type="image/jpeg", filename=filename)


@app.post("/download_slides/{video_id}")
async def download_selected_slides(video_id: str, request: Request):
    from slide_exports import SlideExportError, build_slide_archive
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise SlideExportError(400, "Invalid slide selection")
        archive = await asyncio.to_thread(
            build_slide_archive, video_id, data.get("indices"), SCENES_DIR, FULLSIZE_IMAGES_DIR,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid slide selection") from error
    except SlideExportError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    return StreamingResponse(
        iter([archive.getvalue()]), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{video_id}_slides.zip"'},
    )


@app.get("/download_slide_text_docx/{video_id}")
async def download_slide_text_docx(video_id: str, transcript_language: str = ""):
    from slide_exports import SlideExportError, build_slide_text_document
    try:
        document = await asyncio.to_thread(
            build_slide_text_document, video_id, SCENES_DIR, FULLSIZE_IMAGES_DIR, SUMMARIES_DIR,
            transcript_language=transcript_language,
        )
    except SlideExportError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error
    return StreamingResponse(
        iter([document.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{video_id}_slide_text.docx"'},
    )


@app.get("/ocr_text/{video_id}")
async def get_ocr_text(video_id: str):
    return await ocr_processor.get_ocr_text(video_id)


def build_slides_pdf(video_id, destination):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader

    image_dir = (FULLSIZE_IMAGES_DIR / video_id).resolve()
    if image_dir.parent != FULLSIZE_IMAGES_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid video ID")
    scene_path = SCENES_DIR / f"{video_id}.json"
    if not scene_path.is_file():
        raise HTTPException(status_code=404, detail="Detect slides before exporting a slides PDF")
    scenes = json.loads(scene_path.read_text(encoding="utf-8"))
    if not isinstance(scenes, list) or not scenes:
        raise HTTPException(status_code=400, detail="No detected slides are available")
    images = [image_dir / f"{index}.jpg" for index in range(len(scenes))]
    missing = [index + 1 for index, image in enumerate(images) if not image.is_file()]
    if missing:
        raise HTTPException(status_code=409, detail=f"Missing screenshots for slides {missing}. Run slide detection again.")
    pdf = Canvas(str(destination))
    pdf.setTitle("Detected slides")
    for image in images:
        reader = ImageReader(str(image))
        width, height = reader.getSize()
        pdf.setPageSize((width, height))
        pdf.drawImage(str(image), 0, 0, width=width, height=height)
        pdf.showPage()
    pdf.save()


@app.get("/download_slides_pdf/{video_id}")
async def download_slides_pdf(video_id: str):
    workspace = Path(tempfile.mkdtemp(prefix="lecture-slides-pdf-"))
    try:
        destination = workspace / "slides.pdf"
        await asyncio.to_thread(build_slides_pdf, video_id, destination)
    except HTTPException:
        cleanup_export_workspace(workspace)
        raise
    except (OSError, ValueError, UnidentifiedImageError) as error:
        cleanup_export_workspace(workspace)
        raise HTTPException(status_code=500, detail=f"Could not create slides PDF: {error}")
    return FileResponse(
        destination, media_type="application/pdf", filename="slides.pdf",
        background=BackgroundTask(cleanup_export_workspace, workspace),
    )

@app.post("/stop_ocr/{video_id}")
async def stop_ocr(video_id: str):
    return await ocr_processor.stop_ocr(video_id)

@app.post("/start_ocr/{video_id}")
async def start_ocr(video_id: str):
    return await ocr_processor.start_ocr(video_id)

@app.post("/process_surya_ocr/{video_id}")
async def process_surya_ocr(video_id: str):
    return await ocr_processor.process_surya_ocr(video_id)

@app.post("/set_ocr_preference")
async def set_ocr_preference(request: Request):
    data = await request.json()
    return await ocr_processor.set_preference(data.get("preference", "tesseract"))

@app.get("/get_ocr_preference")
def get_ocr_preference():
    return {"preference": ocr_processor.get_preference()}

@app.post("/set_transcript_preference")
async def set_transcript_preference(request: Request):
    data = await request.json()
    return await transcript_processor.set_preference(
        data.get("preference", "youtube")
    )

@app.get("/get_transcript_preference")
def get_transcript_preference():
    return {"preference": transcript_processor.get_preference()}

@app.get("/get_transcript/{video_id}/{source}")
async def get_transcript(video_id: str, source: str):
    return await transcript_processor.get_transcript(video_id, source)

@app.post("/generate_whisper_transcript/{video_id}")
async def generate_whisper_transcript(video_id: str, request: Request, background_tasks: BackgroundTasks):
    try:
        options = await request.json()
    except json.JSONDecodeError:
        options = {}
    return await transcript_processor.generate_whisper_transcript(
        video_id,
        background_tasks,
        bool(options.get("diarization", False)),
        bool(options.get("force", False)),
        str(options.get("model") or "") or None,
        str(options.get("prompt") or "").strip() or None,
        speakers_only=bool(options.get("speakers_only", False)),
    )

@app.get("/whisper_transcript_status/{video_id}")
async def whisper_transcript_status(video_id: str):
    return await transcript_processor.get_whisper_status(video_id)

@app.post("/cancel_whisper_transcript/{video_id}")
async def cancel_whisper_transcript(video_id: str):
    return await transcript_processor.cancel_whisper_generation(video_id)

@app.get("/transcript_metadata/{video_id}")
async def transcript_metadata(video_id: str):
    return {"metadata": transcript_processor._public_whisper_metadata(video_id)}

@app.post("/speaker_names/{video_id}")
async def speaker_names(video_id: str, request: Request):
    data = await request.json()
    names = data.get("names")
    if not isinstance(names, dict):
        raise HTTPException(status_code=400, detail="Speaker names must be an object")
    return await transcript_processor.save_speaker_names(video_id, names)

@app.post("/compute_embeddings/{video_id}")
async def compute_embeddings_endpoint(video_id: str, background_tasks: BackgroundTasks):
    return await embedding_processor.compute_embeddings(video_id, background_tasks)

@app.get("/embeddings_status/{video_id}")
async def embeddings_status(video_id: str):
    return await embedding_processor.get_status(video_id)

@app.get("/get_transcript_ocr_relationships/{video_id}")
async def get_transcript_ocr_relationships(video_id: str):
    return await embedding_processor.get_relationships(video_id)

@app.get("/find_ocr_for_transcript/{video_id}/{transcript_index}")
async def get_ocr_for_transcript(video_id: str, transcript_index: int):
    return await embedding_processor.find_ocr_for_transcript(video_id, transcript_index)

@app.get("/find_transcript_for_ocr/{video_id}/{scene_index}")
async def get_transcript_for_ocr(video_id: str, scene_index: int, ocr_text: str):
    return await embedding_processor.find_transcript_for_ocr(video_id, scene_index, ocr_text)

@app.get("/find_scene_for_transcript/{video_id}/{transcript_index}")
async def get_scene_for_transcript(video_id: str, transcript_index: int):
    return await embedding_processor.find_scene_for_transcript(video_id, transcript_index)

# SSE routes and handlers
@app.get("/ocr_progress/{video_id}")
async def ocr_progress(video_id: str):
    async def event_generator():
        if video_id not in sse_clients:
            sse_clients[video_id] = []
        
        queue = asyncio.Queue()
        client = (queue, asyncio.get_running_loop())
        sse_clients[video_id].append(client)
        
        try:
            await queue.put(json.dumps({
                "event": "connected",
                "data": {"message": "SSE connection established"}
            }))
            
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if video_id in sse_clients and client in sse_clients[video_id]:
                sse_clients[video_id].remove(client)
                if not sse_clients[video_id]:
                    del sse_clients[video_id]
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

def extract_video_id(url):
    """Extract video ID from YouTube URL."""
    pattern = r'(?:v=|\/)([0-9A-Za-z_-]{11}).*'
    match = re.search(pattern, url)
    return match.group(1) if match else None

def _move_moov_atom_to_front(video_path: Path) -> None:
    """Remux an mp4 in place so its moov atom is at the front (faststart).

    yt-dlp's ffmpeg merge does not add ``+faststart`` by default. Without it, a
    merged mp4 can store its moov atom at the end of the file, so a browser's
    <video> element must fetch close to the end of the file before it can read
    metadata. On a slow connection that routinely exceeded the frontend's
    video-preview timeout even though the download itself had succeeded.
    """
    try:
        ffmpeg = media_executable("ffmpeg")
    except RuntimeError as error:
        print(f"faststart remux skipped: {error}")
        return
    temp_path = video_path.with_name(f"{video_path.stem}.faststart{video_path.suffix}")
    try:
        result = subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(video_path),
                "-c", "copy", "-movflags", "+faststart",
                str(temp_path),
            ],
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0 and temp_path.is_file() and temp_path.stat().st_size > 0:
            temp_path.replace(video_path)
        else:
            print(f"faststart remux skipped for {video_path.name}: {result.stderr.decode('utf-8', 'ignore')[:500]}")
    except Exception as error:
        print(f"faststart remux failed for {video_path.name}: {error}")
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _download_youtube_video_sync(video_id: str, quality: int) -> None:
    """Blocking yt-dlp download and faststart remux, run off the event loop."""
    ydl_opts = {
        'format': (
            f'bestvideo[height<={quality}][vcodec^=vp9]+bestaudio/'
            f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'
        ),
        'outtmpl': str(VIDEO_DIR / f'{video_id}.%(ext)s'),
        'merge_output_format': 'mp4',
    }
    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file:
        cookies_path = Path(cookies_file).expanduser().resolve()
        if not cookies_path.is_file():
            raise RuntimeError(
                f"YTDLP_COOKIES_FILE does not exist: {cookies_path}"
            )
        ydl_opts["cookiefile"] = str(cookies_path)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
        except yt_dlp.utils.DownloadError as error:
            error_text = str(error)
            if "Sign in to confirm you're not a bot" in error_text:
                raise RuntimeError(
                    "YouTube requires authentication for this download. "
                    "Export YouTube cookies in Netscape format, upload the "
                    "cookie file to Colab, and set YTDLP_COOKIES_FILE to its path."
                ) from error
            raise

    merged_path = VIDEO_DIR / f"{video_id}.mp4"
    if merged_path.is_file():
        _move_moov_atom_to_front(merged_path)
        (VIDEO_DIR / f"{video_id}_video_quality.json").write_text(
            json.dumps({"height": quality}), encoding="utf-8"
        )


@app.get("/download/{video_id}")
@app.post("/download/{video_id}")
async def download_video(video_id: str, background_tasks: BackgroundTasks, quality: int = 480):
    try:
        if quality not in {480, 720}:
            raise HTTPException(status_code=400, detail="Video quality must be 480 or 720")
        video_path = str(VIDEO_DIR / f"{video_id}.mp4")
        quality_path = VIDEO_DIR / f"{video_id}_video_quality.json"
        stored_quality = None
        if quality_path.exists():
            try:
                stored_quality = int(json.loads(quality_path.read_text(encoding="utf-8")).get("height", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stored_quality = None
        existing_video_files = [
            Path(path) for path in glob.glob(str(VIDEO_DIR / f"{video_id}.*"))
            if not path.endswith("_video_quality.json")
        ]
        if existing_video_files and stored_quality and stored_quality < quality:
            for existing_path in existing_video_files:
                existing_path.unlink()
            existing_video_files = []
        if not existing_video_files:
            # Downloading (network I/O) and remuxing (CPU) are blocking calls.
            # Run them in a worker thread so the event loop stays free to serve
            # other requests -- including the browser's request for this
            # video's own preview once this response is sent.
            await asyncio.to_thread(_download_youtube_video_sync, video_id, quality)

        downloaded_files = os.listdir(VIDEO_DIR)
        for file in downloaded_files:
            if file.startswith(video_id) and not file.endswith("_video_quality.json"):
                video_path = str(VIDEO_DIR / file)
                break

        whisper_transcript_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
        has_whisper_transcript = os.path.exists(whisper_transcript_path)

        # Restore whichever transcript/translation the user was last viewing so a
        # reload (or re-selecting this video) doesn't silently revert the language.
        active_source = transcript_versions.get_active_source(video_id)
        active_language = None
        transcript_source = "whisper"
        active_transcript_path = whisper_transcript_path
        if active_source.get("kind") == "translation":
            language = active_source.get("language")
            candidate_path = TRANSCRIPTS_DIR / f"{video_id}_translated_{language}.json"
            if language and candidate_path.exists():
                active_transcript_path = str(candidate_path)
                active_language = language
                transcript_source = "translation"
        elif active_source.get("source") == "uploaded":
            candidate_path = TRANSCRIPTS_DIR / f"{video_id}_uploaded.json"
            if candidate_path.exists():
                active_transcript_path = str(candidate_path)
                transcript_source = "uploaded"

        scenes_path = str(SCENES_DIR / f"{video_id}.json")
        has_scenes = False
        if os.path.exists(scenes_path):
            try:
                with open(scenes_path, 'r', encoding='utf-8') as file:
                    has_scenes = bool(json.load(file))
            except (OSError, json.JSONDecodeError):
                has_scenes = False
        
        # Load existing scenes if available
        existing_scenes = []
        if has_scenes:
            try:
                with open(scenes_path, 'r') as f:
                    existing_scenes = json.load(f)
            except Exception as e:
                print(f"Error loading existing scenes: {e}")
        
        # Check if transcript is being generated
        progress_file = str(TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt")
        transcript_in_progress = os.path.exists(progress_file)
        
        # Load existing transcript if available (the active translation/upload, if any)
        transcript_to_use = None
        if has_whisper_transcript:
            try:
                with open(active_transcript_path, 'r') as f:
                    transcript_to_use = json.load(f)
            except Exception as e:
                print(f"Error loading existing transcript: {e}")
                active_language = None
                transcript_source = "whisper"
        
        # Try to get YouTube transcript if Whisper transcript is not available
        has_youtube_transcript = False
        if not transcript_to_use and not transcript_in_progress:
            try:
                youtube_transcript = await asyncio.to_thread(
                    YouTubeTranscriptApi.get_transcript, video_id, languages=['en']
                )
                if youtube_transcript:
                    transcript_to_use = youtube_transcript
                    has_youtube_transcript = True
                    with open(str(TRANSCRIPTS_DIR / f"{video_id}_youtube.json"), 'w') as f:
                        json.dump(youtube_transcript, f)
            except Exception as e:
                print(f"Error getting YouTube transcript: {e}")
        
        # Start missing processing tasks
        if not has_whisper_transcript and not transcript_in_progress:
            await transcript_processor.start_whisper_generation(video_id, video_path, background_tasks)
            transcript_in_progress = True
        
        return JSONResponse({
            "success": True,
            "video_url": f"/video/{video_id}",
            "video_id": video_id,
            "transcript": transcript_to_use,
            "has_youtube_transcript": has_youtube_transcript,
            "has_whisper_transcript": has_whisper_transcript,
            "transcript_in_progress": transcript_in_progress,
            "scenes": existing_scenes,
            "is_duplicate": os.path.exists(video_path),
            "transcript_source": transcript_source,
            "active_language": active_language
        })
            
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })

@app.post("/process_youtube")
async def process_youtube(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        url = data.get("url")
        if not url:
            return JSONResponse({
                "success": False,
                "error": "No URL provided"
            })
        
        video_id = extract_video_id(url)
        if not video_id:
            return JSONResponse({
                "success": False,
                "error": "Invalid YouTube URL"
            })
        
        return await download_video(video_id, background_tasks)
        
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })

@app.post("/generate_summary")
async def generate_summary(request: Request):
    try:
        data = await request.json()
        transcript = data.get("transcript", [])
        video_id = data.get("video_id")
        return await summary_processor.generate_summary(
            transcript, video_id, data.get("model"), data.get("language", "transcript"),
            structure=data.get("structure", "sections"), detail=data.get("detail", "balanced"),
            instructions=str(data.get("instructions") or ""),
        )
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })

@app.get("/summary/{video_id}")
async def get_summary(video_id: str, language: str = ""):
    return await summary_processor.get_summary(video_id, language)

def ascii_filename(text: str) -> str:
    import unicodedata
    text = str(text or "")
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(source, target)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")[:80]


@app.post("/export_suggestions/{video_id}")
async def post_export_suggestions(video_id: str, request: Request):
    """Suggest a short title for the chapters currently shown (in their language)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    chapters = data.get("chapters") if isinstance(data, dict) else None
    return await build_export_suggestions(video_id, chapters if isinstance(chapters, list) else None)


@app.get("/export_suggestions/{video_id}")
async def get_export_suggestions(video_id: str):
    return await build_export_suggestions(video_id)


async def build_export_suggestions(video_id: str, chapters=None):
    metadata_path = VIDEO_DIR / "metadata.json"
    source_title = video_id
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source_title = Path(
                metadata.get(video_id, {}).get("filename", source_title)
            ).stem or source_title
        except (OSError, json.JSONDecodeError):
            pass

    title_suggestion = source_title
    summary_path = SUMMARIES_DIR / f"{video_id}.json"
    if chapters is None and summary_path.is_file():
        try:
            chapters = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            chapters = None
    ai_suggestion = None
    if chapters:
        try:
            ai_suggestion = await summary_processor.suggest_title(chapters, video_id, source_title)
        except Exception as error:
            print(f"AI title suggestion failed: {error}")
    if ai_suggestion:
        title_suggestion = ai_suggestion["title"]
    elif chapters:
        try:
            title_suggestion = next(
                (
                    str(chapter.get("title", "")).strip()
                    for chapter in chapters
                    if isinstance(chapter, dict)
                    and str(chapter.get("title", "")).strip()
                    and not str(chapter.get("title", "")).lower().startswith(("part ", "chapter "))
                ),
                source_title,
            )
        except (TypeError, AttributeError):
            pass

    filename_suggestion = (
        (ai_suggestion or {}).get("filename") or ascii_filename(title_suggestion) or f"video_{video_id}"
    )
    return {
        "title_suggestion": title_suggestion,
        "filename_suggestion": filename_suggestion,
        "source_filename": source_title,
    }

def parse_chapter_timestamp(timestamp: str) -> float:
    parts = [float(part) for part in timestamp.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid chapter timestamp: {timestamp}")

def format_chapter_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def chapter_boundaries_from_topics(chapters):
    boundaries = []
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict) or "timestamp" not in chapter:
            continue
        boundary = {"timestamp": str(chapter["timestamp"]), "title": str(chapter.get("title", f"Chapter {index + 1}"))}
        if str(chapter.get("summary") or "").strip():
            boundary["summary"] = str(chapter["summary"]).strip()
        if isinstance(chapter.get("sections"), list):
            boundary["sections"] = [
                section for section in chapter["sections"]
                if isinstance(section, dict) and section.get("timestamp") is not None
            ]
        boundaries.append(boundary)
    return boundaries


def saved_section_points(chapter):
    """Turn sections saved with generated chapters into export key points."""
    points = []
    for section in chapter.get("sections") or []:
        title = clean_outline_title(section.get("title"))
        text = strip_narration(section.get("point"))
        if not title or not text:
            return []
        try:
            start = parse_chapter_timestamp(str(section["timestamp"]))
        except (KeyError, TypeError, ValueError):
            return []
        if chapter["end"] is not None and start >= chapter["end"]:
            continue
        points.append({"start": max(start, chapter["start"]), "title": title, "text": text})
    return sorted(points, key=lambda point: point["start"])


def scene_slide_text(scene):
    """Return the OCR text of one slide as ordered, de-duplicated lines."""
    blocks = []
    detections = (scene.get("yolo_detections") or {})
    if detections.get("success", False):
        for detection in detections.get("detections", []):
            text = str(detection.get("ocr_text") or "").strip()
            if text:
                blocks.append((detection.get("bbox") or [], text))
    surya = scene.get("surya_ocr") or {}
    if surya.get("success", True):
        for result in surya.get("results", []):
            text = str(result.get("text") or "").strip()
            if text and not result.get("matched", False):
                blocks.append((result.get("bbox") or [], text))

    def reading_order(block):
        box = block[0]
        return (float(box[1]), float(box[0])) if len(box) >= 2 else (float("inf"), 0.0)

    lines, seen = [], set()
    for _, text in sorted(blocks, key=reading_order):
        for line in text.splitlines():
            line = " ".join(line.split()).lstrip("\u2022\u25aa\u25cf\u25e6\u2013\u2014-*\u00b7o ").strip()
            key = line.casefold()
            if sum(character.isalnum() for character in line) < 2 or key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return lines


def chapter_slide_content(chapter, scene_chapters):
    """Slides visible during a chapter, including the one already on screen at its start."""
    end = chapter.get("end")
    starts = [export_slide_start(scene) for scene in scene_chapters]
    selected = [
        index for index, start in enumerate(starts)
        if chapter["start"] - 1 < start and (end is None or start < end)
    ]
    earlier = [index for index, start in enumerate(starts) if start <= chapter["start"] - 1]
    if earlier and (not selected or starts[selected[0]] > chapter["start"] + 1):
        selected.insert(0, earlier[-1])
    return [
        {
            "number": scene_chapters[index]["image_index"] + 1,
            "start": starts[index],
            "lines": scene_chapters[index].get("slide_text", []),
        }
        for index in selected if scene_chapters[index].get("slide_text")
    ]


def chapter_boundaries_from_scenes(scenes):
    return [
        {
            "timestamp": str(scene["timestamp"]),
            "time_seconds": float(scene.get("time_seconds", parse_chapter_timestamp(str(scene["timestamp"])))),
            "title": str(scene.get("title") or f"Slide change {index + 1}"),
            "image_index": index,
            "slide_text": scene_slide_text(scene),
        }
        for index, scene in enumerate(scenes)
        if isinstance(scene, dict) and "timestamp" in scene
    ]

def export_slide_start(scene):
    return float(scene.get("time_seconds", parse_chapter_timestamp(scene["timestamp"])))


def concise_export_title(title, limit=36):
    text = " ".join(str(title).split())
    if len(text) <= limit:
        return text
    prefix = text[:limit - 3]
    return (prefix.rsplit(" ", 1)[0] if " " in prefix else prefix) + "..."


def export_thumbnail(image, title, label=""):
    full_title = html.escape(title, quote=True)
    short_title = html.escape(concise_export_title(title))
    return (
        f'<figure class="export-thumbnail"><a href="{html.escape(image, quote=True)}" '
        f'target="_blank" rel="noopener" title="Enlarge: {full_title}">'
        f'<img src="{html.escape(image, quote=True)}" alt="{full_title}" loading="lazy"></a>'
        f'<figcaption title="{full_title}">{html.escape(label)}{short_title}</figcaption></figure>'
    )


def prepare_export_image(video_id, video_path, timestamp, scenes, destination, cache, step):
    key = round(timestamp * 1000)
    source = cache.get(key)
    if source is None:
        # Reuse the detected slide that is on screen at this time instead of
        # decoding a new frame from the video.
        shown = [scene for scene in scenes if export_slide_start(scene) <= timestamp + 0.5]
        matching = max(shown, key=export_slide_start) if shown else None
        if matching is not None:
            saved = FULLSIZE_IMAGES_DIR / video_id / f"{matching['image_index']}.jpg"
            if saved.is_file():
                source = saved
    if source is not None:
        step(f"Reusing detected slide image for {format_chapter_timestamp(timestamp)}")
        shutil.copy2(source, destination)
    else:
        step(f"Extracting missing export image at {format_chapter_timestamp(timestamp)}")
        subprocess.run([
            media_executable("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(timestamp), "-i", str(video_path),
            "-frames:v", "1", str(destination),
        ], check=True)
    cache[key] = destination


def combine_chapter_boundaries(topic_chapters, scene_chapters):
    boundaries = {}
    for chapter in topic_chapters + scene_chapters:
        seconds = parse_chapter_timestamp(chapter["timestamp"])
        existing = boundaries.get(seconds)
        if existing is None or existing.startswith("Slide change"):
            boundaries[seconds] = chapter["title"]
    return [
        {"timestamp": format_chapter_timestamp(seconds), "title": title}
        for seconds, title in sorted(boundaries.items())
    ]

def export_sentences(transcript):
    """Join caption fragments; punctuation or a speech pause closes an utterance."""
    sentences, words = [], []
    ordered = sorted(transcript, key=lambda item: float(item.get("start", 0)))
    previous_end, previous_speaker = None, None
    for index, item in enumerate(ordered):
        tokens = str(item.get("text", "")).split()
        start = float(item.get("start", 0))
        duration = float(item.get("duration", 0) or 0)
        explicit_end = float(item.get("end", start + duration) or start + duration)
        if duration <= 0 and explicit_end > start:
            duration = explicit_end - start
        speaker = item.get("speaker")
        # Whisper sometimes emits unpunctuated phrases. A real pause (not an
        # arbitrary caption boundary) or speaker turn is a safe utterance break.
        if words and (
            (previous_end is not None and start - previous_end >= 1.2)
            or (speaker and previous_speaker and speaker != previous_speaker)
        ):
            sentences.append({
                "start": words[0][1], "text": " ".join(word[0] for word in words),
                "speaker": words[0][2],
            })
            words = []
        previous_end = start + duration if duration > 0 else None
        previous_speaker = speaker
        if duration <= 0 and index + 1 < len(ordered):
            duration = max(0, float(ordered[index + 1].get("start", start)) - start)
        for offset, token in enumerate(tokens):
            words.append((token, start + duration * offset / len(tokens), item.get("speaker")))
            # Avoid decimal numbers and common abbreviations as sentence endings.
            abbreviation = token.lower().rstrip('"\'”)') in {
                "mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.", "vs.", "etc.",
            }
            if re.search(r'[.!?。！？]["\'”’)\]]*$', token) and not abbreviation:
                sentences.append({
                    "start": words[0][1], "text": " ".join(word[0] for word in words),
                    "speaker": words[0][2],
                })
                words = []
    if words:
        sentences.append({
            "start": words[0][1], "text": " ".join(word[0] for word in words),
            "speaker": words[0][2],
        })
    return sentences


def align_export_chapters(chapters, sentences):
    """Move internal boundaries forward; never drop the opening transcript."""
    result = []
    for chapter in sorted(chapters, key=lambda item: parse_chapter_timestamp(item["timestamp"])):
        source_start = parse_chapter_timestamp(chapter["timestamp"])
        start = 0 if not result else next(
            (item["start"] for item in sentences if item["start"] >= source_start), None
        )
        if start is None:
            if not sentences:
                start = source_start
            else:
                continue
        if result and start <= result[-1]["start"]:
            continue
        result.append({
            "index": len(result) + 1, "title": chapter["title"],
            "start": start, "source_start": source_start, "end": None,
            **{key: chapter[key] for key in ("summary", "sections") if chapter.get(key)},
        })
    for current, following in zip(result, result[1:]):
        current["end"] = following["start"]
    return result


def strip_narration(text):
    """Turn 'The lecture explains that X' into the direct statement 'X'."""
    narration = re.compile(
        r"^\s*(?:(?:in|throughout)\s+this\s+(?:part|chapter|section|segment|lecture|video|session|lesson)\s*,?\s*)?"
        r"(?:(?:the|this|our)\s+)?(?:lecture|lecturer|speaker|instructor|professor|presenter|teacher|"
        r"video|chapter|section|segment|session|lesson|course|talk|author|narrator|host)\s+"
        r"(?:also\s+|then\s+|further\s+|briefly\s+|first\s+|now\s+)?"
        r"(?:explains|points\s+out|notes|mentions|discusses|describes|says|states|emphasi[sz]es|highlights|"
        r"introduces|shows|demonstrates|covers|talks\s+about|stresses|reminds\s+(?:students|learners|the\s+audience|viewers)|"
        r"tells\s+(?:students|learners|the\s+audience|viewers)|clarifies|outlines|presents|reviews|walks\s+through|"
        r"illustrates|argues|suggests|notes|addresses|explores|focuses\s+on|begins\s+(?:by|with)|concludes\s+(?:by|with)|"
        r"goes\s+over|is\s+about|deals\s+with)\s*(?:that\s+)?",
        re.IGNORECASE,
    )
    learner = re.compile(
        r"^\s*(?:(?:in|throughout)\s+this\s+(?:part|chapter|section|segment|lecture|video|session|lesson)\s*,?\s*)?"
        r"(?:the\s+)?(?:learners?|students?|participants?|viewers?|audience|you|we)\s+"
        r"(?:will\s+|can\s+|are\s+going\s+to\s+|should\s+|get\s+to\s+)?(?:also\s+|then\s+|first\s+)?"
        r"(?:be\s+introduced\s+to|be\s+shown|learns?(?:\s+about)?|understands?|discovers?|explores?|"
        r"gets?\s+to\s+know|sees?|finds?\s+out(?:\s+about)?|are\s+introduced\s+to|is\s+introduced\s+to)\s*(?:that\s+)?",
        re.IGNORECASE,
    )
    text = " ".join(str(text or "").split())
    stripped = narration.sub("", text, count=1)
    if stripped == text:
        stripped = learner.sub("", text, count=1)
    if stripped == text or len(stripped.split()) < 2:
        return text
    return stripped[0].upper() + stripped[1:]


def clean_outline_title(title, limit=60):
    title = " ".join(str(title or "").split()).strip(" .,:;-–—\"'")
    title = re.sub(r"(?:\.\.\.|…)$", "", title).strip()
    if not title or len(title) > limit or title.lower() in {"key point", "important point", "topic", "summary"}:
        return ""
    return title


async def export_key_points(sentences, section_starts=None, extras=None):
    """Return (points, method, summary); chapter/section titles go into extras."""
    extras = extras if extras is not None else {}
    batches, batch, characters = [], [], 0
    for sentence in sentences:
        if batch and characters + len(sentence["text"]) > 10000:
            batches.append(batch)
            batch, characters = [], 0
        batch.append(sentence)
        characters += len(sentence["text"])
    if batch:
        batches.append(batch)
    if len(batches) <= 1:
        return await summarize_export_batch(sentences, section_starts, extras)
    points, summaries, source = [], [], ""
    section_titles = {}
    for batch in batches:
        batch_extras = {}
        batch_points, source, summary = await summarize_export_batch(batch, section_starts, batch_extras)
        points.extend(batch_points)
        section_titles.update(batch_extras.get("section_titles", {}))
        if summary:
            summaries.append({"start": batch[0]["start"], "text": summary})
    if summaries:
        _, _, summary = await export_key_points(summaries, None, extras)
    else:
        summary = None
    extras["section_titles"] = section_titles
    return sorted(points, key=lambda point: point["start"]), source, summary


async def summarize_export_batch(sentences, section_starts=None, extras=None):
    """Generate a chapter title, summary, key points and section titles in one request."""
    extras = extras if extras is not None else {}
    if not sentences:
        return [], "No spoken content", None
    summary_processor._refresh_clients()
    chain = summary_processor._provider_chain()
    if not chain:
        return (
            build_outline_points(sentences),
            "Extractive key sentences (AI not configured)",
            None,
        )
    failures = []
    prompt_sentences = [
        {"id": index, "text": sentence["text"]}
        for index, sentence in enumerate(sentences)
    ]
    section_ids = sorted({
        next((index for index, sentence in enumerate(sentences) if sentence["start"] >= start), len(sentences) - 1)
        for start in (section_starts or [])
        if sentences[0]["start"] <= start <= sentences[-1]["start"]
    })
    section_request = (
        " Also return section_titles: an object mapping each of these sentence ids "
        f"{json.dumps(section_ids)} (as strings) to a heading of 2 to 6 words for the section that starts there."
        if section_ids else ""
    )
    prompt = (
        "You are writing a course outline that helps learners study this lecture chapter. "
        "Write everything in the transcript's original language. Return a JSON object with: "
        "title (a clear chapter heading of 2 to 6 words, a specific topic noun phrase in title case, "
        "not a sentence, no trailing punctuation or ellipsis); "
        "summary (one or two concise sentences stating what the learner will learn); "
        "points (1 to 4 important key points). "
        "Each point must contain sentence_id (an input id), title (a specific topic noun phrase of at most "
        "5 words, not a sentence and not 'Key point'), and text (a concise complete sentence with terminal "
        "punctuation, grounded only in this transcript)."
        + section_request
        + " State the content directly. Never mention the lecture, speaker, instructor, video or chapter: "
        "write 'Gradient descent minimizes the loss.' instead of 'The lecture explains that gradient descent "
        "minimizes the loss.' or 'The speaker points out that...'. Do not invent facts. Input:\n"
        + json.dumps(prompt_sentences, ensure_ascii=False)
    )
    for provider, model in chain:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(summary_processor._complete, provider, model, prompt, 0.2),
                timeout=90,
            )
            data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            if not isinstance(data.get("summary"), str):
                raise ValueError("AI returned an invalid chapter summary")
            summary = strip_narration(data["summary"])
            if not summary:
                raise ValueError("AI returned no chapter summary")
            validated = []
            allowed = {item["id"] for item in prompt_sentences}
            for point in data.get("points", []):
                sentence_id = point.get("sentence_id")
                title, text = str(point.get("title", "")).strip(), str(point.get("text", "")).strip()
                if type(sentence_id) is not int or sentence_id not in allowed or not title or not text:
                    raise ValueError("AI returned invalid key points")
                if title.lower() in {"key point", "important point", "topic"}:
                    raise ValueError("AI returned generic key-point titles")
                if not re.search(r'[.!?。！？]["\'”’)\]]*$', text):
                    raise ValueError("AI returned an incomplete key-point sentence")
                validated.append({
                    "start": sentences[sentence_id]["start"], "title": title, "text": strip_narration(text),
                })
            if not validated:
                raise ValueError("AI returned no key points")
            chapter_title = clean_outline_title(data.get("title"))
            if chapter_title:
                extras["title"] = chapter_title
            section_titles = {}
            raw_sections = data.get("section_titles")
            if isinstance(raw_sections, dict):
                for key, value in raw_sections.items():
                    try:
                        sentence_id = int(key)
                    except (TypeError, ValueError):
                        continue
                    heading = clean_outline_title(value)
                    if sentence_id in allowed and heading:
                        section_titles[sentences[sentence_id]["start"]] = heading
            extras["section_titles"] = section_titles
            return (
                sorted(validated, key=lambda point: point["start"]),
                "AI-summarized key points",
                summary,
            )
        except Exception as error:
            failures.append(f"{provider} {model}: {error}")
    raise HTTPException(
        status_code=502,
        detail="Could not summarize export chapter: " + " | ".join(failures),
    )


def build_export_subparts(chapter, sentences, points, scenes, mode, section_titles=None):
    section_titles = section_titles or {}
    boundaries = {}
    for kind, entries in (
        ("point", points if mode in {"points", "both"} else []),
        ("slide", scenes if mode in {"slides", "both"} else []),
    ):
        for entry in entries:
            source_start = entry["start"] if kind == "point" else export_slide_start(entry)
            if source_start < chapter["start"] or (chapter["end"] is not None and source_start >= chapter["end"]):
                continue
            start = next((item["start"] for item in sentences if item["start"] >= source_start), None)
            if start is None or (chapter["end"] is not None and start >= chapter["end"]):
                # Still retain the slide event, even if it occurs during the last sentence.
                start = sentences[-1]["start"] if sentences else chapter["start"]
            item = boundaries.setdefault(start, {"start": start, "points": [], "slides": []})
            item["points" if kind == "point" else "slides"].append(entry)
    boundaries.setdefault(chapter["start"], {"start": chapter["start"], "points": [], "slides": []})
    subparts = [boundaries[start] for start in sorted(boundaries)]
    # Opening speech belongs to the first real topic, not an invented transcript heading.
    if mode == "points" and len(subparts) > 1 and not subparts[0]["points"] and not section_titles.get(chapter["start"]):
        opening = subparts.pop(0)
        subparts[0]["start"] = opening["start"]
    for index, subpart in enumerate(subparts):
        end = subparts[index + 1]["start"] if index + 1 < len(subparts) else chapter["end"]
        subpart["sentences"] = [
            item for item in sentences if item["start"] >= subpart["start"]
            and (end is None or item["start"] < end)
        ]
        subpart["title"] = (
            subpart["points"][0]["title"] if subpart["points"] else
            section_titles.get(subpart["start"])
            or chapter["title"]
        )
    return subparts


def export_archive_paths(directory, flags):
    """Include chosen deliverables and referenced webpage assets, not working files."""
    webpage = flags["include_webpage"] or flags["include_scorm"]
    names = set()
    if webpage:
        names.add("index.html")
    if flags["include_scorm"]:
        names.update({"imsmanifest.xml", "scorm_api.js"})
    if flags["include_word"]:
        names.add("chapter_document.docx")
    if flags["include_pdf"]:
        names.add("chapter_document.pdf")
    if flags["include_outline"]:
        names.add("video_outline.docx")
    if flags["include_transcripts"]:
        names.update({"transcript_by_chapter.md", "chapters.json"})
    return [
        path for path in directory.iterdir() if path.name in names
        or (path.suffix == ".jpg" and (flags["include_images"] or webpage))
        or (path.suffix == ".mp4" and (flags["include_clips"] or webpage or flags.get("include_section_clips")))
        or (path.suffix == ".txt" and flags["include_transcripts"])
    ]


def is_rtl_text(text, language=""):
    """True for right-to-left languages or text written mostly in Arabic/Hebrew script."""
    if str(language or "").split("-")[0].lower() in {"ar", "fa", "he", "ur"}:
        return True
    letters = [character for character in str(text or "") if character.isalpha()]
    if not letters:
        return False
    rtl_letters = sum(1 for character in letters if "\u0590" <= character <= "\u08ff" or "\ufb1d" <= character <= "\ufeff")
    return rtl_letters / len(letters) > 0.3


def make_word_rtl(document):
    """Mark every paragraph and run of a python-docx document as right-to-left."""
    from docx.oxml.ns import qn
    successors = (
        "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing",
        "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
        "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
    )
    for paragraph in document.element.body.iter(qn("w:p")):
        properties = paragraph.get_or_add_pPr()
        if properties.find(qn("w:bidi")) is None:
            properties.insert_element_before(OxmlElement("w:bidi"), *successors)
        for run in paragraph.iter(qn("w:r")):
            run_properties = run.get_or_add_rPr()
            if run_properties.find(qn("w:rtl")) is None:
                run_properties.get_or_add_rtl()


_pdf_rtl_support = None


def pdf_rtl_support():
    """Return (font name, reshape, get_display) for Arabic PDFs, or None when unavailable."""
    global _pdf_rtl_support
    cached = globals().get("_pdf_rtl_support")
    if cached is not None:
        return cached or None
    _pdf_rtl_support = False
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:
        return None
    candidates = [os.environ.get("EXPORT_RTL_FONT", "")]
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates += [str(windows_fonts / name) for name in ("arial.ttf", "tahoma.ttf", "segoeui.ttf")]
    candidates += [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ]
    try:
        import matplotlib
        candidates.append(str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"))
    except ImportError:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                pdfmetrics.registerFont(TTFont("ExportRTL", candidate))
            except Exception:
                continue
            _pdf_rtl_support = ("ExportRTL", arabic_reshaper.reshape, get_display)
            return _pdf_rtl_support
    return None


def create_combined_chapter_documents(
    chapters, chapter_files, pdf_path=None, docx_path=None,
    pdf_text=True, pdf_images=True, word_text=True, word_images=True,
    document_title="Lecture Chapters", document_subtitle=None, timestamp_mode="subpart", rtl=False,
):
    """Create title, screenshot, and transcript documents for all chapters."""
    if not pdf_path and not docx_path:
        return
    styles = getSampleStyleSheet()
    rtl_pdf = pdf_rtl_support() if rtl and pdf_path else None
    rtl_styles = {}

    def pdf_paragraph(text, style_name, label="", indent=0):
        text = str(text)
        if rtl_pdf is None:
            body = html.escape(text).replace("\n", "<br/>")
            return Paragraph((f"<b>{html.escape(label)}</b> " if label else "") + body, styles[style_name])
        # ReportLab has no bidi layout: wrap the shaped logical text first, then
        # reorder each line so wrapped Arabic lines keep their reading order.
        font_name, reshape, get_display = rtl_pdf
        if style_name not in rtl_styles:
            rtl_styles[style_name] = ParagraphStyle(
                f"{style_name}RTL", parent=styles[style_name], fontName=font_name, alignment=TA_RIGHT,
            )
        style = rtl_styles[style_name]
        width = letter[0] - 1.2 * inch - indent - 4
        lines = []
        for raw_line in ((f"{label} " if label else "") + text).split("\n"):
            shaped = reshape(raw_line)
            lines.extend(
                html.escape(get_display(line, base_dir="R"))
                for line in (simpleSplit(shaped, font_name, style.fontSize, width) or [""])
            )
        return Paragraph("<br/>".join(lines), style)

    pdf_story = [pdf_paragraph(document_title, "Title")] if pdf_path else None
    if pdf_story is not None and document_subtitle:
        pdf_story.append(pdf_paragraph(document_subtitle, "Italic"))
    word_document = Document() if docx_path else None
    if word_document:
        word_document.add_heading(document_title, level=0)
        if document_subtitle:
            word_document.add_paragraph(document_subtitle, style="Subtitle")

    for index, chapter in enumerate(chapters):
        files = chapter_files[index]
        title = str(chapter["title"])
        timestamp = format_chapter_timestamp(chapter["start"])
        transcript = files["transcript"].read_text(encoding="utf-8").strip()
        chapter_summary = chapter.get("summary")
        heading = f"Part {index + 1}: {title}"
        image_path = files["image"]
        try:
            with PillowImage.open(image_path) as image:
                image.verify()
            normalized_image_path = image_path.with_suffix(".png")
            with PillowImage.open(image_path) as image:
                image.convert("RGB").save(normalized_image_path, format="PNG")
            image_path = normalized_image_path
        except (FileNotFoundError, UnidentifiedImageError, OSError):
            image_path = None

        if pdf_story is not None:
            pdf_story.extend([
            pdf_paragraph(heading, "Heading1"),
            pdf_paragraph(f"Starts at {timestamp}", "Normal"),
            Spacer(1, 0.15 * inch),
            ])
            if pdf_text and chapter_summary:
                pdf_story.append(pdf_paragraph(chapter_summary, "BodyText", label="Summary:"))
                pdf_story.append(Spacer(1, 0.1 * inch))
            if pdf_text and files.get("slide_content"):
                pdf_story.append(pdf_paragraph("Slide content", "Heading2"))
                for slide in files["slide_content"]:
                    pdf_story.append(pdf_paragraph(
                        f"({format_chapter_timestamp(slide['start'])})", "Normal", label=f"Slide {slide['number']}",
                    ))
                    if rtl_pdf is not None:
                        pdf_story.extend(pdf_paragraph(f"? {line}", "BodyText") for line in slide["lines"])
                    else:
                        pdf_story.append(ListFlowable(
                            [ListItem(Paragraph(html.escape(line), styles["BodyText"])) for line in slide["lines"]],
                            bulletType="bullet", leftIndent=14,
                        ))
                pdf_story.append(Spacer(1, 0.1 * inch))
        if pdf_story is not None and pdf_images and image_path is not None:
            pdf_story.extend([
                PdfImage(str(image_path), width=6.5 * inch, height=3.65 * inch, kind="proportional"),
                Spacer(1, 0.15 * inch),
            ])
        if pdf_story is not None and pdf_text:
            for subpart_index, subpart in enumerate(files.get("subparts", []), start=1):
                pdf_story.append(pdf_paragraph(
                    f"{index + 1}.{subpart_index} " + subpart["title"] + (
                        f" ({format_chapter_timestamp(subpart['start'])})" if timestamp_mode != "part" else ""
                    ),
                    "Heading2",
                ))
                for scene in subpart["slides"]:
                    pdf_story.append(pdf_paragraph(
                        f"Slide at {format_chapter_timestamp(export_slide_start(scene))}", "Normal",
                    ))
                    if pdf_images and scene["image"].is_file():
                        pdf_story.append(PdfImage(str(scene["image"]), width=6.5 * inch,
                                                  height=3.65 * inch, kind="proportional"))
                for point in subpart["points"]:
                    pdf_story.append(pdf_paragraph(point["text"], "BodyText"))
                pdf_story.append(pdf_paragraph(subpart["transcript"], "BodyText"))
            if not files.get("subparts"):
                pdf_story.append(pdf_paragraph(
                    transcript or "No transcript available for this chapter.", "BodyText",
                ))
        if word_document:
            word_document.add_heading(heading, level=1)
            word_document.add_paragraph(f"Starts at {timestamp}")
            if word_text and chapter_summary:
                word_document.add_paragraph(f"Summary: {chapter_summary}")
            if word_text and files.get("slide_content"):
                add_word_slide_content(word_document, files["slide_content"], level=2)
        if word_document and word_images and image_path is not None:
            try:
                word_document.add_picture(str(image_path), width=Inches(6.5))
            except UnrecognizedImageError:
                pass
        if word_document and word_text:
            for subpart_index, subpart in enumerate(files.get("subparts", []), start=1):
                word_document.add_heading(
                    f"{index + 1}.{subpart_index} " + subpart["title"] + (
                        f" ({format_chapter_timestamp(subpart['start'])})" if timestamp_mode != "part" else ""
                    ), level=2
                )
                for scene in subpart["slides"]:
                    word_document.add_paragraph(
                        f"Slide at {format_chapter_timestamp(export_slide_start(scene))}"
                    )
                    if word_images and scene["image"].is_file():
                        try:
                            word_document.add_picture(str(scene["image"]), width=Inches(6.5))
                        except UnrecognizedImageError:
                            pass
                for point in subpart["points"]:
                    word_document.add_paragraph(point["text"], style="List Bullet")
                word_document.add_paragraph(subpart["transcript"] or "No spoken content.")
            if not files.get("subparts"):
                word_document.add_heading("Transcript", level=2)
                word_document.add_paragraph(transcript or "No transcript available for this chapter.")
    footnote = "Transcription model: faster-whisper turbo."
    if pdf_story is not None:
        pdf_story.extend([Spacer(1, 0.3 * inch), pdf_paragraph(footnote, "Italic")])
        SimpleDocTemplate(str(pdf_path), pagesize=letter, rightMargin=0.6 * inch,
                      leftMargin=0.6 * inch, topMargin=0.6 * inch,
                      bottomMargin=0.6 * inch).build(pdf_story)
    if word_document:
        word_document.add_paragraph(footnote, style="Caption")
        if rtl:
            make_word_rtl(word_document)
        word_document.save(str(docx_path))

def add_word_slide_content(document, slides, level):
    document.add_heading("Slide content", level=level)
    for slide in slides:
        document.add_paragraph().add_run(
            f"Slide {slide['number']} ({format_chapter_timestamp(slide['start'])})"
        ).bold = True
        for line in slide["lines"]:
            document.add_paragraph(line, style="List Bullet")


def collapse_word_heading(paragraph):
    paragraph_properties = paragraph._p.get_or_add_pPr()
    paragraph_properties.append(OxmlElement("w:collapsed"))

export_jobs = ExportJobStore(EXPORTS_DIR / "jobs")


@app.post("/caption_file/{video_id}/{format}")
async def download_caption_file(video_id: str, format: str, request: Request):
    if format not in {"srt", "vtt"}:
        raise HTTPException(status_code=400, detail="Caption format must be srt or vtt")
    video_path = video_processor.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    options = await request.json()
    if not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="Caption options must be an object")
    try:
        captions = validate_captions(options.get("transcript"))
        content = caption_text(captions, await video_duration(video_path), format)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    from urllib.parse import quote
    name = caption_filename(str(options.get("filename") or "video")[:150]) + f".{format}"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/vtt" if format == "vtt" else "application/x-subrip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.post("/caption_video_jobs/{video_id}")
async def start_caption_video_job(video_id: str, request: Request):
    video_path = video_processor.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    options = await request.json()
    if not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="Caption export options must be an object")
    try:
        captions = validate_captions(options.get("transcript"))
        style = validate_caption_style(options.get("style"))
        language = options.get("caption_language") or detect_transcript_language(captions)
        if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2}", language):
            raise ValueError("Caption language must be a two-letter code.")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    label = str(options.get("transcript_label") or "Selected transcript")[:200]
    filename = f"{caption_filename(str(options.get('filename') or 'video')[:150])[:97]}_{language.upper()}.mp4"

    async def generate(video_id, options, step, artifacts, workspace):
        step(f"Caption source: {label}")
        return await render_captioned_video(video_path, captions, filename, workspace, step, style)

    job_id = export_jobs.start(video_id, {"document_title": f"Captioned video - {label}"}, generate)
    return {"success": True, "job_id": job_id, "status_url": f"/export_jobs/{job_id}/status"}


@app.post("/caption_preview/{video_id}")
async def caption_preview(video_id: str, request: Request):
    video_path = video_processor.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    options = await request.json()
    if not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="Caption preview options must be an object")
    try:
        captions = validate_captions(options.get("transcript"))
        style = validate_caption_style(options.get("style"))
        at = options.get("at", 0)
        validate_captions([{"start": at, "text": "Preview time"}])
        with export_workspace() as workspace:
            image = await render_caption_preview(video_path, captions, at, style, workspace)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/export_chapters/{video_id}")
async def export_chapters_endpoint(video_id: str, request: Request):
    return await export_chapters(video_id, request)


@app.post("/export_jobs/{video_id}")
async def start_export_job(video_id: str, request: Request):
    if not video_processor.get_video_path(video_id):
        raise HTTPException(status_code=404, detail="Video not found")
    options = await request.json()
    if not isinstance(options, dict):
        raise HTTPException(status_code=400, detail="Export options must be an object")

    async def generate(video_id, options, step, artifacts, export_root):
        class ExportRequest:
            async def json(self):
                return options
        return await export_chapters(video_id, ExportRequest(), step, artifacts, export_root)

    job_id = export_jobs.start(video_id, options, generate)
    return {"success": True, "job_id": job_id, "status_url": f"/export_jobs/{job_id}/status"}


def read_export_job(job_id):
    try:
        return export_jobs.read(job_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="Export not found")


@app.get("/export_jobs/{job_id}/status")
async def export_job_status(job_id: str):
    return read_export_job(job_id)


@app.get("/saved_exports/{video_id}")
async def saved_exports(video_id: str):
    return {"exports": export_jobs.list(video_id)}

@app.get("/saved_caption_videos")
async def saved_caption_videos():
    return {"exports": [
        job for job in export_jobs.list()
        if job["title"].startswith("Captioned video - ")
    ]}


@app.get("/export_jobs/{job_id}/download")
async def download_export_job(job_id: str):
    state = read_export_job(job_id)
    if state["status"] != "complete":
        raise HTTPException(status_code=409, detail="Export is not complete")
    try:
        path = export_jobs.file(job_id, state["download_file"])
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export download not found")
    return FileResponse(path, filename=state["filename"])


@app.get("/export_jobs/{job_id}/webpage/{filename}")
async def open_export_webpage(job_id: str, filename: str):
    state = read_export_job(job_id)
    if state["status"] != "complete":
        raise HTTPException(status_code=409, detail="Export is not complete")
    try:
        path = export_jobs.file(job_id, filename, webpage=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export asset not found")
    return FileResponse(path)


async def export_chapters(video_id: str, request: Request, on_step=None, on_artifacts=None, export_root=None):
    """Create a ZIP containing chapter clips, screenshots, and transcripts."""
    options = await request.json()
    export_root = Path(export_root) if export_root is not None else EXPORTS_DIR
    step = on_step or (lambda message: None)
    step("Reading export options and saved chapters, slides and transcript")
    interval_minutes = options.get("interval_minutes")
    transcript_language = str(options.get("transcript_language", "")).strip()
    if transcript_language and transcript_language not in {"de", "en", "ar", "pl"}:
        raise HTTPException(status_code=400, detail="Invalid transcript language")
    chapter_grouping = options.get("chapter_grouping", "topic")
    if chapter_grouping not in {"topic", "slides", "combined"}:
        raise HTTPException(status_code=400, detail="Invalid chapter grouping")
    timestamp_mode = options.get("timestamp_mode", "subpart")
    if timestamp_mode not in {"original", "part", "subpart"}:
        raise HTTPException(status_code=400, detail="Invalid timestamp mode")
    subpart_mode = options.get("subpart_mode", "points")
    if subpart_mode not in {"points", "slides", "both"}:
        raise HTTPException(status_code=400, detail="Invalid subpart mode")
    format_selected = any(name.startswith("include_") for name in options)
    export_flags = {name: bool(options.get(name, not format_selected and name == "include_webpage")) for name in (
        "include_images", "include_transcripts", "include_clips",
        "include_word", "include_pdf", "include_webpage", "include_outline", "include_scorm",
        "include_section_clips",
    )}
    if not any(export_flags.values()):
        raise HTTPException(status_code=400, detail="Select at least one export option")
    video_path = video_processor.get_video_path(video_id)
    translated_summary_path = (
        SUMMARIES_DIR / f"{video_id}_summary_{transcript_language}.json"
        if transcript_language else None
    )
    summary_path = translated_summary_path if translated_summary_path and translated_summary_path.is_file() else SUMMARIES_DIR / f"{video_id}.json"
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found")
    scene_path = SCENES_DIR / f"{video_id}.json"
    if interval_minutes is None and chapter_grouping in {"slides", "combined"} and not scene_path.is_file():
        if chapter_grouping == "slides":
            raise HTTPException(status_code=400, detail="Detect slides before exporting by slide changes")

    try:
        scene_chapters = []
        if scene_path.is_file():
            with scene_path.open("r", encoding="utf-8") as file:
                scene_chapters = chapter_boundaries_from_scenes(json.load(file))
        if interval_minutes is not None:
            try:
                interval_seconds = float(interval_minutes) * 60
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Interval duration must be a number of minutes")
            if interval_seconds <= 0:
                raise HTTPException(status_code=400, detail="Interval duration must be greater than zero")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                check=True, capture_output=True, text=True
            )
            duration = float(probe.stdout.strip())
            chapters = [
                {"timestamp": format_chapter_timestamp(start), "title": f"Interval {index + 1}"}
                for index, start in enumerate(
                    range(0, max(1, int(duration)), max(1, int(interval_seconds)))
                )
            ]
        else:
            topic_chapters = []
            if summary_path.is_file():
                with summary_path.open("r", encoding="utf-8") as file:
                    topic_chapters = chapter_boundaries_from_topics(json.load(file))
                if translated_summary_path and summary_path != translated_summary_path:
                    # The saved chapters are in another language: keep their
                    # timestamps, but let the export rewrite summaries and sections.
                    step("Chapters are not available in the transcript language; writing sections from the transcript")
                    for chapter in topic_chapters:
                        chapter.pop("summary", None)
                        chapter.pop("sections", None)
            if scene_path.is_file():
                with scene_path.open("r", encoding="utf-8") as file:
                    scene_chapters = chapter_boundaries_from_scenes(json.load(file))
            if chapter_grouping == "topic":
                chapters = topic_chapters
            elif chapter_grouping == "slides":
                chapters = scene_chapters
            else:
                # Keep topics as the top-level parts so slide changes can be
                # represented as subparts in the outline.
                chapters = topic_chapters
        if not isinstance(chapters, list) or not chapters:
            if chapter_grouping == "slides":
                raise HTTPException(status_code=400, detail="Detect slides before exporting by slide changes")
            # Keep asset and transcript exports usable when AI chapter
            # generation failed. The full video becomes one export part.
            chapters = [{"timestamp": "00:00", "title": "Full video"}]

        transcript = []
        transcript_candidates = (
            [TRANSCRIPTS_DIR / f"{video_id}_translated_{transcript_language}.json"]
            if transcript_language else []
        ) + [
            TRANSCRIPTS_DIR / f"{video_id}_whisper.json",
            TRANSCRIPTS_DIR / f"{video_id}_youtube.json",
            TRANSCRIPTS_DIR / f"{video_id}.json",
        ]
        for transcript_path in transcript_candidates:
            if transcript_path.is_file():
                with transcript_path.open("r", encoding="utf-8") as file:
                    transcript = json.load(file)
                break
        transcript = export_sentences(transcript)
        step(f"Reconstructed {len(transcript)} complete transcript sentences")
        transcript_required = any((
            export_flags["include_transcripts"],
            export_flags["include_word"],
            export_flags["include_pdf"],
            export_flags["include_webpage"],
            export_flags["include_outline"],
            export_flags["include_scorm"],
        ))
        if not transcript and transcript_required:
            raise HTTPException(status_code=400, detail="No transcript available")
        speaker_names = {}
        speaker_meta_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"
        if speaker_meta_path.is_file():
            try:
                speaker_names = json.loads(
                    speaker_meta_path.read_text(encoding="utf-8")
                ).get("speaker_names", {})
            except (OSError, json.JSONDecodeError):
                speaker_names = {}

        chapter_data = align_export_chapters(chapters, transcript)
        if not chapter_data:
            raise HTTPException(status_code=400, detail="Chapter timestamps are invalid")

        export_dir = export_root / video_id
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_path = export_root / f"{video_id}_chapters.zip"
        direct_export_path = None
        document_title = Path(video_path).stem
        source_filename = document_title
        metadata_path = VIDEO_DIR / "metadata.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                document_title = Path(
                    metadata.get(video_id, {}).get("filename", document_title)
                ).stem or document_title
            except (OSError, json.JSONDecodeError):
                pass
        requested_title = str(options.get("document_title", "")).strip()
        if requested_title:
            document_title = requested_title
        with export_workspace(step) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            generic_name = (
                bool(re.fullmatch(r"[0-9a-f]{32,64}", document_title, re.IGNORECASE))
                or document_title.lower() in {"video", "meeting", "recording", "input"}
            )
            topic_title = next(
                (
                    str(chapter.get("title", "")).strip()
                    for chapter in chapter_data
                    if str(chapter.get("title", "")).strip()
                    and not str(chapter.get("title", "")).lower().startswith(("part ", "chapter "))
                ),
                "",
            )
            document_subtitle = f"Source clip: {source_filename}" if topic_title and generic_name else None
            if topic_title and generic_name:
                document_title = topic_title
            export_title = re.sub(r"[^A-Za-z0-9._-]+", "_", document_title).strip("._-")
            export_title = export_title or f"video_{video_id}"
            requested_filename = re.sub(
                r"[^A-Za-z0-9._-]+", "_", str(options.get("export_filename", "")).strip()
            ).strip("._-")
            zip_path = export_root / (
                requested_filename if requested_filename.lower().endswith(".zip")
                else requested_filename + ".zip"
            ) if requested_filename else export_root / f"{export_title}_chapters.zip"
            chaptered_lines = [f"# {document_title}", ""]
            chapter_files = []
            image_cache = {}
            overlap_seconds = 1.0 if options.get("clip_overlap", True) else 0.0
            include_slide_text = bool(options.get("include_slide_text", False))
            if include_slide_text and not any(scene.get("slide_text") for scene in scene_chapters):
                step("Slide content: no slide text found yet. Run text extraction (OCR) on the slides first")
            for chapter in chapter_data:
                number = chapter["index"]
                chapter["clip_start"] = max(0.0, chapter["start"] - (overlap_seconds if number > 1 else 0.0))
                step(f"Part {number}/{len(chapter_data)}: preparing {chapter['title']}")
                safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", chapter["title"]).strip("_") or f"chapter_{number}"
                base_name = f"{number:02d}_{safe_title}"
                clip_path = temp_dir / f"{base_name}.mp4"
                image_path = temp_dir / f"{base_name}.jpg"
                transcript_path = temp_dir / f"{base_name}.txt"

                ffmpeg_clip = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", str(chapter["clip_start"]),
                    "-i", str(video_path),
                ]
                if chapter["end"] is not None:
                    ffmpeg_clip.extend(["-t", str(chapter["end"] - chapter["clip_start"])])
                ffmpeg_clip.extend(["-c:v", "libx264", "-c:a", "aac", str(clip_path)])
                if export_flags["include_clips"] or export_flags["include_webpage"] or export_flags["include_scorm"]:
                    ffmpeg_clip[0] = media_executable("ffmpeg")
                    step(f"Part {number}/{len(chapter_data)}: encoding video clip with FFmpeg")
                    if chapter["clip_start"] < chapter["start"]:
                        step(f"Clip starts {chapter['start'] - chapter['clip_start']:.1f}s early to preserve speech at the boundary")
                    subprocess.run(ffmpeg_clip, check=True)
                if export_flags["include_images"] or export_flags["include_word"] or export_flags["include_pdf"] or export_flags["include_webpage"] or export_flags["include_scorm"]:
                    prepare_export_image(
                        video_id, video_path, chapter["start"], scene_chapters,
                        image_path, image_cache, step,
                    )

                chapter_transcript = [
                    item for item in transcript
                    if chapter["start"] <= float(item.get("start", 0))
                    and (chapter["end"] is None or float(item.get("start", 0)) < chapter["end"])
                ]
                def format_export_transcript_item(item):
                    speaker = item.get("speaker")
                    speaker_prefix = (
                        f"[{speaker_names.get(speaker, speaker)}] " if speaker else ""
                    )
                    return (
                        f"[{format_chapter_timestamp(float(item.get('start', 0)))}] "
                        f"{speaker_prefix}{item.get('text', '').strip()}"
                    )

                transcript_text = "\n".join(
                    format_export_transcript_item(item) for item in chapter_transcript
                )
                if timestamp_mode != "original":
                    transcript_lines = transcript_text.splitlines()
                    if timestamp_mode == "part":
                        transcript_lines = [
                            re.sub(r"^\[[^\]]+\]\s*", "", line)
                            for line in transcript_lines
                        ]
                    else:
                        transcript_lines = [
                            line if index == 0 else re.sub(r"^\[[^\]]+\]\s*", "", line)
                            for index, line in enumerate(transcript_lines)
                        ]
                    transcript_text = "\n".join(transcript_lines)
                if export_flags["include_transcripts"] or export_flags["include_word"] or export_flags["include_pdf"] or export_flags["include_webpage"] or export_flags["include_scorm"] or export_flags["include_outline"]:
                    transcript_path.write_text(transcript_text + "\n", encoding="utf-8")
                chapter_files.append({
                    "image": image_path,
                    "clip": clip_path,
                    "transcript": transcript_path,
                    "outline_points": [],
                    "slide_subparts": [
                        scene for scene in scene_chapters
                        if chapter["start"] <= export_slide_start(scene)
                        and (chapter["end"] is None or export_slide_start(scene) < chapter["end"])
                    ],
                })
                files = chapter_files[-1]
                files["slide_content"] = (
                    chapter_slide_content(chapter, scene_chapters) if include_slide_text else []
                )
                needs_structure = any(export_flags[name] for name in (
                    "include_word", "include_pdf", "include_webpage", "include_outline", "include_scorm",
                    "include_section_clips",
                ))
                if needs_structure:
                    chapter_slides = [
                        export_slide_start(scene) for scene in files["slide_subparts"]
                    ] if subpart_mode in {"slides", "both"} else []
                    ai_extras = {}
                    saved_points = saved_section_points(chapter) if not chapter_slides else []
                    if saved_points:
                        # Sections were already written when chapters were generated; reuse them.
                        step(f"Part {number}/{len(chapter_data)}: using the saved chapter sections")
                        points, point_method = saved_points, "Saved chapter sections"
                        chapter["summary"] = chapter.get("summary")
                    else:
                        step(f"Part {number}/{len(chapter_data)}: generating title, summary and key points")
                        points, point_method, chapter["summary"] = await export_key_points(
                            chapter_transcript, [chapter["start"], *chapter_slides], ai_extras,
                        )
                    if ai_extras.get("title"):
                        chapter["original_title"] = chapter["title"]
                        chapter["title"] = ai_extras["title"]
                    if subpart_mode in {"points", "both"}:
                        files["outline_points"], files["point_method"] = points, point_method
                    else:
                        files["point_method"] = "Slide-based sections"
                    files["subparts"] = build_export_subparts(
                        chapter, chapter_transcript, files["outline_points"], scene_chapters, subpart_mode,
                        ai_extras.get("section_titles"),
                    )
                    for subpart in files["subparts"]:
                        subpart["transcript"] = "\n".join(
                            format_export_transcript_item(item) if timestamp_mode == "original"
                            else re.sub(r"^\[[^\]]+\]\s*", "", format_export_transcript_item(item))
                            for item in subpart["sentences"]
                        )
                        for scene in subpart["slides"]:
                            slide_start = export_slide_start(scene)
                            scene["image"] = temp_dir / f"slide_{int(slide_start * 1000):010d}.jpg"
                            if not scene["image"].is_file() and any(export_flags[name] for name in (
                                "include_webpage", "include_scorm", "include_word", "include_pdf", "include_outline",
                            )):
                                prepare_export_image(
                                    video_id, video_path, slide_start, scene_chapters,
                                    scene["image"], image_cache, step,
                                )
                    if export_flags["include_section_clips"] and len(files["subparts"]) > 1:
                        for subpart_index, subpart in enumerate(files["subparts"], start=1):
                            section_end = (
                                files["subparts"][subpart_index]["start"]
                                if subpart_index < len(files["subparts"]) else chapter["end"]
                            )
                            section_start = max(0.0, subpart["start"] - overlap_seconds)
                            section_clip = temp_dir / f"{base_name}_{subpart_index:02d}.mp4"
                            command = [
                                media_executable("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                                "-ss", str(section_start), "-i", str(video_path),
                            ]
                            if section_end is not None:
                                command.extend(["-t", str(max(0.1, section_end - section_start))])
                            command.extend(["-c:v", "libx264", "-c:a", "aac", str(section_clip)])
                            step(f"Part {number}/{len(chapter_data)}: encoding section {number}.{subpart_index} clip")
                            subprocess.run(command, check=True)
                            subpart["clip"] = section_clip
                chaptered_lines.extend([
                    f"## Part {number}: {chapter['title']}",
                    f"Start: {format_chapter_timestamp(chapter['start'])}",
                    "",
                    transcript_text,
                    "",
                ])

            if export_flags["include_transcripts"]:
                (temp_dir / "transcript_by_chapter.md").write_text("\n".join(chaptered_lines), encoding="utf-8")
            if export_flags["include_transcripts"]:
                (temp_dir / "chapters.json").write_text(json.dumps(chapter_data, indent=2), encoding="utf-8")
            export_rtl = is_rtl_text(
                " ".join(item.get("text", "") for item in transcript[:200]), transcript_language,
            )
            if export_flags["include_word"] or export_flags["include_pdf"]:
                step("Building Word/PDF documents")
                if export_rtl and export_flags["include_pdf"] and pdf_rtl_support() is None:
                    step("PDF: right-to-left text support is missing (install arabic-reshaper and python-bidi)")
            create_combined_chapter_documents(
                chapter_data, chapter_files,
                temp_dir / "chapter_document.pdf" if export_flags["include_pdf"] else None,
                temp_dir / "chapter_document.docx" if export_flags["include_word"] else None,
                pdf_text=export_flags["include_pdf"], pdf_images=export_flags["include_pdf"],
                word_text=export_flags["include_word"], word_images=export_flags["include_word"],
                document_title=document_title,
                document_subtitle=document_subtitle,
                timestamp_mode=timestamp_mode,
                rtl=export_rtl,
            )
            if export_flags["include_webpage"] or export_flags["include_scorm"]:
                step("Building HTML webpage")
                show_section_times = timestamp_mode != "part"

                def seek_link(clip_index, clip_start, seconds, label, css_class="seek"):
                    offset = max(0.0, seconds - clip_start)
                    clip = html.escape(chapter_files[clip_index - 1]["clip"].name, quote=True)
                    return (
                        f"<a class=\"{css_class}\" href=\"{clip}#t={offset:.3f}\" "
                        f"data-video=\"video-{clip_index}\" data-t=\"{offset:.3f}\">{label}</a>"
                    )

                html_language = html.escape(transcript_language or ("ar" if export_rtl else "en"))
                html_direction = ' dir="rtl"' if export_rtl else ""
                webpage_parts = [
                    f"<!doctype html><html lang=\"{html_language}\"{html_direction}><head><meta charset=\"utf-8\">",
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
                    f"<title>{html.escape(document_title)}</title>",
                    "<style>:root{--brand:#4f46e5;--text:#1f2330;--muted:#5d6475;--line:#e3e6ee;--soft:#f5f6fb}"
                    "*{box-sizing:border-box}body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;"
                    "max-width:1000px;margin:0 auto;padding:2rem 1rem 4rem;color:var(--text);line-height:1.55}"
                    "h1{font-size:1.9rem;margin:0 0 .25rem}h2{font-size:1.35rem;margin:0}h3{font-size:1rem;margin:0;display:inline}"
                    ".subtitle{color:var(--muted);margin:0 0 1.5rem}"
                    ".outline{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:1rem 1.25rem;margin:1.5rem 0 2rem}"
                    ".outline h2{font-size:1.05rem;margin-bottom:.5rem}.outline ol{margin:0;padding-inline-start:1.4rem}"
                    ".outline li{margin:.2rem 0}.outline ol ol{list-style:none;padding-inline-start:1rem;font-size:.92rem}"
                    ".outline a{color:var(--text);text-decoration:none}.outline a:hover{color:var(--brand);text-decoration:underline}"
                    ".outline-head{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin-bottom:.5rem}"
                    ".outline-head h2{margin:0}.outline-toggle{font:inherit;font-size:.82rem;color:var(--brand);background:none;"
                    "border:1px solid var(--line);border-radius:6px;padding:.15rem .6rem;cursor:pointer}"
                    ".outline-chapter>summary{cursor:pointer}.outline-chapter>summary::marker{color:var(--brand)}"
                    ".outline-chapter>ol{margin:.2rem 0 .4rem}"
                    ".slide-content{margin:1rem 0;padding:.5rem 1rem;background:var(--soft);border-radius:10px}"
                    ".slide-content>summary{font-weight:600;cursor:pointer;color:var(--brand)}"
                    ".slide-content h4{font-size:.95rem;margin:.75rem 0 .2rem}.slide-content ul{margin:.2rem 0 .5rem}"
                    ".time{color:var(--muted);font-variant-numeric:tabular-nums;font-size:.85em;white-space:nowrap}"
                    "section.chapter{border-top:1px solid var(--line);padding:2rem 0;scroll-margin-top:1rem}"
                    "img,video{max-width:100%;display:block}video{width:100%;border-radius:10px;background:#000;margin:1rem 0}"
                    ".chapter-summary{font-size:1.05rem;margin:.75rem 0}"
                    "figure{margin:0}.chapter-heading,.subpart-heading{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}"
                    ".export-thumbnail{margin:.5rem 0;width:120px;flex-shrink:0}"
                    ".export-thumbnail img{width:120px;height:68px;object-fit:contain;margin:0;cursor:zoom-in;border-radius:6px;border:1px solid var(--line)}"
                    ".export-thumbnail figcaption{font-size:.78rem;color:var(--muted);overflow-wrap:anywhere;margin:.25rem 0}"
                    ".chapter-subparts{margin-block:1rem 0;margin-inline:1.5rem 0;padding-inline-start:1rem;border-inline-start:3px solid var(--line)}"
                    ".chapter-subparts>summary{font-weight:600;cursor:pointer;color:var(--brand)}"
                    ".subpart{margin-block:.35rem;margin-inline:1rem 0;padding:.35rem 0}.subpart>summary{cursor:pointer}"
                    ".subpart ul{margin:.5rem 0}.seek{color:var(--brand);text-decoration:none;font-size:.9rem;margin-inline-end:.75rem}"
                    ".seek:hover{text-decoration:underline}.transcript>summary{cursor:pointer;color:var(--muted);font-size:.9rem}"
                    "pre{white-space:pre-wrap;font-family:inherit;background:var(--soft);padding:.75rem 1rem;border-radius:8px;margin:.5rem 0}"
                    "@media print{details{display:block}video{display:none}}</style></head><body>",
                    f"<h1>{html.escape(document_title)}</h1>",
                    '<p id="media-help" role="alert" hidden>'
                    'Pictures or videos could not be loaded. If you downloaded a ZIP, use Extract All '
                    'and then open index.html from the extracted folder. Keep the HTML, JPG and MP4 files '
                    'together. You can also use Saved exports &gt; Open in the app.</p>',
                    "<script>function showMediaHelp(){document.getElementById('media-help').hidden=false;}"
                    "document.addEventListener('error',function(event){"
                    "if(event.target.matches&&event.target.matches('img,video'))showMediaHelp();},true);"
                    "document.addEventListener('DOMContentLoaded',function(){"
                    "document.querySelectorAll('img').forEach(function(image){"
                    "if(image.complete&&!image.naturalWidth)showMediaHelp();});"
                    "document.querySelectorAll('video').forEach(function(video){"
                    "if(video.error)showMediaHelp();});});</script>",
                ]
                if export_flags["include_scorm"]:
                    webpage_parts.insert(1, "<script src=\"scorm_api.js\"></script>")
                if document_subtitle:
                    webpage_parts.append(f"<p class=\"subtitle\">{html.escape(document_subtitle)}</p>")
                has_outline_sections = any(len(files.get("subparts", [])) > 1 for files in chapter_files)
                outline = ["<nav class=\"outline\" aria-label=\"Course outline\"><div class=\"outline-head\"><h2>Course outline</h2>"]
                if has_outline_sections:
                    outline.append(
                        "<button type=\"button\" class=\"outline-toggle\" onclick=\"var d=document.querySelectorAll("
                        "'.outline-chapter'),o=[].some.call(d,function(x){return !x.open});"
                        "d.forEach(function(x){x.open=o});this.textContent=o?'Collapse all':'Expand all'\">Expand all</button>"
                    )
                outline.append("</div><ol>")
                for chapter, files in zip(chapter_data, chapter_files):
                    chapter_link = (
                        f"<a href=\"#part-{chapter['index']}\">{html.escape(chapter['title'])}</a> "
                        f"<span class=\"time\">{format_chapter_timestamp(chapter['start'])}</span>"
                    )
                    if len(files.get("subparts", [])) > 1:
                        outline.append(
                            f"<li><details class=\"outline-chapter\"><summary>{chapter_link}</summary><ol>" + "".join(
                                f"<li><a href=\"#part-{chapter['index']}-{subpart_index}\">"
                                f"{chapter['index']}.{subpart_index} {html.escape(subpart['title'])}</a></li>"
                                for subpart_index, subpart in enumerate(files["subparts"], start=1)
                            ) + "</ol></details></li>"
                        )
                    else:
                        outline.append(f"<li>{chapter_link}</li>")
                outline.append("</ol></nav>")
                webpage_parts.extend(outline)
                for chapter, files in zip(chapter_data, chapter_files):
                    image_name = files["image"].name
                    clip_name = files["clip"].name
                    summary = chapter.get("summary")
                    webpage_parts.extend([
                        f"<section class=\"chapter\" id=\"part-{chapter['index']}\"><div class=\"chapter-heading\">",
                        export_thumbnail(image_name, chapter["title"], f"{chapter['index']}: "),
                        f"<div><h2 title=\"{html.escape(chapter['title'], quote=True)}\">Part {chapter['index']}: "
                        f"{html.escape(chapter['title'])}</h2>"
                        f"<span class=\"time\">{format_chapter_timestamp(chapter['start'])}</span></div></div>",
                    ])
                    if summary:
                        webpage_parts.append(f"<p class=\"chapter-summary\">{html.escape(summary)}</p>")
                    if files.get("slide_content"):
                        webpage_parts.append("<details class=\"slide-content\" open><summary>Slide content</summary>")
                        for slide in files["slide_content"]:
                            webpage_parts.append(
                                f"<h4>Slide {slide['number']} "
                                + seek_link(
                                    chapter["index"], chapter["clip_start"], max(slide["start"], chapter["start"]),
                                    f"({format_chapter_timestamp(slide['start'])})", "time",
                                )
                                + "</h4><ul>"
                                + "".join(f"<li>{html.escape(line)}</li>" for line in slide["lines"])
                                + "</ul>"
                            )
                        webpage_parts.append("</details>")
                    webpage_parts.extend([
                        f"<video id=\"video-{chapter['index']}\" controls preload=\"metadata\" src=\"{html.escape(clip_name)}\"></video>",
                        "<details class=\"chapter-subparts\" open><summary>Sections</summary>",
                    ])
                    for subpart_index, subpart in enumerate(files["subparts"], start=1):
                        subpart_timestamp = (
                            f" ({format_chapter_timestamp(subpart['start'])})"
                            if show_section_times else ""
                        )
                        webpage_parts.append(
                            f"<details class=\"subpart\" id=\"part-{chapter['index']}-{subpart_index}\">"
                            f"<summary><h3 title=\"{html.escape(subpart['title'], quote=True)}\">"
                            f"{chapter['index']}.{subpart_index} {html.escape(subpart['title'])}"
                            f"{subpart_timestamp}</h3></summary>"
                        )
                        if subpart["points"]:
                            webpage_parts.append("<ul>" + "".join(
                                f"<li>{html.escape(point['text'])}</li>" for point in subpart["points"]
                            ) + "</ul>")
                        webpage_parts.append("<div class=\"subpart-heading\">")
                        for scene in subpart["slides"]:
                            webpage_parts.append(export_thumbnail(
                                scene["image"].name, subpart["title"], f"{scene['image_index'] + 1}: ",
                            ))
                        if not subpart["slides"]:
                            webpage_parts.append(export_thumbnail(image_name, subpart["title"]))
                        webpage_parts.append("</div><p>")
                        webpage_parts.append(seek_link(
                            chapter["index"], chapter["clip_start"], subpart["start"],
                            f"&#9654; Play from {format_chapter_timestamp(subpart['start'])}",
                        ))
                        for scene in subpart["slides"]:
                            slide_start = export_slide_start(scene)
                            webpage_parts.append(seek_link(
                                chapter["index"], chapter["clip_start"], slide_start,
                                f"Slide {scene['image_index'] + 1}: {format_chapter_timestamp(slide_start)}",
                            ))
                        webpage_parts.append("</p>")
                        if subpart.get("clip"):
                            webpage_parts.append(
                                f"<video class=\"section-video\" controls preload=\"none\" "
                                f"src=\"{html.escape(subpart['clip'].name, quote=True)}\"></video>"
                            )
                        webpage_parts.append(
                            "<details class=\"transcript\"><summary>Transcript</summary>"
                            f"<pre>{html.escape(subpart['transcript'])}</pre></details></details>"
                        )
                    webpage_parts.append("</details></section>")
                webpage_parts.append(
                    "<script>document.addEventListener('click',function(event){"
                    "var link=event.target.closest('a[data-video]');if(!link)return;"
                    "var video=document.getElementById(link.dataset.video);if(!video)return;"
                    "event.preventDefault();video.currentTime=parseFloat(link.dataset.t)||0;"
                    "video.scrollIntoView({behavior:'smooth',block:'center'});video.play();});</script>"
                )
                webpage_parts.append("</body></html>")
                (temp_dir / "index.html").write_text("\n".join(webpage_parts), encoding="utf-8")
            if export_flags["include_outline"]:
                outline_document = Document()
                outline_document.add_heading(document_title, level=0)
                if document_subtitle:
                        outline_document.add_paragraph(document_subtitle, style="Subtitle")
                outline_document.add_heading("Video outline", level=1)
                for chapter, files in zip(chapter_data, chapter_files):
                        outline_document.add_heading(
                            f"Part {chapter['index']}: {chapter['title']}", level=2
                        )
                        outline_document.add_paragraph(
                            f"Timestamp: {format_chapter_timestamp(chapter['start'])}"
                        )
                        if chapter.get("summary"):
                            outline_document.add_paragraph(chapter["summary"], style="Intense Quote")
                        if files.get("slide_content"):
                            add_word_slide_content(outline_document, files["slide_content"], level=3)
                        if files["subparts"]:
                            for subpart_index, subpart in enumerate(files["subparts"], start=1):
                                outline_document.add_heading(
                                    f"{chapter['index']}.{subpart_index} {subpart['title']} "
                                    f"({format_chapter_timestamp(subpart['start'])})",
                                    level=3,
                                )
                                for point in subpart["points"]:
                                    outline_document.add_paragraph(
                                        point["text"],
                                        style="List Bullet 2",
                                    )
                                for scene in subpart["slides"]:
                                    outline_document.add_paragraph(
                                        f"Slide at {format_chapter_timestamp(export_slide_start(scene))}"
                                    )
                                    if scene["image"].is_file():
                                        try:
                                            outline_document.add_picture(str(scene["image"]), width=Inches(6.5))
                                        except UnrecognizedImageError:
                                            pass
                                outline_document.add_paragraph(subpart["transcript"] or "No spoken content.")
                        else:
                            outline_document.add_paragraph(
                                "No spoken content was available for this part.",
                                style="List Bullet 2",
                            )
                        supporting_heading = outline_document.add_heading(
                            "Picture and transcript",
                            level=3,
                        )
                        collapse_word_heading(supporting_heading)
                        image_path = files["image"]
                        if image_path.is_file():
                            try:
                                outline_document.add_picture(
                                    str(image_path), width=Inches(6.5)
                                )
                            except UnrecognizedImageError:
                                pass
                        outline_document.add_heading("Transcript", level=4)
                        outline_document.add_paragraph(
                            files["transcript"].read_text(encoding="utf-8").strip()
                            or "No transcript available for this part."
                        )
                if export_rtl:
                    make_word_rtl(outline_document)
                outline_document.save(str(temp_dir / "video_outline.docx"))
            if export_flags["include_scorm"]:
                (temp_dir / "scorm_api.js").write_text(
                        "window.addEventListener('load',()=>{"
                        "try{if(window.parent&&window.parent.API){window.parent.API.LMSInitialize('');"
                        "window.parent.API.LMSSetValue('cmi.core.lesson_status','completed');"
                        "window.parent.API.LMSCommit('');}}catch(e){console.warn('SCORM API unavailable',e);}});",
                        encoding="utf-8",
                )
                (temp_dir / "imsmanifest.xml").write_text(
                        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                        "<manifest identifier=\"slidedec-video\" version=\"1.2\" "
                        "xmlns=\"http://www.imsproject.org/xsd/imscp_rootv1p1p2\" "
                        "xmlns:adlcp=\"http://www.adlnet.org/xsd/adlcp_rootv1p2\" "
                        "xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" "
                        "xsi:schemaLocation=\"http://www.imsproject.org/xsd/imscp_rootv1p1p2 "
                        "http://www.imsglobal.org/xsd/imscp_rootv1p1p2.xsd "
                        "http://www.adlnet.org/xsd/adlcp_rootv1p2 "
                        "http://www.imsglobal.org/xsd/adlcp_rootv1p2.xsd\">"
                        "<organizations default=\"org1\"><organization identifier=\"org1\">"
                        f"<title>{html.escape(document_title)}</title><item identifier=\"item1\" "
                        "identifierref=\"resource1\"><title>Video lecture</title></item>"
                        "</organization></organizations><resources><resource identifier=\"resource1\" "
                        "type=\"webcontent\" adlcp:scormtype=\"sco\" href=\"index.html\">"
                        "<file href=\"index.html\"/><file href=\"scorm_api.js\"/>"
                        + "".join(
                            f"<file href=\"{html.escape(path.name)}\"/>"
                            for path in export_archive_paths(temp_dir, export_flags)
                            if path.suffix in {".jpg", ".mp4"}
                        )
                        + "</resource></resources></manifest>",
                        encoding="utf-8",
                )
            only_word = export_flags["include_word"] and not export_flags["include_pdf"]
            only_pdf = export_flags["include_pdf"] and not export_flags["include_word"]
            only_outline = export_flags["include_outline"] and not any(
                export_flags[name] for name in (
                    "include_word", "include_pdf", "include_webpage", "include_scorm",
                    "include_images", "include_transcripts", "include_clips", "include_section_clips",
                )
            )
            no_extra_files = not any(
                export_flags[name] for name in (
                    "include_images", "include_transcripts", "include_clips",
                    "include_webpage", "include_outline", "include_scorm", "include_section_clips",
                )
            )
            if no_extra_files and (only_word or only_pdf):
                document_name = "chapter_document.docx" if only_word else "chapter_document.pdf"
                requested_filename = str(options.get("export_filename", "")).strip()
                requested_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", requested_filename).strip("._-")
                if requested_filename:
                    suffix = ".docx" if only_word else ".pdf"
                    direct_export_path = export_root / (
                        requested_filename if requested_filename.lower().endswith(suffix)
                        else requested_filename + suffix
                    )
                else:
                    direct_export_path = export_root / f"{export_title}_{'word' if only_word else 'pdf'}.{'docx' if only_word else 'pdf'}"
                shutil.copy2(temp_dir / document_name, direct_export_path)
            elif only_outline:
                requested_filename = str(options.get("export_filename", "")).strip()
                requested_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", requested_filename).strip("._-")
                direct_export_path = export_root / (
                    (requested_filename if requested_filename.lower().endswith(".docx") else requested_filename + ".docx")
                    if requested_filename else f"{export_title}_outline.docx"
                )
                shutil.copy2(temp_dir / "video_outline.docx", direct_export_path)
            step("Packaging the selected documents and media")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for file_path in export_archive_paths(temp_dir, export_flags):
                    archive.write(file_path, file_path.name)
            if on_artifacts:
                on_artifacts(temp_dir, export_archive_paths(temp_dir, export_flags), document_title)

        if direct_export_path is not None:
            return FileResponse(
                direct_export_path,
                media_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    if direct_export_path.suffix == ".docx" else "application/pdf"
                ),
                filename=direct_export_path.name,
            )
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=zip_path.name
        )
    except HTTPException:
        raise
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"Chapter export failed: {error}")
    except Exception as error:
        error_type = type(error).__name__
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Chapter export failed unexpectedly ({error_type}): {error!r}"
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)