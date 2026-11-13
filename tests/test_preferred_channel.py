"""Reminders and proactive text of a linked conversation leave through its preferred endpoint."""
from tests import astrbot_data_root  # noqa: F401

import asyncio
from datetime import timedelta
import json
import unittest

from astrbot_host.core_reply import CORE_REPLY_CONTEXT_EXTRA
from astrbot_host.core_reply_tools import native_tool_name
from companion_v2.agenda.models import AgendaTarget
from companion_v2.execution.models import require
from tests.linked_reply_support import LinkedTelegramFixture, TELEGRAM_OWNER
from tests.multi_host_support import multi_config


class PreferredChannelFixture(LinkedTelegramFixture):
    """QQ (first) and Telegram (second) on the real reply path, with the host's own senders."""

    def make_controller(self, config=None):
        from companion_v2.adapters.astrbot.staging_config import REQUIRED_CAPABILITIES
        from companion_v2.conversation.agenda_tools import TOOL_FIELDS
        config = config or multi_config(capabilities=sorted(
            REQUIRED_CAPABILITIES | set(TOOL_FIELDS) | {"reminder.deliver", "memo.reminder.set"}))
        return super().make_controller(config)

    async def turn(self, index, text, identifier):
        event = self.event_for(index, text, identifier)
        await asyncio.wait_for(await self.dispatch(event), 30)
        await self.controller.session.linked_conversations.gate.drain()
        prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        await prepared["native_source_future"]
        return prepared

    async def remind(self, index, message, *, identifier, minutes=10):
        """The human asks for a reminder on this endpoint; the model uses the existing tool."""
        self.steer(index)
        self.model_rounds.clear()
        due = self.clock.now() + timedelta(minutes=minutes)
        self.tool_script = [(native_tool_name("reminder.create"),
                             {"message": message, "schedule": {"anchor": due.isoformat(), "timezone_name": "UTC"}})]
        context = (await self.turn(index, "remind me", identifier))["context"]
        cid = await self.manager.get_curr_conversation_id(self.origins[index])
        history = json.loads((await self.manager.get_conversation(self.origins[index], cid)).history)
        result = [json.loads(entry["content"]) for entry in history if entry["role"] == "tool"][-1]
        self.assertEqual(result["state"], "succeeded", result)
        self.tool_script = []
        occurrence = self.app.runtime.read_one("SELECT occurrence_id FROM agenda_occurrences WHERE reminder_id = ?",
                                               (result["payload"]["object_id"],))["occurrence_id"]
        return context, occurrence

    def status(self, context, occurrence):
        """The reminder's scheduling and delivery report, as its creator may read it."""
        target = AgendaTarget(context.scope, context.role_binding.role, context.actor.subject)
        return require(self.app.agenda_background.observe(context.actor, target=target, occurrence_id=occurrence,
                                                          observed_at=self.clock.now()))

    async def settled(self, context, occurrence, *, minutes=10):
        """Let the reminder come due and wait until the host has recorded what became of it."""
        self.clock.advance(timedelta(minutes=minutes, seconds=1))
        async with asyncio.timeout(30):
            while True:
                self.app.notify()
                report = self.status(context, occurrence)
                if report["scheduling"] in ("skipped", "blocked") or report["delivery"] not in (
                        "not_started", "pending", "dispatched"):
                    return report
                self.assertEqual(self.app.state, "running", self.app.failure_code)
                await asyncio.sleep(0.02)

    def telegram_texts(self):
        return [(str(sent["chat_id"]), sent["text"]) for platform, sent in self.sent if platform == "tg-main"]

    def qq_calls(self):
        return self.native.platforms["qq-main"].client._api._wsr_api.calls


class PreferredReminderTests(PreferredChannelFixture, unittest.IsolatedAsyncioTestCase):
    async def test_a_reminder_created_on_qq_before_linking_is_delivered_once_through_telegram(self):
        context, occurrence = await self.remind(0, "drink water", identifier="before-link")
        self.assertEqual((await self.link())["status"], "ok")
        self.sent.clear()
        self.qq_calls().clear()
        report = await self.settled(context, occurrence)
        self.assertEqual(report["delivery"], "delivered", report)
        self.assertEqual(self.telegram_texts(), [(str(TELEGRAM_OWNER), "drink water")])
        self.assertEqual(self.qq_calls(), [])

    async def restart(self):
        await self.controller.terminate()
        self.controller = self.make_controller()
        self.plugin.staging = self.controller
        result = await self.controller.start()
        self.assertTrue(result.is_success, result.failure)
        self.app = self.controller.session.application

    async def idle(self):
        """Give the host's background loop several passes over whatever is due."""
        for _ in range(5):
            self.app.notify()
            await asyncio.sleep(0.05)
        self.assertEqual(self.app.state, "running", self.app.failure_code)

    async def test_a_delivered_reminder_is_not_sent_again_after_a_restart_or_through_the_other_endpoint(self):
        self.assertEqual((await self.link())["status"], "ok")
        context, occurrence = await self.remind(0, "stretch", identifier="linked")
        self.sent.clear()
        self.assertEqual((await self.settled(context, occurrence))["delivery"], "delivered")
        await self.restart()
        await self.idle()
        self.assertEqual(self.status(context, occurrence)["delivery"], "delivered")
        self.assertEqual(self.telegram_texts(), [(str(TELEGRAM_OWNER), "stretch")])
        self.assertEqual(self.qq_calls(), [])

    def qq_texts(self):
        """Private messages the QQ account was asked to send, with their recipient."""
        return [(str(params["user_id"]), "".join(part["data"]["text"] for part in params["message"]
                                                 if part["type"] == "text"))
                for action, params in self.qq_calls() if action == "send_private_msg"]

    async def test_telegram_is_preferred_at_first_and_an_administrator_can_select_qq(self):
        created = await self.link()
        self.assertEqual((created["status"], created["data"]["preferred_target_id"]), ("ok", "second"))
        chosen = await self.link(revision=1, preferred_target_id="first")
        self.assertEqual((chosen["status"], chosen["data"]["preferred_target_id"], chosen["data"]["revision"]),
                         ("ok", "first", 2))
        context, occurrence = await self.remind(1, "call home", identifier="from-telegram")
        self.sent.clear()
        self.qq_calls().clear()
        self.assertEqual((await self.settled(context, occurrence))["delivery"], "delivered")
        self.assertEqual(self.qq_texts(), [("200", "call home")])
        self.assertEqual(self.telegram_texts(), [])
