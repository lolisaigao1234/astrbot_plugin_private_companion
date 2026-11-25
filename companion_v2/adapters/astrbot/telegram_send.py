"""Keep implicit Telegram typing out of Companion's private message delivery."""

from functools import wraps
from types import MethodType

from .telegram_logging import redact_telegram_token


class _DeliveryClient:
    """Use the live bot for delivery and leave typing to the foreground lease."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def send_chat_action(self, chat_id, action, **kwargs):
        if action == "typing":
            return True
        return await self._client.send_chat_action(chat_id=chat_id, action=action, **kwargs)


class _DeliveryReceiver:
    """Bind SDK methods to a per-send client while keeping state on the owner."""

    def __init__(self, owner, **overrides):
        object.__setattr__(self, "_owner", owner)
        vars(self).update(overrides)

    @property
    def __class__(self):
        # Python's super() accepts a proxy reporting the receiver's class.
        return type(self._owner)

    def __getattr__(self, name):
        value = getattr(self._owner, name)
        if isinstance(value, MethodType) and value.__self__ is self._owner:
            return MethodType(value.__func__, self)
        return value

    def __setattr__(self, name, value):
        if name in vars(self):
            object.__setattr__(self, name, value)
        else:
            setattr(self._owner, name, value)

    def __delattr__(self, name):
        if name in vars(self):
            object.__delattr__(self, name)
        else:
            delattr(self._owner, name)


def _private_adapter_for_delivery(platform, session):
    from astrbot.core.platform.message_type import MessageType

    if session.message_type != MessageType.FRIEND_MESSAGE or getattr(platform.meta(), "name", None) != "telegram":
        return platform
    from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter

    if not isinstance(platform, TelegramPlatformAdapter):
        return platform
    return _DeliveryReceiver(platform, client=_DeliveryClient(platform.client))


async def send_private_session(platform, session, message):
    """Send through the session adapter with implicit typing suppressed."""
    local = _private_adapter_for_delivery(platform, session)
    return await local.send_by_session(session, message)


async def send_context_message(context, session, message):
    """Run the native Context send with a private, per-call adapter view."""
    send = context.send_message
    from astrbot.core.star.context import Context

    if isinstance(context, Context) and isinstance(send, MethodType) and send.__self__ is context:
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        try:
            target = MessageSession.from_str(session) if isinstance(session, str) else session
        except ValueError:
            return await send(session, message)
        if isinstance(target, MessageSession) and target.message_type == MessageType.FRIEND_MESSAGE:
            manager = context.platform_manager
            for platform in manager.platform_insts:
                if platform.meta().id != target.platform_name:
                    continue
                local = _private_adapter_for_delivery(platform, target)
                if local is not platform:
                    # Context owns matching, send tracing and its return value.
                    platforms = [local if item is platform else item for item in manager.platform_insts]
                    local_manager = _DeliveryReceiver(manager, platform_insts=platforms)
                    local_context = _DeliveryReceiver(context, platform_manager=local_manager)
                    with redact_telegram_token(platform.client):
                        return await local_context.send_message(session, message)
                break
    return await send(session, message)


def suppress_private_event_typing(event):
    """Use a delivery-only bot view for the managed event's native sends."""
    from astrbot.core.platform.astr_message_event import streaming_response_event
    from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent

    if not isinstance(event, TelegramPlatformEvent):
        return

    def wrap(method):
        @wraps(method)
        async def send(*args, **kwargs):
            local = _DeliveryReceiver(event, client=_DeliveryClient(event.client))
            # The SDK uses receiver identity to avoid recording a stream twice.
            token = streaming_response_event.set(local) if streaming_response_event.get() is event else None
            try:
                with redact_telegram_token(event.client):
                    return await method.__func__(local, *args, **kwargs)
            finally:
                if token is not None:
                    streaming_response_event.reset(token)

        return send

    for name in ("send", "send_streaming"):
        method = getattr(event, name, None)
        if isinstance(method, MethodType) and method.__self__ is event:
            setattr(event, name, wrap(method))
