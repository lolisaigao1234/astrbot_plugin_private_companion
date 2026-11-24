"""Group mention positions from native components and QQ Official source text."""

from collections.abc import Mapping
import re
from types import SimpleNamespace

from companion_v2.conversation.group_timing import SELF_MENTION
from .group_quotes import mention_text
from .telegram_reply import reply_original
from .voice_stt import (
    ASTRBOT_STT_TRANSCRIPTS_EXTRA,
    STT_EXTRA_KEYS,
    VOICE_TRANSCRIPT_PREFIX,
    read_stt_extra,
    trusted_stt_extra_transcript,
    trusted_stt_extra_with_source,
    trusted_stt_transcript,
)


QQ_MENTION = re.compile(r'<@!?(?P<short>[^<>\s]+)>|<qqbot-at-user\s+id="(?P<xml>[^"]+)"\s*/>')
OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def component_kind(part):
    kind = _get(part, "type")
    return getattr(kind, "value", kind)


def bot_mention_ids(event, account_id):
    if _get(_get(event, "platform_meta"), "name") == "telegram":
        return {event.message_obj.self_id}
    identifiers = {str(account_id)}
    if _get(_get(event, "platform_meta"), "name") in OFFICIAL_PLATFORMS:
        raw = getattr(event.message_obj, "raw_message", None)
        mentions = _get(raw, "mentions") or _get(_get(raw, "raw_data"), "mentions", ())
        identifiers.update(str(_get(item, "id")) for item in mentions
                           if _get(item, "is_you") is True and _get(item, "id") is not None)
        identifiers.update((str(getattr(event.message_obj, "self_id", "")), "qq_official"))
    return identifiers


def official_mention_targets(event):
    """Other QQ mentions may remain in Plain, including debounced messages."""
    if _get(_get(event, "platform_meta"), "name") not in OFFICIAL_PLATFORMS:
        return ()
    raw = getattr(event.message_obj, "raw_message", None)
    mentions = _get(raw, "mentions") or _get(_get(raw, "raw_data"), "mentions", ())
    targets = tuple(str(_get(item, "id")) for item in mentions if _get(item, "id") is not None)
    return targets + tuple(match.group("short") or match.group("xml")
                 for part in event.message_obj.message if component_kind(part) == "Plain"
                 for match in QQ_MENTION.finditer(part.text))


def _official_original(event, identifiers):
    raw = getattr(event.message_obj, "raw_message", None)
    content = _get(raw, "content")
    if not isinstance(content, str):
        content = _get(_get(raw, "raw_data"), "content")
    if not isinstance(content, str):
        return None
    tokens = [match.group() for match in QQ_MENTION.finditer(content)
              if (match.group("short") or match.group("xml")) in identifiers]
    if not tokens:
        return None
    # Match the adapter's projection before using raw content: debounce and
    # other input transforms can leave raw_message pointing at an older body.
    pattern = "(?:" + "|".join(re.escape(token) for token in set(tokens)) + ")"
    projected = re.sub(r"(?:[ \t]*" + pattern + r"[ \t]*)+", " ", content).strip()
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import QQOfficialPlatformAdapter
    parse_face = getattr(QQOfficialPlatformAdapter, "_parse_face_message", lambda text: text)
    plain = "".join(part.text for part in event.message_obj.message if component_kind(part) == "Plain")
    # The host adapter may collapse the spaces around a removed At into a
    # single blank; compare after whitespace normalization so a stale raw body
    # is still rejected without losing a valid mention-only message.
    normalized = lambda value: " ".join(parse_face(value).split())
    if normalized(plain) not in (normalized(projected), normalized(content.strip())):
        return None
    restored = QQ_MENTION.sub(lambda match: SELF_MENTION
        if (match.group("short") or match.group("xml")) in identifiers else match.group(), content)
    return parse_face(restored).strip()


def _telegram_group_message(event):
    if (_get(_get(event, "platform_meta"), "name") == "telegram"
            and _get(event.message_obj.type, "value") == "GroupMessage"):
        return _get(_get(event.message_obj, "raw_message"), "message")
    return None


def _sent_by_bot(event, original):
    author = _get(original, "from_user")
    return (_get(author, "is_bot") is True
            and _get(author, "id") == _get(_get(event, "client"), "id"))


def telegram_reply_to_bot(event):
    return _sent_by_bot(event, reply_original(_telegram_group_message(event)))


def telegram_has_reply_prefix(event):
    # AstrBot prefixes even implicit references to a bot-created forum topic.
    message = _telegram_group_message(event)
    return bool(_get(message, "text")) and _sent_by_bot(event, _get(message, "reply_to_message"))


def telegram_plain_text(event, text):
    if telegram_has_reply_prefix(event):
        for marker in ("/ ", f"/@{event.message_obj.self_id} "):
            if text.startswith(marker):
                return text[len(marker):]
    return text


def _telegram_mention_projection(event, parts):
    """Use raw positions only if the SDK's current text still represents that input."""
    original = event.message_obj.raw_message.message
    body = original.text or original.caption or ""
    entities = original.entities if original.text else original.caption_entities
    parse = original.parse_entity if original.text else original.parse_caption_entity
    bot_name = event.message_obj.self_id
    source = body.encode("utf-16-le")
    restored, projected = bytearray(source), bytearray(source)
    has_mention = False
    for entity in reversed(entities or ()):
        start = entity.offset * 2
        end = start + entity.length * 2
        if entity.type == "mention" and parse(entity)[1:].casefold() == bot_name.casefold():
            restored[start:end] = SELF_MENTION.encode("utf-16-le")
            if original.text:
                del projected[start:end]
            has_mention = True
        elif entity.type == "text_link" and entity.url:
            suffix = f" ({entity.url})".encode("utf-16-le")
            restored[end:end] = suffix
            projected[end:end] = suffix
    if not has_mention:
        return None
    expected = projected.decode("utf-16-le")
    plains = [(i, telegram_plain_text(event, part.text)) for i, part in enumerate(parts)
              if component_kind(part) == "Plain"]
    matches = [i for i, value in plains if value == expected]
    if len(matches) == 1:
        return matches[0], restored.decode("utf-16-le")
    if not plains and not expected and any(component_kind(p) == "At" and p.qq == bot_name for p in parts):
        return len(parts), restored.decode("utf-16-le")
    return None


def _telegram_parts(event, parts):
    from .telegram_media import sticker_marker
    from .telegram_references import voice_caption_parts

    parts = voice_caption_parts(event.message_obj.raw_message.message, parts)
    mention = _telegram_mention_projection(event, parts)
    sticker = sticker_marker(event)
    result = []
    for index, part in enumerate(parts):
        kind = component_kind(part)
        if kind == "At":
            part = SimpleNamespace(type="Plain", text="")
        elif kind == "Plain":
            text = mention[1] if mention and mention[0] == index else telegram_plain_text(event, part.text)
            if sticker and text.startswith("Sticker: "):
                text = ""
            part = SimpleNamespace(type="Plain", text=text)
        result.append(part)
    if mention and mention[0] == len(parts):
        result.append(SimpleNamespace(type="Plain", text=mention[1]))
    if sticker:
        result.append(SimpleNamespace(type="Plain", text=sticker))
    return tuple(result)


def group_message_parts(event, account_id, *, native_parts=None):
    """Keep original order; fall back to native components on older hosts."""
    message = event.message_obj
    raw = getattr(message, "raw_message", None)
    segments = _get(raw, "message")
    if native_parts is None and isinstance(segments, list):
        return segments
    parts = tuple(message.message if native_parts is None else native_parts)
    if _get(_get(event, "platform_meta"), "name") == "telegram":
        return _telegram_parts(event, parts)
    if _get(_get(event, "platform_meta"), "name") not in OFFICIAL_PLATFORMS:
        return parts
    identifiers = bot_mention_ids(event, account_id)
    restored = _official_original(event, identifiers)
    if restored is None:
        return parts
    result, inserted = [], False
    for part in parts:
        kind = component_kind(part)
        if kind == "At" and str(part.qq) in identifiers:
            # Keep component indices stable for the full media intake path.
            result.append(SimpleNamespace(type="Plain", text=""))
            continue
        if kind == "Plain":
            if not inserted:
                result.append(SimpleNamespace(type="Plain", text=restored))
                inserted = True
            else:
                result.append(SimpleNamespace(type="Plain", text=""))
        else:
            result.append(part)
    if not inserted and restored:
        result.append(SimpleNamespace(type="Plain", text=restored))
    return result


def current_mention_text(target, event, account_id):
    if target is not None and str(target).strip() in bot_mention_ids(event, account_id):
        return SELF_MENTION
    return mention_text(target, account_id)


def group_message_text(event, account_id):
    """Text and mentions for the model, folding trusted upstream voice transcripts cleanly."""
    stt_text = trusted_stt_transcript(event)
    text = []
    stt_used = False
    for part in group_message_parts(event, account_id):
        kind = component_kind(part)
        data = part.get("data", {}) if isinstance(part, Mapping) else part
        if kind in ("Plain", "text"):
            piece = _get(data, "text", "")
            if stt_text and isinstance(piece, str) and piece.strip() == "[语音消息]" and not stt_used:
                text.append(VOICE_TRANSCRIPT_PREFIX + stt_text)
                stt_used = True
            else:
                if stt_text and isinstance(piece, str) and stt_text in piece:
                    stt_used = True
                text.append(piece)
        elif kind in ("At", "at"):
            text.append(current_mention_text(_get(data, "qq"), event, account_id))
        elif kind in ("Record", "record") and stt_text and not stt_used:
            text.append(VOICE_TRANSCRIPT_PREFIX + stt_text)
            stt_used = True
    return "".join(text)
