# AgentService

**文件路径：** `services/agent_service.py`

**模块职责：** 双脑循环编排器。管理逻辑脑（工具调度）与演员脑（人格渲染）的协作流程，协调工具执行、终态感知、突变日志等子系统。

---

## 核心常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `TERMINAL_TOOLS` | `{"no_op", "mark_task_complete", "exit_session"}` | 终态工具集合：调用后立即终止逻辑循环 |
| `RECENT_MESSAGES_LIMIT` | 30 | 发给 LLM 的近期消息轮次上限 |
| `DB_QUERY_TIMEOUT` | 3.0s | 数据层调用超时 |
| `TOOL_EXEC_TIMEOUT` | 15.0s | 工具执行超时 |
| `SEMANTIC_SEARCH_TIMEOUT` | 3.0s | 语义检索超时 |
| `TOOL_RESULT_MAX_LEN` | 300 | 工具执行结果截断长度 |

---

## 类：`AgentService`

| 方法 | 说明 |
|------|------|
| `run_agent(user_id, text, context)` | 主入口：规则匹配 → 硬指令路由 → 逻辑脑循环 → 演员脑渲染 |
| `close()` | 关闭 HTTP 连接池 |

### `__init__`

创建实例级资源：

- `self.repo` — `MemoryRepository` 单例
- `self.registry` — `AgentToolRegistry`（数据面注册表，LLM 可见）
- `self.prompt_adapter` — `PromptAdapter`（双脑 Prompt 编译）
- `self.rule_engine` — `RuleEngine`（动态规则匹配）
- `self.rule_repo` — `RuleRepository`（规则 CRUD）
- `self.world_book` — `WorldBook`（世界书关键词检索）
- `self.semantic_lorebook` — 语义向量检索实例
- `self.aesam_adapter` — `AesamAdapter` 实例（懒初始化，`enable_aesam_runtime=True` 时首次使用）

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
  - `topic_id: str`（话题路由结果）
  - `is_tome / is_reply_bot / has_wake_word` — 触发判定标志
  - `_active_sessions` — 沉浸会话状态引用

**返回值：**
```python
{"text": str, "images": [str]}
```

**路由优先级：**
1. `enable_aesam_runtime=True` → 委托给 `AesamAdapter.handle_turn()`（事件溯源架构）
2. `enable_dual_brain=False` → `_run_agent_single_brain()`（单脑 ReAct）
3. 默认 → 双脑模式（逻辑脑 + 演员脑）

**执行流程：**

```
入口: run_agent()
    │
    ├─ enable_aesam_runtime=True → AesamAdapter.handle_turn()
    │   │  (事件溯源新架构，详见 ARCHITECTURE.md 第 8 节)
    │   │
    │   │  AesamAdapter 内部流程:
    │   │  ├── logic_runner: compile_logic_prompt → LLM → XML 解析 → 工具执行
    │   │  ├── actor_runner: compile_actor_prompt + 工具结果通知 → LLM → 纯文本
    │   │  └── ConversationRuntime.process_turn() 编排事件流
    │   │
    │   └─ 返回 {"text": str, "images": []}
    │
    └─ enable_aesam_runtime=False → 旧版双脑/单脑模式
        │
        ├─ Phase 0: 单脑回退
        │    enable_dual_brain=False → _run_agent_single_brain()
        │
        ▼
        Phase 1: 系统级短路
        │  source_type="system" → 仅演员脑渲染，无 DB 写入
        │
        ▼
        Phase 2: 规则匹配 + 消息落库
        │  rule_engine.match() → repo.add_message()
        │
        ▼
        Phase 3: 精准备料
        │  话题级精准查询 (get_messages_by_topic) or 全局回退
        │  世界书关键词检索 (world_book.search)
        │  语义向量检索 (semantic_lorebook.search, 可选)
        │
        ▼
        Phase 3.5: 硬指令前缀快速路由
        │  匹配 force_tool_prefixes（/jm, #搜图, /画图）
        │  命中 → 直接执行工具 → 演员脑渲染 → 返回
        │
        ▼
        Phase 4: 逻辑脑循环（最多 agent_max_loops 轮）
        │  · compile_logic_prompt() → 极简调度 Prompt
        │  · tool_choice="required"（强制输出 tool_call）
        │  · 签名去重：(tool_name, normalized_args) 集合
        │  · 防重放：AgentToolRegistry 请求级计数器
        │  · 终态感知：TERMINAL_TOOLS 命中 → 立即 break
        │  · exit_session → 跳过演员脑，静默退出
        │  · 非法输出拦截：逻辑脑输出自然语言 → 系统纠正
        │
        ▼
        Phase 5: 演员脑渲染
        │  · compile_actor_prompt() → 完整角色扮演 Prompt
        │  · 注入 system_notification（工具执行结果）
        │  · 注入世界书词条
        │  · 无工具调用，纯文本生成
        │  · 解析 session_ctl 控制块（close_session）
        │
        ▼
        持久化最终回复 → 返回
```

---

## 终态工具与 TERMINAL_TOOLS 机制

当逻辑脑调用的工具属于 `TERMINAL_TOOLS` 时，系统**立即 break** 跳出逻辑循环：

```python
TERMINAL_TOOLS = {"no_op", "mark_task_complete", "exit_session"}

# 在工具执行后立即检测
if func_name in TERMINAL_TOOLS:
    lb_terminal_hit = True
    logger.info(f"[Agent] 逻辑脑调用终态工具 '{func_name}'，立即终止逻辑循环")

# 内层循环结束后跳出外层
if lb_terminal_hit:
    break
```

| 终态工具 | 语义 | 特殊处理 |
|---------|------|---------|
| `no_op` | 用户输入为闲聊，无需工具操作 | 仅触发终态跳出 |
| `mark_task_complete` | 逻辑层判断任务已完成 | 仅触发终态跳出（需 `enable_dynamic_loop`） |
| `exit_session` | 沉浸会话应结束 | 触发终态跳出 + 跳过演员脑渲染 |

---

## 逻辑脑防线体系

| 防线 | 机制 | 说明 |
|------|------|------|
| 防线 1 | 纯净历史 | `_build_logic_history()` 对 Actor 历史以 `<actor_past_reply>` XML 封印 |
| 防线 2 | 签名去重 | `executed_signatures` 集合，同签名仅执行一次 |
| 防线 3 | 非法输出拦截 | 逻辑脑输出自然语言 → 注入系统纠正消息，continue |
| 防线 4 | 终态感知 | `TERMINAL_TOOLS` 命中 → 立即 break |
| 防线 5 | 防重放 | `AgentToolRegistry` 请求级计数器，同签名最多执行 2 次 |

---

## 突变日志写入点

`insert_tool_log()` 仅在 `tool.is_write_operation == True` 时调用，共三个写入点：

| 写入点 | 位置 | trigger | 说明 |
|--------|------|---------|------|
| Site 1 | `_handle_forced_tool` | `"forced_shortcut"` | 硬指令短路路径 |
| Site 2 | `run_agent` 逻辑脑循环 | `"llm"` | 双脑模式 LLM 工具调用 |
| Site 3 | `_run_agent_single_brain` | `"llm"` | 单脑模式 LLM 工具调用 |

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
| `NoOpTool` | `system` | ❌ | 空操作信号（终态工具） |
| `ExitSessionTool` | `system` | ❌ | 退出沉浸会话（终态工具） |
| `MarkTaskCompleteTool` | `system` | ❌ | 任务完成信号（终态工具，需 `enable_dynamic_loop`） |

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
        "topic_id": "abc123",
        "is_tome": True,
        "is_reply_bot": False,
        "has_wake_word": False,
        "_active_sessions": ACTIVE_SESSIONS,
    }
)
print(result["text"])
```
