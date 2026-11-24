"""Freeze Telegram private references from the delivered update and its converted chain."""

from dataclasses import dataclass, replace

from companion_v2.conversation.forwards.models import CapturedForward, CapturedForwardNode
from companion_v2.conversation.quotes.models import TelegramForwardOrigin, TelegramQuote
from companion_v2.execution.models import TaskError, digest

from .quotes import image_locator
from .telegram_media import freeze_attachment, safe_text, sticker_label


@dataclass(frozen=True)
class TelegramReferences:
    quote: TelegramQuote | None = None
    images: tuple = ()
    unavailable: bool = False
    forward_origin: str = ""
    forwarded_at: int | None = None
    forward_user_id: str | None = None

    @property
    def forward_marker(self):
        return f"[转发消息，来源：{self.forward_origin}]"

    def capture_forward(self, event_id, text, images, audios, videos):
        resource = digest(["telegram-forward", event_id, self.forward_origin, self.forwarded_at])
        node = CapturedForwardNode((0,), resource, self.forward_user_id, self.forward_origin[:256], self.forwarded_at,
                                   text, images, audios, videos)
        position = 1 if self.quote and self.quote.position == 0 else 0
        return CapturedForward(position, resource, (node,))

    def quote_availability(self, text):
        if self.unavailable:
            text += "\n[引用消息不可用]"
        return text


def _origin(event, origin):
    if not origin:
        return None
    source = "未知"
    if origin.type == "user":
        source = f"{origin.sender_user.full_name} ({origin.sender_user.id})"
    elif origin.type == "hidden_user":
        source = origin.sender_user_name
    elif origin.type in ("chat", "channel"):
        chat = origin.sender_chat if origin.type == "chat" else origin.chat
        source = f"{chat.title} ({chat.id})"
        if origin.type == "channel":
            source += f"，原消息 {origin.message_id}"
        if origin.author_signature:
            source += f"，署名 {origin.author_signature}"
    return TelegramForwardOrigin(str(origin.type), safe_text(event, source or "未知"), int(origin.date.timestamp()),
                                 str(origin.sender_user.id) if origin.type == "user" else None)


def private_references(event):
    message = event.message_obj
    raw = getattr(message.raw_message, "message", None)
    peer = str(message.sender.user_id)
    if (raw is None or str(raw.chat.id) != peer or raw.chat.type != "private"
            or str(getattr(raw.from_user, "id", None)) != peer or str(raw.message_id) != str(message.message_id)):
        raise TaskError("host_route_mismatch", "Telegram update does not match the private message")
    origin = _origin(event, raw.forward_origin)
    references = TelegramReferences(forward_origin=origin.label, forwarded_at=origin.sent_at,
                                    forward_user_id=origin.user_id) if origin else TelegramReferences()
    reply = next(((index, part) for index, part in enumerate(message.message)
                  if getattr(part.type, "value", part.type) == "Reply"), None)
    original = raw.reply_to_message
    if not (original or reply or raw.external_reply or raw.quote):
        return references
    user = getattr(original, "from_user", None)
    sender = str(user.id) if user else None
    if (raw.external_reply or not original or original.chat.id != raw.chat.id or original.chat.type != "private"
            or original.message_id <= 0 or original.message_id == raw.message_id
            or sender is None or sender not in (peer, str(getattr(event.client, "id", None)))
            or not original.date or original.date.timestamp() <= 0):
        return replace(references, unavailable=True)
    position, part = reply or (0, None)
    text = raw.quote.text if raw.quote else original.text or original.caption or ""
    images = ()
    static_sticker = original.sticker and not (original.sticker.is_animated or original.sticker.is_video)
    if (original.photo or static_sticker) and not raw.quote:
        if static_sticker:
            text = sticker_label(original.sticker)
        text = "\n[图片 1]\n" + text
        if part and str(part.id) == str(original.message_id) and str(part.sender_id) == sender:
            images = tuple(freeze_attachment(event, image_locator(index, item.file, item.url))
                           for index, item in enumerate(part.chain or ())
                           if getattr(item.type, "value", item.type) == "Image")
    # AstrBot inserts these suffixes in the admitted Plain. Keep their UTF-16
    # locations and fingerprints so verified history can recover Telegram's text.
    link_suffixes = []
    for entity in original.entities or original.caption_entities or ():
        if entity.type == "text_link" and entity.url:
            suffix = f" ({entity.url})"
            link_suffixes.append((entity.offset + entity.length, len(suffix.encode("utf-16-le")) // 2,
                                  digest(suffix)))
    quote = TelegramQuote(position, str(original.message_id), sender, int(original.date.timestamp()),
                          safe_text(event, text) if text.strip() else "[附件]", companion_sender=sender != peer,
                          selection_offset=raw.quote.position if raw.quote else None, link_suffixes=tuple(link_suffixes),
                          forward_origin=_origin(event, original.forward_origin))
    return replace(references, quote=quote, images=images)
