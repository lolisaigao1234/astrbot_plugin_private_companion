"""Telegram's file endpoint includes the credential in its path."""

from dataclasses import dataclass, field, replace

from companion_v2.execution.models import TaskError


@dataclass(frozen=True)
class TelegramFile:
    path: str
    client: object = field(repr=False, compare=False)


def telegram_token(event):
    if (getattr(getattr(event, "platform_meta", None), "name", None) == "telegram"
            and getattr(getattr(event.message_obj, "type", None), "value", None) in {"FriendMessage", "GroupMessage"}):
        return getattr(getattr(event, "client", None), "token", None)
    return None


def safe_text(event, text):
    token = telegram_token(event)
    return text.replace(token, "[redacted]") if token else text


def freeze_attachment(event, locator):
    if getattr(getattr(event, "platform_meta", None), "name", None) != "telegram":
        return locator
    client = getattr(event, "client", None)
    token = getattr(client, "token", None)
    value = locator.locator
    if token and token in getattr(locator, "name", ""):
        locator = replace(locator, name=locator.name.replace(token, "[redacted]"))
    if not token or token not in value:
        return locator
    prefix = getattr(client, "base_file_url", "") + "/"
    path = value.removeprefix(prefix)
    if path == value or not path or token in path or token not in prefix:
        raise TaskError("host_media_location_invalid", "invalid Telegram file location")
    return replace(locator, locator=TelegramFile(path, client))


def fetch_location(locator):
    value = locator.locator
    if isinstance(value, TelegramFile):
        return value.client.base_file_url + "/" + value.path
    return value


def sticker_label(sticker):
    return f"[贴纸 {sticker.emoji}]" if sticker.emoji else "[贴纸]"


def sticker_marker(event):
    if getattr(getattr(event, "platform_meta", None), "name", None) != "telegram":
        return ""
    message = getattr(getattr(event.message_obj, "raw_message", None), "message", None)
    sticker = getattr(message, "sticker", None)
    if sticker is None:
        return ""
    return sticker_label(sticker)


async def capture_turn_media(bridge, references, images, audios, videos, text_parts, partial_media):
    """Give direct attachments first claim, then admit verified quote photos."""
    from companion_v2.conversation.media import models as visual
    from companion_v2.conversation.media.audio import models as voice
    from companion_v2.conversation.media.video import models as movie

    remaining = visual.MAX_TURN_BYTES
    descriptors = {"image": [], "audio": [], "video": []}

    async def capture(kind, locators, loader, expected, count, ceiling, *, quoted=False):
        nonlocal remaining
        accepted = []
        retained = descriptors[kind]
        for locator in locators:
            try:
                if remaining <= 0 or len(retained) >= count:
                    raise TaskError(f"host_{kind}_limit", "turn attachment capacity exhausted")
                item = await loader(locator, maximum_bytes=min(remaining, ceiling))
                if type(item) is not expected or item.descriptor.position != locator.position:
                    raise TaskError(f"host_{kind}_invalid", "loader changed the original component position")
                retained.append(item.descriptor)
                if item.descriptor.retained_bytes > remaining or not movie.fits_video_budget(
                        descriptors["video"], descriptors["image"], descriptors["audio"]):
                    retained.pop()
                    raise TaskError(f"host_{kind}_limit", "decoded attachment exceeds the turn budget")
                remaining -= item.descriptor.retained_bytes
                accepted.append(item)
            except TaskError as error:
                if error.code != f"host_{kind}_limit" or not (partial_media or quoted and any(descriptors.values())):
                    raise
                if not quoted:
                    text_parts[locator.position] = "[附件超过读取上限，本轮未读取]"
        return tuple(accepted)

    async def photos(locators, *, quoted=False):
        return await capture("image", locators, bridge.image_loader, visual.CapturedImage,
                             visual.MAX_INPUT_IMAGES, visual.MAX_IMAGE_BYTES, quoted=quoted)

    direct = await photos(images)
    records = await capture("audio", audios, bridge.audio_loader, voice.CapturedAudio,
                            voice.MAX_INPUT_AUDIOS, voice.MAX_AUDIO_BYTES)
    clips = await capture("video", videos, bridge.video_loader, movie.CapturedVideo,
                          movie.MAX_VIDEOS, movie.MAX_VIDEO_BYTES)
    quotes = ()
    if references.quote:
        quotes = (replace(references.quote, images=await photos(references.images, quoted=True)),)
    return direct, quotes, (), records, clips
