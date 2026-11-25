"""Protect Bot API credentials in AstrBot warnings and HTTPX request logs."""

from contextlib import contextmanager, suppress
from contextvars import ContextVar
import logging
from threading import Lock
import traceback

from companion_v2.execution.models import TaskError


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
        if record.exc_info:
            # AstrBot's log sink prints frame locals, including the bot object.
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record.msg, record.args, record.exc_info = message, (), None
    except Exception:
        # Some handlers accept records that do not support %-formatting.
        message = record.exc_info = None
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


def redact_event_token(event):
    telegram = getattr(getattr(event, "platform_meta", None), "name", None) == "telegram"
    return redact_telegram_token(getattr(event, "client", None) if telegram else None)


def redact_event_error(event, error):
    """Let a failed Telegram send leave without the credential or the frames that hold the bot."""
    telegram = getattr(getattr(event, "platform_meta", None), "name", None) == "telegram"
    token = getattr(getattr(event, "client", None), "token", None)
    if not telegram or not isinstance(token, str) or not token or not isinstance(error, Exception):
        return error
    if not isinstance(error, TaskError):
        with redact_event_token(event):
            logging.getLogger("astrbot").warning("Telegram reply was not sent", exc_info=error)
    error.args = tuple(arg.replace(token, "[redacted]") if isinstance(arg, str) else arg for arg in error.args)
    with suppress(AttributeError, TypeError):
        error.message = error.message.replace(token, "[redacted]")
    error.__cause__ = error.__context__ = None
    error = error.with_traceback(None)
    if token in str(error) + "".join(traceback.format_exception(error)):
        # Some errors build their text from attributes that args cannot replace.
        error = RuntimeError(f"{type(error).__name__}: {error}".replace(token, "[redacted]"))
        error.__suppress_context__ = True
    return error
