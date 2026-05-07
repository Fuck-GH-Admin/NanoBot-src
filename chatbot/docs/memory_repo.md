# MemoryRepository

**文件路径：** `repositories/memory_repo.py`

**模块职责：** 异步结构化记忆存储。基于 SQLAlchemy 2.0 + aiosqlite 的单例仓储，管理 9 张表的 CRUD 操作。

---

## 初始化

```python
repo = MemoryRepository()       # 单例
await repo.init_db()            # 建表（启动时调用一次）
```

默认数据库路径：`data/chatbot_memory.db`（自动创建）。

---

## ChatHistory 操作

### `async def add_message(session_id, role, content, user_id=None, name=None, timestamp=None, tool_calls=None) -> int`

插入单条消息，返回自增 `id`。

```python
msg_id = await repo.add_message(
    session_id="group_12345678",
    role="user",
    content="你好",
    user_id="987654321",
    name="Alice",
)
```

### `async def get_recent_messages(session_id, limit=50) -> List[Dict]`

返回最近 N 条消息，按 `id ASC` 排列（旧→新）。

### `async def get_unsummarized_messages(session_id) -> List[Dict]`

返回所有 `is_summarized = False` 的消息。供记忆压缩服务使用。

### `async def mark_messages_summarized(message_ids: List[int]) -> int`

批量更新 `is_summarized = True`，返回影响行数。

---

## GroupMemory 操作

### `async def upsert_group_summary(session_id, summary) -> None`

插入或更新群组宏观摘要。以 `session_id` 为主键。

### `async def get_group_summary(session_id) -> str`

返回摘要文本，不存在时返回 `""`。

---

## UserTrait 操作

### `async def upsert_user_traits(session_id, user_id, traits_list) -> int`

原子化 upsert（`INSERT ... ON CONFLICT DO UPDATE`）。

**输入格式：**
```python
traits_list = [
    {"content": "喜欢猫", "confidence": 0.9},
    {"content": "程序员", "confidence": 0.7, "source_msg_id": 42},
]
```

冲突时（相同 `session_id + user_id + content`）：更新 `confidence` 为较高值，刷新 `updated_at`。

### `async def get_active_profiles(session_id, user_ids) -> Dict[str, List[Dict]]`

返回活跃用户的结构化画像：

```python
{
    "123456": [
        {"content": "喜欢猫", "confidence": 0.9, "updated_at": "2026-05-01T..."},
        {"content": "友善", "confidence": 0.6, "updated_at": "2026-05-01T..."},
    ]
}
```

### `async def deactivate_user_traits(session_id, user_id, trait_ids=None) -> int`

软删除：设置 `is_active = False`。`trait_ids` 为 `None` 时删除该用户在该 session 的所有特征。

---

## 知识图谱操作

### `async def upsert_entities(entities) -> int`

批量 upsert 实体节点。

### `async def get_active_entities(session_id) -> List[Dict]`

返回指定 session 的所有活跃实体。

### `async def upsert_relations(relations) -> int`

批量 upsert 关系三元组。冲突时合并 `evidence_msg_ids`。

### `async def get_relations_with_decay(session_id, entity_ids, half_life_days=30.0) -> List[Dict]`

带时间衰减的关系查询。置信度按指数衰减，低于 0.15 的关系自动过滤。

```
effective_confidence = confidence × 0.5 ^ (age_days / half_life_days)
```

---

## 记忆快照聚合

### `async def get_memory_snapshot(session_id) -> Dict`

组装完整的记忆快照，包含：

```python
{
    "summary": "群组摘要文本",
    "profiles": [{"user_id": "123", "traits": [...]}],
    "entities": [{"entity_id": "...", "name": "...", "type": "..."}],
    "relations": [{"subject": "...", "predicate": "...", "object": "...", "confidence": 0.8}],
}
```

---

## 突变日志操作

### `async def insert_tool_log(session_id, request_id, step, trigger, tool_name, arguments=None, result_summary="", error=None) -> None`

记录一次工具执行到审计日志表。`result_summary` 和 `error` 自动截断至 2000 字符。

### `async def get_recent_tool_logs(session_id, limit=20) -> List[Dict]`

查询指定 session 最近的突变日志，按 `id` 降序排列。返回字典列表，包含 `id`、`session_id`、`tool_name`、`trigger`、`arguments`、`result_summary`、`error`、`created_at` 等字段。

---

## Session ID 约定

```
group_{group_id}       →  群聊会话
private_{user_id}      →  私聊会话
```

所有查询均按 `session_id` 作用域隔离，无跨 session 数据泄漏。
