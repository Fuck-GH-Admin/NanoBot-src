from collections import deque

from cachetools import TTLCache


class ShadowContext:
    """影子上下文：控制面操作的短期事实记忆，注入演员脑 Prompt"""

    _instance = None
    MAX_LEN = 5
    TTL = 86400  # 24 小时
    MAX_SESSIONS = 1000

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._queues = TTLCache(maxsize=cls.MAX_SESSIONS, ttl=cls.TTL)
        return cls._instance

    def push(self, session_id: str, fact: str):
        q = self._queues.get(session_id) or deque(maxlen=self.MAX_LEN)
        q.append(fact)
        self._queues[session_id] = q

    def get_recent(self, session_id: str, n: int = 3) -> list[str]:
        q = self._queues.get(session_id, deque())
        return list(q)[-n:]
