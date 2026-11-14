"""Telegram's private-text boundary, without importing its SDK."""

from companion_v2.conversation.native_quotes import NativeTextQuote


def text_quote(event):
    """Freeze only the receiver's embedded same-chat text, never fetch a message.

    8000 characters is our snapshot budget, not a Telegram transport limit.
    Reply.message_str already contains AstrBot's partial-quote/link processing.
    """
    raw = event.message_obj.raw_message.message
    original = getattr(raw, "reply_to_message", None)
    if (original is None or original.chat.id != raw.chat.id or original.chat.type != "private"
            or original.from_user is None or not original.text):
        return ()
    replies = [(position, part) for position, part in enumerate(event.message_obj.message)
               if getattr(getattr(part, "type", None), "value", None) == "Reply"]
    if len(replies) != 1:
        return ()
    position, reply = replies[0]
    if (str(reply.id) != str(original.message_id) or str(reply.id) == str(raw.message_id)
            or str(reply.sender_id) != str(original.from_user.id)
            or not isinstance(reply.message_str, str) or not reply.message_str.strip()):
        return ()
    return (NativeTextQuote(position, str(original.message_id), str(original.from_user.id),
        int(original.date.timestamp()), reply.message_str[:8000],
        sender_is_self=original.from_user.id == event.client.id, partial=raw.quote is not None,
        truncated=len(reply.message_str) > 8000),)


def event_session(event):
    """Return the private chat of one human, topic-free text message.

    AstrBot only converts ``Update.message``. The raw message check also excludes
    button interactions, which use the adapter's separate callback route.
    Private topics need an explicit check because the adapter shares their
    session with the main private chat.
    """
    try:
        message = event.message_obj
        raw = message.raw_message.message
        chat, sender = raw.chat, raw.from_user
        admitted = (chat.type == "private" and sender.is_bot is False
                    and type(chat.id) is int and chat.id == sender.id
                    and str(sender.id) == str(message.sender.user_id)
                    and isinstance(raw.text, str) and bool(raw.text.strip())
                    and getattr(raw, "message_thread_id", None) is None
                    and not getattr(raw, "is_topic_message", False)
                    and getattr(raw, "direct_messages_topic", None) is None)
    except (AttributeError, TypeError):
        admitted = False
    if not admitted:
        raise ValueError("Telegram event is not a human private text message")
    return str(chat.id)


def private_text_event(event):
    try:
        event_session(event)
        return True
    except ValueError:
        return False


def explicit_target(settings, target):
    """Telegram admission never inherits a wildcard or a window alias."""
    return (target is not None and target in settings.targets
            and target.participant_id != "*"
            and settings.window_link_for_target(target.target_id) is None)


def application_running(platform, client):
    """Whether ``client`` is the bot of the adapter's current, running application.

    ``application.running`` is the SDK's public lifecycle flag. The two
    adapter-private flags only narrow it further (restart and shutdown windows);
    a host that renames them still answers on the public one.
    """
    try:
        application = platform.application
        return (platform.get_client() is client and application.bot is client
                and application.running is True
                and getattr(platform, "_application_started", True) is True
                and not getattr(platform, "_terminating", False)
                and type(client.id) is int and client.id > 0)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
