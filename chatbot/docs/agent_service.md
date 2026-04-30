# AgentService

**文件路径：** `plugins/chatbot/services/agent_service.py`

**模块职责：** 瘦终端 Agent。只负责与 Node.js 大脑交互完成 ReAct 循环，所有意图分析和对话智慧由远端大模型提供。

---

## 核心类与接口

### `AgentService`

| 方法 | 说明 |
|------|------|
| `run_agent` | 主入口：加载历史 → 构建工具 Schema → 与 Node 对话 → 执行工具调用 → 返回结果 |

---

### `async def run_agent(user_id: str, text: str, context: Dict[str, Any]) -> Dict[str, Any]`

**参数说明：**
- `user_id` — 用户 QQ 号，用于加载历史记忆
- `text` — 用户输入的文本
- `context` — 上下文字典，必须包含：
  - `permission_service` — PermissionService 实例
  - `is_admin: bool` — 用户是否为管理员
  - `group_id: int` — 群号（私聊为 0）
  - `allow_r18: bool` — 是否允许 R18
  - `bot` — NoneBot Bot 实例
  - `drawing_service` (可选) — DrawingService 实例
  - `image_service` (可选) — ImageService 实例
  - `book_service` (可选) — BookService 实例

**返回值：**
```python
{
    "text": str,       # 最终回复文本
    "images": [str]    # 图片路径列表
}
```
- 失败：`{"text": "大脑短路了...", "images": []}`

**调用示例：**
```python
result = await agent_service.run_agent(
    user_id="123456",
    text="给我画一只猫",
    context={
        "permission_service": perm_srv,
        "is_admin": True,
        "group_id": 12345678,
        "allow_r18": False,
        "bot": bot_instance,
        "drawing_service": drawing_srv,
        "image_service": image_srv,
        "book_service": book_srv,
    }
)
print(result["text"])
for img in result["images"]:
    print(img)
```

---

### 已注册工具

| 工具类 | 说明 |
|--------|------|
| `GenerateImageTool` | AI 绘图工具 |
| `SearchAcgImageTool` | ACG 图片搜索工具 |
| `BanUserTool` | 禁言工具 |
| `RecommendBookTool` | 书籍推荐工具 |
| `JmDownloadTool` | JM 下载工具 |
