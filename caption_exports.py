"""Render a snapshot of the chosen transcript into a separate MP4."""
import asyncio
import html
import json
import math
import re
import subprocess
from pathlib import Path

from fastapi.responses import FileResponse
from ffmpeg_tools import media_executable


def validate_captions(transcript):
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("Choose a transcript with timed captions first.")
    captions = []
    for index, item in enumerate(transcript):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError(f"Caption {index + 1} must contain text and a start time.")
        start = item.get("start")
        duration = item.get("duration")
        for name, value in (("start", start), ("duration", duration)):
            if name == "duration" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Caption {index + 1} has an invalid {name}.")
        text = item["text"].strip()
        if text:
            captions.append({"start": start, "duration": duration, "text": text})
    if not captions:
        raise ValueError("The chosen transcript has no caption text.")
    return sorted(captions, key=lambda item: item["start"])


def _srt_time(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def caption_text(captions, video_duration, format="srt", burn=False):
    if format not in {"srt", "vtt"}:
        raise ValueError("Caption format must be srt or vtt.")
    cues = []
    following = video_duration
    next_starts = [video_duration] * len(captions)
    for index in range(len(captions) - 1, -1, -1):
        if index + 1 < len(captions) and captions[index + 1]["start"] > captions[index]["start"]:
            following = captions[index + 1]["start"]
        next_starts[index] = following
    for index, item in enumerate(captions):
        start = item["start"]
        if start >= video_duration:
            continue
        duration = item["duration"]
        end = min(video_duration, start + duration if duration else next_starts[index])
        if end <= start:
            continue
        # SRT supports markup and libass overrides; render transcript wording as text.
        text = html.escape(item["text"], quote=False)
        if burn:
            text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        text = re.sub(r"\s*\n\s*", " ", text)
        start_stamp, end_stamp = _srt_time(start), _srt_time(end)
        if start_stamp == end_stamp:
            continue
        if format == "vtt":
            start_stamp, end_stamp = start_stamp.replace(",", "."), end_stamp.replace(",", ".")
        cues.append(f"{len(cues) + 1}\n{start_stamp} --> {end_stamp}\n{text}\n")
    if not cues:
        raise ValueError("No caption timing falls within this video.")
    return ("WEBVTT\n\n" if format == "vtt" else "") + "\n".join(cues)


async def video_duration(video_path):
    probe = await asyncio.to_thread(
        subprocess.run,
        [media_executable("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(video_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if probe.returncode:
        raise RuntimeError(f"Could not inspect the video: {probe.stderr[-2000:]}")
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("Video duration is unavailable or invalid.") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration is unavailable or invalid.")
    return duration


def caption_filename(filename):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(filename or "video"))
    name = Path(name).stem.strip(" .")[:100] or "video"
    return name


async def render_captioned_video(video_path, captions, filename, workspace, step):
    duration = await video_duration(video_path)
    workspace = Path(workspace)
    (workspace / "captions.srt").write_text(caption_text(captions, duration, burn=True), encoding="utf-8")
    name = caption_filename(filename)
    output = workspace / f"{name}-captioned.mp4"
    step("Burning captions into a video copy; the original video is unchanged")
    command = [
        media_executable("ffmpeg"), "-nostdin", "-y", "-i", str(video_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", "subtitles=filename=captions.srt:force_style='FontName=Arial,FontSize=22,Outline=2,Shadow=1,MarginV=20',pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(output),
    ]
    with (workspace / "encoder.log").open("wb") as errors:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(workspace), stdout=asyncio.subprocess.PIPE, stderr=errors,
        )
        last_percent = -5
        try:
            while line := await process.stdout.readline():
                key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
                if key == "out_time_us":
                    percent = min(99, int(int(value) / 1000000 / duration * 100))
                    if percent >= last_percent + 5:
                        step(f"Burning captions: {percent}% of video encoded")
                        last_percent = percent
            returncode = await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    if returncode:
        detail = (workspace / "encoder.log").read_text(encoding="utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"Caption encoding failed. FFmpeg must include the subtitles/libass filter. {detail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not produce a captioned video.")
    step("Captioned MP4 encoded; saving download")
    return FileResponse(output, media_type="video/mp4", filename=output.name)
