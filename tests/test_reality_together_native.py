"""Contract checks for V2's bundled Reality and Together ports."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from companion_v2.capabilities.native import parse_manifest, parse_result
from companion_v2.execution.models import SideEffect
from astrbot_host.native_extensions import together_execution_binding
from companion_v2.conversation.voice_reply import VoiceReplyPolicy
from companion_v2.product import PRODUCT_VERSION
from companion_v2.reality import provider as REALITY
from companion_v2.together import provider as TOGETHER
from tests.native_internal_support import internal_registry


def context(*, bot="bot-a", role="role-a", user="12345", policy=1):
    return {"actor": {"subject": {"subject_id": "onebot-user:" + user},
                      "principal": {"native_account_id": user}},
            "audience": {"scope": {"installation_id": "install-a", "bot_id": bot,
                                   "account_id": bot + "-account", "platform": "aiocqhttp",
                                   "kind": "private", "session_id": user}},
            "role_binding": {"role_id": role}, "policy_revision": policy}


def request(provider, action, *, invocation, arguments=None, identity=None):
    resolved = identity or context()
    value = {"schema": "companion.native-provider.v1", "invocation_id": invocation,
            "parent_task_id": "task-a", "tool_call_id": "call-a", "action": action,
            "origin_key": "origin-a", "context": resolved,
            "binding": {"capability": action, "provider": provider.manifest()["provider_id"],
                        "version": provider.version, "generation": provider.generation},
            "arguments": arguments or {}, "arguments_hash": "arguments-a",
            "deadline_at": "2026-09-27T00:00:00+00:00"}
    if action == "together.room.create":
        scope = resolved["audience"]["scope"]
        value["host_persona"] = {"persona_id": resolved["role_binding"]["role_id"],
                                 "prompt": "V2 人格专用提示词", "bot_id": scope["bot_id"],
                                 "account_id": scope["account_id"]}
        value["host_execution"] = {"model_provider_id": "v2-chat", "tts_provider_id": "v2-voice",
                                    "tts_language": "ja", "tts_timeout_seconds": 42}
    return value


class FakeReality:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.confirmed = True
        self.users = []
        self.mihome_web_api = self

    def mobile_context(self, user_id):
        self.users.append(user_id)
        return {"available": True, "user_id": user_id}

    def extension_control_preflight(self, alias, *, native=False, actor=None):
        if not native:
            raise ValueError("native control required")
        return "test-did"

    async def extension_control_power(self, alias, is_on, *, native=False, actor=None):
        self.extension_control_preflight(alias, native=native, actor=actor)
        self.calls += 1
        if self.fail:
            raise TimeoutError("unconfirmed")
        return {"alias": alias, "is_on": is_on, "confirmed": self.confirmed}

    def extension_device_target(self, alias, *, native=False, actor=None):
        return {"alias": alias, "binding_revision": "rev-1"}

    def extension_device_profile(self, alias):
        return {"alias": alias, "model": "lumi.acpartner.mcn02", "category": "空调类别",
                "direct_control_supported": True,
                "writable_properties": [{"key": "target_temperature", "name": "温度", "examples": ["26"], "hint": ""}],
                "readable_properties": [{"key": "electric_power", "name": "电功率"}],
                "actions": [], "unsupported_reason": ""}


class FakeTickets:
    def __init__(self):
        self.tickets = {}

    def get(self, token):
        return self.tickets.get(token)

    def revoke(self, token):
        self.tickets.pop(token, None)


class FakeTogether:
    def __init__(self):
        self.ticket_store = FakeTickets()
        self.room_server = SimpleNamespace(running=True)
        self.rooms = {}
        self.detached_rooms = {}
        self.issued = 0
        self.closed = []

    def _get_provider_by_id(self, provider_id):
        return SimpleNamespace(text_chat=lambda **kwargs: None) if provider_id == "v2-chat" else None

    def issue_room_ticket(self, *, mode, user_id, native_v2=None):
        self.issued += 1
        ticket = SimpleNamespace(token=f"ticket-{self.issued}", mode=mode,
                                 user_id=user_id, created_at=float(self.issued), expires_at=1000.0,
                                 native_v2=native_v2)
        self.ticket_store.tickets[ticket.token] = ticket
        return ticket

    def _ticket_url(self, ticket):
        return f"https://example.invalid/join/{ticket.token}"

    async def close_room(self, room):
        room.integration_closed = True
        self.closed.append(room.room_id)
        self.ticket_store.revoke(room.ticket_token)
        self.rooms.pop(room.room_id, None)
        self.detached_rooms.pop(room.room_id, None)


class NativeRealityTogetherTests(unittest.IsolatedAsyncioTestCase):
    def test_together_execution_uses_v2_model_and_language_voice(self):
        settings = SimpleNamespace(provider_id="v2-chat", voice_reply_policy=VoiceReplyPolicy(
            language="ja", provider_id="default-voice", ja_provider_id="v2-voice", timeout_seconds=42))
        self.assertEqual(together_execution_binding(settings), {
            "model_provider_id": "v2-chat", "tts_provider_id": "v2-voice",
            "tts_language": "ja", "tts_timeout_seconds": 42})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def check_receipt(self, provider, req, result, effect):
        parsed = parse_result(result, request=req, side_effect=effect)
        self.assertEqual(parsed.state, result["state"])

    async def test_device_inspect_is_a_read_only_profile_receipt(self):
        reality = REALITY.NativeRealityProvider(FakeReality(), self.root / "reality", version=PRODUCT_VERSION)
        req = request(reality, "reality.device.inspect", invocation="inspect",
                      arguments={"alias": "客厅空调"})
        result = await reality.execute(req, before_dispatch=lambda: True)
        self.check_receipt(reality, req, result, SideEffect.NONE)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["alias"], "客厅空调")
        self.assertEqual(result["result"]["writable_properties"][0]["name"], "温度")
        self.assertIsNone(result["operation_id"])

    async def test_device_inspect_requires_an_alias(self):
        reality = REALITY.NativeRealityProvider(FakeReality(), self.root / "reality", version=PRODUCT_VERSION)
        req = request(reality, "reality.device.inspect", invocation="inspect")
        with self.assertRaises(REALITY.NativeRealityError):
            await reality.execute(req, before_dispatch=lambda: True)

    async def test_manifest_and_read_receipts(self):
        reality_plugin = FakeReality()
        reality = REALITY.NativeRealityProvider(reality_plugin, self.root / "reality", version=PRODUCT_VERSION)
        together = TOGETHER.NativeTogetherProvider(FakeTogether(), self.root / "together", version=PRODUCT_VERSION)
        for module, provider in ((REALITY, reality), (TOGETHER, together)):
            manifest = parse_manifest(provider.manifest(), plugin_id=module.PLUGIN_ID, version=PRODUCT_VERSION)
            self.assertEqual(len(manifest.tools), len(module._ACTIONS))
        for provider, action in ((reality, "reality.snapshot.read"), (together, "together.room.snapshot")):
            req = request(provider, action, invocation=action)
            result = await provider.execute(req, before_dispatch=lambda: True)
            self.check_receipt(provider, req, result, SideEffect.NONE)
            self.assertIsNone(result["operation_id"])
            again = await provider.lookup(req, before_dispatch=lambda: True)
            self.assertEqual(again, result)
        self.assertEqual(reality_plugin.users, ["12345"])

    async def test_registry_uses_exact_bundled_ports_and_rejects_closed_lease(self):
        reality = REALITY.NativeRealityProvider(FakeReality(), self.root / "reality", version=PRODUCT_VERSION)
        together = TOGETHER.NativeTogetherProvider(FakeTogether(), self.root / "together", version=PRODUCT_VERSION)

        registry = internal_registry(reality, together)
        self.addAsyncCleanup(registry.aclose)
        self.assertEqual(registry.issues, ())
        # Keep the published surface pinned: discovery silently drops every
        # native declaration once the aggregate passes MAX_TOOL_CATALOG, so a
        # growing action face must be an explicit decision, not drift.
        self.assertEqual(REALITY._ACTIONS, {
            "reality.snapshot.read", "reality.camera.observe", "reality.device.list",
            "reality.device.status", "reality.device.inspect", "reality.device.control",
            "reality.device.action",
            "reality.operation.lookup", "reality.operation.cancel",
        })
        self.assertEqual(TOGETHER._ACTIONS, {
            "together.room.snapshot", "together.room.create", "together.room.close",
            "together.room.members", "together.room.invite", "together.room.invitations",
            "together.room.join",
        })
        self.assertEqual(len(registry.declarations()),
                         len(REALITY._ACTIONS) + len(TOGETHER._ACTIONS))
        tool_id = f"native:{REALITY.PLUGIN_ID}:reality.snapshot.read"
        self.assertIsNotNone(registry.current_declaration(tool_id))
        await registry._bundled_extensions[0].aclose()
        self.assertIsNone(registry.current_declaration(tool_id))
        self.assertIsNotNone(registry.current_declaration(
            f"native:{TOGETHER.PLUGIN_ID}:together.room.snapshot"))

    async def test_scope_isolation_and_original_invocation(self):
        for module, plugin, provider_class, action in (
            (REALITY, FakeReality(), REALITY.NativeRealityProvider, "reality.snapshot.read"),
            (TOGETHER, FakeTogether(), TOGETHER.NativeTogetherProvider, "together.room.snapshot"),
        ):
            provider = provider_class(plugin, self.root / module.PROVIDER_ID)
            original = request(provider, action, invocation=module.PROVIDER_ID)
            result = await provider.execute(original, before_dispatch=lambda: True)
            changed_policy = copy.deepcopy(original)
            changed_policy["context"]["policy_revision"] = 2
            self.assertEqual(await provider.lookup(changed_policy, before_dispatch=lambda: True), result)
            for changed in (dict(bot="bot-b"), dict(role="role-b"), dict(user="67890")):
                different = copy.deepcopy(original)
                different["context"] = context(**changed)
                with self.assertRaisesRegex(module.__dict__["Native" + module.PROVIDER_ID.split(".")[0].title() + "Error"], "native_original_invocation_mismatch"):
                    await provider.execute(different, before_dispatch=lambda: True)
                with self.assertRaisesRegex(module.__dict__["Native" + module.PROVIDER_ID.split(".")[0].title() + "Error"], "native_operation_not_found"):
                    await provider.lookup({**different, "operation_id": result["receipt_ref"].split(":", 1)[1]},
                                          before_dispatch=lambda: True)

    async def test_reality_device_receipts_and_no_retry(self):
        plugin = FakeReality()
        provider = REALITY.NativeRealityProvider(plugin, self.root, version=PRODUCT_VERSION)
        req = request(provider, "reality.device.control", invocation="device-a",
                      arguments={"alias": "desk", "is_on": True})
        result = await provider.execute(req, before_dispatch=lambda: True)
        self.check_receipt(provider, req, result, SideEffect.EXTERNAL_NETWORK)
        self.assertEqual(result["effect"], "committed")
        self.assertEqual(plugin.calls, 1)
        restarted = REALITY.NativeRealityProvider(plugin, self.root, version=PRODUCT_VERSION)
        self.assertEqual(await restarted.lookup(req, before_dispatch=lambda: True), result)
        self.assertEqual(await restarted.execute(req, before_dispatch=lambda: True), result)
        self.assertEqual(plugin.calls, 1)
        plugin.fail = True
        failing = request(provider, "reality.device.control", invocation="device-b",
                          arguments={"alias": "desk", "is_on": False})
        uncertain = await provider.execute(failing, before_dispatch=lambda: True)
        self.check_receipt(provider, failing, uncertain, SideEffect.EXTERNAL_NETWORK)
        self.assertEqual(uncertain["effect"], "unknown")
        self.assertEqual(await provider.execute(failing, before_dispatch=lambda: True), uncertain)
        self.assertEqual(plugin.calls, 2)
        plugin.fail = False
        plugin.confirmed = False
        unconfirmed = request(provider, "reality.device.control", invocation="device-unconfirmed",
                              arguments={"alias": "desk", "is_on": False})
        receipt = await provider.execute(unconfirmed, before_dispatch=lambda: True)
        self.check_receipt(provider, unconfirmed, receipt, SideEffect.EXTERNAL_NETWORK)
        self.assertEqual((receipt["state"], receipt["effect"]), ("unknown", "unknown"))
        self.assertEqual(plugin.calls, 3)
        calls = iter((True, False))
        revoked = request(provider, "reality.device.control", invocation="device-c",
                          arguments={"alias": "desk", "is_on": True})
        denied = await provider.execute(revoked, before_dispatch=lambda: next(calls))
        self.check_receipt(provider, revoked, denied, SideEffect.EXTERNAL_NETWORK)
        self.assertEqual((denied["state"], denied["effect"]), ("failed", "not_committed"))
        self.assertEqual(plugin.calls, 3)

    async def test_together_room_scope_close_order_and_restart(self):
        plugin = FakeTogether()
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        creations = []
        for number in (1, 2):
            req = request(provider, "together.room.create", invocation=f"create-{number}",
                          arguments={"mode": "call"})
            result = await provider.execute(req, before_dispatch=lambda: True)
            self.check_receipt(provider, req, result, SideEffect.LOCAL)
            self.assertEqual(await provider.execute(req, before_dispatch=lambda: True), result)
            self.assertEqual(result["result"]["room"]["user_id"], "12345")
            token = result["result"]["room"]["ticket_token"]
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["prompt"],
                             "V2 人格专用提示词")
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["scope_key"],
                             TOGETHER.scope_key(req["context"]))
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["model_provider_id"], "v2-chat")
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["tts_provider_id"], "v2-voice")
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["tts_language"], "ja")
            self.assertEqual(plugin.ticket_store.tickets[token].native_v2["session_id"], "12345")
            room = SimpleNamespace(room_id=f"room-{number}", ticket_token=token, user_id="12345",
                                   integration_closed=False, mode="call", created_at=float(number),
                                   call_active=False, resume_token="")
            plugin.rooms[room.room_id] = room
            creations.append(req)
        plugin.rooms["foreign"] = SimpleNamespace(room_id="foreign", ticket_token="foreign-token",
            user_id="12345", integration_closed=False, mode="call", created_at=3.0,
            call_active=False, resume_token="")
        snap = request(provider, "together.room.snapshot", invocation="snapshot-a")
        first = await provider.execute(snap, before_dispatch=lambda: True)
        self.assertEqual(first["result"]["active_count"], 2)
        other = request(provider, "together.room.snapshot", invocation="snapshot-other",
                        identity=context(role="role-b"))
        self.assertEqual((await provider.execute(other, before_dispatch=lambda: True))["result"]["rooms"], [])
        for number in (1, 2):
            close = request(provider, "together.room.close", invocation=f"close-{number}")
            result = await provider.execute(close, before_dispatch=lambda: True)
            self.check_receipt(provider, close, result, SideEffect.LOCAL)
            self.assertNotIn("closed_ticket_token", result["result"])
            self.assertEqual(result["result"]["room_id"], f"room-{3-number}")
        self.assertEqual(plugin.closed, ["room-2", "room-1"])
        self.assertEqual(plugin.issued, 2)
        self.assertNotIn("V2 人格专用提示词", (self.root / "native_v2" / "operations.json").read_text(encoding="utf-8"))
        restarted = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        self.assertEqual((await restarted.lookup(creations[0], before_dispatch=lambda: True))["state"], "succeeded")
        snap2 = request(restarted, "together.room.snapshot", invocation="snapshot-after")
        self.assertEqual((await restarted.execute(snap2, before_dispatch=lambda: True))["result"]["rooms"], [])

    async def test_group_invite_replay_after_restart_reports_spent_ticket(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        plugin = FakeTogether()
        plugin.telegram_group_invite_route = Mock(return_value=object())
        plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "delivered"})
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        req = request(provider, "together.room.create", invocation="delivered-group", identity=identity,
                      arguments={"mode": "call"})
        original = await provider.execute(req, before_dispatch=lambda: True)
        self.assertTrue(original["result"]["delivered_privately"])
        self.assertIn("重启", original["result"]["message"])
        self.assertTrue((await provider.lookup(req, before_dispatch=lambda: True))["result"]["ticket_available"])
        plugin.ticket_store = FakeTickets()
        restarted = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        for reader in (restarted.execute, restarted.lookup):
            replay = await reader(req, before_dispatch=lambda: True)
            self.assertTrue(replay["result"]["delivered_privately"])
            self.assertFalse(replay["result"]["ticket_available"])
            self.assertNotIn("ticket-1", json.dumps(replay))
        plugin.send_telegram_group_invite.assert_awaited_once()
        self.assertEqual(plugin.issued, 1)

    async def test_close_revokes_group_ticket_after_uncertain_delivery(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        plugin = FakeTogether()
        plugin.telegram_group_invite_route = Mock(return_value=object())
        plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "unknown"})
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        create = request(provider, "together.room.create", invocation="maybe-delivered", identity=identity,
                         arguments={"mode": "call"})
        first = await provider.execute(create, before_dispatch=lambda: True)
        self.assertEqual(first["state"], "unknown")
        token, = plugin.ticket_store.tickets
        for field, value in (("installation_id", "other-install"), ("bot_id", "other-bot"),
                             ("account_id", "other-account"), ("session_id", "-100#24")):
            other = copy.deepcopy(identity)
            other["audience"]["scope"][field] = value
            denied = request(provider, "together.room.close", invocation="other-" + field, identity=other)
            self.assertEqual((await provider.execute(denied, before_dispatch=lambda: True))["state"], "failed")
            self.assertIsNotNone(plugin.ticket_store.get(token))
        other = copy.deepcopy(identity)
        other["role_binding"]["role_id"] = "other-role"
        denied = request(provider, "together.room.close", invocation="other-role", identity=other)
        self.assertEqual((await provider.execute(denied, before_dispatch=lambda: True))["state"], "failed")
        self.assertIsNotNone(plugin.ticket_store.get(token))
        close = request(provider, "together.room.close", invocation="close-uncertain", identity=identity)
        receipt = await provider.execute(close, before_dispatch=lambda: True)
        self.assertIsNone(plugin.ticket_store.get(token))
        self.assertTrue(receipt["result"]["ticket_revoked"])
        self.assertNotIn(token, json.dumps(receipt))
        self.assertEqual(await provider.execute(create, before_dispatch=lambda: True), first)
        plugin.send_telegram_group_invite.assert_awaited_once()

    async def test_close_orders_uncertain_ticket_with_live_and_detached_rooms(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        for room_first in (True, False):
            for detached in (True, False):
                with self.subTest(room_first=room_first, detached=detached):
                    plugin = FakeTogether()
                    plugin.telegram_group_invite_route = Mock(return_value=object())
                    plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "unknown"})
                    root = self.root / f"{room_first}-{detached}"
                    provider = TOGETHER.NativeTogetherProvider(plugin, root, version=PRODUCT_VERSION)
                    create = request(provider, "together.room.create", invocation="invite",
                                     identity=identity, arguments={"mode": "call"})
                    original = await provider.execute(create, before_dispatch=lambda: True)
                    self.assertEqual(original["state"], "unknown")
                    ticket, = plugin.ticket_store.tickets.values()
                    room = SimpleNamespace(room_id="owned", ticket_token="bound-token", user_id="12345",
                                           integration_closed=False, native_v2=SimpleNamespace(**ticket.native_v2),
                                           created_at=ticket.created_at + (1 if room_first else -1))
                    (plugin.detached_rooms if detached else plugin.rooms)["owned"] = room
                    for name, user, scope in (("other-user", "67890", room.native_v2.scope_key),
                                              ("other-scope", "12345", "different")):
                        plugin.rooms[name] = SimpleNamespace(room_id=name, ticket_token=name, user_id=user,
                            integration_closed=False, native_v2=SimpleNamespace(scope_key=scope), created_at=99)
                    provider = TOGETHER.NativeTogetherProvider(plugin, root, version=PRODUCT_VERSION)
                    for index, expect_room in enumerate((room_first, not room_first)):
                        close = request(provider, "together.room.close", invocation=f"close-{index}", identity=identity)
                        result = await provider.execute(close, before_dispatch=lambda: True)
                        self.assertEqual(result["result"]["closed"], expect_room)
                        self.assertTrue(result["result"]["ticket_revoked"])
                        self.assertEqual(await provider.execute(close, before_dispatch=lambda: True), result)
                        if index == 0:
                            self.assertEqual(plugin.closed, ["owned"] if room_first else [])
                            self.assertEqual(plugin.ticket_store.get(ticket.token) is not None, room_first)
                    self.assertEqual(plugin.closed, ["owned"])
                    self.assertEqual(set(plugin.rooms), {"other-user", "other-scope"})
                    self.assertIsNone(plugin.ticket_store.get(ticket.token))
                    self.assertEqual(await provider.lookup(create, before_dispatch=lambda: True), original)
                    plugin.send_telegram_group_invite.assert_awaited_once()

    async def test_refused_group_invite_survives_notice_error_and_replay(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        for failure in (RuntimeError("group unavailable"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                root = self.root / type(failure).__name__
                plugin = FakeTogether()
                plugin.telegram_group_invite_route = Mock(return_value=object())
                plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "failed", "reason": "telegram_forbidden"})
                plugin._revoke_unused_ticket = lambda ticket: plugin.ticket_store.tickets.pop(ticket.token) is not None
                plugin.notify_telegram_invite_refused = AsyncMock(side_effect=failure)
                provider = TOGETHER.NativeTogetherProvider(plugin, root, version=PRODUCT_VERSION)
                req = request(provider, "together.room.create", invocation="refused", identity=identity,
                              arguments={"mode": "call"})
                if isinstance(failure, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await provider.execute(req, before_dispatch=lambda: True)
                else:
                    self.assertEqual((await provider.execute(req, before_dispatch=lambda: True))["state"], "failed")
                restarted = TOGETHER.NativeTogetherProvider(plugin, root, version=PRODUCT_VERSION)
                receipt = await restarted.execute(req, before_dispatch=lambda: True)
                self.check_receipt(restarted, req, receipt, SideEffect.LOCAL)
                self.assertEqual((receipt["state"], receipt["effect"]), ("failed", "not_committed"))
                self.assertEqual(plugin.issued, 1)
                self.assertFalse(plugin.ticket_store.tickets)
                plugin.notify_telegram_invite_refused.assert_awaited_once()

    async def test_group_invite_replay_resumes_only_a_confirmed_unsent_notice(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        plugin = FakeTogether()
        plugin.telegram_group_invite_route = Mock(return_value=object())
        plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "failed", "reason": "telegram_forbidden"})
        plugin._revoke_unused_ticket = lambda ticket: plugin.ticket_store.tickets.pop(ticket.token) is not None
        plugin.notify_telegram_invite_refused = AsyncMock(side_effect=[{"state": "failed"}, {"state": "delivered"}])
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        req = request(provider, "together.room.create", invocation="refused", identity=identity,
                      arguments={"mode": "call"})
        # The refusal is on disk, but the process stops before reserving the group send.
        provider._notify_invite_refused = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await provider.execute(req, before_dispatch=lambda: True)
        plugin.notify_telegram_invite_refused.assert_not_awaited()
        restarted = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        restarted.NOTICE_RETRY_SECONDS = 0.01
        route = plugin.telegram_group_invite_route.return_value
        plugin.telegram_group_invite_route.side_effect = RuntimeError("route is offline")
        offline = await restarted.execute(req, before_dispatch=lambda: True)
        self.assertEqual((offline["state"], offline["effect"]), ("failed", "not_committed"))
        plugin.telegram_group_invite_route.side_effect = None
        plugin.telegram_group_invite_route.return_value = None
        await restarted.recover_group_notices()
        plugin.notify_telegram_invite_refused.assert_not_awaited()
        plugin.telegram_group_invite_route.return_value = route
        # Reconnection must deliver a confirmed-unsent notice even when the
        # registered tool returns a cached receipt and never calls this provider.
        await asyncio.wait_for(restarted._notice_retry, 2)
        await restarted.close()
        self.assertEqual(plugin.notify_telegram_invite_refused.await_count, 2)
        for _ in range(3):
            provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
            result = await provider.execute(req, before_dispatch=lambda: True)
            self.assertEqual((result["state"], result["effect"]), ("failed", "not_committed"))
        self.assertEqual(plugin.notify_telegram_invite_refused.await_count, 2)
        plugin.send_telegram_group_invite.assert_awaited_once()
        self.assertEqual(plugin.issued, 1)

    async def test_stale_refusal_notice_expires_without_contacting_group(self):
        identity = context()
        identity["audience"]["scope"].update(platform="tg-main", kind="group", session_id="-100#23")
        plugin = FakeTogether()
        plugin.telegram_group_invite_route = Mock(return_value=object())
        plugin.send_telegram_group_invite = AsyncMock(return_value={"state": "failed", "reason": "telegram_forbidden"})
        plugin._revoke_unused_ticket = lambda ticket: plugin.ticket_store.tickets.pop(ticket.token) is not None
        plugin.notify_telegram_invite_refused = AsyncMock(return_value={"state": "failed"})
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        req = request(provider, "together.room.create", invocation="stale", identity=identity, arguments={"mode": "call"})
        receipt = await provider.execute(req, before_dispatch=lambda: True)
        scope = TOGETHER.scope_key(identity)
        original = provider.store.get(scope, receipt["operation_id"])
        with patch.object(TOGETHER.time, "time", return_value=original["created_at"] + TOGETHER.INVITE_DEFAULT_SECONDS):
            await provider.recover_group_notices()
        self.assertEqual(provider.store.get(scope, original["id"])["result"]["group_notice"], "expired")
        plugin.notify_telegram_invite_refused.assert_awaited_once()
        await provider.close()

    async def test_together_create_rejects_missing_or_mismatched_persona(self):
        plugin = FakeTogether()
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        original = request(provider, "together.room.create", invocation="persona-a",
                           arguments={"mode": "call"})
        for mutation in (lambda item: item.pop("host_persona"),
                         lambda item: item["host_persona"].update(bot_id="other"),
                         lambda item: item["host_persona"].update(account_id="other")):
            invalid = copy.deepcopy(original)
            mutation(invalid)
            with self.assertRaisesRegex(TOGETHER.NativeTogetherError, "invalid_host_persona"):
                await provider.execute(invalid, before_dispatch=lambda: True)
        self.assertEqual(plugin.issued, 0)

    async def test_together_create_rejects_unbound_or_missing_v2_model(self):
        plugin = FakeTogether()
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        original = request(provider, "together.room.create", invocation="execution-a",
                           arguments={"mode": "call"})
        for mutation in (lambda item: item.pop("host_execution"),
                         lambda item: item["host_execution"].update(tts_language="invalid"),
                         lambda item: item["host_execution"].update(tts_timeout_seconds=0)):
            invalid = copy.deepcopy(original)
            mutation(invalid)
            with self.assertRaisesRegex(TOGETHER.NativeTogetherError, "invalid_host_execution"):
                await provider.execute(invalid, before_dispatch=lambda: True)
        missing = copy.deepcopy(original)
        missing["host_execution"]["model_provider_id"] = "missing"
        result = await provider.execute(missing, before_dispatch=lambda: True)
        self.assertEqual((result["state"], result["effect"]), ("failed", "not_committed"))
        self.assertEqual(plugin.issued, 0)

    async def test_invited_native_room_keeps_scope_in_snapshot_and_close(self):
        plugin = FakeTogether()
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root, version=PRODUCT_VERSION)
        created = request(provider, "together.room.create", invocation="create-invited",
                          arguments={"mode": "watch"})
        await provider.execute(created, before_dispatch=lambda: True)
        binding = plugin.ticket_store.tickets["ticket-1"].native_v2
        plugin.rooms["invited"] = SimpleNamespace(
            room_id="invited", ticket_token="invite-ticket", user_id="12345",
            native_v2=SimpleNamespace(**binding), integration_closed=False,
            mode="watch", created_at=3.0, call_active=False, resume_token="")
        snap = request(provider, "together.room.snapshot", invocation="snapshot-invited")
        self.assertEqual((await provider.execute(snap, before_dispatch=lambda: True))["result"]["active_count"], 1)
        other = request(provider, "together.room.snapshot", invocation="snapshot-other-invited",
                        identity=context(role="role-b"))
        self.assertEqual((await provider.execute(other, before_dispatch=lambda: True))["result"]["rooms"], [])
        closed = request(provider, "together.room.close", invocation="close-invited")
        result = await provider.execute(closed, before_dispatch=lambda: True)
        self.assertEqual(result["result"]["room_id"], "invited")
        self.assertEqual(plugin.closed, ["invited"])

    async def test_new_read_actions_are_declared_read_only(self):
        """The added query actions must stay read-only on the wire contract."""
        reality = REALITY.NativeRealityProvider(FakeReality(), self.root / "reality-read", version=PRODUCT_VERSION)
        together = TOGETHER.NativeTogetherProvider(FakeTogether(), self.root / "together-read", version=PRODUCT_VERSION)
        manifest_reality = parse_manifest(reality.manifest(), plugin_id=REALITY.PLUGIN_ID,
                                          version=PRODUCT_VERSION)
        manifest_together = parse_manifest(together.manifest(), plugin_id=TOGETHER.PLUGIN_ID,
                                           version=PRODUCT_VERSION)
        declared = {tool["action"]: tool for tool in manifest_reality.tools + manifest_together.tools}
        # Only journal reads are read-only.  A cancel still names an operation
        # and reports an effect, so it must not claim side_effect "none" (that
        # value obliges operation_id to be null and the effect to be "none").
        for action in ("reality.operation.lookup", "together.room.members",
                       "together.room.invitations"):
            self.assertEqual(declared[action]["side_effect"], "none", action)
        self.assertEqual(declared["reality.operation.cancel"]["side_effect"], "local")
        self.assertEqual(declared["together.room.invite"]["side_effect"], "local")
        self.assertEqual(declared["together.room.join"]["side_effect"], "local")
        self.assertEqual(declared["reality.device.action"]["side_effect"], "external_device")
        # A parameterised action is published as a closed object, never a union type.
        for tool in manifest_reality.tools + manifest_together.tools:
            self.assertEqual(tool["parameters"]["type"], "object")
            self.assertIs(tool["parameters"]["additionalProperties"], False)
            for name, spec in tool["parameters"]["properties"].items():
                self.assertIsInstance(spec["type"], str, f"{tool['action']}.{name}")

    async def test_reality_operation_lookup_reads_the_ledger_without_dispatching(self):
        plugin = FakeReality()
        provider = REALITY.NativeRealityProvider(plugin, self.root / "ledger", version=PRODUCT_VERSION)
        controlled = request(provider, "reality.device.control", invocation="device-ledger",
                             arguments={"alias": "desk", "is_on": True})
        written = await provider.execute(controlled, before_dispatch=lambda: True)
        self.check_receipt(provider, controlled, written, SideEffect.EXTERNAL_NETWORK)
        self.assertEqual(plugin.calls, 1)

        looked_up = request(provider, "reality.operation.lookup", invocation="lookup-ledger",
                            arguments={"operation_id": written["operation_id"]})
        found = await provider.execute(looked_up, before_dispatch=lambda: True)
        self.check_receipt(provider, looked_up, found, SideEffect.NONE)
        self.assertIsNone(found["operation_id"])
        # The ledger read returns the original receipt unchanged.
        self.assertEqual(found["result"]["control"], written["result"]["control"])
        # Reading the ledger must never reach the device again.
        self.assertEqual(plugin.calls, 1)

    async def test_reality_operation_cancel_refuses_a_dispatched_operation(self):
        plugin = FakeReality()
        provider = REALITY.NativeRealityProvider(plugin, self.root / "cancel", version=PRODUCT_VERSION)
        controlled = request(provider, "reality.device.control", invocation="device-cancel",
                             arguments={"alias": "desk", "is_on": False})
        written = await provider.execute(controlled, before_dispatch=lambda: True)
        self.assertEqual(written["effect"], "committed")

        cancel = request(provider, "reality.operation.cancel", invocation="cancel-1",
                         arguments={"operation_id": written["operation_id"]})
        refused = await provider.execute(cancel, before_dispatch=lambda: True)
        self.check_receipt(provider, cancel, refused, SideEffect.LOCAL)
        self.assertEqual(refused["state"], "failed")
        # The original operation keeps its real, already-committed effect.
        restarted = REALITY.NativeRealityProvider(plugin, self.root / "cancel", version=PRODUCT_VERSION)
        original = await restarted.lookup(controlled, before_dispatch=lambda: True)
        self.assertEqual((original["state"], original["effect"]), ("succeeded", "committed"))
        self.assertEqual(plugin.calls, 1)

    async def test_closed_ports_reject_all_entries(self):
        for provider, action in (
            (REALITY.NativeRealityProvider(FakeReality(), self.root / "reality", version=PRODUCT_VERSION), "reality.snapshot.read"),
            (TOGETHER.NativeTogetherProvider(FakeTogether(), self.root / "together", version=PRODUCT_VERSION), "together.room.snapshot"),
        ):
            req = request(provider, action, invocation=action)
            await provider.close()
            with self.assertRaises(ValueError):
                provider.manifest()
            with self.assertRaises(ValueError):
                await provider.execute(req, before_dispatch=lambda: True)
            with self.assertRaises(ValueError):
                await provider.lookup(req, before_dispatch=lambda: True)

    async def test_unavailable_room_server_and_corrupt_journals(self):
        plugin = FakeTogether()
        plugin.room_server.running = False
        provider = TOGETHER.NativeTogetherProvider(plugin, self.root / "unavailable", version=PRODUCT_VERSION)
        req = request(provider, "together.room.create", invocation="server-down", arguments={"mode": "call"})
        result = await provider.execute(req, before_dispatch=lambda: True)
        self.check_receipt(provider, req, result, SideEffect.LOCAL)
        self.assertEqual((result["state"], result["effect"]), ("failed", "not_committed"))
        self.assertEqual(plugin.issued, 0)
        for module, constructor in ((REALITY, REALITY.NativeRealityProvider),
                                    (TOGETHER, TOGETHER.NativeTogetherProvider)):
            root = self.root / (module.PROVIDER_ID + "-corrupt")
            journal = root / "native_v2" / "operations.json"
            journal.parent.mkdir(parents=True)
            journal.write_text("{bad json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "native_operation_store_invalid"):
                constructor(FakeReality() if module is REALITY else FakeTogether(), root)


if __name__ == "__main__":
    unittest.main()
