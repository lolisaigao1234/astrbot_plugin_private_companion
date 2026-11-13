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
from astrbot.core.message.components import At, Plain
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.context import Context
from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter, EventMessageType
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry

from tests.multi_host_support import MultiFixture, multi_config
from tests.test_main_no_reply import ENTRY
from tests.test_telegram_private_reply import OWNER as TELEGRAM_OWNER, TelegramPlatform, telegram_event

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
        self.steered = ORIGINS[0]

    async def get_conversation(self, umo, cid, *args, **kwargs):
        self.reads.append((umo, cid))
        return await super().get_conversation(umo, cid, *args, **kwargs)

    async def update_conversation(self, umo, cid, **kwargs):
        self.tasks.append(asyncio.current_task())
        if self.pause_save and umo == self.steered:
            self.saving.set()
            await self.allow_save.wait()
        if self.save_error and umo == self.steered:
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
    def __init__(self, platform, at, text, identifier="7", group=None, sender="200"):
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        message.self_id = "100" if platform.meta().id == "qq-main" else "101"
        message.sender = MessageMember(sender, "Synthetic owner")
        message.message_id, message.message, message.raw_message = identifier, [Plain(text)], {"post_type": "message"}
        if group:
            message.group_id = group
            message.message.insert(0, At(qq=message.self_id))
        meta = platform.meta()
        meta.support_streaming_message = False
        meta.support_proactive_message = False
        super().__init__(text, message, meta, group or sender, platform.get_client())
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
    origins = ORIGINS
    event_type = LinkedQQEvent

    async def asyncSetUp(self):
        self.types = replace(self.types, event=self.event_type, session_persona_override_lookup=None)
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
        for origin, cid in zip(self.origins, ("conversation-a", "conversation-b")):
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
        # The origin whose turn a test pauses or fails; replies stay "reply-A" for the
        # first endpoint and "reply-B" for the second unless a test supplies its own.
        self.steered, self.replies = self.origins[0], {}
        self.tool_call = None  # (name, arguments) for the "tool" mode; default reads the turn's evidence
        self.tool_script = []  # synthetic provider's successive tool responses, optionally using prior results
        self.tool_call_ids = {}

        async def text_chat(**args):
            self.calls.append({key: deepcopy(args.get(key)) for key in
                               ("contexts", "prompt", "session_id", "conversation_id", "model")})
            origin = args["session_id"]
            if self.pause_model and origin == self.steered:
                self.model_done.set()
                await self.allow_model.wait()
            self.model_rounds[origin] = self.model_rounds.get(origin, 0) + 1
            step = self.model_rounds[origin] - 1
            if origin == self.steered and step < len(self.tool_script):
                response = self.tool_script[step]
                name, arguments = response(args) if callable(response) else response
                return LLMResponse(role="assistant", completion_text="", tools_call_ids=[self.tool_call_ids.get(step, f"script-call-{step}")],
                    tools_call_name=[name], tools_call_args=[deepcopy(arguments)])
            if origin == self.steered and self.model_rounds[origin] == 1:
                if self.provider_mode == "provider_error":
                    return LLMResponse(role="err", completion_text="synthetic provider error")
                if self.provider_mode in ("tool", "tool_failure"):
                    from astrbot_host.core_reply_tools import native_tool_name
                    name, arguments = self.tool_call or (native_tool_name("turn.evidence")
                        if self.provider_mode == "tool" else "synthetic_failure", {})
                    return LLMResponse(role="assistant", completion_text="", tools_call_ids=["synthetic-tool-call"],
                        tools_call_name=[name], tools_call_args=[deepcopy(arguments)])
            if self.pause_final_model and origin == self.steered:
                self.final_model_done.set()
                await self.allow_final_model.wait()
            if self.model_error and origin == self.steered:
                raise self.model_error
            return LLMResponse(role="assistant", completion_text=self.replies.get(origin,
                "reply-A" if origin == self.origins[0] else "reply-B"))

        async def text_chat_stream(**args):
            self.stream_calls.append(args["session_id"])
            response = await text_chat(**args)
            if response.role == "assistant" and not response.tools_call_name:
                yield LLMResponse(role="assistant", completion_text="reply-", is_chunk=True)
                yield LLMResponse(role="assistant", completion_text="A" if args["session_id"] == self.origins[0] else "B", is_chunk=True)
            yield response

        self.provider = NS(provider_config={"id": "native-model", "modalities": ["text", "tool_use"]},
            get_model=lambda: "fixture", text_chat=text_chat, text_chat_stream=text_chat_stream, meta=lambda: NS(type="fixture"))
        async def provider(*_, **__):
            return self.provider
        self.native.get_using_provider_async = provider
        for platform_id, platform in self.native.platforms.items():
            async def send(*, _platform_id=platform_id, **kwargs):
                if self.send_error and _platform_id == self.steered.split(":")[0]:
                    raise OSError("synthetic send error")
                self.sent.append((_platform_id, kwargs))
                return {"message_id": len(self.sent)}
            if platform.meta().name == "telegram":
                platform.client.send_message = send
            else:
                platform.client.send_private_msg = platform.client.send_group_msg = send
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
        # AstrBot's own session send finds its platform among the synthetic ones.
        core_context.platform_manager = type("Platforms", (), {
            "platform_insts": property(lambda _: list(self.native.platforms.values()))})()
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
            if self.pause_after_response and event.unified_msg_origin == self.steered:
                self.model_done.set()
                await self.allow_model.wait()
        self.register(EventType.OnLLMResponseEvent, pause_completed_model, priority=2000000)
        self.requests = []
        async def record_final_request(event, req):
            # After every other request hook: what AstrBot's runner is about to use.
            self.requests.append((event.unified_msg_origin, sorted(req.func_tool.names()) if req.func_tool else [],
                                  req.system_prompt or ""))
        self.register(EventType.OnLLMRequestEvent, record_final_request, priority=-2000000)
        self.register(EventType.AdapterMessageEvent, self.plugin.guard_linked_companion_commands,
            [EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE)], priority=1000000)
        self.register(EventType.AdapterMessageEvent, self.plugin.settle_linked_companion_control,
            [EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE)], priority=-1)
        self.register(EventType.OnDecoratingResultEvent, self.plugin.observe_companion_context_reset, priority=100000)
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

    def steer(self, index):
        """Pauses and failures apply to the turn arriving on this endpoint."""
        self.steered = self.manager.steered = self.origins[index]

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

    def event_for(self, index, text=None, identifier="7", group=None, sender=None):
        # The sender defaults to the participant of the endpoint the event arrives on.
        return LinkedQQEvent(self.native.platforms[self.origins[index].split(":")[0]], self.clock.now(),
            text or ("A" if index == 0 else "B"), identifier, group,
            sender or self.origins[index].split(":")[-1])

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

    async def held_at_permit(self, event):
        """Dispatch a linked event behind a running turn: it queues and loads nothing."""
        reads, calls = len(self.manager.reads), len(self.calls)
        task = await self.dispatch(event)
        await self.queued_for_permit(event)
        # An admitted turn reads the conversation in the step after its permit.
        await asyncio.sleep(.05)
        self.assertEqual((len(self.manager.reads), len(self.calls)), (reads, calls))
        return task

    async def history(self):
        conversation = await self.manager.get_conversation(self.origins[0], "conversation-a")
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

    def builtin(self, command, handler, name=None, module="astrbot.builtin_stars.builtin_commands.main"):
        from astrbot.core.star.filter.command import CommandFilter
        if module not in star_map:
            star_map[module] = StarMetadata(name=module.split(".")[-2], module_path=module,
                reserved=module.startswith("astrbot."))
            self.addCleanup(star_map.pop, module, None)
        metadata = StarHandlerMetadata(EventType.AdapterMessageEvent, module + "." + command,
            name or handler.__name__, module, handler, [])
        command_filter = CommandFilter(command)
        command_filter.init_handler_md(metadata)
        metadata.event_filters = [command_filter]
        star_handlers_registry.append(metadata)
        self.addCleanup(star_handlers_registry.remove, metadata)
        return metadata


class SyntheticTelegramChannel(TelegramPlatform, TelegramPlatformAdapter):
    """A synthetic application and client behind the channel's own metadata and session sender."""
    config = {"id": "tg-main"}
    meta = TelegramPlatformAdapter.meta


class LinkedTelegramFixture(LinkedReplyFixture):
    """The same real path with the second endpoint on AstrBot's Telegram channel.

    Only the Telegram application and its client are synthetic; the event is
    AstrBot's own TelegramPlatformEvent, which formats and sends by itself.
    """
    origins = (ORIGINS[0], f"tg-main:FriendMessage:{TELEGRAM_OWNER}")
    event_type = AstrMessageEvent

    def make_controller(self, config=None):
        if not hasattr(self, "telegram"):
            # Once per test: a channel a test removes stays removed across a restart.
            self.telegram = self.native.platforms["tg-main"] = SyntheticTelegramChannel()
        config = config or multi_config()
        config["multi_host"]["targets"][1] = dict(target_id="second", platform_id="tg-main",
            account_id="tg-main", bot_id="bot-tg", participant_id=str(TELEGRAM_OWNER))
        config["multi_host"]["personas"] = [row for row in config["multi_host"]["personas"]
            if row["bot_id"] == "bot-a"] + [dict(bot_id="bot-tg", persona_id="original", role_id="role-a")]
        return super().make_controller(config)

    def event_for(self, index, text=None, identifier="7", group=None, sender=None, **telegram_fields):
        if index != 1:
            return super().event_for(index, text, identifier, group, sender)
        # Telegram message ids are integers; a named synthetic id keeps one number.
        numbers = self.__dict__.setdefault("telegram_message_numbers", {})
        number = int(identifier) if str(identifier).isdigit() else numbers.setdefault(identifier, 1000 + len(numbers))
        event = telegram_event(self.telegram, identifier=number, sender=int(sender or TELEGRAM_OWNER),
                               text=text or "B", **telegram_fields)
        event.created_at = self.clock.now().timestamp()
        return event
