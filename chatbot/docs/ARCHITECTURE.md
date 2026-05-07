# 架构蓝图说明书

> Chatbot B 系统内部架构的权威技术文档。基于实际代码生成。

---

## 目录

1. [系统拓扑](#1-系统拓扑)
2. [数据持久层](#2-数据持久层)
3. [SillyTavern 提示词引擎](#3-sillytavern-提示词引擎)
4. [数据流转机制](#4-数据流转机制)
5. [动态规则引擎](#5-动态规则引擎)
6. [工具系统](#6-工具系统)
7. [配置管理机制](#7-配置管理机制)
8. [Feature Flag 机制](#8-feature-flag-机制)
9. [性能与可靠性](#9-性能与可靠性)
10. [安全架构](#10-安全架构)
11. [消息匹配与事件系统](#11-消息匹配与事件系统)
12. [工具模块](#12-工具模块)

---

## 1. 系统拓扑

### 1.1 单进程模型

系统运行在 **单一 Python 进程** 中，由 NoneBot2 框架驱动。不存在独立的 Node.js 服务——所有提示词编排、世界书扫描、Token 预算仲裁均在 Python 端完成（`engine/` 模块，SillyTavern 核心逻辑的纯 Python 重实现）。

| 组件 | 技术栈 | 端口 | 职责 |
|------|--------|------|------|
| **Python 主进程** | NoneBot2 + SQLAlchemy 2.0 Async + asyncio | — | 事件监听、业务逻辑、提示词编排、LLM 调用、数据持久化 |
| **Web 管理面板** | http.server (stdlib, daemon thread) | 8081 | 配置读写（Bearer Token 鉴权） |

### 1.2 外部服务依赖

```
┌─────────────────────────────────────────────────────────────┐
│                    Python 主进程 (NoneBot2)                  │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ DeepSeek │  │SiliconFlow│  │  go-cqhttp│  │ Pixiv DB │   │
│  │   API    │  │   API    │  │  (OneBot) │  │ (SQLite) │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
└───────┼──────────────┼──────────────┼──────────────┼─────────┘
        │              │              │              │
   Chat/Memory    Draw/Embed/     消息收发/       图片元数据
   Completion     Reranker       群管操作         查询
```

| 外部服务 | 用途 | 调用方式 |
|---------|------|---------|
| **DeepSeek API** | 聊天补全、记忆压缩提取、提示词增强、AI 审计 | `httpx.AsyncClient` POST |
| **SiliconFlow API** | AI 绘图、文本嵌入 (BAAI/bge-m3)、重排序 (BAAI/bge-reranker-v2-m3) | `httpx.AsyncClient` POST |
| **go-cqhttp (OneBot v11)** | QQ 消息收发、群管操作、文件上传 | NoneBot2 OneBot V11 Adapter |
| **Pixiv 图库 SQLite** | 图片元数据查询（`pixiv_master_image`, `pixiv_ai_info`） | `aiosqlite` 直连 |

### 1.3 目录结构

```
chatbot/
├── __init__.py          # 插件生命周期：启动/关闭钩子、后台任务管理
├── config.py            # ConfigManager + Web 管理面板 + YAML 热重载
├── guardian.py          # 熔断器 (MemoryCircuitBreaker) + 事件循环监控 (EventLoopMonitor)
├── schemas.py           # Pydantic 数据契约 (MemorySnapshot, UserProfile, Entity, Relation)
│
├── engine/              # SillyTavern 核心逻辑 (纯 Python 重实现)
│   ├── card_schema.py   #   角色卡 Pydantic 模型 (V1/V2/V3)
│   ├── card_parser.py   #   角色卡解析 (PNG tEXt / JSON / YAML)
│   ├── macro_engine.py  #   宏替换引擎 ({{placeholder}})
│   ├── prompt_builder.py#   提示词组装 (PromptPipeline, ChatCompletionBuilder, StoryStringBuilder)
│   ├── token_budget.py  #   优先级 Token 仲裁 (TokenArbitrator, 两阶段计数)
│   ├── depth_injection.py#  @Depth 深度注入机制
│   ├── api_formatters.py#   API 格式转换 (OpenAI / Anthropic / TextCompletion)
│   └── lorebook_engine.py#  世界书扫描 (关键词匹配 + 递归级联)
│
├── matchers/            # NoneBot2 事件匹配器
│   ├── chat_entry.py    #   主聊天入口 (沉浸会话、触发判定)
│   ├── admin_hard.py    #   管理员硬指令 (退群、活跃度调整)
│   └── event_notice.py  #   事件通知 (戳一戳、入群/退群)
│
├── services/            # 业务逻辑层
│   ├── agent_service.py #   核心 ReAct 循环编排器
│   ├── memory_service.py#   后台记忆压缩 (队列 + 日志 + 重试)
│   ├── prompt_adapter.py#   PromptAdapter: 桥接领域数据到 engine
│   ├── rule_engine.py   #   动态规则匹配引擎
│   ├── rule_injector.py #   规则→LLM 指令注入器
│   ├── permission_service.py # 权限与群管操作
│   ├── drawing_service.py#   AI 绘图 (SiliconFlow)
│   ├── image_service.py #   图片检索 + 隐写处理
│   └── book_service.py  #   JM 漫画下载/加密/发送
│
├── tools/               # Function Calling 工具层
│   ├── base_tool.py     #   抽象基类 (BaseTool)
│   ├── registry.py      #   工具注册表 + 权限过滤
│   ├── admin_tool.py    #   BanUserTool
│   ├── image_tool.py    #   GenerateImageTool, SearchAcgImageTool
│   ├── book_tool.py     #   RecommendBookTool, JmDownloadTool
│   ├── rule_tool.py     #   LearnRuleTool, ForgetRuleTool
│   └── system_tool.py   #   MarkTaskCompleteTool
│
├── repositories/        # 数据访问层
│   ├── models.py        #   SQLAlchemy 2.0 模型 (8 张表)
│   ├── memory_repo.py   #   MemoryRepository (单例, 主数据库)
│   ├── rule_repo.py     #   RuleRepository (复用 MemoryRepository 引擎)
│   ├── image_repo.py    #   ImageRepository (aiosqlite 直连 Pixiv DB)
│   └── book_repo.py     #   BookRepository (纯文件系统)
│
├── utils/               # 工具模块
│   ├── embedding.py     #   SemanticLorebook (FAISS + Reranker 两阶段语义检索)
│   ├── alert_manager.py #   超管紧急告警 (1 小时冷却)
│   ├── keyword_utils.py #   关键词规范化 + MD5 哈希
│   ├── string_utils.py  #   模糊匹配 + 编辑距离
│   ├── file_utils.py    #   异步原子文件 I/O
│   └── pdf_utils.py     #   ZIP→PDF 转换 + 元数据混淆
│
└── web/
    └── index.html       # Web 管理面板前端
```

---

## 2. 数据持久层

### 2.1 ER 图

系统使用 **单一 SQLite 数据库**（`data/chatbot_memory.db`），通过 SQLAlchemy 2.0 Async + aiosqlite 驱动。共 8 张表：

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   ChatHistory     │     │   GroupMemory     │     │    UserTrait      │
│  (chat_history)   │     │  (group_memory)   │     │   (user_trait)    │
├──────────────────┤     ├──────────────────┤     ├──────────────────┤
│ id (PK, autoinc) │     │ session_id (PK)  │     │ trait_id (PK,uuid)│
│ session_id (idx) │────→│ summary (Text)   │     │ session_id (idx) │
│ role             │     │ updated_at       │     │ user_id (idx)    │
│ user_id (idx)    │     └──────────────────┘     │ content (512)    │
│ name             │                              │ confidence (0-1) │
│ content (Text)   │     ┌──────────────────┐     │ source_msg_id    │
│ timestamp        │     │CompactionJournal  │     │ is_active (bool) │
│ is_summarized    │     │(compaction_journal)│    │ updated_at       │
│ tool_calls (JSON)│     ├──────────────────┤     └──────────────────┘
└──────────────────┘     │ journal_id(PK,uuid)│
                         │ session_id (idx) │     ┌──────────────────┐
  Composite index:       │ status           │     │     Entity        │
  (session_id, timestamp)│ retry_count      │     │    (entity)       │
                         │ max_retries      │     ├──────────────────┤
                         │ last_error       │     │ entity_id(PK,uuid)│
                         │ created_at       │     │ session_id (idx) │
                         │ updated_at       │     │ name (128)       │
                         └──────────────────┘     │ type (32)        │
                                                  │ attributes (JSON)│
┌──────────────────┐                              │ updated_at       │
│    Relation       │                              └──────────────────┘
│    (relation)     │
├──────────────────┤     ┌──────────────────┐     ┌──────────────────┐
│relation_id(PK,uuid)    │   CustomRule      │     │  RuleChangelog   │
│ session_id (idx) │     │ (custom_rule)    │     │(rule_changelog)  │
│subject_entity(32)│     ├──────────────────┤     ├──────────────────┤
│ predicate (128)  │     │ rule_id (PK)     │     │ id (PK, autoinc) │
│object_entity (32)│     │ scope_type       │     │ timestamp        │
│ confidence (0-1) │     │ scope_id         │     │ action           │
│evidence_msg(JSON)│     │ keywords_hash    │     │ rule_id          │
│ updated_at       │     │ keywords (JSON)  │     │ operator         │
└──────────────────┘     │ tool_name        │     │ scope_type       │
                         │ args_extractor   │     │ scope_id         │
  Unique constraint:     │ pattern_id       │     │ old_value (JSON) │
  (session_id,           │ description      │     │ new_value (JSON) │
   subject_entity,       │ examples (JSON)  │     └──────────────────┘
   predicate,            │ hit_count
   object_entity)        │ last_hit
                         │ created_at
                         │ created_by
                         │ updated_at
                         │ ttl_days (default 30)
                         │ active (0/1)
                         │ priority
                         │ confidence (0-1)
                         │ allow_forced_exec (0/1)
                         └──────────────────┘

  Unique constraint:
  (scope_type, scope_id,
   keywords_hash)
```

### 2.2 表设计理念

| 表 | 设计理念 |
|---|---|
| **ChatHistory** | 每条消息独立一行，支持增量总结（`is_summarized` 游标）和溯源（`id` 自增）。`tool_calls` 字段存储 JSON 格式的 Function Calling 数据 |
| **GroupMemory** | 每个 session 一行的宏观摘要，upsert 更新，供 LLM 作为背景上下文 |
| **UserTrait** | 每条特征独立一行，支持置信度（`confidence`）、溯源（`source_msg_id`）和逻辑删除（`is_active`）。唯一约束 `(session_id, user_id, content)` 防重 |
| **CompactionJournal** | 压缩任务状态机（pending→running→success→failed→dead），支持僵尸任务恢复和死信追踪 |
| **Entity** | 知识图谱节点，按 `entity_id` upsert，`attributes` 为自由 JSON |
| **Relation** | 知识图谱三元组（S-P-O），唯一约束防重，`evidence_msg_ids` 合并去重，支持时间衰减 |
| **CustomRule** | 动态关键词触发规则，`keywords_hash` 用于冲突检测，`ttl_days` 控制自动过期 |
| **RuleChangelog** | 规则变更审计日志，记录 create/update/delete 操作的 old/new 值 |

### 2.3 Repository 模式

| Repository | 模式 | 存储方式 | 说明 |
|-----------|------|---------|------|
| **MemoryRepository** | 单例（`__new__` 覆写） | SQLAlchemy Async + aiosqlite | 主数据库，管理引擎和会话工厂的生命周期 |
| **RuleRepository** | 单例 | 复用 MemoryRepository 的引擎 | 共享同一 SQLite 文件，`init_db()` 确保规则表存在 |
| **ImageRepository** | 非单例，每次实例化 | 原始 aiosqlite 直连 | 查询独立的 Pixiv 图库数据库 |
| **BookRepository** | 非单例 | 纯文件系统 | 扫描图书目录，无数据库 |

### 2.4 时间衰减机制

关系（Relation）的置信度随时间自然淡化：

```
effective_confidence = confidence × 0.5 ^ (age_days / half_life_days)
```

- **默认半衰期**：30 天
- **过滤阈值**：`effective_confidence < 0.15` 的关系自动排除
- **实现位置**：`MemoryRepository.get_relations_with_decay()`
- **返回格式**：字典列表（非 ORM 对象），包含 `decayed_confidence` 字段

此机制确保 LLM 在群聊回复时感知的是"当前活跃"的关系，而非历史累积的全量数据。

---

## 3. SillyTavern 提示词引擎

`engine/` 模块是 SillyTavern 核心提示词组装逻辑的 **纯 Python 重实现**，替代了旧架构中的 Node.js 服务。它负责角色卡解析、宏替换、世界书扫描、深度注入、Token 预算仲裁和 API 格式转换。

### 3.1 核心管线：PromptPipeline

```
CharacterCard (JSON/PNG/YAML)
        │
        ▼
┌─ MacroEngine.substitute() ──────────────────────────────┐
│  {{char}} → "Elena"    {{user}} → "User"                │
│  {{description}}       {{personality}}                   │
│  {{time}} / {{date}}   {{mesExamples}}                   │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ LorebookEngine.recursive_scan() ───────────────────────┐
│  · 关键词匹配 (AND_ANY / AND_ALL / NOT_ALL / NOT_ANY)   │
│  · 递归级联 (新激活条目的 content 追加到扫描缓冲区)      │
│  · 位置分类 → wi_before / wi_after / wi_depth           │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ ChatCompletionBuilder ─────────────────────────────────┐
│  按 prompt_order 组装:                                   │
│  main → worldInfoBefore → personaDescription             │
│  → charDescription → charPersonality → scenario          │
│  → enhanceDefinitions → nsfw → worldInfoAfter            │
│  → dialogueExamples → chatHistory → jailbreak            │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ DepthInjection.inject_at_depth() ──────────────────────┐
│  按 depth (距末尾偏移) 插入扩展提示                       │
│  同 depth 按 order 降序排列                               │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ TokenArbitrator.apply_budget() ────────────────────────┐
│  Phase 1: 粗估 (len/3.35)，按优先级裁剪 trimmable blocks │
│  Phase 2: 精确 (tiktoken cl100k_base) 最终校验           │
│  never_cut=True 的块 (SYSTEM_DIRECTIVES, ROLE_PLAY) 不裁 │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ APIFormatter ──────────────────────────────────────────┐
│  OpenAI: [{role, content, name?, tool_calls?}]           │
│  Anthropic: {system: "...", messages: [...]}             │
│  TextCompletion: "Name: content\n..."                    │
└──────────────────────────────────────────────────────────┘
```

### 3.2 Token 预算仲裁

`TokenArbitrator` 实现基于优先级的两阶段裁剪：

| 优先级 | 名称 | 数值 | never_cut | 说明 |
|--------|------|------|-----------|------|
| SYSTEM_DIRECTIVES | 系统指令 | 1 | **是** | 动态规则注入、安全边界 |
| ROLE_PLAY_SETTING | 角色扮演设定 | 2 | **是** | 角色卡 system_prompt、description |
| CHAT_HISTORY | 聊天历史 | 3 | 否 | 从旧到新裁剪，保留 `min_recent` 条 |
| GROUP_DYNAMICS | 群聊动态 | 4 | 否 | 关系图谱 |
| GROUP_MEMORY | 群聊记忆 | 5 | 否 | 摘要 + 用户画像 |
| WORLD_KNOWLEDGE | 世界知识 | 6 | 否 | 世界书条目，最优先被裁剪 |

**两阶段计数**：
- **Phase 1（粗估）**：`len(text) / 3.35` 字符/token，极低成本，用于快速迭代裁剪
- **Phase 2（精确）**：`tiktoken` `cl100k_base` 编码，最终校验，超限抛出 `TokenBudgetExceeded`

### 3.3 角色卡格式

支持三种输入格式，统一解析为 `CharacterCard` Pydantic 模型：

| 格式 | 解析方式 | 说明 |
|------|---------|------|
| **PNG tEXt** | 读取 `ccv3` (V3) 或 `chara` (V2) chunk，Base64→JSON | V3 优先于 V2 |
| **JSON** | 直接解析 | V1 (无 `spec` 字段) / V2/V3 (有 `spec`) |
| **YAML** | 自动检测后解析 | 字段映射：`context`→`description`, `greeting`→`first_mes` |

---

## 4. 数据流转机制

### 4.1 单次请求生命周期

```
用户发送消息 (@Bot / 唤醒词 / 沉浸会话 / 私聊)
        │
        ▼
┌─ 1. 事件入口 (matchers/) ────────────────────────────────┐
│  admin_hard (priority=3): 管理员指令拦截                   │
│  chat_entry (priority=10): 消息解析 + 触发判定             │
│    · @ 提及保留 (@Bot / @Name(QQ:id) / @全体成员)         │
│    · 沉浸会话窗口 (120s) + 对话转移检测                    │
│    · 新群需管理员首次激活                                   │
│    · 构建 context (bot, services, permissions, flags)      │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 2. AgentService.run_agent() ────────────────────────────┐
│  2a. 规则匹配 (RuleEngine.match) → context['_matched_rule']│
│  2b. 第一时间落库: add_message(role="user")                │
│  2c. 精准备料 (全部带 3s 超时降级):                         │
│      · get_recent_messages(limit=30) → chatHistory         │
│      · get_active_profiles() → profiles                    │
│      · get_group_summary() → summary                       │
│      · get_relations_with_decay() → relations              │
│      · semantic_lorebook.search() → semantic_hits          │
│  2d. 获取工具 schema (按权限过滤)                           │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 3. ReAct 循环 (最多 agent_max_loops 轮) ───────────────┐
│  ┌──────────────────────────────────────────────────────┐ │
│  │ 3a. PromptAdapter.compile_actor_prompt()              │ │
│  │     · 角色卡 + 宏替换                                 │ │
│  │     · 世界书扫描 (关键词 + 语义向量)                   │ │
│  │     · 注入群聊记忆/动态/规则指令                       │ │
│  │     · Token 预算仲裁                                  │ │
│  │     · → OpenAI 格式 messages                          │ │
│  └──────────────────────────────────────────────────────┘ │
│                         │                                  │
│                         ▼                                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ 3b. DeepSeek API 调用 (httpx POST)                    │ │
│  │     · 含 tools 定义                                   │ │
│  │     · thinking: disabled                              │ │
│  └──────────────────────────────────────────────────────┘ │
│                         │                                  │
│              ┌──────────┴──────────┐                       │
│              ▼                     ▼                       │
│        无 tool_calls          有 tool_calls                │
│              │                     │                       │
│              ▼                     ▼                       │
│     检查兜底执行规则         ToolRegistry.execute_tool()   │
│     (低风险+allow_forced)    · 权限二次验证                │
│              │               · 执行 + 持久化 tool 消息     │
│              │               · 检查 mark_task_complete     │
│              │                     │                       │
│              ▼                     ▼                       │
│         退出循环              continue 下一轮              │
│                                                          │
│  终止条件:                                                │
│    · 无 tool_calls (自然结束)                              │
│    · mark_task_complete (显式完成信号)                     │
│    · Jaccard 重复检测 (similarity > 0.9)                  │
│    · max_loops 兜底 (默认 10)                             │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 4. 返回结果 ────────────────────────────────────────────┐
│  · 更新规则命中统计 (increment_hit_count)                  │
│  · 触发后台记忆压缩 (熔断器保护)                           │
│  · 返回 {text, images} 给 chat_entry                      │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 5. chat_entry 发送回复 ─────────────────────────────────┐
│  · 文本消息 → chat_entry.send(reply_text)                 │
│  · 图片 → MessageSegment.image(file:///path)              │
└──────────────────────────────────────────────────────────┘
```

### 4.2 记忆压缩流程

```
AgentService.run_agent() 结束
        │
        ▼
  circuit_breaker.allow_new_task() ?
        │
   ┌────┴────┐
   ▼         ▼
  允许      熔断中 → 跳过
   │
   ▼
┌─ MemoryService.process_session_memory() ─────────────────┐
│                                                          │
│  轻量模式 (enable_task_queue=False):                      │
│    asyncio.create_task → _do_process (无持久化)           │
│                                                          │
│  高可用模式 (enable_task_queue=True):                     │
│    入队 → CompactionJournal 持久化                        │
│    消费者协程逐个出队                                      │
│    指数退避重试 (最多 3 次, 402/403 跳过重试)              │
│    超限进入死信队列 (_dlq)                                 │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ _do_process() 核心逻辑 ────────────────────────────────┐
│  1. 获取未总结消息 (is_summarized=False)                   │
│     → 不足 15 条则跳过                                     │
│  2. 获取已有群组摘要                                       │
│  3. 格式化对话 ([ID:uid] 前缀)                            │
│  4. 调用 DeepSeek API (强制 tool_choice)                  │
│     → 返回 update_memory_graph 结构化数据:                │
│       {group_summary, user_traits[], entities[], relations[]}│
│  5. 持久化:                                               │
│     · upsert GroupSummary (与已有摘要合并)                 │
│     · upsert UserTraits (按 user_id 分组)                 │
│     · upsert Entities                                     │
│     · upsert Relations                                    │
│  6. 标记已总结 (is_summarized=True)                       │
└──────────────────────────────────────────────────────────┘
```

---

## 5. 动态规则引擎

动态规则引擎允许管理员通过自然语言对话（`learn_rule` / `forget_rule` 工具）创建关键词触发规则，使 LLM 在匹配到特定关键词时自动调用指定工具。

### 5.1 架构总览

```
管理员: "记住，当有人说'发书'的时候就推荐一本书"
        │
        ▼
┌─ LearnRuleTool.execute() ───────────────────────────────┐
│  · 关键词规范化 (去停用词、去重、排序、小写)              │
│  · MD5 哈希 → keywords_hash                              │
│  · 冲突检测 (find_by_hash)                               │
│  · 创建/覆写规则 → CustomRule 表                         │
│  · 审计日志 → RuleChangelog 表                           │
└──────────────────────────────────────────────────────────┘
        │
        ▼
规则入库，等待触发
        │
        ▼
用户: "发点书来看看"
        │
        ▼
┌─ RuleEngine.match() ────────────────────────────────────┐
│  1. 获取当前 scope 的活跃规则                             │
│  2. RuleEngineCore.match():                              │
│     · AND 关键词过滤 (所有关键词必须出现在消息中)          │
│     · 参数可提取性验证                                    │
│     · 排序: priority DESC → confidence DESC               │
│            → hit_count DESC → keyword_length DESC         │
│  3. extract_args(): 从消息中提取工具参数                   │
│  4. 结果写入 context['_matched_rule']                     │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 双路径触发 ────────────────────────────────────────────┐
│                                                          │
│  路径 A: LLM 主动调用 (首选)                              │
│    RuleInjector 将规则指令注入 PromptAdapter              │
│    → LLM 看到指令后自行输出 tool_call                     │
│    → AgentService 执行工具                                │
│                                                          │
│  路径 B: 兜底强制执行 (fallback)                          │
│    LLM 未输出 tool_calls，但规则匹配成功                  │
│    → 检查: tool.risk_level == "low"                      │
│           AND tool.allow_forced_exec == True              │
│    → 伪造 assistant + tool 消息对 (matching IDs)          │
│    → 执行工具，追加到上下文，继续循环让 LLM 生成最终回复   │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 5.2 参数提取器类型

| 提取器 | 说明 | 输出示例 |
|--------|------|---------|
| `none` | 无参数 | `{}` |
| `number_list` | 从关键词附近的窗口中提取数字 | `{"ids": ["350234", "350235"]}` |
| `string_after_kw` | 提取关键词之后的文本 | `{"keywords": "风景 二次元"}` |
| `pattern` | 使用预编译正则匹配 | `{"match": "350234"}` |

预编译安全正则（`SAFE_PATTERNS`）：

| 模式 | 正则 | 用途 |
|------|------|------|
| `JM_ID` | `\b\d{5,}\b` | JM 禁漫 ID |
| `MENTION` | `\[CQ:at,qq=(\d+)\]` | OneBot @ 提及 |
| `URL` | `https?://\S+` | URL 链接 |

### 5.3 规则作用域

规则按 `(scope_type, scope_id)` 隔离：

| scope_type | scope_id | 说明 |
|-----------|----------|------|
| `global` | `*` | 全局生效 |
| `group` | `{group_id}` | 仅在指定群生效 |
| `private` | `{user_id}` | 仅在指定私聊生效 |

### 5.4 规则生命周期

- **TTL 过期**：`cleanup_stale_rules()` 每 24 小时执行一次，基于 `last_hit + ttl_days` 或 `created_at + ttl_days` 判定过期
- **软删除**：`delete_rule()` 设置 `active=0` 而非物理删除
- **命中统计**：`increment_hit_count()` 使用原子 `UPDATE SET hit_count=hit_count+1`
- **冲突检测**：`keywords_hash` (MD5) + `(scope_type, scope_id)` 唯一约束

---

## 6. 工具系统

### 6.1 工具注册表 (ToolRegistry)

```
ToolRegistry
├── register(tool) / unregister(name)
├── get_all_schemas(permissions, user_id, is_admin)
│   └── 按权限过滤后返回 OpenAI Function Calling schema
└── execute_tool(name, arguments, context)
    └── 权限二次验证 → tool.execute() → (text, images[])
```

### 6.2 权限模型

| 权限等级 | 说明 | Schema 过滤 | 执行验证 |
|---------|------|------------|---------|
| `user` | 所有用户可用 | 始终包含 | 始终放行 |
| `drawing_whitelist` | 画图白名单用户 | `is_user_whitelisted(uid, "drawing")` | 同左 |
| `admin` | 群管理/超管 | `is_admin == True` | 同左 |
| `system` | 系统内部工具 | 始终注入（最后） | 跳过权限检查 |

### 6.3 风险控制

| 属性 | 说明 | 作用 |
|------|------|------|
| `risk_level` | `"low"` / `"high"` | 高风险工具不允许兜底强制执行 |
| `allow_forced_exec` | `True` / `False` | 控制规则引擎是否可自动触发此工具 |

### 6.4 工具清单

| 工具 | 权限 | 风险 | 强制执行 | 说明 |
|------|------|------|---------|------|
| `generate_image` | drawing_whitelist | low | 否 | AI 绘图 (SiliconFlow) |
| `search_acg_image` | user | low | 是 | 图库搜索 |
| `ban_user` | admin | **high** | **否** | 群禁言（需管理员权限） |
| `recommend_book` | user | low | 是 | 随机推荐书籍 |
| `jm_download` | user | low | 是 | JM 漫画下载 |
| `learn_rule` | admin | — | — | 创建/更新动态规则 |
| `forget_rule` | admin | — | — | 删除动态规则 |
| `mark_task_complete` | system | low | 否 | 显式完成信号（仅 enable_dynamic_loop 时注册） |

---

## 7. 配置管理机制

### 7.1 ConfigManager 架构

```
config_bot_base.yaml (磁盘)
        │
        ▼
┌─ ConfigManager (单例) ──────────────────────────────────┐
│  · threading.Lock 保护 load/save                         │
│  · __setattr__ / __getattr__ 代理到内部 Config 对象      │
│  · 自动生成 admin token (secrets.token_urlsafe(32))      │
└──────────────────────────────────────────────────────────┘
        │
   ┌────┴────────────────────┐
   ▼                         ▼
watchdog Observer         Web API (port 8081)
(YAML 文件监听)           (ConfigAPIHandler)
   │                         │
   ▼                         ▼
0.5s 防抖自动重载         GET /api/config → JSON
                          POST /api/config → 合并写入
```

### 7.2 配置分组

| 配置块 | 字段 | 说明 |
|--------|------|------|
| **DeepSeek** | `deepseek_api_key`, `deepseek_api_url`, `deepseek_model_name`, `deepseek_memory_model` | LLM API 配置 |
| **SiliconFlow** | `siliconflow_api_key`, `siliconflow_api_url`, `siliconflow_model`, `embedding_model`, `reranker_model`, `enable_reranker` | 绘图/嵌入/重排序 |
| **Agent 循环** | `agent_max_loops` (默认 10), `agent_request_timeout` (默认 60s) | ReAct 循环控制 |
| **Feature Flags** | 6 个灰度开关 | 见 [第 8 章](#8-feature-flag-机制) |
| **绘图** | `drawing_enhance_timeout` (默认 30s) | 提示词增强超时 |
| **路径** | `image_folder`, `books_folder`, `db_path`, `jm_download_dir`, `jm_option_path`, `font_path` | 文件/目录路径 |
| **权限集** | `superusers`, `private_whitelist`, `ai_admin_qq`, `drawing_whitelist`, `welcome_groups` | QQ 号集合 |
| **其他** | `welcome_mode` (默认 "all"), `group_configs` | 入群欢迎模式、群组配置 |

### 7.3 群组配置 (GroupSettings)

每个群独立配置，存储在 `group_configs` 字典中，key 为群号字符串：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `random_reply_prob` | float | 0.0 | 随机插嘴概率 (0.0-1.0) |
| `record_all_messages` | bool | False | 是否静默记录所有消息 |
| `allowed_tools` | list | `["search_acg_image", "recommend_book"]` | 群内可用工具 |
| `allow_r18` | bool | False | 是否允许 R18 内容 |

### 7.4 原子写入

`save_config()` 使用临时文件 + `os.replace()` 策略：
1. 写入 `{path}.tmp` 临时文件
2. `os.replace(tmp, target)` — OS 层面原子替换
3. 写入失败时清理临时文件

---

## 8. Feature Flag 机制

所有 Feature Flag 在 `config.py` 的 `Config` 类中定义，默认关闭。可通过 YAML 文件或 Web 管理面板实时切换。

| Flag | 作用域 | 控制内容 |
|------|--------|----------|
| `enable_strict_schema` | Python | Pydantic `ChatRequestPayload` 校验严格模式 |
| `enable_task_queue` | Python | 记忆压缩走队列（持久化 + 重试 + 死信）还是直接 `create_task` |
| `enable_dynamic_loop` | Python | Jaccard 重复检测 + `mark_task_complete` 工具注册 |
| `entity_relation_enabled` | Python | 是否查询衰减关系并注入 memorySnapshot |
| `semantic_lorebook_enabled` | Python | 是否启用 FAISS 语义向量检索 |
| `token_arbitration_enabled` | Python | 是否启用优先级 Token 裁剪（通过 context 透传给 engine） |

### 8.1 Flag 影响链路

```
enable_task_queue
  └─→ MemoryService.process_session_memory()
       ├─ True:  入队 → CompactionJournal 持久化 → 消费者协程
       └─ False: asyncio.create_task 直接执行

enable_dynamic_loop
  └─→ AgentService._register_tools()
       ├─ True:  注册 MarkTaskCompleteTool
       └─ False: 不注册
  └─→ AgentService.run_agent() ReAct 循环
       ├─ True:  启用 Jaccard 重复检测 + mark_task_complete 退出信号
       └─ False: 仅自然结束和 max_loops 兜底

semantic_lorebook_enabled
  └─→ AgentService.run_agent() 每轮循环
       ├─ True:  执行 FAISS 语义检索，结果注入 lorebook_context
       └─ False: 跳过语义检索

entity_relation_enabled
  └─→ AgentService.run_agent() 备料阶段
       ├─ True:  查询衰减关系，注入 memory_snapshot
       └─ False: 跳过关系查询，relations 为空

token_arbitration_enabled
  └─→ PromptAdapter.compile_actor_prompt() → lorebook_context
       └─→ TokenArbitrator.apply_budget()
            ├─ True:  执行优先级裁剪
            └─ False: 跳过裁剪（可能超限）
```

---

## 9. 性能与可靠性

### 9.1 熔断器 (MemoryCircuitBreaker)

保护记忆压缩队列免受 LLM API 故障导致的积压：

```
        ┌──────────┐
        │  CLOSED  │ ← 正常状态
        │(允许入队) │
        └────┬─────┘
             │ queue.qsize() > 50
             ▼
        ┌──────────┐
        │ HALF_OPEN│ ← 降级观察
        │(仍允许入队)│
        └────┬─────┘
             │ worker 退出 (task.done())
             ▼
        ┌──────────┐
        │   OPEN   │ ← 熔断
        │(拒绝入队) │
        └──────────┘

恢复路径:
  HALF_OPEN → CLOSED: queue.qsize() < 10
  OPEN → HALF_OPEN: worker 自动恢复
  OPEN → CLOSED: on_worker_dead 回调重启 worker
```

- **监控间隔**：10 秒轮询
- **调用方**：`AgentService.run_agent()` 在触发记忆压缩前检查 `allow_new_task()`
- **重启回调**：`_restart_memory_worker()` — shutdown → sleep(0.5) → start_consumer

### 9.2 事件循环监控 (EventLoopMonitor)

检测同步阻塞导致的事件循环卡顿：

- **原理**：每秒测量 `asyncio.sleep(1.0)` 的实际耗时
- **drift = (实际耗时 - 1.0)**
- **drift > 1.5s**：WARNING 级别日志
- **drift > 5.0s**：ERROR 级别日志，提示系统响应严重退化

### 9.3 超时降级策略

所有数据库查询和外部调用均设有超时保护：

| 操作 | 超时 | 降级行为 |
|------|------|---------|
| `get_recent_messages` | 3s | 降级为空历史 |
| `get_active_profiles` | 3s | 降级为空画像 |
| `get_group_summary` | 3s | 降级为空摘要 |
| `get_relations_with_decay` | 3s | 降级为空关系 |
| `semantic_lorebook.search` | 3s | 降级为纯关键词匹配 |
| 工具执行 | 15s | 返回超时错误提示 |
| LLM API 请求 | 60s (可配置) | 返回"连接失败"提示 |

### 9.4 后台任务自愈

`run_with_self_heal()` 包装器：
- `CancelledError` → 正常退出
- 其他异常 → 日志 + 紧急告警 + 5 秒后自动重启
- 应用于：TTL 清理循环

### 9.5 事件循环卸载

CPU/IO 密集型同步操作通过 `loop.run_in_executor()` 卸载到线程池：
- `ImageService.generate_stealth()` — 隐写图片生成
- `BookService._sync_download_task()` — JM 漫画下载
- `BookService._encrypt_pdf_task()` — PDF 加密
- `PDFUtils.convert_zip_to_pdf()` — ZIP→PDF 转换

### 9.6 文件原子写入

| 场景 | 实现 |
|------|------|
| YAML 配置保存 | `tempfile` + `os.replace()` |
| JSON 文件写入 | `AsyncFileUtils.write_json`: `.tmp` + `os.replace()` |

### 9.7 重复输出检测

ReAct 循环中启用 `enable_dynamic_loop` 时，使用 Jaccard 相似度检测连续两轮输出：
- 阈值：0.9
- 超过阈值 → 退出循环，防止 LLM 陷入重复

### 7.1 ConfigManager 架构

```
config_bot_base.yaml (磁盘)
        │
        ▼
┌─ ConfigManager (单例) ──────────────────────────────────┐
│  · threading.Lock 保护 load/save                         │
│  · __setattr__ / __getattr__ 代理到内部 Config 对象      │
│  · 自动生成 admin token (secrets.token_urlsafe(32))      │
└──────────────────────────────────────────────────────────┘
        │
   ┌────┴────────────────────┐
   ▼                         ▼
watchdog Observer         Web API (port 8081)
(YAML 文件监听)           (ConfigAPIHandler)
   │                         │
   ▼                         ▼
0.5s 防抖自动重载         GET /api/config → JSON
                          POST /api/config → 合并写入
```

### 7.2 配置分组

| 配置块 | 字段 | 说明 |
|--------|------|------|
| **DeepSeek** | `deepseek_api_key`, `deepseek_api_url`, `deepseek_model_name`, `deepseek_memory_model` | LLM API 配置 |
| **SiliconFlow** | `siliconflow_api_key`, `siliconflow_api_url`, `siliconflow_model`, `embedding_model`, `reranker_model`, `enable_reranker` | 绘图/嵌入/重排序 |
| **Agent 循环** | `agent_max_loops` (默认 10), `agent_request_timeout` (默认 60s) | ReAct 循环控制 |
| **Feature Flags** | 6 个灰度开关 | 见 [第 8 章](#8-feature-flag-机制) |
| **绘图** | `drawing_enhance_timeout` (默认 30s) | 提示词增强超时 |
| **路径** | `image_folder`, `books_folder`, `db_path`, `jm_download_dir`, `jm_option_path`, `font_path` | 文件/目录路径 |
| **权限集** | `superusers`, `private_whitelist`, `ai_admin_qq`, `drawing_whitelist`, `welcome_groups` | QQ 号集合 |
| **其他** | `welcome_mode` (默认 "all"), `group_configs` | 入群欢迎模式、群组配置 |

### 7.3 群组配置 (GroupSettings)

每个群独立配置，存储在 `group_configs` 字典中，key 为群号字符串：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `random_reply_prob` | float | 0.0 | 随机插嘴概率 (0.0-1.0) |
| `record_all_messages` | bool | False | 是否静默记录所有消息 |
| `allowed_tools` | list | `["search_acg_image", "recommend_book"]` | 群内可用工具 |
| `allow_r18` | bool | False | 是否允许 R18 内容 |

### 7.4 原子写入

`save_config()` 使用临时文件 + `os.replace()` 策略：
1. 写入 `{path}.tmp` 临时文件
2. `os.replace(tmp, target)` — OS 层面原子替换
3. 写入失败时清理临时文件

---

## 10. 安全架构

### 10.1 管理面板零信任

```
┌─ Web 管理面板 (port 8081) ──────────────────────────────┐
│                                                          │
│  启动时:                                                 │
│    token = secrets.token_urlsafe(32)                     │
│    → 注入 HTML <meta name="admin-token" content="...">   │
│    → 前端 JS 读取后立即 .remove()                         │
│                                                          │
│  每次请求:                                               │
│    Authorization: Bearer <token>                         │
│    → secrets.compare_digest (常量时间比较, 防时序攻击)    │
│                                                          │
│  Token 生命周期:                                         │
│    · 每次 Python 进程重启生成新 Token                     │
│    · 不持久化到磁盘                                      │
│    · 仅内存中存活                                        │
└──────────────────────────────────────────────────────────┘
```

### 10.2 工具权限双重验证

```
第一次验证 (Schema 过滤):
  get_all_schemas(permissions, user_id, is_admin)
  → 仅返回用户有权使用的工具 schema 给 LLM

第二次验证 (执行时):
  execute_tool(name, arguments, context)
  → 再次检查权限，防止 LLM 伪造 tool_call 绕过
```

### 10.3 工具风险控制

```
管理员可教学的工具:
  LEARNABLE_TOOLS = ["jm_download", "search_acg_image", "recommend_book", "generate_image"]

禁止教学的工具 (DANGEROUS_TOOLS):
  DANGEROUS_TOOLS = {"ban_user"}

兜底执行条件 (所有必须满足):
  1. 规则匹配成功
  2. LLM 未输出 tool_calls
  3. tool.risk_level == "low"
  4. tool.allow_forced_exec == True
```

### 10.4 提示词注入防护

| 防护层 | 机制 |
|--------|------|
| **变量转义** | `PromptAdapter._escape()` — HTML 实体转义 (`<`, `>`, `&`, `"`, `'`) |
| **XML 结构隔离** | 群聊记忆/动态/规则指令均包裹在 `<group_memory>`, `<relation>`, `<rule_instruction>` 等 XML 标签中 |
| **规则指令截断** | `RuleInjector.build_instruction()`: description 截断 100 字符, 最多 2 个 example, 每个 example 截断 120 字符 |
| **行为边界警告** | 规则指令末尾注入："仅执行此规则指定的操作，不要过度解读" |
| **R18 内容门控** | `context["allow_r18"]` 双重检查：工具层 AND 群组配置 |

### 10.5 新群激活机制

未注册的群必须同时满足以下条件才能激活：
1. 用户 @ 了机器人 (`event.is_tome()`)
2. 用户是超管或 AI 管理员 (`is_superuser` OR `is_ai_admin`)

激活后自动创建 `GroupSettings()` 并保存到 YAML。

---

## 11. 消息匹配与事件系统

### 11.1 匹配器优先级

```
NoneBot2 on_message 事件流
        │
        ▼
┌─ admin_hard (priority=3) ───────────────────────────────┐
│  管理员指令拦截 (退群、活跃度调整、潜水模式、画图白名单)   │
│  命中 → block=False (不阻塞后续匹配器)                   │
└──────────────────────────────────────────────────────────┘
        │
        ▼
┌─ chat_entry (priority=10) ──────────────────────────────┐
│  主聊天入口 (见下方触发判定)                               │
│  命中 → block=False                                      │
└──────────────────────────────────────────────────────────┘
```

### 11.2 聊天触发判定

**群聊触发条件**（任一满足）：

| 条件 | 说明 |
|------|------|
| `@Bot` | `event.is_tome()` |
| 回复 Bot 消息 | `event.reply.sender.user_id == bot.self_id` |
| 唤醒词 | 消息包含 `elena` / `Elena` / `艾蕾娜`（不区分大小写） |
| 沉浸会话 | 120 秒内曾触发过回复，且未发生对话转移 |
| 随机插嘴 | `random.random() < random_reply_prob` |

**对话转移检测**：
- 引用回复了非 Bot 用户
- @ 了非 Bot 用户
- 检测到转移 → 沉浸会话不生效，Bot 保持沉默

**私聊**：始终触发回复。

### 11.3 沉浸会话管理

```python
ACTIVE_SESSIONS: dict[int, dict[str, float]]
# {group_id: {user_id: last_active_timestamp}}
```

- **窗口期**：120 秒
- **清理**：每 10 分钟后台协程 `_cleanup_sessions()` 清理过期条目
- **更新时机**：每次成功触发回复后更新时间戳

### 11.4 事件通知 (event_notice)

| 事件 | 处理 |
|------|------|
| **戳一戳** | 状态机：正常回复 → 发图 → 禁言 → 呼叫主人，30 秒重置窗口 |
| **入群** | 调用 `agent_srv.run_agent()` 生成欢迎语（受 `welcome_groups` 和 `welcome_mode` 控制） |
| **退群** | 同上，生成告别语 |

---

## 12. 工具模块

### 12.1 语义检索 (SemanticLorebook)

两阶段语义向量检索系统：

```
用户消息
    │
    ▼
┌─ Stage 1: Recall (FAISS) ──────────────────────────────┐
│  · 文本 → Embedding API (SiliconFlow, BAAI/bge-m3)     │
│  · FAISS IndexFlatIP 搜索 (L2 归一化 = 余弦相似度)      │
│  · 召回 top-10 候选，阈值 > 0.3                         │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─ Stage 2: Rerank (可选) ───────────────────────────────┐
│  · Reranker API (BAAI/bge-reranker-v2-m3)              │
│  · 重新评分，返回 top-3                                  │
│  · 失败时降级为 FAISS 结果                               │
└─────────────────────────────────────────────────────────┘
    │
    ▼
  返回 [{key, content, position, depth, uid, similarity, rerank_score?}]
```

- **启动时**：批量嵌入 `worldbook.json` 所有条目，构建 FAISS 索引
- **运行时**：异步嵌入用户消息，执行两阶段检索
- **降级安全**：FAISS 不可用 / API 密钥未配置 / worldbook.json 不存在 → 返回 `None`，禁用语义检索

### 12.2 紧急告警 (AlertManager)

| 特性 | 说明 |
|------|------|
| **冷却期** | 3600 秒（1 小时） |
| **目标** | 所有配置的 `superusers` |
| **通道** | `bot.send_private_msg()` |
| **触发场景** | API 402/403、Reranker 故障、后台任务崩溃 |
| **重置** | `reset_cooldown()` — LLM 调用成功后重置计时器 |

### 12.3 关键词规范化 (keyword_utils)

- `normalize_keywords(keywords)`: 去重 → 去空格 → 小写 → 排序
- `compute_keywords_hash(keywords)`: 规范化 → `_` 拼接 → MD5 十六进制摘要

### 12.4 PDF 工具 (pdf_utils)

- `convert_zip_to_pdf()`: ZIP → 提取图片 → Pillow 缩放 → JPEG 压缩 → img2pdf 合并
- `modify_pdf_metadata()`: 用随机 UUID 替换 PDF 元数据（Title, Author, Producer 等）
- `natural_sort_key()`: 按数字边界分割实现自然排序（"page2" 排在 "page10" 前面）

---

*文档生成时间: 2026-05-06 · 基于 commit ec21094 的代码*
