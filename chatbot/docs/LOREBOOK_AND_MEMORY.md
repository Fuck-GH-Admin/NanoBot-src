# 记忆与设定系统 — Chatbot B V4

> 详细说明世界书、Topic Harvester、去重融合等核心机制。
> 最后更新：2026-05-14

---

## 目录

1. [静态世界书 (WorldBook)](#1-静态世界书-worldbook)
2. [SillyTavern Lorebook 引擎](#2-sillytavern-lorebook-引擎)
3. [语义向量检索 (Semantic Lorebook)](#3-语义向量检索-semantic-lorebook)
4. [Topic Harvester：自动设定收割](#4-topic-harvester自动设定收割)
5. [世界书去重融合 (dedup_worldbook.py)](#5-世界书去重融合-dedup_worldbookpy)
6. [数据库 ER 结构](#6-数据库-er-结构)

---

## 1. 静态世界书 (WorldBook)

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

### 词条结构

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
| `keysecondary` | 副关键词，配合 `selectiveLogic` 使用 |
| `constant` | `true` 时无条件注入（忽略 key） |
| `custom_scope` | `"global"` 对所有群生效；指定 group_id 则仅限该群 |
| `selectiveLogic` | 副关键词逻辑门：0=AND_ANY, 1=NOT_ALL, 2=NOT_ANY, 3=AND_ALL |
| `position` | 注入位置分类 |
| `order` | 排序优先级 |
| `disable` | `true` 时跳过该条目 |

### 热重载

`WorldBook.search()` 每次调用时检查文件 `mtime`，变动则重新加载整个 JSON，无需重启进程。

---

## 2. SillyTavern Lorebook 引擎

`LorebookEngine`（`engine/lorebook_engine.py`）实现兼容 SillyTavern 的关键词扫描协议：

### 扫描特性

| 特性 | 说明 |
|------|------|
| **负向关键词** | 前缀 `-` 实现否决（如 `-cat` 排除含 cat 的条目） |
| **主关键词** | ANY 逻辑（任一命中即激活） |
| **副关键词逻辑门** | `AND_ANY`、`NOT_ALL`、`NOT_ANY`、`AND_ALL` |
| **递归级联** | 已激活条目的 content 回馈扫描缓冲区，最深 `max_depth=10` |
| **位置分类** | 按 `(order ASC, depth ASC, content ASC)` 排序，分入 `wi_before` / `wi_after` / `wi_depth` |

### 副关键词逻辑门

| selectiveLogic | 名称 | 行为 |
|:--------------:|------|------|
| 0 | AND_ANY | 副关键词任一命中即激活 |
| 1 | NOT_ALL | 副关键词全部命中则否决 |
| 2 | NOT_ANY | 副关键词任一命中则否决 |
| 3 | AND_ALL | 副关键词全部命中才激活 |

---

## 3. 语义向量检索 (Semantic Lorebook)

在关键词匹配之上，系统支持基于向量相似度的语义检索（`utils/embedding.py`）。

### 两阶段检索

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
│  · 返回 Top-3                    │
└──────────────────────────────────┘
```

### 降级策略

以下任一条件满足时，语义检索静默跳过，不影响主链路：

- FAISS 未安装
- `siliconflow_api_key` 未配置
- `semantic_lorebook_enabled = False`
- `worldbook.json` 不存在
- Embedding API 超时（2s）或异常

---

## 4. Topic Harvester：自动设定收割

### 4.1 定位

Topic Harvester 是系统**唯一的长期记忆沉淀中枢**。旧的消息压缩机制（`process_session_memory` / `SUMMARY_THRESHOLD`）已移除。

### 4.2 触发时机

话题状态从 `SUSPENDED` 转为 `ARCHIVED` 时触发（默认 30 分钟无活动）。

### 4.3 收割流程

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

### 4.4 提炼策略

LLM 的 System Prompt 指定了提取规则：

| 提取 | 不提取 |
|------|--------|
| 世界观：地名、组织、势力、规则体系 | 打招呼、表情包 |
| 人物设定：角色名、身份、能力、外貌 | 一次性闲聊 |
| 专有名词：术语、道具、技能名 | 无意义回复 |
| 关系网络：师徒、敌对、队友等固定关系 | |
| 重要事件：有长期影响的事件 | |

原则：**宁可多提，不要漏提**。让管理员在 Web 端审核时丢弃，而非遗漏重要设定。

### 4.5 Read-Before-Write 防重机制

在调用 LLM 提炼设定之前，系统会先从 `worldbook.json` + `draft_worldbook.json` 中收集当前作用域（含 global）的**所有已知关键词**，注入 LLM 的 System Prompt：

```python
def _collect_known_keys(session_id: str) -> set[str]:
    """从 worldbook.json + draft_worldbook.json 中收集已知关键词。"""
    keys: set[str] = set()
    for fname in ("worldbook.json", "draft_worldbook.json"):
        # 读取 global 作用域 + 当前 session 作用域的所有 key
        ...
    return keys
```

LLM 被指示避免生成与已有关键词高度重复的条目，从源头减少冗余。

### 4.6 草稿审批闭环

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

审批通过 Web 面板的 `/api/worldbook/draft/approve` 和 `/api/worldbook/draft/reject` 端点完成。

### 4.7 守护进程

`topic_harvester_daemon()` 以 60 秒间隔轮询，扫描所有 session 中状态为 `SUSPENDED` 且超时的话题。使用 `run_with_self_heal()` 包装，异常时自动告警并 5 秒后重启。

---

## 5. 世界书去重融合 (dedup_worldbook.py)

### 5.1 概述

`dedup_worldbook.py` 是一个独立脚本，用于清理 `worldbook.json` 中的冗余词条。

```bash
# 分析模式（不写入）
python -m chatbot.scripts.dedup_worldbook --dry-run

# 执行模式
python -m chatbot.scripts.dedup_worldbook
```

### 5.2 两阶段去重流程

```
worldbook.json
       │
       ▼
┌──────────────────────────────────────────────────────┐
│  Phase 1: 严格物理去重 (_exact_dedup)                 │
│                                                      │
│  · 遍历所有条目                                      │
│  · content 完全一致（strip 后）→ 保留最后出现的一条   │
│  · O(N) 时间复杂度                                   │
│  · 输出: 去重后的条目列表                            │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  Phase 2: 保守聚类 + LLM 融合                        │
│                                                      │
│  2a. 按 custom_scope 分组                            │
│      · 每个作用域独立聚类，不跨域合并                │
│                                                      │
│  2b. 并查集聚类 (_cluster_entries)                   │
│      · O(N²) 两两比较（N < 500，完全可接受）        │
│      · 合并条件（满足任一）：                        │
│        ① key 集合完全一致                           │
│        ② Jaccard 相似度 ≥ 0.5                       │
│        ③ 其中一个是另一个的子集（且两者都非空）      │
│      · 仅返回 2+ 条目的簇                           │
│                                                      │
│  2c. LLM 智能融合 (_llm_merge)                      │
│      · 对每个 2+ 条目的簇调用 LLM                    │
│      · System Prompt 指示融合为单条 SillyTavern 条目 │
│      · response_format=json_object 确保结构化输出    │
│      · temperature=0.2（确定性融合）                 │
│      · 输出: {"key": [...], "content": "..."}        │
│      · 保留第一个条目的 UID 和扩展字段               │
│      · 标记 comment="Auto-Merged"                    │
│      · 簇间 sleep(1.0) 防 rate limit                 │
└──────────────────────┬───────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  输出: worldbook_clean.json                          │
│  · 重新编号 UID（从 1 开始）                         │
│  · 同时创建带时间戳的备份文件                        │
│  · 打印执行报告（原始/去重/融合/最终条目数）         │
└──────────────────────────────────────────────────────┘
```

### 5.3 Jaccard 相似度聚类细节

```python
def _should_merge(set_a: set[str], set_b: set[str]) -> bool:
    """判断两个 key 集合是否应合并。"""
    if not set_a or not set_b:
        return False
    if set_a == set_b:           # 条件 1：完全一致
        return True
    if set_a <= set_b or set_b <= set_a:  # 条件 3：子集关系
        return True
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union > 0 and intersection / union >= 0.5:  # 条件 2：Jaccard ≥ 0.5
        return True
    return False
```

注意：子集关系检查在 Jaccard 之前，因为子集可能 Jaccard < 0.5（如 `{"A"}` 与 `{"A", "B", "C"}` 的 Jaccard = 0.25，但明显应合并）。

### 5.4 SillyTavern 兼容性保证

LLM 融合输出的条目自动补全所有 SillyTavern 扩展字段：

```python
merged.setdefault("keysecondary", [])
merged.setdefault("selectiveLogic", 0)
merged.setdefault("position", 0)
merged.setdefault("depth", 4)
merged.setdefault("order", 100)
merged.setdefault("disable", False)
merged.setdefault("selective", False)
merged.setdefault("excludeRecursion", False)
merged.setdefault("preventRecursion", False)
merged.setdefault("group", "")
merged.setdefault("groupOverride", False)
merged.setdefault("groupWeight", 100)
merged.setdefault("probability", 100)
merged.setdefault("useProbability", True)
merged.setdefault("outletName", "")
merged.setdefault("role", 0)
```

---

## 6. 数据库 ER 结构

系统使用 SQLAlchemy 2.0 异步声明式模型，共 10 张核心表：

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   ChatHistory    │     │   GroupMemory    │     │    UserTrait     │
├─────────────────┤     ├─────────────────┤     ├─────────────────┤
│ id (PK, auto)   │     │ session_id (PK) │     │ trait_id (PK)   │
│ session_id (idx) │     │ summary         │     │ session_id (idx) │
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

### 关键约束

| 表 | 约束 | 说明 |
|-----|------|------|
| `ChatHistory` | `UNIQUE(session_id, message_fingerprint)` | 幂等写入，`ON CONFLICT DO NOTHING` |
| `UserTrait` | `UNIQUE(session_id, user_id, content)` | 同一用户同一特征只保留一行 |
| `Relation` | `UNIQUE(session_id, subject_entity, predicate, object_entity)` | 三元组唯一 |
| `CustomRule` | `UNIQUE(scope_type, scope_id, keywords_hash)` | 规则去重 |

---

*本文档基于源码全局审阅生成，反映 2026-05-14 的系统状态。*
