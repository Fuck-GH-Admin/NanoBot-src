from dataclasses import dataclass
from types import MappingProxyType
from typing import Tuple, Dict, Any

from .state import CanonicalState


@dataclass(frozen=True)
class ActorProjection:
    trust_level: float
    tension_level: float
    narrative_stage: str


@dataclass(frozen=True)
class LogicProjection:
    driver_owner: str
    epoch: int
    driver_lease_id: str


class StateProjector:
    @staticmethod
    def for_actor(state: CanonicalState) -> ActorProjection:
        return ActorProjection(
            trust_level=state.trust_level,
            tension_level=state.tension_level,
            narrative_stage=state.narrative_stage,
        )

    @staticmethod
    def for_logic(state: CanonicalState) -> LogicProjection:
        return LogicProjection(
            driver_owner=state.driver_owner,
            epoch=state.epoch,
            driver_lease_id=state.driver_lease_id,
        )
