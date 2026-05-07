"""
conftest.py — 在任何 chatbot 模块被导入前，预装 nonebot mock，
避免 NoneBot 未初始化导致的 ImportError / ValueError。

这段代码在模块顶层执行，pytest 在收集测试用例前会先加载 conftest，
因此 mock 会在 chatbot 包被首次导入前生效。
"""

import sys
from unittest.mock import MagicMock

# 构建完整的 nonebot mock 子模块树
_SUBMODULES = [
    "nonebot",
    "nonebot.log",
    "nonebot.plugin",
    "nonebot.params",
    "nonebot.matcher",
    "nonebot.adapters",
    "nonebot.adapters.onebot",
    "nonebot.adapters.onebot.v11",
    "nonebot.adapters.onebot.v11.event",
    "nonebot.adapters.onebot.v11.message",
    "nonebot.message",
    "nonebot.rule",
    "nonebot.permission",
    "nonebot.typing",
    "nonebot.exception",
    "nonebot.drivers",
    "nonebot.drivers.none",
    "loguru",
    "watchdog",
    "watchdog.observers",
    "watchdog.events",
    "img2pdf",
]

for _mod_name in _SUBMODULES:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

# nonebot.get_driver 必须返回可调用对象
sys.modules["nonebot"].get_driver.return_value = MagicMock()
