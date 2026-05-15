# MemoryRepository

**文件路径：** `repositories/memory_repo.py`

**模块职责：** 异步结构化记忆存储。基于 SQLAlchemy 2.0 + aiosqlite 的单例仓储，管理聊天历史、话题线程、突变日志等核心表的 CRUD 操作。

---

## 初始化

```python
repo = MemoryRepository()       # 单例
await repo.init_db()            # 建表（启动时调用一次）
```

默认数据库路径：`data/pixiv.db`（自动创建）。

---

## ChatHistory 操作

### `async def add_message(session_id, role, content, topic_id=None, user_id=None, name=None, timestamp=None, message_fingerprint=None) -> int`

插入单条消息。`UNIQUE(session_id, message_fingerprint)` 约束保证幂等写入（`ON CONFLICT DO NOTHING`）。

```python
msg_id = await repo.add_message(
    session_id="group_12345678",
    role="user",
    content="你好",
    user_id="987654321",
    name="Alice",
    topic_id="abc123",
    message_fingerprint="42",
)
```

### `async def get_recent_messages(session_id, limit=50) -> List[Dict]`

返回最近 N 条消息，按 `id ASC` 排列（旧→新）。

### `async def get_messages_by_topic(topic_id) -> List[Dict]`

返回指定话题下的所有消息，屏蔽其他话题的噪音。

### `async def get_topic_id_by_message_id(platform_msg_id) -> Optional[str]`

根据平台消息 ID 反查对应的 `topic_id`。供话题路由 L1 物理强连通使用。

---

## TopicThread 操作

### `async def upsert_topic_thread(topic_id, session_id, status="ACTIVE", participants=None) -> None`

插入或更新话题线程记录。`participants` 列表自动合并去重。

```python
await repo.upsert_topic_thread(
    topic_id="abc123",
    session_id="group_12345678",
    participants=["user1", "user2"],
)
```

---

## 突变日志操作

### `async def insert_tool_log(session_id, request_id, step, trigger, tool_name, arguments=None, result_summary="", error=None) -> None`

记录一次工具执行到审计日志表。`result_summary` 和 `error` 自动截断至 2000 字符。仅 `is_write_operation=True` 的工具才写入。

### `async def get_recent_tool_logs(session_id, limit=20) -> List[Dict]`

查询指定 session 最近的突变日志，按 `id` 降序排列。

---

## 规则操作

规则的 CRUD 由 `RuleRepository`（`repositories/rule_repo.py`）独立管理，不在 `MemoryRepository` 中。

---

## Session ID 约定

```
group_{group_id}       →  群聊会话
private_{user_id}      →  私聊会话
```

所有查询均按 `session_id` 作用域隔离，无跨 session 数据泄漏。

---

## 已废弃方法

以下方法仍存在于源码中，但自记忆压缩机制移除后已**无调用方**。保留代码供未来参考或复用：

| 方法 | 说明 |
|------|------|
| `upsert_user_traits` | 用户特征 upsert（置信度只升不降） |
| `get_active_profiles` | 获取活跃用户画像 |
| `deactivate_user_traits` | 软删除用户特征 |
| `upsert_entities` | 实体节点 upsert |
| `upsert_relations` | 关系三元组 upsert |
| `get_relations_with_decay` | 带时间衰减的关系查询 |
| `get_memory_snapshot` | 组装完整记忆快照 |
| `get_unsummarized_messages` | 获取未总结消息 |
| `mark_messages_summarized` | 批量标记已总结 |
| `upsert_group_summary` | 群组摘要 upsert |
| `get_group_summary` | 获取群组摘要 |
