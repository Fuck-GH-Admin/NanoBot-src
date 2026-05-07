# src/plugins/chatbot/tools/base_tool.py

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple


class BaseTool(ABC):
    """
    工具抽象基类
    每个工具必须提供名称、描述、参数 schema 以及权限标识，
    并实现异步的 execute 方法。
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}
    require_permission: str = "user"  # user, drawing_whitelist, admin 等
    risk_level: str = "low"           # low / high
    allow_forced_exec: bool = True    # 是否允许规则兜底执行
    is_write_operation: bool = False  # 是否为状态变更操作（仅写操作记入审计日志）

    @abstractmethod
    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        """
        执行工具逻辑
        :param arguments: 大模型传递的参数字典
        :param context: 运行时上下文，包含 bot、服务实例、用户信息等
        :return: (文本结果, 本地图片路径列表)
        """
        pass