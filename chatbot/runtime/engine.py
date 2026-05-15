import uuid
import asyncio
import logging
from types import MappingProxyType
from typing import Callable, Awaitable, Dict, Any

from .events import ConversationEvent, EventType
from .state import CanonicalState
from .reducer import StateReducer
from .actor import SessionActor
from .store_protocol import EventStoreProtocol
from .projections import StateProjector


class ConversationRuntime:
    """Saga Coordinator / Ingress Adapter (Transitional).

    This is NOT the central authority.
    All mutation authority belongs to Actor -> EventStore -> Reducer.
    """

    def __init__(self, store: EventStoreProtocol):
        self.store = store
        self._actors: Dict[str, SessionActor] = {}
        self._actor_locks: Dict[str, asyncio.Lock] = {}
        self._lock_guard = asyncio.Lock()
        self.logger = logging.getLogger("ConversationRuntime")

    async def shutdown(self) -> None:
        """Stop all active actors. Must be called before discarding the runtime."""
        for actor in self._actors.values():
            await actor.stop()
        self._actors.clear()

    async def _get_or_create_actor(self, session_id: str) -> SessionActor:
        # Get or create per-session lock (under global lock for dict safety)
        async with self._lock_guard:
            if session_id not in self._actor_locks:
                self._actor_locks[session_id] = asyncio.Lock()
            lock = self._actor_locks[session_id]

        # Serialize actor creation per session
        async with lock:
            if session_id in self._actors:
                return self._actors[session_id]
            # Deterministic replay: reconstruct state from event log
            state = CanonicalState(session_id=session_id)
            history_events = await self.store.load_stream(session_id)

            if history_events:
                # Anti-regression: the ONLY invariant the replay core enforces.
                # All other policy (gap detection, duplicate detection, ghost
                # classification) belongs to the Actor Pre-Append Validation
                # or a separate TimelineIntegrityValidator — NOT replay.
                prev_epoch = -1
                for evt in history_events:
                    if evt.epoch < prev_epoch:
                        self.logger.critical(
                            f"Epoch regression in session {session_id}: "
                            f"epoch {evt.epoch} < previous {prev_epoch}."
                        )
                        raise RuntimeError(
                            f"Timeline corruption: epoch {evt.epoch} < {prev_epoch} "
                            f"in session {session_id}"
                        )
                    prev_epoch = evt.epoch

                # Pure deterministic replay — no IO, no side effects
                for evt in history_events:
                    state = StateReducer.apply(state, evt)

                # Invariant: replayed state.epoch must equal last event's epoch.
                if history_events:
                    assert state.epoch == history_events[-1].epoch, (
                        f"Replay epoch mismatch: "
                        f"state.epoch={state.epoch} != last_event.epoch={history_events[-1].epoch}"
                    )

            actor = SessionActor(session_id, self.store, state)
            actor.start()
            self._actors[session_id] = actor
            self.logger.info(
                f"Created actor for session {session_id} "
                f"with {len(history_events)} replayed events, "
                f"epoch={state.epoch}."
            )
        return self._actors[session_id]

    async def process_turn(
        self,
        session_id: str,
        user_text: str,
        logic_runner: Callable[[Any, str], Awaitable[Dict]],
        actor_runner: Callable[[Any, str, Dict], Awaitable[str]],
    ) -> str:
        actor = await self._get_or_create_actor(session_id)
        correlation_id = uuid.uuid4().hex

        def _make_event(
            evt_type: EventType,
            payload: dict,
            source: str,
            causation_id: str = "",
        ) -> ConversationEvent:
            return ConversationEvent(
                event_id=uuid.uuid4().hex,
                correlation_id=correlation_id,
                causation_id=causation_id,
                session_id=session_id,
                epoch=0,  # Placeholder, injected by Actor
                type=evt_type,
                source=source,
                payload=MappingProxyType(payload),
            )

        # 1. 提交 USER_INPUT
        input_event = _make_event(EventType.USER_INPUT, {"text": user_text}, "user")
        await actor.enqueue_and_wait(input_event)

        # 2. 逻辑脑申请 Lease
        lease_id = uuid.uuid4().hex
        lease_event = _make_event(
            EventType.DRIVER_LEASED,
            {"driver_owner": "logic", "driver_lease_id": lease_id},
            "arbiter",
        )
        state_after_lease = await actor.enqueue_and_wait(lease_event)

        logic_view = StateProjector.for_logic(state_after_lease)
        tool_results: Dict = {}
        success = True
        error_msg = ""

        try:
            tool_results = await logic_runner(logic_view, user_text)
        except Exception as exc:
            self.logger.error(f"Logic brain failed: {exc}")
            success = False
            error_msg = str(exc)
            tool_results = {"error": error_msg}

        # 3. 提交 Tool Result（携带 lease_id）
        if success:
            result_event = _make_event(
                EventType.TOOL_SUCCEEDED,
                {"results": tool_results, "driver_lease_id": lease_id},
                "logic_brain",
                causation_id=lease_event.event_id,
            )
        else:
            result_event = _make_event(
                EventType.TOOL_FAILED,
                {"error": error_msg, "driver_lease_id": lease_id},
                "logic_brain",
                causation_id=lease_event.event_id,
            )
        await actor.enqueue_and_wait(result_event)

        # 4. 强制释放 Lease
        release_event = _make_event(
            EventType.DRIVER_RELEASED,
            {},
            "arbiter",
            causation_id=lease_event.event_id,
        )
        state_after_release = await actor.enqueue_and_wait(release_event)

        # 5. 演员脑执行
        actor_view = StateProjector.for_actor(state_after_release)
        reply = await actor_runner(actor_view, user_text, tool_results)

        return reply
