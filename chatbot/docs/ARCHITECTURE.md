# 架构蓝图说明书

> Chatbot B V3 系统内部架构的权威技术文档。
> 基于微内核（Microkernel）设计哲学，控制面与数据面物理隔离。

---

## 1. 系统拓扑

### 1.1 进程模型

系统为单进程架构，运行于 NoneBot2 框架之上：

| 组件 | 端口 | 职责 |
|------|------|------|
| **NoneBot2 主进程** | — | 事件监听、业务逻辑、LLM 调用、数据持久化 |
| **Web 管理面板** | 8081 | 配置读写（零信任 Bearer Token 鉴权） |

### 1.2 消息处理链路

```
用户消息进入 NoneBot2
        │
        ├─ priority=3 → admin_hard.py (Alconna 结构化匹配)
        │               block=True，指令文本不会泄漏到下游
        │
        └─ priority=10 → chat_entry.py (消息入口)
                         触发判定 → AgentService.run_agent()
```

`block=True` 是安全隔离的关键：一旦 Alconna 匹配成功，事件传播即被终止，`chat_entry.py` 永远不会看到管理指令的原始文本。

---

## 2. 微内核架构：控制面与数据面

### 2.1 设计哲学

传统架构中，所有工具（包括管理工具）共享同一个注册表，LLM 可以通过 function-calling 看到并调用任何工具。V3 架构将工具系统在物理层面切割为两个独立的注册表：

```
tools/
├── base_tool.py              # BaseTool 抽象基类（is_write_operation 标记）
├── registry.py               # ToolRegistry 基类 + 两个子类
├── agent_tools/              # 数据面：LLM 可见
│   ├── image_tool.py         # GenerateImageTool, SearchAcgImageTool
│   ├── book_tool.py          # RecommendBookTool, JmDownloadTool
│   ├── rule_tool.py          # LearnRuleTool, ForgetRuleTool
│   └── system_tool.py        # MarkTaskCompleteTool
└── system_tools/             # 控制面：LLM 绝对不可见
    └── admin_tool.py         # BanUserTool
```

### 2.2 注册表层次结构

```python
class ToolRegistry:              # 基类：register / get_tool / get_all_schemas / execute_tool
    ├── AgentToolRegistry        # 数据面注册表：实例化时注册 agent_tools/ 下的 7 个工具
    └── SystemToolRegistry       # 控制面注册表：实例化时注册 system_tools/ 下的 1 个工具
```

`AgentService` 仅持有 `AgentToolRegistry` 实例。`BanUserTool` 注册在 `SystemToolRegistry` 中，由 `admin_hard.py` 直接调用，不经过 `AgentService`。

### 2.3 权限模型

工具权限在两个层面进行验证：

**Schema 层面**（`get_all_schemas`）：

| 权限等级 | Schema 可见性 | 说明 |
|---------|:------------:|------|
| `user` | ✅ | 所有用户可见 |
| `drawing_whitelist` | ✅ | 仅白名单用户可见 |
| `admin` | ✅ | 仅管理员可见 |
| `system` | ❌ | 永远不注入 Schema（`MarkTaskCompleteTool`） |

**执行层面**（`execute_tool`）：

即使 Schema 被注入，执行前仍进行二次权限验证。`system` 级工具跳过检查（仅内部调用）；`admin` 级工具检查 `context["is_admin"]`。

---

## 3. 数据持久层

### 3.1 ER 图

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  ChatHistory  │     │  GroupMemory  │     │  UserTrait   │
├──────────────┤     ├──────────────┤     ├──────────────┤
│ id (PK)      │     │ session_id   │     │ trait_id(PK) │
│ session_id   │────→│ summary      │     │ session_id   │
│ role         │     │ updated_at   │     │ user_id      │
│ user_id      │     └──────────────┘     │ content      │
│ name         │                          │ confidence   │
│ content      │     ┌──────────────┐     │ is_active    │
│ timestamp    │     │CompactionJnl │     │ updated_at   │
│ is_summarized│     ├──────────────┤     └──────────────┘
│ tool_calls   │     │ journal_id   │
└──────────────┘     │ session_id   │     ┌──────────────┐
                     │ status       │     │   Entity     │
                     │ retry_count  │     ├──────────────┤
                     │ last_error   │     │ entity_id(PK)│
                     │ created_at   │     │ session_id   │
                     │ updated_at   │     │ name         │
                     └──────────────┘     │ type         │
                                          │ attributes   │
┌──────────────┐     ┌──────────────┐     │ updated_at   │
│   Relation   │     │  CustomRule  │     └──────────────┘
├──────────────┤     ├──────────────┤
│relation_id   │     │ rule_id (PK) │     ┌──────────────┐
│ session_id   │     │ scope_type   │     │RuleChangelog │
│subject_entity│     │ scope_id     │     ├──────────────┤
│ predicate    │     │ keywords     │     │ id (PK)      │
│object_entity │     │ tool_name    │     │ action       │
│ confidence   │     │ hit_count    │     │ rule_id      │
│evidence_msgs │     │ active       │     │ operator     │
│ updated_at   │     │ ttl_days     │     │ old/new_value│
└──────────────┘     └──────────────┘     └──────────────┘

┌──────────────────┐
│ ToolExecutionLog  │  ← 突变日志：仅记录 is_write_operation=True 的工具
├──────────────────┤
│ id (PK)          │
│ session_id       │
│ request_id       │
│ step             │
│ trigger          │  ← "llm" / "forced_shortcut" / "control_plane"
│ tool_name        │
│ arguments        │
│ result_summary   │
│ error            │
│ created_at       │
└──────────────────┘
```

### 3.2 表设计理念

| 表 | 设计理念 |
|---|---|
| **ChatHistory** | 每条消息独立一行，支持增量总结（`is_summarized` 游标）和溯源 |
| **GroupMemory** | 每个 session 一行的宏观摘要，upsert 更新 |
| **UserTrait** | 每条特征独立一行，支持置信度和逻辑删除（`is_active`） |
| **CompactionJournal** | 压缩任务状态机（pending→running→success/dead），支持僵尸恢复 |
| **Entity** | 知识图谱节点，`attributes` 为自由 JSON |
| **Relation** | 知识图谱三元组（S-P-O），唯一约束防重，`evidence_msg_ids` 合并去重 |
| **CustomRule** | 动态关键词规则，软删除（`active` 标记），TTL 自动过期 |
| **RuleChangelog** | 规则变更审计日志，记录 old/new 值 |
| **ToolExecutionLog** | 突变日志：仅记录状态变更操作，`trigger` 区分调用来源 |

### 3.3 时间衰减机制

关系（Relation）的置信度随时间自然淡化：

```
effective_confidence = confidence × 0.5 ^ (age_days / half_life_days)
```

- **默认半衰期**：30 天
- **过滤阈值**：`effective_confidence < 0.15` 的关系自动排除
- **实现位置**：`MemoryRepository.get_relations_with_decay()`

---

## 4. 双脑协作架构

### 4.1 Prompt 编译流程

```
用户消息
    │
    ▼
┌─ 逻辑脑（PromptAdapter.compile_logic_prompt）───────────────┐
│  · 极简调度指令：分析意图，决定是否调用工具                      │
│  · 注入：group_dynamics + group_memory + dynamic_rule         │
│  · 无角色扮演设定（include_role_play_setting=False）           │
│  · 可调用 AgentToolRegistry 中的工具                          │
└──────────────────────────────────────────────────────────────┘
    │ (工具执行结果 → system_notification)
    ▼
┌─ 演员脑（PromptAdapter.compile_actor_prompt）────────────────┐
│  · 完整角色卡 + 世界书 + 宏替换                                │
│  · 注入：影子上下文（ShadowContext）                            │
│  · 注入：system_notification（工具执行结果）                    │
│  · 注入：dynamic_rule（如有匹配）                              │
│  · 无工具访问（纯文本生成）                                     │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
  最终回复
```

### 4.2 Token 预算仲裁

当 Prompt 总长度超出模型上下文窗口时，`TokenArbitrator` 按优先级裁剪：

| 优先级 | 名称 | 裁剪策略 | never_cut |
|:------:|------|---------|:---------:|
| 1 | `SYSTEM_DIRECTIVES` | 不裁剪 | ✅ |
| 2 | `ROLE_PLAY_SETTING` | 不裁剪 | ✅ |
| 3 | `CHAT_HISTORY` | 从最旧消息开始移除，保留≥2条最近消息 | ❌ |
| 4 | `GROUP_DYNAMICS` | 从末尾弹出 items | ❌ |
| 5 | `GROUP_MEMORY` | 从末尾弹出 items | ❌ |
| 6 | `WORLD_KNOWLEDGE` | 从末尾弹出 items | ❌ |

裁剪分两阶段：Phase 1 使用 `len(text)/3.35` 快速估算；Phase 2 使用 `tiktoken` 精确验证。

### 4.3 影子上下文（Shadow Context）

```python
class ShadowContext:  # 单例
    _queues: TTLCache[session_id → deque(maxlen=5)]  # TTL=86400s

    def push(session_id, fact)       # 控制面操作后调用
    def get_recent(session_id, n=3)  # 演员脑编译时读取
```

注入位置：`compile_actor_prompt()` 中，以 `Priority.SYSTEM_DIRECTIVES` + `never_cut=True` 的 `SystemBlock` 注入，确保演员脑始终可见。

---

## 5. 强指令系统（Alconna 协议）

### 5.1 指令定义

所有管理指令在 `matchers/admin_hard.py` 中通过 `Alconna` 类定义：

```python
cmd_leave          = Alconna("退群")
cmd_activity       = Alconna("调整活跃度", Args["prob", str])
cmd_dive           = Alconna("潜水模式", Args["action", ["开启", "关闭"]])
cmd_draw_whitelist = Alconna("授权画图", Args["targets", At, ...])
cmd_ban            = Alconna("禁言", Args["target", At]["duration", int, 600])
```

### 5.2 Matcher 注册

所有指令注册时设置 `priority=3, block=True`：

```python
admin_leave = on_alconna(cmd_leave, aliases={"leave"}, priority=3, block=True)
admin_ban   = on_alconna(cmd_ban,   aliases={"ban"},   priority=3, block=True)
```

`block=True` 确保匹配成功后事件传播终止，`chat_entry.py`（`priority=10`）不会收到指令文本。

### 5.3 权限检查

所有 handler 共享 `_check_privilege(bot, event)` 函数，通过 `PermissionService.has_command_privilege()` 验证用户身份（超管 / AI 管理员 / 群管理）。

---

## 6. 突变日志（Mutation Logs）

### 6.1 设计动机

传统日志记录所有工具调用，包括只读查询（`search_acg_image`）和内部信号（`mark_task_complete`），导致审计表噪声过高。V3 引入 `is_write_operation` 标记，仅记录状态变更操作。

### 6.2 写入路径

| 调用来源 | 写入位置 | trigger 值 |
|---------|---------|-----------|
| 硬指令短路（`/jm` 等） | `agent_service.py` Site 1 | `"forced_shortcut"` |
| 逻辑脑 LLM 循环 | `agent_service.py` Site 2 | `"llm"` |
| 单脑模式循环 | `agent_service.py` Site 3 | `"llm"` |
| 控制面（禁言） | `admin_tool.py` 显式调用 | `"control_plane"` |

所有路径均检查 `tool.is_write_operation`，仅当为 `True` 时才调用 `insert_tool_log()`。

### 6.3 查询接口

```python
async def get_recent_tool_logs(session_id: str, limit: int = 20) -> List[Dict]
```

返回指定 session 最近的突变日志条目，按 `id` 降序排列。

---

## 7. 配置管理

### 7.1 原子化写入

```
save_config(data)
    │
    ├─ 1. tempfile.mkstemp() → 创建临时文件
    ├─ 2. yaml.dump() → 写入临时文件
    ├─ 3. os.replace() → 原子性替换目标文件
    ├─ 4. _version += 1 → 递增版本计数器
    └─ 5. 仅当上述全部成功 → 更新内存 _config
```

Web API 的 `do_POST` 遵循相同策略：先构建候选配置（`candidate`），序列化为 `payload`，落盘成功后才更新内存。

### 7.2 热重载

`watchdog.Observer` 监控 `config/` 目录，文件修改后 0.5 秒延迟触发 `load_config()`。`load_config()` 采用深拷贝方案：仅更新 YAML 中显式出现的键，未出现的键保留当前值。

### 7.3 零信任 Web 面板

- 每次 Python 重启生成新 Token（`secrets.token_urlsafe(32)`）
- HTML 通过 `<meta>` 标签注入 Token，前端读取后立即 `.remove()`
- 所有 API 请求需 `Authorization: Bearer <token>`
- 使用 `secrets.compare_digest` 防时序攻击

---

## 8. Feature Flag 机制

| Flag | 默认值 | 控制内容 |
|------|:------:|---------|
| `enable_dual_brain` | `True` | 双脑模式开关（关闭则使用单脑 ReAct） |
| `enable_strict_schema` | `True` | Pydantic Schema 校验 |
| `enable_task_queue` | `False` | 记忆压缩走队列（持久化+重试）还是直接 create_task |
| `enable_dynamic_loop` | `False` | Jaccard 重复检测 + `mark_task_complete` 工具注册 |
| `entity_relation_enabled` | `False` | 是否查询衰减关系并注入 memorySnapshot |
| `semantic_lorebook_enabled` | `False` | 是否启用语义向量检索 |
| `token_arbitration_enabled` | `False` | 是否启用 Token 优先级裁剪 |

---

## 9. 性能与可靠性

### 9.1 事件循环保护

CPU/IO 密集型同步操作通过 `asyncio.to_thread()` 或 `loop.run_in_executor()` 卸载到线程池，避免阻塞 NoneBot 主事件循环。

### 9.2 记忆压缩熔断器

`MemoryCircuitBreaker` 监控压缩 worker 的健康状态。当 worker 崩溃时，`on_worker_dead` 回调自动重启 worker 协程。`EventLoopMonitor` 检测事件循环漂移（阈值 1.5 秒）。

### 9.3 自愈机制

`run_with_self_heal(name, coro_func)` 将协程包装在无限重试循环中（5 秒退避），崩溃时通过 `alert_manager` 发送紧急告警。

---

## 10. 数据模型 ER 图（完整）

```
┌──────────────────┐
│  ToolExecutionLog │  ← 突变日志（仅 is_write_operation=True）
├──────────────────┤
│ id (PK)          │
│ session_id (idx) │
│ request_id       │
│ step             │
│ trigger          │  "llm" / "forced_shortcut" / "control_plane"
│ tool_name        │
│ arguments (JSON) │
│ result_summary   │
│ error            │
│ created_at       │
└──────────────────┘
```

> 注：`ToolExecutionLog` 表仅记录状态变更操作。只读工具（`search_acg_image`、`recommend_book`）和内部信号（`mark_task_complete`）的调用不会写入此表。
