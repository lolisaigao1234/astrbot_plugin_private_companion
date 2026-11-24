"""Telegram group management against the installed adapter, with a synthetic bot."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from tests.test_telegram_platform import OWNER, TelegramPlatform, group_event


class TelegramAdministrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_standing_is_chat_scoped_cached_and_fails_closed(self):
        from astrbot_host.group_standing import resolve_group_standing

        platform = TelegramPlatform()
        bot = platform.client
        bot.get_chat_member = AsyncMock()
        owner = NS(user=NS(id=OWNER), status="creator")
        admin = NS(user=NS(id=OWNER), status="administrator")
        member = NS(user=NS(id=OWNER), status="member")
        bot.get_chat_member.side_effect = [owner, admin, member, RuntimeError()]
        cache = {}
        with patch("astrbot_host.group_standing.monotonic", return_value=1):
            for topic in (22, 23):
                event = await group_event(platform, -100400, "supergroup", topic)
                self.assertEqual((await resolve_group_standing(event, cache))["role"], "owner")
            bot.get_chat_member.assert_awaited_once_with(chat_id=-100400, user_id=OWNER)
            other = await group_event(platform)
            self.assertEqual((await resolve_group_standing(other, cache))["role"], "admin")
        with patch("astrbot_host.group_standing.monotonic", return_value=100):
            self.assertEqual((await resolve_group_standing(event, cache))["role"], "member")
            self.assertIsNone(await resolve_group_standing(other, cache))

    async def test_relays_resolve_only_configured_windows_and_keep_confirmation(self):
        import json
        from datetime import timedelta
        from tests.test_relay_tools import RelayToolPortTests, NOW, SETTINGS
        from companion_v2.conversation.relay_tools import SEND_ACTION
        from companion_v2.execution.models import TaskError

        fixture = RelayToolPortTests()
        fixture.addCleanup = self.addCleanup
        await fixture.asyncSetUp()
        port = fixture.port
        platform = TelegramPlatform()
        platform.client.send_message = AsyncMock()
        platform.client.send_chat_action = AsyncMock()
        port.bridge.platform_name = lambda scope: "telegram"
        port.bridge.platform = lambda scope: platform
        port.bridge.client = lambda scope: platform.client
        port.bridge.connections = NS(acquire=lambda key: NS(account_key=key, client=platform.client, platform=platform),
                                     assert_current=lambda _: None)

        def window(name, **fields):
            return NS(target_id=name, platform_id="onebot", account_id="account-a", bot_id="bot-a", **fields)
        port.bridge.settings = NS(
            targets=[window("person-a", participant_id="2002"), window("person-b", participant_id="2003")],
            group_targets=[window("group-a", group_id="-1004#22")])
        port.window_names = lambda kind: {("onebot", "account-a", "2002"): "家人甲",
                                          ("onebot", "account-a", "2003"): "家人乙",
                                          ("onebot", "account-a", "-1004#22"): "读书小组"}
        port.application.relay_settings.read = lambda *_: {**SETTINGS, "allowed_groups": ["-1004#22"]}

        async def invoke(action, arguments):
            invocation = fixture.invocation(action, arguments, action)
            return await port.invoke("relay:" + action, invocation)

        for hint, code in (("家人", "relay_target_ambiguous"), ("9999", "relay_target_not_found")):
            with self.assertRaises(TaskError) as refused:
                await invoke("relay_message", dict(destination_id=hint, text="你好"))
            self.assertEqual(refused.exception.code, code)
        for kind, hint, chat, topic in (("private", "家人甲", "2002", None),
                                        ("group", "读书小组", "-1004", 22)):
            args = dict(destination_kind=kind, destination_id=hint, text="你好")
            if kind == "group":
                args["mention"] = "家人甲"
            fixture.current_input = dict(event_id="ask-" + kind, observed_at=NOW.isoformat())
            proposal = json.loads((await invoke("relay_message", args)).source.payload)
            self.assertEqual(proposal["state"], "awaiting_confirmation")
            args.update(confirmed=True, operation_id=proposal["operation_id"])
            self.assertEqual((await invoke(SEND_ACTION, args)).error_code, "relay_confirmation_required")
            fixture.current_input = dict(event_id="confirm-" + kind, observed_at=(NOW + timedelta(seconds=1)).isoformat())
            fixture.current_text = "确认发送"
            for _ in range(2):
                self.assertEqual((await invoke(SEND_ACTION, args)).state, "succeeded")
            kwargs = platform.client.send_message.await_args.kwargs
            self.assertEqual((kwargs["chat_id"], kwargs.get("message_thread_id")), (chat, topic))
            if kind == "group":
                self.assertEqual(kwargs["entities"][0].url, "tg://user?id=2002")
            platform.client.send_message.assert_awaited_once()
            platform.client.send_message.reset_mock()

    async def test_pending_receipts_do_not_cross_platform_or_bot_accounts(self):
        from time import time
        from astrbot_host.relay_pending import add_task, claim_private_receipts
        from tests.test_relay_pending_events import RelayPendingEventTests

        fixture = RelayPendingEventTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        task = dict(kind="private_receipt", state="waiting_reply", target_user_id=str(OWNER),
                    expires_at=time() + 60)
        add_task(fixture.runtime, {**task, "telegram_account": ["tg-main", "tg-main"]})
        self.assertEqual(claim_private_receipts(fixture.runtime, str(OWNER)), [])
        self.assertEqual(claim_private_receipts(fixture.runtime, str(OWNER), telegram_account=["tg-other", "tg-other"]), [])
        self.assertEqual(len(claim_private_receipts(fixture.runtime, str(OWNER),
                                                    telegram_account=["tg-main", "tg-main"])), 1)
