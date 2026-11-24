"""Telegram group management against the installed adapter, with a synthetic bot."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx

from tests.test_telegram_platform import OWNER, TelegramPlatform, group_event


class TelegramAdministrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_standing_and_mentioned_relay_redact_httpx_requests(self):
        from astrbot_host.group_standing import resolve_group_standing
        from astrbot_host.private_notice import send_group_notice

        platform = TelegramPlatform()
        bot = platform.client
        event = await group_event(platform)
        route = NS(peer="-400", platform=platform, client=bot, platform_name="telegram",
                   platform_id="tg-main", login="tg-main")
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as http:
            async def lookup(*, chat_id, user_id):
                await http.post(f"https://telegram.invalid/bot{bot.token}/getChatMember")
                return NS(user=NS(id=user_id), status="member")

            async def send(**kwargs):
                await http.post(f"https://telegram.invalid/bot{bot.token}/sendMessage")

            bot.get_chat_member = lookup
            bot.send_message = send
            with self.assertLogs("httpx", "INFO") as logs:
                standing = await resolve_group_standing(event, {})
                receipt = await send_group_notice(None, route, text="Hello", mention_id=str(OWNER))

        self.assertEqual(standing["role"], "member")
        self.assertEqual(receipt["state"], "delivered")
        lookup_record, send_record = logs.records
        self.assertIn("/bot[redacted]/getChatMember", lookup_record.getMessage())
        self.assertIn("/bot[redacted]/sendMessage", send_record.getMessage())
        for record in logs.records:
            self.assertNotIn(bot.token, repr(vars(record)))

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
            self.assertIsNone(await resolve_group_standing(other, cache))
            bot.get_chat_member.side_effect = [admin]
            self.assertEqual((await resolve_group_standing(other, cache, fresh=True))["role"], "admin")

    async def relay_fixture(self):
        from tests.test_relay_tools import RelayToolPortTests, SETTINGS

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

        return fixture, platform, invoke

    async def test_relays_resolve_only_configured_windows_and_keep_confirmation(self):
        import json
        from datetime import timedelta
        from tests.test_relay_tools import NOW
        from companion_v2.conversation.relay_tools import SEND_ACTION
        from companion_v2.execution.models import TaskError

        fixture, platform, invoke = await self.relay_fixture()
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

    async def test_immediate_relay_replays_definite_refusal_and_ambiguous_timeout(self):
        import json
        from datetime import timedelta
        from telegram.error import Forbidden
        from tests.test_relay_tools import NOW
        from companion_v2.conversation.relay_tools import SEND_ACTION

        fixture, platform, invoke = await self.relay_fixture()
        fixture.current_text = "确认发送"
        for kind, peer in (("private", "2002"), ("group", "-1004#22")):
            for error, outcome, code in ((Forbidden("Bot was blocked by the user"), "failed", "telegram_bot_blocked"),
                                         (TimeoutError(), "unknown", "relay_delivery_unknown")):
                with self.subTest(kind=kind, outcome=outcome):
                    args = dict(destination_kind=kind, destination_id=peer, text=outcome)
                    if kind == "group":
                        args["mention"] = "2002"
                    request = dict(event_id="ask", observed_at=NOW.isoformat())
                    fixture.current_input = request
                    proposal = json.loads((await invoke("relay_message", args)).source.payload)
                    send_args = dict(args, operation_id=proposal["operation_id"], confirmed=True)
                    fixture.current_input = dict(event_id="confirm", observed_at=(NOW + timedelta(seconds=1)).isoformat())
                    platform.client.send_message.reset_mock()
                    platform.client.send_message.side_effect = error
                    for _ in range(2):
                        result = await invoke(SEND_ACTION, send_args)
                        self.assertEqual((result.state, result.error_code), (outcome, code))
                    fixture.current_input = request
                    proposed = await invoke("relay_message", args)
                    self.assertEqual(json.loads(proposed.source.payload)["state"], outcome)
                    platform.client.send_message.assert_awaited_once()

    async def test_private_relay_records_native_question_id_once(self):
        import json
        from datetime import timedelta
        from astrbot_host.relay_pending import _read
        from companion_v2.conversation.relay_tools import SEND_ACTION
        from tests.test_relay_tools import NOW

        fixture, platform, invoke = await self.relay_fixture()
        fixture.current_text = "确认发送"
        args = dict(destination_kind="private", destination_id="2002", text="明天可以吗？", need_receipt=True)
        fixture.current_input = dict(event_id="ask", observed_at=NOW.isoformat())
        proposal = json.loads((await invoke("relay_message", args)).source.payload)
        fixture.current_input = dict(event_id="confirm", observed_at=(NOW + timedelta(seconds=1)).isoformat())
        send_args = dict(args, confirmed=True, operation_id=proposal["operation_id"])
        platform.client.send_message.return_value = NS(message_id=102)
        result = await invoke(SEND_ACTION, send_args)
        self.assertEqual(json.loads(result.source.payload)["state"], "sent")
        await invoke(SEND_ACTION, send_args)
        question, = _read(fixture.runtime)["tasks"].values()
        self.assertEqual(question["question_message_id"], 102)
        platform.client.send_message.assert_awaited_once()

    async def test_delayed_relay_tool_replays_the_delivery_outcome(self):
        import json
        from datetime import timedelta
        from telegram.error import Forbidden
        from tests.test_relay_tools import NOW
        from companion_v2.conversation.relay_tools import SEND_ACTION
        from astrbot_host.relay_pending_events import process_group_presence

        fixture, platform, invoke = await self.relay_fixture()
        fixture.current_text = "确认发送"
        target = NS(group_id="-1004#22", platform_id="onebot", account_id="account-a")
        event = await group_event(platform, -1004, "supergroup", 22)
        context = NS(actor=NS(principal=NS(native_account_id="2002")))
        route = NS(peer=target.group_id, platform=platform, client=platform.client,
                   platform_name="telegram", platform_id="onebot", login="account-a")
        for error, expected in ((None, "already_sent"), (Forbidden("blocked"), "failed"),
                                (TimeoutError(), "unknown")):
            with self.subTest(expected=expected):
                args = dict(destination_kind="group", destination_id=target.group_id,
                            text=expected, mention="2002", delay_until_seen=True)
                request = dict(event_id="request", observed_at=NOW.isoformat())
                fixture.current_input = request
                proposal = json.loads((await invoke("relay_message", args)).source.payload)
                send_args = dict(args, confirmed=True, operation_id=proposal["operation_id"])
                fixture.current_input = dict(event_id="confirm", observed_at=(NOW + timedelta(seconds=1)).isoformat())
                for _ in range(2):
                    replay = await invoke(SEND_ACTION, send_args)
                    self.assertEqual(json.loads(replay.source.payload)["state"], "scheduled")
                platform.client.send_message.side_effect = error
                with patch("astrbot_host.relay_pending_events._route", return_value=route), patch(
                        "astrbot_host.relay_pending_events.prepare_relay_transport", return_value=None):
                    for _ in range(2):
                        await process_group_presence(NS(runtime=fixture.runtime), event, target, context)
                replay = await invoke(SEND_ACTION, send_args)
                self.assertEqual(json.loads(replay.source.payload)["state"] if replay.source else replay.state, expected)
                fixture.current_input = request
                proposed = await invoke("relay_message", args)
                self.assertEqual(json.loads(proposed.source.payload)["state"], expected)
                platform.client.send_message.assert_awaited_once()
                if expected == "unknown":
                    fixture.current_input = dict(event_id="new-request", observed_at=NOW.isoformat())
                    fresh = json.loads((await invoke("relay_message", args)).source.payload)
                    self.assertNotEqual(fresh["operation_id"], send_args["operation_id"])
                    fixture.current_input = dict(event_id="new-confirm", observed_at=(NOW + timedelta(seconds=2)).isoformat())
                    queued = await invoke(SEND_ACTION, dict(args, confirmed=True, operation_id=fresh["operation_id"]))
                    from astrbot_host.relay_pending import scheduled_state
                    task_id = json.loads(queued.source.payload)["task_id"]
                    self.assertEqual(scheduled_state(fixture.runtime, task_id), "scheduled")
                platform.client.send_message.reset_mock()

    async def test_delayed_mention_timeout_stays_unknown_on_replay(self):
        from time import time
        from astrbot_host.relay_pending import _read, add_task
        from astrbot_host.relay_pending_events import process_group_presence
        from tests.test_relay_pending_events import RelayPendingEventTests

        fixture = RelayPendingEventTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        platform = TelegramPlatform()
        event = await group_event(platform)
        route = NS(peer="-400", platform=platform, client=platform.client, platform_name="telegram",
                   platform_id="tg-main", login="tg-main")
        platform.client.send_message = AsyncMock(side_effect=TimeoutError())
        target = NS(group_id=route.peer, platform_id=route.platform_id, account_id=route.login)
        context = NS(actor=NS(principal=NS(native_account_id=str(OWNER))))
        task = dict(kind="group_wait", state="waiting", group_id=route.peer, mention_id=str(OWNER),
                    target_user_id=str(OWNER), text="Hello", expires_at=time()+60, signature="one-mention",
                    telegram_account=["tg-main", "tg-main"])
        task_id = add_task(fixture.runtime, task)
        with patch("astrbot_host.relay_pending_events._route", return_value=route), patch(
                "astrbot_host.relay_pending_events.prepare_relay_transport", return_value=None):
            await process_group_presence(fixture.controller, event, target, context)
            self.assertEqual(_read(fixture.runtime)["tasks"][task_id]["state"], "unknown")
            self.assertEqual(add_task(fixture.runtime, task), task_id)
            await process_group_presence(fixture.controller, event, target, context)
        platform.client.send_message.assert_awaited_once()

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
