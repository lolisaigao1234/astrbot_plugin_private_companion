"""Telegram private text: the bot is the event's ``client``, and a rebuilt bot keeps the chat admitted."""

from tests import astrbot_data_root  # noqa: F401 - isolate the SDK before import

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

import httpx

from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram import Chat, Message, Update, User
from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
from astrbot_host.display_names import event_person_names
from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.recalls import authenticate_notice_route
from companion_v2.adapters.astrbot.group import group_event_route
from companion_v2.adapters.astrbot.native import event_native_client, native_client_connected, supported_platform_name
from companion_v2.adapters.astrbot.platform_sender import AstrBotPlatformSender
from companion_v2.delivery.models import DeliveryEnvelope
from companion_v2.execution.models import ProviderBinding, TaskError, digest, require
from companion_v2.foundation.refs import ScopeKind
from tests.multi_host_support import MultiFixture, multi_config
from tests.test_main_no_reply import ENTRY

OWNER = 5123456789012


def telegram_bot(token="424242:synthetic"):
    return NS(token=token)


class TelegramPlatform(TelegramPlatformAdapter):
    def __init__(self):
        self.config = {"id": "tg-main"}
        self.client = telegram_bot()
        self.application = NS(running=True, updater=NS(running=True))
        self._application_started = True
        self._terminating = False


def telegram_event(platform, number=1, text="你好 😀", peer=OWNER):
    message = AstrBotMessage()
    message.type, message.self_id, message.message_id = MessageType.FRIEND_MESSAGE, "synthetic_bot", str(number)
    message.session_id = str(peer)
    message.sender, message.message, message.message_str = MessageMember(str(peer), "Synthetic"), [Plain(text)], text
    message.raw_message = Update(number, message=Message(number, date=datetime.now(timezone.utc),
                                                         chat=Chat(peer, "private"),
                                                         from_user=User(peer, "Synthetic", False), text=text))
    event = TelegramPlatformEvent(text, message, platform.meta(), str(peer), platform.client)
    event.set_extra("activated_handlers", [])
    return event


class TelegramPlatformTests(unittest.TestCase):
    def test_names_come_only_from_the_original_message_sender(self):
        event = telegram_event(TelegramPlatform())
        event.message_obj.sender.nickname = "Unknown"
        quoted = event.message_obj.raw_message.message
        for first, last, username, sender_id, expected in (
            ("小", "林", "handle", OWNER, "小 林"),
            ("小林", None, "handle", OWNER, "小林"),
            ("", None, "handle", OWNER, "handle"),
            ("", None, None, OWNER, ""),
            ("Unknown", None, "Unknown", OWNER, ""),
            ("其他人", None, "other", OWNER + 1, ""),
        ):
            with self.subTest(expected=expected, sender_id=sender_id):
                author = User(sender_id, first, False, last_name=last, username=username)
                event.message_obj.raw_message = Update(1, message=Message(
                    1, date=quoted.date, chat=quoted.chat, from_user=author,
                    reply_to_message=quoted, text="@other"))
                self.assertEqual(event_person_names(event), (expected, ""))

    def test_connection_tracks_polling_shutdown_and_rebuild(self):
        platform = TelegramPlatform()
        for started, running, polling, terminating, expected in (
            (True, True, True, False, True),
            (True, True, False, False, False),
            (True, True, True, True, False),
            (False, True, True, False, False),
            (True, False, True, False, False),
            (True, True, True, False, True),
        ):
            with self.subTest(started=started, running=running, polling=polling, terminating=terminating):
                platform._application_started, platform._terminating = started, terminating
                platform.application.running, platform.application.updater.running = running, polling
                self.assertEqual(native_client_connected(platform, "tg-main"), expected)

    def test_running_application_without_start_flag_requires_the_right_account(self):
        from companion_v2.adapters.astrbot.native import native_client_connected

        platform = TelegramPlatform()
        del platform._application_started
        self.assertTrue(native_client_connected(platform, "tg-main"))
        self.assertFalse(native_client_connected(platform, "other-platform"))
        platform.application.updater.running = False
        self.assertFalse(native_client_connected(platform, "tg-main"))

    def test_the_bot_is_the_event_client(self):
        event = telegram_event(TelegramPlatform())
        self.assertTrue(supported_platform_name("telegram"))
        self.assertIs(event_native_client(None, event), event.client)

    def test_group_messages_are_not_served(self):
        event = telegram_event(TelegramPlatform())
        event.message_obj.type = MessageType.GROUP_MESSAGE
        event.session.message_type = MessageType.GROUP_MESSAGE
        with self.assertRaises(TaskError) as refused:
            group_event_route(event)
        self.assertEqual(refused.exception.code, "host_route_mismatch")


class TelegramHostFixture(MultiFixture):
    def setUp(self):
        super().setUp()
        self.platform = self.native.platforms["tg-main"] = TelegramPlatform()
        self.native.selections["tg-main"], self.native.conversations["tg-main"] = "original", "tg-conversation"
        self.window_personas[f"tg-main:FriendMessage:{OWNER}"] = "original"
        self.types = replace(self.types, event=TelegramPlatformEvent)
        self.native.get_config = lambda umo=None: {"platform": [dict(id="tg-main", type="telegram", enable=True)]}
        self.controller = self.make_controller(multi_config(
            accounts=[dict(platform_id="tg-main", account_id="tg-main", bot_id="bot-tg")],
            targets=[dict(target_id="tg-owner", platform_id="tg-main", account_id="tg-main",
                          bot_id="bot-tg", participant_id=str(OWNER))],
            personas=[dict(bot_id="bot-tg", persona_id="original", role_id="role-tg")], group_targets=[]))

    async def admitted(self, plugin, number):
        event = telegram_event(self.platform, number)
        event.created_at = self.clock.now().timestamp()
        await plugin.receive_companion_message(event)
        observer = self.controller.session.core_reply_observations
        while pending := observer._pending - observer._intervention_tasks:
            await asyncio.gather(*tuple(pending))
            await asyncio.sleep(0)
        prepared = (event.get_extra(CORE_REPLY_CONTEXT_EXTRA) or {}).get("prepared")
        return prepared and prepared["context"].actor.subject.subject_id

    def bound_person(self):
        authority, target = self.controller.authority, self.controller.settings.target("tg-owner")
        return require(authority.identities.resolve_authenticated(
            authority.principal(target), observed_at=self.clock.now())).subject.subject_id

    def started_plugin(self):
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        plugin.staging, plugin._host_start_task = self.controller, None
        return plugin

    async def connect_late(self, plugin):
        """AstrBot loads the channel, then announces it to the running plugin."""
        self.native.platforms["tg-main"] = self.platform
        await plugin.on_companion_platform_loaded()
        if plugin._host_start_task is not None:
            await asyncio.wait_for(plugin._host_start_task, timeout=20)


class TelegramPresenceTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await self.start_host()
        self.context, _ = await self.controller.session.synchronize(self.controller.settings.target("tg-owner"))
        self.action = self.platform.client.send_chat_action = AsyncMock(return_value=True)
        self.presence = self.app.presence

    async def test_typing_redacts_httpx_request(self):
        self.enable_typing()
        bot = self.platform.client
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as http:
            async def chat_action(**kwargs):
                await http.post(f"https://telegram.invalid/bot{bot.token}/sendChatAction")
                return True

            self.action.side_effect = chat_action
            with self.assertLogs("httpx", "INFO") as logs:
                result = await self.presence.port.typing(self.context)
        self.assertEqual(result["state"], "accepted")
        record, = logs.records
        self.assertIn("/bot[redacted]/sendChatAction", record.getMessage())
        self.assertNotIn(bot.token, repr(vars(record)))

    def enable_typing(self, interval=1):
        from astrbot_host.management_identity import dashboard_operator
        from companion_v2.role_work.state.management import StateManagement
        manager = StateManagement(self.app, self.context)
        operator = dashboard_operator(self.app.runtime, "synthetic", at=self.clock.now())
        status = manager.status(operator).unwrap()
        manager.command(operator, dict(action="configure", operation_key="typing-on",
            edit_token=status["edit_token"], role_edit_token=status["role_edit_token"],
            changes={"typing_enabled": True, "typing_refresh_seconds": interval})).unwrap()

    async def run_to_host_waiting_hook(self, number, *, peer=OWNER):
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal

        # Follow Telegram's ordinary update -> adapter queue -> plugin intake
        # route, then run the real host substage up to the waiting hook.
        self.platform._event_queue = asyncio.Queue()
        self.platform.client.username = "synthetic_bot"
        update = Update(number, message=Message(number, date=datetime.now(timezone.utc),
            chat=Chat(peer, "private"), from_user=User(peer, "Synthetic", False), text="你好 😀"))
        await self.platform.message_handler(update, NS(bot=self.platform.client))
        event = self.platform._event_queue.get_nowait()
        event.set_extra("activated_handlers", [])
        event.created_at = self.clock.now().timestamp()
        await self.started_plugin().receive_companion_message(event)
        waiting = AsyncMock(return_value=True)
        with patch.object(internal, "call_event_hook", waiting):
            stage = internal.InternalAgentSubStage.__new__(internal.InternalAgentSubStage)
            stage.streaming_response = stage.show_reasoning = False
            async with asyncio.timeout(3):
                async for _ in stage.process(event, ""):
                    pass
        waiting.assert_awaited_once_with(event, internal.EventType.OnWaitingLLMRequestEvent)
        await asyncio.sleep(0)
        return event

    async def test_host_indicator_respects_setting_and_leaves_unmanaged_peer_alone(self):
        await self.run_to_host_waiting_hook(1)
        self.action.assert_not_awaited()
        with patch.object(self.app.private_access_settings, "defaults_installation",
                          return_value={"stranger_notice": False}):
            await self.run_to_host_waiting_hook(2, peer=OWNER + 1)
        self.action.assert_awaited_once_with(chat_id=str(OWNER + 1), action="typing")

    async def test_host_indicator_with_disconnected_client(self):
        self.enable_typing()
        self.platform.application.updater.running = False
        await self.run_to_host_waiting_hook(1)
        self.action.assert_not_awaited()

    async def test_host_indicator_with_missing_grant(self):
        self.enable_typing()
        identities = self.controller.session.bridge.identities
        grant = next(g for g in identities.grants_for_subject(self.context.actor.subject)
                     if g.capability == "platform.typing")
        identities.revoke_grant(grant.grant_id, revoked_at=self.clock.now(), expected_revision=identities.revision)
        await self.run_to_host_waiting_hook(1)
        self.action.assert_not_awaited()

    async def test_host_waiting_hook_proceeds_while_authorized_indicator_is_pending(self):
        self.enable_typing()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(**kwargs):
            entered.set()
            await release.wait()
            return True

        self.action.side_effect = slow
        try:
            event = await self.run_to_host_waiting_hook(1)
            await asyncio.wait_for(entered.wait(), 3)
            self.action.assert_awaited_once_with(chat_id=OWNER, action="typing")
            self.assertTrue(event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"])
        finally:
            release.set()

    async def test_long_reply_refreshes_until_finished(self):
        self.enable_typing(interval=10)
        renewed = asyncio.Event()

        async def typing(**kwargs):
            if self.action.await_count >= 2:
                renewed.set()
            return True

        self.action.side_effect = typing
        self.presence.begin_native_reply(self.context, "long-reply")
        try:
            await asyncio.wait_for(renewed.wait(), 8)
            self.action.assert_awaited_with(chat_id=OWNER, action="typing")
            worker = self.presence.typing[self.presence.route(self.context)][2]
        finally:
            self.presence.finish_native_reply(self.context, "long-reply")
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
        self.assertFalse(self.presence.active_handles)

    async def test_setting_off_does_not_start_typing(self):
        self.presence.begin_native_reply(self.context, "disabled")
        self.assertIsNotNone(self.presence.active_turn(self.context))
        self.assertFalse(self.presence.active_handles)
        self.presence.finish_native_reply(self.context, "disabled")
        self.action.assert_not_awaited()

    async def test_disconnect_and_revoked_grant_prevent_dispatch(self):
        self.enable_typing()
        port = self.presence.port
        self.platform.application.updater.running = False
        self.assertEqual((await port.typing(self.context))["state"], "not_started")
        self.platform.application.updater.running = True
        identities = self.controller.session.bridge.identities
        grant = next(g for g in identities.grants_for_subject(self.context.actor.subject)
                     if g.capability == "platform.typing")
        identities.revoke_grant(grant.grant_id, revoked_at=self.clock.now(), expected_revision=identities.revision)
        self.assertEqual((await port.typing(self.context))["state"], "not_started")
        self.action.assert_not_awaited()

    async def test_account_status_is_unsupported(self):
        port = self.presence.port
        self.assertEqual((await port.online_status(self.context, "online"))["code"], "presence_unsupported")
        self.assertEqual((await port.custom_status(self.context, "休息"))["code"], "presence_unsupported")
        self.action.assert_not_awaited()

    async def test_slow_rate_limited_action_is_independent_of_reply(self):
        self.enable_typing()
        entered, release = asyncio.Event(), asyncio.Event()

        async def limited(**kwargs):
            entered.set()
            await release.wait()
            raise RetryAfter(42)

        self.action.side_effect = limited
        self.presence.begin_native_reply(self.context, "rate-limited")
        await asyncio.wait_for(entered.wait(), 3)
        release.set()
        route = self.presence.route(self.context)
        await asyncio.wait_for(self.presence.typing[route][2], 3)
        self.assertIsNotNone(self.presence.active_turn(self.context))
        self.assertEqual(self.presence.results_for(self.context)["typing:" + route]["state"], "unknown")
        self.presence.finish_native_reply(self.context, "rate-limited")
        self.action.assert_awaited_once()

    async def test_failed_and_cancelled_reply_tasks_release_typing(self):
        self.enable_typing()
        plugin = self.started_plugin()
        for number, cancel in enumerate((False, True), start=1):
            with self.subTest(cancel=cancel):
                prepared = asyncio.get_running_loop().create_future()
                release = asyncio.Event()

                async def reply():
                    event = telegram_event(self.platform, number)
                    event.created_at = self.clock.now().timestamp()
                    await plugin.receive_companion_message(event)
                    prepared.set_result(event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]["context"])
                    await release.wait()
                    raise RuntimeError("synthetic reply failure")

                task = asyncio.create_task(reply())
                self.addAsyncCleanup(self.cancel_reply, task)
                context = await asyncio.wait_for(prepared, 30)
                self.assertIsNotNone(self.presence.active_turn(context))
                if cancel:
                    task.cancel()
                else:
                    release.set()
                with self.assertRaises(asyncio.CancelledError if cancel else RuntimeError):
                    await task
                self.assertIsNone(self.presence.active_turn(context))

    @staticmethod
    async def cancel_reply(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TelegramDeliveryTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_host()
        self.context, _ = await self.controller.session.synchronize(self.controller.settings.target("tg-owner"))
        self.binding = ProviderBinding("message.delivery", "synthetic-sender", "1", digest("synthetic"))
        self.sender = AstrBotPlatformSender(self.controller.session.bridge, binding=self.binding,
            version="4.29.4", reverse_api_type=object, authorize=lambda: None)
        self.platform.client.send_chat_action = AsyncMock()
        self.platform.client.send_message = AsyncMock()

    async def send(self, text="你好"):
        envelope = DeliveryEnvelope("part-tg", "op-tg", self.context, 1, text, digest(text),
                                    self.binding, before_dispatch=lambda: True)
        return await self.sender.send(envelope)

    async def test_background_send_waits_for_polling(self):
        self.platform.application.updater.running = False
        receipt = await self.send()
        self.assertEqual((receipt.status, receipt.failure_code), ("not_sent", "host_account_offline"))
        self.platform.client.send_chat_action.assert_not_awaited()
        self.platform.client.send_message.assert_not_awaited()
        self.platform.application.updater.running = True
        self.assertEqual((await self.send()).status, "delivered")
        self.platform.client.send_message.assert_awaited_once()

    async def test_definite_telegram_refusals_have_safe_reasons(self):
        token = self.platform.client.token
        for error, reason in (
            (Forbidden("Bot was blocked by the user"), "telegram_bot_blocked"),
            (BadRequest("Chat not found"), "telegram_chat_not_found"),
            (InvalidToken(token), "telegram_invalid_token"),
            (RetryAfter(42), "telegram_rate_limited"),
        ):
            with self.subTest(reason=reason):
                self.platform.client.send_message.side_effect = error
                with self.assertLogs("companion_v2.adapters.astrbot.platform_sender", "WARNING") as logs:
                    receipt = await self.send()
                self.assertEqual((receipt.status, receipt.failure_code), ("not_sent", reason))
                self.assertNotIn(token, " ".join(logs.output))

    async def test_chat_action_warning_redacts_the_bot_token(self):
        token = self.platform.client.token
        self.platform.client.send_chat_action.side_effect = InvalidToken(token)
        self.platform.client.send_message.side_effect = InvalidToken(token)
        with self.assertLogs("astrbot", level="WARNING") as host_logs:
            receipt = await self.send()
        self.assertEqual((receipt.status, receipt.failure_code), ("not_sent", "telegram_invalid_token"))
        self.assertNotIn(token, " ".join(host_logs.output))
        self.assertIn("[redacted]", " ".join(host_logs.output))

    async def test_private_notice_redacts_host_chat_action_warning(self):
        from astrbot_host.private_notice import send_private_notice

        event = telegram_event(self.platform)
        route = authenticate_notice_route(self.controller, event, ScopeKind.PRIVATE, expected_post="message")
        token = self.platform.client.token
        self.platform.client.send_chat_action.side_effect = InvalidToken(token)
        with self.assertLogs("astrbot", level="WARNING") as logs:
            receipt = await send_private_notice(self.controller, event, text="验证", route=route)
        self.assertEqual(receipt["state"], "delivered")
        self.assertNotIn(token, " ".join(logs.output))
        self.assertIn("[redacted]", " ".join(logs.output))

    async def test_relay_receipt_uses_the_telegram_client_without_an_event_lease(self):
        from astrbot_host.relay_pending_events import _send_private

        event = telegram_event(self.platform)
        token = self.platform.client.token
        self.platform.client.send_chat_action.side_effect = InvalidToken(token)
        with self.assertLogs("astrbot", level="WARNING") as logs:
            receipt = await _send_private(self.controller, event, platform_id="tg-main",
                                          login="tg-main", peer=str(OWNER), text="送达回执")
        self.assertEqual(receipt["state"], "delivered")
        self.platform.client.send_message.assert_awaited_once()
        self.assertNotIn(token, " ".join(logs.output))

    async def test_httpx_logs_redact_delivery_notice_and_relay_requests(self):
        from astrbot_host.private_notice import send_private_notice
        from astrbot_host.relay_pending_events import _send_private

        token = self.platform.client.token
        event = telegram_event(self.platform)
        route = authenticate_notice_route(self.controller, event, ScopeKind.PRIVATE, expected_post="message")
        paths = []

        def respond(request):
            paths.append(request.url.path)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async def chat_action(*args, **kwargs):
                await http.post(f"https://telegram.invalid/bot{token}/sendChatAction")

            async def message(*args, **kwargs):
                await http.post(f"https://telegram.invalid/bot{token}/sendMessage")

            self.platform.client.send_chat_action.side_effect = chat_action
            self.platform.client.send_message.side_effect = message
            with self.assertLogs("httpx", "INFO") as logs:
                self.assertEqual((await self.send()).status, "delivered")
                notice = await send_private_notice(self.controller, event, text="验证", route=route)
                relay = await _send_private(self.controller, event, platform_id="tg-main",
                                            login="tg-main", peer=str(OWNER), text="回执")
                self.assertEqual((notice["state"], relay["state"]), ("delivered", "delivered"))

        methods = ["sendChatAction", "sendMessage"] * 3
        self.assertEqual(paths, [f"/bot{token}/{method}" for method in methods])
        self.assertEqual(len(logs.records), len(methods))
        for record, method in zip(logs.records, methods):
            self.assertIn(f"/bot[redacted]/{method}", record.getMessage())
            self.assertNotIn(token, repr(record.args))

    async def test_unavailable_refusal_classifier_keeps_dispatch_unknown(self):
        self.platform.client.send_message.side_effect = NetworkError("connection lost")
        with patch("companion_v2.adapters.astrbot.platform_sender._telegram_refusal", side_effect=ImportError):
            receipt = await self.send()
        self.assertEqual((receipt.status, receipt.failure_code), ("unknown", None))
        self.platform.client.send_message.assert_awaited_once()

    async def test_transport_errors_are_unknown(self):
        for error in (TimedOut(), NetworkError("connection lost")):
            with self.subTest(error=type(error).__name__):
                self.platform.client.send_message.reset_mock()
                self.platform.client.send_message.side_effect = error
                receipt = await self.send()
                self.assertEqual((receipt.status, receipt.failure_code), ("unknown", None))
                self.platform.client.send_message.assert_awaited_once()

    async def test_refusal_after_first_text_chunk_is_unknown(self):
        self.platform.client.send_message.side_effect = [None, Forbidden("bot was blocked by the user")]
        receipt = await self.send("x" * 4097)
        self.assertEqual((receipt.status, receipt.failure_code), ("unknown", None))
        self.assertEqual(self.platform.client.send_message.await_count, 2)

    async def test_proactive_face_only_sends_visible_text(self):
        from companion_v2.delivery.plan import OutboundPlan
        envelope = DeliveryEnvelope("face-tg", "op-tg", self.context, 1, "", digest(""), self.binding,
                                    before_dispatch=lambda: True, plan=OutboundPlan(faces=(14,)))
        receipt = await self.sender.send(envelope)
        self.assertEqual(receipt.status, "delivered")
        self.assertIn("14", self.platform.client.send_message.await_args.kwargs["text"])

    async def test_negative_session_ack_never_records_delivery(self):
        self.platform.send_by_session = AsyncMock(return_value=False)
        receipt = await self.send()
        self.assertEqual(receipt.status, "not_sent")
        self.assertIsNone(receipt.delivered_at)

    async def test_refusal_after_text_before_voice_is_unknown(self):
        audio = self.root / "synthetic.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", ""))
            stream.writeframes(bytes(160))
        self.sender.voice = AsyncMock(return_value={"type": "record", "data": {"file": str(audio)}})
        self.platform.client.send_voice = AsyncMock(side_effect=RetryAfter(42))
        receipt = await self.send()
        self.assertEqual((receipt.status, receipt.failure_code), ("unknown", None))
        self.platform.client.send_voice.assert_awaited_once()
        from pathlib import Path
        sent = Path(self.platform.client.send_voice.await_args.kwargs["voice"]).read_bytes()
        self.assertEqual(sent[:4], b"OggS")
        self.assertIn(b"OpusHead", sent[:100])
        self.platform.client.send_message.assert_awaited_once()


class TelegramPrivateCommandTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_host()
        self.plugin = self.started_plugin()
        self.platform.send_by_session = AsyncMock()
        self.number = 0

    async def dispatch(self, text, peer=OWNER):
        self.number += 1
        event = telegram_event(self.platform, self.number, text, peer)
        event.created_at = self.clock.now().timestamp()
        await self.plugin.receive_companion_message(event)
        return event

    async def wait_for_contention(self, lock):
        async def waiting():
            while not lock._waiters:
                await asyncio.sleep(0)

        await asyncio.wait_for(waiting(), timeout=30)

    async def dispatch_overlapping_notices(self, peer, owner=None):
        from astrbot_host.private_notice import notice_cooldown_lock

        lock = notice_cooldown_lock(self.controller, owner)

        sending, release = asyncio.Event(), asyncio.Event()

        async def send(*args):
            sending.set()
            await release.wait()

        self.platform.send_by_session.side_effect = send
        first = asyncio.create_task(self.dispatch("你好", peer))
        try:
            await asyncio.wait_for(sending.wait(), timeout=10)
            second = asyncio.create_task(self.dispatch("你好", peer))
            await self.wait_for_contention(lock)
            self.clock.advance(timedelta(seconds=61))
        finally:
            release.set()
        await asyncio.gather(first, second)

    def sent_text(self, peer=OWNER):
        session, message = self.platform.send_by_session.await_args.args
        self.assertEqual(str(session), f"tg-main:FriendMessage:{peer}")
        return message.chain[0].text

    async def test_calendar_commands_run_once_and_suppress_the_model(self):
        planner = AsyncMock()
        self.controller.session.plan_calendar = planner
        for prefix in ("", "陪伴 "):
            for action in ("生成", "刷新", "重新生成"):
                for period, mode, label in (("", "daily", "今天的日程"), ("周", "horizon", "未来日程")):
                    with self.subTest(prefix=prefix, action=action, period=period):
                        planner.return_value = {"state": "succeeded", "saved": 2, "mode": mode}
                        event = await self.dispatch(prefix + action + period + "日程")
                        self.assertTrue(event.call_llm)
                        planner.assert_awaited_once_with(event, self.controller.settings.target("tg-owner"),
                                                         mode=mode, refresh=action != "生成")
                        self.platform.send_by_session.assert_awaited_once()
                        self.assertEqual(self.sent_text(), label + "已生成，共写入 2 项。")
                        planner.reset_mock()
                        self.platform.send_by_session.reset_mock()

    async def test_stranger_notice_honors_switch_and_cooldown(self):
        peer = OWNER + 1
        defaults = {"stranger_notice": False, "denied_reply": "陌生人提示",
                    "stranger_daily_cap": 3, "notice_cooldown_seconds": 60}
        self.controller.session.application.private_access_settings.defaults_installation = Mock(return_value=defaults)
        await self.dispatch("你好", peer)
        self.platform.send_by_session.assert_not_awaited()
        defaults["stranger_notice"] = True
        await self.dispatch_overlapping_notices(peer)
        self.platform.send_by_session.assert_awaited_once()
        self.assertEqual(self.sent_text(peer), "陌生人提示")
        self.clock.advance(timedelta(seconds=60))
        await self.dispatch("你好", peer)
        self.assertEqual(self.platform.send_by_session.await_count, 2)

    async def test_refused_stranger_notice_remains_due(self):
        peer = OWNER + 1
        self.controller.session.application.private_access_settings.defaults_installation = Mock(return_value={
            "stranger_notice": True, "denied_reply": "陌生人提示",
            "stranger_daily_cap": 1, "notice_cooldown_seconds": 60,
        })
        self.platform.send_by_session.side_effect = [False, None]
        for _ in range(3):
            await self.dispatch("你好", peer)
        self.assertEqual(self.platform.send_by_session.await_count, 2)

    async def test_stranger_cooldown_keeps_legacy_notice_on_its_own_platform(self):
        from astrbot_host.stranger_notice import COOLDOWN_KIND, StrangerNotice
        from companion_v2.execution.models import stamp

        notices = StrangerNotice(self.controller)
        now = self.clock.now()
        qq = NS(platform_id="qq-main", login="12345", peer="67890")
        telegram = NS(platform_id="12345", login=qq.login, peer=qq.peer)
        notices._write("stranger-notice:12345:67890", COOLDOWN_KIND,
                       {"platform_id": qq.platform_id, "last_notice_at": stamp(now)}, now)
        self.assertFalse(notices._cooldown_ok(qq, now, 60))
        self.assertTrue(notices._cooldown_ok(telegram, now, 60))

    async def test_stranger_notice_uses_delivery_day_for_daily_cap(self):
        defaults = {"stranger_notice": True, "denied_reply": "陌生人提示",
                    "stranger_daily_cap": 1, "notice_cooldown_seconds": 60}
        self.controller.session.application.private_access_settings.defaults_installation = Mock(return_value=defaults)
        self.platform.send_by_session.side_effect = lambda *_: self.clock.advance(timedelta(days=1))
        await self.dispatch("你好", OWNER + 1)
        await self.dispatch("你好", OWNER + 2)
        self.platform.send_by_session.assert_awaited_once()

    async def test_queued_stranger_notice_observes_new_mute(self):
        from astrbot_host.private_notice import notice_cooldown_lock
        from astrbot_host.stranger_notice import BLOCK_KIND, stranger_block_view_id
        from companion_v2.execution.models import encode, stamp

        peer = OWNER + 1
        defaults = {"stranger_notice": True, "denied_reply": "陌生人提示",
                    "stranger_daily_cap": 3, "notice_cooldown_seconds": 60}
        self.controller.session.application.private_access_settings.defaults_installation = Mock(return_value=defaults)
        lock = notice_cooldown_lock(self.controller)
        async with lock:
            queued = asyncio.create_task(self.dispatch("你好", peer))
            await self.wait_for_contention(lock)
            key = stranger_block_view_id("tg-main", "tg-main", str(peer))
            with self.controller.runtime.transaction() as connection:
                connection.execute("INSERT INTO memory_views VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                                   (key, key, BLOCK_KIND, 1, encode({}), encode({}), stamp(self.clock.now())))
        await queued
        self.platform.send_by_session.assert_not_awaited()

    async def test_queued_notices_recheck_binding_and_dialogue_access(self):
        from astrbot_host.private_notice import notice_cooldown_lock
        from companion_v2.identity_policy.models import AuthorizationTier

        session = self.controller.session
        app = session.application
        target = self.controller.settings.target("tg-owner")
        context = self.controller.authority.context("tg-owner", "role-tg")
        grants = self.controller.authority.identities
        grants.revoke_grant(self.controller.authority.grant_id(context.scope, "conversation.reply"),
                            revoked_at=self.clock.now(), expected_revision=grants.revision)
        defaults = {"stranger_notice": True, "denied_reply": "权限提示",
                    "stranger_daily_cap": 3, "notice_cooldown_seconds": 60}
        app.private_access_settings.defaults_installation = Mock(return_value=defaults)
        app.private_notice.settings = NS(defaults=lambda context: defaults)
        for peer, owner in ((OWNER, app.private_notice.owner_key(context)), (OWNER + 1, None)):
            lock = notice_cooldown_lock(self.controller, owner)
            async with lock:
                queued = asyncio.create_task(self.dispatch("你好", peer))
                await self.wait_for_contention(lock)
                if peer == OWNER:
                    grants.issue_grant(grant_id="restored-dialogue", subject=context.actor.subject,
                                       scope=context.scope, capability="conversation.reply",
                                       tier=AuthorizationTier.STANDARD, valid_from=self.clock.now(),
                                       expected_revision=grants.revision)
                else:
                    new_target = replace(target, target_id="new-peer", participant_id=str(peer))
                    self.controller.settings = replace(self.controller.settings, targets=(target, new_target))
            await queued
            self.platform.send_by_session.assert_not_awaited()

    async def test_revoked_dialogue_honors_blank_copy_and_cooldown(self):
        authority = self.controller.authority
        context = authority.context("tg-owner", "role-tg")
        authority.identities.revoke_grant(authority.grant_id(context.scope, "conversation.reply"),
            revoked_at=self.clock.now(), expected_revision=authority.identities.revision)
        defaults = {"denied_reply": "", "notice_cooldown_seconds": 60}
        self.controller.session.application.private_notice.settings = NS(defaults=lambda context: defaults)
        await self.dispatch("你好")
        self.platform.send_by_session.assert_not_awaited()
        defaults["denied_reply"] = "权限提示"
        await self.dispatch_overlapping_notices(OWNER, self.controller.session.application.private_notice.owner_key(context))
        self.platform.send_by_session.assert_awaited_once()
        self.assertEqual(self.sent_text(), "权限提示")
        self.clock.advance(timedelta(seconds=60))
        await self.dispatch("你好")
        self.assertEqual(self.platform.send_by_session.await_count, 2)

    async def test_reset_requires_admin_and_confirmation(self):
        authority = self.controller.authority
        context = authority.context("tg-owner", "role-tg")
        from astrbot_host.reset_command import receive_reset_command
        plain = telegram_event(self.platform, text="全部重置")
        self.assertIsNone(await receive_reset_command(self.controller, plain))
        self.platform.send_by_session.assert_not_awaited()
        event = await self.dispatch("陪伴 全部重置")
        self.assertTrue(event.call_llm)
        self.assertIn("只允许安装核心管理员", self.sent_text())
        authority.provision_scope(authority.settings.target("tg-owner"), context.scope,
                                  context.role_binding.role, core_admin=True)
        application = self.controller.session.application
        reset = application.reset_conversation_window = Mock(wraps=application.reset_conversation_window)
        await self.dispatch("陪伴 全部重置")
        preview = self.sent_text()
        self.assertIn("尚未执行", preview)
        reset.assert_not_called()
        confirmation = preview.split("确认请回复「")[1].split("」")[0]
        await self.dispatch(confirmation)
        reset.assert_called_once()
        self.assertIn("已执行", self.sent_text())

    async def test_route_requires_matching_client_connection_and_session(self):
        def route(event):
            return authenticate_notice_route(self.controller, event, ScopeKind.PRIVATE, expected_post="message")

        event = telegram_event(self.platform)
        self.assertIs(route(event).platform, self.platform)
        event.client = telegram_bot()
        self.assertIsNone(route(event))
        event = telegram_event(self.platform)
        event.session.session_id = str(OWNER + 1)
        self.assertIsNone(route(event))
        event = telegram_event(self.platform)
        event.message_obj.raw_message = telegram_event(self.platform, peer=OWNER + 1).message_obj.raw_message
        self.assertIsNone(route(event))
        event = telegram_event(self.platform)
        self.platform.application.updater.running = False
        with self.assertRaises(TaskError) as refused:
            route(event)
        self.assertEqual(refused.exception.code, "host_account_offline")

    async def test_calendar_reply_survives_rebuild_of_the_same_bot(self):
        async def plan(event, target, *, mode, refresh):
            self.platform.client = telegram_bot()
            return {"state": "succeeded", "saved": 1}

        self.controller.session.plan_calendar = plan
        await self.dispatch("生成日程")
        self.platform.send_by_session.assert_awaited_once()
        self.assertIn("1", self.sent_text())

    async def test_calendar_reply_requires_the_same_bot_after_planning(self):
        async def plan(event, target, *, mode, refresh):
            self.platform.client = telegram_bot("535353:synthetic")
            return {"state": "succeeded", "saved": 1}

        self.controller.session.plan_calendar = plan
        event = await self.dispatch("生成日程")
        self.assertTrue(event.call_llm)
        self.platform.send_by_session.assert_not_awaited()

    async def test_phone_confirmation_reaches_the_configured_checkout(self):
        actions = self.controller.session.application.phone_actions
        actions.answer_checkout = Mock()
        event = await self.dispatch("确认支付")
        self.assertTrue(event.call_llm)
        actions.answer_checkout.assert_called_once()
        context, token = actions.answer_checkout.call_args.args
        self.assertEqual(context.scope.native_scope_id, str(OWNER))
        self.assertIsNone(token)
        self.assertTrue(actions.answer_checkout.call_args.kwargs["approve"])
        self.assertEqual(self.sent_text(), "收到确认，我会先核对手机上的订单。")


class TelegramPrivateAdmissionTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_session_directory_ignores_unknown_and_keeps_the_observed_name(self):
        event = telegram_event(self.platform)
        original = event.message_obj.raw_message
        event.message_obj.sender.nickname = "Unknown"
        message = original.message
        unnamed = Update(1, message=Message(1, date=message.date, chat=message.chat,
                                            from_user=User(OWNER, "", False)))
        directory = self.controller.session_topology
        for update, expected in ((unnamed, ""), (original, "Synthetic"), (unnamed, "Synthetic")):
            event.message_obj.raw_message = update
            await directory.observe(event)
            rows = json.loads(directory.path.read_text(encoding="utf-8"))
            self.assertEqual([row.get("display_name", "") for row in rows.values()], [expected])

    async def test_directory_reads_old_telegram_observations_without_the_placeholder(self):
        event = telegram_event(self.platform)
        directory = self.controller.session_topology
        await directory.observe(event)
        rows = json.loads(directory.path.read_text(encoding="utf-8"))
        stored = next(iter(rows.values()))
        for name, expected in (("Unknown", ""), ("Unknown Artist", "Unknown Artist")):
            stored["display_name"] = name
            directory.path.write_text(json.dumps(rows), encoding="utf-8")
            windows = await directory.inventory()
            self.assertEqual([row.get("display_name", "") for row in windows
                              if row.get("unified_msg_origin") == event.unified_msg_origin], [expected])

    async def test_private_text_is_admitted_with_the_bot_astrbot_rebuilt(self):
        await self.start_host()
        # AstrBot replaces the bot object after repeated polling errors (same token).
        self.platform.client = telegram_bot()
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        plugin.staging = self.controller
        self.assertEqual(await self.admitted(plugin, 1), f"telegram-user:{OWNER}")

    async def test_private_text_is_refused_once_another_bot_answers(self):
        await self.start_host()
        self.platform.client = telegram_bot("535353:synthetic")
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        plugin.staging = self.controller
        self.assertIsNone(await self.admitted(plugin, 1))

    async def test_a_present_channel_without_a_bot_joins_once_it_has_one(self):
        bot, self.platform.client = self.platform.client, None
        await self.start_host()
        self.platform.client, plugin = bot, self.started_plugin()
        await self.connect_late(plugin)
        self.assertEqual(await self.admitted(plugin, 1), f"telegram-user:{OWNER}")

    async def test_first_start_before_the_channel_loads_keeps_the_telegram_person(self):
        del self.native.platforms["tg-main"]
        plugin = self.started_plugin()
        self.assertTrue(await plugin._try_start_host())
        self.assertEqual(self.bound_person(), f"telegram-user:{OWNER}")
        await self.connect_late(plugin)
        self.assertEqual(await self.admitted(plugin, 1), f"telegram-user:{OWNER}")

    async def test_first_start_with_disabled_channel_keeps_the_telegram_person(self):
        del self.native.platforms["tg-main"]
        self.native.get_config = lambda umo=None: {"platform": [dict(id="tg-main", type="telegram", enable=False)]}
        self.assertTrue(await self.started_plugin()._try_start_host())
        self.assertEqual(self.bound_person(), f"telegram-user:{OWNER}")

    def test_disabled_telegram_row_does_not_claim_another_platform(self):
        del self.native.platforms["tg-main"]
        rows = [dict(id="tg-main", type="telegram", enable=False),
                dict(id="tg-main", type="aiocqhttp", enable=True)]
        self.native.get_config = lambda umo=None: {"platform": rows}
        self.assertEqual(self.controller._configured_platform_name("tg-main"), "")
        self.assertEqual(self.controller._configured_platform_name("other"), "")

    async def test_restart_before_the_channel_loads_keeps_the_telegram_person(self):
        await self.start_host()
        await self.controller.terminate()
        del self.native.platforms["tg-main"]
        self.controller = self.make_controller(self.controller.config)
        plugin = self.started_plugin()
        self.assertTrue(await plugin._try_start_host())
        self.assertEqual(self.bound_person(), f"telegram-user:{OWNER}")
        await self.connect_late(plugin)
        self.assertEqual(await self.admitted(plugin, 1), f"telegram-user:{OWNER}")


if __name__ == "__main__":
    unittest.main()
