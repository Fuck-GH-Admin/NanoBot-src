# BookService

**文件路径：** `plugins/chatbot/services/book_service.py`

**模块职责：** 书籍业务服务。调度 JM 下载、协调 PDF 转换/加密/发送流程，处理业务彩蛋。

---

## 核心类与接口

### `BookService`

| 方法 | 说明 |
|------|------|
| `handle_jm_download` | 普通下载指令入口 |
| `handle_bitter_lovebirds` | 苦命鸳鸯彩蛋入口 |

---

### `async def handle_jm_download(bot: Bot, target_id: int, message_type: str, ids: List[str]) -> str`

**参数说明：**
- `bot` — NoneBot Bot 实例
- `target_id` — 群号或用户 QQ 号
- `message_type` — 消息类型，`"group"` 或 `"private"`
- `ids` — 本子 ID 列表

**返回值：**
- 成功：`"✅ 任务结束。发送 N/M 本。"`
- 环境不完整：`"❌ 环境配置不完整..."`

**调用示例：**
```python
result = await book_service.handle_jm_download(bot, 12345678, "group", ["350234", "350235"])
```

---

### `async def handle_bitter_lovebirds(bot: Bot, group_id: int) -> str`

**参数说明：**
- `bot` — NoneBot Bot 实例
- `group_id` — 群号

**返回值：**
- 成功：`"…这何尝不是一种苦命鸳鸯"`
- 失败：`"❌ 苦命鸳鸯彻底走散了..."`

**调用示例：**
```python
msg = await book_service.handle_bitter_lovebirds(bot, 12345678)
```
