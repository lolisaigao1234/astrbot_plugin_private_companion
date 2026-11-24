"""Keep implicit Telegram typing out of Companion's private message delivery."""

from copy import copy

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


def _private_adapter_for_delivery(platform, session):
    from astrbot.core.platform.message_type import MessageType

    if session.message_type != MessageType.FRIEND_MESSAGE or getattr(platform.meta(), "name", None) != "telegram":
        return platform
    from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter

    if getattr(platform.send_by_session, "__func__", None) is not TelegramPlatformAdapter.send_by_session:
        return platform
    # The native adapter reads self.client and then delegates to the SDK
    # converter and Platform metric hook. Keep that entire path per send.
    local = copy(platform)
    local.client = _DeliveryClient(platform.client)
    return local


async def send_private_session(platform, session, message):
    """Send through the session adapter with implicit typing suppressed."""
    local = _private_adapter_for_delivery(platform, session)
    return await local.send_by_session(session, message)


async def send_context_message(context, session, message):
    """Run the native Context send with a private, per-call adapter view."""
    send = context.send_message
    from astrbot.core.star.context import Context

    if getattr(send, "__func__", None) is Context.send_message:
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
                    # Limit the view to the first match it would have used.
                    local_context = copy(context)
                    local_context.platform_manager = copy(manager)
                    local_context.platform_manager.platform_insts = [local]
                    with redact_telegram_token(platform.client):
                        return await local_context.send_message(session, message)
                break
    return await send(session, message)


def suppress_private_event_typing(event):
    """Use a delivery-only bot view for the managed event's native sends."""
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent

    if getattr(event.send, "__func__", None) is TelegramPlatformEvent.send:
        async def send(message):
            client = event.client
            with redact_telegram_token(client):
                await event.send_with_client(
                    _DeliveryClient(client), message, event.get_sender_id(),
                )
            # Keep the original event's trace, metric and send flag.
            return await AstrMessageEvent.send(event, message)

        event.send = send

    original_stream = getattr(event, "send_streaming", None)
    if getattr(original_stream, "__func__", None) is TelegramPlatformEvent.send_streaming:
        async def send_streaming(*args, **kwargs):
            local = copy(event)
            local.client = _DeliveryClient(event.client)
            with redact_telegram_token(event.client):
                result = await TelegramPlatformEvent.send_streaming(local, *args, **kwargs)
            if local._has_send_oper:
                event._has_send_oper = True
            return result

        event.send_streaming = send_streaming
