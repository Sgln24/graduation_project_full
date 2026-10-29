import os
import json
import shutil
import time
import asyncio
import threading
import multiprocessing
from fastapi.responses import JSONResponse
from youtube_transcript_api import YouTubeTranscriptApi
from transcribe import (
    WHISPER_MODELS,
    describe_transcription_method,
    get_whisper_device_config,
    transcribe_audio,
)
from cloud_transcribe import (
    CLOUD_TRANSCRIPTION_MODELS,
    describe_cloud_method,
    is_cloud_model,
    provider_api_key,
    recommended_transcription_model,
    transcribe_audio_cloud,
)
from project_paths import VIDEO_DIR, TRANSCRIPTS_DIR
from processors import transcript_versions
from audio_decoder import decode_audio_samples

TRANSCRIPTION_MODELS = tuple(WHISPER_MODELS) + tuple(CLOUD_TRANSCRIPTION_MODELS)


def default_transcription_model():
    device, _ = get_whisper_device_config()
    return recommended_transcription_model(device == "cuda")


def get_huggingface_token():
    """Return a Hugging Face token from the environment or a saved hub login."""
    for name in ("HUGGINGFACE_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    try:
        from huggingface_hub import get_token
        return (get_token() or "").strip() or None
    except Exception:
        return None


_ACTIVE_WHISPER_PROCESSES = {}
_ACTIVE_WHISPER_PROCESSES_LOCK = threading.Lock()


def _transcript_steps_path(video_id):
    return TRANSCRIPTS_DIR / f"{video_id}_whisper_steps.jsonl"


def log_transcript_step(video_id, message, reset=False):
    """Append a timestamped step; the file is shared with the transcription process."""
    message = str(message)[:400]
    print(f"[transcript {video_id}] {message}")
    try:
        with open(_transcript_steps_path(video_id), "w" if reset else "a", encoding="utf-8") as file:
            file.write(json.dumps({"time": time.time(), "message": message}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_transcript_steps(video_id, limit=60):
    """Return steps with seconds since the job started, and the current elapsed time."""
    path = _transcript_steps_path(video_id)
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and "time" in entry:
                entries.append(entry)
    except OSError:
        return [], None
    if not entries:
        return [], None
    first = float(entries[0]["time"])
    steps = [
        {"elapsed": round(float(entry["time"]) - first, 1), "message": str(entry.get("message", ""))}
        for entry in entries
    ]
    return steps[-limit:], round(time.time() - first, 1)


def _run_whisper_worker(
    video_id, video_path, output_path, diarization, existing_transcript,
    model="turbo", prompt=None,
):
    TranscriptProcessor()._process_whisper_transcript_sync(
        video_id, video_path, output_path, diarization, existing_transcript,
        model, prompt,
    )


class TranscriptProcessor:
    def __init__(self):
        self.transcript_preference = "youtube"  # Can be "youtube" or "whisper"
        self.progress_stale_seconds = int(os.getenv("WHISPER_PROGRESS_STALE_SECONDS", "900"))
        self.manual_retry_stale_seconds = int(
            os.getenv("WHISPER_MANUAL_RETRY_STALE_SECONDS", "90")
        )
        self.heartbeat_seconds = int(os.getenv("WHISPER_HEARTBEAT_SECONDS", "15"))

    @staticmethod
    def _speaker_backup_path(video_id):
        return TRANSCRIPTS_DIR / f"{video_id}_whisper_before_speakers.json"

    def restore_interrupted_speaker_jobs(self):
        """Put back transcripts whose speaker identification was cut off by a server restart."""
        try:
            backups = list(TRANSCRIPTS_DIR.glob("*_whisper_before_speakers.json"))
        except OSError:
            return
        for backup in backups:
            video_id = backup.name[: -len("_whisper_before_speakers.json")]
            transcript = TRANSCRIPTS_DIR / f"{video_id}_whisper.json"
            try:
                if not transcript.exists():
                    os.replace(backup, transcript)
                    (TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt").unlink(missing_ok=True)
                    self._remove_whisper_phase(video_id)
                    print(f"Restored the transcript of {video_id} after interrupted speaker identification")
                else:
                    backup.unlink()
            except OSError as error:
                print(f"Could not restore the transcript of {video_id}: {error}")

    async def get_transcript(self, video_id: str, source: str):
        """Get a specific transcript by source."""
        if source not in ["youtube", "whisper"]:
            return JSONResponse({
                "success": False,
                "error": "Invalid source. Must be 'youtube' or 'whisper'."
            })
        
        transcript_path = str(TRANSCRIPTS_DIR / f"{video_id}_{source}.json")
        if not os.path.exists(transcript_path):
            # If transcript doesn't exist and source is youtube, try to fetch it
            if source == "youtube":
                transcript, error = await self.get_youtube_transcript(video_id)
                if transcript:
                    return JSONResponse({
                        "success": True,
                        "transcript": transcript
                    })
                else:
                    return JSONResponse({
                        "success": False,
                        "error": error or "Failed to fetch YouTube transcript"
                    })
            return JSONResponse({
                "success": False,
                "error": f"No {source} transcript available for this video"
            })
        
        try:
            with open(transcript_path, 'r') as f:
                transcript = json.load(f)
            
            # Ensure consistent format
            if source == "youtube":
                transcript = self.format_youtube_transcript(transcript)
            else:
                names = self._read_speaker_names(video_id)
                transcript = [
                    {
                        **item,
                        "speaker_name": names.get(item.get("speaker"), item.get("speaker"))
                        if item.get("speaker") else None,
                    }
                    for item in transcript
                ]
            
            return JSONResponse({
                "success": True,
                "transcript": transcript,
                "metadata": (
                    self._public_whisper_metadata(video_id)
                    if source == "whisper" else None
                ),
            })
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": str(e)
            })

    def format_youtube_transcript(self, transcript):
        """Ensure YouTube transcript has consistent format."""
        formatted = []
        for item in transcript:
            formatted.append({
                "text": item.get("text", ""),
                "start": item.get("start", 0),
                "duration": item.get("duration", 0)
            })
        return formatted

    async def get_youtube_transcript(self, video_id: str):
        """Get transcript from YouTube."""
        try:
            transcript = await asyncio.to_thread(
                YouTubeTranscriptApi.get_transcript, video_id, languages=['en', 'fr']
            )
            
            # Format transcript
            formatted_transcript = self.format_youtube_transcript(transcript)
            
            # Save transcript for future use
            with open(str(TRANSCRIPTS_DIR / f"{video_id}_youtube.json"), 'w') as f:
                json.dump(formatted_transcript, f)
            
            return formatted_transcript, None
        except Exception as e:
            return None, str(e)

    async def generate_whisper_transcript(
        self, video_id: str, background_tasks, diarization=False, force=False,
        model="turbo", prompt=None, speakers_only=False,
    ):
        """Generate a transcript using local Whisper or an online model.

        speakers_only identifies speakers in the existing transcript without transcribing again.
        """
        if speakers_only:
            if not (TRANSCRIPTS_DIR / f"{video_id}_whisper.json").is_file():
                return JSONResponse({
                    "success": False,
                    "error": "Generate a transcript first; speakers are identified in an existing transcript.",
                })
            metadata = self._read_whisper_metadata(video_id) or {}
            model = metadata.get("model") or model
            prompt = metadata.get("prompt") or None
            diarization = force = True
        model = model or default_transcription_model()
        prompt = str(prompt or "").strip()[:2000] or None
        if model not in TRANSCRIPTION_MODELS:
            return JSONResponse({
                "success": False,
                "error": f"Unsupported transcription model: {model}",
            })
        if is_cloud_model(model):
            provider = CLOUD_TRANSCRIPTION_MODELS[model]["provider"]
            if not provider_api_key(provider):
                key_name = "GROQ_API_KEY" if provider == "groq" else "GOOGLE_API_KEY"
                return JSONResponse({
                    "success": False,
                    "error": (
                        f"{CLOUD_TRANSCRIPTION_MODELS[model]['label']} needs {key_name} "
                        "in the server environment. Add it and restart the server."
                    ),
                })
        try:
            if force:
                if self._terminate_whisper_process(video_id):
                    progress_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
                    progress_path.unlink(missing_ok=True)
                    self._remove_whisper_phase(video_id)

            # Check if video exists
            video_files = os.listdir(VIDEO_DIR)
            video_path = None
            for file in video_files:
                if file.startswith(video_id):
                    video_path = str(VIDEO_DIR / file)
                    break
            
            if not video_path:
                return JSONResponse({
                    "success": False,
                    "error": "Video not found"
                })
            
            output_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
            progress_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
            existing_transcript = None

            # Check if transcript already exists
            if os.path.exists(output_path) and not force:
                with open(output_path, 'r') as f:
                    transcript = json.load(f)
                return JSONResponse({
                    "success": True,
                    "transcript": transcript,
                    "message": "Using existing Whisper transcript"
                })

            if progress_path.exists():
                progress_age = time.time() - progress_path.stat().st_mtime
                stale_seconds = (
                    self.manual_retry_stale_seconds
                    if force
                    else self.progress_stale_seconds
                )
                if progress_age > stale_seconds:
                    progress_path.unlink()
                    self._remove_whisper_phase(video_id)
                else:
                    return JSONResponse({
                        "success": True,
                        "message": "Transcript generation is already in progress",
                        "status": "in_progress",
                        "status_url": f"/whisper_transcript_status/{video_id}"
                    })

            error_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt"
            if error_path.exists():
                error_path.unlink()

            if (
                diarization
                and force
                and os.path.exists(output_path)
                and (speakers_only or self._can_reuse_whisper_transcript(video_id, model, prompt))
            ):
                existing_transcript = self._read_transcript(video_id)
                if not existing_transcript:
                    existing_transcript = None
            if speakers_only and not existing_transcript:
                return JSONResponse({"success": False, "error": "The existing transcript could not be read."})
            
            # Start background task to generate transcript
            if os.path.exists(output_path):
                try:
                    transcript_versions.sync_existing(video_id)
                except Exception as error:
                    print(f"Could not store the previous transcript version: {error}")
                shutil.copyfile(output_path, self._speaker_backup_path(video_id))
                os.remove(output_path)
            progress_path.write_text(
                "100" if existing_transcript else "0",
                encoding="utf-8",
            )
            self._write_whisper_phase(
                video_id,
                "identifying_speakers" if existing_transcript else "starting",
            )
            log_transcript_step(
                video_id,
                "Speaker identification requested for the existing transcript"
                if existing_transcript
                else f"Transcription requested with {model}"
                + (", then speaker identification" if diarization else ""),
                reset=True,
            )
            background_tasks.add_task(
                self.process_whisper_transcript,
                video_id,
                video_path,
                output_path,
                diarization,
                existing_transcript,
                model,
                prompt,
            )
            
            return JSONResponse({
                "success": True,
                "message": (
                    "Speaker identification started using the existing transcript"
                    if existing_transcript
                    else "Transcript generation started"
                ),
                "phase": (
                    "identifying_speakers" if existing_transcript else "starting"
                ),
                "status_url": f"/whisper_transcript_status/{video_id}"
            })
            
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": str(e)
            })

    async def start_whisper_generation(self, video_id: str, video_path: str, background_tasks):
        """Start Whisper transcript generation."""
        output_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
        error_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt"
        progress_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
        if error_path.exists():
            error_path.unlink()
        if progress_path.exists():
            progress_age = time.time() - progress_path.stat().st_mtime
            if progress_age <= self.progress_stale_seconds:
                return
            progress_path.unlink()

        # Create progress file
        with open(progress_path, 'w') as f:
            f.write("0")
        log_transcript_step(
            video_id,
            f"Transcription requested with {default_transcription_model()}",
            reset=True,
        )
        
        # Start background task
        background_tasks.add_task(
            self.process_whisper_transcript, video_id, video_path, output_path,
            model=default_transcription_model(),
        )

    async def process_whisper_transcript(
        self, video_id: str, video_path: str, output_path: str,
        diarization=False, existing_transcript=None, model="turbo",
        prompt=None,
    ):
        """Run native Whisper inference outside the FastAPI server process."""
        log_transcript_step(
            video_id,
            "Starting a separate transcription process (loads the AI libraries first)",
        )
        process = multiprocessing.get_context("spawn").Process(
            target=_run_whisper_worker,
            args=(
                video_id,
                video_path,
                output_path,
                diarization,
                existing_transcript,
                model,
                prompt,
            ),
            name=f"whisper-{video_id}",
        )
        started = False
        try:
            with _ACTIVE_WHISPER_PROCESSES_LOCK:
                process.start()
                started = True
                _ACTIVE_WHISPER_PROCESSES[video_id] = process
            await asyncio.to_thread(process.join)
            with _ACTIVE_WHISPER_PROCESSES_LOCK:
                if (
                    _ACTIVE_WHISPER_PROCESSES.get(video_id) is process
                    and process.exitcode != 0
                ):
                    self._record_whisper_process_failure(video_id, process.exitcode)
        except Exception as error:
            with _ACTIVE_WHISPER_PROCESSES_LOCK:
                active = _ACTIVE_WHISPER_PROCESSES.get(video_id)
                if active is process or (not started and active is None):
                    self._record_whisper_process_failure(video_id, None, error)
        finally:
            with _ACTIVE_WHISPER_PROCESSES_LOCK:
                if _ACTIVE_WHISPER_PROCESSES.get(video_id) is process:
                    _ACTIVE_WHISPER_PROCESSES.pop(video_id, None)

    @staticmethod
    def _terminate_whisper_process(video_id):
        with _ACTIVE_WHISPER_PROCESSES_LOCK:
            process = _ACTIVE_WHISPER_PROCESSES.pop(video_id, None)
        if process and process.is_alive():
            process.terminate()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            return True
        return False

    async def cancel_whisper_generation(self, video_id):
        if not self._terminate_whisper_process(video_id):
            return JSONResponse({"success": False, "error": "No running transcription process was found. Refresh its status before retrying."})
        backup = self._speaker_backup_path(video_id)
        if backup.exists():
            os.replace(backup, TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
        (TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt").unlink(missing_ok=True)
        (TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt").unlink(missing_ok=True)
        self._remove_whisper_phase(video_id)
        log_transcript_step(video_id, "Processing stopped by the user; the previous transcript was preserved")
        return JSONResponse({"success": True, "transcript": self._read_transcript(video_id)})

    def _record_whisper_process_failure(self, video_id, exit_code, error=None):
        output_path = TRANSCRIPTS_DIR / f"{video_id}_whisper.json"
        error_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt"
        if output_path.exists() or error_path.exists():
            return
        progress_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
        if progress_path.exists():
            progress_path.unlink()
        self._remove_whisper_phase(video_id)
        detail = (
            f": {error}" if error is not None
            else f" with exit code {exit_code}"
        )
        backup_path = self._speaker_backup_path(video_id)
        if backup_path.exists():
            try:
                os.replace(backup_path, output_path)
                log_transcript_step(
                    video_id,
                    "Restored the previous transcript after the worker stopped unexpectedly",
                )
            except OSError as restore_error:
                detail += f"; also failed to restore the previous transcript: {restore_error}"
        guidance = (
            " The worker received SIGTERM (a termination request), not a CUDA "
            "library error. Check whether the server/runtime was restarted or "
            "another transcription request replaced this job."
            if exit_code == -15 and error is None
            else " Retry transcription and check the server output for memory "
            "or CTranslate2 errors."
        )
        log_transcript_step(video_id, f"Transcription process stopped unexpectedly{detail}")
        error_path.write_text(
            "Whisper's isolated inference process stopped unexpectedly"
            f"{detail}. The web server remained available.{guidance}",
            encoding="utf-8",
        )

    def _process_whisper_transcript_sync(
        self, video_id: str, video_path: str, output_path: str,
        diarization=False, existing_transcript=None, model="turbo",
        prompt=None,
    ):
        """Blocking Whisper transcription and diarization, run off the event loop."""
        original_transcript = [
            dict(item) for item in (existing_transcript or [])
        ]
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_whisper_job,
            args=(video_id, heartbeat_stop),
            daemon=True,
        )
        heartbeat.start()
        started = time.monotonic()

        def step(message):
            log_transcript_step(video_id, message)

        try:
            try:
                transcript_versions.sync_existing(video_id)
            except Exception as error:
                print(f"Could not store the previous transcript version: {error}")
            transcript = [dict(item) for item in original_transcript]
            if transcript:
                step("Reusing the existing transcript; only speakers will be identified")
            else:
                self._write_whisper_phase(video_id, "transcribing")
                step(
                    f"Transcription process ready; using {model}"
                    + (" with your prompt" if prompt else "")
                )
                if is_cloud_model(model):
                    for items, progress in transcribe_audio_cloud(
                        video_path, model, prompt, on_step=step
                    ):
                        transcript.extend(items)
                        (TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt").write_text(
                            str(progress), encoding="utf-8"
                        )
                for sentence_data, progress in (
                    () if is_cloud_model(model)
                    else transcribe_audio(
                        video_path, model_name=model, prompt=prompt, on_step=step
                    )
                ):
                    lines = sentence_data.strip().split('\n')
                    i = 0
                    while i < len(lines):
                        if i + 2 < len(lines) and '-->' in lines[i+1]:
                            timestamp_line = lines[i+1]
                            start_time = timestamp_line.split(' --> ')[0].strip()
                            end_time = timestamp_line.split(' --> ')[1].strip()

                            h, m, s = start_time.split(':')
                            s, ms = s.split(',')
                            start_seconds = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

                            h, m, s = end_time.split(':')
                            s, ms = s.split(',')
                            end_seconds = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

                            text = lines[i+2].strip()
                            transcript.append({
                                "text": text,
                                "start": start_seconds,
                                "duration": end_seconds - start_seconds
                            })

                            i += 4
                        else:
                            i += 1

                    progress_file = str(TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt")
                    with open(progress_file, 'w') as f:
                        f.write(str(progress))

            if not transcript:
                raise RuntimeError(
                    "Whisper completed without any transcript segments. "
                    "The video may not contain recognizable speech."
                )
            if not original_transcript:
                step(f"Transcription finished: {len(transcript)} lines")
            speaker_names = {}
            diarization_error = None
            if diarization:
                self._write_whisper_phase(video_id, "identifying_speakers")
                try:
                    transcript, speaker_names = self.apply_diarization(
                        video_path, transcript, on_step=step
                    )
                except Exception as error:
                    if original_transcript:
                        raise
                    # Keep the new transcript; only the speaker labels are missing.
                    diarization_error = str(error)[:500]
                    step(f"Speaker identification failed; keeping the transcript: {error}")
            step(f"Saving the transcript (total {time.monotonic() - started:.0f}s)")
            with open(output_path, 'w') as f:
                json.dump(transcript, f)
            previous_metadata = self._read_whisper_metadata(video_id)
            if original_transcript:
                device = previous_metadata.get("device")
                method = previous_metadata.get("transcription_method")
                generated_at = previous_metadata.get("generated_at")
            elif is_cloud_model(model):
                device = "cloud"
                method = describe_cloud_method(model)
                generated_at = time.strftime("%Y-%m-%d %H:%M")
            else:
                device, _ = get_whisper_device_config()
                method = describe_transcription_method(device)
                generated_at = time.strftime("%Y-%m-%d %H:%M")
            with open(str(TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"), "w") as f:
                json.dump({
                    "model": model,
                    "prompt": prompt or "",
                    "device": device,
                    "transcription_method": method,
                    "word_timestamps": not is_cloud_model(model),
                    "diarization": bool(diarization) and not diarization_error,
                    "diarization_error": diarization_error,
                    "speaker_names": speaker_names,
                    "generated_at": generated_at,
                }, f)
            if speaker_names:
                try:
                    updated = transcript_versions.copy_speakers_to_translations(video_id, transcript, speaker_names)
                    if updated:
                        step(f"Speakers added to {updated} translated transcript(s)")
                except Exception as error:
                    print(f"Could not add speakers to translations: {error}")
            try:
                transcript_versions.sync_existing(video_id)
            except Exception as error:
                print(f"Could not store the transcript version: {error}")

            progress_file = str(TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt")
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 1)
            if os.path.exists(progress_file):
                os.remove(progress_file)
            self._remove_whisper_phase(video_id)
            self._speaker_backup_path(video_id).unlink(missing_ok=True)

        except Exception as e:
            print(f"Error generating Whisper transcript: {str(e)}")
            step(f"Failed after {time.monotonic() - started:.0f}s: {e}")
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_seconds + 1)
            progress_file = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
            if progress_file.exists():
                progress_file.unlink()
            self._remove_whisper_phase(video_id)
            error_message = str(e)
            if original_transcript:
                with open(output_path, "w", encoding="utf-8") as file:
                    json.dump(original_transcript, file, ensure_ascii=False)
                self._speaker_backup_path(video_id).unlink(missing_ok=True)
            else:
                backup_path = self._speaker_backup_path(video_id)
                if backup_path.exists():
                    try:
                        os.replace(backup_path, output_path)
                        step("Restored the previous transcript after processing failed")
                    except OSError as restore_error:
                        error_message += (
                            f"; also failed to restore the previous transcript: "
                            f"{restore_error}"
                        )
            with open(str(TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt"), 'w') as f:
                f.write(error_message)

    async def get_whisper_status(self, video_id: str):
        """Check the status of Whisper transcript generation."""
        response = await self._get_whisper_status(video_id)
        steps, elapsed = read_transcript_steps(video_id)
        if isinstance(getattr(response, "content", None), dict):
            response.content.update({"steps": steps, "steps_elapsed": elapsed})
            return response
        try:
            payload = json.loads(response.body)
        except (AttributeError, TypeError, json.JSONDecodeError):
            return response
        payload.update({"steps": steps, "steps_elapsed": elapsed})
        return JSONResponse(payload)

    async def _get_whisper_status(self, video_id: str):
        try:
            error_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt")
            if os.path.exists(error_path):
                with open(error_path, 'r') as f:
                    error = f.read()
                return JSONResponse({
                    "success": False,
                    "status": "error",
                    "error": error
                })

            transcript_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper.json")
            if os.path.exists(transcript_path):
                try:
                    with open(transcript_path, 'r') as f:
                        transcript = json.load(f)
                except (OSError, json.JSONDecodeError) as error:
                    return JSONResponse({
                        "success": False,
                        "status": "error",
                        "error": f"Whisper transcript file is invalid: {error}"
                    })
                if not isinstance(transcript, list) or not transcript:
                    return JSONResponse({
                        "success": False,
                        "status": "error",
                        "error": "Whisper completed without any transcript segments."
                    })
                return JSONResponse({
                    "success": True,
                    "status": "complete",
                    "transcript": transcript,
                    "model": self._read_whisper_model(video_id),
                    "speaker_names": self._read_speaker_names(video_id),
                    "metadata": self._public_whisper_metadata(video_id),
                })

            progress_path = str(TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt")
            if os.path.exists(progress_path):
                try:
                    with open(progress_path, 'r') as f:
                        progress = float(f.read().strip() or "0")
                        progress_age = time.time() - os.fstat(f.fileno()).st_mtime
                except FileNotFoundError:
                    return await self._get_whisper_status(video_id)
                phase = self._read_whisper_phase(video_id)
                stale_seconds = (
                    max(self.progress_stale_seconds, 3600)
                    if phase == "identifying_speakers"
                    else self.progress_stale_seconds
                )
                if progress_age > stale_seconds:
                    stale_message = (
                        "Transcript processing stopped responding. "
                        "The previous job was marked stale and can be retried."
                    )
                    Path(progress_path).unlink(missing_ok=True)
                    self._remove_whisper_phase(video_id)
                    with open(str(TRANSCRIPTS_DIR / f"{video_id}_whisper_error.txt"), 'w') as f:
                        f.write(stale_message)
                    return JSONResponse({
                        "success": False,
                        "status": "error",
                        "error": stale_message
                    })
                return JSONResponse({
                    "success": True,
                    "status": "in_progress",
                    "progress": progress,
                    "phase": phase or ("starting" if progress <= 0 else "transcribing"),
                    "last_updated_seconds_ago": round(progress_age, 1)
                })

            return JSONResponse({
                "success": True,
                "status": "queued"
            })

        except Exception as e:
            return JSONResponse({
                "success": False,
                "status": "error",
                "error": str(e)
            })

    def _read_whisper_model(self, video_id: str):
        metadata_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"
        if metadata_path.exists():
            try:
                return json.loads(metadata_path.read_text(encoding="utf-8")).get("model", "turbo")
            except (OSError, json.JSONDecodeError):
                pass
        return "turbo"

    def _read_speaker_names(self, video_id: str):
        metadata_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"
        if metadata_path.exists():
            try:
                return json.loads(metadata_path.read_text(encoding="utf-8")).get("speaker_names", {})
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _read_whisper_metadata(self, video_id):
        metadata_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _public_whisper_metadata(self, video_id):
        metadata = self._read_whisper_metadata(video_id)
        if not metadata:
            return None
        return {
            key: metadata.get(key)
            for key in (
                "model", "prompt", "device", "transcription_method",
                "diarization", "diarization_error", "generated_at",
            )
        }

    def _can_reuse_whisper_transcript(self, video_id, model="turbo", prompt=None):
        metadata = self._read_whisper_metadata(video_id)
        if not metadata:
            return False
        return (
            metadata.get("model", "turbo") == model
            and str(metadata.get("prompt") or "").strip() == str(prompt or "").strip()
        )

    def _whisper_phase_path(self, video_id):
        return TRANSCRIPTS_DIR / f"{video_id}_whisper_phase.txt"

    def _write_whisper_phase(self, video_id, phase):
        self._whisper_phase_path(video_id).write_text(phase, encoding="utf-8")

    def _read_whisper_phase(self, video_id):
        phase_path = self._whisper_phase_path(video_id)
        if not phase_path.exists():
            return None
        try:
            return phase_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _remove_whisper_phase(self, video_id):
        phase_path = self._whisper_phase_path(video_id)
        if phase_path.exists():
            phase_path.unlink()

    def _heartbeat_whisper_job(self, video_id, stop_event):
        progress_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_progress.txt"
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                os.utime(progress_path, None)
            except FileNotFoundError:
                return
            except OSError:
                return

    @staticmethod
    def _load_pyannote_pipeline(Pipeline, token):
        """Load the diarization pipeline; retry with plain HTTPS downloads if the Xet CDN is blocked."""
        try:
            return TranscriptProcessor._load_pyannote_pipeline_once(Pipeline, token)
        except RuntimeError as error:
            message = str(error).lower()
            if "cas client" not in message and "xet" not in message:
                raise
            try:
                from huggingface_hub import constants
            except ImportError:
                raise error
            if constants.HF_HUB_DISABLE_XET:
                raise
            # Some networks block the Xet CDN; normal Hugging Face downloads still work there.
            constants.HF_HUB_DISABLE_XET = True
            os.environ["HF_HUB_DISABLE_XET"] = "1"
            return TranscriptProcessor._load_pyannote_pipeline_once(Pipeline, token)

    @staticmethod
    def _load_pyannote_pipeline_once(Pipeline, token):
        """Load the diarization pipeline with pyannote.audio 3.x or 4.x."""
        errors = []
        for name in ("pyannote/speaker-diarization-3.1", "pyannote/speaker-diarization-community-1"):
            # pyannote.audio 4 renamed use_auth_token to token.
            for auth in ({"token": token}, {"use_auth_token": token}):
                try:
                    pipeline = Pipeline.from_pretrained(name, **auth)
                except TypeError as error:
                    errors.append(f"{name}: {error}")
                    continue
                except Exception as error:
                    errors.append(f"{name}: {error}")
                    break
                if pipeline is not None:
                    return pipeline
                errors.append(f"{name}: access denied or model terms not accepted")
                break
        raise RuntimeError(" | ".join(errors))

    @staticmethod
    def _load_audio_waveform(video_path):
        """Decode mono 16 kHz audio with ffmpeg so pyannote does not need a video decoder."""
        import torch

        samples = decode_audio_samples(video_path)
        return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": 16000}

    def _run_pyannote(self, Pipeline, token, video_path, on_step=None):
        step = on_step or (lambda message: None)
        step("Loading the pyannote speaker model (the first use downloads it)")
        started = time.monotonic()
        pipeline = self._load_pyannote_pipeline(Pipeline, token)
        step(f"Speaker model loaded in {time.monotonic() - started:.0f}s")
        device = "CPU"
        try:
            import torch

            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
                device = "GPU"
        except Exception as error:
            print(f"Speaker identification will run on the CPU: {error}")
        step("Decoding the audio for speaker detection")
        started = time.monotonic()
        audio = self._load_audio_waveform(video_path)
        step(f"Audio decoded in {time.monotonic() - started:.0f}s")
        step(
            f"Detecting speakers on the {device}"
            + (" (much slower without a GPU)" if device == "CPU" else "")
        )
        started = time.monotonic()
        reported = {}

        def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
            # Report each pyannote stage and every 10% within it, so long CPU runs show progress.
            percent = min(100, int(100 * completed / total)) if total and completed is not None else None
            key = (step_name, None if percent is None else percent // 10)
            if reported.get("key") != key:
                reported["key"] = key
                label = str(step_name).replace("_", " ")
                step(f"Detecting speakers: {label}" + (f" {percent}%" if percent is not None else ""))

        try:
            result = pipeline(audio, hook=hook)
        except TypeError as error:
            if "hook" not in str(error):
                raise
            result = pipeline(audio)
        # pyannote.audio 4 returns an object whose speaker_diarization is the annotation.
        annotation = getattr(result, "speaker_diarization", result)
        step(f"Speaker detection finished in {time.monotonic() - started:.0f}s")
        return annotation

    def apply_diarization(self, video_path, transcript, on_step=None):
        """Assign pyannote speaker labels to Whisper segments by timestamp overlap."""
        token = get_huggingface_token()
        if not token:
            raise RuntimeError(
                "Speaker identification requires HUGGINGFACE_TOKEN (or HF_TOKEN) "
                "in the server environment, or a saved huggingface_hub login, "
                "with access to pyannote/speaker-diarization-3.1."
            )
        try:
            if on_step:
                on_step("Loading speaker identification libraries")
            from pyannote.audio import Pipeline
        except ImportError as error:
            raise RuntimeError(
                "Speaker identification requires pyannote.audio. Install the project "
                "requirements and retry."
            ) from error
        try:
            annotation = self._run_pyannote(Pipeline, token, video_path, on_step)
        except Exception as error:
            raise RuntimeError(
                f"Speaker identification failed: {error}"
            ) from error

        speaker_segments = [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]
        speakers = sorted({speaker for _, _, speaker in speaker_segments})
        if on_step:
            on_step(f"Found {len(speakers)} speaker(s); matching them to transcript lines")
        speaker_names = {speaker: speaker.replace("_", " ").title() for speaker in speakers}
        for item in transcript:
            start = float(item.get("start", 0))
            end = start + float(item.get("duration", 0))
            overlaps = {}
            for segment_start, segment_end, speaker in speaker_segments:
                overlap = max(0.0, min(end, segment_end) - max(start, segment_start))
                overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap
            if overlaps:
                item["speaker"] = max(overlaps, key=overlaps.get)
        return transcript, speaker_names

    async def save_speaker_names(self, video_id, names):
        metadata_path = TRANSCRIPTS_DIR / f"{video_id}_whisper_meta.json"
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
        known_speakers = {
            item.get("speaker")
            for item in self._read_transcript(video_id)
            if item.get("speaker")
        }
        cleaned = {
            str(key): str(value).strip()
            for key, value in names.items()
            if str(key) in known_speakers and str(value).strip()
        }
        metadata["speaker_names"] = cleaned
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return {"success": True, "speaker_names": cleaned}

    def _read_transcript(self, video_id):
        path = TRANSCRIPTS_DIR / f"{video_id}_whisper.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    async def set_preference(self, preference: str):
        """Set the transcript preference."""
        if preference not in ["youtube", "whisper"]:
            return JSONResponse({
                "success": False,
                "error": "Invalid preference"
            })
        
        self.transcript_preference = preference
        return JSONResponse({
            "success": True,
            "message": f"Transcript preference set to {preference}"
        })

    def get_preference(self):
        """Get the current transcript preference."""
        return self.transcript_preference 