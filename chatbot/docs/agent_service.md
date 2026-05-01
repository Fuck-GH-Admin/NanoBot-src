# AgentService

**File:** `plugins/chatbot/services/agent_service.py`

**Responsibility:** ReAct loop orchestrator. Manages the conversation cycle between Python (state, tools, persistence) and Node.js (prompt compilation, LLM invocation).

---

## Class: `AgentService`

| Method | Description |
|---|---|
| `run_agent(user_id, text, context)` | Main entry: prepare context → call Node.js → execute tools → return result |
| `close()` | Cleanup HTTP connection pool on shutdown |

### `__init__`

Creates instance-level resources:
- `self.repo` — `MemoryRepository` singleton
- `self.memory_service` — `MemoryService` for background compression
- `self.http_client` — `httpx.AsyncClient` (persistent connection pool, timeout from config)
- `self.registry` — `ToolRegistry` with all tools pre-registered

---

### `async def run_agent(user_id, text, context) -> Dict[str, Any]`

**Parameters:**
- `user_id` — QQ number (string)
- `text` — User message text
- `context` — Must contain:
  - `permission_service` — PermissionService instance
  - `is_admin: bool`
  - `group_id: int` (0 for private chat)
  - `allow_r18: bool`
  - `bot` — NoneBot Bot instance
  - `sender_name: str`
  - `drawing_service`, `image_service`, `book_service` (optional)

**Returns:**
```python
{"text": str, "images": [str]}
```

**Flow:**
1. Build `session_id` from `group_id` / `user_id`
2. Insert user message into `chat_history` (single `add_message` call)
3. Fetch recent messages (limit 30), active profiles, group summary
4. Build `lorebook_context = {group_id, active_uids}`
5. ReAct loop (max 5 iterations):
   - POST to Node.js `/api/chat`
   - If `tool_calls` returned: execute tools, append results, continue loop
   - If no tool_calls: fire-and-forget `memory_service.process_session_memory()`, return response
6. If loop exhausted: return last assistant message

---

## Registered Tools

| Tool Class | Permission | Description |
|---|---|---|
| `GenerateImageTool` | `drawing_whitelist` | AI image generation |
| `SearchAcgImageTool` | `user` | ACG image search |
| `BanUserTool` | `admin` | Group mute |
| `RecommendBookTool` | `user` | Book recommendations |
| `JmDownloadTool` | `drawing_whitelist` | JM comic download |

---

## Example

```python
result = await agent.run_agent(
    user_id="123456",
    text="给我画一只猫",
    context={
        "permission_service": perm_srv,
        "is_admin": True,
        "group_id": 12345678,
        "allow_r18": False,
        "bot": bot_instance,
        "sender_name": "Alice",
    }
)
print(result["text"])
```
