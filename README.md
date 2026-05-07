# Chatbot B — 基于微内核架构的 QQ 智能对话机器人

> **版本**：V3（Alconna 协议 + 控制面/数据面物理隔离 + 影子上下文 + 突变日志）

一个运行于 **NoneBot2** 框架之上的 QQ 群聊/私聊智能对话系统。采用**微内核（Microkernel）设计哲学**，将控制面（Control Plane）与数据面（Data Plane）在物理层面进行隔离，确保大语言模型（LLM）绝对无法越权调用系统管理工具。

---

## 核心架构特征

### 微内核设计：控制面与数据面隔离

```
┌─────────────────────────────────────────────────────────────┐
│                      NoneBot2 事件循环                       │
├──────────────────────────┬──────────────────────────────────┤
│     控制面 Control Plane  │      数据面 Data Plane           │
│  ┌─────────────────────┐ │  ┌────────────────────────────┐  │
│  │  admin_hard.py      │ │  │  chat_entry.py             │  │
│  │  (Alconna 强指令)    │ │  │  (消息入口 + 触发判定)      │  │
│  └────────┬────────────┘ │  └────────────┬───────────────┘  │
│           ▼              │               ▼                  │
│  ┌─────────────────────┐ │  ┌────────────────────────────┐  │
│  │ SystemToolRegistry  │ │  │  AgentToolRegistry         │  │
│  │ (LLM 绝对不可见)     │ │  │  (LLM 可见，受权限过滤)    │  │
│  │                     │ │  │                            │  │
│  │ · BanUserTool       │ │  │ · GenerateImageTool        │  │
│  │                     │ │  │ · SearchAcgImageTool       │  │
│  │                     │ │  │ · RecommendBookTool        │  │
│  │                     │ │  │ · JmDownloadTool           │  │
│  │                     │ │  │ · LearnRuleTool            │  │
│  │                     │ │  │ · ForgetRuleTool           │  │
│  │                     │ │  │ · MarkTaskCompleteTool     │  │
│  └─────────────────────┘ │  └────────────────────────────┘  │
└──────────────────────────┴──────────────────────────────────┘
```

**安全保证**：`SystemToolRegistry` 与 `AgentToolRegistry` 是两个独立的注册表实例，物理隔离于不同的目录（`tools/system_tools/` vs `tools/agent_tools/`）。`BanUserTool` 等控制面工具的 Schema **永远不会**出现在 LLM 的 tools 定义中，因此 LLM 在技术上无法生成对应的 `tool_call`。

### 双脑协作架构

| 脑 | 职责 | 工具访问 | Prompt 特征 |
|----|------|---------|------------|
| **逻辑脑（Logic Brain）** | 意图分析、工具调度 | 可调用 AgentToolRegistry 中的工具 | 极简指令，无角色扮演设定 |
| **演员脑（Actor Brain）** | 人格渲染、角色扮演 | 无工具访问 | 完整角色卡、世界书、影子上下文 |

流程：逻辑脑执行工具 → 将结果通过 `system_notification` 注入演员脑 → 演员脑以角色人格向用户转述结果。

### 影子上下文（Shadow Context）

控制面操作（如禁言）不会写入 `chat_history`（`block=True` 阻断传播），但演员脑需要感知这些操作以避免认知割裂。`ShadowContext` 是一个基于 `TTLCache` 的单例短期事实队列：

- 每次控制面操作后，事实被推入对应 session 的队列
- 演员脑编译 Prompt 时，影子上下文以 `never_cut=True` 的 `SystemBlock` 注入
- 事实自动过期：24 小时无访问即淘汰，单 session 最多保留 5 条

### 突变日志（Mutation Logs）

并非所有工具调用都值得记录。只有**状态变更操作**（`is_write_operation = True`）才写入 `tool_execution_log` 审计表：

| 工具 | 分类 | is_write_operation | 说明 |
|------|------|:------------------:|------|
| `ban_user` | 控制面 | ✅ | 改变用户禁言状态 |
| `generate_image` | 数据面 | ✅ | 消耗资源，产生文件 |
| `jm_download` | 数据面 | ✅ | 消耗资源，产生文件 |
| `learn_rule` / `forget_rule` | 数据面 | ✅ | 改变规则配置 |
| `search_acg_image` | 数据面 | ❌ | 只读查询 |
| `recommend_book` | 数据面 | ❌ | 只读查询 |
| `mark_task_complete` | 数据面 | ❌ | 内部循环信号 |

---

## 强指令系统（Alconna 协议）

所有管理指令通过 `nonebot-plugin-alconna` 进行结构化解析，**不依赖正则表达式**。指令匹配后设置 `block=True`，指令文本**绝对不会**泄漏到 `chat_entry` 的消息处理链路中。

| 指令 | 语法 | 别名 | 权限 | 说明 |
|------|------|------|------|------|
| 退群 | `退群` | `leave` | 管理员 | Bot 退出当前群聊 |
| 调整活跃度 | `调整活跃度 <概率>` | `活跃度`、`插嘴概率` | 管理员 | 设置随机插嘴概率（`0.5` 或 `50%`） |
| 潜水模式 | `潜水模式 <开启\|关闭>` | — | 管理员 | 切换非 @ 消息记录模式 |
| 授权画图 | `授权画图 @用户1 @用户2 ...` | `开启画图白名单` | 管理员 | 为用户解锁画图功能 |
| 禁言 | `禁言 @用户 <秒数>` | `ban` | 管理员 | 禁言指定用户（默认 600 秒） |

### 硬指令前缀（短路路由）

以下前缀命中时，跳过逻辑脑 LLM 推理，直接执行对应工具：

| 前缀 | 映射工具 |
|------|---------|
| `/jm` | `jm_download` |
| `#搜图` | `search_acg_image` |
| `/画图` | `generate_image` |

---

## 快速开始

### 环境要求

- Python 3.11+
- SQLite（随 Python 自动创建）

### 安装依赖

```bash
pip install -e .
```

依赖项定义在 `pyproject.toml` 中，核心包括：

- `nonebot2[fastapi]` + `nonebot-adapter-onebot`
- `nonebot-plugin-alconna`（结构化命令解析）
- `sqlalchemy` + `aiosqlite`（异步 ORM）
- `cachetools`（影子上下文 TTL 缓存）
- `pydantic` + `pydantic-settings`（配置与 Schema 校验）
- `pyyaml` + `watchdog`（配置热重载）
- `httpx`（HTTP 客户端）
- `tiktoken`（Token 精确计数）

### 配置

所有核心配置位于 `config/` 目录：

| 文件 | 用途 |
|------|------|
| `config_bot_base.yaml` | 主配置文件（API Key、超管 QQ、Feature Flag、路径等） |
| `character.json` | 角色卡人设（名字、性格、开场白） |
| `worldbook.json` | 世界书条目（可选，配合语义检索使用） |
| `option.yml` | JM 下载配置 |

**可视化配置面板**：启动后访问 `http://127.0.0.1:8081` 打开 Web 管理面板，所有配置项支持在线修改，热更新即时生效。

### 启动

```bash
python bot.py
```

---

## 项目结构

```
src/plugins/chatbot/
├── __init__.py                 # 插件入口：生命周期钩子、服务自愈
├── config.py                   # ConfigManager（原子写入 + 热重载 + Web API）
├── schemas.py                  # Pydantic 跨端 Schema（CommandResult 等）
├── guardian.py                 # 事件循环监控器
│
├── matchers/                   # NoneBot 事件匹配器
│   ├── admin_hard.py           # 控制面：Alconna 管理指令（block=True）
│   ├── chat_entry.py           # 数据面：消息入口 + 触发判定
│   └── event_notice.py         # 事件通知（戳一戳、欢迎/退群）
│
├── tools/                      # 工具系统（微内核核心）
│   ├── base_tool.py            # BaseTool 抽象基类
│   ├── registry.py             # ToolRegistry / AgentToolRegistry / SystemToolRegistry
│   ├── agent_tools/            # 数据面工具（LLM 可见）
│   │   ├── image_tool.py       # GenerateImageTool, SearchAcgImageTool
│   │   ├── book_tool.py        # RecommendBookTool, JmDownloadTool
│   │   ├── rule_tool.py        # LearnRuleTool, ForgetRuleTool
│   │   └── system_tool.py      # MarkTaskCompleteTool
│   └── system_tools/           # 控制面工具（LLM 绝对不可见）
│       └── admin_tool.py       # BanUserTool
│
├── services/                   # 业务服务层
│   ├── agent_service.py        # AgentService：双脑循环编排
│   ├── prompt_adapter.py       # PromptAdapter：逻辑脑/演员脑 Prompt 编译
│   ├── shadow_context.py       # 影子上下文（TTLCache 单例）
│   ├── permission_service.py   # 权限服务（鉴权 + 管理操作 + AI 审计）
│   ├── memory_service.py       # 记忆压缩服务
│   ├── rule_engine.py          # 动态规则引擎
│   ├── rule_injector.py        # 规则指令注入器
│   ├── drawing_service.py      # AI 画图服务
│   ├── image_service.py        # 搜图服务
│   └── book_service.py         # 书籍/下载服务
│
├── repositories/               # 数据持久层
│   ├── models.py               # SQLAlchemy 2.0 表定义（9 张表）
│   ├── memory_repo.py          # MemoryRepository（单例，异步 CRUD）
│   ├── rule_repo.py            # RuleRepository（规则 + 变更日志）
│   ├── book_repo.py            # BookRepository（文件扫描）
│   └── image_repo.py           # ImageRepository（Pandas 查询）
│
├── engine/                     # Prompt 编译引擎
│   ├── token_budget.py         # Token 预算仲裁（Priority 裁剪）
│   ├── prompt_builder.py       # PromptPipeline 构建器
│   ├── card_parser.py          # 角色卡解析器
│   ├── lorebook_engine.py      # 世界书扫描引擎
│   └── macro_engine.py         # 宏替换引擎
│
├── utils/                      # 工具函数
│   ├── session_dumper.py       # 调试快照（JSONL 格式）
│   ├── alert_manager.py        # 告警管理器
│   ├── embedding.py            # 向量嵌入工具
│   └── keyword_utils.py        # 关键词规范化
│
├── web/                        # Web 管理面板
│   └── index.html              # 前端页面
│
└── docs/                       # 架构文档
    └── ARCHITECTURE.md         # 架构蓝图说明书
```

---

## 安全与物理隔离

### 为什么 LLM 无法越权调用 SystemTools

1. **物理目录隔离**：`BanUserTool` 定义在 `tools/system_tools/admin_tool.py`，而 `AgentToolRegistry` 仅注册 `tools/agent_tools/` 下的工具。两个注册表是独立的实例。

2. **Schema 过滤**：`AgentToolRegistry.get_all_schemas()` 仅返回数据面工具的 OpenAI function-calling Schema。`BanUserTool` 的 Schema **永远不会**出现在发送给 LLM 的 `tools` 参数中。

3. **执行时权限二次验证**：即使通过某种方式绕过了 Schema 过滤，`ToolRegistry.execute_tool()` 在执行前会进行权限二次验证。`admin` 级工具要求 `context["is_admin"]` 为真。

4. **控制面指令走独立路径**：禁言等管理操作通过 `admin_hard.py` 中的 Alconna 指令直接调用 `BanUserTool.execute()`，完全绕过 `AgentService` 和 LLM 推理链路。

### 配置原子化写入

`ConfigManager.save_config()` 采用 **Write-First-Then-Update-Memory** 策略：

1. 将候选配置序列化为 YAML 字典
2. 通过 `tempfile.mkstemp()` 创建临时文件
3. 写入临时文件后，调用 `os.replace()` 原子性替换目标文件
4. 仅在落盘成功后，才更新内存中的 `_config` 对象
5. 每次写入递增 `_version` 计数器

此机制确保：写入中途进程崩溃不会损坏原始配置文件；内存中的配置始终与磁盘一致。

---

## 进阶文档

详细的架构设计、数据模型、API 参考等，请前往 [`docs/`](src/plugins/chatbot/docs/) 目录。

---

## 许可证

私有项目，仅供内部使用。
