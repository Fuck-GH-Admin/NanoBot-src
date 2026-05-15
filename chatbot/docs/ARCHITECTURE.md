# Chatbot B — Architecture Reference (V4)

> 权威技术文档。基于微内核（Microkernel）设计哲学，控制面与数据面物理隔离。
> 最后更新：2026-05-12

---

## 目录

1. [核心架构拓扑](#1-核心架构拓扑)
2. [记忆与知识图谱系统](#2-记忆与知识图谱系统)
3. [世界书与动态设定系统](#3-世界书与动态设定系统)
4. [动态上下文与 Token 仲裁](#4-动态上下文与-token-仲裁)
5. [日志路由系统](#5-日志路由系统)
6. [Web 控制台与配置热重载](#6-web-控制台与配置热重载)

---

## 1. 核心架构拓扑

### 1.1 单进程异步架构

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
│  │(守护线程)│   │ (协程)       │                               │
│  └──────────┘   └──────────────┘                               │
└─────────────────────────────────────────────────────────────────┘
```

**消息处理链路**：

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
                         ├─ 沉浸会话判定 (ACTIVE_SESSIONS 超时机制)
                         ├─ 话题路由 (TopicRouter L1/L1.5/L2/L3)
                         └─ AgentService.run_agent()
```

### 1.2 双脑协作模型 (Dual-Brain)

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
│  循环：最多 agent_max_loops 轮（默认 10）                    │
│  去重：(tool_name, normalized_args) 签名集合，同签名仅执行1次 │
│  防污染：Actor 历史回复以 <actor_past_reply> XML 封装        │
│  强制收口：末尾 System 消息反复提醒 "content 必须为空"        │
│                                                              │
│  输出：有且仅有一个 tool_call，content 恒为空                 │
└──────────────────────┬───────────────────────────────────────┘
                       │ 工具执行结果 → system_notification
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 Phase 2: 演员脑 (Actor)                      │
│                                                              │
│  身份：完整角色人格（角色卡 + 世界书 + 宏替换）              │
│  工具：无（纯文本生成）                                      │
│  注入：                                                     │
│    · 角色卡 (CharacterCard V1/V2/V3)                        │
│    · 世界书词条 (WorldBook matched entries)                  │
│    · ~~群组摘要 + 群友画像 + 社交关系网~~（已废弃，不再注入）│
│    · 影子上下文 (ShadowContext TTLCache)                     │
│    · 动态规则指令 (如有匹配)                                │
│    · system_notification（工具执行结果转述）                 │
│    · 会话生命周期感知指令                                    │
│                                                              │
│  输出：自然语言回复 + 可选 session_ctl 控制块                │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
                  最终回复 → 持久化（Topic Harvester 后台归档时提炼）
```

**逻辑脑 Prompt 核心指令**（`prompt_adapter.py:compile_logic_prompt`）：

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

**演员脑会话生命周期感知**：演员脑可输出 ````session_ctl`{"close_session": true}`` 控制块，由 `agent_service.py` 解析后终结沉浸会话。

**单脑降级模式**：当 `enable_dual_brain=False` 时，退化为 ReAct 循环（`_run_agent_single_brain`），使用 Jaccard 相似度（阈值 0.9）检测重复输出，配合 `mark_task_complete` 工具动态终止循环。

### 1.3 工具注册表分层

```
tools/
├── base_tool.py              # BaseTool ABC (is_write_operation 标记)
├── registry.py               # ToolRegistry 基类 + 两个子类
│
├── agent_tools/              # 数据面：LLM 可见，逻辑脑可调用
│   ├── system_tool.py        # MarkTaskComplete, NoOp, ExitSession
│   ├── rule_tool.py          # LearnRule, ForgetRule
│   ├── image_tool.py         # GenerateImage, SearchAcgImage
│   └── book_tool.py          # RecommendBook, JmDownload
│
└── system_tools/             # 控制面：LLM 绝对不可见
    └── admin_tool.py         # BanUser (由 admin_hard.py 直接调用)
```

| 注册表 | 实例持有者 | LLM 可见 | 权限过滤 |
|--------|-----------|:--------:|---------|
| `AgentToolRegistry` | `AgentService` | 是 | Schema 级 + 执行级二次验证 |
| `SystemToolRegistry` | `admin_hard.py` | 否 | 仅内部调用 |

**防重放机制**：`AgentToolRegistry` 在每个 `request_id` 内维护 `executed_signatures: set[str]`，同一签名仅执行 1 次（exactly-once 语义）。

### 1.4 话题路由系统 (Topic Router)

四级路由，为每个 session 维护内存话题池（`ACTIVE_TOPICS_POOL`，每 session 最多 10 个活跃话题）：

```
消息进入
   │
   ├─ L1: 物理强连通 (O(1))
   │       引用了某条消息？→ 直接继承该消息的 topic_id
   │
   ├─ L1.5: 低熵收容所
   │       纯表情/极短文本？→ 搭最近活跃话题的便车，不调 API
   │
   ├─ L2: 语义向量匹配 (网络 I/O)
   │       SiliconFlow Embedding API → cosine_sim × time_decay
   │       time_decay = exp(-Δt / 600s)
   │       命中阈值 → EMA 更新话题中心向量
   │
   └─ L3: 新建话题
           生成 UUID，写入内存池，超限按 last_active 淘汰
```

**话题生命周期状态机**：

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

## 2. 记忆与知识图谱系统

### 2.1 数据库 ER 结构

系统使用 SQLAlchemy 2.0 异步声明式模型，共 10 张核心表：

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   ChatHistory    │     │   GroupMemory    │     │    UserTrait     │
├─────────────────┤     ├─────────────────┤     ├─────────────────┤
│ id (PK, auto)   │     │ session_id (PK) │     │ trait_id (PK)   │
│ session_id (idx) │────→│ summary         │     │ session_id (idx) │
│ topic_id (idx)  │     │ updated_at      │     │ user_id (idx)   │
│ role            │     └─────────────────┘     │ content         │
│ user_id (idx)   │                             │ confidence      │
│ name            │     ┌─────────────────┐     │ source_msg_id   │
│ content         │     │CompactionJournal │     │ is_active       │
│ timestamp       │     ├─────────────────┤     │ updated_at      │
│ is_summarized   │     │ journal_id (PK) │     └─────────────────┘
│ tool_calls(JSON)│     │ session_id (idx)│
│ message_fprint  │     │ status          │     ┌─────────────────┐
│ UQ:(sess,fprint)│     │ retry_count     │     │     Entity       │
└─────────────────┘     │ max_retries     │     ├─────────────────┤
                        │ last_error      │     │ entity_id (PK)  │
                        │ created_at      │     │ session_id (idx) │
                        │ updated_at      │     │ name            │
                        └─────────────────┘     │ type            │
                                                │ attributes(JSON)│
┌─────────────────┐     ┌─────────────────┐     │ updated_at      │
│    Relation      │     │   CustomRule     │     └─────────────────┘
├─────────────────┤     ├─────────────────┤
│ relation_id(PK) │     │ rule_id (PK)    │     ┌─────────────────┐
│ session_id (idx)│     │ scope_type      │     │  RuleChangelog   │
│ subject_entity  │     │ scope_id        │     ├─────────────────┤
│ predicate       │     │ keywords_hash   │     │ id (PK, auto)   │
│ object_entity   │     │ keywords (JSON) │     │ timestamp       │
│ confidence      │     │ tool_name       │     │ action          │
│ evidence(JSON)  │     │ args_extractor  │     │ rule_id         │
│ updated_at      │     │ pattern_id      │     │ operator        │
│ UQ:(s,subj,p,obj)│    │ description     │     │ scope_type      │
└─────────────────┘     │ examples (JSON) │     │ scope_id        │
                        │ hit_count       │     │ old_value (JSON)│
┌──────────────────────┐│ last_hit        │     │ new_value (JSON)│
│  ToolExecutionLog    ││ created_at      │     └─────────────────┘
├──────────────────────┤│ created_by      │
│ id (PK, auto)        ││ updated_at      │     ┌─────────────────┐
│ session_id (idx)     ││ ttl_days        │     │  TopicThread     │
│ request_id           ││ active          │     ├─────────────────┤
│ step                 ││ priority        │     │ topic_id (PK)   │
│ trigger              ││ confidence      │     │ session_id (idx) │
│ tool_name            ││ allow_forced_exec│    │ status          │
│ arguments (JSON)     ││ UQ:(scope,hash) │     │ summary         │
│ result_summary       │└─────────────────┘     │ participants    │
│ error                │                        │ created_at      │
│ created_at           │                        │ last_active_at  │
└──────────────────────┘                        └─────────────────┘
```

**关键约束**：
- `ChatHistory`: `UNIQUE(session_id, message_fingerprint)` — 幂等写入，`ON CONFLICT DO NOTHING`
- `UserTrait`: `UNIQUE(session_id, user_id, content)` — 同一用户同一特征只保留一行
- `Relation`: `UNIQUE(session_id, subject_entity, predicate, object_entity)` — 三元组唯一

### 2.2 长期记忆沉淀：Topic Harvester（唯一机制）

旧的 15 条消息触发压缩机制（`process_session_memory` / `SUMMARY_THRESHOLD`）已移除（"双重抽税"问题）。**Topic Harvester 是系统唯一的长期记忆沉淀中枢。**

`MemoryService` 保留为骨架类（生命周期接口供 `__init__.py` 调用），其 `start_consumer()` 为 no-op，不再启动 worker。

```
Topic Harvester（唯一长期记忆路径）

话题归档 (SUSPENDED → ARCHIVED)
       │
       ▼
┌──────────────────────────────────────────────────────┐
│  _archive_topic()                                    │
│                                                      │
│  并行执行:                                           │
│    ① _generate_topic_summary()  → ≤200 字归档摘要    │
│    ② _extract_lore_from_topic() → JSON 设定条目      │
│                                                      │
│  设定条目 → draft_worldbook.json（草稿箱）            │
│  → Web 控制台「世界书」标签 → 管理员审核              │
│                                                      │
│  摘要 → TopicThread.summary 字段                     │
└──────────────────────────────────────────────────────┘
```

**已废弃的路径**（代码残留但无调用方）：

| 方法 | 位置 | 状态 |
|------|------|------|
| `process_session_memory` | `memory_service.py` | 已删除 |
| `_build_memory_snapshot` | `agent_service.py` | 已删除，`snapshot` 固定为 `{}` |
| `_build_extra_blocks_from_snapshot` | `prompt_adapter.py` | 保留但返回 `[]` |
| `upsert_user_traits` | `memory_repo.py` | 死代码，无调用方 |
| `upsert_relations` | `memory_repo.py` | 死代码，无调用方 |
| `get_relations_with_decay` | `memory_repo.py` | 死代码，无调用方 |
| `get_memory_snapshot` | `memory_repo.py` | 死代码，无调用方 |

### 2.3 UserTrait 与 Relation 的 Upsert 语义（Legacy）

> **注意**：以下方法仍存在于 `memory_repo.py` 中，但自压缩机制移除后已无调用方。保留代码供未来参考或复用。

**UserTrait Upsert**（`memory_repo.py:upsert_user_traits`）：

```sql
INSERT INTO user_trait (trait_id, session_id, user_id, content, confidence, ...)
VALUES (?, ?, ?, ?, ?, ...)
ON CONFLICT (session_id, user_id, content) DO UPDATE SET
    confidence = MAX(user_trait.confidence, excluded.confidence),
    updated_at = excluded.updated_at,
    source_msg_id = COALESCE(excluded.source_msg_id, user_trait.source_msg_id)
```

核心语义：**置信度只升不降**。

**Relation Upsert**（`memory_repo.py:upsert_relations`）：

三元组 `(session_id, subject_entity, predicate, object_entity)` 构成唯一键。冲突时 `confidence` 取较高值，`evidence_msg_ids` 合并去重。

### 2.4 时间衰减机制（Legacy）

> **注意**：`get_relations_with_decay` 仍存在于 `memory_repo.py`，但自压缩机制移除后已无调用方。`agent_service.py` 不再组装 `memorySnapshot`（固定为 `{}`）。

关系置信度随时间自然淡化：

```
effective_confidence = confidence × 0.5 ^ (age_days / half_life_days)
```

- 默认半衰期：30 天
- 过滤阈值：`effective_confidence < 0.15` 的关系自动排除

---

## 3. 世界书与动态设定系统

### 3.1 静态世界书 (WorldBook)

`WorldBook`（`world_book.py`）提供基于关键词的静态词条匹配：

```
config/worldbook.json
       │
       ▼ (mtime 热重载，每次 search() 检查)
┌──────────────────────────────────────────────────┐
│  遍历 entries[]                                  │
│                                                  │
│  对每个 entry:                                   │
│    1. 作用域过滤                                  │
│       custom_scope ≠ "global" && ≠ group_id → 跳过│
│                                                  │
│    2. constant=True → 无条件注入                  │
│                                                  │
│    3. constant=False → 关键词匹配                 │
│       any(k.lower() in text_lower for k in keys) │
│       命中 → 注入 content                        │
└──────────────────────────────────────────────────┘
```

**词条结构**：

```json
{
  "uid": 1,
  "key": ["关键词1", "关键词2"],
  "content": "设定描述文本",
  "constant": false,
  "custom_scope": "global"
}
```

| 字段 | 说明 |
|------|------|
| `key` | 触发关键词数组，任意子串命中即激活 |
| `constant` | `true` 时无条件注入（忽略 key） |
| `custom_scope` | `"global"` 对所有群生效；指定 group_id 则仅限该群 |

### 3.2 语义向量检索 (Semantic Lorebook)

在关键词匹配之上，系统支持基于向量相似度的语义检索（`utils/embedding.py`）：

```
用户消息文本
       │
       ▼
┌──────────────────────────────────┐
│  Stage 1: FAISS Recall           │
│  · L2 归一化 + IndexFlatIP       │
│  · 内积 = 余弦相似度             │
│  · Top-10 召回                   │
└──────────────┬───────────────────┘
               │
               ▼ (可选)
┌──────────────────────────────────┐
│  Stage 2: Reranker Precision     │
│  · SiliconFlow Reranker API      │
│  · 对 Top-10 重排序              │
└──────────────────────────────────┘
```

降级策略：FAISS 未安装 / API Key 缺失 / worldbook.json 不存在 → 静默跳过，不影响主链路。

### 3.3 SillyTavern Lorebook 引擎

`LorebookEngine`（`engine/lorebook_engine.py`）实现兼容 SillyTavern 的关键词扫描协议：

- **负向关键词**：前缀 `-` 实现否决（如 `-cat` 排除含 cat 的条目）
- **主关键词**：ANY 逻辑（任一命中即激活）
- **副关键词逻辑门**：`AND_ANY`、`NOT_ALL`、`NOT_ANY`、`AND_ALL`
- **递归级联**：已激活条目的 content 回馈扫描缓冲区，最深 `max_depth=10`
- **位置分类**：按 `(order ASC, depth ASC, content ASC)` 排序，分入 `wi_before` / `wi_after` / `wi_depth`

### 3.4 Topic Harvester：自动设定收割

话题归档时，系统自动从对话中提炼世界观设定，写入草稿箱等待人工审核：

```
话题 SUSPENDED 超过 30 分钟
       │
       ▼
┌──────────────────────────────────────────────────────┐
│  _archive_topic() [受信号量限流，最多 2 并发]         │
│                                                      │
│  1. 拉取话题全量消息                                  │
│                                                      │
│  2. 并行执行:                                        │
│     ┌─────────────────┐  ┌──────────────────────────┐│
│     │ _generate_topic  │  │ _extract_lore_from_topic ││
│     │ _summary()       │  │ ()                       ││
│     │                  │  │                          ││
│     │ LLM 生成 ≤200字  │  │ LLM JSON Mode 输出:     ││
│     │ 归档摘要          │  │ {"entries": [            ││
│     │                  │  │   {"key": [...],         ││
│     │                  │  │    "content": "...",     ││
│     │                  │  │    "constant": false}    ││
│     │                  │  │ ]}                       ││
│     └─────────────────┘  └──────────────────────────┘│
│                                                      │
│  3. 提炼结果写入 draft_worldbook.json                 │
│     · 每条附加 custom_scope = session_id              │
│     · 线程安全 (asyncio.Lock)                        │
│     · 自动递增 UID                                   │
│                                                      │
│  4. 通知 superusers 审核                              │
│     · 获取 Bot 实例：最多 3 次重试（间隔 5s）         │
│     · 发送私信：无重试，失败记 warning                │
│                                                      │
│  5. 更新话题状态 → ARCHIVED                          │
└──────────────────────────────────────────────────────┘
```

**提炼策略**（System Prompt 摘要）：

| 提取 | 不提取 |
|------|--------|
| 世界观：地名、组织、势力、规则体系 | 打招呼、表情包 |
| 人物设定：角色名、身份、能力、外貌 | 一次性闲聊 |
| 专有名词：术语、道具、技能名 | 无意义回复 |
| 关系网络：师徒、敌对、队友等固定关系 | |
| 重要事件：有长期影响的事件 | |

原则：**宁可多提，不要漏提**。让管理员在 Web 端审核时丢弃，而非遗漏重要设定。

**草稿审批闭环**：

```
Topic Harvester → draft_worldbook.json → Web 控制台「世界书」标签
                                              │
                                    ┌─────────┴─────────┐
                                    ▼                   ▼
                              批准 (Approve)        拒绝 (Reject)
                              分配新正式 UID        从草稿箱删除
                              追加到 worldbook.json
                              清理 custom_scope
```

---

## 4. 动态上下文与 Token 仲裁

### 4.1 Prompt 组装流程

`PromptPipeline`（`engine/prompt_builder.py`）负责将所有上下文组装为最终 Prompt：

```
┌──────────────────────────────────────────────────────────────┐
│  PromptPipeline.build()                                      │
│                                                              │
│  1. 角色卡 (CharacterCard)                                   │
│     ├─ system prompt (name + description + personality)      │
│     └─ 宏替换 ({{user}}, {{char}}, {{time}}, ...)           │
│                                                              │
│  2. Depth Injection                                          │
│     └─ 在指定 depth 插入历史消息                             │
│                                                              │
│  3. SystemBlock 组装                                         │
│     ├─ logic_directives / actor_world_knowledge              │
│     ├─ shadow_context (影子上下文, never_cut)                │
│     ├─ dynamic_rule (匹配的动态规则, never_cut)              │
│     ├─ session_lifecycle (生命周期感知, never_cut)           │
│     └─ system_tool_result (工具执行结果, never_cut)          │
│                                                              │
│  已废弃（_build_extra_blocks_from_snapshot 返回 []）：        │
│     · group_dynamics (社交关系网, Priority 4) — 不再注入     │
│     · group_memory (群友画像, Priority 5) — 不再注入         │
│                                                              │
│  4. TokenArbitrator 裁剪                                     │
│     └─ 超预算时按优先级裁剪                                  │
│                                                              │
│  5. 输出 ChatMessage[] → to_openai_format()                  │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 TokenArbitrator 裁剪机制

**优先级层次**（`engine/token_budget.py`）：

```
Priority 1: SYSTEM_DIRECTIVES  ──→  never_cut = True  (永不裁剪)
Priority 2: ROLE_PLAY_SETTING  ──→  never_cut = True  (永不裁剪)
Priority 3: CHAT_HISTORY       ──→  shift from oldest (从最旧开始移除)
Priority 4: GROUP_DYNAMICS     ──→  pop items from end (从末尾弹出)
Priority 5: GROUP_MEMORY       ──→  pop items from end (从末尾弹出)
Priority 6: WORLD_KNOWLEDGE    ──→  pop items from end (从末尾弹出)
```

**裁剪循环**（`_trim_loop`）：

```
while estimate > budget:
    for block in trimmable_blocks (priority DESC):
        if block.has_items:
            block.items.pop()          # 弹出最后一个 item
        elif block == CHAT_HISTORY:
            shift from oldest          # 移除最旧消息
            (保留 min_recent 条最近消息)
        estimate = re-estimate()
        if estimate <= budget:
            break

if still exceeds budget:
    _forced_fallback()                 # 剥离所有可裁剪内容
    仅保留 never_cut + min_recent

if even that exceeds:
    raise TokenBudgetExceeded          # 降级：注入截断通知
```

### 4.3 基于置信度的 Items 弹出策略（Legacy 设计）

> **注意**：自压缩机制移除后，`group_memory` 和 `group_dynamics` 不再注入任何 SystemBlock，因此以下弹出策略虽然代码存在（`TokenArbitrator.pop()` 逻辑），但当前实际操作的 `items` 列表始终为空。

`group_memory` 和 `group_dynamics` 两个 SystemBlock 使用 `items` 列表模式，条目按**置信度降序**排列：

```
group_memory.items = [
    "[123456] 喜欢二次元 (置信度: 0.95)",    ← 高置信度，排在前面
    "[789012] 是群管理 (置信度: 0.85)",      ← 不易被裁剪
    "[123456] 最近在学 Python (置信度: 0.60)",
    "[111111] 偶尔说冷笑话 (置信度: 0.40)",  ← 低置信度，排在末尾
    "[222222] 可能是学生 (置信度: 0.30)",    ← 最先被弹出
]
```

裁剪时 `pop()` 从末尾移除，即**低置信度条目最先牺牲**。这确保了高确信度的核心画像在 Token 预算紧张时优先保留。

```
Token 预算紧张时的裁剪顺序：

  ┌─────────────────────────────────────────┐
  │  group_memory.items                     │
  │                                         │
  │  [0] 置信度 0.95 ──→ 保留到最后        │
  │  [1] 置信度 0.85 ──→ 保留              │
  │  [2] 置信度 0.60 ──→ 保留              │
  │  [3] 置信度 0.40 ──→ 可能被裁          │
  │  [4] 置信度 0.30 ──→ 最先弹出 ← pop() │
  └─────────────────────────────────────────┘
```

### 4.4 Token 计数策略

两阶段计数，平衡性能与精度：

| 阶段 | 方法 | 触发条件 | 精度 |
|------|------|---------|------|
| Phase 1 | 自适应字符比率估算 | 始终执行 | 近似（Latin 4.0, CJK 1.8, Other 3.0 chars/token） |
| Phase 2 | `tiktoken cl100k_base` | Phase 1 接近预算时 | 精确 |

`_adaptive_ratio()` 扫描文本的字符类别分布，计算加权比率，`estimate_tokens()` 用此比率除文本长度。

---

## 5. 日志路由系统

### 5.1 Custom Callable Sink 架构

系统放弃 loguru 原生的 path 模板轮转，改用 **Custom Callable Sink** 实现按身份 + 按天的动态文件分离：

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
│    实现: 每条日志动态构建路径，天然按天轮转                   │
│                                                              │
│  Sink 3: 私聊日志 (Custom Callable)                          │
│    filter: record["extra"] 中有 private_user_id              │
│    路径: logs/private/{YYYY-MM-DD}_private_{uid}.log         │
│    实现: 同上                                               │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 Logger 绑定与传播

在 `chat_entry.py` 入口处，logger 被绑定上下文：

```python
if is_group:
    chat_logger = logger.bind(group_id=group_id)
else:
    chat_logger = logger.bind(private_user_id=user_id)
```

此绑定沿调用链自动传播——loguru 的 `bind()` 返回新 logger 实例，下游服务接收并使用该实例即可继承 `extra` 字段。注意：如果下游服务直接使用模块级 `from nonebot.log import logger` 而非接收绑定后的实例，则不会继承路由上下文。

### 5.3 文件命名与轮转

```
logs/
├── chatbot.log                           # 系统级（10MB 轮转，保留 20 个）
├── groups/
│   ├── 2026-05-12_group_123456.log       # 按天 × 群 自动创建
│   ├── 2026-05-12_group_789012.log
│   └── 2026-05-11_group_123456.log       # 旧日期文件自然沉淀
└── private/
    ├── 2026-05-12_private_111111.log     # 按天 × 用户 自动创建
    └── 2026-05-11_private_111111.log
```

无需外部轮转配置（如 logrotate），Custom Sink 按日期动态创建新文件，旧文件自然沉淀。物理清理由运维按需执行。

---

## 6. Web 控制台与配置热重载

### 6.1 零信任架构

```
┌──────────────────────────────────────────────────────────────┐
│  Web 管理面板 (127.0.0.1:8081)                               │
│                                                              │
│  启动方式：daemon 线程（随主进程退出）                        │
│  服务端：stdlib HTTPServer                                    │
│                                                              │
│  ┌─────────────┐         ┌──────────────────────────────┐   │
│  │  前端 SPA    │  ←→     │  后端 API (ConfigAPIHandler) │   │
│  │ index.html   │  HTTP   │                              │   │
│  │ (单文件)     │         │  GET /api/config             │   │
│  │              │         │  POST /api/config            │   │
│  │ 3 个 Tab:    │         │  GET /api/worldbook          │   │
│  │ · 全局设置   │         │  POST /api/worldbook/save    │   │
│  │ · 群管理     │         │  POST /api/worldbook/draft/  │   │
│  │ · 世界书     │         │      approve | reject        │   │
│  └─────────────┘         └──────────────────────────────┘   │
│                                                              │
│  鉴权：                                                     │
│    · Token = secrets.token_urlsafe(32)（每次重启重新生成）   │
│    · 注入方式：HTML <meta name="admin-token">               │
│    · 前端读取后立即 .remove()（一次性消费）                  │
│    · API 请求：Authorization: Bearer <token>                │
│    · 防时序攻击：secrets.compare_digest                      │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 功能清单

| 功能 | 端点 | 说明 |
|------|------|------|
| 查看全局配置 | `GET /api/config` | 返回所有 `Config` 字段为 JSON |
| 修改配置 | `POST /api/config` | 类型校验 → 候选构建 → 原子落盘 → 内存刷新 |
| 查看世界书 | `GET /api/worldbook` | 返回 `entries`（正式）+ `drafts`（待审） |
| 保存世界书 | `POST /api/worldbook/save` | 整体覆写正式词条 |
| 批准草稿 | `POST /api/worldbook/draft/approve` | 草稿 → 正式（分配新 UID，清理 custom_scope） |
| 拒绝草稿 | `POST /api/worldbook/draft/reject` | 从草稿箱删除 |

**配置修改流程**：

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
       │     · tempfile → yaml.dump → os.replace
       │     · 失败则内存不更新
       │
       └─ 4. 落盘成功 → 更新内存 _config
```

### 6.3 配置热重载

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

### 6.4 后台守护进程总览

```
┌──────────────────────────────────────────────────────────────┐
│  @driver.on_startup 启动的后台任务                           │
│                                                              │
│  #. 任务                          间隔       自愈包装        │
│  ─────────────────────────────────────────────────────────── │
│  1. MemoryCircuitBreaker.monitor  10s       裸协程 (raw)     │
│  2. EventLoopMonitor.start        1s        裸协程 (raw)     │
│  3. MemoryService.start_consumer  —         no-op（已废弃）  │
│  4. chat_entry._cleanup_sessions  10min     裸协程 (raw)     │
│  5. topic_harvester_daemon        60s       run_with_self_heal│
│  6. ttl_cleanup_loop              86400s    run_with_self_heal│
│  7. start_config_web_server       —         daemon 线程      │
│                                                              │
│  run_with_self_heal() 包装：                                 │
│    · 异常捕获 → 紧急告警 → 5 秒后自动重启                   │
│    · CancelledError → 正常退出                               │
│                                                              │
│  仅 #5、#6 使用了自愈包装；#1/#2/#4 为裸协程，              │
│  异常后不会自动重启（需依赖进程级重启）。                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 附录：Feature Flags

| Flag | 默认值 | 控制内容 |
|------|:------:|---------|
| `enable_dual_brain` | `True` | 双脑模式（关闭则退化为单脑 ReAct） |
| `enable_task_queue` | `False` | ~~记忆压缩走持久化队列 vs 火后不管~~（压缩机制已移除，此 flag 当前无实际效果） |
| `entity_relation_enabled` | `False` | ~~是否查询衰减关系注入 memorySnapshot~~（注入路径已移除，此 flag 当前无实际效果） |
| `semantic_lorebook_enabled` | `False` | 是否启用 FAISS 语义向量检索 |
| `token_arbitration_enabled` | `False` | 是否启用 Token 优先级裁剪 |

---

*本文档基于源码全局审阅生成，反映 2026-05-12 的系统状态。已根据压缩机制移除后的实际代码校正。*
