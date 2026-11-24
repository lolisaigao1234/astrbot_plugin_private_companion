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

from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram import Chat, ForumTopicCreated, Message, MessageEntity, Update, User
from astrbot.core.message.components import Plain, Reply
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
from astrbot_host.display_names import event_person_names
from astrbot_host.group_admission import group_trigger_facts
from companion_v2.adapters.astrbot.group_mentions import group_message_text
from companion_v2.conversation.group_timing import SELF_MENTION
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
    return NS(token=token, username="synthetic_bot", id=424242)


class TelegramPlatform(TelegramPlatformAdapter):
    def __init__(self):
        self.config = {"id": "tg-main"}
        self._forum_topic_names = {}
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

    def test_private_update_cannot_be_relabelled_as_group(self):
        event = telegram_event(TelegramPlatform())
        event.message_obj.type = MessageType.GROUP_MESSAGE
        event.session.message_type = MessageType.GROUP_MESSAGE
        with self.assertRaises(TaskError) as refused:
            group_event_route(event)
        self.assertEqual(refused.exception.code, "host_route_mismatch")


async def group_event(platform, chat_id=-400, kind="group", topic=None, text="你好", **fields):
    chat = Chat(chat_id, kind, is_forum=True if topic else None)
    message = Message(22, date=datetime.now(timezone.utc), chat=chat, text=text,
                      from_user=fields.pop("from_user", User(OWNER, "Synthetic", False)),
                      message_thread_id=topic, is_topic_message=bool(topic), **fields)
    native = await platform.convert_message(Update(12, message=message), NS(bot=platform.client))
    return platform.create_event(native)


async def forum_reply_event(platform, *, topic_root):
    original = Message(10 if topic_root else 11, date=datetime.now(timezone.utc),
                       chat=Chat(-400, "supergroup", is_forum=True),
                       from_user=User(platform.client.id, "Bot", True),
                       is_topic_message=True, message_thread_id=10,
                       forum_topic_created=ForumTopicCreated("Topic", 0) if topic_root else None,
                       text=None if topic_root else "之前")
    return await group_event(platform, kind="supergroup", topic=10, text="/ 原文", reply_to_message=original)


class TelegramGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_group_and_topic_routes(self):
        platform = TelegramPlatform()
        for kind, topic, window in (("group", None, "-400"), ("supergroup", None, "-400"),
                                    ("supergroup", 1, "-400"), ("supergroup", 23, "-400#23")):
            with self.subTest(kind=kind, topic=topic):
                event = await group_event(platform, kind=kind, topic=topic)
                route = group_event_route(event)
                self.assertEqual((route.group_id, route.session_id, route.sender_id),
                                 (window, window, str(OWNER)))


    async def test_sdk_addressing_uses_bot_username_and_original_reply_author(self):
        platform = TelegramPlatform()
        quote = Message(10, date=datetime.now(timezone.utc), chat=Chat(-400, "group"),
                        from_user=User(platform.client.id, "Bot", True), text="之前")
        for text, fields, mentioned, replied, expected in (
            ("你好", {}, False, False, "你好"),
            ("😀 @Synthetic_bot 好", {"entities": [MessageEntity("mention", 3, 14)]},
             True, False, "😀 " + SELF_MENTION + " 好"),
            ("@tg_main", {"entities": [MessageEntity("mention", 0, 8)]}, False, False, "@tg_main"),
            ("@SYNTHETIC_BOT", {"entities": [MessageEntity("mention", 0, 14)]}, True, False, SELF_MENTION),
            ("@synthetic_bot 你好", {"entities": [MessageEntity("mention", 0, 14)]},
             True, False, SELF_MENTION + " 你好"),
            ("/ 原文", {"reply_to_message": quote}, False, True, "/ 原文"),
        ):
            with self.subTest(text=text):
                event = await group_event(platform, text=text, **fields)
                facts = group_trigger_facts(event, "tg-main")
                self.assertEqual((facts["mentioned"], facts["quoted_bot"]), (mentioned, replied))
                self.assertEqual(group_message_text(event, "tg-main"), expected)

    async def test_bot_created_topic_only_addresses_bot_for_genuine_replies(self):
        platform = TelegramPlatform()
        for topic_root in (True, False):
            with self.subTest(topic_root=topic_root):
                event = await forum_reply_event(platform, topic_root=topic_root)
                self.assertEqual(any(isinstance(part, Reply) for part in event.message_obj.message), not topic_root)
                self.assertEqual(group_trigger_facts(event, "tg-main")["quoted_bot"], not topic_root)
                self.assertEqual(group_message_text(event, "tg-main"), "/ 原文")

    async def test_reply_to_member_whose_id_matches_the_instance_is_unaddressed(self):
        platform = TelegramPlatform()
        platform.config["id"] = str(OWNER)
        quoted = Message(9, date=datetime.now(timezone.utc), chat=Chat(-400, "group"),
                         from_user=User(OWNER, "Member", False), text="上文")
        event = await group_event(platform, reply_to_message=quoted)
        facts = group_trigger_facts(event, str(OWNER))
        self.assertFalse(facts["quoted_bot"])

    async def test_rejects_non_human_messages_and_mismatched_topic(self):
        platform = TelegramPlatform()
        for fields in ({"from_user": User(91, "Other", True)},
                       {"sender_chat": Chat(-400, "supergroup")},
                       {"kind": "channel"}):
            with self.subTest(fields=fields), self.assertRaises(TaskError):
                group_event_route(await group_event(platform, **fields))
        event = await group_event(platform, kind="supergroup", topic=23)
        event.message_obj.raw_message = (await group_event(platform, kind="supergroup", topic=24)).message_obj.raw_message
        with self.assertRaises(TaskError):
            group_event_route(event)
        original = event.message_obj.raw_message.message
        event.message_obj.raw_message = Update(33, channel_post=original)
        with self.assertRaises(TaskError):
            group_event_route(event)


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

    def enable_typing(self, interval=1):
        from astrbot_host.management_identity import dashboard_operator
        from companion_v2.role_work.state.management import StateManagement
        manager = StateManagement(self.app, self.context)
        operator = dashboard_operator(self.app.runtime, "synthetic", at=self.clock.now())
        status = manager.status(operator).unwrap()
        manager.command(operator, dict(action="configure", operation_key="typing-on",
            edit_token=status["edit_token"], role_edit_token=status["role_edit_token"],
            changes={"typing_enabled": True, "typing_refresh_seconds": interval})).unwrap()

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


class TelegramGroupHostTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        windows = ("-400", "-100400", "-100400#23")
        config = self.controller.config
        config["multi_host"]["group_targets"] = [
            dict(target_id=f"group-{i}", platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id=window) for i, window in enumerate(windows)]
        self.window_personas.update({f"tg-main:GroupMessage:{window}": "original" for window in windows})
        self.controller = self.make_controller(config)
        await self.start_host()
        self.platform.client.send_chat_action = AsyncMock()
        self.platform.client.send_message = AsyncMock()

    async def test_group_delivery_and_collective_audience(self):
        from astrbot.core.message.message_event_result import MessageChain

        binding = ProviderBinding("message.delivery", "synthetic-sender", "1", digest("synthetic"))
        sender = AstrBotPlatformSender(self.controller.session.bridge, binding=binding,
            version="4.29.4", reverse_api_type=object, authorize=lambda: None)
        for chat, kind, topic in ((-400, "group", None), (-100400, "supergroup", None),
                                  (-100400, "supergroup", 23)):
            quote = Message(17, date=datetime.now(timezone.utc), chat=Chat(chat, kind),
                            from_user=User(424242, "Bot", True), text="上文")
            text = "继续" if topic else "@synthetic_bot" if kind == "group" else "@synthetic_bot 你好"
            fields = {"reply_to_message": quote} if topic else {"entities": [MessageEntity("mention", 0, 14)]}
            event = await group_event(self.platform, chat, kind, topic, text=text, **fields)
            event.created_at = self.clock.now().timestamp()
            admission = require(await self.controller.group_reply_candidate(event))
            self.assertEqual(admission["reason"], "native_quote" if topic else "mention")
            prepared = require(await self.controller.prepare_core_reply(event, group_admission=admission))
            context = prepared["context"]
            self.assertEqual(prepared["group_trigger"]["bare_mention"], kind == "group")
            if topic:
                from astrbot_host.core_reply_mentions import restore_group_mentions
                native = prepared["native_input"]
                self.assertEqual((native.text, native.group_message_text), ("继续", "继续"))
                request = NS(prompt=event.message_str)
                restore_group_mentions(event, request, native, self.native)
                self.assertEqual(request.prompt, "继续")
            self.assertEqual(context.actor.subject.subject_id, self.bound_person())
            self.assertEqual(context.scope.kind, ScopeKind.GROUP)
            self.assertEqual({r.kind.value for r in context.audience.recipients}, {"human", "group"})
            await event.send(MessageChain(chain=[Plain("前台")]))
            receipt = await sender.send(DeliveryEnvelope("part", "op", context, 1, "后台", digest("后台"),
                                        binding, before_dispatch=lambda: True))
            self.assertEqual(receipt.status, "delivered", receipt)
            calls = self.platform.client.send_message.await_args_list
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual(call.kwargs["chat_id"], str(chat))
                self.assertEqual(call.kwargs.get("message_thread_id"), str(topic) if topic else None)
            self.platform.client.send_message.reset_mock()

    async def test_unconfigured_and_migrated_windows_stay_silent(self):
        plugin = self.started_plugin()
        for chat, topic in ((-100401, None), (-100400, 24)):
            event = await group_event(self.platform, chat, "supergroup", topic)
            with self.assertLogs("astrbot_host.multi_controller", level="INFO") as logs:
                await plugin.receive_companion_message(event)
            self.assertIn("未配置", " ".join(logs.output))
            self.assertTrue(event.call_llm)
            self.assertIsNone(event.get_extra(CORE_REPLY_CONTEXT_EXTRA))
        self.platform.client.send_message.assert_not_awaited()


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

    async def dispatch_overlapping_notices(self, peer, owner=None):
        from astrbot_host.private_notice import notice_cooldown_lock

        lock = notice_cooldown_lock(self.controller, owner)

        async def wait_for_contention():
            while not lock._waiters:
                await asyncio.sleep(0)
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(*args):
            sending.set()
            await release.wait()

        self.platform.send_by_session.side_effect = send
        first = asyncio.create_task(self.dispatch("你好", peer))
        try:
            await asyncio.wait_for(sending.wait(), timeout=10)
            second = asyncio.create_task(self.dispatch("你好", peer))
            await asyncio.wait_for(wait_for_contention(), timeout=30)
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
            async with asyncio.timeout(30):
                while not lock._waiters:
                    await asyncio.sleep(0)
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
                async with asyncio.timeout(30):
                    while not lock._waiters:
                        await asyncio.sleep(0)
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
        event = await self.dispatch("全部重置")
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
