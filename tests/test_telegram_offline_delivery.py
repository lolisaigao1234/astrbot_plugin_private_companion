"""Exercise AstrBot's Telegram converter and Bot API requests offline."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest
from urllib.parse import parse_qs

import httpx
from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot
from telegram.request import HTTPXRequest

from tests.test_telegram_platform import OWNER, TelegramHostFixture, telegram_event
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType
from companion_v2.adapters.astrbot.platform_sender import AstrBotPlatformSender
from companion_v2.delivery.models import DeliveryEnvelope
from companion_v2.execution.models import ProviderBinding, digest


class TelegramOfflineDeliveryTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.before_response = None

        async def respond(request):
            method = request.url.path.rsplit("/", 1)[-1]
            self.requests.append((method, request.content))
            if self.before_response is not None:
                await self.before_response(method, request)
            if method == "getMe":
                result = {"id": 424242, "is_bot": True, "first_name": "Synthetic", "username": "synthetic_bot"}
            elif method in {"sendChatAction", "sendMessageDraft"}:
                result = True
            else:
                result = {"message_id": 1, "date": 1, "chat": {"id": OWNER, "type": "private"}}
            return httpx.Response(200, json={"ok": True, "result": result})

        def request():
            return HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})

        self.bot = ExtBot("424242:synthetic", request=request(), get_updates_request=request())
        self.platform.client = self.bot
        await self.bot.initialize()
        self.addAsyncCleanup(self.bot.shutdown)
        self.requests.clear()
        self.app = await self.start_host()
        self.context, _ = await self.controller.session.synchronize(self.controller.settings.target("tg-owner"))
        self.binding = ProviderBinding("message.delivery", "synthetic-sender", "1", digest("synthetic"))
        self.sender = AstrBotPlatformSender(self.controller.session.bridge, binding=self.binding,
            version="4.29.4", reverse_api_type=object, authorize=lambda: None)

    async def background_send(self, text):
        envelope = DeliveryEnvelope("part-offline", "op-offline", self.context, 1, text, digest(text),
                                    self.binding, before_dispatch=lambda: True)
        return await self.sender.send(envelope)

    async def managed_event(self):
        self.platform._event_queue = asyncio.Queue()
        update = Update(1, message=Message(1, date=datetime.now(timezone.utc),
            chat=Chat(OWNER, "private"), from_user=User(OWNER, "Synthetic", False), text="question"))
        await self.platform.message_handler(update, NS(bot=self.bot))
        event = self.platform._event_queue.get_nowait()
        event.set_extra("activated_handlers", [])
        event.created_at = self.clock.now().timestamp()
        plugin = self.started_plugin()
        await plugin.receive_companion_message(event)
        return plugin, event

    async def test_disconnected_background_send_makes_no_requests_then_delivers_without_typing(self):
        self.platform.application.updater.running = False
        receipt = await self.background_send("**background**")
        self.assertEqual((receipt.status, receipt.failure_code), ("not_sent", "host_account_offline"))
        self.assertEqual(self.requests, [])

        self.platform.application.updater.running = True
        receipt = await self.background_send("**background**")
        self.assertEqual(receipt.status, "delivered")
        self.assertEqual([method for method, _ in self.requests], ["sendMessage"])
        fields = parse_qs(self.requests[0][1].decode())
        self.assertEqual(fields["chat_id"], [str(OWNER)])
        self.assertEqual(fields["parse_mode"], ["MarkdownV2"])
        self.assertEqual(fields["text"][0].strip(), "*background*")

    async def test_managed_final_reply_does_not_type_while_unmanaged_send_uses_same_bot(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def block_managed_text(method, request):
            if method == "sendMessage" and b"managed+final" in request.content:
                entered.set()
                await release.wait()

        self.before_response = block_managed_text
        plugin, event = await self.managed_event()
        result = MessageEventResult().message("managed final").set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)
        await plugin.guard_companion_core_delivery(event)
        final = asyncio.create_task(event.send(MessageChain([Plain("managed final")])))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            unmanaged = telegram_event(self.platform, peer=OWNER + 1)
            await unmanaged.send(MessageChain([Plain("independent")]))
        finally:
            release.set()
            await final
        self.assertTrue(event._has_send_oper)
        self.assertEqual([method for method, _ in self.requests], ["sendMessage", "sendChatAction", "sendMessage"])
        self.assertIn(b"chat_id=" + str(OWNER + 1).encode(), self.requests[1][1])

    async def test_authorized_foreground_heartbeat_then_revoked_grant_and_background_delivery(self):
        self.enable_typing(interval=10)
        pulse = asyncio.Event()

        async def observe_heartbeat(method, request):
            if method == "sendChatAction":
                pulse.set()

        self.before_response = observe_heartbeat
        presence = self.app.presence
        presence.begin_native_reply(self.context, "offline-turn")
        try:
            await asyncio.wait_for(pulse.wait(), 5)
            self.assertEqual(parse_qs(self.requests[0][1].decode())["action"], ["typing"])
        finally:
            presence.finish_native_reply(self.context, "offline-turn")

        identities = self.controller.session.bridge.identities
        grant = next(g for g in identities.grants_for_subject(self.context.actor.subject)
                     if g.capability == "platform.typing")
        identities.revoke_grant(grant.grant_id, revoked_at=self.clock.now(), expected_revision=identities.revision)
        self.requests.clear()
        self.assertEqual((await presence.port.typing(self.context))["state"], "not_started")
        self.assertEqual((await self.background_send("still allowed")).status, "delivered")
        self.assertEqual([method for method, _ in self.requests], ["sendMessage"])

    async def test_managed_stream_sends_media_and_final_text_without_typing(self):
        from astrbot.core.message.components import Image
        from tests.test_conversation_images import image_bytes

        plugin, event = await self.managed_event()
        event.set_result(MessageEventResult().set_result_content_type(ResultContentType.STREAMING_RESULT))
        await plugin.guard_companion_core_delivery(event)
        image = Image.fromBytes(image_bytes())

        async def chunks():
            yield MessageChain([Plain("stream final")])
            yield MessageChain([image])

        await event.send_streaming(chunks())
        self.assertTrue(event._has_send_oper)
        actions = [parse_qs(body.decode())["action"][0] for method, body in self.requests
                   if method == "sendChatAction"]
        self.assertEqual(actions, ["upload_photo"])
        deliveries = [(method, body) for method, body in self.requests if method in {"sendPhoto", "sendMessage"}]
        self.assertEqual([method for method, _ in deliveries], ["sendPhoto", "sendMessage"])
        self.assertIn(image_bytes(), deliveries[0][1])
        self.assertIn(b"stream+final", deliveries[1][1])

    async def test_native_context_keeps_private_delivery_and_trace(self):
        from astrbot.core.platform.astr_message_event import current_message_event
        from astrbot.core.star.context import Context
        from companion_v2.adapters.astrbot.telegram_send import send_context_message

        context = Context.__new__(Context)
        context.platform_manager = NS(platform_insts=[self.platform])
        context.astrbot_config_mgr = NS(get_conf=lambda _: {})
        event = telegram_event(self.platform)
        event._execution_active = True
        recorded = []
        event.trace = NS(record=lambda stage, **fields: recorded.append((stage, fields["target_umo"])))
        token = current_message_event.set(event)
        try:
            self.assertIs(await send_context_message(
                context, event.unified_msg_origin, MessageChain([Plain("native context")])), True)
        finally:
            current_message_event.reset(token)
        self.assertEqual(recorded, [("response_sent", event.unified_msg_origin)])
        self.assertEqual([method for method, _ in self.requests], ["sendMessage"])
        self.assertIs(context.platform_manager.platform_insts[0], self.platform)
        self.assertIs(self.platform.client, self.bot)

    async def test_subclass_context_can_delegate_native_send_without_implicit_typing(self):
        from astrbot.core.star.context import Context
        from companion_v2.adapters.astrbot.telegram_send import send_context_message

        class WrappedContext(Context):
            async def send_message(self, session, message):
                self.calls += 1
                delivered = await super().send_message(session, message)
                return ("wrapped", delivered)

        context = WrappedContext.__new__(WrappedContext)
        context.calls = 0
        context.platform_manager = NS(platform_insts=[self.platform])
        context.astrbot_config_mgr = NS(get_conf=lambda _: {})
        event = telegram_event(self.platform)
        result = await send_context_message(context, event.unified_msg_origin, MessageChain([Plain("context")]))
        self.assertEqual(result, ("wrapped", True))
        self.assertEqual(context.calls, 1)
        self.assertIs(context.platform_manager.platform_insts[0], self.platform)
        self.assertEqual([method for method, _ in self.requests], ["sendMessage"])

    async def test_subclass_sends_keep_override_state_and_isolate_typing(self):
        from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
        from companion_v2.adapters.astrbot.telegram_send import send_private_session, suppress_private_event_typing
        from tests.test_telegram_platform import TelegramPlatform

        class Adapter(TelegramPlatform):
            async def send_by_session(self, session, message):
                self.session_calls += 1
                await super().send_by_session(session, message)
                return "session result"

        class Event(TelegramPlatformEvent):
            async def send(self, message):
                self.event_calls += 1
                await super().send(message)
                return "event result"

            async def send_streaming(self, *args, **kwargs):
                self.event_calls += 1
                await self._ensure_typing(self.get_sender_id(), None)
                await super().send_streaming(*args, **kwargs)
                return "stream result"

        platform = Adapter()
        platform.client, platform.session_calls = self.bot, 0
        native = telegram_event(platform)
        event = Event(native.message_str, native.message_obj, platform.meta(), str(OWNER), self.bot)
        event.event_calls = 0
        suppress_private_event_typing(event)
        message = MessageChain([Plain("managed")])

        async def chunks():
            yield message

        for send, expected in (
            (lambda: send_private_session(platform, event.session, message), "session result"),
            (lambda: event.send(message), "event result"),
            (lambda: event.send_streaming(chunks()), "stream result"),
        ):
            with self.subTest(expected=expected):
                self.requests.clear()
                entered, release = asyncio.Event(), asyncio.Event()

                async def pause(method, request):
                    if method == "sendMessage" and b"text=managed" in request.content:
                        entered.set()
                        await release.wait()

                self.before_response = pause
                task = asyncio.create_task(send())
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    await telegram_event(platform, peer=OWNER + 1).send(MessageChain([Plain("other")]))
                finally:
                    release.set()
                    result = await task
                self.assertEqual(result, expected)
                actions = [parse_qs(body.decode())["chat_id"] for method, body in self.requests
                           if method == "sendChatAction"]
                self.assertEqual(actions, [[str(OWNER + 1)]])
        self.assertEqual((platform.session_calls, event.event_calls), (1, 2))
        self.assertTrue(event._has_send_oper)
