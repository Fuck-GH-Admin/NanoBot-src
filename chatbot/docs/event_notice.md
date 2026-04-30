# EventNotice

**文件路径：** `plugins/chatbot/matchers/event_notice.py`

**模块职责：** 群事件通知处理器。处理"戳一戳"互动（含多轮连锁反应）和群成员进出欢迎/欢送。

---

## 事件处理器

### `poke` — 戳一戳处理器

**类型：** `on_notice(priority=5, block=True)`

**行为说明：** 仅响应别人戳 Bot 自身的事件。维护每个用户的连续戳状态，支持多轮连锁反应：

| 上次回复 | 当前触发条件 | 行为 |
|----------|-------------|------|
| `"戳我也没用，我不会给你发图的！"` | 再戳一次 | 发送一张随机图片 |
| `"你再戳我就把你禁言一分钟哦～"` | 连续戳 **≥3 次** | 禁言用户 60 秒 |
| `"好痒！停下！我要叫主人了！"` | 再戳一次 | @主人 求救 |
| 其他 | 任意 | 从 `POKE_REPLIES` 随机回复一句 |

**冷却机制：** 两次戳之间超过 30 秒则重置计数。

---

### `welcome` — 进出群处理器

**类型：** `on_notice(priority=5, block=False)`

**行为说明：** 根据 `welcome_mode` 配置控制行为：

- `"hello"` — 仅处理进群欢迎
- `"bye"` — 仅处理退群欢送
- `"all"` — 两者都处理

**配置控制：** 通过 `welcome_groups` 白名单控制生效的群聊。

---

## 依赖的跨模块接口

| 导入 | 来源 | 用途 |
|------|------|------|
| `img_srv.get_image("", allow_r18=False)` | `services/__init__` 全局实例 | 戳一戳连锁反应中发图 |
| `llm_srv.simple_chat(...)` | `services/__init__` 全局实例 | 生成 AI 欢迎语/欢送语 |
| `POKE_REPLIES` | `consts.py` | 戳一戳随机回复语料池 |
| `plugin_config.welcome_groups` | `config.py` | 欢迎功能白名单群号集合 |
| `plugin_config.welcome_mode` | `config.py` | 欢迎模式开关 |

---

## 调用示例

```python
# 模拟用户连续戳 Bot 3 次触发禁言
# 第1次: 随机回复（假设回复了"你再戳我就把你禁言一分钟哦～"）
# 第2次: 计数 +1
# 第3次: 计数 ≥3 → 触发 set_group_ban(60s)

# 模拟新人进群
# 自动检测 GroupIncreaseNoticeEvent
# → 调用 llm_srv.simple_chat 生成欢迎语
# → @新人 + "欢迎语"
```
