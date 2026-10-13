# LectureSeek - Lecture Video Analysis

[![ACM MM 2025](https://img.shields.io/badge/ACMMM-2025-1B427D)](https://dl.acm.org/doi/pdf/10.1145/3746027.3756873) 
[![Python](https://img.shields.io/badge/python-3.7+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.68+-green.svg)](https://fastapi.tiangolo.com)

## News
[31.10.2025] We got **Best Open-Source SW Paper Award at ACM Multimedia 2025**!

[03.07.2025] Video Lecture Analysis Toolkit got accepted **ACM Multimedia 2025 Open Source Track**!

## Overview

**LectureSeek** is a lecture video analysis platform for educational content processing, particularly slide-based lecture videos and presentations. It works with uploaded video recordings; YouTube import is an optional source, not the application's purpose. The system combines computer vision, natural language processing, and machine learning techniques to provide automated video analysis, transcript generation, slide text extraction, and content relationships discovery.

![Demo](demo.gif)

## About this fork

This repository builds on [NimaZahedi's Slide-Video-Lecture-Analysis-Toolkit](https://github.com/NimaZahedi/Slide-Video-Lecture-Analysis-Toolkit), which is itself based on [travisseng/svla-toolkit](https://github.com/travisseng/svla-toolkit). The sections below distinguish early fixes needed to run the inherited project from fixes and development added later in this repository.

Compared with the shared base commit `94750bc`, this repository had **190 commits not present in its parent fork's shared history**, as of `3222940` (72 files changed, with 18,828 additions and 1,513 deletions). This count includes maintenance and iterative fixes; it is not a count of 190 separate features. The initial compatibility work below is part of that total.

### Initial fixes to the inherited project

The first changes addressed compatibility and runtime problems in the code inherited by this fork:

- Updated Gemini and PySceneDetect integration, and adapted FastAPI/Starlette template responses to the installed API.
- Fixed Gemini credential initialization for Colab Secrets and local environment variables.
- Made Whisper device selection account for the available hardware instead of assuming a GPU.
- Corrected runtime paths and GPU assumptions across video, scene, OCR, transcript, embedding, and export processing.
- Updated dependencies and addressed early performance, security, and compatibility issues.

These are the changes specifically described as fixes to the inherited project. Later fixes listed below address problems encountered or introduced while developing this fork; they should not be read as claims about bugs in the original project.

### Recent fixes in this repository

- Preserve existing chapters when chapter generation fails, and recover from malformed model-generated JSON before accepting or saving a result.
- Avoid losing transcript work during speaker processing; show transcription progress and allow users to stop or browse other saved transcripts.
- Prevent outdated OCR controls from appearing and keep the primary and optional OCR passes in the correct order.
- Keep transcript translations, chapter titles, timeline labels, and export suggestions consistent with the selected video and language.
- Improve saved-video loading, Unicode handling, export media paths, section titles, and generated filenames.
- Group sections around genuine topic changes without imposing an arbitrary section-count limit.

### Improvements over the inherited project

- More observable and recoverable processing: clearer progress, retries for recoverable failures, and controls to stop or resume browsing while work continues.
- More flexible slide analysis: configurable detection modes and detail, diagnostics, optional OCR, and slide-aware navigation.
- More useful course exports: selectable grouping and formats, sentence-aware boundaries, persistent export jobs, progress reporting, and saved-export links.
- A more adaptable interface with a compact responsive layout, resizable panels, collapsible options, and synchronized transcript, chapter, slide, and timeline navigation.

### Model and provider changes

- Added Groq and OpenAI fallbacks for chapter generation and translation, with explicit model choices where supported.
- Added hosted transcription choices alongside local Whisper, and selected local Whisper settings based on available hardware.
- Updated model compatibility as providers changed their APIs and retired models; automatic Gemini fallbacks now use Gemini 3.5 Flash-Lite rather than retired Gemini 2.5 models.
- Added fallback handling for provider quotas and temporary unavailability. Provider outages or exhausted quotas can still prevent AI work.

### New capabilities added in this fork

- Upload, translate, regenerate, select, and retain versions of transcripts.
- Generate transcript-language chapters with topic sections, summaries, and optional slide titles.
- Detect slides independently of transcript generation; run OCR, download slide images, and export OCR text.
- Identify speakers without retranscribing, with controls and progress for speaker detection.
- Create grouped HTML, Word, PDF, SCORM, transcript, and media exports, and retain completed exports for later viewing or download.

**In short:** this fork aims to be a more practical end-to-end lecture workflow: it addresses early environment compatibility, gives users more control over analysis, helps preserve work when processing fails, and connects transcripts, translations, chapters, slides, and exports in one interface.

### Key Features

- **Scene Detection**: Content-cut detection by default, plus adaptive slide detection with configurable detail, duration, duplicate filtering, hourly limits, and optional transcript chapter boundaries
- **Slide Content in Exports**: Optionally add each slide's extracted text (OCR) as a bullet list under its chapter in HTML, Word, PDF and outline exports
- **Visual Timeline**: Thumbnails follow the Navigate by setting (content chapters, slide changes, or both); the minimum slide duration is a percentage of the video length (2 s floor)
- **Scene Names**: Slide Changes shows the existing timeline topic name next to
  each scene number when available, preferring the specific section title and
  falling back to the chapter title, without another AI request.
  **Add / refresh slide titles** applies the same titles after slides were detected
  earlier. If chapters are missing, it first generates them from the open transcript;
  it never reruns slide detection or OCR.
- **Optional Chapter Slides**: Chapter generation uses the transcript only.
  Enable **Add detected slides** afterwards to show existing slide images under
  chapters and sections; this does not detect slides, run OCR, or regenerate chapters.
- **Dual OCR Systems**: Run Start Slide OCR first to read detected text with Tesseract. Once that pass completes with readable results and no pending or failed text elements, Process with Surya OCR becomes available as an optional additional pass.
- **Multi-Source Transcription**: YouTube transcript extraction and Whisper-based speech recognition
- **Burned-in Caption Video**: Select a transcript or translation from the saved
  versions, then click **Add captions to video** in the Transcript toolbar.
  Its **Transcript for captions** dropdown uses the same saved versions; changing
  it also changes the transcript shown on screen.
  Choose the appearance, inspect the preview, then click **Create video copy**.
  The current transcript is captured when you create the copy, so later version
  changes do not alter the export. **Previously generated captioned videos** at
  the top of **Add captions to video** is a dropdown listing copies from all lectures, newest first, with
  filenames and creation dates. Only completed copies appear in the dropdown;
  the newest is selected automatically. Click **Download MP4** to save the selected
  copy with the app's save-file workflow; the refresh button reloads the list.
  Below the creation controls, only the current export's progress is shown,
  followed by a **Download MP4** button when it finishes. Opening the dialog
  reconnects to an active export, without repeating old completed or failed jobs.
  The original lecture
  does not need to be selected. Copies also remain in **Saved exports**.
  Jobs and completed downloads remain available after a page
  reload; a server restart interrupts unfinished encoding. The original video is
  never overwritten. Choose white-on-black-box, white outlined, or yellow outlined
  captions. New video filenames include the caption language as an uppercase
  two-letter code, for example `Lecture_DE caption.mp4` or `Lecture_EN caption.mp4`.
  The language comes from the selected translation or the existing transcript-language
  detector, without a separate language selector. Older saved copies keep their original names.
  Choose small/medium/large text and top/bottom positioning in that dialog.
  The default is medium white text in a black box, at the bottom.
  This dialog shows a real FFmpeg-rendered preview on the selected video's
  frame before exporting. **Open large preview at current video time** opens a
  large, viewport-fitted view; closing it returns to the side-panel options.
  Preview captions scale with the displayed video frame; a small thumbnail is
  not the final video's absolute text size. The enlarged preview uses the same
  caption style and wrapping as the exported MP4.
  Opening the dialog or changing options does not start a video export.
  Captions use safe margins and short, two-line blocks rather than large paragraphs.
  Long cues are split without dropping words; their sub-cue timing is estimated
  proportionally from the source cue duration, not newly recognized word timings.
  SRT/WebVTT files retain the original transcript cues. Preview and MP4 encoding
  use the same style and wrapping code;
  rendering requires FFmpeg with the subtitles/libass filter and installed fonts
  for the selected language. No transcription, translation, or AI call is made.
  The separate **Download transcript** toolbar action opens a format chooser for
  **SRT** and **WebVTT**,
  using UTF-8 text and the selected version's timestamps. These files are quick
  sidecar downloads, not burned-in video. Missing/zero cue durations extend to the
  next distinct cue start (or video end); cues beyond the video end are excluded.
- **Processing Controls**: Stop transcription/speaker detection and restore the
  previous transcript, or keep processing in the background while browsing saved
  transcripts. Selecting a saved version prevents progress polling from replacing it.
  Reopening a saved video reconnects to the running job's steps and refreshes
  the saved-transcript selector even when the current transcript is still processing.
- **Saved Video Loading**: Saved transcripts and scenes are read as UTF-8,
  including Arabic text, without printing transcript content to the server console.
- **Speaker Identification**: Add speakers to the current transcript without
  re-transcribing. Interrupted speaker jobs restore the original transcript
  on server startup; worker initialization leaves active jobs untouched.
- **Gemini Transcript Formatting**: Structured JSON output with one visible retry
  per audio part if the response is unreadable; malformed output is not saved
  as a partial transcript.
- **Batched Translation**: Translate transcripts and chapter titles with an explicit Gemini/OpenAI model choice and visible provider/timing details
- **Semantic Embeddings**: Sentence-BERT based semantic similarity for transcript-OCR alignment
- **YOLO Object Detection**: Custom-trained YOLOv8 model for slide content detection (`slidevqa_best.pt`)
- **AI-Powered Summarization**: Google Gemini integration for chapter generation
- **Summary Provider Default**: Automatic summary generation prefers configured
  Groq, then Gemini and OpenAI; explicit model choices remain available.
- **Summary Language**: Chapter options default to the transcript's language,
  with English, German, French, Spanish and Arabic overrides.
- **Saved Transcript Versions**: Every generated or uploaded transcript and every
  translation is kept. Pick one from the list below the Transcript options button to use it again
  (summaries and exports follow the chosen version), or delete versions you no longer need.
- **Resizable Layout**: Drag the handles between the video, visual timeline and side
  panel to resize them; sizes are remembered and a double-click resets them.
- **Options State**: Options buttons show a downward indicator when closed and
  a highlighted **Open** state when expanded. Chapter options changes to
  **Hide chapter options** so it is clear how to collapse the inline panel.
- **Export Options**: Export Chapters opens the full options dialog immediately:
  grouping, fixed intervals, timestamps, sections, filenames and document formats.
  The default structure uses content chapters with key-point sections (slide changes
  are optional), numbered 1.1, 1.2 and timestamped at each part/section. Caption fragments are
  reassembled into sentences; internal part boundaries move to the next sentence
  start so no transcript words are lost or duplicated. These times are estimated
  from caption timing, while slide links retain the actual detected slide time.
  By default (option "Start each clip 1 second early"), each exported clip after the first starts up to one second early (clamped
  at the video's beginning), overlapping the previous clip to protect speech
  from small caption-timing errors. Logical chapter/transcript timestamps stay
  unchanged, and slide links account for the clip's earlier start.
  Unpunctuated Whisper phrases stay together until a timed speech pause or speaker
  turn; if neither timing nor punctuation identifies a safe break, the utterance
  remains intact rather than introducing arbitrary mid-sentence cuts.
  Each chapter also includes a concise summary in the transcript's language.
  The HTML export reads like a course outline: a linked outline at the top, AI-written
  part and section headings, direct summaries (no "the lecture explains that" filler),
  "Play from" links that seek the part's video, and collapsible transcripts.
  The app's **Navigate by** selector chooses content chapters, slide changes,
  or both for Previous/Next navigation and synchronizes with export grouping.
  Titles appear directly beneath timeline screenshots (with the timestamp
  beside them) and beneath the playing video; HTML
  exports repeat chapter titles beneath each screenshot and chapter clip.
  New AI chapter titles are short noun phrases (at most five words); display
  titles are capped at 36 characters with full titles on hover. HTML
  screenshots are small clickable thumbnails beside chapter/section headings;
  clicking opens the image at full size. Slide labels use numbers only.
  Exports reuse saved full-size slide screenshots at matching timestamps, at
  their saved resolution. New captures are made only for missing images or
  chapter starts that do not match a detected slide; duplicate timestamps share
  one capture within an export.
  The slide-detection tab also offers **Export slides as one PDF**: one saved
  full-size screenshot per page in detection order, with no transcript, clips,
  or AI requests. Missing screenshots produce an explicit error.
  Video controls and the timeline sit directly below the player. Playback
  auto-scrolls within the transcript and timeline panels, without scrolling
  the surrounding page away from the video.
  The compact desktop layout keeps controls, video, timeline, and transcript
  together at normal browser zoom. Buttons and transcript rows use tighter
  spacing, video height adapts to the viewport, and panels remain scrollable.
  Narrow screens stack the panels and retain normal page scrolling.
  The interface uses a modern light theme that follows the operating system's
  dark-mode setting automatically, with visible keyboard focus and reduced
  motion when the system requests it. Options controls stay neutral while
  processing, preview and download actions use
  accent-tinted buttons with an accent edge. Main start/save actions use solid
  accent buttons; stop/delete actions use a red treatment. Navigation and
  Cancel/Close controls remain neutral, so opening options is visually distinct
  from starting work.
  The right panel is narrow, and option dialogs and Settings open over it so
  the video stays visible. Caption previews, transcript downloads and saved
  caption settings use the same side-panel placement and width on desktop.
  Side-panel option dialogs are non-modal, so video controls and both resize
  handles remain usable while options are open. Escape closes the open options.
  Only the explicitly enlarged caption preview is modal.
  Narrow-screen popups share one position near the content panel, keeping at
  least 248 pixels of usable height when the panel starts near the screen bottom.
  Detection
  buttons stay on the playback toolbar row, becoming icon-only on smaller screens.
  In HTML, the chapter's sections and transcript are grouped in indented,
  collapsible sections beneath the chapter. Word, PDF, HTML and outline exports
  share this hierarchy. Without configured AI,
  points are explicitly labelled extractive full sentences; configured AI failures
  are reported instead of silently substituting fabricated summaries. Select
  slide-only sections to omit AI key-point sections; chapter summaries are still
  generated when AI is configured. HTML downloads as a ZIP
  containing `index.html` and its required media: extract the complete package and
  open `index.html` locally. TXT/Markdown/JSON transcript files are packaged only
  when **Transcript files** is selected; other explicitly chosen formats remain
  included. Content-only, slide-only and fixed-interval grouping remain available.
- **Web Interface**: FastAPI-based web application with real-time updates

## Interactive User Interface

The Video Lecture Analysis Toolkit features a web interface designed as an interactive dashboard for exploring analyzed lecture content with multiple synchronized views:

### Main Interface Components

**Video Player and Timelines**: The central video player includes an event timeline that marks slide transitions, chapter breaks, and search results. A visual timeline with clickable scene thumbnails provides navigation through the presentation's visual flow.

**Content Navigation Panel**: A vertical tabbed interface provides access to:
- **Transcript Tab**: Timestamped transcript with click-to-seek functionality
- **Chapters Tab**: AI-generated chapter markers for navigation  
- **Scene Changes Tab**: Log of detected scene transitions
- **Slide Content Tab**: OCR-extracted text from slides with temporal alignment

**Slide downloads**: Download an individual full-size JPG with the download icon
beside a slide in **Slide changes** or beneath a chapter/section in the sidebar.
The same download icon appears on detected-slide timeline cards, OCR slide
headers, and the enlarged slide view; downloading never seeks or selects a slide.
The default chapter-only timeline remains unchanged.
Chapter and section rows have a separate download icon for **all their slides**
(ZIP, including a `screenshots.json` timestamp manifest). These downloads follow
the nested slide list and never include slides from the next chapter/section.

**All slide text in Word**: In **Slide changes**, choose **Export all slide text
as Word**. One `.docx` contains chapter → section → slide headings and numbered
OCR text lists in chronological slide order. Existing OCR is reused; slides
without saved text are read automatically from their full-size screenshots using
Tesseract (install Tesseract or configure `TESSERACT_CMD`). This can take a few
minutes. Slides with no readable text remain in the document with an explicit
notice; failures are reported instead of silently omitting slides. With no
generated chapters, the document contains a single ordered Slides group.

**Integrated Search**: Search functionality that queries both spoken transcript and OCR-extracted visual text

### The Interactive Layer

**Content Customization**: Users can directly manipulate visual components overlaid on the video player:
- **Element Control**: Move, resize, or completely hide detected elements (presenter video, text boxes, images)
- **Accessibility Features**: Reduce visual clutter and cognitive load for neurodivergent users
- **Focus Enhancement**: Hide presenter to focus on slides, or enlarge specific diagrams

**Semantic Link Visualization**: 
- **Bidirectional Highlighting**: When transcript segments play, related slide text is highlighted
- **Interactive Exploration**: Clicking slide text boxes highlights corresponding transcript sentences
- **Cross-Modal Links**: Shows connections between spoken and visual information

### User Experience Features

- **Visual Clutter Reduction**: Customizable interface elements for cognitive load management
- **Multimodal Navigation**: Switch between audio, visual, and textual content modes


## Installation and Setup

### Prerequisites

- **Python>=3.10+** with pip package manager
- **CUDA-capable GPU** (recommended for optimal performance)
- **FFmpeg** for video processing
- **Tesseract OCR** for text extraction
- **Google API Key** for Gemini AI integration

### System Dependencies

**Ubuntu/Debian:**
```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-eng ffmpeg
```

**macOS:**
```bash
brew install tesseract ffmpeg
```

**Windows:**
- Install Tesseract from: https://github.com/UB-Mannheim/tesseract/wiki
- Install FFmpeg from: https://ffmpeg.org/download.html

### Python Environment Setup

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd slideDec
   ```

2. **Create virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

   Local Whisper and speaker identification decode audio with system FFmpeg
   and pass 16 kHz mono samples directly to their models, bypassing PyAV's file
   decoder. FFmpeg must be installed and available on `PATH`. Windows launches
   also check the installed user/system PATH and the Python environment's
   `Library/bin` directory, so an older VS Code process PATH does not hide a
   newly installed FFmpeg. Set `FFMPEG_BINARY` and `FFPROBE_BINARY` to executable
   paths if using a custom installation. These tools are shared by speaker
   detection, online transcription and chapter clip exports.

   Dependencies also constrain `av>=11,<19`: PyAV 19 removed the
   `metadata_errors` argument used by faster-whisper 1.2.1. If an existing
   environment reports `open() got an unexpected keyword argument
   'metadata_errors'`, reinstall the requirements and restart the application
   before retrying transcription. Updating the repository alone does not update
   installed packages.

### Windows local launch

Chapter exports default to the HTML webpage package. Word, PDF and other
formats are optional selections in the export dialog.
The export dialog immediately uses the current chapter topic or uploaded
filename while a whole-video title suggestion loads. Suggestions from an
older video or language cannot overwrite the current fields, and names you
type yourself are preserved.

Chapter sections follow topic changes rather than a fixed count. Balanced
groups related examples, steps and continuations of the same subject.
Topic grouping is requested in the initial generation, with no second AI
review or fixed cap on genuinely distinct topics. This applies when generating
chapters, not retroactively to saved chapters. Slide-based export sections
still follow the detected slide changes.

Automatic chapter and translation fallbacks include Gemini 3.5 Flash-Lite
instead of the retired Gemini 2.5 models. Provider quota limits and temporary
overload can still prevent generation; failures retain existing chapters and
include each provider's reason and retry timing when supplied.
Chapter responses are parsed before a transcript part is accepted. Malformed
JSON gets one corrective retry, then smaller transcript parts when possible
and a provider fallback if parsing still fails. Valid responses need no extra
AI calls; ambiguous extra JSON is never silently discarded.

For downloaded HTML exports, use **Extract All** on the ZIP and open
`index.html` from the extracted folder, not from inside the ZIP. Keep the
HTML, images and videos together. The page shows guidance if its media cannot
load. Alternatively, use **Saved exports > Open** in the app.
Opening speech before the first named section is included in that section's
transcript rather than turned into an extra section with a verbatim greeting
as its title.

Exports run in a background job with timestamped steps (clip encoding, images,
AI summaries, document building, packaging and saving). Export and
transcription progress show only the current step, replacing the previous one.
Saved exports include
an **Open HTML in browser** link and a download link in the Chapters tab.
HTML pages and their media remain under `static/exports/jobs` across app
restarts. Keep this directory to retain previous exports. In Colab, files
under `/content` still disappear when the runtime resets; download the package
or persist the exports directory in Drive. Older downloads created before
saved-export support are not automatically imported.

Intermediate export files use the operating system's temporary directory,
outside the saved exports folder, to avoid cloud-sync locks on Windows.
Temporary-file cleanup retries briefly; persistent cleanup failures are logged
and shown as warnings in export progress without invalidating saved output.
Progress-file updates retry when Windows briefly locks them (for example during
OneDrive sync), so a locked progress file no longer aborts an export.

Create an isolated Conda environment, install the Python requirements, and
install FFmpeg (including ffprobe) and Tesseract:

```powershell
conda create -n lecture-toolkit python=3.11 pip -y
conda run -n lecture-toolkit python -m pip install -r requirements.txt
conda install -n lecture-toolkit -c conda-forge tesseract -y
winget install --id Gyan.FFmpeg --exact
```

Copy `.env.example` to `.env` if it does not already exist, and enter
`GROQ_API_KEY` locally. Colab Secrets are not automatically available on your PC.
The `.env` file is ignored by Git but may still be synced by cloud storage if
your checkout is in OneDrive.

```powershell
conda run --no-capture-output -n lecture-toolkit python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. This binds only to your PC, uses the
`lecture-toolkit` environment, and loads `.env` through the application.
Open a new terminal after installing FFmpeg so its updated `PATH` is available.
Groq does not require local CUDA; audio is sent to Groq when selected.

### Configuration

Local Whisper GPU inference needs CUDA 12 cuBLAS and cuDNN 9 in addition to
a GPU recognized by PyTorch. On Linux, install `nvidia-cublas-cu12` and
`nvidia-cudnn-cu12==9.*`, and include their `lib` directories in
`LD_LIBRARY_PATH` **before starting the server**. Whisper checks that these
libraries can load; otherwise it reports the reason in its progress log and
uses CPU int8. The runtime status distinguishes GPU availability from the
device usable by Whisper. Groq transcription does not require local CUDA.

Regenerating a transcript replaces an active transcription worker. An
intentionally stopped worker cannot overwrite the replacement job's progress
or report its termination as a new failure. An unexpected exit code `-15`
means SIGTERM: check for a server/runtime restart or an external termination
request, rather than treating it as proof of a CUDA or memory error.

1. **Set up Google Gemini API:**
   ```bash
   # Create .env file
   echo "GOOGLE_API_KEY=your_api_key_here" > .env
   ```
   Get your API key from: https://makersuite.google.com/app/apikey

2. **Configure Tesseract (if not in PATH):**
   The app finds Tesseract on PATH, inside the active Conda/Python environment
   (even when the environment is not activated), or in the standard Windows install
   folders. To use another copy, set `TESSERACT_CMD` to the full path of the
   `tesseract` executable before starting the server.

### Model Files

The system requires pre-trained models (included in repository):
- `slidecraft_best.pt` - Alternative slide detection model

## Usage

### Quick Start

1. **Launch the application:**
   ```bash
   # For modular version (recommended)
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

2. **Access the web interface:**
   Open your browser to: http://localhost:8000

3. **Process content:**
   - **YouTube Videos**: Paste a URL, choose 480p (faster) or 720p, and click "Load Video"
   - **Local Files**: Upload video files directly
   - **Real-time Processing**: Monitor progress via live updates
   - **Slide Detection**: Choose content cuts (default), adaptive slides, or existing transcript chapters; higher video and screenshot quality uses more time and storage

### Advanced Usage Examples

#### 1. YouTube Video Analysis
```bash
# Navigate to http://localhost:8000
# Enter YouTube URL: https://www.youtube.com/watch?v=example_id
# System automatically:
# - Downloads video 
# - Extracts YouTube transcript
# - Detects scenes and extracts keyframes
# - Perform slide visual analysis
# - Performs OCR on slides
# - Generates semantic embeddings
# - Creates chapter summaries
```

#### 2. Local Video Processing
```bash
# Upload local video file via web interface
# System processes identically to YouTube content
# Supports: MP4, AVI, MOV, MKV formats
```

#### 3. API Integration
```python
import requests

# Process YouTube video
response = requests.post("http://localhost:8000/process_youtube", 
                        json={"url": "https://youtube.com/watch?v=example"})

# Get transcript-OCR relationships
relationships = requests.get("http://localhost:8000/get_transcript_ocr_relationships/video_id")

# Find OCR text for specific transcript segment
ocr_text = requests.get("http://localhost:8000/find_ocr_for_transcript/video_id/5")
```

### Core Functionality

#### Scene Detection and Analysis
- **Adaptive Detection**: Finds settled slide states and ignores cursor movement,
  compression noise and constantly moving regions such as webcam insets. Full
  slide changes are kept at every detail level; “More slides” also keeps each
  build step, “Balanced” merges small bullet builds, and “Fewer slides” merges
  all builds. With “Match detail level”, the minimum duration and slide limit
  follow the preset (More 5 s/unlimited, Balanced 10 s/unlimited, Fewer 20 s/60
  per hour). A fixed limit applies to every level, so a low limit can make all
  levels return the same count; the result summary shows the applied settings,
  what was merged or hidden by the limit, and the count each level would give.
- **Content-based Segmentation**: Detects visual changes in presentation slides
- **Keyframe Extraction**: Saves representative frames for each scene
- **Thumbnail Generation**: Creates navigation thumbnails

#### OCR Text Extraction
- **Dual OCR Support**: Tesseract and Surya OCR engines
- **Preference System**: User-configurable OCR method selection
- **Live Progress**: Tesseract reads up to four text elements at once; one status line shows the slides being read now, elements done, elapsed time, and a warning if no update arrives for 20 seconds. **Stop OCR** cancels the remaining work, and starting again only reads elements that are still missing.
- **Bounding Box Detection**: Precise text location mapping
- **Quality Filtering**: Confidence-based text filtering

#### Transcript Processing
- **Multi-source Support**: YouTube transcripts and Whisper STT
- **Temporal Alignment**: Precise timestamp synchronization

#### Semantic Embeddings
- **Sentence-BERT**: Advanced semantic similarity computation
- **FAISS Indexing**: Efficient similarity search
- **Temporal Mapping**: Scene-transcript synchronization

## Technical Specifications

### Performance Characteristics
- **Processing Speed**: ~1-2x real-time for typical lecture videos
- **Memory Usage**: 2-4GB RAM for standard processing
- **GPU Acceleration**: CUDA support for Whisper and YOLO models
- **Concurrent Processing**: Multi-threaded scene and OCR analysis

## Slide Visual Analysis


### Supported Formats
- **Input Videos**: MP4, AVI, MOV, MKV, WebM

### API Endpoints

#### Core Processing
- `POST /process_youtube` - Process YouTube video
- `POST /upload_video` - Upload and process local video
- `GET /scenes/{video_id}` - Retrieve scene information
- `GET /video/{video_id}` - Stream processed video

#### OCR and Text Analysis
- `GET /ocr_text/{video_id}` - Get extracted OCR text
- `POST /process_surya_ocr/{video_id}` - Run Surya OCR
- `POST /set_ocr_preference` - Configure OCR engine

#### Transcript Processing
- `GET /get_transcript/{video_id}/{source}` - Retrieve transcripts
- `POST /generate_whisper_transcript/{video_id}` - Generate Whisper transcript
- `GET /whisper_transcript_status/{video_id}` - Check processing status

#### Embeddings and Relationships
- `POST /compute_embeddings/{video_id}` - Generate semantic embeddings
- `GET /get_transcript_ocr_relationships/{video_id}` - Get alignment data
- `GET /find_ocr_for_transcript/{video_id}/{index}` - Find related OCR text
- `GET /find_scene_for_transcript/{video_id}/{index}` - Find corresponding scenes


## Dependencies and Licenses

### Core Dependencies
```
fastapi              # Web framework (MIT License)
uvicorn             # ASGI server (BSD License)
yt-dlp              # YouTube downloader (Unlicense)
youtube-transcript-api # Transcript extraction (MIT License)
ultralytics         # YOLOv8 models (AGPL-3.0)
sentence-transformers # Semantic embeddings (Apache 2.0)
faster-whisper      # Speech recognition (MIT License)
google-generativeai # Gemini AI (Apache 2.0)
surya-ocr           # Advanced OCR (GPL-3.0)
opencv-python       # Computer vision (BSD License)
scenedetect         # Scene detection (BSD License)
faiss-cpu           # Similarity search (MIT License)
```

## Performance Optimization

### Docker Deployment
```dockerfile
FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tesseract-ocr tesseract-ocr-eng ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy and install requirements
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application
COPY . /app
WORKDIR /app

# Expose port
EXPOSE 8000

# Run application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Production Configuration
```bash
# High-performance deployment
uvicorn main:app --workers 4 --host 0.0.0.0 --port 8000 --access-log

# With SSL (recommended for production)
uvicorn main:app --host 0.0.0.0 --port 443 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

## Troubleshooting

### Common Issues

**CUDA/GPU Issues:**
```bash
# Verify CUDA installation
nvidia-smi
python -c "import torch; print(torch.cuda.is_available())"
```

**Tesseract Not Found:**
```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-eng

# Or point the app at a specific binary
export TESSERACT_CMD=/usr/bin/tesseract
```

**YouTube Download Errors:**
```bash
# Update yt-dlp
pip install --upgrade yt-dlp

# Check video availability and region restrictions
```

**Memory Issues:**
- Reduce batch sizes in configuration
- Process shorter video segments
- Use CPU-only inference if GPU memory limited


### Acknowledgments
- **YOLOv8**: Ultralytics team for object detection framework
- **Whisper**: OpenAI for speech recognition technology
- **Sentence-BERT**: UKP Lab for semantic embeddings
- **Surya OCR**: VikParuchuri for advanced OCR capabilities
- **FastAPI**: Sebastian Ramirez for the excellent web framework
- **[Dubby fork](https://github.com/lailashoukryaiu/Dubby)** and its
  **[upstream project](https://github.com/MohammedAly22/Dubby)**: Mohammed Aly
  and contributors for the context-aware translation, isolated model-worker,
  and timestamp-aware dubbing architecture that inspired parts of this toolkit's
  translation roadmap.
  The implementation in this repository is original and does not vendor Dubby
  source code.
