from dataclasses import dataclass


@dataclass
class CanonicalState:
    session_id: str
    version: int = 0
    epoch: int = 0
    driver_owner: str = "actor"
    driver_lease_id: str = ""
    trust_level: float = 50.0
    tension_level: float = 0.0
    narrative_stage: str = "neutral"
