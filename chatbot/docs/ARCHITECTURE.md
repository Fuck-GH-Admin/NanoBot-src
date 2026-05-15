# 架构白皮书 — Chatbot B V4

> 权威技术文档。基于源码全局审阅生成。
> 最后更新：2026-05-16

---

## 目录

1. [微内核架构与单进程异步模型](#1-微内核架构与单进程异步模型)
2. [Dual-Brain 双脑调度流转](#2-dual-brain-双脑调度流转)
3. [工具注册表分层与安全隔离](#3-工具注册表分层与安全隔离)
4. [话题路由系统 (Topic Router)](#4-话题路由系统-topic-router)
5. [沉浸会话生命周期](#5-沉浸会话生命周期)
6. [日志路由系统 (contextvars)](#6-日志路由系统-contextvars)
7. [Web 控制台与配置热重载](#7-web-控制台与配置热重载)
8. [AESAM 事件溯源运行时](#8-aesam-事件溯源运行时)

---

## 1. 微内核架构与单进程异步模型

### 1.1 系统拓扑

系统运行于 NoneBot2 框架之上，采用**单进程异步**模型。所有 I/O 密集操作（HTTP 调用、数据库查询、文件写入）均通过 `asyncio` 协程调度；CPU 密集操作通过 `asyncio.to_thread()` 卸载至线程池，保护主事件循环。

```
┌─────────────────────────────────────────────────────────────────┐
│                    NoneBot2 主进程 (asyncio)                     │
│                                                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐ │
│  │ Matchers │──→│ Services │──→│  Repos   │──→│   SQLite     │ │
│  │ (事件层) │   │ (业务层) │   │ (数据层) │   │  (持久化)    │ │
│  └──────────┘   └──────────┘   └──────────┘   └──────────────┘ │
│       │              │                                         │
│       │              ├─→ LLM API (DeepSeek / SiliconFlow)      │
│       │              ├─→ Embedding API (向量检索)               │
│       │              └─→ Pixiv / JM 外部服务                   │
│       │                                                        │
│  ┌──────────┐   ┌──────────────┐                               │
│  │ Web 面板 │   │ Background   │                               │
│  │ :8081    │   │ Daemons      │                               │
│  │(FastAPI) │   │ (协程)       │                               │
│  └──────────┘   └──────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 消息处理链路

```
用户消息进入 NoneBot2
        │
        ├─ priority=3  → admin_hard.py  (Alconna 结构化匹配, block=True)
        │                管理指令在此终结，绝不泄漏至下游
        │
        ├─ priority=5  → event_notice.py (戳一戳/入群退群通知)
        │
        └─ priority=10 → chat_entry.py  (主消息入口)
                         │
                         ├─ 噪音过滤 (_NOISE_PATTERN + 最小长度)
                         ├─ 新群拦截 (未注册群需管理员 @ 激活)
                         ├─ 触发判定 (@/回复/唤醒词/沉浸窗口/随机插嘴)
                         ├─ 话题路由 (TopicRouter L1/L1.5/L2/L3)
                         ├─ 沉浸会话状态更新 (ACTIVE / SOFT_SUSPEND)
                         └─ AgentService.run_agent()
                              │
                              ├─ enable_aesam_runtime=True
                              │   └─ AesamAdapter.handle_turn()
                              │       └─ ConversationRuntime.process_turn()
                              │           (事件溯源新架构，见第 8 节)
                              │
                              └─ enable_aesam_runtime=False
                                  ├─ enable_dual_brain=True  → 双脑模式
                                  └─ enable_dual_brain=False → 单脑 ReAct
```

### 1.3 微内核设计：控制面与数据面隔离

```
┌─────────────────────────────────────────────────────────────┐
│                      NoneBot2 事件循环                       │
├──────────────────────────┬──────────────────────────────────┤
│     控制面 Control Plane  │      数据面 Data Plane           │
│  ┌─────────────────────┐ │  ┌────────────────────────────┐  │
│  │  admin_hard.py      │ │  │  chat_entry.py             │  │
│  │  (Alconna 强指令)    │ │  │  (消息入口 + 触发判定)      │  │
│  └────────┬────────────┘ │  └────────────┬───────────────┘  │
│           ▼              │               ▼                  │
│  ┌─────────────────────┐ │  ┌────────────────────────────┐  │
│  │ SystemToolRegistry  │ │  │  AgentToolRegistry         │  │
│  │ (LLM 绝对不可见)     │ │  │  (LLM 可见，受权限过滤)    │  │
│  │                     │ │  │                            │  │
│  │ · BanUserTool       │ │  │ · GenerateImageTool        │  │
│  │                     │ │  │ · SearchAcgImageTool       │  │
│  │                     │ │  │ · RecommendBookTool        │  │
│  │                     │ │  │ · JmDownloadTool           │  │
│  │                     │ │  │ · LearnRuleTool            │  │
│  │                     │ │  │ · ForgetRuleTool           │  │
│  │                     │ │  │ · NoOpTool                 │  │
│  │                     │ │  │ · MarkTaskCompleteTool     │  │
│  │                     │ │  │ · ExitSessionTool          │  │
│  └─────────────────────┘ │  └────────────────────────────┘  │
└──────────────────────────┴──────────────────────────────────┘
```

**安全保证**：`SystemToolRegistry` 与 `AgentToolRegistry` 是两个独立的注册表实例，物理隔离于不同的目录（`tools/system_tools/` vs `tools/agent_tools/`）。`BanUserTool` 等控制面工具的 Schema **永远不会**出现在 LLM 的 `tools` 定义中，因此 LLM 在技术上无法生成对应的 `tool_call`。

---

## 2. Dual-Brain 双脑调度流转

### 2.1 总览

核心设计：每次用户交互触发**两次独立的 LLM 调用**，分别由"逻辑脑"和"演员脑"承担，职责严格隔离。

```
用户消息
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│                 Phase 1: 逻辑脑 (Scheduler)                  │
│                                                              │
│  身份：底层逻辑调度模块（严禁输出自然语言）                    │
│  工具：AgentToolRegistry 中的所有数据面工具                   │
│  温度：0.0（确定性输出）                                     │
│  循环：最多 agent_max_loops 轮（默认 10）                    │
│  去重：(tool_name, normalized_args) 签名集合                 │
│  防重放：AgentToolRegistry 请求级计数器                      │
│  防污染：Actor 历史以 <actor_past_reply> XML 封装            │
│                                                              │
│  终态感知：TERMINAL_TOOLS 命中 → 立即 break                  │
│  输出：tool_calls + 空 content                               │
└──────────────────────┬───────────────────────────────────────┘
                       │ 工具执行结果 → system_notification
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 Phase 2: 演员脑 (Actor)                      │
│                                                              │
│  身份：完整角色人格（角色卡 + 世界书 + 宏替换）              │
│  工具：无（纯文本生成）                                      │
│  温度：0.7（创造性输出）                                     │
│  注入：角色卡、世界书词条、动态规则、system_notification      │
│  输出：自然语言回复 + 可选 session_ctl 控制块                │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
                  最终回复 → 持久化到 chat_history
```

### 2.2 Phase 1：逻辑循环详解

逻辑循环位于 `agent_service.py:run_agent()` 的 `for loop_count in range(max_loops)` 块中。

**循环体流程**：

```
for loop_count in range(max_loops):
    │
    ├─ 1. 调用 LLM（tools=数据面工具, temperature=0.0）
    │     └─ tool_choice="required"（强制输出 tool_call）
    │
    ├─ 2. 防线：非法输出拦截
    │     └─ 逻辑脑输出了自然语言？→ 注入系统纠正消息，continue
    │
    ├─ 3. 签名去重
    │     └─ 计算 (func_name, normalized_args) 签名
    │     └─ 全部已执行？→ break
    │
    ├─ 4. 执行去重后的工具
    │     for tc in new_tcs:
    │         ├─ execute_tool()（带 15s 超时）
    │         ├─ 终态检测：func_name in TERMINAL_TOOLS → lb_terminal_hit = True
    │         ├─ exit_session 特殊处理：lb_exit_session = True
    │         ├─ 记录日志 + 审计日志
    │         └─ 工具出错？→ 注入系统警告
    │
    └─ 5. 终态跳出
          └─ if lb_terminal_hit: break
```

### 2.3 终态工具与 TERMINAL_TOOLS 机制

**问题背景**：在 V4 架构中，逻辑脑被强制约束为"严禁输出自然语言，必须且只能输出 `tool_call`"。当系统将 `no_op` 或 `mark_task_complete` 的结果塞回给逻辑脑并继续 `while` 循环时，逻辑脑被强制逼迫，只能产生重复的 `tool_call` —— 形成"幽灵空转（Ghost Loop）"。

**解决方案**：引入 `TERMINAL_TOOLS` 终态感知机制。

```python
# agent_service.py 顶层常量
TERMINAL_TOOLS = {"no_op", "mark_task_complete", "exit_session"}
```

当逻辑脑调用的工具属于 `TERMINAL_TOOLS` 时，系统**立即 break** 跳出逻辑循环，携带现有的 `system_notification` 推进到 Phase 2（演员脑）。

| 终态工具 | 语义 | 特殊处理 |
|---------|------|---------|
| `no_op` | 用户输入为闲聊，无需工具操作 | 仅触发终态跳出 |
| `mark_task_complete` | 逻辑层判断任务已完成 | 仅触发终态跳出（需 `enable_dynamic_loop`） |
| `exit_session` | 沉浸会话应结束 | 触发终态跳出 + 跳过演员脑渲染，静默退出 |

### 2.4 硬指令前缀快速路由

部分高频操作跳过逻辑脑 LLM 推理，直接执行工具：

```python
# config.py
force_tool_prefixes = {
    "/jm": "jm_download",
    "#搜图": "search_acg_image",
    "/画图": "generate_image",
}
```

命中时走 `_handle_forced_tool()` 短路：直接执行工具 → 结果注入演员脑渲染 → 返回。

### 2.5 单脑降级模式

当 `enable_dual_brain=False` 时，退化为 `_run_agent_single_brain()` ReAct 循环：

- 使用 Jaccard 相似度（阈值 0.9）检测重复输出
- 配合 `mark_task_complete` 工具动态终止循环
- 演员脑可直接输出自然语言，无终态工具约束

### 2.6 演员脑会话生命周期感知

演员脑可输出 ````session_ctl`{"close_session": true}`` 控制块，由 `agent_service.py:_parse_pb_close_session()` 解析后终结沉浸会话（从 `ACTIVE_SESSIONS` 中移除该用户）。

### 2.7 逻辑脑 Prompt 核心指令

```
=== CRITICAL: PURE LOGIC SCHEDULER MODE ===
你是 {char_name} 的底层逻辑调度模块。你的唯一使命是：分析用户意图并调用适当的工具。

【STRICTLY PROHIBITED ACTIONS】
- 禁止输出任何自然语言、对话、解释或角色扮演。
- 禁止在工具调用前添加任何思考过程。
- 严禁主动结束用户的会话状态。

【YOUR ONLY ALLOWED BEHAVIOR】
1. 如果用户指令明确需要后台操作，调用对应功能工具。
2. 如果用户输入为日常问候/聊天/无意义字符，必须调用 `no_op` 工具。
```

---

## 3. 工具注册表分层与安全隔离

### 3.1 注册表类层次

```python
# tools/registry.py

class ToolRegistry:
    """基类：register / unregister / get_tool / get_all_schemas / execute_tool"""

class AgentToolRegistry(ToolRegistry):
    """数据面注册表：LLM 可见工具，含请求级防重放拦截"""
    MAX_EXEC_PER_SIGNATURE = 2  # 同一请求内同签名最大执行次数

class SystemToolRegistry(ToolRegistry):
    """控制面注册表：LLM 绝对不可见"""
    pass
```

### 3.2 权限模型

工具通过 `require_permission` 字段声明权限级别：

| 权限级别 | 说明 | Schema 可见 | 执行时校验 |
|---------|------|:-----------:|----------:|
| `"user"` | 所有用户可用 | 是 | 无 |
| `"drawing_whitelist"` | 画图白名单用户 | 是 | `perm_srv.is_user_whitelisted()` |
| `"admin"` | 仅管理员 | 是 | `context["is_admin"]` |
| `"system"` | 系统内部工具 | 是（始终注入） | 跳过权限检查 |

### 3.3 防重放机制

`AgentToolRegistry` 在每个 `request_id` 内维护签名计数器：

```python
def _check_and_record(self, request_id, tool_name, arguments):
    sig = f"{tool_name}|{normalized_args}"
    count = counters.get(sig, 0)
    if count >= MAX_EXEC_PER_SIGNATURE:  # 2
        return False, "防重放拦截..."
    counters[sig] = count + 1
    return True, ""
```

配合逻辑循环中的 `executed_signatures` 集合，实现双重去重：
- **循环层**：同签名仅进入执行流程一次（`agent_service.py`）
- **注册表层**：同签名同一请求最多执行 2 次（`AgentToolRegistry`）

### 3.4 突变日志

并非所有工具调用都值得记录。只有 `is_write_operation = True` 的工具才写入 `tool_execution_log` 审计表：

| 工具 | is_write_operation | 说明 |
|------|:------------------:|------|
| `generate_image` | ✅ | 消耗资源，产生文件 |
| `jm_download` | ✅ | 消耗资源，产生文件 |
| `learn_rule` / `forget_rule` | ✅ | 改变规则配置 |
| `search_acg_image` | ❌ | 只读查询 |
| `recommend_book` | ❌ | 只读查询 |
| `no_op` | ❌ | 空操作信号 |
| `mark_task_complete` | ❌ | 内部循环信号 |

---

## 4. 话题路由系统 (Topic Router)

### 4.1 四级路由

为每个 session 维护内存话题池（`ACTIVE_TOPICS_POOL`，每 session 最多 10 个活跃话题）：

```
消息进入
   │
   ├─ L1: 物理强连通 (O(1), 无 API 调用)
   │       引用了某条消息？→ 反查该消息的 topic_id → 直接继承
   │
   ├─ L1.5: 低熵收容所 (O(1), 无 API 调用)
   │       纯表情/极短文本？→ 搭最近活跃话题的便车
   │
   ├─ L2: 混合语义路由 (Embedding API + 实体词池)
   │       ├─ 调用 SiliconFlow Embedding API 获取消息向量
   │       ├─ 与池中所有话题中心向量计算 cosine_sim × time_decay
   │       │   time_decay = exp(-Δt / 600s)
   │       ├─ 叠加 EntityPool 实体词交集加权分
   │       │   EntityPool 半衰期 2 分钟，仅保留强特征实体
   │       ├─ 命中阈值 (similarity_threshold, 默认 0.35)
   │       │   → EMA 更新话题中心向量
   │       └─ 未命中 → L3
   │
   └─ L3: 新建话题
           生成 UUID，写入内存池，超限按 last_active 淘汰
```

### 4.2 EntityPool 实体词池

每个 `ActiveTopic` 维护一个 `EntityPool`，用于辅助 L2 路由：

```python
@dataclass
class EntityPool:
    entities: dict[str, float]  # 强特征实体词 → 权重
    last_updated: float

    PROHIBITED_GENERIC_TERMS = {
        "姐姐", "妹妹", "哥哥", "弟弟", "妈妈", "爸爸", "主人", "指挥官",
        "大人", "前辈", "老师", "长官", "兄弟", "朋友",
    }

    def add(self, word: str) -> None:
        """仅当词不在禁止列表、长度>=2、非纯数字时才加入。"""

    def decay(self, half_life: float = 120.0) -> None:
        """激进指数衰减：半衰期默认 2 分钟。"""

    def score_intersection(self, candidate_entities: set[str]) -> float:
        """返回候选实体集与本池的交集加权分。"""
```

通用扮演称谓（如"姐姐"、"主人"）被禁止加入实体池，防止跨话题误路由。

### 4.3 社交熵硬闸 (Social Entropy Hard Gate)

高频热聊时，随机插嘴功能被硬闸拦截：

```python
@dataclass
class SocialWindow:
    events: deque  # deque[(timestamp, sender_id)]
    window_size: float = 60.0  # 滑动窗口 60 秒

def is_hot_conversation(group_id: int) -> bool:
    """≥2 个不同发言者 且 密度 > 0.1 msg/s（即 60s 内 > 6 条）"""
    return window.unique_senders() >= 2 and window.interaction_density() > 0.1
```

### 4.4 话题生命周期

```
  ACTIVE ──(10min 无活动)──→ SUSPENDED ──(30min 无活动)──→ ARCHIVED
    │                          │                              │
    │  (用户发消息时 refresh)   │  (Topic Harvester 扫描)      │
    └──────────────────────────┘                              │
                                                              ▼
                                                    LLM 摘要 + 设定提炼
                                                    → draft_worldbook.json
                                                    → 通知管理员审核
```

---

## 5. 沉浸会话生命周期

### 5.1 Soft Suspend 有限状态机

`chat_entry.py` 维护三层嵌套的沉浸会话状态：`{group_id: {topic_id: {user_id: SessionState}}}`

```python
@dataclass
class SessionState:
    topic_id: str
    status: str = "ACTIVE"           # "ACTIVE" | "SOFT_SUSPEND" | "INACTIVE"
    suspend_timestamp: float = 0.0
    last_active: float = time.time()
```

**状态转换**：

```
                    ┌─────────────────────────────────┐
                    │                                 │
                    ▼                                 │
              ┌──────────┐    注意力转移       ┌──────────────┐
  新消息 ───→ │  ACTIVE  │ ──────────────→ │ SOFT_SUSPEND │
              └──────────┘   (@其他人/       └──────────────┘
                    │         引用其他人)          │     │
                    │                              │     │ 5 分钟超时
                    │         用户再次发言          │     ▼
                    │         ┌────────────────────┘  ┌──────────┐
                    └─────────┘                       │ INACTIVE │
                                                      └──────────┘
```

### 5.2 触发判定优先级

```
1. 私聊 + 白名单 → 直接回复
2. 群聊触发条件（满足任一）：
   a. @ 机器人 (is_tome)
   b. 引用回复 Bot 的消息 (is_reply_bot)
   c. 唤醒词命中 ("elena", "Elena", "艾蕾娜")
   d. 沉浸会话窗口内 (ACTIVE 或 SOFT_SUSPEND → ACTIVE)
   e. 随机插嘴命中 (random_reply_prob, 受社交熵硬闸保护)
3. 注意力转移检测：@ 其他人或引用其他人 → SOFT_SUSPEND（不销毁会话）
4. 噪音过滤：纯标点/表情/极短文本 → 不触发
```

### 5.3 后台清理

`_cleanup_sessions()` 每 10 分钟运行一次，清理：
- `ACTIVE` 状态超过 `session_timeout`（默认 600s）的条目
- `SOFT_SUSPEND` 状态超过 `SOFT_SUSPEND_TIMEOUT`（300s）的条目

---

## 6. 日志路由系统 (contextvars)

### 6.1 Custom Callable Sink 架构

系统使用 loguru 的 Custom Callable Sink 实现按身份 + 按天的动态文件分离：

```
┌──────────────────────────────────────────────────────────────┐
│  loguru logger                                               │
│                                                              │
│  Sink 1: 系统级兜底 (logs/chatbot.log)                       │
│    filter: record["extra"] 中无 group_id 且无 private_user_id │
│    rotation: 10 MB, retention: 20 files                      │
│                                                              │
│  Sink 2: 群聊日志 (Custom Callable)                          │
│    filter: record["extra"] 中有 group_id                     │
│    路径: logs/groups/{YYYY-MM-DD}_group_{gid}.log            │
│                                                              │
│  Sink 3: 私聊日志 (Custom Callable)                          │
│    filter: record["extra"] 中有 private_user_id              │
│    路径: logs/private/{YYYY-MM-DD}_private_{uid}.log         │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 contextvars 自动传播

在 `chat_entry.py` 入口处，通过 `logger.contextualize()` 绑定日志上下文：

```python
log_ctx = {"group_id": group_id} if is_group else {"private_user_id": user_id}

with logger.contextualize(**log_ctx):
    logger.info(f"[ATTENTION] handle_chat 入口: ...")
    result = await agent_srv.run_agent(user_id, text, context)
```

`contextualize()` 基于 Python `contextvars` 实现，通过 `contextvars.ContextVar` 自动传播到整个异步调用链。`agent_service`、`topic_router`、`tools`、`repositories` 等下游模块的 `logger` 调用自动携带 `group_id` / `private_user_id`，由 loguru filter 路由到正确的分文件 sink。

**关键区别**：`contextualize()` 与 `bind()` 不同 —— `contextualize()` 使用 contextvars 传播，`bind()` 返回新实例。在异步场景中，`contextualize()` 能自动跨越 `await` 边界传播上下文。

### 6.3 文件命名与轮转

```
logs/
├── chatbot.log                           # 系统级（10MB 轮转，保留 20 个）
├── groups/
│   ├── 2026-05-14_group_123456.log       # 按天 × 群 自动创建
│   └── 2026-05-13_group_123456.log
└── private/
    ├── 2026-05-14_private_111111.log     # 按天 × 用户 自动创建
    └── 2026-05-13_private_111111.log
```

无需外部轮转配置（如 logrotate），Custom Sink 按日期动态创建新文件，旧文件自然沉淀。

---

## 7. Web 控制台与配置热重载

### 7.1 FastAPI 驱动

Web 管理面板基于 FastAPI 构建，以 `asyncio.create_task()` 启动，不阻塞 NoneBot2 主事件循环：

```python
# config.py
fastapi_app = FastAPI(title="Chatbot Config Panel")

async def _serve(manager: ConfigManager, port: int):
    import uvicorn
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()
```

### 7.2 鉴权机制

```
┌──────────────────────────────────────────────────────────────┐
│  鉴权流程                                                     │
│                                                              │
│  1. Token 生成                                               │
│     secrets.token_urlsafe(32) → 每次重启重新生成             │
│                                                              │
│  2. 登录                                                     │
│     POST /api/login {password: "..."}                        │
│     ├─ asyncio.sleep(1.0) 防暴力破解                         │
│     ├─ secrets.compare_digest 防时序攻击                     │
│     └─ 成功 → Set-Cookie: chatbot_admin_session=<token>      │
│         (HTTPOnly, SameSite=Lax, Max-Age=86400)             │
│                                                              │
│  3. API 鉴权                                                 │
│     每个需认证的端点通过 Depends(_check_auth) 校验：         │
│     ├─ 优先读取 Cookie: chatbot_admin_session                │
│     ├─ 回退读取 Header: Authorization: Bearer <token>        │
│     └─ secrets.compare_digest 恒定时间比较                   │
└──────────────────────────────────────────────────────────────┘
```

### 7.3 功能端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/login` | POST | 登录，返回 token 并设置 HTTPOnly Cookie |
| `/api/config` | GET | 获取全部配置字段 |
| `/api/config` | POST | 保存配置（原子落盘 + 内存刷新） |
| `/api/worldbook` | GET | 获取正式词条 + 草稿 |
| `/api/worldbook/save` | POST | 整体覆写正式词条 |
| `/api/worldbook/draft/approve` | POST | 批准草稿（分配新 UID） |
| `/api/worldbook/draft/reject` | POST | 拒绝草稿（从草稿箱删除） |

### 7.4 配置原子化写入

`ConfigManager.save_config()` 采用 **Write-First-Then-Update-Memory** 策略：

```
POST /api/config (JSON body)
       │
       ├─ 1. 字段校验 & 类型转换
       │     · Set 字段：列表/逗号分隔字符串 → set
       │     · Bool 字段：字符串 "true"/"1" → True
       │     · GroupSettings：逐 group_id 校验
       │
       ├─ 2. 构建候选配置 (deep copy，不更新内存)
       │
       ├─ 3. 原子落盘
       │     · tempfile.mkstemp() → yaml.dump() → os.replace()
       │     · 失败则内存不更新
       │
       └─ 4. 落盘成功 → 更新内存 _config → _version++
```

### 7.5 配置热重载

`ConfigManager` 使用 `watchdog.Observer` 监控 `config/` 目录：

```
config/*.yaml 文件变动
       │
       ▼ (FileSystemEventHandler)
  等待 0.5 秒（防抖）
       │
       ▼
  load_config()
       │
       ├─ 读取 YAML
       ├─ Pydantic 校验
       ├─ 深拷贝合并（仅更新 YAML 中显式出现的键）
       └─ 替换内存中的 _config 对象
```

**世界书热重载**：`WorldBook.search()` 每次调用时检查文件 mtime，变动则重新加载，无需重启。

---

## 8. AESAM 事件溯源运行时

### 8.1 总览

AESAM (Agentic Event-Sourced Actor Model) 是系统的下一代运行时架构，通过 Feature Flag `enable_aesam_runtime` 灰度启用。当启用时，`AgentService.run_agent()` 将对话处理委托给 `AesamAdapter`，由事件溯源运行时接管全部状态管理。

**核心设计原则：**
- **Event Log 是唯一真相源**：所有状态变更通过不可变事件流持久化
- **CanonicalState 是派生视图**：通过纯函数 Reducer 从事件流重建
- **SessionActor 是时间线所有者**：单线程并发隔离，拥有 Epoch 注入和幽灵拦截权
- **Reducer 是纯函数**：无 IO、无副作用、无验证逻辑

### 8.2 架构拓扑

```
AgentService.run_agent()
       │
       ▼ (enable_aesam_runtime=True)
┌─────────────────────────────────────────────────────────────────┐
│  AesamAdapter (桥接层)                                           │
│  将旧版基础设施 (repo, http_client, prompt_adapter, registry)     │
│  与新运行时对接                                                   │
│                                                                  │
│  ├── logic_runner 闭包: 编译 prompt → LLM → XML 解析 → 工具执行   │
│  ├── actor_runner 闭包: 注入世界书 + 工具结果 → LLM → 纯文本回复   │
│  └── _call_llm: 统一 LLM 调用（兼容 reasoner / non-reasoner）     │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  ConversationRuntime (Saga Coordinator / 入口适配器)              │
│  NOT 中央权威。所有变更权威属于 Actor → EventStore → Reducer       │
│                                                                  │
│  ├── _get_or_create_actor: 确定性回放 + 反回归检查                 │
│  ├── process_turn: 编排完整对话轮次                                │
│  └── shutdown: 停止所有活跃 Actor                                  │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  SessionActor (时间线所有者)                                       │
│  单线程、并发隔离的话题边界拥有者                                    │
│                                                                  │
│  ├── mailbox: asyncio.Queue (maxsize=100)                        │
│  ├── enqueue_and_wait: Ack 同步投递（无 sleep 轮询）               │
│  ├── _process_loop: 幽灵拦截 → Epoch 注入 → 落库 → Reducer → Ack  │
│  └── get_state: deep copy 隔离（防止状态泄漏）                     │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  EventStoreAdapter (持久化桥接)                                    │
│  桥接 MemoryRepository 的连接池，实现 EventStoreProtocol            │
│                                                                  │
│  ├── append_event: 事件持久化到 event_log 表（失败抛异常）          │
│  └── load_stream: 从 event_log 表加载事件流（失败抛异常）           │
└─────────────────────────────────────────────────────────────────┘
```

### 8.3 对话轮次生命周期 (process_turn)

```
用户消息进入
       │
       ▼
① 提交 USER_INPUT 事件
       │  Actor 注入 Epoch，落库，Reducer 更新 state
       ▼
② 逻辑脑申请 DRIVER_LEASED
       │  state.driver_owner = "logic"
       │  state.driver_lease_id = lease_id
       ▼
③ logic_runner 执行
       │  StateProjector.for_logic(state) → 逻辑投影
       │  LLM 调用 → XML 解析 → 工具执行
       ▼
④ 提交 TOOL_SUCCEEDED / TOOL_FAILED
       │  携带 driver_lease_id
       │  Actor 拦截幽灵：lease 不匹配 → 转为 TOOL_REJECTED
       ▼
⑤ 强制释放 DRIVER_RELEASED
       │  state.driver_owner = "actor"
       │  state.driver_lease_id = ""
       ▼
⑥ actor_runner 执行
       │  StateProjector.for_actor(state) → 演员投影
       │  注入世界书 + 工具结果 → LLM → 纯文本回复
       ▼
⑦ 叙事评估 EVAL_PROPOSAL
       │  trust_delta, narrative_stage
       ▼
返回最终回复
```

### 8.4 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `ConversationEvent` | `runtime/events.py` | 不可变事实（Frozen dataclass） |
| `EventType` | `runtime/events.py` | 事件类型枚举 |
| `CanonicalState` | `runtime/state.py` | 可变状态快照（Actor 拥有） |
| `StateReducer` | `runtime/reducer.py` | 纯函数：state + event → new_state |
| `SessionActor` | `runtime/actor.py` | 时间线所有者，单线程并发隔离 |
| `ConversationRuntime` | `runtime/engine.py` | Saga Coordinator，回放 + Actor 生命周期 |
| `EventStoreProtocol` | `runtime/store_protocol.py` | 持久化接口 |
| `EventStoreAdapter` | `runtime/adapter.py` | SQLite 实现 |
| `AesamAdapter` | `runtime/adapter.py` | 旧版基础设施桥接层 |
| `StateProjector` | `runtime/projections.py` | 不可变投影（ActorProjection / LogicProjection） |

### 8.5 事件类型

| 事件类型 | 方向 | 说明 |
|---------|------|------|
| `USER_INPUT` | 用户 → 系统 | 用户消息 |
| `DRIVER_LEASED` | Arbiter → Actor | 逻辑脑获取驾驶权 |
| `TOOL_SUCCEEDED` | Logic Brain → Actor | 工具执行成功 |
| `TOOL_FAILED` | Logic Brain → Actor | 工具执行失败 |
| `TOOL_REJECTED` | Actor 内部 | 幽灵执行拦截（lease 不匹配） |
| `DRIVER_RELEASED` | Arbiter → Actor | 释放驾驶权 |
| `EVAL_PROPOSAL` | Evaluator → Actor | 叙事评估提案 |
| `STATE_PATCHED` | 外部 | 状态补丁（跳过 Actor 拥有字段） |

### 8.6 幽灵执行拦截

当逻辑脑的工具结果到达 Actor 时，Actor 检查 `driver_lease_id` 是否匹配当前 state：

```
TOOL_SUCCEEDED 到达
       │
       ├─ claimed_lease == state.driver_lease_id → 正常处理，Epoch +1
       │
       └─ claimed_lease != state.driver_lease_id
           → 转为 TOOL_REJECTED（Epoch 不变）
           → 日志记录 "Ghost execution intercepted!"
```

**设计意图**：防止超时/取消的旧工具结果污染当前时间线。`TOOL_REJECTED` 不推进 Epoch，因为它不代表有效状态变更。

### 8.7 确定性回放

Actor 创建时通过 `EventStoreAdapter.load_stream()` 加载历史事件，经 `StateReducer.apply()` 逐事件重建状态：

```
Event Store (SQLite event_log)
       │
       ▼ load_stream(session_id)
[Event 1, Event 2, ..., Event N]
       │
       ▼ 纯函数回放
for event in history:
    state = StateReducer.apply(state, event)
       │
       ▼ 反回归检查
assert state.epoch == last_event.epoch
       │
       ▼
SessionActor(store, state)  → 开始接收新事件
```

**回放不变量**：Epoch 单调递增检查是回放核心唯一强制的不变量。所有其他策略（间隙检测、重复检测、幽灵分类）属于 Actor Pre-Append Validation 或独立的 TimelineIntegrityValidator，不属于回放。

### 8.8 Feature Flags

| Flag | 默认值 | 控制内容 |
|------|:------:|---------|
| `enable_aesam_runtime` | `False` | 启用事件溯源运行时（灰度开关） |

启用方式：在 YAML 配置中设置 `enable_aesam_runtime: true`。启用后，`AgentService.run_agent()` 将委托给 `AesamAdapter.handle_turn()`，所有对话状态通过事件溯源管理。

---

## 附录：Feature Flags

| Flag | 默认值 | 控制内容 |
|------|:------:|---------|
| `enable_aesam_runtime` | `False` | 事件溯源运行时（优先级高于 `enable_dual_brain`） |
| `enable_dual_brain` | `True` | 双脑模式（关闭则退化为单脑 ReAct） |
| `enable_dynamic_loop` | `False` | 智能循环终止（`mark_task_complete` 工具可用） |
| `enable_strict_schema` | `True` | 跨端 Schema 校验 |
| `semantic_lorebook_enabled` | `False` | FAISS 语义向量检索 |
| `token_arbitration_enabled` | `False` | Token 优先级裁剪 |
| `entity_relation_enabled` | `False` | 知识图谱实体/关系提取（预留） |
| `enable_task_queue` | `False` | 持久化任务队列（预留） |

---

*本文档基于源码全局审阅生成，反映 2026-05-16 的系统状态。*
