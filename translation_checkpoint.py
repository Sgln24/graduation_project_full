"""Persist validated translation fragments between failed attempts."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading


_active = set()
_lock = threading.Lock()


class TranslationCheckpointError(RuntimeError):
    pass


class _Checkpoint(dict):
    def __init__(self, path, count):
        self.path = path
        try:
            saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            expected = {f"segment-{index}" for index in range(count)}
            if not isinstance(saved, dict) or any(
                key not in expected or not isinstance(text, str) or not text.strip()
                for key, text in saved.items()
            ):
                raise ValueError("Invalid saved fragments")
        except (OSError, ValueError) as error:
            raise TranslationCheckpointError("Could not read saved translation progress.") from error
        super().__init__(saved)

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise TranslationCheckpointError("Could not discard invalid translation progress.") from error
        super().clear()

    def update(self, values):
        if not values or all(self.get(key) == value for key, value in values.items()):
            return
        super().update(values)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        except OSError as error:
            raise TranslationCheckpointError("Could not save completed translation fragments.") from error
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _checkpoint_path(transcript, target, checkpoint_dir):
    # Include timestamps, speakers and chapter fields: only identical input can resume.
    payload = json.dumps([target, transcript], sort_keys=True, ensure_ascii=False, allow_nan=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return Path(checkpoint_dir) / f"{digest}.json"


def clear_translation_checkpoint(transcript, target, checkpoint_dir):
    try:
        _checkpoint_path(transcript, target, checkpoint_dir).unlink(missing_ok=True)
    except OSError as error:
        raise TranslationCheckpointError("Translation finished but saved progress could not be cleared.") from error


async def translate_with_resume(
    processor, transcript, target, model=None, *, checkpoint_dir,
    on_progress=None, allow_fallback=True, clear_on_success=True,
):
    path = _checkpoint_path(transcript, target, checkpoint_dir)
    identity = str(path.resolve())
    with _lock:
        if identity in _active:
            raise TranslationCheckpointError("This transcript is already being translated to this language.")
        _active.add(identity)
    try:
        done = _Checkpoint(path, len(transcript))
        resumed = len(done)
        try:
            result = await processor.translate_transcript(
                transcript, target, model, on_progress=on_progress,
                allow_fallback=allow_fallback, _done=done,
            )
        except (RuntimeError, ValueError, TimeoutError) as error:
            if isinstance(error, TranslationCheckpointError) or not done:
                raise
            raise RuntimeError(
                f"{error} Completed translations for {len(done)}/{len(transcript)} lines are saved. "
                "Retry with the same input and target language to resume."
            ) from error
        result["resumed_segments"] = resumed
        if clear_on_success:
            clear_translation_checkpoint(transcript, target, checkpoint_dir)
        return result
    finally:
        with _lock:
            _active.remove(identity)
