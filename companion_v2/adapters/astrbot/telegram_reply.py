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


def align_group_wake(event):
    """Wake on real replies to the bot, and not on the prefix AstrBot adds in a topic the bot created."""
    if telegram_reply_to_bot(event):
        event.is_at_or_wake_command = True
    elif getattr(event, "is_at_or_wake_command", False) is True and telegram_has_reply_prefix(event):
        message = event.message_obj
        marked, remaining = message.message_str.strip(), event.message_str.strip()
        removed = marked.removesuffix(remaining).strip() if marked.endswith(remaining) else "/"
        mentioned = any(str(getattr(part, "qq", "")) == message.self_id for part in message.message)
        event.is_at_or_wake_command = mentioned or message.raw_message.message.text.startswith(removed)
