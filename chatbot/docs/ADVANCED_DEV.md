# 高级开发与扩展指南 — Chatbot B V4

> 面向二次开发者的技术参考。
> 最后更新：2026-05-14

---

## 目录

1. [contextvars 日志路由系统](#1-contextvars-日志路由系统)
2. [向工具注册表添加新工具](#2-向工具注册表添加新工具)
3. [FastAPI Web 面板鉴权机制](#3-fastapi-web-面板鉴权机制)
4. [Prompt 编译引擎](#4-prompt-编译引擎)
5. [后台守护进程与自愈机制](#5-后台守护进程与自愈机制)

---

## 1. contextvars 日志路由系统

### 1.1 问题背景

在群聊场景中，多个群的消息并发处理。传统日志将所有群的消息混写到同一个文件，排查问题时需要逐行筛选。系统需要按 `group_id`（群聊）或 `user_id`（私聊）自动分流到独立日志文件。

### 1.2 实现方案

使用 Python 标准库 `contextvars` 配合 loguru 的 `logger.contextualize()` 实现。

#### 核心代码（chat_entry.py）

```python
from nonebot.log import logger

# 在消息入口绑定日志上下文
log_ctx = {"group_id": group_id} if is_group else {"private_user_id": user_id}

with logger.contextualize(**log_ctx):
    # 整个处理链路中的 logger 调用自动携带 group_id / private_user_id
    logger.info(f"[ATTENTION] handle_chat 入口: group_id={group_id}")
    result = await agent_srv.run_agent(user_id, text, context)
```

#### contextvars 传播机制

`logger.contextualize()` 内部使用 `contextvars.ContextVar` 存储额外字段：

```python
# loguru 内部实现（简化）
_token = _context_var.set({"group_id": 123456})
try:
    yield  # 执行 with 块内的代码
finally:
    _context_var.reset(_token)
```

`contextvars.ContextVar` 的特性：
- 自动跨越 `await` 边界传播（协程切换时 context 被保留）
- 每个 asyncio Task 拥有独立的 context 副本（天然隔离）
- 嵌套调用自动继承父 context

#### loguru Filter 路由

Custom Callable Sink 中通过 `record["extra"]` 判断路由目标：

```python
def _group_log_sink(message):
    record = message.record
    extra = record["extra"]

    if "group_id" in extra:
        gid = extra["group_id"]
        path = f"logs/groups/{datetime.now():%Y-%m-%d}_group_{gid}.log"
    elif "private_user_id" in extra:
        uid = extra["private_user_id"]
        path = f"logs/private/{datetime.now():%Y-%m-%d}_private_{uid}.log"
    else:
        path = "logs/chatbot.log"

    # 写入对应文件
    with open(path, "a", encoding="utf-8") as f:
        f.write(message)
```

### 1.3 contextualize() vs bind()

| 特性 | `contextualize()` | `bind()` |
|------|:-----------------:|:--------:|
| 底层机制 | `contextvars.ContextVar` | 返回新 logger 实例 |
| 异步传播 | 自动跨越 `await` | 需手动传递实例 |
| 代码侵入性 | 低（入口绑定一次） | 高（需在每个调用点传递） |
| 嵌套继承 | 自动 | 仅当使用同一实例 |

**推荐**：在异步场景中使用 `contextualize()`，在同步场景中两者皆可。

### 1.4 文件结构

```
logs/
├── chatbot.log                           # 系统级（10MB 轮转，保留 20 个）
├── groups/
│   ├── 2026-05-14_group_123456.log       # 按天 × 群 自动创建
│   ├── 2026-05-14_group_789012.log
│   └── 2026-05-13_group_123456.log       # 旧日期文件自然沉淀
└── private/
    ├── 2026-05-14_private_111111.log     # 按天 × 用户 自动创建
    └── 2026-05-13_private_111111.log
```

---

## 2. 向工具注册表添加新工具

### 2.1 工具基类

所有工具继承 `BaseTool`（`tools/base_tool.py`）：

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

class BaseTool(ABC):
    name: str                          # 工具名称（唯一标识）
    description: str                   # 工具描述（LLM 可见）
    parameters: dict                   # JSON Schema 参数定义
    require_permission: str = "user"   # 权限级别
    is_write_operation: bool = False   # 是否写操作（决定是否记录审计日志）

    @abstractmethod
    async def execute(self, arguments: Dict[str, Any], context: Dict[str, Any]) -> Tuple[str, List[str]]:
        """
        执行工具。
        返回: (result_text, [image_file_paths])
        """
        ...
```

### 2.2 添加数据面工具（LLM 可见）

**步骤 1：创建工具类**

在 `tools/agent_tools/` 下创建或编辑文件：

```python
# tools/agent_tools/my_tool.py

from ..base_tool import BaseTool

class MyNewTool(BaseTool):
    name = "my_new_tool"
    description = "这是一个新工具，用于执行某某操作。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询参数"
            }
        },
        "required": ["query"]
    }
    require_permission = "user"      # "user" | "drawing_whitelist" | "admin" | "system"
    is_write_operation = False       # True 则记录审计日志

    async def execute(self, arguments, context):
        query = arguments.get("query", "")
        # ... 执行逻辑 ...
        result = f"查询结果: {query}"
        images = []  # 如有图片文件路径，放入此列表
        return result, images
```

**步骤 2：注册到 AgentToolRegistry**

在 `services/agent_service.py:_register_tools()` 中添加：

```python
from ..tools.agent_tools.my_tool import MyNewTool

def _register_tools(self):
    # ... 现有工具 ...
    self.registry.register(MyNewTool())
```

**步骤 3（可选）：添加硬指令前缀**

在 `config.py` 的 `force_tool_prefixes` 中添加映射：

```python
force_tool_prefixes = {
    "/jm": "jm_download",
    "#搜图": "search_acg_image",
    "/画图": "generate_image",
    "/my": "my_new_tool",      # 新增
}
```

### 2.3 添加控制面工具（LLM 不可见）

**步骤 1：创建工具类**

在 `tools/system_tools/` 下创建：

```python
# tools/system_tools/my_admin_tool.py

from ..base_tool import BaseTool

class MyAdminTool(BaseTool):
    name = "my_admin_action"
    description = "管理员专用操作"
    parameters = { ... }
    require_permission = "admin"
    is_write_operation = True

    async def execute(self, arguments, context):
        # ... 执行逻辑 ...
        return result, []
```

**步骤 2：注册到 SystemToolRegistry**

在 `matchers/admin_hard.py` 中注册并直接调用：

```python
from ..tools.system_tools.my_admin_tool import MyAdminTool

system_registry = SystemToolRegistry()
system_registry.register(MyAdminTool())

# 在 Alconna 指令处理中直接调用
result, _ = await system_registry.execute_tool("my_admin_action", args, context)
```

### 2.4 权限模型详解

| 权限级别 | Schema 可见 | 执行时校验 | 典型用途 |
|---------|:-----------:|----------:|---------|
| `"user"` | 是 | 无 | 搜图、推荐 |
| `"drawing_whitelist"` | 是 | `perm_srv.is_user_whitelisted(uid, "drawing")` | 画图 |
| `"admin"` | 是 | `context["is_admin"]` | 管理操作 |
| `"system"` | 是（始终注入） | 跳过权限检查 | `no_op`、`mark_task_complete` |

### 2.5 AgentToolRegistry 防重放

`AgentToolRegistry` 在每个 `request_id` 内维护签名计数器：

```python
class AgentToolRegistry(ToolRegistry):
    MAX_EXEC_PER_SIGNATURE = 2

    def _check_and_record(self, request_id, tool_name, arguments):
        sig = f"{tool_name}|{normalized_args}"
        count = counters.get(sig, 0)
        if count >= self.MAX_EXEC_PER_SIGNATURE:
            return False, "防重放拦截..."
        counters[sig] = count + 1
        return True, ""
```

与逻辑循环中的 `executed_signatures` 集合形成双重去重：
- **循环层**（`agent_service.py`）：同签名仅进入执行流程一次
- **注册表层**（`AgentToolRegistry`）：同签名同一请求最多执行 2 次

---

## 3. FastAPI Web 面板鉴权机制

### 3.1 架构

```
┌──────────────────────────────────────────────────────────────┐
│  FastAPI App (config.py)                                     │
│                                                              │
│  中间件: CORSMiddleware (allow_origins=["*"])                │
│                                                              │
│  静态文件: GET / → web/index.html                            │
│                                                              │
│  认证端点:                                                    │
│    POST /api/login → 验证密码 → Set-Cookie                   │
│                                                              │
│  受保护端点 (Depends(_check_auth)):                           │
│    GET  /api/config           获取配置                       │
│    POST /api/config           保存配置                       │
│    GET  /api/worldbook        获取世界书                     │
│    POST /api/worldbook/save   保存世界书                     │
│    POST /api/worldbook/draft/approve  批准草稿               │
│    POST /api/worldbook/draft/reject   拒绝草稿               │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 登录流程

```python
@fastapi_app.post("/api/login")
async def api_login(body: dict, response: Response):
    await asyncio.sleep(1.0)  # 防暴力破解：固定延迟 1 秒
    password = body.get("password", "")
    if not secrets.compare_digest(password, plugin_config.web_admin_password):
        return JSONResponse({"success": False, "error": "密码错误"}, status_code=401)
    token = plugin_config._admin_token
    response.set_cookie(
        key="chatbot_admin_session", value=token,
        httponly=True, samesite="lax", max_age=86400,
    )
    return {"success": True, "token": token}
```

**安全特性**：

| 特性 | 实现 | 防御目标 |
|------|------|---------|
| 固定延迟 | `asyncio.sleep(1.0)` | 防暴力破解（响应时间不泄露密码对错） |
| 恒定时间比较 | `secrets.compare_digest()` | 防时序攻击（比较时间不泄露差异位置） |
| HTTPOnly Cookie | `httponly=True` | 防 XSS 窃取 Cookie |
| SameSite=Lax | `samesite="lax"` | 防 CSRF |
| Token 重启刷新 | `secrets.token_urlsafe(32)` | 重启后旧 Token 失效 |

### 3.3 鉴权依赖注入

```python
async def _check_auth(
    chatbot_admin_session: str = Cookie(default=None),
    authorization: str = Header(default=None),
):
    """FastAPI Depends：校验 session cookie 或 Bearer token。"""
    token = chatbot_admin_session
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if token and secrets.compare_digest(token, _manager._admin_token):
        return
    raise _Unauthorized
```

支持两种认证方式：
1. **Cookie**（浏览器）：`chatbot_admin_session`
2. **Bearer Token**（API 调用）：`Authorization: Bearer <token>`

### 3.4 配置保存的原子性

`POST /api/config` 的处理流程：

```
1. 字段校验 & 类型转换
   ├─ Set 字段：列表/逗号分隔字符串 → set
   ├─ Bool 字段：字符串 "true"/"1" → True
   └─ GroupSettings：逐 group_id 校验

2. 构建候选配置 (deep copy，不更新内存)

3. 原子落盘
   ├─ tempfile.mkstemp() 创建临时文件
   ├─ yaml.dump() 写入临时文件
   └─ os.replace() 原子性替换目标文件
   └─ 失败则内存不更新

4. 落盘成功 → 更新内存 _config → _version++
```

---

## 4. Prompt 编译引擎

### 4.1 PromptPipeline 架构

`PromptPipeline`（`engine/prompt_builder.py`）负责将所有上下文组装为最终 Prompt：

```
┌──────────────────────────────────────────────────────────────┐
│  PromptPipeline.build()                                      │
│                                                              │
│  1. 角色卡 (CharacterCard)                                   │
│     ├─ system prompt (name + description + personality)      │
│     └─ 宏替换 ({{user}}, {{char}}, {{time}}, ...)           │
│                                                              │
│  2. Depth Injection                                          │
│     └─ 在指定 depth 插入历史消息                             │
│                                                              │
│  3. SystemBlock 组装                                         │
│     ├─ logic_directives / actor_world_knowledge              │
│     ├─ dynamic_rule (匹配的动态规则, never_cut)              │
│     ├─ session_lifecycle (生命周期感知, never_cut)           │
│     └─ system_tool_result (工具执行结果, never_cut)          │
│                                                              │
│  4. TokenArbitrator 裁剪                                     │
│     └─ 超预算时按优先级裁剪                                  │
│                                                              │
│  5. 输出 ChatMessage[] → to_openai_format()                  │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 双脑 Prompt 编译

`PromptAdapter`（`prompt_adapter.py`）为逻辑脑和演员脑编译不同的 Prompt：

```python
class PromptAdapter:
    def compile_logic_prompt(self, history, snapshot, context, worldbook_entries=""):
        """逻辑脑：极简指令 + 工具 schema + 历史（XML 封印）"""

    def compile_actor_prompt(self, chat_history, snapshot, context,
                              system_notification="", worldbook_entries=""):
        """演员脑：完整角色卡 + 世界书 + 工具结果 + 生命周期感知"""
```

### 4.3 Token 仲裁

**优先级层次**（`engine/token_budget.py`）：

```
Priority 1: SYSTEM_DIRECTIVES  ──→  never_cut = True  (永不裁剪)
Priority 2: ROLE_PLAY_SETTING  ──→  never_cut = True  (永不裁剪)
Priority 3: CHAT_HISTORY       ──→  shift from oldest (从最旧开始移除)
Priority 4: WORLD_KNOWLEDGE    ──→  pop items from end (从末尾弹出)
```

**两阶段计数**：

| 阶段 | 方法 | 触发条件 | 精度 |
|------|------|---------|------|
| Phase 1 | 自适应字符比率估算 | 始终执行 | 近似（Latin 4.0, CJK 1.8 chars/token） |
| Phase 2 | `tiktoken cl100k_base` | Phase 1 接近预算时 | 精确 |

---

## 5. 后台守护进程与自愈机制

### 5.1 任务清单

```
┌──────────────────────────────────────────────────────────────┐
│  @driver.on_startup 启动的后台任务                           │
│                                                              │
│  #. 任务                          间隔       自愈包装        │
│  ─────────────────────────────────────────────────────────── │
│  1. MemoryCircuitBreaker.monitor  10s       裸协程 (raw)     │
│  2. EventLoopMonitor.start        1s        裸协程 (raw)     │
│  3. chat_entry._cleanup_sessions  10min     裸协程 (raw)     │
│  4. topic_harvester_daemon        60s       run_with_self_heal│
│  5. ttl_cleanup_loop              86400s    run_with_self_heal│
│  6. start_config_web_server       —         asyncio task     │
│  7. start_web_server (FastAPI)    —         asyncio task     │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 自愈包装

`run_with_self_heal()` 包装器（`__init__.py`）：

```python
async def run_with_self_heal(coro_func, *args, **kwargs):
    """异常捕获 → 紧急告警 → 5 秒后自动重启。"""
    while True:
        try:
            await coro_func(*args, **kwargs)
        except asyncio.CancelledError:
            break  # 正常退出
        except Exception as e:
            logger.error(f"[SelfHeal] {coro_func.__name__} 崩溃: {e}")
            await send_emergency_alert(f"后台任务 {coro_func.__name__} 崩溃，5 秒后重启")
            await asyncio.sleep(5)
```

仅 `topic_harvester_daemon` 和 `ttl_cleanup_loop` 使用了自愈包装；其他为裸协程，异常后不会自动重启（需依赖进程级重启）。

### 5.3 内存断路器

`MemoryCircuitBreaker`（`guardian.py`）每 10 秒检查进程内存使用：

```
内存 < 阈值 → 正常
内存 ≥ 阈值 → 触发 GC → 重新检查
内存仍超限 → 发送紧急告警
```

### 5.4 事件循环监控

`EventLoopMonitor`（`guardian.py`）每 1 秒检测 asyncio 事件循环延迟：

```
延迟 < 1s → 正常
延迟 ≥ 1s → 记录警告（事件循环可能被阻塞）
```

---

*本文档基于源码全局审阅生成，反映 2026-05-14 的系统状态。*
