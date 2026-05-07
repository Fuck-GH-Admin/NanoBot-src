# AgentService

**文件路径：** `services/agent_service.py`

**模块职责：** 双脑循环编排器。管理逻辑脑（工具调度）与演员脑（人格渲染）的协作流程，协调工具执行、记忆压缩、突变日志等子系统。

---

## 类：`AgentService`

| 方法 | 说明 |
|------|------|
| `run_agent(user_id, text, context)` | 主入口：准备上下文 → 逻辑脑循环 → 演员脑渲染 → 返回结果 |
| `close()` | 关闭 HTTP 连接池 |

### `__init__`

创建实例级资源：

- `self.repo` — `MemoryRepository` 单例
- `self.memory_service` — `MemoryService`（后台记忆压缩）
- `self.registry` — `AgentToolRegistry`（数据面注册表，LLM 可见）
- `self.prompt_adapter` — `PromptAdapter`（双脑 Prompt 编译）
- `self.rule_engine` — `RuleEngine`（动态规则匹配）
- `self.rule_repo` — `RuleRepository`（规则 CRUD）

> **重要：** `AgentService` 仅持有 `AgentToolRegistry`（数据面注册表）。`BanUserTool` 等控制面工具注册在 `SystemToolRegistry` 中，由 `admin_hard.py` 直接调用，不经过 `AgentService`。

---

### `async def run_agent(user_id, text, context) -> Dict[str, Any]`

**参数：**
- `user_id` — QQ 号（字符串）
- `text` — 用户消息文本
- `context` — 上下文字典，必须包含：
  - `permission_service` — `PermissionService` 实例
  - `is_admin: bool`
  - `group_id: int`（私聊为 0）
  - `allow_r18: bool`
  - `bot` — NoneBot Bot 实例
  - `sender_name: str`
  - `scope_type / scope_id` — 规则作用域
  - `drawing_service`, `image_service`, `book_service`（可选）

**返回值：**
```python
{"text": str, "images": [str]}
```

**执行流程（双脑模式）：**

```
Phase 3.5：硬指令短路检测
    │  匹配 force_tool_prefixes（/jm, #搜图, /画图）
    │  命中 → 直接执行工具，跳过 LLM
    │
    ▼
Phase 1：逻辑脑循环（最多 agent_max_loops 轮）
    │  · compile_logic_prompt() → 极简调度 Prompt
    │  · LLM 返回 tool_calls → 逐个执行
    │  · 仅 is_write_operation=True 的工具写入突变日志
    │  · 终止条件：无工具调用 / mark_task_complete / Jaccard 重复 / max_loops
    │
    ▼
Phase 2：演员脑渲染
    │  · compile_actor_prompt() → 完整角色扮演 Prompt
    │  · 注入 system_notification（工具执行结果）
    │  · 注入影子上下文（ShadowContext）
    │  · 无工具调用，纯文本生成
    │
    ▼
后台异步：记忆压缩 + 调试快照
```

---

## 注册工具（数据面）

| 工具类 | 权限 | is_write_operation | 说明 |
|--------|------|:------------------:|------|
| `GenerateImageTool` | `drawing_whitelist` | ✅ | AI 画图 |
| `SearchAcgImageTool` | `user` | ❌ | 搜图 |
| `RecommendBookTool` | `user` | ❌ | 推荐书籍 |
| `JmDownloadTool` | `user` | ✅ | JM 漫画下载 |
| `LearnRuleTool` | `admin` | ✅ | 创建动态规则 |
| `ForgetRuleTool` | `admin` | ✅ | 删除动态规则 |
| `MarkTaskCompleteTool` | `system` | ❌ | 内部循环终止信号 |

> `MarkTaskCompleteTool` 仅在 `enable_dynamic_loop=True` 时注册。

---

## 突变日志写入点

`insert_tool_log()` 仅在 `tool.is_write_operation == True` 时调用，共三个写入点：

| 写入点 | 位置 | trigger | 说明 |
|--------|------|---------|------|
| Site 1 | `_handle_forced_tool` | `"forced_shortcut"` | 硬指令短路路径 |
| Site 2 | `run_agent` 逻辑脑循环 | `"llm"` | 双脑模式 LLM 工具调用 |
| Site 3 | `_run_agent_single_brain` | `"llm"` | 单脑模式 LLM 工具调用 |

控制面工具（`BanUserTool`）在其自身 `execute()` 方法中显式调用 `insert_tool_log()`，trigger 为 `"control_plane"`。

---

## 调用示例

```python
result = await agent_srv.run_agent(
    user_id="123456",
    text="给我画一只猫",
    context={
        "permission_service": perm_srv,
        "is_admin": True,
        "group_id": 12345678,
        "allow_r18": False,
        "bot": bot_instance,
        "sender_name": "Alice",
        "scope_type": "group",
        "scope_id": "12345678",
    }
)
print(result["text"])
```
