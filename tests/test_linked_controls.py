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


if __name__ == "__main__":
    unittest.main()
