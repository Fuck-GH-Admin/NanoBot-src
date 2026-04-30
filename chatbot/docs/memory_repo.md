# MemoryRepository

**文件路径：** `plugins/chatbot/repositories/memory_repo.py`

**模块职责：** 用户记忆仓库。负责 `user_{id}.json` 的读写，包含细粒度的用户级并发锁。

---

## 核心类与接口

### `MemoryRepository`（单例）

| 方法 | 说明 |
|------|------|
| `load_memory` | 加载用户记忆 |
| `save_memory` | 保存用户记忆 |
| `clear_history` | 仅清空历史记录，保留画像 |

---

### `async def load_memory(user_id: str) -> Dict[str, Any]`

**参数说明：**
- `user_id` — 用户 QQ 号

**返回值：**
```python
{
    "history": [{"role": "user", "content": "..."}, ...],  # 聊天记录列表
    "profile": {}                                           # 用户画像字典
}
```

**调用示例：**
```python
repo = MemoryRepository()
mem = await repo.load_memory("123456")
print(mem["history"][-1]["content"])
```

---

### `async def save_memory(user_id: str, history: List[dict], profile: dict) -> bool`

**参数说明：**
- `user_id` — 用户 QQ 号
- `history` — 聊天记录列表
- `profile` — 用户画像字典

**返回值：**
- 成功：`True`
- 失败：`False`

**调用示例：**
```python
success = await repo.save_memory("123456", history_list, profile_dict)
```

---

### `async def clear_history(user_id: str) -> bool`

**参数说明：** `user_id` — 用户 QQ 号

**返回值：** 成功 `True` / 失败 `False`

**调用示例：**
```python
await repo.clear_history("123456")
```
