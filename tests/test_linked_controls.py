"""Removing an endpoint from a link, and shared conversation controls, on the real path."""
from tests import astrbot_data_root  # noqa: F401

import asyncio
import json
import unittest

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from companion_v2.execution.models import require
from companion_v2.memory.models import Proposal
from tests.linked_reply_support import LinkedTelegramFixture

QQ, TELEGRAM = LinkedTelegramFixture.origins


class LinkedControlFixture(LinkedTelegramFixture):
    def event_for(self, index, *args, **kwargs):
        event = super().event_for(index, *args, **kwargs)
        if index == 0:
            # The OneBot payload names both accounts; the companion's own commands authenticate it.
            event.message_obj.raw_message.update(self_id=100, user_id=200)
        return event

    async def turn(self, index, text, identifier):
        """One dispatched input: its event, and its prepared reply when it was admitted."""
        event = self.event_for(index, text, identifier)
        await asyncio.wait_for(await self.dispatch(event), 20)
        await self.controller.session.linked_conversations.gate.drain()
        prepared = (event.get_extra(CORE_REPLY_CONTEXT_EXTRA) or {}).get("prepared")
        if prepared is not None:
            await prepared["native_source_future"]
        return event, prepared

    def learn(self, prepared, text, operation):
        context = prepared["context"]
        evidence = self.app.memory.writer.access.capture(context, prepared["native_source_future"].result())
        return require(self.app.memory.writer.submit(context, Proposal("add", predicate="drink",
            object=text, representation="The owner drinks " + text, evidence=(evidence,)), operation_key=operation))

    def can_read_memory(self, context):
        return self.app.tasks.check_context_access(context, action="memory.read", observed_at=self.clock.now())

    async def stored(self, origin, conversation_id):
        conversation = await self.manager.get_conversation(origin, conversation_id)
        return self.contents(json.loads(conversation.history))


class UnlinkTests(LinkedControlFixture, unittest.IsolatedAsyncioTestCase):
    async def test_removing_the_endpoint_the_conversation_started_on_revokes_its_shared_reads(self):
        self.assertEqual((await self.link())["status"], "ok")
        _, original = await self.turn(0, "from QQ", "1")
        self.assertTrue(self.can_read_memory(original["context"]).is_success)
        self.assertEqual((await self.link(revision=1, members=["second"]))["status"], "ok")

        # The endpoint that remains keeps the conversation and all of its history.
        _, remaining = await self.turn(1, "from Telegram", 2)
        self.learn(remaining, "tea", "learned-after-unlink")
        self.assertEqual(self.contents(await self.history()),
                         ["old", "old-reply", "from QQ", "reply-A", "from Telegram", "reply-B"])

        # The removed endpoint is the conversation's native owner: its saved context reads nothing more.
        refused = self.can_read_memory(original["context"])
        self.assertFalse(refused.is_success)
        self.assertEqual(refused.failure.code, "conversation_link_revoked")
        with self.assertRaises(Exception) as denied:
            self.app.memory.query.begin(original["context"], "after-unlink")
        self.assertEqual(denied.exception.code, "conversation_link_revoked")
        direct = self.controller.authority.identities.authorize(original["context"].actor,
            scope=original["context"].scope, capability="memory.read", observed_at=self.clock.now())
        self.assertEqual(direct.failure.code, "scope_not_granted")
        # Nor is a send authorized for it: in the link record it is no member any more.
        for action in ("conversation.reply", "proactive.contact"):
            self.assertEqual(self.app.tasks.check_context_access(original["context"], action=action,
                observed_at=self.clock.now()).failure.code, "conversation_link_revoked")
        record = self.controller.session.linked_conversations.get("owner-dialogue")
        self.assertEqual(([row["origin"] for row in record["members"]], [row["origin"] for row in record["detached"]]),
                         ([TELEGRAM], [QQ]))
        # It selects the shared conversation no longer, receives no copy of it and starts no turn.
        selected = await self.manager.get_curr_conversation_id(QQ)
        self.assertNotIn(selected, (None, "conversation-a"))
        self.assertEqual(await self.stored(QQ, selected), [])
        calls, writes = len(self.calls), len(self.manager.writes)
        event, prepared = await self.turn(0, "still here?", "3")
        self.assertTrue(event.is_stopped())
        self.assertIsNone(prepared)
        self.assertEqual((len(self.calls), len(self.manager.writes)), (calls, writes))

    async def test_removed_endpoint_stays_closed_and_relinking_revives_none_of_its_earlier_work(self):
        self.assertEqual((await self.link())["status"], "ok")
        event, stale = await self.turn(1, "from Telegram", 1)
        self.assertEqual((await self.link(revision=1, members=["first"]))["status"], "ok")

        refused = self.can_read_memory(stale["context"])
        self.assertEqual(refused.failure.code, "conversation_link_revoked")
        selected = await self.manager.get_curr_conversation_id(TELEGRAM)
        self.assertNotIn(selected, (None, "conversation-a", "conversation-b"))
        self.assertEqual(await self.stored(TELEGRAM, selected), [])
        self.assertEqual(await self.stored(TELEGRAM, "conversation-b"), ["separate-old", "separate-reply"])
        sent = len(self.sent)
        closed, prepared = await self.turn(1, "still here?", 2)
        self.assertTrue(closed.is_stopped())
        self.assertIsNone(prepared)
        self.assertEqual(len(self.calls), 1)
        _, remaining = await self.turn(0, "from QQ", "3")
        self.assertEqual(self.contents(await self.history()),
                         ["old", "old-reply", "from Telegram", "reply-B", "from QQ", "reply-A"])

        # Linked again: a reply prepared, and a context saved, under the earlier membership stay dead.
        self.assertEqual((await self.link(revision=2))["status"], "ok")
        denied = await self.controller.authorize_core_reply(event, stale)
        self.assertFalse(denied.is_success)
        self.assertEqual(denied.failure.code, "linked_revision_changed")
        self.assertEqual([platform for platform, _ in self.sent[sent:]], ["tg-main", "qq-main"])  # refusal notice, QQ reply
        _, again = await self.turn(1, "back", 4)
        self.assertEqual(self.contents(await self.history())[-2:], ["back", "reply-B"])

    async def test_a_removed_endpoint_is_independent_only_after_explicit_admission_and_then_starts_fresh(self):
        self.assertEqual((await self.link())["status"], "ok")
        _, shared = await self.turn(0, "from QQ", "1")
        self.learn(shared, "tea", "learned-while-linked")
        self.assertEqual((await self.link(revision=1, members=["first"]))["status"], "ok")
        closed, _ = await self.turn(1, "not admitted yet", 2)
        self.assertTrue(closed.is_stopped())

        admitted = await self.link(revision=2, members=["first"], independent_target_ids=["second"])
        self.assertEqual(admitted["status"], "ok", admitted)
        self.assertEqual(admitted["data"]["detached"], [])
        event, own = await self.turn(1, "on my own", 3)
        self.assertFalse(event.is_stopped())
        selected = await self.manager.get_curr_conversation_id(TELEGRAM)
        self.assertNotIn(selected, ("conversation-a", "conversation-b"))
        self.assertEqual(await self.stored(TELEGRAM, selected), ["on my own", "reply-B"])
        self.assertNotIn("from QQ", str(self.calls[-1]["contexts"]))
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "from QQ", "reply-A"])
        self.app.memory.query.begin(own["context"], "independent-facts")
        facts = require(self.app.memory.query.query(own["context"], "independent-facts", query_intent="", kind="facts"))
        self.assertNotIn("tea", str(facts))

    async def test_the_endpoint_the_conversation_started_on_is_not_admitted_beside_its_own_stored_identity(self):
        self.assertEqual((await self.link())["status"], "ok")
        self.assertEqual((await self.link(revision=1, members=["second"]))["status"], "ok")
        refused = await self.link(revision=2, members=["second"], independent_target_ids=["first"])
        self.assertEqual(refused["status"], "error", refused)
        self.assertEqual(refused["data"]["code"], "linked_anchor_endpoint_closed")
        closed, _ = await self.turn(0, "still closed", "2")
        self.assertTrue(closed.is_stopped())
        self.assertEqual(self.calls, [])


class SharedControlTests(LinkedControlFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # AstrBot's own command handlers, registered as its loader registers them.
        from astrbot.builtin_stars.builtin_commands.commands.conversation import ConversationCommands
        from astrbot.builtin_stars.builtin_commands.commands.setunset import SetUnsetCommands
        conversation, variables = ConversationCommands(self.core_context), SetUnsetCommands(self.core_context)
        self.builtin("reset", conversation.reset)
        self.builtin("new", conversation.new_conv)
        self.builtin("stop", conversation.stop)
        self.command_with_arguments("set", variables.set_variable)
        self.command_with_arguments("unset", variables.unset_variable)
        self.assertEqual((await self.link())["status"], "ok")

    def command_with_arguments(self, command, method, *filters):
        # AstrBot's loader reads the parameters from the function and then binds its instance.
        metadata = self.builtin(command, method.__func__, method.__name__)
        metadata.handler = method
        metadata.event_filters.extend(filters)
        return metadata

    def link_record(self):
        return self.controller.session.linked_conversations.get("owner-dialogue")

    async def selections(self):
        return [await self.manager.get_curr_conversation_id(origin) for origin in self.origins]

    async def held_command(self, index, text, identifier):
        """A control sent while another turn runs: queued on the permit, nothing changed yet."""
        before = (await self.selections(), self.contents(await self.history()), self.link_record()["revision"])
        event = self.event_for(index, text, identifier)
        task = await self.dispatch(event)
        await self.queued_for_permit(event)
        await asyncio.sleep(.05)
        self.assertFalse(task.done())
        self.assertEqual((await self.selections(), self.contents(await self.history()),
                          self.link_record()["revision"]), before)
        return task

    async def test_new_conversation_from_one_endpoint_moves_every_member_behind_the_running_turn(self):
        _, first = await self.turn(0, "one", "1")
        self.learn(first, "tea", "learned-before-new")
        self.pause_after_response = True
        running = await self.dispatch(self.event_for(0, "two", "2"))
        await asyncio.wait_for(self.model_done.wait(), 15)
        command = await self.held_command(1, "/new", 3)
        self.allow_model.set()
        await asyncio.wait_for(asyncio.gather(running, command), 20)
        await self.controller.session.linked_conversations.gate.drain()

        fresh = self.link_record()["conversation_id"]
        self.assertNotEqual(fresh, "conversation-a")
        self.assertEqual(await self.selections(), [fresh, fresh])
        self.assertEqual(self.link_record()["revision"], 2)
        # AstrBot's /new keeps the previous conversation; the turn it interrupted is not saved, as natively.
        self.assertEqual(self.contents(await self.history()), ["old", "old-reply", "one", "reply-A"])
        self.assertIn("Switched to new conversation", str(self.sent[-1]))
        self.assertEqual(self.sent[-1][0], "tg-main")
        _, after = await self.turn(0, "three", "4")
        self.assertEqual(await self.stored(QQ, fresh), ["three", "reply-A"])
        self.assertNotIn("one", self.contents(self.calls[-1]["contexts"]))
        _, other = await self.turn(1, "four", 5)
        self.assertEqual(await self.stored(TELEGRAM, fresh), ["three", "reply-A", "four", "reply-B"])
        # Long-term memory is not part of the conversation context.
        for session, prepared in (("facts-on-qq", after), ("facts-on-telegram", other)):
            self.app.memory.query.begin(prepared["context"], session)
            facts = require(self.app.memory.query.query(prepared["context"], session, query_intent="", kind="facts"))
            self.assertIn("The owner drinks tea", str(facts))

    async def test_context_reset_from_the_other_endpoint_clears_the_one_history_and_keeps_memory(self):
        _, first = await self.turn(1, "one", 1)
        self.learn(first, "tea", "learned-before-reset")
        self.steer(1)
        self.pause_after_response = True
        running = await self.dispatch(self.event_for(1, "two", 2))
        await asyncio.wait_for(self.model_done.wait(), 15)
        command = await self.held_command(0, "/reset", "3")
        self.allow_model.set()
        await asyncio.wait_for(asyncio.gather(running, command), 20)
        await self.controller.session.linked_conversations.gate.drain()

        self.assertEqual(self.contents(await self.history()), [])
        self.assertEqual(await self.selections(), ["conversation-a", "conversation-a"])
        # The companion's temporary context ended on the endpoint that did not send the command too.
        from companion_v2.conversation.reset import boundary
        self.assertIsNotNone(boundary(self.app.runtime, first["context"].scope))
        self.assertEqual(self.link_record()["revision"], 1)
        self.assertEqual(self.sent[-1][0], "qq-main")
        self.assertIn("context has been cleared", str(self.sent[-1]))
        _, after = await self.turn(1, "three", 4)
        self.assertEqual(self.contents(await self.history()), ["three", "reply-B"])
        self.app.memory.query.begin(after["context"], "facts-after-reset")
        facts = require(self.app.memory.query.query(after["context"], "facts-after-reset", query_intent="", kind="facts"))
        self.assertIn("The owner drinks tea", str(facts))

    async def test_a_session_variable_set_on_one_endpoint_is_the_value_of_every_member(self):
        from astrbot.core import sp
        for origin in self.origins:
            self.addAsyncCleanup(sp.session_put, origin, "session_variables", {})
        event, _ = await self.turn(1, "/set mood calm", 1)
        self.assertFalse(event.is_stopped())
        self.assertEqual([await sp.session_get(origin, "session_variables", {}) for origin in self.origins],
                         [{"mood": "calm"}, {"mood": "calm"}])
        # The members agree, so the next turn of either is admitted.
        turn, _ = await self.turn(0, "hello", "2")
        self.assertFalse(turn.is_stopped())
        self.assertEqual(self.contents(await self.history())[-2:], ["hello", "reply-A"])
        await self.turn(0, "/unset mood", "3")
        self.assertEqual([await sp.session_get(origin, "session_variables", {}) for origin in self.origins], [{}, {}])
        turn, _ = await self.turn(1, "again", 4)
        self.assertEqual(self.contents(await self.history())[-2:], ["again", "reply-B"])

    def two_providers(self):
        """A second synthetic provider, selected per origin the way AstrBot stores it."""
        from types import SimpleNamespace as NS
        from astrbot.core import sp
        from astrbot.builtin_stars.builtin_commands.commands.provider import ProviderCommands
        from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
        first = self.provider
        first.meta = lambda: NS(type="fixture", id="native-model")
        second = NS(provider_config={"id": "second-model", "modalities": ["text", "tool_use"]},
            get_model=lambda: "fixture-2", text_chat=first.text_chat, text_chat_stream=first.text_chat_stream,
            meta=lambda: NS(type="fixture", id="second-model"))
        providers = {"native-model": first, "second-model": second}

        async def using(umo=None, **_):
            return providers[await sp.session_get(umo, "provider_perf_chat_completion", "native-model")]

        async def set_provider(provider_id, provider_type, umo=None):
            await sp.session_put(umo, f"provider_perf_{provider_type.value}", providers[provider_id].meta().id)
        for context in (self.native, self.core_context):
            context.get_using_provider_async = using
            context.get_all_providers = lambda: list(providers.values())
            context.provider_manager = NS(set_provider=set_provider)
        for origin in self.origins:
            self.addAsyncCleanup(sp.session_remove, origin, "provider_perf_chat_completion")
        self.command_with_arguments("provider", ProviderCommands(self.core_context).provider,
                                    PermissionTypeFilter(PermissionType.ADMIN))

        async def selected():
            return [(await using(umo=origin)).provider_config["id"] for origin in self.origins]
        return selected

    async def test_a_provider_switch_needs_the_endpoints_own_administrator_and_then_applies_to_every_member(self):
        selected = self.two_providers()
        self.config["admins_id"] = ["200"]  # the QQ sender only: the shared human is one, the endpoints are two
        refused, _ = await self.turn(1, "/provider 2", 1)
        self.assertEqual(await selected(), ["native-model", "native-model"])
        self.assertEqual(self.calls, [])
        switched, _ = await self.turn(0, "/provider 2", "2")
        self.assertFalse(switched.is_stopped())
        self.assertEqual(await selected(), ["second-model", "second-model"])
        turn, _ = await self.turn(1, "hello", 3)
        self.assertFalse(turn.is_stopped())
        self.assertEqual(self.contents(await self.history())[-2:], ["hello", "reply-B"])

    async def test_stop_from_one_endpoint_reaches_the_turn_running_on_the_other(self):
        self.pause_model = True
        running = self.event_for(0, "long", "1")
        task = await self.dispatch(running)
        await asyncio.wait_for(self.model_done.wait(), 15)
        stop = self.event_for(1, "/stop", 2)
        await asyncio.wait_for(await self.dispatch(stop), 10)
        self.assertFalse(stop.is_stopped())
        self.assertTrue(running.get_extra("agent_stop_requested"))
        self.allow_model.set()
        await asyncio.wait_for(task, 20)
        await self.controller.session.linked_conversations.gate.drain()
        self.assertFalse(running.is_stopped())

    async def test_shared_selection_settings_and_revocation_survive_a_restart(self):
        from astrbot.core import sp
        from tests.linked_reply_support import RecordedConversations
        for origin in self.origins:
            self.addAsyncCleanup(sp.session_put, origin, "session_variables", {})
        _, before = await self.turn(0, "one", "1")
        await self.turn(1, "/new", 2)
        await self.turn(0, "/set mood calm", "3")
        fresh = self.link_record()["conversation_id"]
        self.assertEqual((await self.link(revision=2, members=["second"], conversation_id=fresh))["status"], "ok")

        await self.controller.terminate()
        self.manager = RecordedConversations(self.db)
        self.native.conversation_manager = self.core_context.conversation_manager = self.manager
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        started = await self.controller.start()
        self.assertTrue(started.is_success, started.failure)
        self.app = self.controller.session.application

        self.assertEqual((self.link_record()["conversation_id"], self.link_record()["revision"]), (fresh, 3))
        self.assertEqual(await self.manager.get_curr_conversation_id(TELEGRAM), fresh)
        self.assertEqual(await sp.session_get(TELEGRAM, "session_variables", {}), {"mood": "calm"})
        turn, _ = await self.turn(1, "after restart", 4)
        self.assertFalse(turn.is_stopped())
        self.assertEqual(await self.stored(TELEGRAM, fresh), ["after restart", "reply-B"])
        closed, _ = await self.turn(0, "still removed", "5")
        self.assertTrue(closed.is_stopped())
        self.assertEqual(self.can_read_memory(before["context"]).failure.code, "conversation_link_revoked")

    async def facts(self, prepared, session):
        self.app.memory.query.begin(prepared["context"], session)
        return str(require(self.app.memory.query.query(prepared["context"], session, query_intent="", kind="facts")))

    async def test_the_companions_full_reset_keeps_its_own_authority_preview_and_confirmation(self):
        import re
        from companion_v2.identity_policy.models import AuthorizationTier
        from companion_v2.identity_policy.object_access import ObjectAccessPolicy
        _, first = await self.turn(0, "one", "1")
        self.learn(first, "tea", "learned-before-full-reset")
        policy = ObjectAccessPolicy(self.app.runtime)
        policy.set_tier(first["context"].actor.subject, AuthorizationTier.CORE,
                        expected_revision=policy.revision, observed_at=self.clock.now())
        shared = ["old", "old-reply", "one", "reply-A"]

        def qq_notice():
            # The companion's own receipts leave through the OneBot action port.
            return str(self.native.platforms["qq-main"].client._api._wsr_api.calls[-1])

        # The joined endpoint speaks for the same human and has no core standing of its own.
        await self.turn(1, "陪伴 全部重置", 2)
        self.assertEqual(self.sent[-1][0], "tg-main")
        self.assertIn("只允许安装核心管理员", str(self.sent[-1]))
        # On the endpoint that holds it: a preview first, and nothing is executed.
        await self.turn(0, "陪伴 全部重置", "3")
        preview = qq_notice()
        self.assertIn("尚未执行", preview)
        self.assertEqual(self.contents(await self.history()), shared)
        _, between = await self.turn(1, "two", 4)
        self.assertIn("The owner drinks tea", await self.facts(between, "facts-before-confirmation"))
        # The memory changed since the preview: its code confirms nothing any more.
        self.learn(between, "coffee", "learned-after-preview")
        code = re.search(r"确认 ([0-9A-F]{6})", preview).group(1)
        await self.turn(0, "陪伴 全部重置 确认 " + code, "5")
        self.assertIn("未执行", qq_notice())
        await self.turn(0, "陪伴 全部重置", "6")
        code = re.search(r"确认 ([0-9A-F]{6})", qq_notice()).group(1)
        await self.turn(0, "陪伴 全部重置 确认 " + code, "7")
        self.assertIn("已执行", qq_notice())
        # It forgets the relationship's memory for every member; it is not AstrBot's context reset.
        self.assertEqual(self.contents(await self.history()), shared + ["two", "reply-B"])
        _, after = await self.turn(1, "three", 8)
        self.assertNotIn("drinks", await self.facts(after, "facts-after-full-reset"))


if __name__ == "__main__":
    unittest.main()
