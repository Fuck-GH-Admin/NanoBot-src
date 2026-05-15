from dataclasses import replace

from .state import CanonicalState
from .events import ConversationEvent, EventType

# Fields owned by Actor lifecycle. STATE_PATCHED MUST NOT mutate these.
# Mutating them would bypass ghost fence and epoch authority.
_ACTOR_OWNED_FIELDS = frozenset({"epoch", "session_id", "driver_owner", "driver_lease_id"})


class StateReducer:
    """Pure function: state + event -> new_state.

    No IO. No validation. No side effects. Deterministic.
    All validation (ACL, lease, dedupe) MUST happen in EventStore.
    """

    @staticmethod
    def apply(state: CanonicalState, event: ConversationEvent) -> CanonicalState:
        new_state = replace(state)

        if event.type == EventType.STATE_PATCHED:
            for key, value in event.payload.items():
                if key in _ACTOR_OWNED_FIELDS:
                    continue  # Actor-owned fields are not patchable
                if hasattr(new_state, key):
                    object.__setattr__(new_state, key, value)

        elif event.type == EventType.DRIVER_LEASED:
            new_state.driver_owner = event.payload.get("driver_owner", "")
            new_state.driver_lease_id = event.payload.get("driver_lease_id", "")

        elif event.type == EventType.DRIVER_RELEASED:
            new_state.driver_owner = "actor"
            new_state.driver_lease_id = ""

        # Epoch must track the latest event's position on the timeline.
        # Without this, state.epoch lags behind the store after non-DRIVER_LEASED
        # events, causing duplicate epochs on subsequent turns.
        new_state.epoch = event.epoch

        # Invariant: reducer output epoch always equals input event epoch.
        # Reducer copies, never generates.
        assert new_state.epoch == event.epoch, (
            f"Reducer epoch invariant violated: "
            f"output.epoch={new_state.epoch} != event.epoch={event.epoch}"
        )

        return new_state
