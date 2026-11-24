"""Freeze Telegram private references from the delivered update and its converted chain."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

from companion_v2.conversation.forwards.models import CapturedForward, CapturedForwardNode
from companion_v2.conversation.quotes.models import TelegramAttachmentQuote, TelegramForwardOrigin, TelegramQuote
from companion_v2.execution.models import TaskError, digest

from companion_v2.sources.privacy import redact_text

from .files import safe_name
from .quotes import image_locator
from .telegram_media import freeze_attachment, safe_text, sticker_label


@dataclass(frozen=True)
class TelegramReferences:
    quote: TelegramQuote | None = None
    images: tuple = ()
    unavailable: bool = False
    origin: TelegramForwardOrigin | None = None

    @property
    def forward_origin(self):
        return redact_text(self.origin.label) if self.origin else ""

    @property
    def forward_marker(self):
        return f"[转发消息，来源：{self.forward_origin}]"

    def capture_forward(self, event_id, text, images, audios, videos):
        resource = self.origin.resource_hash(event_id)
        node = CapturedForwardNode((0,), resource, self.origin.user_id, self.origin.label[:256], self.origin.sent_at,
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
    chat_fingerprint = None
    if origin.type == "user":
        source = f"{origin.sender_user.full_name} ({origin.sender_user.id})"
    elif origin.type == "hidden_user":
        source = origin.sender_user_name
    elif origin.type in ("chat", "channel"):
        chat = origin.sender_chat if origin.type == "chat" else origin.chat
        chat_fingerprint = digest([origin.type, chat.id, chat.title, origin.author_signature,
                                   getattr(origin, "message_id", None)])
        source = f"{chat.title} ({chat.id})"
        if origin.type == "channel":
            source += f"，原消息 {origin.message_id}"
        if origin.author_signature:
            source += f"，署名 {origin.author_signature}"
    return TelegramForwardOrigin(str(origin.type), safe_text(event, source or "未知"), int(origin.date.timestamp()),
                                 str(origin.sender_user.id) if origin.type == "user" else None, chat_fingerprint)


def voice_caption_parts(raw, parts):
    # AstrBot converts a voice note to Record without its Telegram caption.
    kinds = [getattr(part.type, "value", part.type) for part in parts]
    body = kinds[1:] if kinds[:1] == ["Reply"] else kinds
    if not raw.voice or not raw.caption or body not in (["Record"], ["Plain"]):
        return parts
    caption = raw.caption.encode("utf-16-le")
    for entity in reversed(raw.caption_entities or ()):
        if entity.type == "text_link" and entity.url:
            end = 2 * (entity.offset + entity.length)
            caption = caption[:end] + f" ({entity.url})".encode("utf-16-le") + caption[end:]
    caption = caption.decode("utf-16-le")
    if body == ["Plain"] and parts[-1].text == caption:
        return parts
    return (*parts, SimpleNamespace(type="Plain", text=caption))


def private_text_parts(message, parts):
    """Recover Telegram text only for matching converted component shapes."""
    raw = message.raw_message.message
    if raw.voice:
        return voice_caption_parts(raw, parts)
    if not raw.text or not any(entity.type == "mention" for entity in raw.entities):
        return parts
    # Text messages have optional Reply, ordered At components, then optional Plain.
    # Captions already contain their mentions. Leave transformed or stale chains alone.
    kinds = [getattr(part.type, "value", part.type) for part in parts]
    mentions = [part for part, kind in zip(parts, kinds) if kind == "At"]
    plain = [part.text for part, kind in zip(parts, kinds) if kind == "Plain"]
    expected = (["Reply"] if "Reply" in kinds else []) + ["At"] * len(mentions) + ["Plain"] * len(plain)
    if not mentions or len(plain) > 1 or kinds != expected:
        return parts

    source = raw.text.encode("utf-16-le")
    projected, restored, targets = [], [], []
    cursor = 0
    for entity in raw.entities:
        if entity.type not in ("mention", "text_link"):
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        if not cursor <= start < end <= len(source):
            return parts
        label = source[start:end]
        visible = label
        if entity.type == "mention":
            name = raw.parse_entity(entity)
            if not name.startswith("@"):
                return parts
            name = name[1:]
            target = name
            if name.lower() == message.self_id.lower():
                target = message.self_id
                visible = b""
            targets.append((name, target))
        elif entity.url:
            label += f" ({entity.url})".encode("utf-16-le")
            visible = label
        restored.extend((source[cursor:start], label))
        projected.extend((source[cursor:start], visible))
        cursor = end
    if targets != [(part.name, part.qq) for part in mentions]:
        return parts
    projected.append(source[cursor:])
    restored.append(source[cursor:])
    if projected == restored or b"".join(projected) != "".join(plain).encode("utf-16-le"):
        return parts
    # Keep earlier indices (notably Reply) stable, including for mention-only text.
    return (*parts[:-1], SimpleNamespace(type="Plain", text=b"".join(restored).decode("utf-16-le")))


def private_references(event):
    message = event.message_obj
    raw = getattr(message.raw_message, "message", None)
    peer = str(message.sender.user_id)
    if (raw is None or str(raw.chat.id) != peer or raw.chat.type != "private"
            or str(getattr(raw.from_user, "id", None)) != peer or str(raw.message_id) != str(message.message_id)):
        raise TaskError("host_route_mismatch", "Telegram update does not match the private message")
    references = TelegramReferences(origin=_origin(event, raw.forward_origin))
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
    attachment = None
    if original.voice or original.document:
        if original.voice:
            kind, label = "voice", "[语音消息]"
        elif original.document.file_name:
            kind, label = "document", f"[文件 {safe_name(original.document.file_name)}]"
        else:
            kind, label = "unnamed_document", "[文件]"
        full = label + ("\n" + original.caption if original.caption else "")
        attachment = TelegramAttachmentQuote(kind, digest(redact_text(safe_text(event, full))))
        if not raw.quote:
            text = full
    sticker = original.sticker
    if sticker and not raw.quote:
        text = sticker_label(sticker)
    sticker_image = sticker and (sticker.thumbnail or not (sticker.is_animated or sticker.is_video))
    if not raw.quote and (original.photo or sticker_image):
        text = "\n[图片 1]\n" + text
        if part and str(part.id) == str(original.message_id) and str(part.sender_id) == sender:
            # A Telegram photo or sticker contributes at most one bitmap.
            for index, item in enumerate(part.chain or ()):
                if getattr(item.type, "value", item.type) == "Image":
                    images = (freeze_attachment(event, image_locator(index, item.file, item.url)),)
                    break
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
                          forward_origin=_origin(event, original.forward_origin), attachment=attachment)
    return replace(references, quote=quote, images=images)
