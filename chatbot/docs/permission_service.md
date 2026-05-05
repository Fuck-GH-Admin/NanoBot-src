# PermissionService

**文件路径：** `plugins/chatbot/services/permission_service.py`

**模块职责：** 权限与群管理核心服务。统一鉴权（Superuser / AI Admin / Group Admin）、执行禁言/踢人操作、基于 Node.js 大脑的智能违规审计。

---

## 核心类与接口

### `PermissionService`

> **热更新支持：** 本服务不在 `__init__` 中缓存配置数据。所有鉴权方法（`is_superuser`、`is_ai_admin`、`is_user_whitelisted` 等）在每次调用时实时读取 `plugin_config` 中的最新值，确保 YAML 配置热重载后立即生效。

| 方法 | 说明 |
|------|------|
| `is_superuser` | 检查是否为最高权限宿主 |
| `is_ai_admin` | 检查是否为 Bot 维护管理员 |
| `is_group_admin` | 检查是否为群管理员/群主 |
| `has_command_privilege` | 综合权限检查 |
| `is_user_whitelisted` | 检查特定功能白名单 |
| `is_private_whitelisted` | 检查私聊白名单（兼容方法） |
| `check_bot_admin_status` | 检查 Bot 自身在群内的管理员权限 |
| `ban_user` | 执行禁言操作（含层级检查） |
| `kick_user` | 执行踢人操作 |
| `ai_audit_and_punish` | AI 智能审计并自动惩罚 |
| `parse_duration` | 解析自然语言时间描述 |

---

### `def is_superuser(user_id: str) -> bool`

**参数说明：** `user_id` — 用户 QQ 号

**返回值：** `True` 如果用户是最高权限宿主

---

### `def has_command_privilege(user_id: str, sender_role: str) -> bool`

**参数说明：**
- `user_id` — 用户 QQ 号
- `sender_role` — 发送者角色（`"owner"`, `"admin"`, `"member"`）

**返回值：** `True` 如果用户有权执行管理指令

---

### `async def ban_user(bot: Bot, group_id: int, target_id: int, duration: int, operator_id: str, reason: str = "Admin Command") -> str`

**参数说明：**
- `bot` — NoneBot Bot 实例
- `group_id` — 群号
- `target_id` — 目标用户 QQ 号
- `duration` — 禁言时长（秒），0 表示解除禁言
- `operator_id` — 操作者 QQ 号
- `reason` — 禁言原因

**返回值：**
- 成功：`"✅ 已解除 <target_id> 的禁言。"` 或 `"🚫 用户 <target_id> 已被禁言 N秒。"`
- 失败：`"❌ 我没有管理员权限..."` 或 `"❌ 无法禁言管理员..."`

**调用示例：**
```python
msg = await perm_service.ban_user(bot, 12345678, 87654321, 3600, "admin_qq", "刷屏")
```

---

### `async def ai_audit_and_punish(bot: Bot, group_id: int, target_id: int) -> str`

**参数说明：**
- `bot` — NoneBot Bot 实例
- `group_id` — 群号
- `target_id` — 目标用户 QQ 号

**返回值：**
- 违规：`"🤖 AI 审计判定违规。\n理由：...\n执行结果：..."`
- 未违规：`"🤖 AI 审计判定未违规。\n理由：..."`
- 失败：`"❌ ..."`

**调用示例：**
```python
result = await perm_service.ai_audit_and_punish(bot, 12345678, 87654321)
```

---

### `def parse_duration(text: str) -> int`

**参数说明：** `text` — 自然语言时间，如 `"10分钟"`, `"1小时"`, `"30s"`, `"1天"`

**返回值：** 对应的秒数（上限 2592000 秒 = 30 天），默认 1800 秒

**调用示例：**
```python
seconds = perm_service.parse_duration("5分钟")  # 返回 300
```

---

### 依赖变更说明（与旧版对比）

| 项目 | 旧版 | 新版 |
|------|------|------|
| LLM 依赖 | `LLMService` | 无（已移除） |
| 审计调用 | `self.llm_service.chat(...)` | `self._call_node_chat(...)` → Node.js 大脑 |
| HTTP 库 | `aiohttp` | `httpx` |
| 构造函数 | 初始化 `self.llm_service = LLMService()` | 初始化 `self.node_chat_url` |
| 配置绑定 | `__init__` 中硬绑定 `self.superusers` 等 | 每次方法调用实时读取 `plugin_config`（支持热重载） |
