"""Delayed group relay and private receipt event dispatch."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import time
import unittest

from astrbot_host.relay_pending import add_task, claim_private_receipts, claim_telegram_receipt
from astrbot_host.relay_pending_events import process_group_presence, process_private_receipts
from companion_v2.storage.runtime import StorageRuntime


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class FakeReverseApi:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"message_id": 900 + len(self.calls)}


class RelayPendingEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="v2-relay-pending-")
        self.addCleanup(temporary.cleanup)
        self.runtime = StorageRuntime.open(data_root=Path(temporary.name), installation_id="relay-events")
        self.addCleanup(self.runtime.close)
        self.api = FakeReverseApi()
        self.client = SimpleNamespace(_api=SimpleNamespace(_wsr_api=self.api),
                                      _wsr_api_clients={"account": self.api})
        bridge = SimpleNamespace(platforms={}, groups=SimpleNamespace(platforms={}))
        self.controller = SimpleNamespace(
            runtime=self.runtime,
            native_types=SimpleNamespace(reverse_api=FakeReverseApi),
            session=SimpleNamespace(bridge=bridge),
        )
        self.event = SimpleNamespace(bot=self.client, message_str="好的",
                                     platform_meta=SimpleNamespace(name="aiocqhttp"))

    async def test_delayed_group_relay_fires_when_target_speaks(self):
        add_task(self.runtime, {
            "kind": "group_wait", "state": "waiting", "group_id": "5005",
            "target_user_id": "7777", "mention_id": "7777", "text": "看到后回我",
            "source_kind": "private", "source_id": "1001", "source_user": "1001",
            "expires_at": time.time() + 3600, "signature": "delayed-1",
        })
        target = SimpleNamespace(group_id="5005", platform_id="onebot", account_id="account")
        context = SimpleNamespace(actor=SimpleNamespace(principal=SimpleNamespace(native_account_id="7777")))
        await process_group_presence(self.controller, self.event, target, context)
        action, kwargs = self.api.calls[0]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(kwargs["group_id"], 5005)
        self.assertEqual(kwargs["message"][0], {"type": "at", "data": {"qq": "7777"}})
        self.assertEqual(kwargs["message"][-1]["data"]["text"], "看到后回我")

    def test_pruned_telegram_delivery_keeps_its_original_outcome(self):
        from astrbot_host.relay_pending import finish, scheduled_state
        from unittest.mock import patch
        for state, expected in (("sent", "already_sent"), ("failed", "failed"), ("unknown", "unknown")):
            with self.subTest(state=state):
                task = add_task(self.runtime, {"kind": "group_wait", "state": "waiting",
                    "expires_at": time.time()+10, "telegram_account": ["telegram", "main"]})
                finish(self.runtime, task, state)
                with patch("astrbot_host.relay_pending.time", return_value=time.time()+20):
                    add_task(self.runtime, {"kind": "private_receipt", "state": "waiting_reply"})
                self.assertEqual(scheduled_state(self.runtime, task), expected)

    def test_telegram_receipt_claims_distinct_out_of_order_messages_once(self):
        account = ["telegram", "main"]
        task = {"kind": "private_receipt", "state": "waiting_permission", "target_user_id": "peer",
                "telegram_account": account, "expires_at": time.time() + 60}
        for message_id in (127, 63, 126, 64):
            task_id = add_task(self.runtime, {**task, "prompt_message_id": message_id - 2})
            permission, replies = claim_telegram_receipt(self.runtime, account, "peer", message_id, reply_to=message_id - 2, permission_answer=True)
            self.assertEqual([record["id"] for record in permission], [task_id])
            self.assertEqual(replies, [])
            for seen in (127, message_id):
                self.assertEqual(claim_telegram_receipt(self.runtime, account, "peer", seen, reply_to=seen - 2, permission_answer=True), ([], []))

    def test_addressed_answer_cannot_be_claimed_by_a_legacy_question(self):
        from astrbot_host.relay_pending import _read

        account = ["telegram", "main"]
        ids = []
        for topic in ("-400#22", "-400#23"):
            ids.append(add_task(self.runtime, {
                "kind": "private_receipt", "state": "waiting_reply", "source_kind": "group",
                "source_id": topic, "target_user_id": "peer", "telegram_account": account,
                "confirm_before_report": False, "expires_at": time.time() + 60,
            }))
        self.assertEqual(claim_telegram_receipt(self.runtime, account, "peer", 99, reply_to=75), ([], []))
        tasks = _read(self.runtime)["tasks"]
        self.assertEqual([tasks[key]["state"] for key in ids], ["waiting_reply", "waiting_reply"])

    def test_telegram_receipt_keeps_legacy_replay_boundary(self):
        account = ["telegram", "main"]
        key = "conversation.relay.private_seen:" + json.dumps([*account, "peer"], separators=(",", ":"))
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (key, "100"))
        task = {"kind": "private_receipt", "state": "waiting_reply", "target_user_id": "peer",
                "telegram_account": account, "expires_at": time.time() + 60}
        task_id = add_task(self.runtime, task)
        for message_id in (99, 100):
            self.assertEqual(claim_telegram_receipt(self.runtime, account, "peer", message_id), ([], []))
        _, replies = claim_telegram_receipt(self.runtime, account, "peer", 102)
        self.assertEqual([record["id"] for record in replies], [task_id])
        second = add_task(self.runtime, task)
        _, replies = claim_telegram_receipt(self.runtime, account, "peer", 101)
        self.assertEqual([record["id"] for record in replies], [second])

    async def test_private_receipt_reports_next_reply_to_source(self):
        add_task(self.runtime, {
            "kind": "private_receipt", "state": "waiting_reply", "target_user_id": "2002",
            "source_kind": "private", "source_id": "1001", "source_user": "1001",
            "question": "今晚来吗", "sent_text": "今晚来吗", "confirm_before_report": False,
            "expires_at": time.time() + 3600, "signature": "receipt-1",
        })
        target = SimpleNamespace(participant_id="2002", platform_id="onebot", account_id="account")
        await process_private_receipts(self.controller, self.event, target)
        action, kwargs = self.api.calls[0]
        self.assertEqual(action, "send_private_msg")
        self.assertEqual(kwargs["user_id"], 1001)
        self.assertIn("今晚来吗", kwargs["message"][0]["data"]["text"])
        self.assertIn("好的", kwargs["message"][0]["data"]["text"])

    async def test_qq_cancelled_permission_prompt_can_resume(self):
        from asyncio import CancelledError
        from unittest.mock import patch
        from astrbot_host.relay_pending import _read

        task = add_task(self.runtime, {
            "kind": "private_receipt", "state": "waiting_reply", "target_user_id": "2002",
            "confirm_before_report": True, "expires_at": time.time() + 60,
        })
        target = SimpleNamespace(participant_id="2002", platform_id="onebot", account_id="account")
        with patch("astrbot_host.relay_pending_events._ask_permission", side_effect=CancelledError):
            with self.assertRaises(CancelledError):
                await process_private_receipts(self.controller, self.event, target)
        self.assertEqual(_read(self.runtime)["tasks"][task]["state"], "waiting_reply")

    async def test_qq_explicit_refusal_never_reports_the_saved_answer(self):
        from astrbot_host.relay_pending import _read

        target = SimpleNamespace(participant_id="2002", platform_id="onebot", account_id="account")
        for refusal in ("不同意", "我不同意转告", "不好"):
            with self.subTest(refusal=refusal):
                task = add_task(self.runtime, {
                    "kind": "private_receipt", "state": "waiting_permission", "target_user_id": "2002",
                    "source_id": "1001", "reply_text": "private answer", "expires_at": time.time() + 60,
                })
                self.event.message_str = refusal
                await process_private_receipts(self.controller, self.event, target)
                self.assertEqual(_read(self.runtime)["tasks"][task]["state"], "declined")
                self.assertEqual(self.api.calls, [])

    async def test_cancelled_qq_receipt_reports_keep_failed_state(self):
        from asyncio import CancelledError
        from unittest.mock import patch
        from astrbot_host.relay_pending import _read

        target = SimpleNamespace(participant_id="2002", platform_id="onebot", account_id="account")
        for state in ("waiting_reply", "waiting_permission"):
            with self.subTest(state=state):
                task = add_task(self.runtime, {
                    "kind": "private_receipt", "state": state, "target_user_id": "2002",
                    "confirm_before_report": state == "waiting_permission", "expires_at": time.time() + 60,
                })
                with patch("astrbot_host.relay_pending_events._report_receipt", side_effect=CancelledError):
                    with self.assertRaises(CancelledError):
                        await process_private_receipts(self.controller, self.event, target)
                self.assertEqual(_read(self.runtime)["tasks"][task]["state"], "failed")

    async def test_receipt_permission_is_requested_before_reporting(self):
        add_task(self.runtime, {
            "kind": "private_receipt", "state": "waiting_reply", "target_user_id": "2002",
            "source_kind": "private", "source_id": "1001", "source_user": "1001",
            "question": "方便吗", "sent_text": "方便吗", "confirm_before_report": True,
            "expires_at": time.time() + 3600, "signature": "receipt-2",
        })
        target = SimpleNamespace(participant_id="2002", platform_id="onebot", account_id="account")
        await process_private_receipts(self.controller, self.event, target)
        self.assertEqual(self.api.calls[0][0], "send_private_msg")
        self.assertEqual(self.api.calls[0][1]["user_id"], 2002)
        self.assertIn("可以", self.api.calls[0][1]["message"][0]["data"]["text"])

        self.event.message_str = "不好意思，可以"
        await process_private_receipts(self.controller, self.event, target)
        self.assertEqual(self.api.calls[1][0], "send_private_msg")
        self.assertEqual(self.api.calls[1][1]["user_id"], 1001)
        self.assertIn("方便吗", self.api.calls[1][1]["message"][0]["data"]["text"])


    async def test_expired_group_relay_notifies_source_once(self):
        add_task(self.runtime, {
            "kind": "group_wait", "state": "waiting", "group_id": "5005",
            "target_user_id": "7777", "mention_id": "7777", "text": "看到后回我",
            "source_kind": "private", "source_id": "1001", "source_user": "1001",
            "expires_at": time.time() - 1, "signature": "expired-1",
        })
        target = SimpleNamespace(group_id="5005", platform_id="onebot", account_id="account")
        context = SimpleNamespace(actor=SimpleNamespace(principal=SimpleNamespace(native_account_id="9999")))
        await process_group_presence(self.controller, self.event, target, context)
        self.assertEqual(self.api.calls[0][0], "send_private_msg")
        self.assertEqual(self.api.calls[0][1]["user_id"], 1001)
        self.assertIn("撤回", self.api.calls[0][1]["message"][0]["data"]["text"])
        # 回报一次后任务已被取走，后续同群消息不会重复通知。
        await process_group_presence(self.controller, self.event, target, context)
        self.assertEqual(len(self.api.calls), 1)

if __name__ == "__main__":
    unittest.main()
