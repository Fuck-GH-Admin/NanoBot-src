# 架构蓝图说明书

> Chatbot B 系统内部架构的权威技术文档。

---

## 1. 系统拓扑

### 1.1 双进程模型

系统由两个独立进程组成，通过 HTTP JSON-RPC 通信：

| 进程 | 技术栈 | 端口 | 职责 |
|------|--------|------|------|
| **Python 主进程** | NoneBot2 + SQLAlchemy + asyncio | — | 事件监听、业务逻辑、数据持久化 |
| **Node.js 引擎** | Express + Zod + axios | 3010 | Prompt 编译、世界书扫描、LLM 调用 |
| **Web 管理面板** | http.server (stdlib) | 8081 | 配置读写（零信任鉴权） |

### 1.2 通信协议

```
Python ──HTTP POST──→ Node.js
         /api/chat
         {
           chatHistory: [...],
           memorySnapshot: { summary, profiles, entities, relations },
           tools: [...],
           context: { group_id, active_uids, semantic_hits, ... }
         }
         ←── { choices: [{ message: { content, tool_calls } }] }
```

- 请求体经 Python `ChatRequestPayload` (Pydantic) 校验后发送
- Node.js 端经 `ChatRequestSchema` (Zod) 二次校验
- 任何一端校验失败即返回 `400 Bad Request`

---

## 2. 数据持久层

### 2.1 ER 图

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
┌──────────────┐                          │ updated_at   │
│   Relation   │                          └──────────────┘
├──────────────┤
│relation_id   │
│ session_id   │
│subject_entity│──→ Entity.entity_id
│ predicate    │
│object_entity │──→ Entity.entity_id
│ confidence   │
│evidence_msgs │
│ updated_at   │
└──────────────┘
```

### 2.2 表设计理念

| 表 | 设计理念 |
|---|---|
| **ChatHistory** | 每条消息独立一行，支持增量总结（`is_summarized` 游标）和溯源（`id` 自增） |
| **GroupMemory** | 每个 session 一行的宏观摘要，upsert 更新，供 LLM 作为背景上下文 |
| **UserTrait** | 每条特征独立一行，支持置信度、溯源（`source_msg_id`）和逻辑删除（`is_active`） |
| **CompactionJournal** | 压缩任务状态机（pending→running→success/dead），支持僵尸恢复和死信追踪 |
| **Entity** | 知识图谱节点，按 `entity_id` upsert，`attributes` 为自由 JSON |
| **Relation** | 知识图谱三元组（S-P-O），唯一约束防重，`evidence_msg_ids` 合并去重 |

### 2.3 时间衰减机制

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

## 3. 数据流转机制

### 3.1 单次请求生命周期

```
用户 @Bot 发送消息
        │
        ▼
┌─ 1. 事件入口 (chat_entry.py) ─────────────────────────────┐
│  · 权限检查 (is_admin / allow_r18)                         │
│  · 构建 context 字典 (bot, services, user_id, group_id)    │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 2. AgentService.run_agent() ─────────────────────────────┐
│  · 第一时间落库：add_message(role="user")                  │
│  · 精准备料：                                              │
│    - get_recent_messages(limit=30) → chatHistory           │
│    - get_active_profiles() → profiles                      │
│    - get_group_summary() → summary                         │
│    - get_relations_with_decay() → relations                │
│    - semantic_lorebook.search() → semantic_hits (可选)     │
│  · 构建 ChatRequestPayload (Pydantic 校验)                │
└────────────────────────────────────────────────────────────┘
        │
        ▼ HTTP POST /api/chat
┌─ 3. Node.js 服务 (server.js) ─────────────────────────────┐
│  · Zod Schema 校验 (ChatRequestSchema)                    │
│  · 创建 DeepSeekTavernClient 实例                          │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 4. TavernCoreV2.buildOpenAIMessages() ───────────────────┐
│  · 世界书扫描 (scanLorebook)                               │
│    - 语义命中预处理 (semanticHitMap)                        │
│    - 关键词匹配 + 递归激活                                  │
│    - 位置分类 (before/after/atDepth)                       │
│  · 构建系统区块 (带优先级元数据)：                           │
│    - role_play_setting  (priority=2, neverCut)             │
│    - world_knowledge    (priority=6, 可裁剪)                │
│    - group_memory       (priority=5, 可裁剪)                │
│    - group_dynamics     (priority=4, 可裁剪)                │
│    - system_directives  (priority=1, neverCut)             │
│  · 深度锚点注入 (wiDepth)                                  │
│  · Token 预算仲裁 (applyTokenBudget)                       │
│    - Phase 1: 按优先级裁剪区块 items                        │
│    - Phase 2: 从旧到新裁剪历史 (保留≥2条)                   │
│    - Phase 3: 极端溢出兜底                                  │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 5. DeepSeek API 调用 ────────────────────────────────────┐
│  · 含 tools 定义 → 可能返回 tool_calls                     │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 6. ReAct 循环 (agent_service.py) ────────────────────────┐
│  · 无 tool_calls → 直接返回文本                             │
│  · 有 tool_calls → 逐个执行工具，追加 tool 消息，继续循环    │
│  · 终止条件：                                              │
│    - 无工具调用（自然结束）                                  │
│    - mark_task_complete（显式完成信号）                      │
│    - Jaccard 重复检测（similarity > 0.9）                   │
│    - max_loops 兜底（默认 10）                              │
│  · 每轮落库：add_message(role="assistant" / "tool")        │
└────────────────────────────────────────────────────────────┘
        │
        ▼
┌─ 7. 后台异步记忆压缩 ─────────────────────────────────────┐
│  · asyncio.create_task(memory_service.process_session_memory)│
│  · 轻量模式 (enable_task_queue=False):                     │
│    - 直接 create_task → _do_process，无持久化               │
│  · 高可用模式 (enable_task_queue=True):                     │
│    - 入队 → CompactionJournal 持久化                        │
│    - 消费者协程逐个出队                                      │
│    - 指数退避重试 (最多3次)                                  │
│    - 超限进入死信队列                                        │
│  · _do_process 核心逻辑：                                   │
│    - 未总结消息 ≥ 15 条时触发                                │
│    - 调用 DeepSeek (Function Calling) 提取：                │
│      group_summary, user_traits, entities, relations        │
│    - 批量 upsert 到 GroupMemory / UserTrait / Entity / Relation│
│    - 标记已总结 (is_summarized=True)                        │
└────────────────────────────────────────────────────────────┘
```

### 3.2 工具调用流程

```
LLM 返回 tool_calls
        │
        ▼
┌─ ToolRegistry.execute_tool() ─────────────────────────────┐
│  · 查找注册工具                                             │
│  · 权限二次验证：                                           │
│    - system 工具 → 跳过检查                                 │
│    - admin 工具 → 检查 is_admin                             │
│    - drawing_whitelist → 检查白名单                         │
│  · 执行 tool.execute(arguments, context)                   │
│  · 返回 (result_text, images[])                            │
└────────────────────────────────────────────────────────────┘
```

---

## 4. Feature Flag 机制

所有 Feature Flag 在 `config.py` 的 `Config` 类中定义，默认关闭。

| Flag | 作用域 | 控制内容 |
|------|--------|----------|
| `enable_strict_schema` | Python | Pydantic `ChatRequestPayload` 校验（当前始终开启） |
| `enable_task_queue` | Python | 记忆压缩走队列（持久化+重试）还是直接 create_task |
| `enable_dynamic_loop` | Python | Jaccard 重复检测 + `mark_task_complete` 工具注册 |
| `entity_relation_enabled` | Python | 是否查询衰减关系并注入 memorySnapshot |
| `semantic_lorebook_enabled` | Python | 是否启用 FAISS 语义检索 |
| `token_arbitration_enabled` | Node.js | 是否启用优先级裁剪（通过 context 透传） |

---

## 5. 性能与可靠性

### 5.1 事件循环保护

Pillow 等 CPU/IO 密集型同步操作通过 `loop.run_in_executor()` 卸载到线程池，避免阻塞 NoneBot 主事件循环。当前应用于 `ImageService.generate_stealth`。

### 5.2 文件原子写入

`AsyncFileUtils.write_json` 采用临时文件 + `os.replace` 策略实现原子写入。写入中途进程崩溃不会损坏原始文件，`os.replace` 在 OS 层面保证替换的原子性。

### 5.3 配置热重载

所有服务（`PermissionService`、`DrawingService`、`AgentService` 等）不在 `__init__` 中硬绑定配置数据。鉴权名单、API 密钥、超时时间等均在每次方法执行时实时读取 `plugin_config`，配合 YAML 文件的 watchdog 热监听，实现配置变更即时生效。

---

## 6. 安全架构

### 6.1 管理面板零信任

- 每次 Python 重启生成新 Token（`secrets.token_urlsafe(32)`）
- HTML 页面通过 `<meta>` 标签注入 Token，前端读取后立即 `.remove()`
- 所有 `/api/config` 请求需携带 `Authorization: Bearer <token>`
- 使用 `secrets.compare_digest` 进行常量时间比较（防时序攻击）

### 6.2 提示词注入防护

- 所有动态变量（用户名、群聊内容、画像）经 `escapeXml()` 转义
- XML 标签块自动包裹，防止结构破坏
- 世界书条目内容原样注入（受信任来源），但经宏替换引擎处理

### 6.3 工具权限模型

```
权限等级：
  user              → 所有用户可用 (search_acg_image, recommend_book)
  drawing_whitelist → 画图白名单用户 (generate_image)
  admin             → 群管理/超管 (ban_user)
  system            → 系统内部 (mark_task_complete，不暴露给用户)
```
