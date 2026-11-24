"""Bounded original audio capture and cancellable decoding in a child."""

import asyncio
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from companion_v2.adapters.astrbot.native import NativeAudioLocator
from companion_v2.conversation.media.audio.models import AudioDescriptor, CapturedAudio, MAX_AUDIO_BYTES, MAX_WAVE_BYTES
from companion_v2.conversation.media.models import MAX_IMAGE_BYTES
from companion_v2.adapters.astrbot.telegram_media import fetch_location
from companion_v2.execution.models import TaskError
from ..media import fetch_image as fetch_public_attachment


DECODE_TIMEOUT = 8
DECODE_ERRORS = frozenset({"host_audio_invalid", "host_audio_format_unsupported", "host_audio_limit",
                           "host_audio_decoder_unavailable"})


async def decode_audio(content, position):
    if not 0 < len(content) <= MAX_AUDIO_BYTES:
        raise TaskError("host_audio_limit", "voice exceeds the intake byte budget")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", "-m", "astrbot_host.audio.worker",
        cwd=str(Path(__file__).resolve().parents[2]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        output, _ = await asyncio.wait_for(process.communicate(content), DECODE_TIMEOUT)
        if len(output) > MAX_WAVE_BYTES + 2048:
            raise ValueError("worker response exceeded its budget")
        header, _, waveform = output.partition(b"\n")
        data = json.loads(header)
        if process.returncode:
            code = data.get("error")
            raise TaskError(code if code in DECODE_ERRORS else "host_audio_invalid", "voice could not be decoded completely")
        return CapturedAudio(AudioDescriptor(**{**data, "position": position}), content, waveform)
    except asyncio.TimeoutError:
        raise TaskError("host_audio_decode_timeout", "voice decoding exceeded its bounded preparation time") from None
    finally:
        # Cancellation and timeout must reap the decoder before capture ends.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # Drain both pipes as well: wait() alone can hang when cancellation
            # interrupts communicate() with stdout's transport buffer full.
            await process.communicate()


def _read_record_bytes(stream):
    # AstrBot has already downloaded and converted the Telegram file at this point.
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise ValueError
    if info.st_size > MAX_AUDIO_BYTES:
        raise TaskError("host_audio_limit", "voice exceeds the intake byte budget")
    return stream.read(MAX_AUDIO_BYTES + 1)


def _read_temp_record(value):
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    root = Path(get_astrbot_temp_path()).resolve()
    try:
        if value.startswith(("//", "\\")):
            raise ValueError
        path = Path(value).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            # Hold each directory open so a symlink swap cannot redirect a later open.
            with ExitStack() as stack:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                directory = os.open(root, flags)
                stack.callback(os.close, directory)
                relative = path.relative_to(root)
                for name in relative.parts[:-1]:
                    directory = os.open(name, flags, dir_fd=directory)
                    stack.callback(os.close, directory)
                fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    return _read_record_bytes(stream)
        with path.open("rb") as stream:
            if not path.resolve(strict=True).is_relative_to(root) or not os.path.samestat(
                    os.fstat(stream.fileno()), path.stat()):
                raise ValueError
            return _read_record_bytes(stream)
    except TaskError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise TaskError("host_audio_location_invalid", "voice requires an AstrBot temporary file") from None


async def load_native_audio(locator):
    if type(locator) is not NativeAudioLocator:
        raise TaskError("host_audio_location_invalid", "voice requires a frozen original component")
    try:
        value = fetch_location(locator)
        if value.startswith("base64://"):
            encoded = value[9:]
            if len(encoded) > (MAX_AUDIO_BYTES + 2) // 3 * 4:
                raise TaskError("host_audio_limit", "voice exceeds the intake byte budget")
            content = base64.b64decode(encoded, validate=True)
        elif locator.telegram_temp and Path(value).is_absolute():
            content = await asyncio.to_thread(_read_temp_record, value)
        else:
            # The existing public attachment transport enforces this same byte
            # ceiling and checks redirects plus the actual connector DNS result.
            assert MAX_AUDIO_BYTES == MAX_IMAGE_BYTES
            content = await fetch_public_attachment(value)
        return await decode_audio(content, locator.position)
    except TaskError as error:
        if error.code.startswith("host_image_"):
            raise TaskError(error.code.replace("host_image_", "host_audio_", 1), "original voice attachment is unavailable") from None
        raise
    except Exception:
        raise TaskError("host_audio_fetch_failed", "original voice could not be prepared") from None
