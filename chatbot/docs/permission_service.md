# PermissionService

**文件路径：** `services/permission_service.py`

**模块职责：** 权限与群管理核心服务。统一鉴权（超管 / AI 管理员 / 群管理）、执行禁言/踢人操作、基于 LLM 的智能违规审计。

> **热更新支持：** 本服务不在 `__init__` 中缓存配置数据。所有鉴权方法在每次调用时实时读取 `plugin_config` 中的最新值，确保 YAML 配置热重载后立即生效。

---

## 鉴权方法

### `def is_superuser(user_id: str) -> bool`

检查用户是否在 `plugin_config.superusers` 集合中。

### `def is_ai_admin(user_id: str) -> bool`

检查用户是否在 `plugin_config.ai_admin_qq` 集合中。

### `def is_group_admin(sender_role: str) -> bool`

检查角色是否为 `"owner"` 或 `"admin"`。

### `def has_command_privilege(user_id: str, sender_role: str) -> bool`

综合权限检查：超管 OR AI 管理员 OR 群管理。供 `admin_hard.py` 中所有管理指令 handler 调用。

### `def is_user_whitelisted(user_id: str, scope: str) -> bool`

检查用户是否在指定功能白名单中（`"private"` 或 `"drawing"`）。

### `def is_private_whitelisted(user_id: str) -> bool`

`is_user_whitelisted` 的便捷封装，检查私聊白名单。

---

## 管理操作

### `async def check_bot_admin_status(bot: Bot, group_id: int) -> bool`

检查 Bot 自身在群内的管理员权限。优先使用 `no_cache=True`（go-cqhttp 扩展），失败时回退到普通查询。

### `async def ban_user(bot, group_id, target_id, duration, operator_id, reason="Admin Command") -> str`

执行禁言操作。

**安全检查链：**
1. 检查 Bot 是否为管理员
2. 拒绝禁言群主和管理员
3. 调用 `bot.set_group_ban()`

**参数：**
- `duration` — 禁言时长（秒），0 表示解除禁言

**返回值：**
- 成功：`"✅ 已解除 <target_id> 的禁言。"` 或 `"🚫 用户 <target_id> 已被禁言 N秒。"`
- 失败：`"❌ 我没有管理员权限..."` 或 `"❌ 无法禁言管理员..."`

### `async def kick_user(bot, group_id, target_id, reject_add_request=False) -> str`

执行踢人操作。安全检查链与 `ban_user` 类似。

---

## AI 智能审计

### `async def ai_audit_and_punish(bot, group_id, target_id) -> str`

基于 LLM 的违规内容审计。

**流程：**
1. 获取群聊历史记录
2. 提取目标用户最近 15 条消息
3. 发送至 DeepSeek LLM 进行违规检测
4. 若 LLM 返回 `{"violation": true}`，自动执行禁言

**返回值：**
- 违规：`"🤖 AI 审计判定违规。\n理由：...\n执行结果：..."`
- 未违规：`"🤖 AI 审计判定未违规。\n理由：..."`
- 失败：`"❌ ..."`

---

## 辅助方法

### `def parse_duration(text: str) -> int`

解析自然语言时间描述。

**支持格式：**
- 中文：`"10分钟"`, `"1小时"`, `"30秒"`, `"1天"`
- 英文：`"30s"`, `"5min"`, `"2h"`, `"1d"`

**返回值：** 对应秒数，上限 2592000 秒（30 天），默认 1800 秒。

```python
seconds = perm_service.parse_duration("5分钟")  # 返回 300
```
