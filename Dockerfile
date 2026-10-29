# SlideDec — Slide-Video-Lecture-Analysis-Toolkit
# CPU-first Docker image. NVIDIA GPU acceleration requires separate host/runtime setup.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# FFmpeg is used for audio/video decoding. Tesseract is used for OCR.
# The additional shared libraries support OpenCV and numerical/ML packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ara \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies separately to improve Docker layer caching.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

# Copy the application source and browser assets.
COPY . .

EXPOSE 8000

# Bind to 0.0.0.0 inside the container so the published port is reachable.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
