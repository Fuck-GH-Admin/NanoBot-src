# Tavern Engine API — Node.js Chat Endpoint

**Base URL:** `http://127.0.0.1:3010`

**Server:** Express 4.x, stateless per-request architecture

---

## POST `/api/chat`

Compiles a prompt from character card + world book + context, calls DeepSeek API, returns raw OpenAI-compatible response.

### Request Body

```json
{
  "chatHistory": [
    {"role": "user", "content": "帮我画一只猫", "name": "Alice", "user_id": "111"},
    {"role": "assistant", "content": "好的", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "call_abc", "name": "generate_image", "content": "绘图成功"}
  ],
  "existingSummary": "群组最近在讨论绘画。",
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "generate_image",
        "description": "生成 AI 图片",
        "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}
      }
    }
  ],
  "userProfiles": {
    "111": "likes cats; programmer",
    "222": "friendly; artist"
  },
  "context": {
    "group_id": 12345678,
    "active_uids": ["111", "222"]
  }
}
```

### Field Reference

| Field | Type | Required | Description |
|---|---|---|---|
| `chatHistory` | `Message[]` | Yes | Recent messages for prompt compilation |
| `existingSummary` | `string` | No | Group macro summary (injected as `<group_memory>`) |
| `tools` | `OpenAITool[]` | No | Function calling definitions |
| `userProfiles` | `Record<string, string>` | No | `{user_id: "trait1; trait2"}` for active users |
| `context` | `object` | No | `{group_id, active_uids}` for lorebook context-aware filtering |

### Message Object

| Field | Type | Description |
|---|---|---|
| `role` | `string` | `user` / `assistant` / `system` / `tool` |
| `content` | `string` | Message text |
| `name` | `string` | Display name (for multi-user group chat) |
| `user_id` | `string` | QQ number (for profile lookup) |
| `tool_calls` | `array` | OpenAI tool_calls (role=assistant only) |
| `tool_call_id` | `string` | Tool call ID (role=tool only) |

### Response Body

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "好的，已经画好了！",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ]
}
```

> The response is a **raw OpenAI-compatible** `choices` array. State management (history, summary, profiles) is entirely on the Python side.

### Tool Call Flow

When `finish_reason` is `"tool_calls"`:
1. Parse `message.tool_calls[].function.arguments` (JSON string)
2. Execute the tool locally
3. Append result as `{role: "tool", tool_call_id, name, content}` to `chatHistory`
4. Re-POST to `/api/chat`

Max loop: 5 iterations (controlled by Python `agent_service`).

---

## POST `/api/reload`

Hot-reloads `character.json` and `worldbook.json` from `engine/data/` directory.

```json
{"status": "success", "message": "Templates reloaded"}
```

---

## Prompt Compilation Details

The Node.js engine assembles the system prompt as XML blocks:

```
<role_play_setting>     ← character card (identity, personality, scenario)
<world_knowledge>       ← lorebook scan results (wiBefore, wiAfter)
<group_memory>          ← summary + user profiles (XML-escaped for injection defense)
<system_directives>     ← jailbreak / post_history_instructions
```

User-sourced content is passed through `escapeXml()` before insertion.
