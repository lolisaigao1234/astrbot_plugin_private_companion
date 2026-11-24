"""Identify the replied-to message in a Telegram update."""


def reply_original(message):
    original = getattr(message, "reply_to_message", None)
    # Forum posts can point at the topic creation service message implicitly.
    if original and message.is_topic_message and message.message_thread_id == original.message_id:
        return None
    return original
