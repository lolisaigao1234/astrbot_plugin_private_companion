"""Task-owned conversation permits, released after AstrBot's physical lock barrier.

This module deliberately has no dependency on reloadable companion types. Its
small process-local state survives both entry-point aliases and plugin reloads.
Only explicit linked conversations use it; it never changes an event's origin.
"""
import asyncio
import gc
from contextlib import asynccontextmanager
import sys
from types import ModuleType
from weakref import WeakKeyDictionary

from astrbot.core.platform.astr_message_event import current_message_event
from astrbot.core.utils.session_lock import session_lock_manager

_STATE_NAME = "_private_companion_linked_turns_v1"
_state = sys.modules.setdefault(_STATE_NAME, ModuleType(_STATE_NAME))
if not hasattr(_state, "loops"):
    _state.loops = WeakKeyDictionary()


class LinkedTurnGate:
    def __init__(self):
        self.locks, self.owners, self.cleanups = {}, {}, set()
        self.editing_cids, self.linked_cids, self.history_writers = set(), set(), {}

    @classmethod
    def shared(cls):
        loop = asyncio.get_running_loop()
        if loop not in _state.loops:
            _state.loops[loop] = cls()
        return _state.loops[loop]

    def holds(self, key, event):
        owned = self.owners.get(asyncio.current_task())
        return owned is not None and owned[0] == key and owned[1] is event

    def protects(self, key, event):
        # AstrBot executes provider/tool awaits in child tasks. They may validate
        # their event's live permit, but may never acquire or release one.
        return any(not task.done() and owned[0] == key and owned[1] is event
                   for task, owned in self.owners.items())

    async def acquire(self, key, event):
        """Called only before AstrBot takes its per-origin lock; fail closed.

        Waits as long as the holder runs, like AstrBot's own per-origin lock.
        """
        try:
            task = asyncio.current_task()
            if (task is None or task.get_coro().__qualname__ != "PipelineScheduler.execute"
                    or current_message_event.get() is not event):
                raise RuntimeError("linked turns require AstrBot's pipeline task")
            owned = self.owners.get(task)
            if owned is not None:
                if owned[:2] != (key, event):
                    raise RuntimeError("a pipeline task cannot serve a second linked event")
                return
            lock = self.locks.setdefault(key, asyncio.Lock())
            await lock.acquire()
            # No suspension between acquisition, ownership and callback registration.
            self.owners[task] = (key, event, event.unified_msg_origin, lock)
            task.add_done_callback(self._finished)
        except BaseException:
            event.stop_event()
            raise

    def _finished(self, task):
        cleanup = asyncio.create_task(self._release(task), name="companion:linked-origin-barrier")
        self.cleanups.add(cleanup)
        cleanup.add_done_callback(self.cleanups.discard)

    async def _release(self, task):
        key, event, origin, lock = self.owners[task]
        # A stopped scheduler can abandon an async generator which still owns
        # the native lock. Collect it once; never release the permit on a timer.
        gc.collect()
        async with session_lock_manager.acquire_lock(origin):
            pass
        if self.owners.get(task) == (key, event, origin, lock):
            del self.owners[task]
            lock.release()

    async def drain(self, keys=None):
        """Unload must await admitted tasks AND deferred native-lock cleanup."""
        while True:
            owners = [task for task, owned in self.owners.items() if keys is None or owned[0] in keys]
            if not owners:
                return
            if asyncio.current_task() in owners:
                raise RuntimeError("cannot drain a linked turn from its own hook")
            await asyncio.gather(*(asyncio.shield(task) for task in owners), return_exceptions=True)
            # Let synchronous done callbacks start their barrier tasks.
            await asyncio.sleep(0)
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self.cleanups)))


@asynccontextmanager
async def linked_history_writer(conversation_id):
    """Keep companion room writers out of explicit links, including link creation.

    Unlinked writes keep their existing concurrency. Tracking their lifetime lets
    management refuse a first link until an already running writer is quiescent.
    """
    gate = LinkedTurnGate.shared()
    if conversation_id in gate.linked_cids or conversation_id in gate.editing_cids:
        yield False
        return
    token = object()
    gate.history_writers[token] = conversation_id
    try:
        yield True
    finally:
        gate.history_writers.pop(token, None)
