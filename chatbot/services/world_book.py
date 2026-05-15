# src/plugins/chatbot/services/world_book.py

import asyncio
import os
import json
from pathlib import Path

from nonebot.log import logger


class WorldBook:
    """轻量级世界书：关键词匹配 + mtime 热重载，独立于现有引擎。"""

    def __init__(self, config_path: str):
        self._path = config_path
        self._mtime: float = 0
        self._entries: list[dict] = []
        self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._entries = data.get("entries", []) if isinstance(data, dict) else data
            self._mtime = os.path.getmtime(self._path)
            logger.info(f"[WorldBook] 加载完成: {len(self._entries)} 条目, path={self._path}")
        except FileNotFoundError:
            logger.warning(f"[WorldBook] 文件不存在: {self._path}")
            self._entries = []
        except Exception as e:
            logger.error(f"[WorldBook] 加载失败: {e}")
            self._entries = []

    def _check_reload(self):
        try:
            mtime = os.path.getmtime(self._path)
            if mtime != self._mtime:
                logger.info("[WorldBook] 检测到文件变动，热重载...")
                self._load()
        except FileNotFoundError:
            pass

    def search(self, text: str, group_id: int = 0) -> str:
        """
        遍历条目，返回所有命中内容拼接。

        - constant=True → 无条件注入
        - constant=False → key 数组中任意子串命中 text
        - custom_scope 存在且 != "global" 且 != group_id → 跳过
        """
        self._check_reload()

        if not self._entries or not text:
            return ""

        matched_parts: list[str] = []
        text_lower = text.lower()

        for entry in self._entries:
            # 作用域过滤
            scope = entry.get("custom_scope", "global")
            if scope != "global" and str(scope) != str(group_id):
                continue

            # constant 条目无条件注入
            if entry.get("constant"):
                content = entry.get("content", "")
                if content:
                    matched_parts.append(content)
                continue

            # 关键词匹配
            keys = entry.get("key", [])
            if keys and any(k.lower() in text_lower for k in keys):
                content = entry.get("content", "")
                if content:
                    matched_parts.append(content)

        return "\n".join(matched_parts)


class DraftWorldBook:
    """世界书草稿箱：自动提炼的设定存入此处，等待管理员审核。"""

    def __init__(self, draft_path: str):
        self._path = draft_path
        self._lock = asyncio.Lock()

    def _read_all(self) -> list[dict]:
        """读取草稿箱全部条目。"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("entries", []) if isinstance(data, dict) else data
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _next_uid(self, entries: list[dict]) -> int:
        """分配唯一 uid（现有最大 uid + 1）。"""
        if not entries:
            return 1
        return max(e.get("uid", 0) for e in entries) + 1

    async def append_entry(self, entry: dict) -> bool:
        """线程安全地追加一条草稿。返回是否成功。"""
        async with self._lock:
            entries = self._read_all()
            entry["uid"] = self._next_uid(entries)
            entries.append(entry)
            try:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
                logger.info(f"[DraftWorldBook] 新增草稿 uid={entry['uid']}, keys={entry.get('key', [])}")
                return True
            except Exception as e:
                logger.error(f"[DraftWorldBook] 写入失败: {e}")
                return False
