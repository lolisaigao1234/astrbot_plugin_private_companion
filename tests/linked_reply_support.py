"""Offline real AstrBot event-bus harness; no core patches or replacement stages."""
from tests import astrbot_data_root  # noqa: F401

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os

os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
from types import SimpleNamespace as NS

from astrbot.core import sp
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.umop_config_router import UmopConfigRouter
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.event_bus import EventBus
from astrbot.core.message.components import Plain
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.context import Context
from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter, EventMessageType
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry

from tests.multi_host_support import MultiFixture
from tests.test_main_no_reply import ENTRY

NAME = "linked_reply_test"
ORIGINS = ("qq-main:FriendMessage:200", "qq-second:FriendMessage:200")


class RecordedConversations(ConversationManager):
    """Instrumentation only: every read/write delegates to the real temp SQLite store."""
    def __init__(self, db):
        super().__init__(db)
        self.reads, self.writes, self.tasks = [], [], []
        self.saving, self.allow_save = asyncio.Event(), asyncio.Event()
        self.pause_save = False
        self.save_error = False

    async def get_conversation(self, umo, cid, *args, **kwargs):
        self.reads.append((umo, cid))
        return await super().get_conversation(umo, cid, *args, **kwargs)

    async def update_conversation(self, umo, cid, **kwargs):
        self.tasks.append(asyncio.current_task())
        if self.pause_save and umo == ORIGINS[0]:
            self.saving.set()
            await self.allow_save.wait()
        if self.save_error and umo == ORIGINS[0]:
            raise OSError("synthetic save failure")
        result = await super().update_conversation(umo, cid, **kwargs)
        self.writes.append((umo, cid, deepcopy(kwargs.get("history"))))
        return result


class SyntheticFailureTool(FunctionTool):
    def __init__(self):
        super().__init__(name="synthetic_failure", description="Synthetic failing channel-side operation",
            parameters={"type": "object", "properties": {}})
        self.calls = 0

    async def call(self, context, **kwargs):
        self.calls += 1
        raise RuntimeError("synthetic tool failure")


class LinkedQQEvent(AiocqhttpMessageEvent):
    def __init__(self, platform, at, text, identifier="7"):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "100" if platform.meta().id == "qq-main" else "101"
        message.sender = MessageMember("200", "Synthetic owner")
        message.message_id, message.message, message.raw_message = identifier, [Plain(text)], {"post_type": "message"}
        meta = platform.meta()
        meta.support_streaming_message = False
        meta.support_proactive_message = False
        super().__init__(text, message, meta, "200", platform.get_client())
        self.created_at = at.timestamp()
        self.stream_invocations = 0
        self.transport_chunks = []

    async def send_streaming(self, generator, use_fallback=False):
        self.stream_invocations += 1
        if not self.get_extra("synthetic_stream_transport"):
            return await super().send_streaming(generator, use_fallback)
        # Fake transport consumes real RespondStage/run_agent chunks as they arrive.
        async for chain in generator:
            if chain.chain:
                self.transport_chunks.append(chain.get_plain_text())
                await self.send(chain)



class LinkedReplyFixture(MultiFixture):
    async def asyncSetUp(self):
        self.types = replace(self.types, event=LinkedQQEvent, session_persona_override_lookup=None)
        self.config = deepcopy(DEFAULT_CONFIG)
        self.config["t2i"] = False
        self.config["plugin_set"] = [NAME]
        self.config["provider_settings"].update(streaming_response=False, datetime_system_prompt=False,
            proactive_capability={"add_cron_tools": False})
        self.config["agent_runner"]["config"]["persona"]["persona_id"] = "original"
        self.config["platform_settings"].update(reply_with_mention=False, reply_with_quote=False, reply_prefix="",
            enable_id_white_list=False, enable_rate_limit=False)
        self.db = SQLiteDatabase(str(self.root / "astrbot.db"))
        await self.db.initialize()
        self.addAsyncCleanup(self.db.engine.dispose)
        self.manager = RecordedConversations(self.db)
        for origin, cid in zip(ORIGINS, ("conversation-a", "conversation-b")):
            await self.db.create_conversation(origin, origin.split(":")[0], cid=cid, persona_id="original",
                content=[{"role": "user", "content": "old" if cid == "conversation-a" else "separate-old"},
                         {"role": "assistant", "content": "old-reply" if cid == "conversation-a" else "separate-reply"}])
            await self.manager.switch_conversation(origin, cid)
        self.native.conversation_manager = self.manager
        router = UmopConfigRouter(sp)
        await router.initialize()
        await router.update_routing_data({})
        self.acm = AstrBotConfigManager(self.config, router, sp)
        await self.acm.initialize()
        self.native.get_config = lambda umo=None: self.acm.get_conf(umo)
        acm = self.acm
        self.native.persona_manager = PersonaManager(self.db, acm)
        await self.db.insert_persona(persona_id="original", system_prompt="你是原来的角色。")
        await self.db.insert_persona(persona_id="second", system_prompt="你是第二个人格。")
        await self.native.persona_manager.initialize()
        self.native.subagent_orchestrator = None
        self.tools = FunctionToolManager()
        self.failure_tool = SyntheticFailureTool()
        self.tools.func_list.append(self.failure_tool)
        self.native.get_llm_tool_manager = lambda: self.tools
        self.native.get_using_tts_provider = lambda _: None
        async def no_tts(*_):
            return None
        self.native.get_using_tts_provider_async = no_tts
        self.calls, self.sent, self.stream_calls = [], [], []
        self.model_done, self.allow_model = asyncio.Event(), asyncio.Event()
        self.pause_model = False
        self.pause_after_response = False
        self.model_error = None
        self.provider_mode = "normal"
        self.send_error = False
        self.model_rounds = {}
        self.final_model_done, self.allow_final_model = asyncio.Event(), asyncio.Event()
        self.pause_final_model = False

        async def text_chat(**args):
            self.calls.append({key: deepcopy(args.get(key)) for key in
                               ("contexts", "prompt", "session_id", "conversation_id", "model")})
            origin = args["session_id"]
            if self.pause_model and origin == ORIGINS[0]:
                self.model_done.set()
                await self.allow_model.wait()
            self.model_rounds[origin] = self.model_rounds.get(origin, 0) + 1
            if origin == ORIGINS[0] and self.model_rounds[origin] == 1:
                if self.provider_mode == "provider_error":
                    return LLMResponse(role="err", completion_text="synthetic provider error")
                if self.provider_mode in ("tool", "tool_failure"):
                    from astrbot_host.core_reply_tools import native_tool_name
                    return LLMResponse(role="assistant", completion_text="", tools_call_ids=["synthetic-tool-call"],
                        tools_call_name=[native_tool_name("turn.evidence") if self.provider_mode == "tool"
                                         else "synthetic_failure"], tools_call_args=[{}])
            if self.pause_final_model and origin == ORIGINS[0]:
                self.final_model_done.set()
                await self.allow_final_model.wait()
            if self.model_error and origin == ORIGINS[0]:
                raise self.model_error
            return LLMResponse(role="assistant", completion_text="reply-A" if origin == ORIGINS[0] else "reply-B")

        async def text_chat_stream(**args):
            self.stream_calls.append(args["session_id"])
            response = await text_chat(**args)
            if response.role == "assistant" and not response.tools_call_name:
                yield LLMResponse(role="assistant", completion_text="reply-", is_chunk=True)
                yield LLMResponse(role="assistant", completion_text="A" if args["session_id"] == ORIGINS[0] else "B", is_chunk=True)
            yield response

        self.provider = NS(provider_config={"id": "native-model", "modalities": ["text"]},
            get_model=lambda: "fixture", text_chat=text_chat, text_chat_stream=text_chat_stream, meta=lambda: NS(type="fixture"))
        async def provider(*_, **__):
            return self.provider
        self.native.get_using_provider_async = provider
        for platform_id, platform in self.native.platforms.items():
            async def send(*, _platform_id=platform_id, **kwargs):
                if self.send_error and _platform_id == "qq-main":
                    raise OSError("synthetic send error")
                self.sent.append((_platform_id, kwargs))
                return {"message_id": len(self.sent)}
            platform.client.send_private_msg = send
        self.controller = self.make_controller()
        self.app = await self.start_host()
        self.reply_settings(private_debounce_enabled=False)
        self.plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        self.plugin.staging = self.controller
        core_context = Context.__new__(Context)
        for name in ("conversation_manager", "persona_manager", "get_llm_tool_manager",
                     "get_using_tts_provider", "get_using_tts_provider_async", "get_using_provider_async",
                     "get_provider_by_id", "subagent_orchestrator"):
            setattr(core_context, name, getattr(self.native, name))
        core_context._config = self.config
        core_context.astrbot_config_mgr = self.acm
        core_context.kb_manager = None
        self.core_context = core_context
        # Register exactly as a plugin loader does. No patched registry/dispatch.
        star_map[NAME] = StarMetadata(name=NAME, module_path=NAME)
        self.addCleanup(star_map.pop, NAME, None)
        self.handlers = []
        self.register(EventType.AdapterMessageEvent, self.plugin.receive_companion_message,
            [EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE)], priority=-230000)
        for kind, method in (
            (EventType.OnLLMRequestEvent, self.plugin.prepare_companion_core_request),
            (EventType.OnAgentBeginEvent, self.plugin.observe_companion_core_agent_begin),
            (EventType.OnAgentDoneEvent, self.plugin.observe_companion_core_agent_done),
            (EventType.OnLLMResponseEvent, self.plugin.observe_companion_model_session),
            (EventType.OnDecoratingResultEvent, self.plugin.guard_companion_core_delivery),
            (EventType.OnAfterMessageSentEvent, self.plugin.observe_companion_core_reply_sent),
        ):
            self.register(kind, method)
        async def pause_completed_model(event, response):
            if self.pause_after_response and event.unified_msg_origin == ORIGINS[0]:
                self.model_done.set()
                await self.allow_model.wait()
        self.register(EventType.OnLLMResponseEvent, pause_completed_model, priority=2000000)
        self.register(EventType.AdapterMessageEvent, self.plugin.guard_linked_companion_commands,
            [EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE)], priority=1000000)
        self.register(EventType.OnLLMRequestEvent, self.plugin.check_linked_companion_permit, priority=1000000)
        self.register(EventType.OnWaitingLLMRequestEvent, self.plugin.admit_linked_companion_turn, priority=1000000)
        self.scheduler = PipelineScheduler(PipelineContext(self.config, NS(context=core_context), "default", self.db))
        await self.scheduler.initialize()
        self.queue = asyncio.Queue()
        self.bus = EventBus(self.queue, {"default": self.scheduler}, self.acm)
        self.dispatcher = asyncio.create_task(self.bus.dispatch())
        self.addAsyncCleanup(self.close_bus)

    def reply_settings(self, **changes):
        from companion_v2.conversation.reply_settings import ReplyConfigurationOwner, configuration_fields, target_for
        owner = ReplyConfigurationOwner(self.app.runtime)
        common = self.controller.settings.common
        fields = configuration_fields(common.segmentation, common.streaming, common.policy.part_gap_seconds)
        for target_id in ("first", "second"):
            target = target_for(self.controller.authority.context(target_id, "role-a"))
            owner.write(target, fields,
                changes={"conversation.presentation." + name: value for name, value in changes.items()},
                reset=(), expected_revision=owner.read(target, fields).revision, observed_at=self.clock.now())

    def register(self, kind, handler, filters=None, priority=0):
        metadata = StarHandlerMetadata(kind, NAME + "." + handler.__name__, handler.__name__, NAME,
            handler, filters or [], extras_configs={"priority": priority})
        star_handlers_registry.append(metadata)
        self.handlers.append(metadata)
        self.addCleanup(star_handlers_registry.remove, metadata)
        return metadata

    async def close_bus(self):
        self.manager.allow_save.set()
        self.allow_model.set()
        self.allow_final_model.set()
        self.dispatcher.cancel()
        await asyncio.gather(self.dispatcher, return_exceptions=True)
        for task in tuple(self.bus._pending_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.bus._pending_tasks), return_exceptions=True)

    def event_for(self, index, text=None, identifier="7"):
        return LinkedQQEvent(self.native.platforms[ORIGINS[index].split(":")[0]], self.clock.now(),
            text or ("A" if index == 0 else "B"), identifier)

    async def dispatch(self, event):
        await self.queue.put(event)
        for _ in range(1000):
            for task, current in self.bus._pending_tasks.items():
                if current is event:
                    return task
            await asyncio.sleep(0)
        self.fail("EventBus did not create the pipeline task")

    async def queued_for_permit(self, event):
        # The stamp is written in the same step that starts waiting for the permit.
        from astrbot_host.linked_conversations import EXTRA
        async with asyncio.timeout(10):
            while event.get_extra(EXTRA) is None:
                await asyncio.sleep(.01)

    async def history(self):
        conversation = await self.manager.get_conversation(ORIGINS[0], "conversation-a")
        return json.loads(conversation.history)

    @staticmethod
    def contents(history):
        def text(content):
            return content if isinstance(content, str) else "".join(p.get("text", "") for p in (content or []))
        return [text(message["content"]) for message in history]

    async def link(self, *, revision=0, members=None, **changes):
        from astrbot_host.account_session_api import AccountSessionWebAPI
        from unittest.mock import patch
        body = dict(schema_version=1, link_id="owner-dialogue", expected_revision=revision,
            conversation_id="conversation-a", source_target_id="first",
            member_target_ids=members if members is not None else ["first", "second"])
        body.update(changes)
        async def request_body():
            return json.dumps(body).encode()
        with patch("astrbot_host.account_session_api.request", NS(username="owner", query={}, body=request_body)):
            return await AccountSessionWebAPI(self.controller).link_conversation()

    def builtin(self, command, handler, name=None):
        from astrbot.core.star.filter.command import CommandFilter
        module = "astrbot.builtin_stars.builtin_commands.main"
        if module not in star_map:
            star_map[module] = StarMetadata(name="builtin_commands", module_path=module, reserved=True)
            self.addCleanup(star_map.pop, module, None)
        metadata = StarHandlerMetadata(EventType.AdapterMessageEvent, module + "." + command,
            name or handler.__name__, module, handler, [])
        command_filter = CommandFilter(command)
        command_filter.init_handler_md(metadata)
        metadata.event_filters = [command_filter]
        star_handlers_registry.append(metadata)
        self.addCleanup(star_handlers_registry.remove, metadata)
        return metadata
