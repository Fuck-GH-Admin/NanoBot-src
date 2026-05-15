# EventNotice

**文件路径：** `matchers/event_notice.py`

**模块职责：** 群事件通知处理器。处理"戳一戳"互动（含多轮连锁反应 + LLM 自然回应）、群成员进出欢迎/欢送、好友请求自动审批。

---

## 事件处理器

### `poke` — 戳一戳处理器

**类型：** `on_notice(priority=5, block=True)`

**行为说明：** 仅响应别人戳 Bot 自身的事件。维护每个用户的连续戳状态（30 秒冷却重置）：

| 连续次数 | 行为 |
|:--------:|------|
| 第 1-2 次 | 通过 `agent_srv.run_agent()` 调用演员脑生成自然回应（`source_type="system"`） |
| 第 3 次 | 发送一张随机图片（`img_srv.get_image`） |
| 第 4 次 | 禁言用户 60 秒（`bot.set_group_ban`） |
| 第 5 次 | @主人求救 |

**LLM 自然回应**：普通戳一戳通过 `agent_srv.run_agent()` 生成，context 中 `source_type="system"` 触发系统级短路路径（仅演员脑渲染，无 DB 写入）。

**日志路由**：使用 `logger.contextualize(group_id=event.group_id or 0)` 绑定日志上下文。

---

### `welcome` — 进出群处理器

**类型：** `on_notice(priority=5, block=False)`

**行为说明：** 通过事件类型注解自动分发：

| 事件 | 条件 | 行为 |
|------|------|------|
| `GroupIncreaseNoticeEvent` | `welcome_mode` 包含 `"hello"` | 通过 `agent_srv.run_agent()` 生成欢迎语（30 字以内） |
| `GroupDecreaseNoticeEvent` | `welcome_mode` 包含 `"bye"` | 通过 `agent_srv.run_agent()` 生成告别语（25 字以内，提及离开者昵称） |

**配置控制：**
- `plugin_config.welcome_mode` — `"hello"` / `"bye"` / `"all"`
- `plugin_config.welcome_groups` — 白名单群号集合，为空则所有群生效

---

### `friend_request` — 好友请求处理器

**类型：** `on_request(priority=5, block=True)`

**行为说明：** 自动审批好友请求，并将用户添加到私聊白名单配置文件。

---

### `friend_add` — 好友添加通知处理器

**类型：** `on_notice(priority=5, block=False)`

**行为说明：** 将新好友添加到私聊白名单配置文件（幂等，已存在则跳过）。

---

## 依赖的跨模块接口

| 导入 | 来源 | 用途 |
|------|------|------|
| `img_srv.get_image("", allow_r18=False)` | `services/__init__` 全局实例 | 戳一戳第 3 次发图 |
| `agent_srv.run_agent(...)` | `services/__init__` 全局实例 | LLM 生成自然回应/欢迎语/告别语 |
| `perm_srv` | `services/__init__` 全局实例 | 权限服务引用（传入 context） |
| `plugin_config.welcome_groups` | `config.py` | 欢迎功能白名单群号集合 |
| `plugin_config.welcome_mode` | `config.py` | 欢迎模式开关 |
| `plugin_config.superusers` | `config.py` | 超管列表（戳一戳第 5 次求救） |

---

## 调用示例

```python
# 模拟用户连续戳 Bot 3 次触发发图
# 第1次: agent_srv.run_agent() 生成自然回应
# 第2次: agent_srv.run_agent() 生成自然回应
# 第3次: img_srv.get_image() → 发送随机图片

# 模拟新人进群
# agent_srv.run_agent("system_welcome", "用可爱温暖的语气欢迎...", ctx)
# → @新人 + 欢迎语
```
