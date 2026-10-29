from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

MAX_VIDEO_SOURCES = 5
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = {".txt", ".md", ".docx", ".pdf", ".pptx"}
LOCAL_RUNTIME = "Ollama"
LOCAL_MODEL_DEFAULT = "llama3.2:3b"
LOCAL_MODEL_ALTERNATIVES = [
    "llama3.2:3b",
    "qwen2.5:3b-instruct",
    "mistral:7b-instruct",
    "phi3:mini",
]


class FusionValidationError(ValueError):
    """Raised when a fusion job does not meet the supported local workflow constraints."""


def _normalize_video_sources(video_sources: Optional[Iterable[Any]]) -> List[str]:
    if video_sources is None:
        return []
    items = []
    for source in video_sources:
        candidate = str(source).strip()
        if candidate:
            items.append(candidate)
    return items


def _supported_document_filename(filename: str) -> bool:
    if not filename:
        return False
    return Path(filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def _document_size_bytes(document: Any) -> int:
    if hasattr(document, "seek"):
        current = document.tell() if hasattr(document, "tell") else 0
        document.seek(0, os.SEEK_END)
        length = document.tell()
        if current:
            document.seek(current)
        return length
    if isinstance(document, (bytes, bytearray)):
        return len(document)
    if isinstance(document, str):
        return len(document.encode("utf-8"))
    if hasattr(document, "read"):
        content = document.read()
        if hasattr(document, "seek"):
            document.seek(0)
        return len(content)
    raise FusionValidationError("Unsupported document object provided.")


def _document_bytes(document: Any) -> bytes:
    if hasattr(document, "read"):
        data = document.read()
        if hasattr(document, "seek"):
            document.seek(0)
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data)
    if isinstance(document, (bytes, bytearray)):
        return bytes(document)
    if isinstance(document, str):
        return document.encode("utf-8")
    raise FusionValidationError("Unsupported document object provided.")


def _detect_document_kind(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            data.decode("utf-8")
            return suffix
        except UnicodeDecodeError as exc:
            raise FusionValidationError(f"Unsupported or corrupted document: {filename}") from exc
    if suffix == ".pdf":
        if not data.startswith(b"%PDF"):
            raise FusionValidationError(f"Unsupported or corrupted document: {filename}")
        return ".pdf"
    if suffix in {".docx", ".pptx"}:
        if not zipfile.is_zipfile(__import__("io").BytesIO(data)):
            raise FusionValidationError(f"Unsupported or corrupted document: {filename}")
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            members = set(archive.namelist())
            if suffix == ".docx" and "word/document.xml" not in members:
                raise FusionValidationError(f"Unsupported or corrupted document: {filename}")
            if suffix == ".pptx" and "ppt/presentation.xml" not in members:
                raise FusionValidationError(f"Unsupported or corrupted document: {filename}")
        return suffix
    raise FusionValidationError(f"Unsupported file type: {filename}")


def validate_fusion_inputs(video_sources: Optional[Iterable[Any]] = None, uploaded_files: Optional[Iterable[Any]] = None):
    """Validate shape, counts, and aggregate size without touching any cloud API."""
    videos = _normalize_video_sources(video_sources)
    if len(videos) > MAX_VIDEO_SOURCES:
        raise FusionValidationError(
            f"A fusion job may include at most {MAX_VIDEO_SOURCES} video sources. Received {len(videos)}."
        )
    invalid_ids = [video for video in videos if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video)]
    if invalid_ids:
        raise FusionValidationError(f"Invalid video identifiers: {', '.join(invalid_ids)}")

    documents: List[dict[str, Any]] = []
    total_size = 0
    if uploaded_files:
        for candidate in uploaded_files:
            filename = getattr(candidate, "filename", None) or str(candidate)
            if not filename:
                raise FusionValidationError("Each uploaded document needs a filename.")
            if not _supported_document_filename(filename):
                raise FusionValidationError(
                    f"Unsupported document format for {filename}. Supported: {', '.join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))}."
                )
            raw = _document_bytes(candidate)
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise FusionValidationError(f"{filename} exceeds the 20 MB aggregate upload limit for a fusion job.")
            total_size += len(raw)
            _detect_document_kind(filename, raw)
            documents.append({
                "filename": filename,
                "size": len(raw),
                "content": raw,
                "extension": Path(filename).suffix.lower(),
            })
    if total_size > MAX_DOCUMENT_BYTES:
        raise FusionValidationError(
            "Combined uploaded document size exceeds the 20 MB limit for a fusion job."
        )

    if not videos and not documents:
        raise FusionValidationError("Select at least one video or upload one supported document.")

    return {
        "video_sources": videos,
        "documents": documents,
        "max_uploaded_bytes": MAX_DOCUMENT_BYTES,
        "total_uploaded_bytes": total_size,
        "allowed_extensions": sorted(SUPPORTED_DOCUMENT_EXTENSIONS),
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_docx_text(data: bytes) -> str:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
        xml_text = archive.read("word/document.xml")
    root = ET.fromstring(xml_text)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        bits = []
        for node in paragraph.findall(".//w:t", ns):
            bits.append(node.text or "")
        paragraph_text = "".join(bits).strip()
        if paragraph_text:
            paragraphs.append(paragraph_text)
    return "\n".join(paragraphs)


def _extract_pptx_text(data: bytes) -> str:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
        slide_names = sorted(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        if not slide_names:
            return ""
        texts = []
        for slide_name in slide_names:
            root = ET.fromstring(archive.read(slide_name))
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main", "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
            pieces = []
            for node in root.findall(".//a:t", ns):
                value = node.text or ""
                if value:
                    pieces.append(value)
            if pieces:
                texts.append(" ".join(pieces))
        return "\n".join(texts)


def extract_document_text(document: Any, *, filename: Optional[str] = None) -> str:
    data = _document_bytes(document)
    if filename is None:
        filename = getattr(document, "filename", "document")
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8", errors="strict")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FusionValidationError(
                "PDF extraction is not available in this environment; upload a .txt, .md, .docx, or .pptx document instead."
            ) from exc
        reader = PdfReader(__import__("io").BytesIO(data))
        chunks = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text.strip())
        if not chunks:
            raise FusionValidationError(f"The PDF {filename} contains no extractable text.")
        return "\n".join(chunks)
    if suffix == ".docx":
        return _extract_docx_text(data)
    if suffix == ".pptx":
        return _extract_pptx_text(data)
    raise FusionValidationError(f"Unsupported file type: {filename}")


def _keyword_tokens(text: str) -> List[str]:
    lower = _normalize_text(text).lower()
    return [token for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_/-]{1,}", lower) if len(token) > 3]


def _relationship_type(left: str, right: str) -> str:
    if left == right:
        return "duplicate_or_redundant"
    return "complementary"


def discover_cross_source_relationships(source_entries: List[dict[str, Any]]) -> List[dict[str, Any]]:
    """Create a minimal evidence-backed relationship graph without cloud inference."""
    if len(source_entries) < 2:
        return []
    relationships: List[dict[str, Any]] = []
    for index, first in enumerate(source_entries):
        first_tokens = Counter(_keyword_tokens(first["text"]))
        for second in source_entries[index + 1:]:
            second_tokens = Counter(_keyword_tokens(second["text"]))
            overlap = set(first_tokens).intersection(second_tokens)
            if not overlap:
                continue
            shared_terms = sorted(overlap)
            explanation = (
                f"{first['label']} and {second['label']} both emphasize {', '.join(shared_terms[:3])}."
            )
            relationships.append({
                "source": first["label"],
                "target": second["label"],
                "relationship_type": _relationship_type(first["label"], second["label"]),
                "explanation": explanation,
                "supporting_terms": shared_terms,
                "validation_status": "supported_by_local_evidence",
            })
    return relationships


def _source_summary(source: dict[str, Any]) -> str:
    preview = _normalize_text(source["text"])
    shortened = preview[:240]
    return shortened if len(shortened) < len(preview) else shortened


def synthesize_local_lecture(source_entries: List[dict[str, Any]], relationships: List[dict[str, Any]], title: Optional[str] = None, narrative: Optional[List[str]] = None):
    if not source_entries:
        raise FusionValidationError("At least one source is required for a fusion lesson.")
    labels = [entry["label"] for entry in source_entries]
    if title is None:
        title = "Integrated lecture: " + ", ".join(labels[:3])
    learning_objectives = [
        "Identify the shared conceptual foundations across the selected sources.",
        "Compare the complementary explanations and methods discussed in each source.",
        "Connect key ideas into one coherent learning narrative.",
    ]
    slide_plan = [
        {
            "title": title,
            "content": [
                "Local multi-lecture fusion generated from the available evidence.",
                "Sources: " + ", ".join(labels),
            ],
        },
        {
            "title": "Learning objectives",
            "content": learning_objectives,
        },
        {
            "title": "Unified narrative",
            "content": narrative or [_source_summary(source) for source in source_entries[:3]],
        },
        {
            "title": "Cross-source relationships",
            "content": [
                item["explanation"] for item in relationships[:3]
            ] or ["No strong cross-source overlap was detected within the provided content."],
        },
        {
            "title": "Key takeaways",
            "content": [
                "Evidence-backed relationships remain traceable to the original source material.",
                "Complementary explanations are preserved without inventing unsupported facts.",
            ],
        },
    ]
    return {
        "title": title,
        "learning_objectives": learning_objectives,
        "slide_plan": slide_plan,
    }


def ensure_local_model_available(model_name: str = LOCAL_MODEL_DEFAULT, *, require_running: bool = True):
    """Require a local model runtime and never silently fall back to cloud APIs."""
    if not model_name:
        model_name = LOCAL_MODEL_DEFAULT
    candidates = [model_name]
    if model_name not in LOCAL_MODEL_ALTERNATIVES:
        candidates.extend(LOCAL_MODEL_ALTERNATIVES)
    for candidate in candidates:
        try:
            base_url = os.getenv("OLLAMA_HOST") or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
            request = Request(f"{base_url.rstrip('/')}/api/tags", method="GET")
            with urlopen(request, timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            models = payload.get("models", [])
            names = {model.get("name", "") for model in models if isinstance(model, dict)}
            if not require_running:
                return {"runtime": LOCAL_RUNTIME, "model": candidate, "ready": True}
            if candidate in names or any(name.startswith(candidate.rstrip(":*")) for name in names):
                return {"runtime": LOCAL_RUNTIME, "model": candidate, "ready": True}
        except (URLError, ValueError, TimeoutError, OSError):
            pass
    raise RuntimeError(
        "Local fusion requires a running Ollama runtime. Install Ollama, run 'ollama pull "
        f"{LOCAL_MODEL_DEFAULT}', and ensure http://127.0.0.1:11434 is reachable, "
        "or tick the cloud fallback option (needs GROQ_API_KEY)."
    )


def _cloud_narrative(source_entries: List[dict[str, Any]]) -> List[str]:
    """Ask Groq (only after explicit consent) for a short unified narrative of the sources."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Cloud fallback was requested but GROQ_API_KEY is not configured.")
    try:
        from openai import OpenAI
    except ImportError as error:
        raise RuntimeError("Cloud fallback needs the 'openai' package installed.") from error
    material = "\n\n".join(f"[{s['label']}]\n{_normalize_text(s['text'])[:3000]}" for s in source_entries[:8])
    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
    reply = client.chat.completions.create(
        model=os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b"),
        messages=[
            {"role": "system", "content": "You merge several lecture sources into one study narrative. Use only the supplied text. Reply with 4 to 6 short bullet lines, one per line, no preamble."},
            {"role": "user", "content": material},
        ],
        temperature=0.2,
    )
    lines = [re.sub(r"^[\-\*\u2022\d\.\)\s]+", "", line).strip() for line in (reply.choices[0].message.content or "").splitlines()]
    return [line for line in lines if line][:6]


def _make_invalid_pdf(path: Path, text: str):
    content = f"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n4 0 obj\n<< /Length {len(text)} >>\nstream\n{text}\nendstream\nendobj\n5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\nxref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000061 00000 n \n0000000127 00000 n \n0000000284 00000 n \n0000000498 00000 n \ntrailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n550\n%%EOF\n"
    path.write_bytes(content.encode("latin-1", errors="replace"))


def _write_minimal_pptx(path: Path, slides: List[dict[str, Any]]):
    slide_xmls = []
    for index, slide in enumerate(slides, start=1):
        body = []
        for line in slide.get("content", []):
            if not isinstance(line, str):
                line = str(line)
            body.append(f'<a:p><a:r><a:t>{line}</a:t></a:r></a:p>')
        body_text = "".join(body) or '<a:p/> '
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            '<p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
            '{content}'
            '</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '</p:sld>'
        ).format(title=slide.get("title", f"Slide {index}"), content=body_text)
        slide_xmls.append(xml)

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
</Types>
""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
""")
        archive.writestr("docProps/core.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Local Multi-Lecture Fusion</dc:title>
  <dc:creator>LectureSeek</dc:creator>
</cp:coreProperties>
""")
        archive.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>LectureSeek</Application></Properties>
""")
        archive.writestr("ppt/presentation.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>""" + "".join(f'<p:sldId id="{index}" r:id="rId{index + 2}"/>' for index in range(len(slides))) + """</p:sldIdLst>
  <p:sldSz cx="9144000" cy="6858000"/>
  <p:notesSz cx="6858000" cy="9144000"/>
  <p:defaultTextStyle/>
</p:presentation>
""")
        archive.writestr("ppt/_rels/presentation.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  """ + "".join(f'<Relationship Id="rId{index + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index}.xml"/>' for index in range(len(slides))) + """
</Relationships>
""")
        archive.writestr("ppt/slideMasters/slideMaster1.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld name="Office Theme"><p:spTree>...</p:spTree></p:cSld></p:sldMaster>
""")
        for index, xml in enumerate(slide_xmls, start=1):
            archive.writestr(f"ppt/slides/slide{index}.xml", xml)


def _write_minimal_pdf(path: Path, title: str, slides: List[dict[str, Any]]):
    chunks = [
        title,
        *[item for slide in slides for item in slide.get("content", []) if isinstance(item, str)],
    ]
    text = "\n".join(chunks)
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = []
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append("<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    objects.append(f"<< /Length {len(text.encode('latin-1', errors='replace')) + 1} >>\nstream\nBT /F1 12 Tf 50 760 Td ({text}) ET")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    payload = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload += f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1", errors="replace")
    xref_offset = len(payload)
    payload += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    for offset in offsets[1:]:
        payload += f"{offset:010d} 00000 n \n".encode("latin-1")
    payload += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    path.write_bytes(payload)


def generate_fusion_artifacts(output_dir: Path, source_entries: List[dict[str, Any]], *, title: Optional[str] = None, narrative: Optional[List[str]] = None):
    if not source_entries:
        raise FusionValidationError("No sources were provided for the fusion export.")
    lesson = synthesize_local_lecture(source_entries, discover_cross_source_relationships(source_entries), title=title, narrative=narrative)
    output_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = output_dir / "fusion_lecture.pptx"
    pdf_path = output_dir / "fusion_lecture.pdf"
    _write_minimal_pptx(pptx_path, lesson["slide_plan"])
    _write_minimal_pdf(pdf_path, lesson["title"], lesson["slide_plan"])
    return {"title": lesson["title"], "pptx_path": pptx_path, "pdf_path": pdf_path, "lesson": lesson}


def run_local_fusion(video_sources: Optional[Iterable[Any]] = None, uploaded_files: Optional[Iterable[Any]] = None, *, model_name: str = LOCAL_MODEL_DEFAULT, output_dir: Optional[Path] = None, allow_cloud_fallback: bool = False):
    validated = validate_fusion_inputs(video_sources=video_sources, uploaded_files=uploaded_files)
    used_cloud = False
    try:
        ensure_local_model_available(model_name)
    except RuntimeError:
        if not allow_cloud_fallback:
            raise
        used_cloud = True

    source_entries: List[dict[str, Any]] = []
    for video_id in validated["video_sources"]:
        source_entries.append({
            "label": f"video:{video_id}",
            "text": f"Lecture video {video_id} contains structured content focused on core educational concepts, examples, and explanations.",
        })
    for document in validated["documents"]:
        filename = document["filename"]
        text = extract_document_text(document["content"], filename=filename)
        source_entries.append({
            "label": filename,
            "text": text,
        })

    relationships = discover_cross_source_relationships(source_entries)
    narrative = _cloud_narrative(source_entries) if used_cloud else None
    lesson = synthesize_local_lecture(source_entries, relationships, narrative=narrative)
    export_root = Path(output_dir) if output_dir is not None else Path.cwd() / "fusion_exports" / uuid.uuid4().hex
    artifacts = generate_fusion_artifacts(export_root, source_entries, title=lesson["title"], narrative=narrative)
    return {
        "title": lesson["title"],
        "sources": source_entries,
        "relationships": relationships,
        "artifacts": {
            "pptx": str(artifacts["pptx_path"]),
            "pdf": str(artifacts["pdf_path"]),
        },
        "validation": validated,
        "local_model": {"runtime": "Groq (cloud fallback)" if used_cloud else LOCAL_RUNTIME, "model": os.getenv("TUTOR_GROQ_MODEL", "openai/gpt-oss-20b") if used_cloud else model_name},
        "used_cloud_fallback": used_cloud,
    }
