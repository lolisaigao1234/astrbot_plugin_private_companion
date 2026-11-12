"""Linked private dialogue through the pinned, unmodified AstrBot pipeline."""
import asyncio
import json
import unittest

from tests.linked_reply_support import LinkedReplyFixture, ORIGINS


class LinkedConversationConcurrencyTests(LinkedReplyFixture, unittest.IsolatedAsyncioTestCase):
    async def test_negative_control_shared_selection_without_gate_loses_a_turn(self):
        await self.manager.switch_conversation(ORIGINS[1], "conversation-a")
        self.pause_after_response = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        second = await self.dispatch(self.event_for(1))
        await asyncio.wait_for(asyncio.shield(second), 10)
        self.allow_model.set()
        await asyncio.wait_for(asyncio.shield(first), 10)
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "A", "reply-A"])
        self.assertEqual([write[0] for write in self.manager.writes], [ORIGINS[1], ORIGINS[0]])
        self.assertEqual(len(self.calls), 2)

    async def test_explicit_versioned_link_selects_existing_history_and_survives_restart(self):
        from astrbot_host.account_session_api import AccountSessionWebAPI
        from unittest.mock import patch
        from types import SimpleNamespace as NS

        api = AccountSessionWebAPI(self.controller)
        body = dict(schema_version=1, link_id="owner-dialogue", expected_revision=0,
            conversation_id="conversation-a", source_target_id="first", member_target_ids=["first", "second"])
        async def request_body():
            return json.dumps(body).encode()
        with patch("astrbot_host.account_session_api.request", NS(username="owner", query={}, body=request_body)):
            result = await api.link_conversation()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["revision"], 1)
        self.assertEqual(await self.manager.get_curr_conversation_id(ORIGINS[1]), "conversation-a")
        independent = await self.manager.get_conversation(ORIGINS[1], "conversation-b")
        self.assertEqual(self.contents(json.loads(independent.history)), ["separate-old", "separate-reply"])
        await self.controller.stop()
        await self.controller.start()
        self.assertEqual(self.controller.session.linked_conversations.get("owner-dialogue")["revision"], 1)
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply"])

    async def test_managed_link_serializes_equal_ids_and_redelivery_has_one_effect(self):
        result = await self.link()
        self.assertEqual(result["status"], "ok", result)
        self.pause_after_response = self.manager.pause_save = True
        a, b = self.event_for(0), self.event_for(1)
        first = await self.dispatch(a)
        await asyncio.wait_for(self.model_done.wait(), 15)
        reads = len(self.manager.reads)
        second = await self.dispatch(b)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.manager.reads), reads)
        self.assertEqual(len(self.calls), 1)
        self.allow_model.set()
        await asyncio.wait_for(self.manager.saving.wait(), 10)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(second.done())
        self.manager.allow_save.set()
        await asyncio.wait_for(asyncio.gather(first, second), 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()),
            ["old", "old-reply", "A", "reply-A", "B", "reply-B"])
        self.assertEqual([write[0] for write in self.manager.writes], list(ORIGINS))
        self.assertEqual(self.manager.tasks, [first, second])
        self.assertEqual([platform for platform, _ in self.sent], ["qq-main", "qq-second"])
        self.assertEqual((a.unified_msg_origin, b.unified_msg_origin), ORIGINS)
        self.assertIn("reply-A", str(self.calls[1]["contexts"]))
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
        prepared = b.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        self.assertEqual(prepared["source_endpoint"].unified_msg_origin, ORIGINS[1])
        self.assertEqual(prepared["conversation"].unified_msg_origin, ORIGINS[0])
        self.assertEqual(prepared["source_endpoint"].target_id, "second")
        await (await self.dispatch(self.event_for(1)))
        self.assertEqual(len(self.manager.writes), 2)
        self.assertEqual(len(self.calls), 2)

    async def test_rapid_messages_from_one_linked_chat_merge_like_an_unlinked_chat(self):
        self.reply_settings(private_debounce_enabled=True, private_debounce_min_ms=1000,
            private_debounce_max_ms=2000, private_debounce_total_ms=6000)
        async def burst(prefix):
            first = await self.dispatch(self.event_for(1, "one", prefix + "one"))
            await asyncio.sleep(.3)  # inside the first message's quiet window
            second = await self.dispatch(self.event_for(1, "two", prefix + "two"))
            await asyncio.wait_for(asyncio.gather(first, second), 30)
        await burst("unlinked-")
        independent = await self.manager.get_conversation(ORIGINS[1], "conversation-b")
        unlinked = self.contents(json.loads(independent.history))[2:]
        self.assertEqual(unlinked, ["one\ntwo", "reply-B"])
        self.assertEqual((await self.link())["status"], "ok")
        await burst("linked-")
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history())[2:], unlinked)
        self.assertEqual(len(self.calls), 2)

    async def test_conversation_continues_after_original_endpoint_is_detached_and_offline(self):
        self.assertEqual((await self.link())["status"], "ok")
        result = await self.link(revision=1, members=["second"])
        self.assertEqual(result["status"], "ok", result)
        self.native.platforms["qq-main"].client._wsr_api_clients.clear()
        b = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(b), 15)
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])
        self.assertEqual([p for p, _ in self.sent], ["qq-second"])

    async def exercise_exit(self, mode):
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_model = True
        self.provider_mode = mode
        if mode == "provider_exception":
            self.model_error = RuntimeError("synthetic provider exception")
        if mode == "send_error":
            self.send_error = True
        if mode == "save_error":
            self.manager.save_error = True
        a, b = self.event_for(0), self.event_for(1)
        first = await self.dispatch(a)
        await asyncio.wait_for(self.model_done.wait(), 15)
        second = await self.dispatch(b)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.calls), 1)
        self.allow_model.set()
        await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 20)
        await self.controller.session.linked_conversations.gate.drain()
        history = self.contents(await self.history())
        self.assertEqual(history[-2:], ["B", "reply-B"])
        if mode in ("provider_error", "provider_exception", "save_error"):
            self.assertEqual([write[0] for write in self.manager.writes], [ORIGINS[1]])
        else:
            self.assertEqual([write[0] for write in self.manager.writes], list(ORIGINS))
            self.assertIn("reply-A", str(self.calls[-1]["contexts"]))
        if mode == "tool_failure":
            self.assertEqual(self.failure_tool.calls, 1)
            self.assertIn("synthetic tool failure", str(await self.history()))
        if mode == "tool":
            tool_messages = [message for message in await self.history() if message["role"] == "tool"]
            self.assertEqual(json.loads(tool_messages[0]["content"])["state"], "succeeded")

    async def test_provider_error_response_releases_only_after_turn_completion(self):
        await self.exercise_exit("provider_error")

    async def test_provider_exception_does_not_let_successor_overwrite_history(self):
        await self.exercise_exit("provider_exception")

    async def test_send_error_unwinds_before_successor(self):
        await self.exercise_exit("send_error")

    async def test_save_error_unwinds_before_successor(self):
        await self.exercise_exit("save_error")

    async def test_tool_continuation_stays_inside_conversation_permit(self):
        await self.exercise_exit("tool")

    async def test_tool_failure_stays_inside_conversation_permit(self):
        await self.exercise_exit("tool_failure")

    async def streaming_pair(self, true_streaming):
        self.config["provider_settings"]["streaming_response"] = True
        from companion_v2.configuration.models import ConfigLayer
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, configuration_fields, target_for
        owner = ReplyConfigurationOwner(self.app.runtime)
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        for target_id in ("first", "second"):
            target = target_for(self.controller.authority.context(target_id, "role-a"), ConfigLayer.ROLE)
            owner.write(target, fields, changes={"conversation.presentation.enabled": False}, reset=(),
                expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())
        semantic = self.app.semantics
        semantic.store.configure({"reactions_enabled": False}, semantic.store.settings()["revision"])
        # Reinitialize the real configured stages, exactly as native profile startup does.
        self.scheduler.stages.clear()
        await self.scheduler.initialize()
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_final_model = self.manager.pause_save = True
        a, b = self.event_for(0), self.event_for(1)
        for event in (a, b):
            event.platform_meta.support_streaming_message = True
            event.set_extra("synthetic_stream_transport", true_streaming)
        first = await self.dispatch(a)
        await asyncio.wait_for(self.final_model_done.wait(), 15)
        second = await self.dispatch(b)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.calls), 1)
        self.allow_final_model.set()
        await asyncio.wait_for(self.manager.saving.wait(), 10)
        self.assertEqual(len(self.calls), 1)
        self.manager.allow_save.set()
        await asyncio.wait_for(asyncio.gather(first, second), 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "A", "reply-A", "B", "reply-B"])
        self.assertEqual(len(self.manager.writes), 2)
        self.assertEqual(self.stream_calls, list(ORIGINS))
        self.assertEqual([a.stream_invocations, b.stream_invocations], [1, 1])
        if true_streaming:
            self.assertEqual(a.transport_chunks, ["reply-", "A"])
            self.assertEqual(b.transport_chunks, ["reply-", "B"])

    async def test_buffered_streaming_saves_once_before_successor(self):
        await self.streaming_pair(False)

    async def test_true_streaming_transport_saves_once_before_successor(self):
        await self.streaming_pair(True)

    async def test_cancelling_a_waiter_stops_it_without_consuming_or_leaking_a_permit(self):
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        waiter_event = self.event_for(1)
        waiter = await self.dispatch(waiter_event)
        await self.queued_for_permit(waiter_event)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        self.assertTrue(waiter_event.is_stopped())
        self.assertEqual(len(self.calls), 1)
        self.allow_model.set()
        await first
        await asyncio.wait_for(await self.dispatch(self.event_for(1, identifier="next-message")), 15)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "A", "reply-A", "B", "reply-B"])

    async def cancel_active_pair(self, when):
        self.assertEqual((await self.link())["status"], "ok")
        if when == "saving":
            self.manager.pause_save = True
        else:
            self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for((self.manager.saving if when == "saving" else self.model_done).wait(), 15)
        second = await self.dispatch(self.event_for(1))
        await asyncio.sleep(.05)
        self.assertEqual(len(self.calls), 1)
        first.cancel()
        await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])
        self.assertEqual([write[0] for write in self.manager.writes], [ORIGINS[1]])

    async def test_cancel_during_model_unwinds_native_lock_before_successor(self):
        await self.cancel_active_pair("model")

    async def test_cancel_during_save_unwinds_native_lock_before_successor(self):
        await self.cancel_active_pair("saving")

    async def test_native_stop_bypasses_gate_and_preserves_native_abort_persistence(self):
        from astrbot.builtin_stars.builtin_commands.commands.conversation import ConversationCommands
        commands = ConversationCommands(self.core_context)
        self.builtin("stop", commands.stop)
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_model = True
        a = self.event_for(0)
        first = await self.dispatch(a)
        await asyncio.wait_for(self.model_done.wait(), 15)
        second = await self.dispatch(self.event_for(1))
        stop = self.event_for(0, "/stop", "stop-command")
        await asyncio.wait_for(await self.dispatch(stop), 5)
        await asyncio.wait_for(asyncio.gather(first, second), 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertTrue(any("Requested to stop" in str(payload) for _, payload in self.sent))
        self.assertFalse(a.is_stopped())
        self.assertEqual([write[0] for write in self.manager.writes], list(ORIGINS))
        self.assertEqual(self.contents(await self.history())[-2:], ["B", "reply-B"])

    async def test_session_changing_and_unknown_commands_are_blocked_before_handlers(self):
        called = []
        for command in ("reset", "new", "provider", "set", "unset", "third_party_destructive"):
            async def destructive(event):
                called.append(event.message_str)
                await self.manager.update_conversation(event.unified_msg_origin, "conversation-a", history=[])
            self.builtin(command, destructive)
        self.assertEqual((await self.link())["status"], "ok")
        for index, text in enumerate(("/reset", "/new", "/provider", "/set", "/unset",
                                      "/third_party_destructive", "陪伴 全部重置")):
            with self.subTest(text=text):
                event = self.event_for(1, text, "command-" + str(index))
                await asyncio.wait_for(await self.dispatch(event), 10)
                self.assertTrue(event.is_stopped())
        self.assertEqual(called, [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply"])

    async def test_stopped_host_cannot_fall_through_to_an_ungated_native_turn(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.controller.stop()
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])

    async def test_cold_controller_refuses_linked_native_turn_before_host_has_loaded(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.controller.terminate()
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])

    async def test_stopped_yield_defers_finalization_and_barrier_never_admits_early(self):
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.utils.session_lock import session_lock_manager
        self.assertEqual((await self.link())["status"], "ok")
        barrier_held, release_barrier = asyncio.Event(), asyncio.Event()
        lock_waiter = None
        async def hold_native_origin():
            async with session_lock_manager.acquire_lock(ORIGINS[0]):
                barrier_held.set()
                await release_barrier.wait()
        async def stop_at_yield(event):
            nonlocal lock_waiter
            if event.unified_msg_origin == ORIGINS[0]:
                lock_waiter = asyncio.create_task(hold_native_origin())
                event.stop_event()
        self.register(EventType.OnDecoratingResultEvent, stop_at_yield, priority=-2000000)
        a = self.event_for(0)
        first = await self.dispatch(a)
        await asyncio.wait_for(first, 15)
        await asyncio.wait_for(barrier_held.wait(), 5)
        second = await self.dispatch(self.event_for(1))
        await asyncio.sleep(.05)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(second.done())
        self.assertEqual(self.manager.writes, [])
        release_barrier.set()
        await lock_waiter
        await asyncio.wait_for(second, 15)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])

    async def test_cancellation_inside_real_waiting_hook_is_swallowed_but_event_stays_stopped(self):
        from astrbot.core.star.star_handler import EventType
        self.assertEqual((await self.link())["status"], "ok")
        # A handler-issued ProviderRequest reaches the agent before companion intake.
        async def request_native_reply(event):
            conversation = await self.manager.get_conversation(event.unified_msg_origin, "conversation-a")
            yield event.request_llm(prompt=event.message_str, conversation=conversation)
        self.register(EventType.AdapterMessageEvent, request_native_reply, self.handlers[0].event_filters, priority=0)
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        b = self.event_for(1)
        waiter = await self.dispatch(b)
        await self.queued_for_permit(b)
        waiter.cancel()
        await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), 5)
        self.assertTrue(b.is_stopped())
        self.assertEqual(len(self.calls), 1)
        self.allow_model.set()
        await first
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual([write[0] for write in self.manager.writes], [ORIGINS[0]])

    async def test_companion_background_history_writer_cannot_overwrite_linked_history(self):
        from astrbot_host.linked_turn_gate import linked_history_writer
        self.assertEqual((await self.link())["status"], "ok")
        async with linked_history_writer("conversation-a") as allowed:
            if allowed:
                await self.manager.update_conversation(ORIGINS[0], "conversation-a", history=[])
        self.assertFalse(allowed)
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply"])

    async def test_link_cannot_adopt_history_with_different_source_model_settings(self):
        from copy import deepcopy
        config = deepcopy(self.config)
        config["provider_settings"]["streaming_response"] = True
        profile_id = await self.acm.create_conf(config, name="synthetic-source-profile")
        await self.acm.ucr.update_route(ORIGINS[0], profile_id)
        result = await self.link(members=["second"])
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["data"]["code"], "linked_settings_mismatch")
        self.assertEqual(await self.manager.get_curr_conversation_id(ORIGINS[1]), "conversation-b")

    async def test_binding_commands_are_unavailable_from_a_linked_endpoint(self):
        self.assertEqual((await self.link())["status"], "ok")
        event = self.event_for(1, "companion_unbind", "binding-command")
        self.assertTrue(self.controller.handles_binding(event))
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])

    async def test_creating_a_link_refuses_an_already_active_unlinked_turn(self):
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        try:
            result = await asyncio.wait_for(self.link(), 1)
            self.assertEqual(result["status"], "error", result)
            self.assertEqual(result["data"]["code"], "linked_busy")
        finally:
            self.allow_model.set()
            await first
        self.assertEqual(await self.manager.get_curr_conversation_id(ORIGINS[1]), "conversation-b")

    async def test_explicit_model_override_cannot_fork_effective_linked_model(self):
        self.assertEqual((await self.link())["status"], "ok")
        event = self.event_for(1)
        event.set_extra("selected_model", "another-model")
        await asyncio.wait_for(await self.dispatch(event), 15)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])

    async def test_linked_preparation_outside_pipeline_is_refused_before_history_read(self):
        self.assertEqual((await self.link())["status"], "ok")
        reads = len(self.manager.reads)
        event = self.event_for(1)
        result = await self.controller.prepare_core_reply(event)
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "linked_permit_required")
        self.assertTrue(event.is_stopped())
        self.assertEqual(len(self.manager.reads), reads)

    async def test_full_restart_restores_link_and_astrbot_public_selection(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.controller.terminate()
        from tests.linked_reply_support import RecordedConversations
        self.manager = RecordedConversations(self.db)
        self.native.conversation_manager = self.manager
        self.core_context.conversation_manager = self.manager
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.assertEqual(await self.manager.get_curr_conversation_id(ORIGINS[1]), "conversation-a")
        await asyncio.wait_for(await self.dispatch(self.event_for(1)), 15)
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])

    async def test_management_revision_conflict_and_prepared_work_are_fenced(self):
        from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
        self.assertEqual((await self.link())["status"], "ok")
        event = self.event_for(0)
        await asyncio.wait_for(await self.dispatch(event), 15)
        prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        result = await self.link(revision=0)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["data"]["code"], "conversation_link_revision_conflict")
        self.assertEqual((await self.link(revision=1, members=["second"]))["status"], "ok")
        denied = await self.controller.authorize_core_reply(event, prepared)
        self.assertFalse(denied.is_success)
        self.assertEqual(denied.failure.code, "linked_revision_changed")
        self.assertEqual(len(self.manager.writes), 1)
        revoked = self.event_for(0, "revoked", "revoked-input")
        await asyncio.wait_for(await self.dispatch(revoked), 10)
        self.assertTrue(revoked.is_stopped())
        self.assertEqual(len(self.manager.writes), 1)

    async def test_missing_anchor_refuses_a_turn_without_creating_a_replacement(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.manager.delete_conversation(ORIGINS[0], "conversation-a")
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        # Nor from the endpoint whose own selection the deletion cleared.
        orphaned = self.event_for(0)
        await asyncio.wait_for(await self.dispatch(orphaned), 10)
        self.assertTrue(orphaned.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
        self.assertIsNone(await self.manager.get_curr_conversation_id(ORIGINS[0]))
        self.assertIsNone(await self.manager.get_conversation(ORIGINS[0], "conversation-a"))

    async def test_selection_drift_fails_closed_instead_of_silently_forking(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.manager.switch_conversation(ORIGINS[1], "conversation-b")
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply"])

    async def test_unrelated_config_edit_leaves_the_next_linked_turn_working(self):
        from astrbot_host.linked_conversations import KEY
        self.assertEqual((await self.link())["status"], "ok")
        # A record written before the fingerprint left the document is still read.
        records = self.controller.session.linked_conversations.records()
        records["owner-dialogue"]["settings"] = "sha256:frozen-at-link-time"
        self.controller.authority.write(KEY, records)
        self.config["admins_id"].append("synthetic-admin")
        self.config["platform_settings"]["id_whitelist"].append("synthetic:FriendMessage:1")
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 15)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])

    async def test_member_settings_divergence_is_refused_until_the_members_agree_again(self):
        from astrbot.core import sp
        self.assertEqual((await self.link())["status"], "ok")
        for origin in ORIGINS:
            self.addAsyncCleanup(sp.session_put, origin, "session_variables", {})
        changed = {"synthetic-variable": "changed"}
        await sp.session_put(ORIGINS[1], "session_variables", changed)
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
        # The same change on the other member is simply followed: no re-link.
        await sp.session_put(ORIGINS[0], "session_variables", changed)
        event = self.event_for(1, identifier="after-alignment")
        await asyncio.wait_for(await self.dispatch(event), 15)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])
        self.assertEqual(self.controller.session.linked_conversations.get("owner-dialogue")["revision"], 1)

    async def test_links_require_local_runner_and_enabled_companion_hooks(self):
        saved = self.config["agent_runner"]["runner_type"]
        self.config["agent_runner"]["runner_type"] = "dify"
        result = await self.link()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["data"]["code"], "linked_runner_unsupported")
        self.config["agent_runner"]["runner_type"] = saved
        self.config["plugin_set"] = []
        result = await self.link()
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["data"]["code"], "linked_plugin_disabled")
        self.assertEqual(self.controller.session.linked_conversations.records(), {})

    async def test_live_mode_is_refused_before_its_double_save_path(self):
        self.assertEqual((await self.link())["status"], "ok")
        event = self.event_for(1)
        event.set_extra("action_type", "live")
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])

    async def test_unlinked_endpoints_remain_concurrent_with_separate_histories(self):
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        second = await self.dispatch(self.event_for(1))
        await asyncio.wait_for(second, 15)
        self.assertFalse(first.done())
        self.allow_model.set()
        await first
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "A", "reply-A"])
        independent = await self.manager.get_conversation(ORIGINS[1], "conversation-b")
        self.assertEqual(self.contents(json.loads(independent.history)), ["separate-old", "separate-reply", "B", "reply-B"])
        self.assertEqual(self.controller.session.linked_conversations.gate.owners, {})

    async def test_reload_reuses_coordinator_and_unload_drains_before_replacing_it(self):
        from astrbot_host.linked_turn_gate import LinkedTurnGate
        import importlib.util
        from astrbot_host import linked_turn_gate
        self.assertEqual((await self.link())["status"], "ok")
        gate = LinkedTurnGate.shared()
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        stopping = asyncio.create_task(self.controller.stop())
        await asyncio.sleep(.05)
        self.assertFalse(stopping.done())
        spec = importlib.util.spec_from_file_location("synthetic_reloaded_linked_gate", linked_turn_gate.__file__)
        reloaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reloaded)
        self.assertIs(reloaded.LinkedTurnGate.shared(), gate)
        self.allow_model.set()
        await first
        await asyncio.wait_for(stopping, 10)
        self.assertEqual(gate.owners, {})
        self.assertTrue(first.done())
        self.assertIs(reloaded.LinkedTurnGate.shared(), gate)

    async def test_tripwire_stops_missing_permit_before_provider_or_save(self):
        self.assertEqual((await self.link())["status"], "ok")
        # Simulate disabled intake/waiting components through their real metadata.
        for handler in self.handlers:
            if handler.handler_name in ("receive_companion_message", "admit_linked_companion_turn"):
                handler.enabled = False
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])

    async def test_management_requires_dashboard_administrator(self):
        from astrbot_host.account_session_api import AccountSessionWebAPI
        from unittest.mock import patch
        from types import SimpleNamespace as NS
        with patch("astrbot_host.account_session_api.request", NS(username="api_key:synthetic", query={})):
            result = await AccountSessionWebAPI(self.controller).link_conversation()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["data"]["code"], "dashboard_identity_required")
        self.assertEqual(self.controller.session.linked_conversations.records(), {})

    async def test_waiting_linked_turn_queues_without_a_time_limit(self):
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        waiter = self.event_for(1)
        second = await self.dispatch(waiter)
        # Longer than any tool or streaming turn was once allowed to take.
        await asyncio.sleep(17)
        self.assertFalse(second.done())
        self.assertFalse(waiter.is_stopped())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sent, [])
        self.allow_model.set()
        await asyncio.wait_for(asyncio.gather(first, second), 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertEqual(self.contents(await self.history()),
            ["old", "old-reply", "A", "reply-A", "B", "reply-B"])

    async def test_native_stop_can_abort_a_turn_while_unload_is_draining(self):
        from astrbot.builtin_stars.builtin_commands.commands.conversation import ConversationCommands
        self.builtin("stop", ConversationCommands(self.core_context).stop)
        self.assertEqual((await self.link())["status"], "ok")
        self.pause_model = True
        first = await self.dispatch(self.event_for(0))
        await asyncio.wait_for(self.model_done.wait(), 15)
        stopping = asyncio.create_task(self.controller.stop())
        await asyncio.sleep(.05)
        try:
            stop_event = self.event_for(0, "/stop", "stop-during-drain")
            await asyncio.wait_for(await self.dispatch(stop_event), 5)
            await asyncio.wait_for(asyncio.shield(first), 5)
            self.assertTrue(any("Requested to stop" in str(payload) for _, payload in self.sent))
        finally:
            self.allow_model.set()
            await asyncio.gather(first, stopping, return_exceptions=True)

    async def test_unload_waits_for_an_admitted_link_management_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_provider = self.native.get_using_provider_async
        async def provider(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_provider(*args, **kwargs)
        self.native.get_using_provider_async = provider
        linking = asyncio.create_task(self.link())
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(self.controller.stop())
        await asyncio.sleep(.05)
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(stopping), .5)
        finally:
            release.set()
            await asyncio.gather(linking, stopping, return_exceptions=True)
        self.assertEqual(linking.result()["status"], "ok")

    async def test_unreadable_link_record_fails_closed_even_when_hook_errors_are_swallowed(self):
        from astrbot_host.linked_conversations import KEY
        self.assertEqual((await self.link())["status"], "ok")
        # A deliberately invalid stored management document in the synthetic store.
        self.controller.authority.write(KEY, {"owner-dialogue": {"schema_version": 99}})
        for handler in self.handlers:
            if handler.handler_name in ("receive_companion_message", "guard_linked_companion_commands"):
                handler.enabled = False
        event = self.event_for(1)
        try:
            await asyncio.wait_for(await self.dispatch(event), 10)
            self.assertTrue(event.is_stopped())
            self.assertEqual(self.calls, [])
            self.assertEqual(self.manager.writes, [])
        finally:
            self.controller.authority.write(KEY, {})

    async def test_native_provider_context_cache_does_not_change_effective_settings(self):
        # AstrBot providers normally retain the same dict stored in native config.
        self.config["provider"] = [self.provider.provider_config]
        self.assertEqual((await self.link())["status"], "ok")
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 15)
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "B", "reply-B"])

    async def test_loaded_but_unstarted_host_refuses_ungated_native_fallback(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.controller.stop()
        loaded = await self.controller.load_existing()
        self.assertTrue(loaded.is_success, loaded.failure)
        self.assertEqual(self.controller.session.application.state, "new")
        event = self.event_for(1)
        await asyncio.wait_for(await self.dispatch(event), 10)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
