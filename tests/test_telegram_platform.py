"""Telegram private text: the bot is the event's ``client``, and a rebuilt bot keeps the chat admitted."""

from tests import astrbot_data_root  # noqa: F401 - isolate the SDK before import

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

from telegram import Chat, Message, Update, User
from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.recalls import authenticate_notice_route
from companion_v2.adapters.astrbot.group import group_event_route
from companion_v2.adapters.astrbot.native import event_native_client, supported_platform_name
from companion_v2.execution.models import TaskError, require
from companion_v2.foundation.refs import ScopeKind
from tests.multi_host_support import MultiFixture, multi_config
from tests.test_main_no_reply import ENTRY

OWNER = 5123456789012


def telegram_bot(token="424242:synthetic"):
    return NS(token=token)


class TelegramPlatform:
    def __init__(self):
        self.metadata = NS(name="telegram", id="tg-main")
        self.client = telegram_bot()

    def meta(self):
        return self.metadata

    def get_client(self):
        return self.client


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
        await self.dispatch("你好", peer)
        await self.dispatch("你好", peer)
        self.platform.send_by_session.assert_awaited_once()
        self.assertEqual(self.sent_text(peer), "陌生人提示")
        self.clock.advance(timedelta(seconds=60))
        await self.dispatch("你好", peer)
        self.assertEqual(self.platform.send_by_session.await_count, 2)

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
        await self.dispatch("你好")
        await self.dispatch("你好")
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
        self.platform.client.is_shutting_down = True
        with self.assertRaises(TaskError) as refused:
            route(event)
        self.assertEqual(refused.exception.code, "host_account_offline")

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
