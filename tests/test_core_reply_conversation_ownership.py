"""Unlinked QQ keeps its native identity, reply pipeline and stored history."""

from tests import astrbot_data_root  # noqa: F401

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.result_decorate import stage as decorate_module
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_handler import EventType
from astrbot.core.star.context import Context

from astrbot_host.core_reply import (
    CORE_REPLY_CONTEXT_EXTRA, activate_core_reply_delivery_guard, prepare_core_reply_event,
)
from astrbot_host.core_reply_tools import native_tool_name
from companion_v2.conversation.native_history import dialogue
from companion_v2.execution.models import TaskError, require
from companion_v2.memory.models import owner_key
from tests.multi_host_support import MultiFixture, multi_config
from tests.test_core_reply_pipeline import PipelineEvent
from tests.test_main_no_reply import ENTRY


class QQReplyEvent(AiocqhttpMessageEvent):
    def __init__(self, platform, at):
        message = PipelineEvent().message_obj
        message.message_id = "ownership-turn"
        meta = platform.meta()
        meta.support_streaming_message = False
        meta.support_proactive_message = False
        super().__init__("摸摸", message, meta, "200", platform.get_client())
        self.created_at = at.timestamp()
        self.set_extra("activated_handlers", [])


class CoreReplyConversationOwnershipTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.types = replace(self.types, event=QQReplyEvent)
        self.controller = self.make_controller()
        self.db = SQLiteDatabase(str(self.root / "native-history.db"))
        await self.db.initialize()
        self.addAsyncCleanup(self.db.engine.dispose)
        self.manager = ConversationManager(self.db)
        await self.db.create_conversation(
            "qq-main:FriendMessage:200", "qq-main", cid="conversation-a",
            persona_id="original", content=[
                {"role": "user", "content": "以前的消息"},
                {"role": "assistant", "content": "以前的回答"},
            ])
        await self.manager.switch_conversation("qq-main:FriendMessage:200", "conversation-a")
        self.native.conversation_manager = self.manager
        self.channel_send = AsyncMock()
        self.native.platforms["qq-main"].client.send_private_msg = self.channel_send
        self.app = await self.start_host()
        self.plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        self.plugin.staging = self.controller

    def native_event(self):
        return QQReplyEvent(self.native.platforms["qq-main"], self.clock.now())

    async def flush_observations(self):
        observer = self.controller.session.core_reply_observations
        while observer._pending:
            await asyncio.gather(*tuple(observer._pending))

    async def run_native_reply(self, event):
        """Real build, runner, tool dispatch, send and save; only external ports are fake."""
        config = deepcopy(DEFAULT_CONFIG)
        config["t2i"] = False
        config["provider_settings"]["streaming_response"] = False
        config["provider_settings"]["datetime_system_prompt"] = False
        config["provider_settings"]["proactive_capability"] = {"add_cron_tools": False}
        config["agent_runner"]["config"]["persona"]["persona_id"] = "original"
        config["platform_settings"].update(reply_with_mention=False, reply_with_quote=False, reply_prefix="")
        self.native.get_config = lambda **_: deepcopy(config)
        self.native.get_llm_tool_manager = lambda: NS(get_full_tool_set=lambda: ToolSet())
        self.native.subagent_orchestrator = None
        self.native.get_using_tts_provider = lambda _: None
        self.native.get_using_tts_provider_async = AsyncMock(return_value=None)
        requests = []

        async def text_chat(**arguments):
            prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
            self.assertIs(prepared["source_endpoint"].client, event.bot)
            self.assertEqual(prepared["conversation"].unified_msg_origin, "qq-main:FriendMessage:200")
            requests.append({name: deepcopy(arguments.get(name))
                             for name in ("contexts", "session_id", "conversation_id", "model")})
            if len(requests) == 1:
                return LLMResponse(role="assistant", completion_text="", tools_call_ids=["ownership-tool"],
                    tools_call_name=[native_tool_name("turn.evidence")], tools_call_args=[{}])
            return LLMResponse(role="assistant", completion_text="嘿嘿，再摸一下！")

        provider = NS(provider_config={"id": "fake-native-model", "modalities": ["text"]},
            get_model=lambda: "fixture", text_chat=text_chat, meta=lambda: NS(type="fixture"))
        self.native.get_using_provider_async = AsyncMock(return_value=provider)
        # AstrAgentContext requires the SDK Context type. Populate only its
        # service ports, without booting AstrBot or replacing core algorithms.
        core_context = Context.__new__(Context)
        for name in ("conversation_manager", "persona_manager", "get_config", "get_llm_tool_manager",
                     "get_using_tts_provider", "get_using_tts_provider_async", "get_using_provider_async",
                     "get_provider_by_id", "subagent_orchestrator"):
            setattr(core_context, name, getattr(self.native, name))
        ctx = NS(astrbot_config=config, plugin_manager=NS(context=core_context))
        process, decorate, respond = ProcessStage(), decorate_module.ResultDecorateStage(), RespondStage()
        await process.initialize(ctx)
        await decorate.initialize(ctx)
        await respond.initialize(ctx)

        async def hook(current, kind, *args):
            handlers = {
                EventType.OnLLMRequestEvent: self.plugin.prepare_companion_core_request,
                EventType.OnAgentBeginEvent: self.plugin.observe_companion_core_agent_begin,
                EventType.OnAgentDoneEvent: self.plugin.observe_companion_core_agent_done,
                EventType.OnLLMResponseEvent: self.plugin.observe_companion_model_session,
                EventType.OnAfterMessageSentEvent: self.plugin.observe_companion_core_reply_sent,
            }
            if kind in handlers:
                await handlers[kind](current, *args)
            return current.is_stopped()

        handler = NS(handler_module_path="ownership_test", handler_name="guard",
                     handler=self.plugin.guard_companion_core_delivery)
        with patch("astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal.call_event_hook", hook), \
                patch("astrbot.core.astr_agent_hooks.call_event_hook", hook), \
                patch("astrbot.core.pipeline.respond.stage.call_event_hook", hook), \
                patch.object(decorate_module.star_handlers_registry, "get_handlers_by_event_type", return_value=[handler]), \
                patch.dict(decorate_module.star_map, {"ownership_test": NS(name="ownership_test")}), \
                patch("astrbot.core.astr_main_agent.retrieve_knowledge_base", AsyncMock(return_value=None)), \
                patch("astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal._record_internal_agent_stats", AsyncMock()), \
                patch("astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal.Metric.upload", AsyncMock()):
            async for _ in process.process(event):
                async for _ in decorate.process(event):
                    pass
                await respond.process(event)
        await self.flush_observations()
        conversation = await self.manager.get_conversation(event.unified_msg_origin, "conversation-a")
        return requests, json.loads(conversation.history)

    async def test_unlinked_qq_turn_preserves_baseline_through_native_history_save(self):
        event = self.native_event()
        native_fields = (event.message_obj, event.session, event.bot, event.unified_msg_origin)
        await self.plugin.receive_companion_message(event)
        prepared = event.get_extra(CORE_REPLY_CONTEXT_EXTRA)["prepared"]
        admission_keys = set(prepared)
        context = prepared["context"]
        snapshot = {
            "principal": asdict(context.actor.principal), "scope": context.scope.document(),
            "context_key": context.key, "turn_id": prepared["turn_id"],
            "user": context.actor.subject.subject_id, "role": context.role_binding.role.subject_id,
            "instruction": prepared["instruction"],
            "presentation": prepared["presentation_settings"], "segmentation": prepared["segmentation_settings"],
            "memory_owner": owner_key(context),
            "prepared_keys": sorted(set(prepared) - {"source_endpoint", "conversation"}),
        }
        requests, history = await self.run_native_reply(event)
        snapshot["history"] = history
        baseline = json.loads((Path(__file__).resolve().parents[1]
                               / "fixtures/unlinked_qq_core_reply.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot, baseline)
        self.assertEqual(admission_keys - set(baseline["prepared_keys"]), {"source_endpoint", "conversation"})
        self.assertIs(prepared["source_endpoint"].client, event.bot)
        self.assertEqual(prepared["source_endpoint"].scope, context.scope)
        self.assertEqual(prepared["conversation"].scope, context.scope)
        self.assertEqual(len(requests), 2)
        self.assertEqual([(r["session_id"], r["conversation_id"], r["model"]) for r in requests],
                         [("qq-main:FriendMessage:200", "conversation-a", None)] * 2)
        self.assertIn("你是原来的角色", json.dumps(requests[0]["contexts"], ensure_ascii=False))
        self.channel_send.assert_awaited_once_with(user_id=200, message=[
            {"type": "text", "data": {"text": "嘿嘿，再摸一下！"}}])
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "user", "assistant", "tool", "assistant"])
        self.assertEqual([m["content"] for m in history[:2]], ["以前的消息", "以前的回答"])
        self.assertEqual(history[-1]["content"], [{"type": "text", "text": "嘿嘿，再摸一下！"}])
        tool_result = json.loads(history[-2]["content"])
        self.assertEqual(tool_result["state"], "succeeded", tool_result)
        self.assertEqual([(m["speaker"], m["text"]) for m in dialogue(
            self.app.messages, context, self.clock.now(), limit=16)], [("user", "摸摸"), ("assistant", "嘿嘿，再摸一下！")])
        self.assertEqual((event.message_obj, event.session, event.bot, event.unified_msg_origin), native_fields)

    async def test_invalid_client_and_recipient_are_rejected_at_admission(self):
        for changed in ("client", "recipient", "account", "session"):
            with self.subTest(changed=changed):
                event = self.native_event()
                if changed == "client":
                    event.bot = object()
                elif changed == "recipient":
                    event.message_obj.sender.user_id = "201"
                elif changed == "account":
                    event.message_obj.self_id = "999"
                else:
                    event.session.session_id = "201"
                result = await self.controller.prepare_core_reply(event)
                self.assertFalse(result.is_success)
        self.channel_send.assert_not_awaited()

    async def test_prepared_turn_keeps_source_endpoint_separate_from_served_conversation(self):
        event = self.native_event()
        plan = require(await self.controller.prepare_core_reply_context(event))
        prepared = require(await self.controller.prepare_core_reply(event, plan=plan))

        source = prepared["source_endpoint"]
        conversation = prepared["conversation"]
        captured = prepared["native_input"]
        self.assertIs(plan.source_endpoint, source)
        self.assertIs(plan.conversation, conversation)
        self.assertIs(captured.source_endpoint, source)
        self.assertIs(captured.conversation, conversation)
        self.assertIsNot(source, conversation)
        self.assertEqual(source.principal, prepared["context"].actor.principal)
        self.assertEqual(source.scope, prepared["context"].scope)
        self.assertEqual(source.target_id, "first")
        self.assertEqual(source.event_id, "ownership-turn")
        self.assertEqual(source.unified_msg_origin, "qq-main:FriendMessage:200")
        self.assertIs(source.client, event.bot)
        self.assertEqual(conversation.target_id, "first")
        self.assertEqual(conversation.unified_msg_origin, "qq-main:FriendMessage:200")
        self.assertEqual(conversation.scope, prepared["context"].scope)
        self.assertEqual(conversation.role.subject_id, "role-a")
        self.assertTrue((await self.controller.authorize_core_reply(event, prepared)).is_success)

    async def test_source_changes_are_rejected_immediately_before_external_send(self):
        from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType

        for changed in ("client", "recipient", "disconnected"):
            with self.subTest(changed=changed):
                event = self.native_event()
                prepared = require(await self.controller.prepare_core_reply(event))
                prepare_core_reply_event(event, prepared,
                    authorize=lambda value: self.controller.authorize_core_reply(event, value))
                event.set_result(MessageEventResult().message("不应发出").set_result_content_type(
                    ResultContentType.LLM_RESULT))
                activate_core_reply_delivery_guard(event)
                if changed == "client":
                    event.bot = object()
                elif changed == "recipient":
                    event.message_obj.sender.user_id = "201"
                else:
                    client = self.native.platforms["qq-main"].client
                    client._wsr_api_clients.pop("100")
                try:
                    with self.assertRaises(TaskError):
                        await event.send(MessageChain().message("不应发出"))
                finally:
                    if changed == "disconnected":
                        client._wsr_api_clients["100"] = object()
        self.channel_send.assert_not_awaited()


class ExistingWindowLinkCoreTests(MultiFixture, unittest.IsolatedAsyncioTestCase):
    async def test_primary_alias_core_reply_keeps_baseline_target_mismatch(self):
        self.controller = self.make_controller(multi_config(window_links=[
            {"primary_target_id": "first", "alias_target_ids": ["second"], "mode": "primary"}]))
        await self.start_host()
        result = await self.controller.prepare_core_reply(self.event("second"))
        self.assertFalse(result.is_success)
        self.assertEqual(result.failure.code, "host_target_mismatch")

    async def test_role_alias_core_reply_keeps_its_ordinary_native_role(self):
        config = multi_config(window_links=[
            {"primary_target_id": "first", "alias_target_ids": ["second"], "mode": "role"}])
        for persona in config["multi_host"]["personas"]:
            persona["role_id"] = "primary-role" if persona["bot_id"] == "bot-a" else "alias-role"
        self.controller = self.make_controller(config)
        await self.start_host()
        event = self.event("second")
        prepared = require(await self.controller.prepare_core_reply(event))
        self.assertEqual(prepared["context"].scope.platform, "qq-second")
        self.assertEqual(prepared["context"].role_binding.role.subject_id, "alias-role")
        self.assertTrue((await self.controller.authorize_core_reply(event, prepared)).is_success)
