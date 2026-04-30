import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional
from nonebot.log import logger

from ..config import plugin_config
from ..utils.file_utils import AsyncFileUtils

class MemoryRepository:
    """
    用户记忆仓库
    负责 user_{id}.json 的读写，包含细粒度的用户级并发锁。
    """
    _instance = None
    _locks: Dict[str, asyncio.Lock] = {}
    _global_lock = asyncio.Lock()
    
    # 基础存储路径
    BASE_DIR = Path("data/chat_memory")

    def __new__(cls):
        """单例模式，确保锁对象全局唯一"""
        if cls._instance is None:
            cls._instance = super(MemoryRepository, cls).__new__(cls)
            cls.BASE_DIR.mkdir(parents=True, exist_ok=True)
        return cls._instance

    async def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """
        获取指定用户的锁。
        如果锁不存在，使用全局锁保护创建一个新的锁。
        """
        if user_id not in self._locks:
            async with self._global_lock:
                # 双重检查防止竞态
                if user_id not in self._locks:
                    self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def load_memory(self, user_id: str) -> Dict[str, Any]:
        """
        加载用户记忆
        :return: 包含 history(List) 和 profile(Dict) 的字典
        """
        path = self.BASE_DIR / f"user_{user_id}.json"
        
        # 获取锁读取，虽然读取一般不需锁，但为了防止读到写了一半的文件，加锁更稳妥
        lock = await self._get_user_lock(user_id)
        
        async with lock:
            data = await AsyncFileUtils.read_json(path)
            
            # 确保返回的数据结构完整
            return {
                "history": data.get("history", []),
                "profile": data.get("profile", {})
            }

    async def save_memory(self, user_id: str, history: List[dict], profile: dict) -> bool:
        """
        保存用户记忆
        :param history: 聊天记录列表
        :param profile: 用户画像字典
        """
        path = self.BASE_DIR / f"user_{user_id}.json"
        
        # 构造完整数据
        data = {
            "history": history,
            "profile": profile
        }
        
        lock = await self._get_user_lock(user_id)
        async with lock:
            success = await AsyncFileUtils.write_json(path, data)
            if success:
                logger.debug(f"[MemoryRepo] Saved memory for user {user_id}")
            else:
                logger.error(f"[MemoryRepo] Failed to save memory for user {user_id}")
            return success

    async def clear_history(self, user_id: str) -> bool:
        """仅清空历史记录，保留画像 (用于总结后重置)"""
        memory = await self.load_memory(user_id)
        return await self.save_memory(user_id, [], memory["profile"])