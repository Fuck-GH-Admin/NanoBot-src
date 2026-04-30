import asyncio
import json
import os
from pathlib import Path
from typing import Dict, List, Any, Optional

import aiofiles
from nonebot.log import logger

from ..config import plugin_config
from ..utils.file_utils import AsyncFileUtils


class MemoryRepository:
    """
    用户记忆仓库
    负责 session_id 维度的 JSON 读写，包含会话级并发锁。
    """
    _instance = None
    _locks: Dict[str, asyncio.Lock] = {}
    _global_lock = asyncio.Lock()

    BASE_DIR = Path("data/chat_memory")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MemoryRepository, cls).__new__(cls)
            cls.BASE_DIR.mkdir(parents=True, exist_ok=True)
        return cls._instance

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            async with self._global_lock:
                if session_id not in self._locks:
                    self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def load_memory(self, session_id: str) -> Dict[str, Any]:
        """
        加载会话记忆
        :return: 包含 history(List) 和 profile(Dict) 的字典
        """
        path = self.BASE_DIR / f"{session_id}.json"
        lock = await self._get_session_lock(session_id)
        async with lock:
            data = await AsyncFileUtils.read_json(path)
            return {
                "history": data.get("history", []),
                "profile": data.get("profile", {}),
            }

    async def save_memory(self, session_id: str, history: List[dict], profile: dict) -> bool:
        """
        原子写入会话记忆（临时文件 + 重命名）
        """
        path = self.BASE_DIR / f"{session_id}.json"
        tmp_path = path.with_suffix(".tmp")
        data = {
            "history": history,
            "profile": profile,
        }
        lock = await self._get_session_lock(session_id)
        async with lock:
            try:
                # 写入临时文件
                async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                    content = json.dumps(data, ensure_ascii=False, indent=2)
                    await f.write(content)
                # 原子替换
                os.replace(tmp_path, path)
                logger.debug(f"[MemoryRepo] Saved memory for session {session_id}")
                return True
            except Exception as e:
                logger.error(f"[MemoryRepo] Failed to save memory for session {session_id}: {e}")
                try:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return False

    async def clear_history(self, session_id: str) -> bool:
        """仅清空历史记录，保留画像"""
        memory = await self.load_memory(session_id)
        return await self.save_memory(session_id, [], memory["profile"])
