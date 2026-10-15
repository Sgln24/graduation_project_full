"""Translate WebVTT text while preserving cue structure and metadata."""
import html
import json
import math
import re
import zipfile
from pathlib import Path

from fastapi.responses import FileResponse
from processors.summary_processor import TRANSLATION_MODELS

TIMESTAMP = r"(?:[0-9]{2,}:)?[0-5][0-9]:[0-5][0-9]\.[0-9]{3}"
TIMING = re.compile(rf"^({TIMESTAMP})[ \t]+-->[ \t]+({TIMESTAMP})(?:[ \t]+.*)?$")
TAG = re.compile(r"(<[^>\n]+>|\n)")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_BATCH_BYTES = 20 * 1024 * 1024
MAX_FILES = 30


def seconds(stamp):
    parts = stamp.split(":")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def parse_vtt(content):
    text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text)
    if not blocks or not re.fullmatch(r"WEBVTT(?:[ \t].*)?(?:\n.*)*", blocks[0]):
        raise ValueError("A UTF-8 WebVTT file must start with WEBVTT and a blank line.")
    segments = []
    replacements = []
    cue_count = 0
    for index, block in enumerate(blocks[1:], 1):
        if not block.strip() or re.match(r"^(?:NOTE(?:[ \t\n]|$)|STYLE(?:\n|$)|REGION(?:\n|$))", block):
            continue
        lines = block.split("\n")
        timing_index = 0 if "-->" in lines[0] else 1
        match = TIMING.fullmatch(lines[timing_index]) if len(lines) > timing_index else None
        if not match or len(lines) <= timing_index + 1:
            raise ValueError(f"Invalid cue in block {index + 1}.")
        start, end = map(seconds, match.groups())
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError(f"Cue in block {index + 1} must end after it starts.")
        cue_count += 1
        parts = TAG.split("\n".join(lines[timing_index + 1:]))
        for position in range(0, len(parts), 2):
            value = parts[position]
            if not value.strip():
                continue
            prefix = value[:len(value) - len(value.lstrip())]
            suffix = value[len(value.rstrip()):]
            segments.append({"start": start, "duration": end - start,
                             "text": html.unescape(value.strip())})
            replacements.append((index, parts, position, prefix, suffix))
        blocks[index] = ("\n".join(lines[:timing_index + 1]) + "\n", parts)
    if not segments:
        raise ValueError("The VTT file has no caption text to translate.")
    return {"blocks": blocks, "segments": segments, "replacements": replacements, "cues": cue_count}


def translated_vtt(parsed, translated):
    if not isinstance(translated, list) or len(translated) != len(parsed["segments"]):
        raise ValueError("Translation returned an incomplete VTT file.")
    for replacement, item in zip(parsed["replacements"], translated):
        _, parts, position, prefix, suffix = replacement
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ValueError("Translation returned empty or invalid caption text.")
        # Model text cannot create markup, timing lines or new cue blocks.
        value = " ".join(item["text"].split())
        parts[position] = prefix + html.escape(value, quote=False).replace("-->", "--&gt;") + suffix
    return "\n\n".join(
        block[0] + "".join(block[1]) if isinstance(block, tuple) else block
        for block in parsed["blocks"]
    ).rstrip() + "\n"


def output_filename(filename, language, used):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(filename).stem).strip(" .")[:100] or "captions"
    name = f"{stem}_{language.upper()}.vtt"
    number = 2
    while name.casefold() in used:
        name = f"{stem}_{language.upper()}_{number}.vtt"
        number += 1
    used.add(name.casefold())
    return name


async def translate_vtt_batch(files, target, model, processor, workspace, step, allow_fallback=True):
    output = Path(workspace) / f"Translated_VTT_{target.upper()}.zip"
    used = set()
    reports = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (filename, parsed) in enumerate(files, 1):
            step(f"File {index}/{len(files)}: {filename}")
            segments = parsed["segments"]
            def progress(update):
                step(f"File {index}/{len(files)}: {filename} - "
                     f"{update.get('message') or 'Translating'}")
            try:
                result = await processor.translate_transcript(
                    segments, target, model or None, on_progress=progress, allow_fallback=allow_fallback)
                content = translated_vtt(parsed, result["transcript"])
                archive.writestr(output_filename(filename, target, used), content.encode("utf-8"))
                reports.append({
                    "file": filename, "cue_count": parsed["cues"],
                    "text_fragments": len(parsed["segments"]), "translation_items": len(segments),
                    **{key: result.get(key) for key in ("provider", "model", "usage", "elapsed_seconds")},
                })
            except (RuntimeError, ValueError, KeyError) as error:
                raise RuntimeError(f"Could not translate {filename}: {error}") from error
        archive.writestr("translation-report.json", json.dumps(reports, ensure_ascii=False, indent=2))
    models = sorted({model for report in reports for model in (
        (report.get("usage") or {}).get("models") or [f"{report.get('provider', 'Unknown')} {report.get('model', '')}"]
    )})
    usages = [report["usage"] for report in reports if report.get("usage")]
    total = sum(usage["total_tokens"] for usage in usages)
    input_tokens = sum(usage["input_tokens"] for usage in usages)
    output_tokens = sum(usage["output_tokens"] for usage in usages)
    complete = len(usages) == len(reports) and all(usage["complete"] for usage in usages)
    has_usage = any(usage["reported_requests"] for usage in usages)
    tokens = (f"{total:,} tokens ({input_tokens:,} input, {output_tokens:,} output)"
              + ("" if complete else "; partial usage, some requests unreported")) if has_usage else "Token usage unavailable"
    character_requests = sum(usage.get("character_requests", 0) for usage in usages)
    if character_requests:
        submitted = sum(usage.get("submitted_characters", 0) for usage in usages)
        translated = sum(usage.get("translated_characters", 0) for usage in usages)
        characters = f"{submitted:,} characters submitted to translation services; {translated:,} successfully translated"
        tokens = characters + (f"; {tokens}" if any(usage.get("llm_requests", 0) for usage in usages) else "; no LLM tokens used")
    step(f"Translation result: Models tried: {', '.join(models)}. {tokens}.")
    step(f"All {len(files)} VTT files translated; preparing ZIP")
    return FileResponse(output, media_type="application/zip", filename=output.name)
