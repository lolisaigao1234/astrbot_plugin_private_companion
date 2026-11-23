"""Protect the Bot API credential while AstrBot logs a Telegram send."""

from contextlib import contextmanager
import logging


@contextmanager
def redact_telegram_token(client):
    """AstrBot can log the Bot API error text while sending a chat action."""
    token = getattr(client, "token", None)

    if not token:
        yield
        return

    def redact(record):
        message = record.getMessage()
        if token in message:
            record.msg, record.args = message.replace(token, "[redacted]"), ()
        return True

    host_logger = logging.getLogger("astrbot")
    host_logger.addFilter(redact)
    try:
        yield
    finally:
        host_logger.removeFilter(redact)
