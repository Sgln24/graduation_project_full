"""Resolve media tools for both PATH-based installs and Windows app launches."""
import os
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile
from time import monotonic


def _windows_install_path():
    if sys.platform != "win32":
        return ""
    import winreg

    paths = []
    for root, key in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, key) as registry:
                value, _ = winreg.QueryValueEx(registry, "Path")
                paths.append(os.path.expandvars(value))
        except FileNotFoundError:
            continue
    return os.pathsep.join(paths)


def media_executable(name):
    if name not in ("ffmpeg", "ffprobe"):
        raise ValueError(f"Unsupported media executable: {name}")
    configured = os.getenv(f"{name.upper()}_BINARY")
    if configured:
        executable = shutil.which(configured)
        if executable:
            return executable
        raise RuntimeError(f"{name.upper()}_BINARY does not point to an executable: {configured}")
    executable = shutil.which(name)
    if executable:
        return executable
    directories = [str(Path(sys.prefix) / "Library" / "bin"), str(Path(sys.prefix) / "bin")]
    installed_path = _windows_install_path()
    if installed_path:
        directories.append(installed_path)
    executable = shutil.which(name, path=os.pathsep.join(directories))
    if executable:
        return executable
    raise RuntimeError(
        f"Install FFmpeg (including {name}) and restart the server, "
        f"or set {name.upper()}_BINARY to its executable path."
    )


async def encode_export_clip(command, step, label, duration=None):
    output = Path(command[-1])
    command = [*command[:-1], "-nostdin", "-progress", "pipe:1", "-nostats", command[-1]]
    with tempfile.TemporaryFile() as errors:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=errors,
        )
        last_report = monotonic()
        try:
            while line := await process.stdout.readline():
                key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
                if key != "out_time_us" or value == "N/A":
                    continue
                seconds = max(0, int(value) / 1_000_000)
                if monotonic() - last_report >= 10:
                    progress = (
                        f"{min(99, int(seconds / duration * 100))}%"
                        if duration and duration > 0 else f"{seconds / 60:.1f} minutes"
                    )
                    step(f"{label}: encoded {progress} of clip")
                    last_report = monotonic()
            returncode = await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if returncode:
            errors.seek(0, os.SEEK_END)
            size = errors.tell()
            errors.seek(max(0, size - 3000))
            detail = errors.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{label}: FFmpeg clip encoding failed ({returncode}): {detail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"{label}: FFmpeg did not produce a video clip.")
    step(f"{label}: clip encoding complete")
