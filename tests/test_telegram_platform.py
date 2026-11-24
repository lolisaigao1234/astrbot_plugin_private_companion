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
from telegram import Chat, ForumTopicCreated, ForumTopicEdited, Message, MessageEntity, TextQuote, Update, User
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


async def group_event(platform, chat_id=-400, kind="group", topic=None, text="你好", message_id=22, **fields):
    chat = Chat(chat_id, kind, title=fields.pop("title", None), is_forum=True if topic else None)
    message = Message(message_id, date=datetime.now(timezone.utc), chat=chat, text=text,
                      from_user=fields.pop("from_user", User(OWNER, "Synthetic", False)),
                      message_thread_id=topic, is_topic_message=bool(topic), **fields)
    native = await platform.convert_message(Update(12, message=message), NS(bot=platform.client))
    return platform.create_event(native)


async def forum_reply_event(platform, *, topic_root, text="/ 原文"):
    original = Message(10 if topic_root else 11, date=datetime.now(timezone.utc),
                       chat=Chat(-400, "supergroup", is_forum=True),
                       from_user=User(platform.client.id, "Bot", True),
                       is_topic_message=True, message_thread_id=10,
                       forum_topic_created=ForumTopicCreated("Topic", 0) if topic_root else None,
                       text=None if topic_root else "之前")
    return await group_event(platform, kind="supergroup", topic=10, text=text, reply_to_message=original)


class TelegramGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_quoted_group_text_preserves_delivery_certainty(self):
        from astrbot.core.message.components import Reply
        from astrbot.core.message.message_event_result import MessageChain
        from companion_v2.adapters.astrbot.platform_sender import _telegram_refusal
        from companion_v2.adapters.astrbot.telegram_wire import send_optional_reply

        for text, responses, expected in (
            ("短消息", [Forbidden("forbidden")], "telegram_forbidden"),
            ("a" * 4100, [None, Forbidden("forbidden")], None),
        ):
            with self.subTest(length=len(text)):
                client = NS(send_message=AsyncMock(side_effect=responses), send_chat_action=AsyncMock())
                chain = MessageChain(chain=[Reply(id="22"), Plain(text)])
                with self.assertRaises(Forbidden) as refused:
                    await send_optional_reply(client, chain, "-400#23")
                self.assertEqual(_telegram_refusal(refused.exception, chain), expected)
                self.assertEqual(client.send_message.await_count, len(responses))

    async def test_delayed_mention_keeps_entity_on_plain_retry(self):
        from astrbot.core.message.components import At, Reply
        from astrbot.core.message.message_event_result import MessageChain
        from companion_v2.adapters.astrbot.telegram_wire import send_optional_reply, telegram_mentions

        client = NS(send_message=AsyncMock(side_effect=[BadRequest("synthetic Markdown refusal"), None]),
                    send_chat_action=AsyncMock())
        parts, mentions = telegram_mentions([Reply(id="22"), At(qq=str(OWNER)), Plain("稍后回复")], {})
        await send_optional_reply(client, MessageChain(chain=parts), "-400#23", mentions=mentions)
        self.assertEqual(client.send_message.await_count, 2)
        payload = client.send_message.await_args.kwargs
        self.assertEqual((payload["chat_id"], payload["message_thread_id"], str(payload["reply_to_message_id"])),
                         ("-400", "23", "22"))
        entity, = payload["entities"]
        self.assertEqual((entity.offset, entity.length, entity.url), (0, 2, f"tg://user?id={OWNER}"))
        self.assertTrue(payload["allow_sending_without_reply"])
        self.assertIn("成员 稍后回复", payload["text"])

    async def test_mention_preserves_leading_fenced_code_in_markdown_and_plain_retry(self):
        from astrbot.core.message.components import At
        from astrbot.core.message.message_event_result import MessageChain
        from companion_v2.adapters.astrbot.telegram_wire import send_optional_reply, telegram_mentions

        body = "```python\nprint(42)\n```"
        parts, mentions = telegram_mentions([At(qq=str(OWNER)), Plain(body)], {})
        client = NS(send_message=AsyncMock(side_effect=[BadRequest("synthetic Markdown refusal"), None]),
                    send_chat_action=AsyncMock())
        await send_optional_reply(client, MessageChain(chain=parts), "-400#23", mentions=mentions)
        markdown, plain = [call.kwargs for call in client.send_message.await_args_list]
        self.assertIn("\n```python\nprint(42)", markdown["text"])
        self.assertEqual(plain["text"].strip(), "成员\n" + body)
        entity, = plain["entities"]
        self.assertEqual((entity.offset, entity.length, entity.url), (0, 2, f"tg://user?id={OWNER}"))

    async def test_delayed_mention_redacts_httpx_membership_request(self):
        from companion_v2.adapters.astrbot.telegram_wire import member_usernames

        bot = telegram_bot()
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as http:
            async def lookup(*, chat_id, user_id):
                await http.post(f"https://telegram.invalid/bot{bot.token}/getChatMember")
                return NS(user=User(user_id, "Synthetic", False, username="speaker"))

            bot.get_chat_member = lookup
            with self.assertLogs("httpx", "INFO") as logs:
                names = await member_usernames(bot, "-100400#23", [str(OWNER)])

        self.assertEqual(names, {str(OWNER): "speaker"})
        record, = logs.records
        self.assertIn("/bot[redacted]/getChatMember", record.getMessage())
        self.assertNotIn(bot.token, repr(vars(record)))

    async def test_sdk_group_and_topic_routes(self):
        platform = TelegramPlatform()
        for kind, topic, window in (("group", None, "-400"), ("supergroup", None, "-400"),
                                    ("supergroup", 1, "-400"), ("supergroup", 23, "-400#23")):
            with self.subTest(kind=kind, topic=topic):
                event = await group_event(platform, kind=kind, topic=topic)
                route = group_event_route(event)
                self.assertEqual((route.group_id, route.session_id, route.sender_id),
                                 (window, window, str(OWNER)))


    async def test_non_forum_supergroup_reply_thread_uses_chat_window(self):
        platform = TelegramPlatform()
        chat = Chat(-400, "supergroup", is_forum=False)
        now = datetime.now(timezone.utc)
        quoted = Message(5, date=now, chat=chat, from_user=User(platform.client.id, "Bot", True),
                         text="上文")
        message = Message(22, date=now, chat=chat, from_user=User(OWNER, "Synthetic", False),
                          text="回复", message_thread_id=5, is_topic_message=False, reply_to_message=quoted)
        native = await platform.convert_message(Update(12, message=message), NS(bot=platform.client))
        event = platform.create_event(native)
        route = group_event_route(event)
        self.assertEqual((route.group_id, route.session_id), ("-400", "-400"))
        self.assertTrue(group_trigger_facts(event, "tg-main")["quoted_bot"])

    async def test_sdk_addressing_uses_bot_username_and_original_reply_author(self):
        platform = TelegramPlatform()
        quote = Message(10, date=datetime.now(timezone.utc), chat=Chat(-400, "group"),
                        from_user=User(platform.client.id, "Bot", True), text="之前")
        for text, fields, mentioned, replied, expected in (
            ("你好", {}, False, False, "你好"),
            ("😀 @Synthetic_bot 好", {"entities": [MessageEntity("mention", 3, 14)]},
             True, False, "😀 " + SELF_MENTION + " 好"),
            ("😀 @Synthetic_bot guide", {"entities": [MessageEntity("mention", 3, 14),
                 MessageEntity("text_link", 18, 5, url="https://example.org/guide")]},
             True, False, "😀 " + SELF_MENTION + " guide (https://example.org/guide)"),
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

    async def test_self_mention_does_not_restore_a_replaced_host_message(self):
        platform = TelegramPlatform()
        event = await group_event(platform, text="@Synthetic_bot old", entities=[MessageEntity("mention", 0, 14)])
        for part in event.message_obj.message:
            if isinstance(part, Plain):
                part.text = "经过宿主处理的新正文"
        self.assertEqual(group_message_text(event, "tg-main"), "经过宿主处理的新正文")

    async def test_bot_created_topic_only_addresses_bot_for_genuine_replies(self):
        platform = TelegramPlatform()
        for topic_root in (True, False):
            with self.subTest(topic_root=topic_root):
                event = await forum_reply_event(platform, topic_root=topic_root)
                self.assertEqual(any(isinstance(part, Reply) for part in event.message_obj.message), not topic_root)
                self.assertEqual(group_trigger_facts(event, "tg-main")["quoted_bot"], not topic_root)
                self.assertEqual(group_message_text(event, "tg-main"), "/ 原文")

    async def test_functional_commands_in_bot_replies_and_bot_created_topics(self):
        from astrbot_host.functional_commands import (
            classify_functional_command, inject_functional_command_instruction,
        )

        platform = TelegramPlatform()
        for root in (True, False):
            for text, recognized in (("陪伴群 话题", True), ("/陪伴群 话题", True),
                                     ("/ 陪伴群 话题", False), ("你好", False)):
                with self.subTest(topic_root=root, text=text):
                    event = await forum_reply_event(platform, topic_root=root, text=text)
                    self.assertEqual(event.message_obj.message_str, "/ " + text)
                    classified = classify_functional_command(event, channel="group")
                    request = NS(system_prompt="role", extra_user_content_parts=[])
                    injected = inject_functional_command_instruction(event, request, channel="group")
                    self.assertEqual(classified, injected)
                    self.assertEqual(classified is not None, recognized)

    async def test_only_genuine_same_topic_reply_captures_a_quote(self):
        from companion_v2.adapters.astrbot.telegram_references import message_references

        platform = TelegramPlatform()
        root = message_references(await forum_reply_event(platform, topic_root=True))
        self.assertIsNone(root.quote)
        self.assertFalse(root.unavailable)
        reply = message_references(await forum_reply_event(platform, topic_root=False))
        self.assertEqual((reply.quote.message_id, reply.quote.text), ("11", "之前"))

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

    def enable_typing(self, interval=1):
        from astrbot_host.management_identity import dashboard_operator
        from companion_v2.role_work.state.management import StateManagement
        manager = StateManagement(self.app, self.context)
        operator = dashboard_operator(self.app.runtime, "synthetic", at=self.clock.now())
        status = manager.status(operator).unwrap()
        manager.command(operator, dict(action="configure", operation_key="typing-on",
            edit_token=status["edit_token"], role_edit_token=status["role_edit_token"],
            changes={"typing_enabled": True, "typing_refresh_seconds": interval})).unwrap()


class TelegramIntakeLifecycleTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_queued_group_from_replaced_managed_client(self):
        # Like the SDK bot, this fixture supports weak references.
        class Bot(NS):
            pass

        config = self.controller.config
        config["multi_host"]["group_targets"] = [
            dict(target_id="topic", platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id="-400#23")]
        self.controller = self.make_controller(config)
        self.platform.client = Bot(**vars(telegram_bot()))
        self.controller.startup_readiness()
        event = await group_event(self.platform, -400, "supergroup", 24)
        configured = await group_event(self.platform, -400, "supergroup", 23)
        for _ in range(2):
            self.platform.client = Bot(**vars(telegram_bot()))
            self.controller._connections_for().refresh()
        await self.started_plugin().receive_companion_message(event)
        self.assertTrue(event.call_llm)
        await self.started_plugin().receive_companion_message(configured)
        self.assertTrue(configured.call_llm)
        event = await group_event(self.platform, -400, "supergroup", 24)
        event.client = telegram_bot()
        await self.started_plugin().receive_companion_message(event)
        self.assertFalse(event.call_llm)

    async def test_admin_group_window_selectors(self):
        from astrbot_host.group_reply_api import GroupReplyWebAPI
        config = self.controller.config
        config["multi_host"]["group_targets"] = [
            dict(target_id=name, platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id=window)
            for name, window in (("all", "*"), ("topic", "-400#23"))]
        self.controller = self.make_controller(config)
        settings = self.controller.settings
        topology = self.controller.session_topology
        accounts = topology.accounts(settings)
        configured = topology.configured(settings)
        for window, managed in (("-400#23", True), ("-400#24", False)):
            umo = f"tg-main:GroupMessage:{window}"
            with self.subTest(selector="reply", window=window):
                api = GroupReplyWebAPI(self.controller, "all", "role-tg", window)
                if managed:
                    self.assertEqual(api._selected_target(settings).target_id, "topic")
                else:
                    with self.assertRaises(TaskError):
                        api._selected_target(settings)
            with self.subTest(selector="directory", window=window):
                with patch.object(topology, "services", AsyncMock(return_value={"persona_id": "original"})):
                    rows = await topology._session_windows(umo, {"conversation_id": "tg-conversation"},
                        accounts, configured, [vars(p) for p in settings.personas], {}, managed_only=True)
                self.assertEqual(bool(rows), managed)
            with self.subTest(selector="snapshot", window=window):
                await topology._seed_configured_persona_snapshot(settings)
                topology.update_persona_selection_snapshot(umo, "tg-main", "bot-tg", "original",
                                                          "role-tg", "tg-conversation")
                self.assertEqual(topology.persona_selection_snapshot(umo, "tg-main") is not None, managed)

    def test_group_selector_platform_precedence(self):
        from astrbot_host.group_reply_api import GroupReplyWebAPI
        from tests.native_adapter_support import FakePlatform
        config = self.controller.config
        config["multi_host"]["group_targets"] = [dict(target_id="all", platform_id="tg-main",
            account_id="tg-main", bot_id="bot-tg", group_id="*")]
        self.controller = self.make_controller(config)
        api = GroupReplyWebAPI(self.controller, "all", "role-tg", "-400#24")
        for live, configured, exact_only in (
                (None, "telegram", True), (FakePlatform("tg-main"), "telegram", False),
                (self.platform, "aiocqhttp", True), (None, "aiocqhttp", False)):
            with self.subTest(live=live, configured=configured):
                self.native.platforms["tg-main"] = live
                self.native.get_config = lambda: {"platform": [dict(id="tg-main", type=configured)]}
                if exact_only:
                    with self.assertRaises(TaskError):
                        api._selected_target(self.controller.settings)
                else:
                    self.assertEqual(api._selected_target(self.controller.settings).group_id, "-400#24")

    async def test_refused_windows_during_startup_and_restart(self):
        config = self.controller.config
        config["multi_host"]["group_targets"] = [
            dict(target_id="topic", platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id="-400#23"),
            dict(target_id="wildcard", platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id="*")]
        self.window_personas["tg-main:GroupMessage:-400#23"] = "original"
        self.controller = self.make_controller(config)
        plugin = self.started_plugin()
        other = TelegramPlatform()
        other.config["id"] = "tg-other"
        self.native.platforms["tg-other"] = other

        for phase in ("startup", "restart"):
            if phase == "restart":
                await self.start_host()
                require(await self.controller.stop())
            self.assertFalse(self.controller._accepting)
            self.assertIsNone(self.controller.session)
            for platform, chat, topic, suppressed in (
                (self.platform, -401, None, True),
                (self.platform, -400, 24, True),
                (self.platform, -400, 23, False),
                (other, -400, 24, False),
            ):
                with self.subTest(phase=phase, platform=platform.meta().id, chat=chat, topic=topic):
                    event = await group_event(platform, chat, "supergroup", topic)
                    await plugin.receive_companion_message(event)
                    self.assertEqual(event.call_llm, suppressed)

    async def test_startup_requires_current_bot_and_a_verified_window(self):
        plugin = self.started_plugin()
        event = await group_event(self.platform)
        event.client = telegram_bot()
        await plugin.receive_companion_message(event)
        self.assertFalse(event.call_llm)

        event = await group_event(self.platform)
        event.message_obj.raw_message = None
        await plugin.receive_companion_message(event)
        self.assertTrue(event.call_llm)


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

    async def test_rate_limited_typing_resumes_after_the_requested_delay(self):
        self.enable_typing()
        loop = asyncio.get_running_loop()
        stamps = []
        renewed = asyncio.Event()

        async def action(**kwargs):
            stamps.append(loop.time())
            if len(stamps) == 1:
                raise RetryAfter(1)
            renewed.set()
            return True

        self.action.side_effect = action
        self.presence.begin_native_reply(self.context, "rate-limited")
        try:
            await asyncio.wait_for(renewed.wait(), 6)
            self.assertGreaterEqual(stamps[1] - stamps[0], 1)
        finally:
            self.presence.finish_native_reply(self.context, "rate-limited")

    async def test_rate_limit_survives_a_new_reply_to_the_same_peer(self):
        self.enable_typing()
        loop = asyncio.get_running_loop()
        sent = asyncio.Event()
        times = []

        async def action(**kwargs):
            times.append(loop.time())
            if len(times) == 1:
                raise RetryAfter(1)
            sent.set()
            return True

        self.action.side_effect = action
        self.presence.begin_native_reply(self.context, "first")
        route = self.presence.route(self.context)
        async with asyncio.timeout(3):
            while self.presence.results_for(self.context).get("typing:" + route, {}).get("retry_after") != 1:
                await asyncio.sleep(0.01)
        self.presence.finish_native_reply(self.context, "first")
        self.presence.begin_native_reply(self.context, "second")
        try:
            await asyncio.wait_for(sent.wait(), 5)
            self.assertGreaterEqual(times[1] - times[0], 1)
        finally:
            self.presence.finish_native_reply(self.context, "second")

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
        worker = self.presence.typing[route][2]
        async with asyncio.timeout(3):
            while not self.presence.results_for(self.context).get("typing:" + route):
                await asyncio.sleep(0.01)
        self.assertEqual(self.presence.results_for(self.context)["typing:" + route]["retry_after"], 42)
        self.assertIsNotNone(self.presence.active_turn(self.context))
        self.presence.finish_native_reply(self.context, "rate-limited")
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
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
        windows = ("-400", "-100400", "-100400#23", "*")
        config = self.controller.config
        config["multi_host"]["group_targets"] = [
            dict(target_id=f"group-{i}", platform_id="tg-main", account_id="tg-main",
                 bot_id="bot-tg", group_id=window) for i, window in enumerate(windows)]
        self.window_personas.update({f"tg-main:GroupMessage:{window}": "original" for window in windows})
        self.controller = self.make_controller(config)
        await self.start_host()
        self.platform.client.send_chat_action = AsyncMock()
        self.platform.client.send_message = AsyncMock()

    async def test_outgoing_mentions_and_optional_quote_use_the_original_topic(self):
        from astrbot_host.core_reply_outbound import prepare_core_reply_outbound_message
        from astrbot.core.message.message_event_result import MessageChain

        for username in ("synthetic_member", None):
            event = await group_event(self.platform, -100400, "supergroup", 23,
                                      from_user=User(OWNER, "Member", False, username=username))
            native = require(await self.controller.session.bridge.groups.capture(event))
            prepared = dict(native_input=native, event_id="22", channel="group",
                            outbound_settings=dict(reply_quote_enabled=True, reply_mention_speaker_enabled=True))
            message = prepare_core_reply_outbound_message(event, prepared, MessageChain(chain=[Plain("回答")]))
            async def send_message(**payload):
                if not payload.get("allow_sending_without_reply"):
                    raise BadRequest("Message to be replied not found")
            self.platform.client.send_message.side_effect = send_message
            await event.send(message)
            payload = self.platform.client.send_message.await_args.kwargs
            self.assertEqual((payload["chat_id"], payload["message_thread_id"], payload["reply_to_message_id"]),
                             ("-100400", "23", "22"))
            mention = "@synthetic_member" if username else f"tg://user?id={OWNER}"
            self.assertIn(mention, payload["text"].replace(chr(92), ""))
            self.assertIn("回答", payload["text"])
            self.assertNotIn("None", payload["text"])

    async def prepare_topic_stream(self, **outbound_settings):
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
        from astrbot_host.core_reply import activate_core_reply_delivery_guard, prepare_core_reply_event
        from astrbot_host.core_reply_outbound import prepare_core_reply_outbound_message
        from astrbot_host.core_reply_segmentation import prepare_core_reply_segmentation, uses_managed_group_stream
        from companion_v2.foundation.outcome import Outcome

        event = await group_event(self.platform, -100400, "supergroup", 23,
                                  from_user=User(OWNER, "Member", False, username=None))
        native = require(await self.controller.session.bridge.groups.capture(event))
        settings = {}
        prepared = dict(native_input=native, event_id=native.event_id, channel="group",
                        segmentation_settings={"enabled": False}, outbound_settings=settings)
        def decorate(current, message, authorize, index, count):
            return prepare_core_reply_outbound_message(event, current, message, segment_index=index)
        prepare_core_reply_event(event, prepared, authorize=AsyncMock(return_value=Outcome.success(True)),
                                 prepare_send=decorate)
        prepare_core_reply_segmentation(event)
        self.assertFalse(uses_managed_group_stream(event))
        settings.update(outbound_settings)
        self.assertTrue(uses_managed_group_stream(event))
        event.set_result(MessageEventResult().set_result_content_type(ResultContentType.STREAMING_RESULT))
        activate_core_reply_delivery_guard(event)

        return event, native.event_id

    async def test_streamed_reply_uses_topic_headers_when_requested(self):
        from astrbot.core.message.message_event_result import MessageChain

        event, source_id = await self.prepare_topic_stream(reply_quote_enabled=True, reply_mention_speaker_enabled=True)

        async def chunks():
            for word in ("一段", "回答"):
                yield MessageChain(chain=[Plain(word)])

        self.platform.client.send_message.side_effect = [BadRequest("synthetic Markdown refusal"), None]
        await event.send_streaming(chunks())
        self.assertEqual(self.platform.client.send_message.await_count, 2)
        payload = self.platform.client.send_message.await_args.kwargs
        self.assertEqual((payload["chat_id"], payload["message_thread_id"], payload["reply_to_message_id"]),
                         ("-100400", "23", source_id))
        self.assertTrue(payload["allow_sending_without_reply"])
        entity, = payload["entities"]
        self.assertEqual((entity.type, entity.offset, entity.length, entity.url),
                         ("text_link", 0, 2, f"tg://user?id={OWNER}"))
        self.assertTrue(payload["text"].startswith("成员 "))
        self.assertIn("一段回答", payload["text"])

    async def test_stream_quote_threshold_applies_to_completed_topic_reply(self):
        from astrbot.core.message.message_event_result import MessageChain

        for ending, quoted in (("", False), ("丙", True)):
            with self.subTest(ending=ending):
                event, source_id = await self.prepare_topic_stream(reply_quote_enabled=True, reply_quote_skip_chars=2)
                send = self.platform.client.send_message
                send.reset_mock()
                self.platform.client.edit_message_text = AsyncMock()

                async def chunks():
                    yield MessageChain(chain=[Plain("甲 乙")])
                    send.assert_not_awaited()
                    if ending:
                        yield MessageChain(chain=[Plain(ending)])

                await event.send_streaming(chunks())
                send.assert_awaited_once()
                self.platform.client.edit_message_text.assert_not_awaited()
                payload = send.await_args.kwargs
                self.assertEqual((payload["chat_id"], payload["message_thread_id"]), ("-100400", "23"))
                self.assertEqual(payload.get("reply_to_message_id"), source_id if quoted else None)
                self.assertEqual(payload["text"].strip(), "甲 乙" + ending)

    async def test_topic_directory_tracks_group_topic_and_member_renames(self):
        directory = self.controller.session_topology
        for number, (title, topic, member) in enumerate((("读书群", "小说", "阿青"), ("阅读群", "故事", "青")), 40):
            event = await group_event(self.platform, -100400, "supergroup", 23, text=None,
                                      title=title, message_id=number, forum_topic_edited=ForumTopicEdited(name=topic),
                                      from_user=User(OWNER, member, False))
            await directory.observe(event)
            self.assertEqual(directory.stored_group_names()[("tg-main", "tg-main", "-100400#23")],
                             f"{title}-{topic}")
            self.assertEqual(event_person_names(event), (member, ""))
        await directory.observe(await group_event(self.platform, -100400, "supergroup", text=None,
                                                 title="新群名", new_chat_title="新群名", message_id=44))
        await directory.observe(await group_event(self.platform, -100400, "supergroup", 24, text="迟到",
                                                 title="阅读群", message_id=43))
        self.assertEqual(directory.stored_group_names()[("tg-main", "tg-main", "-100400#24")], "新群名")
        self.assertEqual(directory.stored_group_names()[("tg-main", "tg-main", "-100400#23")],
                         "新群名-故事")

    async def test_topic_directory_retains_rename_after_reply_to_creation(self):
        directory = self.controller.session_topology

        async def observe(topic, message_id, **fields):
            event = await group_event(self.platform, -100400, "supergroup", topic, title="Club",
                                      message_id=message_id, **fields)
            await directory.observe(event)
            return event.message_obj.raw_message.message

        root = await observe(23, 23, text=None, forum_topic_created=ForumTopicCreated("Books", 0))
        await observe(24, 24, text=None, forum_topic_created=ForumTopicCreated("Music", 0))
        for message_id, name in ((25, "Fiction"), (27, "Stories")):
            await observe(23, message_id, text=None, forum_topic_edited=ForumTopicEdited(name=name))
            await observe(23, message_id + 1, reply_to_message=root)
            names = directory.stored_group_names()
            self.assertEqual(names[("tg-main", "tg-main", "-100400#23")], f"Club-{name}")
            self.assertEqual(names[("tg-main", "tg-main", "-100400#24")], "Club-Music")

        await observe(23, 25, text=None, forum_topic_edited=ForumTopicEdited(name="Fiction"))
        self.assertEqual(directory.stored_group_names()[("tg-main", "tg-main", "-100400#23")], "Club-Stories")

    async def test_forward_and_comment_preserve_separate_group_speakers(self):
        from astrbot_host.group_debounce import group_plain_text
        from telegram import MessageOriginHiddenUser

        comment = await group_event(self.platform, text="my view")
        forwarded = await group_event(self.platform, text="their view",
            forward_origin=MessageOriginHiddenUser(datetime.now(timezone.utc), "Original"))
        self.assertEqual(group_plain_text(comment), "my view")
        self.assertIsNone(group_plain_text(forwarded))
        captured = require(await self.controller.session.bridge.groups.capture_text_only(forwarded))
        self.assertEqual(captured.forward_origin, "Original")
        self.assertIn("their view", captured.text)

    async def test_group_quote_keeps_the_other_members_identity(self):
        quote = Message(17, date=datetime.now(timezone.utc), chat=Chat(-400, "group"),
                        from_user=User(321, "Quoted", False), text="成员原文")
        event = await group_event(self.platform, reply_to_message=quote)
        bridge = self.controller.session.bridge.groups
        for capture in (bridge.capture, bridge.capture_text_only):
            result = require(await capture(event))
            self.assertEqual(result.principal.native_account_id, str(OWNER))
            reference, = result.quotes
            self.assertEqual((reference.sender_id, reference.text, reference.companion_sender),
                             ("321", "成员原文", False))

    async def test_group_quotes_verify_admitted_link_text(self):
        session = self.controller.session
        target = self.controller.settings.target("group-0")
        url = "https://example.org/guide"
        sender = User(321, "Quoted", False)

        async def receive(event):
            event.created_at = self.clock.now().timestamp()
            return await session.receive(event, target)

        for n, prefix, mentions in ((10, "😀 ", []), (20, "😀 @Synthetic_bot <提及自己> ", [MessageEntity("mention", 3, 14)])):
            end = len(prefix.encode("utf-16-le")) // 2
            link = MessageEntity("text_link", end, 5, url=url)
            fields = dict(message_id=n, from_user=sender, text=prefix + "guide 片段", entities=[*mentions, link])
            original = await group_event(self.platform, **fields)
            require(await receive(original))
            embedded = original.message_obj.raw_message.message
            for number, extra in ((n + 1, {}), (n + 2, {"quote": TextQuote("片段", end + 6)})):
                reply = await group_event(self.platform, message_id=number, reply_to_message=embedded, **extra)
                captured = require(await session.bridge.groups.capture(reply))
                self.assertEqual((captured.principal.native_account_id, captured.quotes[0].sender_id),
                                 (str(OWNER), "321"))
                require(await receive(reply))

            for index, (changes, offset, expected) in enumerate((
                ({}, end + 5, "quote_original_changed"),
                ({"entities": [*mentions, MessageEntity("text_link", end, 5, url=url + "x")]},
                 end + 6, "quote_original_changed"),
                ({"from_user": User(322, "Other", False)}, end + 6, "quote_original_scope_mismatch"),
            ), 3):
                altered = await group_event(self.platform, **(fields | changes))
                reply = await group_event(self.platform, message_id=n + index,
                    reply_to_message=altered.message_obj.raw_message.message, quote=TextQuote("片段", offset))
                self.assertEqual((await receive(reply)).failure.code, expected)

    async def receive_group(self, event):
        event.created_at = self.clock.now().timestamp()
        return await self.controller.session.receive(event, self.controller.settings.target("group-0"))

    async def test_group_quote_of_admitted_reply(self):
        earlier = await group_event(self.platform, message_id=10, text="此前的发言")
        member = User(321, "Quoted", False)
        fields = dict(message_id=11, from_user=member, text="😀成员回答")
        original = await group_event(self.platform, **fields, reply_to_message=earlier.message_obj.raw_message.message)
        require(await self.receive_group(original))
        # Telegram omits reply_to_message inside the embedded original.
        for number, changes, selection, failure in (
            (12, {}, {}, None),
            (13, {}, {"quote": TextQuote("成员", 2)}, None),
            (14, {"text": "😀改写回答"}, {"quote": TextQuote("改写", 2)}, "quote_original_changed"),
            (15, {"from_user": User(322, "Other", False)}, {}, "quote_original_scope_mismatch"),
        ):
            embedded = await group_event(self.platform, **(fields | changes))
            reply = await group_event(self.platform, message_id=number, reply_to_message=embedded.message_obj.raw_message.message,
                                      **selection)
            result = await self.receive_group(reply)
            if failure:
                self.assertEqual(result.failure.code, failure)
            else:
                require(result)
                captured = require(await self.controller.session.bridge.groups.capture(reply))
                quote, = captured.quotes
                self.assertEqual((captured.principal.native_account_id, quote.sender_id, quote.text),
                                 (str(OWNER), "321", "成员" if selection else fields["text"]))
                self.assertNotIn("成员", captured.text)

    async def test_group_quote_verifies_redacted_forward_origin(self):
        from telegram import MessageOriginHiddenUser
        when = datetime.now(timezone.utc)
        fields = dict(message_id=10, from_user=User(321, "Forwarder", False), text="原文")
        original = await group_event(self.platform, **fields,
                                     forward_origin=MessageOriginHiddenUser(when, "token=demo"))
        require(await self.receive_group(original))
        for number, name in ((11, "token=demo"), (12, "token=changed")):
            embedded = await group_event(self.platform, **fields, forward_origin=MessageOriginHiddenUser(when, name))
            reply = await group_event(self.platform, message_id=number, reply_to_message=embedded.message_obj.raw_message.message)
            result = await self.receive_group(reply)
            if number == 11:
                require(result)
            else:
                self.assertEqual(result.failure.code, "quote_original_changed")

    async def test_group_quote_of_admitted_forward(self):
        from telegram import MessageOriginUser
        origin = MessageOriginUser(datetime.now(timezone.utc), User(42, "Source", False))
        fields = dict(message_id=10, from_user=User(321, "Forwarder", False), text="😀转发原文", forward_origin=origin)
        original = await group_event(self.platform, **fields)
        require(await self.receive_group(original))
        for number, changes, selection, failure in (
            (11, {}, {}, None),
            (12, {}, {"quote": TextQuote("原文", 4)}, None),
            (13, {"text": "😀转发改写"}, {"quote": TextQuote("改写", 4)}, "quote_original_changed"),
            (14, {"forward_origin": None}, {}, "quote_original_changed"),
        ):
            embedded = await group_event(self.platform, **(fields | changes))
            reply = await group_event(self.platform, message_id=number, reply_to_message=embedded.message_obj.raw_message.message,
                                      **selection)
            result = await self.receive_group(reply)
            if failure:
                self.assertEqual(result.failure.code, failure)
            else:
                require(result)
                captured = require(await self.controller.session.bridge.groups.capture(reply))
                quote, = captured.quotes
                self.assertEqual((captured.principal.native_account_id, quote.sender_id, quote.forward_origin.user_id),
                                 (str(OWNER), "321", "42"))
                self.assertEqual(quote.text, "原文" if selection else fields["text"])
                self.assertNotIn("原文", captured.text)

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
            from companion_v2.delivery.plan import OutboundPlan
            self.platform.client.get_chat_member = AsyncMock(return_value=NS(user=User(OWNER, "成员", False,
                                                                                       username="speaker")))
            plan = OutboundPlan(quote="22", mentions=(str(OWNER),)) if topic else None
            receipt = await sender.send(DeliveryEnvelope("part", "op", context, 1, "后台", digest("后台"),
                                        binding, before_dispatch=lambda: True, plan=plan))
            self.assertEqual(receipt.status, "delivered", receipt)
            calls = self.platform.client.send_message.await_args_list
            self.assertEqual(len(calls), 2)
            if topic:
                self.assertIn("@speaker", calls[1].kwargs["text"])
                self.assertEqual(calls[1].kwargs["reply_to_message_id"], "22")
                self.assertTrue(calls[1].kwargs["allow_sending_without_reply"])
            for call in calls:
                self.assertEqual(call.kwargs["chat_id"], str(chat))
                self.assertEqual(call.kwargs.get("message_thread_id"), str(topic) if topic else None)
            self.platform.client.send_message.reset_mock()

    async def test_group_command_uses_current_topic_and_sender_standing(self):
        from astrbot_host.group_command import receive_group_llm_command
        from companion_v2.execution.models import require
        platform, controller = self.platform, self.controller
        platform.client.get_chat_member = AsyncMock()
        topic_creator = Message(23, date=datetime.now(timezone.utc),
                                chat=Chat(-100400, "supergroup", is_forum=True),
                                from_user=User(platform.client.id, "Bot", True),
                                forum_topic_created=ForumTopicCreated("Topic", 0))
        for status, text in (("administrator", "陪伴群 关闭LLM"), ("creator", "陪伴群 开启LLM")):
            platform.client.get_chat_member.return_value = NS(user=NS(id=OWNER), status=status)
            event = await group_event(platform, -100400, "supergroup", 23, text=text,
                                      reply_to_message=topic_creator)
            result = require(await receive_group_llm_command(controller, event))
            self.assertIn("只有安装管理员", result["text"])
        authority = controller.authority
        context = authority.context("tg-owner", "role-tg")
        authority.provision_scope(authority.settings.target("tg-owner"), context.scope,
                                  context.role_binding.role, core_admin=True)
        for status, text, expected in (
                ("administrator", "陪伴群 关闭LLM", "已临时封锁"),
                ("member", "陪伴群 开启LLM", "未执行"),
                ("administrator", "陪伴群 关闭LLM", "已经处于临时封锁"),
                ("creator", "陪伴群 LLM状态", "临时封锁"),
                ("administrator", "陪伴群 开启LLM", "已恢复"),
                ("administrator", "陪伴群 关闭生图", "已关闭"),
                ("creator", "陪伴群 开启生图", "已开启"),
                ("member", "陪伴群 开启生图", "未执行"),
                ("member", "陪伴群 关闭LLM", "未执行"),
                (None, "陪伴群 开启LLM", "未执行")):
            platform.client.get_chat_member.return_value = NS(user=NS(id=OWNER), status=status)
            event = await group_event(platform, -100400, "supergroup", 23, text=text,
                                      reply_to_message=topic_creator)
            result = require(await receive_group_llm_command(controller, event))
            self.assertIn(expected, result["text"])
            kwargs = platform.client.send_message.await_args.kwargs
            self.assertEqual((kwargs["chat_id"], kwargs["message_thread_id"]), ("-100400", "23"))

    async def test_group_voice_language_rechecks_standing_before_each_write(self):
        from datetime import timedelta
        from astrbot_host import functional_commands as commands

        controller = self.controller
        authority = controller.authority
        owner = authority.context("tg-owner", "role-tg")
        lookup = self.platform.client.get_chat_member = AsyncMock()
        for number, (core, status, language, expected) in enumerate((
                (False, "administrator", "日语", "未执行"),
                (True, "administrator", "日语", "已切换"),
                (True, "member", "英语", "未执行"),
                (True, None, "英语", "未执行"),
                (True, "creator", "日语", "没有更改")), 100):
            with self.subTest(core=core, status=status):
                if number == 101:
                    authority.provision_scope(authority.settings.target("tg-owner"), owner.scope,
                                              owner.role_binding.role, core_admin=True)
                self.clock.advance(timedelta(seconds=3))
                lookup.return_value = NS(user=NS(id=OWNER), status=status)
                event = await group_event(self.platform, -100400, "supergroup", 23, message_id=number,
                    text=f"@synthetic_bot 陪伴群 切换语种 {language}", entities=[MessageEntity("mention", 0, 14)])
                event.created_at = self.clock.now().timestamp()
                admission = require(await controller.group_reply_candidate(event))
                prepared = require(await controller.prepare_core_reply(event, group_admission=admission))
                command = commands.classify_functional_command(event, channel="group")
                lookup.reset_mock()
                await commands.prepare_telegram_voice_language_receipt(event, command, controller.session, prepared)
                receipt = commands.functional_command_receipt(event, command, application=controller.session.application,
                                                               context=prepared["context"])
                self.assertIn(expected, receipt)
                lookup.assert_awaited_once_with(chat_id=-100400, user_id=OWNER)

    async def test_group_room_only_sends_ticket_to_requester(self):
        from astrbot_host.native_extensions.together.rooms_methods import TogetherRoomsMethods
        from companion_v2.together.models import RoomTicketStore
        from companion_v2.together.provider import OperationStore
        from tests.test_together_room_creation import FakePlugin, provider_with, request

        class Plugin(FakePlugin, TogetherRoomsMethods):
            def issue_room_ticket(self, *, mode, user_id, native_v2):
                self.issued = self.ticket_store.issue(mode=mode, user_id=user_id)
                return self.issued

        plugin = Plugin()
        plugin.controller = self.controller
        plugin.ticket_store = RoomTicketStore()
        provider = provider_with(plugin)
        value = request(kind="group")
        value["context"]["audience"]["scope"].update(platform="tg-main", account_id="tg-main",
            bot_id="bot-tg", session_id="-100400#23", native_scope_id="-100400#23")
        value["host_persona"].update(account_id="tg-main", bot_id="bot-tg")
        for refused, revoked, expected in (
                (False, True, ("succeeded", "committed")),
                (True, True, ("failed", "not_committed")),
                (True, False, ("unknown", "unknown"))):
            async def send(**kwargs):
                if str(kwargs["chat_id"]) == str(OWNER):
                    if not revoked:
                        plugin.ticket_store.consume(plugin.issued.token)
                    if refused:
                        raise Forbidden("private chat unavailable")
                return NS(message_id=77)

            self.platform.client.send_message.side_effect = send
            result = await provider._create_room(value, "scope", {"id": "op"}, user_id=str(OWNER))
            response = provider._response(dict(action="together.room.create", invocation_id="call", origin_key="test",
                arguments_hash="hash", binding={}), result)
            self.assertEqual((response["state"], response["effect"]), expected)
            self.assertNotIn(plugin.issued.token, json.dumps(response))
            calls = self.platform.client.send_message.await_args_list
            self.assertEqual(str(calls[0].kwargs["chat_id"]), str(OWNER))
            self.assertIn("/join/", calls[0].kwargs["text"])
            if refused and revoked:
                self.assertIsNone(response["result"])
                self.assertEqual(str(calls[1].kwargs["chat_id"]), "-100400")
                self.assertEqual(str(calls[1].kwargs["message_thread_id"]), "23")
                self.assertIn("私聊启动", calls[1].kwargs["text"])
                self.assertNotIn("/join/", calls[1].kwargs["text"])
                self.assertFalse(OperationStore._is_reclaimable_failure(result))
            self.assertEqual(len(calls), 2 if refused and revoked else 1)
            self.platform.client.send_message.reset_mock()
            self.assertEqual(plugin.ticket_store.get(plugin.issued.token) is None, refused)

    async def test_group_event_invite_uses_requesters_private_chat(self):
        from astrbot_host.native_extensions.together.rooms_methods import TogetherRoomsMethods

        service = TogetherRoomsMethods()
        service.controller = self.controller
        event = await group_event(self.platform, -100400, "supergroup", 23)
        self.platform.client.send_message.return_value = NS(message_id=77)
        invite = "https://example.invalid/join/a__b_C-d?mode=call"
        self.assertTrue(await service._send_private_room_link(event, invite))
        send = self.platform.client.send_message
        send.assert_awaited_once()
        self.assertEqual(str(send.await_args.kwargs["chat_id"]), str(OWNER))
        self.assertEqual(send.await_args.kwargs["text"], invite)
        self.assertIsNone(send.await_args.kwargs.get("parse_mode"))
        self.assertNotIn("message_thread_id", {k: v for k, v in send.await_args.kwargs.items() if v is not None})

    async def test_repeat_sends_the_configured_interruption_into_the_topic(self):
        from companion_v2.configuration.models import ConfigLayer, ConfigTarget
        from companion_v2.conversation.participation import (
            CONFIG_GROUP, GroupParticipationConfigurationOwner, configuration_fields,
        )

        async def event(number, text):
            value = await group_event(self.platform, -100400, "supergroup", 23, text=text, message_id=number)
            value.created_at = self.clock.now().timestamp()
            return value

        context = require(await self.controller.observe_group_early(await event(10, "初始化")))
        target = ConfigTarget(context.scope.installation_id, ConfigLayer.WINDOW, context.role_binding.role, context.scope)
        owner = GroupParticipationConfigurationOwner(self.controller.runtime)
        fields = configuration_fields()
        settings = {"group_repeat_trigger_threshold": 3, "group_repeat_follow_probability": 0,
                    "group_repeat_interrupt_probability": .8, "group_repeat_interrupt_text": "暂停复读"}
        owner.write(target, fields, changes={CONFIG_GROUP + "." + k: v for k, v in settings.items()},
                    reset=(), expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        for number in (11, 12, 13):
            self.clock.advance(timedelta(seconds=1))
            current = await event(number, "合成测试")
            admission = require(await self.controller.group_reply_candidate(current))
        self.assertEqual(admission["reason"], "repeat")
        with patch("companion_v2.conversation.repeat_actions.random.random", return_value=.1):
            require(await self.controller.receive_group_repeat(current, admission))
        self.platform.client.send_message.assert_awaited_once()
        kwargs = self.platform.client.send_message.await_args.kwargs
        self.assertEqual((kwargs["text"], kwargs["chat_id"], kwargs["message_thread_id"]), ("暂停复读", "-100400", "23"))

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


class TelegramGroupRoomToolTests(TelegramHostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_topic_tool_creates_room_and_replays_private_receipt(self):
        from astrbot.core.agent.message import Message as AgentMessage, ToolCall
        from astrbot_host.core_reply import prepare_core_reply_event
        from astrbot_host.core_reply_tools import close_core_reply_tools, inject_core_reply_tools, native_tool_name
        from astrbot_host.native_extensions.together.rooms_methods import TogetherRoomsMethods
        from companion_v2.capabilities import CapabilityAuthorizationState, CapabilityRepository
        from companion_v2.product import PRODUCT_VERSION
        from companion_v2.together.models import RoomTicketStore
        from companion_v2.together.provider import NativeTogetherProvider
        from tests.native_internal_support import internal_lease
        from tests.test_together_room_creation import FakePlugin

        class Plugin(FakePlugin, TogetherRoomsMethods):
            issue_room_ticket = TogetherRoomsMethods.issue_room_ticket

        plugin = Plugin()
        plugin.rooms = {}
        plugin.detached_rooms = {}
        plugin.ticket_store = RoomTicketStore()
        provider = NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        lease = internal_lease(provider)
        config = self.controller.config
        config["multi_host"]["group_targets"] = [dict(target_id=f"topic-{topic}", platform_id="tg-main",
            account_id="tg-main", bot_id="bot-tg", group_id=f"-100400#{topic}") for topic in (23, 24)]
        self.window_personas["tg-main:GroupMessage:-100400#23"] = "original"
        self.window_personas["tg-main:GroupMessage:-100400#24"] = "original"
        self.controller = plugin.controller = self.make_controller(config)
        with patch("astrbot_host.multi_controller.create_bundled_extensions", return_value=((lease,), ())):
            app = await self.start_host()
        self.addAsyncCleanup(close_core_reply_tools, app)
        self.platform.client.send_chat_action = AsyncMock()
        self.platform.client.send_message = AsyncMock(return_value=NS(message_id=77))
        event = await group_event(self.platform, -100400, "supergroup", 23, text="@synthetic_bot 开房",
                                  from_user=User(OWNER + 1, "Member", False), entities=[MessageEntity("mention", 0, 14)])
        event.created_at = self.clock.now().timestamp()
        admission = require(await self.controller.group_reply_candidate(event))
        prepared = require(await self.controller.prepare_core_reply(event, group_admission=admission))
        context = prepared["context"]
        repo = CapabilityRepository(app.runtime)
        create = "together.room.create"
        close = "together.room.close"
        package = lease.manifest.package
        require(repo.acquire(extension_id="together", package_id=package.package_id,
            package_version=package.package_version, expected_revision=0, observed_at=self.clock.now()))
        require(repo.set_role_enabled(extension_id="together", role=context.role_binding.role,
            bot_id=context.scope.bot_id, enabled=True, expected_revision=0, observed_at=self.clock.now()))
        self.assertNotIn(create, {tool.action for tool in app.tools.catalog_for(context)})
        self.assertNotIn(close, {tool.action for tool in app.tools.catalog_for(context)})
        for capability in package.capabilities:
            require(repo.set_authorization(extension_id="together", capability_id=capability,
                subject=context.actor.subject, scope=context.scope, state=CapabilityAuthorizationState.ACTIVE,
                expected_revision=0, observed_at=self.clock.now()))
        self.assertEqual({tool.action for tool in app.tools.catalog_for(context)
                          if tool.action.startswith("together.")}, {create, close})

        groups = self.controller.session.bridge.groups
        with patch.dict(groups.platforms, {"tg-main": (self.native.platforms["qq-main"], None)}):
            self.assertFalse({create, close} & {tool.action for tool in app.tools.catalog_for(context)})

        async def authorize(state):
            return await self.controller.authorize_core_reply(event, state)

        prepare_core_reply_event(event, prepared, authorize=authorize)
        request = NS(func_tool=None, system_prompt="")
        inject_core_reply_tools(event, request, application=app)
        name = native_tool_name(create)
        tool = request.func_tool.get_tool(name)
        self.assertIsNotNone(tool)
        async def invoke(identifier, mode="call"):
            call = ToolCall(id=identifier, function=ToolCall.FunctionBody(name=name, arguments=json.dumps({"mode": mode})))
            wrapper = NS(context=NS(event=event), messages=[AgentMessage(role="assistant", tool_calls=[call])])
            return json.loads(await tool.call(wrapper, mode=mode))

        for _ in range(2):
            result = await invoke("room-call")
            self.assertEqual(result["state"], "succeeded", result)
            material = json.loads(result["payload"])["native"]["result"]
            self.assertTrue(material["delivered_privately"])
            self.assertNotIn("room", material)
        self.platform.client.send_message.assert_awaited_once()
        sent = self.platform.client.send_message.await_args.kwargs
        self.assertEqual(str(sent["chat_id"]), str(OWNER + 1))
        token = sent["text"].split("/join/")[1].split("?")[0]
        self.assertNotIn(token, json.dumps(result))
        ticket = plugin.ticket_store.get(token)
        self.assertEqual(ticket.user_id, str(OWNER + 1))
        binding = ticket.native_v2
        self.assertEqual((binding.persona_id, binding.session_id, binding.scope_kind),
                         ("original", "-100400#23", "group"))
        self.assertTrue(binding.model_provider_id)

        self.platform.client.send_message.reset_mock()
        self.platform.client.send_message.side_effect = [Forbidden("private chat unavailable"), None]
        for _ in range(2):
            result = await invoke("refused-room-call")
            self.assertEqual(result["state"], "failed", result)
        private, notice = self.platform.client.send_message.await_args_list
        self.assertEqual(str(private.kwargs["chat_id"]), str(OWNER + 1))
        refused = private.kwargs["text"].split("/join/")[1].split("?")[0]
        self.assertIsNone(plugin.ticket_store.get(refused))
        self.assertEqual(str(notice.kwargs["chat_id"]), "-100400")
        self.assertEqual(str(notice.kwargs["message_thread_id"]), "23")
        self.assertIn("私聊启动", notice.kwargs["text"])
        self.assertNotIn(refused, notice.kwargs["text"] + json.dumps(result))

        self.platform.client.send_message.reset_mock()
        self.platform.client.send_message.side_effect = TimeoutError("Telegram acknowledgement lost")
        for _ in range(2):
            result = await invoke("uncertain-room-call", mode="watch")
            self.assertEqual(result["state"], "unknown", result)
        self.platform.client.send_message.assert_awaited_once()
        private = self.platform.client.send_message.await_args.kwargs
        self.assertEqual(str(private["chat_id"]), str(OWNER + 1))
        uncertain = private["text"].split("/join/")[1].split("?")[0]
        self.assertIsNotNone(plugin.ticket_store.get(uncertain))
        self.assertNotIn(uncertain, json.dumps(result))

        async def close_from(source, offered, identifier):
            close_name = native_tool_name(close)
            call = ToolCall(id=identifier, function=ToolCall.FunctionBody(name=close_name, arguments="{}"))
            wrapper = NS(context=NS(event=source), messages=[AgentMessage(role="assistant", tool_calls=[call])])
            return json.loads(await offered.func_tool.get_tool(close_name).call(wrapper))

        # Even with close permission, a different requester or topic cannot spend either link.
        for index, (member, topic) in enumerate(((OWNER + 2, 23), (OWNER + 1, 24))):
            other = await group_event(self.platform, -100400, "supergroup", topic, text="@synthetic_bot 关房",
                                      message_id=30 + index, from_user=User(member, "Member", False),
                                      entities=[MessageEntity("mention", 0, 14)])
            other.created_at = self.clock.now().timestamp()
            admission = require(await self.controller.group_reply_candidate(other))
            turn = require(await self.controller.prepare_core_reply(other, group_admission=admission))
            other_context = turn["context"]
            require(repo.set_authorization(extension_id="together", capability_id=close,
                subject=other_context.actor.subject, scope=other_context.scope, state=CapabilityAuthorizationState.ACTIVE,
                expected_revision=0, observed_at=self.clock.now()))
            prepare_core_reply_event(other, turn, authorize=lambda state: self.controller.authorize_core_reply(other, state))
            offered = NS(func_tool=None, system_prompt="")
            inject_core_reply_tools(other, offered, application=app)
            denied = await close_from(other, offered, f"foreign-close-{index}")
            self.assertEqual(denied["state"], "failed", denied)
            self.assertIsNotNone(plugin.ticket_store.get(uncertain))
            self.assertIsNotNone(plugin.ticket_store.get(token))

        for _ in range(2):
            closed = await close_from(event, request, "close-uncertain")
            self.assertEqual(closed["state"], "succeeded", closed)
            result = json.loads(closed["payload"])["native"]["result"]
            self.assertTrue(result["ticket_revoked"])
            self.assertNotIn(uncertain, json.dumps(closed))
            self.assertNotIn("/join/", json.dumps(closed))
            self.assertIsNone(plugin.ticket_store.get(uncertain))
            self.assertIsNotNone(plugin.ticket_store.get(token))
        self.assertEqual((await invoke("uncertain-room-call", mode="watch"))["state"], "unknown")
        self.platform.client.send_message.assert_awaited_once()

        # Redeeming the older delivered ticket makes its room the next close target.
        redeemed = plugin.ticket_store.consume(token)
        room = NS(room_id="redeemed-room", ticket_token=token, user_id=redeemed.user_id,
                  integration_closed=False, native_v2=redeemed.native_v2, created_at=redeemed.created_at)
        plugin.rooms[room.room_id] = room
        plugin.close_room = AsyncMock()
        for _ in range(2):
            closed = await close_from(event, request, "close-redeemed")
            self.assertEqual(closed["state"], "succeeded", closed)
            self.assertEqual(json.loads(closed["payload"])["native"]["result"]["room_id"], room.room_id)
            self.assertNotIn(token, json.dumps(closed))
        plugin.close_room.assert_awaited_once_with(room)



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

    async def test_delivery_markdown_fallback_warning_redacts_the_bot_token(self):
        token = self.platform.client.token
        self.platform.client.send_message.side_effect = [
            BadRequest(f"can't parse entities: {token}"), InvalidToken(token),
        ]
        with self.assertLogs("astrbot", level="WARNING") as host_logs:
            receipt = await self.send()
        self.assertEqual((receipt.status, receipt.failure_code), ("not_sent", "telegram_invalid_token"))
        self.assertNotIn(token, " ".join(host_logs.output))
        self.assertIn("[redacted]", " ".join(host_logs.output))

    async def test_private_notice_redacts_markdown_fallback_warning(self):
        from astrbot_host.private_notice import send_private_notice

        event = telegram_event(self.platform)
        route = authenticate_notice_route(self.controller, event, ScopeKind.PRIVATE, expected_post="message")
        token = self.platform.client.token
        self.platform.client.send_message.side_effect = [BadRequest(f"can't parse entities: {token}"), None]
        with self.assertLogs("astrbot", level="WARNING") as logs:
            receipt = await send_private_notice(self.controller, event, text="验证", route=route)
        self.assertEqual(receipt["state"], "delivered")
        self.assertNotIn(token, " ".join(logs.output))
        self.assertIn("[redacted]", " ".join(logs.output))

    def reply_to_prompt(self, message_id, prompt_id, text="可以"):
        event = telegram_event(self.platform, message_id, text)
        raw = event.message_obj.raw_message.message
        with raw._unfrozen():
            raw.reply_to_message = Message(prompt_id, raw.date, raw.chat, text="permission")
        return event

    def private_relay_task(self, state, confirm, prompt_id=None, question_id=None):
        from time import time
        from astrbot_host.relay_pending import add_task
        return add_task(self.controller.runtime, dict(kind="private_receipt", state=state,
            target_user_id=str(OWNER), source_id=str(OWNER), confirm_before_report=confirm,
            reply_text="private answer", question_message_id=question_id, prompt_message_id=prompt_id, expires_at=time()+60, telegram_account=["tg-main", "tg-main"]))

    async def test_private_relay_unknown_prompt_and_report_are_terminal(self):
        from astrbot_host.relay_pending import _read
        target = self.controller.settings.target("tg-owner")
        message_ids = iter(range(20, 26))
        for state, confirm in (("waiting_reply", True), ("waiting_permission", True), ("waiting_reply", False)):
            with self.subTest(state=state, confirm=confirm):
                task_id = self.private_relay_task(state, confirm, prompt_id=11)
                self.platform.client.send_message.side_effect = TimeoutError()
                for _ in range(2):
                    event = (self.reply_to_prompt(next(message_ids), 11) if state == "waiting_permission"
                             else telegram_event(self.platform, next(message_ids), "可以"))
                    await self.controller.process_private_relay_receipts(event, target)
                self.assertEqual(_read(self.controller.runtime)["tasks"][task_id]["state"], "unknown")
                self.platform.client.send_message.assert_awaited_once()
                self.platform.client.send_message.reset_mock()

    async def test_forwarded_private_reply_leaves_receipts_waiting(self):
        from telegram import MessageOriginHiddenUser
        from astrbot_host.relay_pending import _read
        target = self.controller.settings.target("tg-owner")
        event = telegram_event(self.platform, 1, "forwarded answer")
        raw = event.message_obj.raw_message.message
        with raw._unfrozen():
            raw.forward_origin = MessageOriginHiddenUser(raw.date, "Synthetic")
        self.platform.client.send_message.return_value = NS(message_id=6)
        for answer_id, confirm in ((2, False), (3, True)):
            with self.subTest(confirm=confirm):
                task_id = self.private_relay_task("waiting_reply", confirm)
                await self.controller.process_private_relay_receipts(event, target)
                self.platform.client.send_message.assert_not_awaited()
                self.assertEqual(_read(self.controller.runtime)["tasks"][task_id]["state"], "waiting_reply")
                await self.controller.process_private_relay_receipts(telegram_event(self.platform, answer_id, "my answer"), target)
                self.platform.client.send_message.assert_awaited_once()
                self.platform.client.send_message.reset_mock()
                task = _read(self.controller.runtime)["tasks"][task_id]
                self.assertEqual(task["state"], "waiting_permission" if confirm else "reported")
                if confirm:
                    self.assertEqual(task["reply_text"], "my answer")

    async def test_private_relay_permission_requires_an_explicit_new_answer(self):
        from astrbot_host.relay_pending import _read

        target = self.controller.settings.target("tg-owner")
        first = self.private_relay_task("waiting_permission", True, prompt_id=72)
        receive = self.controller.process_private_relay_receipts
        await receive(telegram_event(self.platform, 80, "今天心情很好"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][first]["state"], "waiting_permission")
        await receive(telegram_event(self.platform, 79, "可以"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][first]["state"], "waiting_permission")
        self.platform.client.send_message.assert_not_awaited()
        await receive(self.reply_to_prompt(81, 72, "不同意"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][first]["state"], "declined")
        self.platform.client.send_message.assert_not_awaited()
        second = self.private_relay_task("waiting_permission", True, prompt_id=75)
        answer = self.reply_to_prompt(82, 75, "可以。")
        await receive(answer, target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][second]["state"], "reported")
        third = self.private_relay_task("waiting_permission", True)
        await receive(answer, target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][third]["state"], "waiting_permission")
        self.platform.client.send_message.assert_awaited_once()

    async def test_private_receipt_does_not_report_to_removed_source_topic(self):
        from time import time
        from astrbot_host.relay_pending import _read, add_task
        target = self.controller.settings.target("tg-owner")
        task = add_task(self.controller.runtime, dict(kind="private_receipt", state="waiting_reply",
            target_user_id=str(OWNER), source_kind="group", source_id="-100941#54", confirm_before_report=False,
            question_message_id=71, expires_at=time()+60, telegram_account=["tg-main", "tg-main"]))
        self.platform.send_by_session = AsyncMock(wraps=self.platform.send_by_session)
        await self.controller.process_private_relay_receipts(telegram_event(self.platform, 72, "private reply"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "failed")
        self.platform.client.send_message.assert_not_awaited()
        self.platform.send_by_session.assert_not_awaited()

    async def test_early_consent_waits_for_the_native_prompt_receipt(self):
        from astrbot_host.relay_pending import _read
        from astrbot_host.relay_pending_events import process_private_receipts
        target = self.controller.settings.target("tg-owner")
        task = self.private_relay_task("waiting_reply", True, question_id=70)
        receive = self.controller.process_private_relay_receipts
        dispatched, acknowledge = asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            if not dispatched.is_set():
                dispatched.set()
                await acknowledge.wait()
                return NS(message_id=80)
            return NS(message_id=82)

        self.platform.client.send_message.side_effect = send
        answer = asyncio.create_task(receive(self.reply_to_prompt(71, 70, "my reply"), target))
        try:
            await asyncio.wait_for(dispatched.wait(), 60)
            with patch("astrbot_host.relay_pending_events.process_private_receipts",
                       wraps=process_private_receipts) as handler:
                consent = asyncio.create_task(receive(self.reply_to_prompt(81, 80), target))
                await asyncio.sleep(0)
                handled_before_ack = handler.await_count
                acknowledge.set()
                await asyncio.gather(answer, consent)
        finally:
            acknowledge.set()
        self.assertEqual(handled_before_ack, 0)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "reported")
        self.assertEqual(self.platform.client.send_message.await_count, 2)

    async def test_attachment_without_caption_leaves_relay_question_waiting(self):
        from astrbot_host.relay_pending import _read
        target = self.controller.settings.target("tg-owner")
        task = self.private_relay_task("waiting_reply", False, question_id=91)
        event = telegram_event(self.platform, 92, "")
        event.message_str = "[图片]"
        await self.controller.process_private_relay_receipts(event, target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "waiting_reply")
        self.platform.client.send_message.assert_not_awaited()
        caption = telegram_event(self.platform, 93, "")
        with caption.message_obj.raw_message.message._unfrozen():
            caption.message_obj.raw_message.message.caption = "my own caption"
        await self.controller.process_private_relay_receipts(caption, target)
        self.platform.client.send_message.assert_awaited_once()
        self.assertIn("my own caption", self.platform.client.send_message.await_args.kwargs["text"])

    async def test_telegram_question_accepts_only_subsequent_ordinary_messages(self):
        from astrbot_host.relay_pending import _read

        target = self.controller.settings.target("tg-owner")
        task = self.private_relay_task("waiting_reply", False, question_id=101)
        receive = self.controller.process_private_relay_receipts
        await receive(telegram_event(self.platform, 99, "earlier private message"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "waiting_reply")
        external = telegram_event(self.platform, 102, "answer to a different chat")
        raw = external.message_obj.raw_message.message
        with raw._unfrozen():
            raw.external_reply = NS()
        await receive(external, target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "waiting_reply")
        self.platform.client.send_message.assert_not_awaited()
        await receive(telegram_event(self.platform, 103, "my answer"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][task]["state"], "reported")
        self.platform.client.send_message.assert_awaited_once()

    async def test_multiple_waiting_telegram_questions_need_an_addressed_answer(self):
        from astrbot_host.relay_pending import _read

        target = self.controller.settings.target("tg-owner")
        first = self.private_relay_task("waiting_reply", False, question_id=80)
        second = self.private_relay_task("waiting_reply", False, question_id=81)
        receive = self.controller.process_private_relay_receipts
        await receive(telegram_event(self.platform, 82, "ambiguous"), target)
        self.platform.client.send_message.assert_not_awaited()
        await receive(self.reply_to_prompt(83, 81, "for the second requester"), target)
        tasks = _read(self.controller.runtime)["tasks"]
        self.assertEqual((tasks[first]["state"], tasks[second]["state"]), ("waiting_reply", "reported"))

    async def test_unaddressed_reply_during_permission_cannot_answer_another_relay(self):
        from astrbot_host.relay_pending import _read

        target = self.controller.settings.target("tg-owner")
        prompt = self.private_relay_task("waiting_permission", True, prompt_id=80)
        question = self.private_relay_task("waiting_reply", False, question_id=81)
        receive = self.controller.process_private_relay_receipts
        await receive(telegram_event(self.platform, 82, "请勿转告"), target)
        self.platform.client.send_message.assert_not_awaited()
        tasks = _read(self.controller.runtime)["tasks"]
        self.assertEqual((tasks[prompt]["state"], tasks[question]["state"]), ("waiting_permission", "waiting_reply"))
        await receive(self.reply_to_prompt(83, 81, "answer to the question"), target)
        self.platform.client.send_message.assert_awaited_once()

    async def test_private_receipt_consent_selects_the_replied_to_prompt(self):
        from astrbot_host.relay_pending import _read

        target = self.controller.settings.target("tg-owner")
        receive = self.controller.process_private_relay_receipts
        first = self.private_relay_task("waiting_reply", True)
        self.platform.client.send_message.return_value = NS(message_id=100)
        await receive(telegram_event(self.platform, 90, "first answer"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][first]["prompt_message_id"], 100)
        second = self.private_relay_task("waiting_reply", True, question_id=101)
        self.platform.client.send_message.return_value = NS(message_id=103)
        await receive(self.reply_to_prompt(102, 101, "可以"), target)
        self.assertEqual(_read(self.controller.runtime)["tasks"][second]["prompt_message_id"], 103)
        self.platform.client.send_message.reset_mock()
        await receive(telegram_event(self.platform, 107, "可以"), target)
        self.platform.client.send_message.assert_not_awaited()

        await receive(self.reply_to_prompt(106, 105), target)
        self.platform.client.send_message.assert_not_awaited()
        allowed = self.reply_to_prompt(104, 103)
        await receive(allowed, target)
        await receive(allowed, target)
        self.platform.client.send_message.assert_awaited_once()
        tasks = _read(self.controller.runtime)["tasks"]
        self.assertEqual((tasks[first]["state"], tasks[second]["state"]), ("waiting_permission", "reported"))
        self.assertIn("可以", self.platform.client.send_message.await_args.kwargs["text"])

    async def test_only_direct_telegram_words_can_grant_private_relay_permission(self):
        from telegram import MessageOriginHiddenUser
        from astrbot_host.relay_pending import _read
        target = self.controller.settings.target("tg-owner")
        task_id = self.private_relay_task("waiting_permission", True, prompt_id=1)
        event = self.reply_to_prompt(3, 1)
        raw = event.message_obj.raw_message.message
        with raw._unfrozen():
            raw.forward_origin = MessageOriginHiddenUser(raw.date, "Synthetic")
        await self.controller.process_private_relay_receipts(event, target)
        self.platform.client.send_message.assert_not_awaited()
        self.assertEqual(_read(self.controller.runtime)["tasks"][task_id]["state"], "waiting_permission")
        await self.controller.process_private_relay_receipts(self.reply_to_prompt(4, 1), target)
        self.platform.client.send_message.assert_awaited_once()
        self.assertEqual(_read(self.controller.runtime)["tasks"][task_id]["state"], "reported")

    async def test_relay_receipt_uses_the_telegram_client_without_an_event_lease(self):
        from astrbot_host.relay_pending_events import _send_private

        event = telegram_event(self.platform)
        token = self.platform.client.token
        self.platform.client.send_message.side_effect = [BadRequest(f"can't parse entities: {token}"), None]
        with self.assertLogs("astrbot", level="WARNING") as logs:
            receipt = await _send_private(self.controller, event, platform_id="tg-main",
                                          login="tg-main", peer=str(OWNER), text="送达回执")
        self.assertEqual(receipt["state"], "delivered")
        self.assertEqual(self.platform.client.send_message.await_count, 2)
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
            async def message(*args, **kwargs):
                await http.post(f"https://telegram.invalid/bot{token}/sendMessage")

            self.platform.client.send_message.side_effect = message
            with self.assertLogs("httpx", "INFO") as logs:
                self.assertEqual((await self.send()).status, "delivered")
                notice = await send_private_notice(self.controller, event, text="验证", route=route)
                relay = await _send_private(self.controller, event, platform_id="tg-main",
                                            login="tg-main", peer=str(OWNER), text="回执")
                self.assertEqual((notice["state"], relay["state"]), ("delivered", "delivered"))

        self.platform.client.send_chat_action.assert_not_awaited()
        methods = ["sendMessage"] * 3
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
