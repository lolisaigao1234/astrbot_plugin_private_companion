"""Telegram's private-text boundary, without importing its SDK."""


def private_text_event(event):
    """Check the private-message facts not enforced by AstrBot's converter.

    AstrBot only converts Update.message. The raw message check also excludes
    button interaction events, which use the adapter's separate callback route.
    Private topics need an explicit check because the adapter shares their UMO
    with the main private chat.
    """
    try:
        message = event.message_obj
        raw = message.raw_message.message
        sender = raw.from_user
        return (raw.chat.type == "private" and sender.is_bot is False
                and raw.chat.id == sender.id and str(sender.id) == str(message.sender.user_id)
                and isinstance(raw.text, str) and bool(raw.text.strip())
                and getattr(raw, "message_thread_id", None) is None
                and not getattr(raw, "is_topic_message", False)
                and getattr(raw, "direct_messages_topic", None) is None)
    except (AttributeError, TypeError, ValueError):
        return False


def explicit_target(settings, target):
    """Telegram admission never inherits a wildcard or a window alias."""
    return (target is not None and target in settings.targets
            and target.participant_id != "*"
            and settings.window_link_for_target(target.target_id) is None)
