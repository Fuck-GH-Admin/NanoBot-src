import json
import asyncio
from datetime import datetime
from pathlib import Path

from nonebot.log import logger

DUMP_DIR = Path("data/chatbot/dumps")
DUMP_DIR.mkdir(parents=True, exist_ok=True)


class SessionDumper:
    @staticmethod
    def _write_sync(filepath: Path, payload: dict):
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"[Dumper] 写入 dump 失败: {e}")

    @staticmethod
    async def dump(group_id: str, user_id: str, payload: dict):
        """异步写入单行 JSONL 记录，按群和日期分片"""
        if not group_id or group_id == "0":
            group_id = "private"

        today = datetime.now().strftime("%Y%m%d")
        filename = f"group_{group_id}_{today}.jsonl"
        filepath = DUMP_DIR / filename

        # 补充通用元数据
        payload["_timestamp"] = datetime.now().isoformat()
        payload["_group_id"] = group_id
        payload["_user_id"] = user_id

        # 使用线程避免阻塞 Asyncio 事件循环
        await asyncio.to_thread(SessionDumper._write_sync, filepath, payload)
