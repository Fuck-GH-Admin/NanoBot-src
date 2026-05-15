import asyncio
import copy
import logging
from dataclasses import replace
from types import MappingProxyType

from .state import CanonicalState
from .events import ConversationEvent, EventType, MailboxEnvelope
from .reducer import StateReducer
from .store_protocol import EventStoreProtocol


class EpochExpiredError(Exception):
    pass


class SessionActor:
    """单线程、并发隔离的话题边界拥有者 (Timeline Owner)"""

    def __init__(self, session_id: str, store: EventStoreProtocol, initial_state: CanonicalState):
        self.session_id = session_id
        self.store = store
        self._state = initial_state
        self.mailbox: asyncio.Queue[MailboxEnvelope] = asyncio.Queue(maxsize=100)
        self._is_running = False
        self._task: asyncio.Task | None = None
        self.logger = logging.getLogger(f"SessionActor_{session_id}")

    def get_state(self) -> CanonicalState:
        return copy.deepcopy(self._state)

    def start(self) -> None:
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._process_loop())

    async def stop(self) -> None:
        self._is_running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Drain mailbox: resolve all pending ack_futures so callers don't hang.
        while not self.mailbox.empty():
            try:
                envelope = self.mailbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if envelope.ack_future is not None and not envelope.ack_future.done():
                envelope.ack_future.set_exception(
                    asyncio.CancelledError("Actor stopped before processing event")
                )
            self.mailbox.task_done()

    async def enqueue_and_wait(self, event: ConversationEvent) -> CanonicalState:
        """投递并基于 Future 阻塞等待 Actor 消费完成（绝对取代 sleep）"""
        loop = asyncio.get_running_loop()
        ack_future = loop.create_future()
        envelope = MailboxEnvelope(event=event, ack_future=ack_future)

        try:
            await asyncio.wait_for(self.mailbox.put(envelope), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Actor mailbox full for session {self.session_id}. "
                f"Actor may be stalled."
            )
        result_state = await ack_future
        return copy.deepcopy(result_state)

    async def _process_loop(self) -> None:
        while self._is_running:
            try:
                envelope = await asyncio.wait_for(self.mailbox.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                # 0. Session identity guard
                event_to_append = envelope.event
                if event_to_append.session_id != self.session_id:
                    raise ValueError(
                        f"Session ID mismatch: event.session_id={event_to_append.session_id} "
                        f"!= actor.session_id={self.session_id}"
                    )

                # 1. 拦截幽灵回调 (Pre-Append Validation)
                if event_to_append.type in {EventType.TOOL_REQUESTED, EventType.TOOL_SUCCEEDED, EventType.TOOL_FAILED}:
                    claimed_lease = event_to_append.payload.get("driver_lease_id")
                    if claimed_lease != self._state.driver_lease_id:
                        self.logger.warning(
                            f"Ghost execution intercepted! "
                            f"Lease {claimed_lease} != {self._state.driver_lease_id}"
                        )
                        reject_payload = {
                            "reason": "expired_lease_ghost_execution",
                            "original_payload": dict(event_to_append.payload),
                        }
                        event_to_append = replace(
                            event_to_append,
                            type=EventType.TOOL_REJECTED,
                            payload=MappingProxyType(reject_payload),
                        )

                # 2. 注入唯一真相的 Epoch
                next_epoch = (
                    self._state.epoch + 1
                    if event_to_append.type != EventType.TOOL_REJECTED
                    else self._state.epoch
                )

                # Invariant: non-ghost events MUST advance epoch.
                # TOOL_REJECTED keeps epoch (ghosts don't advance timeline).
                assert next_epoch > self._state.epoch or event_to_append.type == EventType.TOOL_REJECTED, (
                    f"Epoch monotonicity violated: "
                    f"next_epoch={next_epoch} <= state.epoch={self._state.epoch} "
                    f"for non-ghost event type={event_to_append.type}"
                )

                epoch_injected = replace(event_to_append, epoch=next_epoch)

                # 3. 经过网关落库 (Validation Pipeline)
                validated_event = await self.store.append_event(epoch_injected)

                # 4. 纯函数状态推导
                self._state = StateReducer.apply(self._state, validated_event)

                # Invariant: after reducer, state.epoch must match the event we just appended.
                assert self._state.epoch == validated_event.epoch, (
                    f"Post-reducer epoch mismatch: "
                    f"state.epoch={self._state.epoch} != event.epoch={validated_event.epoch}"
                )

                # 5. 确认完成 (Ack-based Synchronization)
                if not envelope.ack_future.done():
                    envelope.ack_future.set_result(self._state)

            except asyncio.CancelledError:
                # Actor stopped during processing — notify caller, then propagate
                if not envelope.ack_future.done():
                    envelope.ack_future.set_exception(
                        asyncio.CancelledError("Actor stopped during event processing")
                    )
                raise
            except Exception as e:
                self.logger.error(f"Event processing failed: {e}")
                if not envelope.ack_future.done():
                    envelope.ack_future.set_exception(e)
            finally:
                self.mailbox.task_done()
