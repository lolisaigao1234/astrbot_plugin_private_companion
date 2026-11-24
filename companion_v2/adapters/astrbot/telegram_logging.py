"""Protect Bot API credentials in AstrBot warnings and HTTPX request logs."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from threading import Lock


_tokens = ContextVar("telegram_log_tokens", default=())
_loggers = (logging.getLogger("astrbot"), logging.getLogger("httpx"))
_lock = Lock()
_active_sends = 0


def _redact(record):
    tokens = _tokens.get()
    if not tokens:
        return True

    try:
        message = record.getMessage()
    except Exception:
        # Some handlers accept records that do not support %-formatting.
        message = None
    cleaned = message
    if message is not None:
        for token in tokens:
            cleaned = cleaned.replace(token, "[redacted]")
    if cleaned == message:
        # Structured handlers can serialize args even when a format omits a key.
        try:
            raw = repr((record.msg, record.args))
        except Exception:
            return True
        if not any(token in raw for token in tokens):
            return True
    record.msg, record.args = cleaned if cleaned is not None else "[redacted]", ()
    return True


@contextmanager
def redact_telegram_token(client):
    global _active_sends
    token = getattr(client, "token", None)
    if not isinstance(token, str) or not token:
        yield
        return

    # The loggers are shared across threads, but credentials belong to the
    # sending task. Keep a single filter installed until the last send exits.
    with _lock:
        if not _active_sends:
            for logger in _loggers:
                logger.addFilter(_redact)
        _active_sends += 1
    state = _tokens.set(tuple(sorted((*_tokens.get(), token), key=len, reverse=True)))
    try:
        yield
    finally:
        _tokens.reset(state)
        with _lock:
            _active_sends -= 1
            if not _active_sends:
                for logger in _loggers:
                    logger.removeFilter(_redact)
