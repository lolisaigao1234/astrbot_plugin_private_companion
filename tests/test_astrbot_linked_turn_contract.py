"""Loud upgrade tripwires for the pinned AstrBot 4.29.4 integration assumptions."""
from tests import astrbot_data_root  # noqa: F401

import asyncio
import inspect
import unittest

from astrbot.core.event_bus import EventBus
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.star.star_handler import EventType
from astrbot.core.utils.session_lock import session_lock_manager
from tests.linked_reply_support import LinkedReplyFixture


class AstrBotSourceContractTests(unittest.TestCase):
    def test_waiting_hook_precedes_lock_and_history_build_and_save_is_awaited_inside_lock(self):
        source = inspect.getsource(InternalAgentSubStage.process)
        positions = [source.index(text) for text in (
            "EventType.OnWaitingLLMRequestEvent", "async with session_lock_manager.acquire_lock",
            "await build_main_agent(", "EventType.OnLLMRequestEvent", "await self._save_to_history(")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("if await call_event_hook(event, EventType.OnWaitingLLMRequestEvent):\n                return", source)
        self.assertIn("if not event.is_stopped() or agent_runner.was_aborted():", source)
        self.assertIn("asyncio.create_task(scheduler.execute(event))", inspect.getsource(EventBus.dispatch))

    def test_hook_cancellation_is_swallowed_and_stopped_scheduler_can_defer_generator_close(self):
        self.assertIn("except BaseException:", inspect.getsource(call_event_hook))
        scheduler = inspect.getsource(PipelineScheduler._process_stages)
        self.assertIn("if event.is_stopped():", scheduler)
        self.assertIn("break", scheduler)
        self.assertNotIn("aclose()", scheduler)


class AstrBotRuntimeContractTests(LinkedReplyFixture, unittest.IsolatedAsyncioTestCase):
    async def test_waiting_hook_owns_pipeline_task_before_native_lock_and_stop_prevents_model_and_save(self):
        observed = []
        async def stop_before_build(event):
            async with asyncio.timeout(1):
                async with session_lock_manager.acquire_lock(event.unified_msg_origin):
                    observed.append(asyncio.current_task())
            event.stop_event()
        self.register(EventType.OnWaitingLLMRequestEvent, stop_before_build, priority=2000000)
        event = self.event_for(0)
        task = await self.dispatch(event)
        await asyncio.wait_for(task, 10)
        self.assertEqual(observed, [task])
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.writes, [])
