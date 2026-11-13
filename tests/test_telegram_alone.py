"""The companion keeps running on Telegram alone when QQ is retired.

QQ is the endpoint the conversation started on (the link's anchor); Telegram
joined. QQ is then disconnected, unlinked, or removed from configuration.
"""
from tests import astrbot_data_root  # noqa: F401

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import unittest

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from companion_v2.execution.models import require
from companion_v2.memory.models import AtomRef, Proposal
from tests import test_telegram_text_tools as text_tools
from tests.linked_reply_support import RecordedConversations, TELEGRAM_OWNER
from tests.multi_host_support import MultiFixture, multi_config
from tests.test_preferred_channel import PreferredChannelFixture

QQ, TELEGRAM = PreferredChannelFixture.origins
OWNER = str(TELEGRAM_OWNER)


class TelegramAloneFixture(PreferredChannelFixture):
    """QQ (first, the anchor) and Telegram (second) linked on the real reply path."""

    qq_configured = True
    prior = staticmethod(text_tools.TelegramTextToolTests.prior)

    def make_controller(self, config=None):
        if not self.qq_configured:
            # A removed account stays removed across a restart.
            return MultiFixture.make_controller(self, self.telegram_only(self.controller.config))
        from companion_v2.adapters.astrbot.staging_config import REQUIRED_CAPABILITIES
        from companion_v2.conversation.agenda_tools import TOOL_FIELDS
        return super().make_controller(config or multi_config(capabilities=sorted(
            REQUIRED_CAPABILITIES | set(TOOL_FIELDS) | {"reminder.deliver", "memo.reminder.set",
                "configuration.write", "memory.read", "memory.propose", "memory.reflect", "memory.subscribe",
                "calendar.read", "calendar.write", "role.life.read"})))

    @staticmethod
    def telegram_only(config):
        """The same plugin configuration with the QQ accounts, targets and personas taken out."""
        config = deepcopy(config)
        host = config["multi_host"]
        host["targets"] = [row for row in host["targets"] if row["platform_id"] == "tg-main"]
        host["personas"] = [row for row in host["personas"] if row["bot_id"] == "bot-tg"]
        return config

    async def attempt(self, index, text, identifier):
        """One dispatched input: its event, and its prepared reply when it was admitted."""
        event = self.event_for(index, text, identifier)
        if index == 0:
            # The OneBot payload names both accounts; the companion's own commands authenticate it.
            event.message_obj.raw_message.update(self_id=100, user_id=200)
        await asyncio.wait_for(await self.dispatch(event), 30)
        await self.controller.session.linked_conversations.gate.drain()
        prepared = (event.get_extra(CORE_REPLY_CONTEXT_EXTRA) or {}).get("prepared")
        if prepared is not None:
            await prepared["native_source_future"]
        return event, prepared

    async def turn(self, index, text, identifier):
        event, prepared = await self.attempt(index, text, identifier)
        self.assertIsNotNone(prepared, event.get_extra("companion_linked_failure"))
        return prepared

    async def use_tools(self, index, script, identifier):
        """A turn whose model uses the existing text tools; the turn's context and the tools' results."""
        event, results = await text_tools.TelegramTextToolTests.run_tools(
            self, index, script, event=self.event_for(index, "text tool request", identifier))
        return event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]["context"], results

    def learn(self, prepared, text, operation):
        context = prepared["context"]
        evidence = self.app.memory.writer.access.capture(context, prepared["native_source_future"].result())
        return require(self.app.memory.writer.submit(context, Proposal("add", predicate="drink",
            object=text, representation="The owner drinks " + text, evidence=(evidence,)), operation_key=operation))

    def fact(self, context, saved):
        return require(self.app.memory.writer.read(context, AtomRef(saved["atom_id"], 1)))["object"]

    def qq_disconnect(self):
        clients = self.native.platforms["qq-main"].client._wsr_api_clients
        self.qq_clients = dict(clients)
        clients.clear()

    def qq_connect(self):
        self.native.platforms["qq-main"].client._wsr_api_clients.update(self.qq_clients)

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
        self.assertTrue(saved["started"], saved)
        self.app = self.controller.session.application
        return saved

    async def new_process(self):
        """Stop everything and start again from the stored state and the current configuration."""
        await self.controller.terminate()
        self.manager = RecordedConversations(self.db)
        self.native.conversation_manager = self.core_context.conversation_manager = self.manager
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        started = await self.controller.start()
        self.assertTrue(started.is_success, started.failure)
        self.app = self.controller.session.application

    async def history_seen_from(self, origin):
        cid = await self.manager.get_curr_conversation_id(origin)
        return self.contents(json.loads((await self.manager.get_conversation(origin, cid)).history))

    async def spoken_on(self, origin):
        """What the human and the companion said, without the tool traffic in between."""
        return [text for text in await self.history_seen_from(origin) if text and not text.startswith("{")]

    def health(self):
        status = self.controller.status()
        return ((status["state"], status["accepting"], status["last_failure"]),
                {row["platform_id"]: (row["available"], row["code"]) for row in status["accounts"]})

    def link_record(self):
        return self.controller.session.linked_conversations.get("owner-dialogue")

    def nothing_goes_to_qq_from_here(self):
        self.sent.clear()
        self.qq_calls().clear()

    def sent_to_qq(self):
        """Replies through AstrBot's QQ event and the host's own QQ sends."""
        return [sent for platform, sent in self.sent if platform == "qq-main"] + list(self.qq_calls())


class RetiredQQScenario(TelegramAloneFixture):
    """One conversation built on both endpoints, then continued on Telegram with QQ retired."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # AstrBot's own command handlers, registered as its loader registers them.
        from astrbot.builtin_stars.builtin_commands.commands.conversation import ConversationCommands
        from astrbot.builtin_stars.builtin_commands.commands.setunset import SetUnsetCommands
        from astrbot.core import sp
        self.builtin("new", ConversationCommands(self.core_context).new_conv)
        method = SetUnsetCommands(self.core_context).set_variable
        self.builtin("set", method.__func__, method.__name__).handler = method
        for origin in self.origins:
            self.addAsyncCleanup(sp.session_put, origin, "session_variables", {})

    async def before_retirement(self):
        from companion_v2.configuration.models import ConfigLayer, ConfigTarget
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, configuration_fields, target_for
        from companion_v2.role_work.journal.models import JournalContext, JournalDraft, JournalKey, JournalKind
        from companion_v2.role_work.state.configuration import StateConfigurationOwner, configuration_fields as state_fields
        self.assertEqual((await self.link())["status"], "ok")
        on_qq = await self.turn(0, "from QQ", "1")
        self.qq = on_qq["context"]
        self.qq_source = on_qq["native_source_future"].result()
        self.tea = self.learn(on_qq, "tea", "learned-on-qq")
        _, noted = await self.use_tools(0, [("memo.create", {"title": "tea", "body": "noted on QQ"})], "2")
        self.note_id = noted[0]["payload"]["object_id"]
        life, at = self.app.life_context, self.clock.now()
        self.diary = JournalKey(life.owner(self.qq), JournalKind.DIARY, at.date().isoformat())
        life.journal.save(JournalDraft(JournalContext(self.diary, self.qq.scope, "UTC"), body="A day on QQ"),
                          expected_revision=0, operation_id="diary-on-qq")
        common = self.controller.settings.common
        self.fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        owner, target = ReplyConfigurationOwner(self.app.runtime), target_for(self.qq)
        owner.write(target, self.fields, changes={"conversation.presentation.file_understanding_max_chars": 12000},
            reset=(), expected_revision=owner.read(target, self.fields).revision, observed_at=self.clock.now())
        # A role setting made while QQ was there: the calendar command answers from it.
        role = ConfigTarget(self.qq.scope.installation_id, ConfigLayer.ROLE, self.qq.role_binding.role,
                            bot_id=self.qq.scope.bot_id)
        state = StateConfigurationOwner(self.app.runtime)
        state.write(role, state_fields(), changes={"role.state.calendar_enabled": False}, reset=(),
            expected_revision=state.read(role, state_fields()).revision, observed_at=self.clock.now())
        self.clock.advance(timedelta(seconds=2))
        self.telegram_before = (await self.turn(1, "from Telegram", 3))["context"]
        self.anchor = deepcopy(self.link_record()["anchor"])
        self.shared = ["old", "old-reply", "from QQ", "reply-A", "text tool request", "reply-A",
                       "from Telegram", "reply-B"]
        self.assertEqual(await self.spoken_on(TELEGRAM), self.shared)

    async def telegram_keeps_the_conversation(self):
        """Replies, tools, settings, logs and a reminder on Telegram after QQ was retired."""
        from companion_v2.conversation.linked_ownership import owner_scope
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, target_for
        from companion_v2.model_gateway.core_observations import observations_for_role
        from companion_v2.role_work.state.ownership import role_schedule_key
        self.assertEqual(self.health()[0], ("running", True, None))
        prepared = await self.turn(1, "still here", 10)
        context = prepared["context"]
        # The same conversation and history, on the same stored owners.
        self.assertEqual(await self.manager.get_curr_conversation_id(TELEGRAM), "conversation-a")
        self.assertEqual(await self.spoken_on(TELEGRAM), self.shared + ["still here", "reply-B"])
        self.assertEqual(self.link_record()["anchor"], self.anchor)
        self.assertEqual(owner_scope(self.app.runtime, context.scope), self.qq.scope)
        self.assertEqual(context.actor.subject, self.qq.actor.subject)
        self.assertEqual(role_schedule_key(context), role_schedule_key(self.qq))
        # What QQ contributed is read in place, under Telegram's own permissions, with its source.
        self.assertEqual(self.fact(context, self.tea), "tea")
        self.assertEqual(self.app.memory.writer.access.capture(context, self.qq_source).source.scope.platform, "qq-main")
        archive = self.app.life_context.journal.get_for_audience(self.diary, scope=context.scope,
            recipient=context.actor.subject, observed_at=self.clock.now())
        self.assertEqual((archive.draft.body, archive.draft.context.scope.platform), ("A day on QQ", "qq-main"))
        self.assertEqual({row["source_channel"] for row in observations_for_role(self.app.runtime, context)},
                         {"qq-main", "tg-main"})
        settings = ReplyConfigurationOwner(self.app.runtime).read(target_for(context), self.fields)
        self.assertEqual(settings.fields["conversation.presentation.file_understanding_max_chars"].value, 12000)
        # Text tools: the note made on QQ, edited in place, and a new one.
        _, results = await self.use_tools(1, [("memo.read", {"memo_id": self.note_id}),
            ("memo.edit", {"memo_id": self.note_id, "expected_revision": 1, "changes": {"body": "kept on Telegram"}}),
            ("memo.create", {"title": "coffee", "body": "noted on Telegram"}), ("memo.list", {})], 11)
        self.assertEqual((results[0]["payload"]["body"], results[0]["payload"]["target"]["scope"]["platform"]),
                         ("noted on QQ", "qq-main"))
        self.assertEqual({item["title"] for item in results[3]["payload"]}, {"tea", "coffee"})
        self.assertEqual(self.app.agenda.repository.memo(self.note_id).target.scope, self.qq.scope)
        # A reminder asked for on Telegram arrives on Telegram.
        asked, occurrence = await self.remind(1, "stand up", identifier=12)
        self.assertEqual((await self.settled(asked, occurrence))["delivery"], "delivered")
        self.assertEqual(self.telegram_texts()[-1], (OWNER, "stand up"))
        # No QQ-only capability is offered to the Telegram turn, and none was attempted.
        origin, offered, prompt = self.requests[-1]
        self.assertEqual(origin, TELEGRAM)
        for qq_only in ("poke", "qzone", "presence", "forward", "recall", "relay", "voice", "image", "reaction"):
            self.assertFalse(any(qq_only in name for name in offered), (qq_only, offered))
        for qq_only in ("interaction.poke", "qzone", "relay"):
            self.assertNotIn(qq_only, prompt.lower())
        self.assertEqual(self.telegram.client.onebot_accesses, [])
        return context

    async def telegram_commands_and_controls(self, context):
        """A text command, a session setting and a new conversation, all from Telegram."""
        from astrbot.core import sp
        calls = len(self.calls)
        await self.attempt(1, "生成日程", 13)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((self.sent[-1][0], str(self.sent[-1][1]["chat_id"])), ("tg-main", OWNER))
        self.assertIn("角色日程总开关已关闭", self.sent[-1][1]["text"])
        event, _ = await self.attempt(1, "/set mood calm", 14)
        self.assertFalse(event.is_stopped())
        self.assertEqual(await sp.session_get(TELEGRAM, "session_variables", {}), {"mood": "calm"})
        await self.turn(1, "after the setting", 15)
        kept = await self.spoken_on(TELEGRAM)
        self.assertEqual(kept[-2:], ["after the setting", "reply-B"])
        event, _ = await self.attempt(1, "/new", 16)
        self.assertFalse(event.is_stopped())
        fresh = self.link_record()["conversation_id"]
        self.assertNotEqual(fresh, "conversation-a")
        self.assertEqual(await self.manager.get_curr_conversation_id(TELEGRAM), fresh)
        after = await self.turn(1, "a new conversation", 17)
        self.assertEqual(await self.spoken_on(TELEGRAM), ["a new conversation", "reply-B"])
        # The previous conversation is kept as AstrBot keeps it, and memory is not part of the context.
        previous = await self.manager.get_conversation(TELEGRAM, "conversation-a")
        self.assertEqual([text for text in self.contents(json.loads(previous.history))
                          if text and not text.startswith("{")], kept)
        self.assertEqual(self.fact(after["context"], self.tea), "tea")
        self.assertEqual(self.link_record()["anchor"], self.anchor)
        return fresh


class DisconnectedQQTests(RetiredQQScenario, unittest.IsolatedAsyncioTestCase):
    async def test_telegram_keeps_the_conversation_while_the_qq_client_is_disconnected(self):
        await self.before_retirement()
        self.qq_disconnect()
        self.assertEqual(self.health(), (("running", True, None),
            {"qq-main": (False, "host_account_unavailable"), "tg-main": (True, None)}))
        self.nothing_goes_to_qq_from_here()
        fresh = await self.telegram_commands_and_controls(await self.telegram_keeps_the_conversation())
        self.assertEqual(self.sent_to_qq(), [])
        # QQ is still a member: when its client returns it continues the same conversation.
        self.qq_connect()
        await self.turn(0, "QQ is back", "20")
        self.assertEqual(await self.manager.get_curr_conversation_id(QQ), fresh)
        self.assertEqual(await self.spoken_on(QQ), ["a new conversation", "reply-B", "QQ is back", "reply-A"])

    async def test_telegram_keeps_the_conversation_across_a_restart_without_the_qq_channel(self):
        await self.before_retirement()
        self.native.platforms.pop("qq-main")
        await self.new_process()
        self.assertEqual(self.health()[1]["qq-main"], (False, "host_platform_unavailable"))
        self.assertEqual(self.health()[1]["tg-main"], (True, None))
        await self.telegram_commands_and_controls(await self.telegram_keeps_the_conversation())


class UnlinkedQQTests(RetiredQQScenario, unittest.IsolatedAsyncioTestCase):
    async def test_telegram_keeps_the_conversation_after_qq_is_unlinked(self):
        await self.before_retirement()
        unlinked = await self.link(revision=1, members=["second"])
        self.assertEqual((unlinked["status"], unlinked["data"]["revision"]), ("ok", 2))
        self.nothing_goes_to_qq_from_here()
        await self.telegram_commands_and_controls(await self.telegram_keeps_the_conversation())
        self.assertEqual(self.sent_to_qq(), [])


class NoQQConfiguredTests(RetiredQQScenario, unittest.IsolatedAsyncioTestCase):
    async def test_telegram_keeps_the_conversation_with_qq_removed_from_configuration(self):
        await self.before_retirement()
        await self.remove_qq_from_configuration()
        self.assertEqual(self.health(), (("running", True, None), {"tg-main": (True, None)}))
        fresh = await self.telegram_commands_and_controls(await self.telegram_keeps_the_conversation())
        await self.new_process()
        self.assertEqual(self.health(), (("running", True, None), {"tg-main": (True, None)}))
        prepared = await self.turn(1, "after a restart", 30)
        self.assertEqual(await self.manager.get_curr_conversation_id(TELEGRAM), fresh)
        self.assertEqual((await self.spoken_on(TELEGRAM))[-2:], ["after a restart", "reply-B"])
        self.assertEqual(self.fact(prepared["context"], self.tea), "tea")


class LinkedMappingSaveTests(TelegramAloneFixture, unittest.IsolatedAsyncioTestCase):
    async def test_saving_the_unchanged_account_mapping_of_a_linked_conversation_restarts_the_host(self):
        self.assertEqual((await self.link())["status"], "ok")
        await self.turn(0, "from QQ", "1")
        await self.turn(1, "from Telegram", 2)
        self.controller.save_plugin_config = lambda document: None
        saved = await self.controller.apply_bindings(deepcopy(self.controller.config))
        self.assertTrue(saved["started"], saved)
        self.app = self.controller.session.application
        await self.turn(0, "after the save", "3")
        await self.turn(1, "after the save", 4)
        self.assertEqual((await self.history_seen_from(QQ))[-4:],
                         ["after the save", "reply-A", "after the save", "reply-B"])


if __name__ == "__main__":
    unittest.main()
