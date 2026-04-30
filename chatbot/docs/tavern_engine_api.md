# Tavern Engine API — 微服务接口规范

**服务说明：** 基于 Node.js 的对话引擎微服务，通过 `/api/chat` 端点完成 ReAct 循环。接收带工具的对话历史，返回回复文本、新历史和工具调用指令。

---

## Endpoint

```
POST http://127.0.0.1:3000/api/chat
```

Content-Type: `application/json`

---

## Request Body Schema

```json
{
  "chatHistory": [
    {
      "role": "user",
      "content": "帮我画一只猫"
    },
    {
      "role": "assistant",
      "content": "我帮你画一只猫。让我调用绘图工具。",
      "tool_calls": [
        {
          "id": "call_abc123",
          "type": "function",
          "function": {
            "name": "generate_image",
            "arguments": "{\"prompt\": \"一只可爱的猫，水彩风格\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_abc123",
      "name": "generate_image",
      "content": "绘图成功，文件已保存到 /data/generated_images/cat.png"
    }
  ],
  "existingSummary": "用户喜欢动物和风景画。之前画过一只狗和一座山。",
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "generate_image",
        "description": "生成 AI 图片",
        "parameters": {
          "type": "object",
          "properties": {
            "prompt": {
              "type": "string",
              "description": "图片描述"
            }
          },
          "required": ["prompt"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "search_acg_image",
        "description": "搜索 ACG 图片",
        "parameters": {
          "type": "object",
          "properties": {
            "keyword": {
              "type": "string",
              "description": "搜索关键词"
            },
            "allow_r18": {
              "type": "boolean",
              "description": "是否允许 R18"
            }
          },
          "required": ["keyword"]
        }
      }
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `chatHistory` | `Array<Message>` | 是 | 对话历史消息列表，每条包含 role/content，可选 tool_calls |
| `existingSummary` | `string` | 否 | 已有对话摘要，用于压缩上下文 |
| `tools` | `Array<OpenAITool>` | 否 | OpenAI 格式的工具定义数组 |
| `user_id` | `string` | 否 | 用户 ID，用于个性化 |

### Message 对象

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | `string` | 是 | `"user"` / `"assistant"` / `"tool"` / `"system"` |
| `content` | `string` | 是 | 消息文本内容 |
| `tool_calls` | `Array<ToolCall>` | 否 | assistant 消息中的工具调用（role=assistant 时） |
| `tool_call_id` | `string` | 否 | 工具调用 ID（role=tool 时） |
| `name` | `string` | 否 | 工具名称（role=tool 时） |

---

## Response Body Schema

```json
{
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "好的，已经画好了！这是一只水彩风格的猫。",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "generate_image",
              "arguments": "{\"prompt\": \"一只可爱的猫，水彩风格\"}"
            }
          }
        ]
      },
      "finish_reason": "stop"
    }
  ],
  "newHistory": [
    { "role": "user", "content": "帮我画一只猫" },
    { "role": "assistant", "content": "好的，已经画好了！这是一只水彩风格的猫。" }
  ],
  "newSummary": "用户喜欢动物和风景画。之前画过一只狗、一座山和一只猫。"
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `choices` | `Array<Choice>` | 是 | 模型回复（OpenAI 兼容格式） |
| `newHistory` | `Array<Message>` | 否 | 更新后的对话历史（包含本轮），客户端应替换本地历史 |
| `newSummary` | `string` | 否 | 更新后的对话摘要，客户端应替换本地摘要 |

### Choice 对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `index` | `number` | 选项索引 |
| `message` | `Message` | 回复消息 |
| `finish_reason` | `string` | `"stop"` / `"tool_calls"` / `"length"` |

### tool_calls 说明

当 `finish_reason` 为 `"tool_calls"` 时，`message.tool_calls` 包含模型请求调用的工具列表。客户端应：
1. 解析 `function.arguments`（JSON 字符串）
2. 按 `function.name` 执行对应的本地工具
3. 将工具结果以 `role: "tool"` 追加到历史
4. 重新请求 `/api/chat` 继续循环

---

## 通信流程

```
Client (Python AgentService)          Server (Node.js Tavern Engine)
         |                                      |
         |--- POST /api/chat ------------------>|
         |    {chatHistory, tools, user_id}      |
         |                                      |
         |<-- {choices, newHistory, newSummary} -|
         |                                      |
         |    if tool_calls exists:              |
         |    执行工具 → 追加结果到 chatHistory   |
         |    goto POST /api/chat                |
         |                                      |
         |    else: 返回最终回复给用户            |
```

最大工具循环次数：**5 轮**（由客户端控制，防止无限循环）。
