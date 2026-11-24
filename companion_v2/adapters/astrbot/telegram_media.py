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


def sticker_marker(event):
    if getattr(getattr(event, "platform_meta", None), "name", None) != "telegram":
        return ""
    message = getattr(getattr(event.message_obj, "raw_message", None), "message", None)
    sticker = getattr(message, "sticker", None)
    if sticker is None:
        return ""
    return f"[贴纸 {sticker.emoji}]" if sticker.emoji else "[贴纸]"
