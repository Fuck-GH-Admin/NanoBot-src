import uuid
import time
import asyncio
from enum import Enum
from types import MappingProxyType
from dataclasses import dataclass, field
from typing import Any, Optional


class EventType(str, Enum):
    USER_INPUT = "user_input"
    TOOL_REQUESTED = "tool_requested"
    TOOL_SUCCEEDED = "tool_succeeded"
    TOOL_FAILED = "tool_failed"
    TOOL_REJECTED = "tool_rejected"
    DRIVER_LEASED = "driver_leased"
    DRIVER_RELEASED = "driver_released"
    EVAL_PROPOSAL = "eval_proposal"
    STATE_PATCHED = "state_patched"
    DECAY_APPLIED = "decay_applied"


@dataclass(frozen=True)
class ConversationEvent:
    event_id: str
    correlation_id: str
    causation_id: str
    session_id: str
    epoch: int
    type: EventType
    source: str
    payload: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass
class MailboxEnvelope:
    """Actor Mailbox 的传输信封，隔离 Runtime 概念与现实事件"""
    event: ConversationEvent
    ack_future: asyncio.Future
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline: float = field(default_factory=lambda: time.time() + 10.0)
