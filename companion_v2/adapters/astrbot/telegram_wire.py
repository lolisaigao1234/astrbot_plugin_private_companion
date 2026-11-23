"""Components the Telegram SDK can render through both session and event sends."""

from astrbot.core.message.components import ActionRow, Face, File, Image, Plain, Record, Video
from astrbot.core.utils.media_utils import convert_audio_format


class _TelegramVoice(Record):
    async def convert_to_file_path(self):
        # AstrBot's Record resolves every source to WAV, including encoded input.
        wav = await super().convert_to_file_path()
        return await convert_audio_format(wav, output_format="ogg")


def telegram_component(part):
    if isinstance(part, Face):
        return Plain(text=f"[QQ表情 {part.id}]")
    if isinstance(part, Record):
        return _TelegramVoice(file=part.file, url=part.url, path=part.path, text=part.text)
    return part


def telegram_visible(chain):
    return any(isinstance(part, (Image, File, Video, Record, Face, ActionRow))
               or isinstance(part, Plain) and part.text.strip() for part in chain)
