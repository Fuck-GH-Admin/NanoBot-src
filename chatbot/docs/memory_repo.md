# MemoryRepository

**File:** `plugins/chatbot/repositories/memory_repo.py`

**Responsibility:** Async CRUD operations for all three memory tables. Singleton pattern with SQLAlchemy 2.0 + aiosqlite.

> **Deprecated:** All `user_{id}.json` file-based storage has been removed. This module now operates entirely on SQLite.

---

## Initialization

```python
repo = MemoryRepository()       # Singleton
await repo.init_db()            # Creates tables if not exist (call once at startup)
```

Default database: `data/chatbot_memory.db` (auto-created).

---

## ChatHistory Operations

### `async def add_message(session_id, role, content, user_id=None, name=None, timestamp=None, tool_calls=None) -> int`

Insert a single message row. Returns the auto-increment `id`.

```python
msg_id = await repo.add_message(
    session_id="group_12345678",
    role="user",
    content="hello",
    user_id="987654321",
    name="Alice",
)
```

### `async def get_recent_messages(session_id, limit=50) -> List[Dict]`

Returns the most recent N messages, ordered by `id ASC` (old → new).

### `async def get_unsummarized_messages(session_id) -> List[Dict]`

Returns all messages where `is_summarized = False`. Used by the memory compression agent.

### `async def mark_messages_summarized(message_ids: List[int]) -> int`

Bulk-update `is_summarized = True`. Returns row count.

---

## GroupMemory Operations

### `async def upsert_group_summary(session_id, summary) -> None`

Insert or update the group's macro summary. Uses `session_id` as primary key.

### `async def get_group_summary(session_id) -> str`

Returns the summary string, or `""` if none exists.

---

## UserTrait Operations

### `async def upsert_user_traits(session_id, user_id, traits_list) -> int`

Atomic upsert using `INSERT ... ON CONFLICT DO UPDATE`.

**Input format:**
```python
traits_list = [
    {"content": "likes cats", "confidence": 0.9},
    {"content": "programmer", "confidence": 0.7, "source_msg_id": 42},
]
```

On conflict (same `session_id + user_id + content`): updates `confidence` to the higher value and refreshes `updated_at`.

### `async def get_active_profiles(session_id, user_ids) -> Dict[str, List[Dict]]`

Returns structured profiles for active users:

```python
{
    "123456": [
        {"content": "likes cats", "confidence": 0.9, "updated_at": "2026-05-01T..."},
        {"content": "friendly", "confidence": 0.6, "updated_at": "2026-05-01T..."},
    ]
}
```

### `async def deactivate_user_traits(session_id, user_id, trait_ids=None) -> int`

Soft-delete: sets `is_active = False`. If `trait_ids` is None, deactivates all traits for that user in that session.

---

## Session ID Convention

```
group_{group_id}       →  group chat session
private_{user_id}      →  private chat session
```

All queries are scoped by `session_id`. No cross-session data leakage.
