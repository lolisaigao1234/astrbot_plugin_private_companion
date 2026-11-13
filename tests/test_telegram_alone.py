"""The companion keeps running on Telegram alone when QQ is retired.

QQ is the endpoint the conversation started on (the link's anchor); Telegram
joined. QQ is then disconnected, unlinked, or removed from configuration.
"""
from tests import astrbot_data_root  # noqa: F401

import asyncio
from copy import deepcopy
import json
import unittest

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from companion_v2.execution.models import require
from companion_v2.memory.models import AtomRef, Proposal
from tests.linked_reply_support import RecordedConversations
from tests.multi_host_support import MultiFixture, multi_config
from tests.test_preferred_channel import PreferredChannelFixture

QQ, TELEGRAM = PreferredChannelFixture.origins


class TelegramAloneFixture(PreferredChannelFixture):
    """QQ (first, the anchor) and Telegram (second) linked on the real reply path."""

    qq_configured = True

    def make_controller(self, config=None):
        if not self.qq_configured:
            return MultiFixture.make_controller(self, self.telegram_only(self.controller.config))
        from companion_v2.adapters.astrbot.staging_config import REQUIRED_CAPABILITIES
        from companion_v2.conversation.agenda_tools import TOOL_FIELDS
        return super().make_controller(config or multi_config(capabilities=sorted(
            REQUIRED_CAPABILITIES | set(TOOL_FIELDS) | {"reminder.deliver", "memo.reminder.set",
                "configuration.write", "memory.read", "memory.propose", "memory.reflect", "memory.subscribe"})))

    @staticmethod
    def telegram_only(config):
        """The same plugin configuration with the QQ accounts, targets and personas taken out."""
        config = deepcopy(config)
        host = config["multi_host"]
        host["targets"] = [row for row in host["targets"] if row["platform_id"] == "tg-main"]
        host["personas"] = [row for row in host["personas"] if row["bot_id"] == "bot-tg"]
        return config

    async def turn(self, index, text, identifier):
        """One dispatched input: its event, and its prepared reply when it was admitted."""
        event = self.event_for(index, text, identifier)
        await asyncio.wait_for(await self.dispatch(event), 30)
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

    def fact(self, prepared, saved):
        return require(self.app.memory.writer.read(prepared["context"], AtomRef(saved["atom_id"], 1)))["object"]

    async def remove_qq_from_configuration(self):
        """The administrator saves the account mapping without QQ; its channel is gone from AstrBot too."""
        candidate = self.telegram_only(self.controller.config)

        def save(document):
            self.controller.config.clear()
            self.controller.config.update(deepcopy(document))
        self.controller.save_plugin_config = save
        for platform_id in ("qq-main", "qq-second"):
            self.native.platforms.pop(platform_id, None)
        self.qq_configured = False
        saved = await self.controller.apply_bindings(candidate)
        self.app = self.controller.session.application if self.controller.session else None
        return saved

    async def new_process(self):
        """Stop everything and start again from the stored state and the current configuration."""
        await self.controller.terminate()
        self.manager = RecordedConversations(self.db)
        self.native.conversation_manager = self.core_context.conversation_manager = self.manager
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        started = await self.controller.start()
        if started.is_success:
            self.app = self.controller.session.application
        return started

    async def history_seen_from(self, origin):
        cid = await self.manager.get_curr_conversation_id(origin)
        return self.contents(json.loads((await self.manager.get_conversation(origin, cid)).history))


class NoQQConfiguredTests(TelegramAloneFixture, unittest.IsolatedAsyncioTestCase):
    async def test_the_host_boots_and_answers_on_telegram_with_qq_removed_from_configuration(self):
        self.assertEqual((await self.link())["status"], "ok")
        _, on_qq = await self.turn(0, "from QQ", "1")
        saved = self.learn(on_qq, "tea", "learned-on-qq")
        await self.turn(1, "from Telegram", 2)

        applied = await self.remove_qq_from_configuration()
        self.assertTrue(applied["started"], applied)
        status = self.controller.status()
        self.assertEqual((status["state"], status["accepting"], status["last_failure"]), ("running", True, None))
        self.assertEqual([(row["platform_id"], row["available"]) for row in status["accounts"]], [("tg-main", True)])
        event, prepared = await self.turn(1, "still here", 3)
        self.assertFalse(event.is_stopped())
        self.assertEqual(await self.history_seen_from(TELEGRAM),
            ["old", "old-reply", "from QQ", "reply-A", "from Telegram", "reply-B", "still here", "reply-B"])
        self.assertEqual(self.fact(prepared, saved), "tea")

        started = await self.new_process()
        self.assertTrue(started.is_success, started.failure)
        event, prepared = await self.turn(1, "after a restart", 4)
        self.assertFalse(event.is_stopped())
        self.assertEqual((await self.history_seen_from(TELEGRAM))[-2:], ["after a restart", "reply-B"])
        self.assertEqual(self.fact(prepared, saved), "tea")


if __name__ == "__main__":
    unittest.main()



class LinkedMappingSaveTests(TelegramAloneFixture, unittest.IsolatedAsyncioTestCase):
    async def test_saving_the_unchanged_account_mapping_of_a_linked_conversation_restarts_the_host(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.turn(0, "from QQ", "1")
        await self.turn(1, "from Telegram", 2)
        self.controller.save_plugin_config = lambda document: None
        saved = await self.controller.apply_bindings(deepcopy(self.controller.config))
        self.assertTrue(saved["started"], saved)
        self.app = self.controller.session.application
        for index, identifier in ((0, "3"), (1, 4)):
            event, _ = await self.turn(index, "after the save", identifier)
            self.assertFalse(event.is_stopped())
        self.assertEqual((await self.history_seen_from(QQ))[-4:],
                         ["after the save", "reply-A", "after the save", "reply-B"])


if __name__ == "__main__":
    unittest.main()
