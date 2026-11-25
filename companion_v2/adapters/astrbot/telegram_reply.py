"""Identify the replied-to message in a Telegram update and the prefix AstrBot adds for it."""


def reply_original(message):
    original = getattr(message, "reply_to_message", None)
    # Forum posts can point at the topic creation service message implicitly.
    if original and message.is_topic_message and message.message_thread_id == original.message_id:
        return None
    return original


def _telegram_group_message(event):
    if (getattr(getattr(event, "platform_meta", None), "name", None) == "telegram"
            and getattr(event.message_obj.type, "value", None) == "GroupMessage"):
        return getattr(getattr(event.message_obj, "raw_message", None), "message", None)
    return None


def _sent_by_bot(event, original):
    author = getattr(original, "from_user", None)
    return (getattr(author, "is_bot", None) is True
            and author.id == getattr(getattr(event, "client", None), "id", None))


def telegram_reply_to_bot(event):
    return _sent_by_bot(event, reply_original(_telegram_group_message(event)))


def telegram_has_reply_prefix(event):
    # AstrBot prefixes even implicit references to a bot-created forum topic.
    message = _telegram_group_message(event)
    return bool(getattr(message, "text", None)) and _sent_by_bot(event, getattr(message, "reply_to_message", None))


def telegram_plain_text(event, text):
    if telegram_has_reply_prefix(event):
        for marker in ("/ ", f"/@{event.message_obj.self_id} "):
            if text.startswith(marker):
                return text[len(marker):]
    return text


def align_group_wake(event, native_context):
    """Wake on real replies to the bot; in a topic the bot created, judge only what was typed."""
    reply = telegram_reply_to_bot(event)
    if reply:
        event.is_at_or_wake_command = True
    if not telegram_has_reply_prefix(event):
        return
    # AstrBot put its reply marker in front of the text before looking for a wake prefix.
    message = event.message_obj
    plain = telegram_plain_text(event, message.message_str).strip()
    if reply:
        if event.message_str.strip() == message.message_str.strip():
            event.message_str = plain
        return
    # Repeat AstrBot's waking rules on the text without the marker.
    mentions = [str(getattr(part, "qq", "")) for part in message.message]
    prefixes = native_context.get_config(umo=event.unified_msg_origin).get("wake_prefix", ())
    typed = None
    if mentions[0] in ("", message.self_id):
        typed = next((prefix for prefix in prefixes if plain.startswith(prefix)), None)
    event.is_at_or_wake_command = message.self_id in mentions or typed is not None
    event.message_str = plain.removeprefix(typed or "").strip()
