# src/plugins/chatbot/tools/registry.py

from typing import Any, Dict, List, Optional, Tuple
from nonebot.log import logger

from .base_tool import BaseTool


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(f"[ToolRegistry] 工具 '{tool.name}' 已存在，将被覆盖。")
        self._tools[tool.name] = tool
        logger.info(f"[ToolRegistry] 注册工具: {tool.name} (权限: {tool.require_permission})")

    def unregister(self, name: str) -> None:
        if name in self._tools:
            del self._tools[name]

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """按名称获取已注册工具，不存在返回 None"""
        return self._tools.get(name)

    def get_all_schemas(self, permissions: Any, user_id: str, is_admin: bool = False) -> List[Dict]:
        schemas = []
        for tool in self._tools.values():
            allowed = False
            perm = tool.require_permission
            if perm == "user":
                allowed = True
            elif perm == "drawing_whitelist":
                if permissions and hasattr(permissions, "is_user_whitelisted"):
                    allowed = permissions.is_user_whitelisted(user_id, "drawing")
            elif perm == "admin":
                allowed = is_admin
            # 可继续扩展
            if allowed:
                schema = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "require_permission": tool.require_permission
                    }
                }
                schemas.append(schema)

        # system 工具始终注入（不受权限控制）
        for tool in self._tools.values():
            if tool.require_permission == "system":
                already = any(s["function"]["name"] == tool.name for s in schemas)
                if not already:
                    schemas.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                            "require_permission": tool.require_permission
                        }
                    })
        return schemas

    async def execute_tool(self, name: str, arguments: Dict, context: Dict) -> Tuple[str, List[str]]:
        tool = self._tools.get(name)
        if not tool:
            return f"错误：未知工具 '{name}'", []

        # 权限二次验证（system 工具跳过）
        required_perm = tool.require_permission
        if required_perm == "system":
            pass  # 系统工具无需权限检查
        elif required_perm == "admin" and not context.get("is_admin"):
            return "权限不足：只有管理员可以执行此操作。", []
        elif required_perm == "drawing_whitelist":
            perm_srv = context.get("permission_service")
            user_id = context.get("user_id", "")
            if perm_srv and hasattr(perm_srv, "is_user_whitelisted"):
                if not perm_srv.is_user_whitelisted(user_id, "drawing"):
                    return "权限不足：你不在绘图白名单中。", []
            else:
                return "无法进行权限检查，操作拒绝。", []

        try:
            return await tool.execute(arguments, context)
        except Exception as e:
            logger.error(f"[ToolRegistry] 工具 '{name}' 执行异常: {e}")
            return f"工具执行出错：{str(e)}", []


class AgentToolRegistry(ToolRegistry):
    """数据面注册表：LLM 可见工具，含请求级防重放拦截"""

    # 同一 request 内，相同 (tool_name, normalized_args) 的最大执行次数
    MAX_EXEC_PER_SIGNATURE = 2

    def __init__(self) -> None:
        super().__init__()
        # {request_id: {signature: execution_count}}
        self._request_counters: dict[str, dict[str, int]] = {}

    def begin_request(self, request_id: str) -> None:
        """初始化一个请求级别的执行计数器。"""
        self._request_counters[request_id] = {}

    def end_request(self, request_id: str) -> None:
        """清理请求级别的执行计数器。"""
        self._request_counters.pop(request_id, None)

    def _check_and_record(self, request_id: str, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """
        检查是否超过单请求执行上限。
        返回 (allowed, error_message)。
        """
        if not request_id:
            return True, ""

        counters = self._request_counters.get(request_id)
        if counters is None:
            # No active request tracking — allow (fallback)
            return True, ""

        import json
        try:
            normalized = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            normalized = str(arguments)
        sig = f"{tool_name}|{normalized}"

        count = counters.get(sig, 0)
        if count >= self.MAX_EXEC_PER_SIGNATURE:
            return False, (
                f"防重放拦截：工具 '{tool_name}' 在同一请求中已执行 {count} 次，"
                f"超过上限 {self.MAX_EXEC_PER_SIGNATURE}。"
            )
        counters[sig] = count + 1
        return True, ""

    async def execute_tool(self, name: str, arguments: Dict, context: Dict) -> tuple[str, list[str]]:
        """
        执行工具（带请求级防重放校验）。
        context 中的 'request_id' 用于追踪同一请求内的重复调用。
        """
        request_id = context.get("request_id", "")
        allowed, deny_reason = self._check_and_record(request_id, name, arguments)
        if not allowed:
            from nonebot.log import logger
            logger.warning(f"[AgentToolRegistry] {deny_reason}")
            return deny_reason, []

        return await super().execute_tool(name, arguments, context)


class SystemToolRegistry(ToolRegistry):
    """控制面注册表：LLM 绝对不可见"""
    pass