# src/plugins/chatbot/utils/file_utils.py

import os
import json
import aiofiles
from pathlib import Path
from typing import Any, Union, Optional
from nonebot.log import logger

class AsyncFileUtils:
    """
    异步文件操作工具类
    封装 aiofiles 以提供非阻塞的 I/O 操作
    """

    @staticmethod
    async def read_json(path: Union[str, Path], default: Any = None) -> Any:
        path = Path(path)
        if not path.exists():
            return default if default is not None else {}
        
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"[FileUtils] JSON decode failed: {path}")
            return default if default is not None else {}
        except Exception as e:
            logger.error(f"[FileUtils] Read failed: {path} - {e}")
            return default if default is not None else {}

    @staticmethod
    async def write_json(path: Union[str, Path], data: Any) -> bool:
        path = Path(path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                content = json.dumps(data, ensure_ascii=False, indent=2)
                await f.write(content)
            os.replace(str(tmp_path), str(path))
            return True
        except Exception as e:
            logger.error(f"[FileUtils] Write failed: {path} - {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False