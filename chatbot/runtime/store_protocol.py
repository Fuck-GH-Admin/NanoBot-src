from typing import Protocol, List

from .events import ConversationEvent


class EventStoreProtocol(Protocol):
    async def append_event(self, event: ConversationEvent) -> ConversationEvent: ...

    async def load_stream(self, session_id: str) -> List[ConversationEvent]: ...
