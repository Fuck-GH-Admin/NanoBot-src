# Chatbot B — Multi-Agent 双端协同聊天机器人

> **Python (NoneBot2) 负责逻辑编排 · Node.js (Express) 负责提示词编译与 LLM 调用**

一个面向 QQ 群聊的工业级 AI 聊天机器人系统，采用双进程协作架构，具备完整的记忆管理、知识图谱、工具调用和安全防护能力。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **双端强类型契约** | Python `Pydantic` + Node.js `Zod` 双重校验，CI 自动检测契约漂移 |
| **SQLite 实体关系记忆** | 六张核心表支撑对话历史、群友画像、知识图谱三元组、压缩任务流水 |
| **时间衰减知识图谱** | 半衰期公式 `confidence × 0.5^(age/half_life)` 自动淡化陈旧关系 |
| **高可用任务队列** | `asyncio.Queue` + `CompactionJournal` 持久化 + 指数退避重试 + 死信追踪 |
| **智能循环终止** | Jaccard 重复检测 + `mark_task_complete` 系统工具 + `max_loops` 兜底 |
| **语义向量检索** | FAISS + DeepSeek Embedding API，世界书从纯关键词升级为语义+关键词混合召回 |
| **Token 预算仲裁** | 按优先级裁剪系统区块（世界知识→群组记忆→关系图→历史），保证不超限 |
| **安全 XML 模板** | 所有动态变量自动转义，防止提示词注入 |
| **零信任管理面板** | Bearer Token 鉴权 + 阅后即焚，每次重启自动轮换 |
| **灰度特性开关** | 6 个 Feature Flag 控制各子系统的启用/禁用 |

---

## 系统拓扑

```
┌─────────────────────────────────────────────────────────────┐
│                      Python (NoneBot2)                       │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────┐    │
│  │ Matchers │→│ AgentService  │→│   MemoryService     │    │
│  │ (事件入口)│  │ (ReAct 循环)  │  │ (后台压缩队列)      │    │
│  └──────────┘  └──────┬───────┘  └────────────────────┘    │
│                       │ HTTP POST                           │
│                       ▼                                      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Node.js (Express :3010)                    │ │
│  │  ┌─────────────┐  ┌──────────┐  ┌──────────────────┐  │ │
│  │  │TavernCoreV2 │→│lorebook  │→│DeepSeekTavernClient│  │ │
│  │  │(Prompt编译)  │  │(世界书扫描)│  │  (LLM 调用)       │  │ │
│  │  └─────────────┘  └──────────┘  └──────────────────┘  │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
    ┌─────────┐                      ┌──────────────┐
    │ SQLite  │                      │ DeepSeek API │
    └─────────┘                      └──────────────┘
```

**职责边界**：
- **Python 端**：事件监听、权限控制、工具执行、记忆压缩、实体关系提取、数据库持久化
- **Node 端**：角色卡渲染、世界书扫描、宏替换、Token 预算仲裁、LLM API 调用

---

## 快速启动

### 1. 环境要求

- Python 3.11+
- Node.js 20+
- SQLite (随 Python 自动创建)

### 2. 配置

复制并编辑配置文件：

```bash
cp config_bot_base.yaml.example config_bot_base.yaml
```

关键配置项：

```yaml
# DeepSeek API
deepseek_api_key: "sk-..."
deepseek_api_url: "https://api.deepseek.com/chat/completions"

# Node.js 引擎
node_deepseek_api_key: "sk-..."
node_base_url: "https://api.deepseek.com"
node_model: "deepseek-chat"
node_temperature: 0.7

# 管理员 QQ
superusers:
  - "123456789"
```

### 3. 安装依赖

```bash
# Python 依赖
pip install nonebot2 nonebot-adapter-onebot sqlalchemy aiosqlite httpx pydantic pydantic-settings pyyaml watchdog faiss-cpu numpy

# Node.js 依赖（自动安装，也可手动）
cd engine && npm install
```

### 4. 启动

```bash
# 方式一：通过 NoneBot2 启动（推荐，自动管理 Node.js 子进程）
nb run

# 方式二：分别启动
# 终端 1：启动 Node.js 引擎
cd engine
DEEPSEEK_API_KEY=sk-xxx DEEPSEEK_BASE_URL=https://api.deepseek.com \
DEEPSEEK_MODEL=deepseek-chat LLM_TEMPERATURE=0.7 NODE_PORT=3010 \
node server.js

# 终端 2：启动 Python 主进程
python -m nonebot run
```

### 5. 运行契约测试

```bash
# 确保 Node.js 服务已启动
pytest tests/test_contract_drift.py -v
```

---

## 管理面板

启动后访问 `http://127.0.0.1:8081` 打开 Web 配置面板。

- 首次访问需通过浏览器打开（Token 自动注入 HTML）
- API 接口需携带 `Authorization: Bearer <token>` 头
- 每次 Python 重启后 Token 自动轮换

---

## Feature Flags

在 `config_bot_base.yaml` 中配置，支持灰度开启：

| Flag | 默认值 | 说明 |
|------|--------|------|
| `enable_strict_schema` | `True` | 跨端 Pydantic Schema 校验 |
| `enable_task_queue` | `False` | 高可用任务队列（持久化+重试） |
| `enable_dynamic_loop` | `False` | 智能循环终止（Jaccard+完成信号） |
| `entity_relation_enabled` | `False` | 知识图谱实体/关系提取 |
| `semantic_lorebook_enabled` | `False` | 语义向量检索（FAISS） |
| `token_arbitration_enabled` | `False` | Token 预算优先级裁剪 |

---

## 项目结构

```
src/plugins/chatbot/
├── __init__.py              # 生命周期管理（启动/关闭 Node.js + 消费者）
├── config.py                # 配置模型 + 热更新 + Web 管理面板
├── schemas.py               # Pydantic 数据契约（SSOT）
├── engine/                  # Node.js 提示词编译引擎
│   ├── server.js            # Express 服务入口
│   ├── schemas.js           # Zod 数据契约（镜像）
│   ├── TavernCoreV2.js      # Prompt 构造器 + Token 仲裁
│   ├── lorebook-engine.js   # 世界书扫描引擎
│   ├── prompt-template.js   # 安全 XML 模板
│   ├── tavern-engine.js     # 宏替换 + Token 计数
│   └── DeepSeekTavernClient.js  # LLM 调用客户端
├── repositories/            # 数据访问层
│   ├── models.py            # SQLAlchemy ORM 模型
│   ├── memory_repo.py       # 记忆仓库（六张表 CRUD）
│   ├── image_repo.py        # 图片元数据仓库
│   └── book_repo.py         # 书籍文件仓库
├── services/                # 业务逻辑层
│   ├── agent_service.py     # ReAct 智能体循环
│   ├── memory_service.py    # 后台记忆压缩队列
│   ├── permission_service.py # 权限与审计
│   ├── image_service.py     # 图片检索与抗风控
│   ├── drawing_service.py   # AI 绘图
│   └── book_service.py      # 书籍管理与 JM 下载
├── tools/                   # LLM 可调用工具
│   ├── base_tool.py         # 工具抽象基类
│   ├── registry.py          # 工具注册表 + 权限过滤
│   ├── image_tool.py        # 绘图/搜图工具
│   ├── admin_tool.py        # 禁言管理工具
│   ├── book_tool.py         # 推荐书/JM 下载工具
│   └── system_tool.py       # 系统级工具（mark_task_complete）
├── utils/                   # 工具库
│   ├── embedding.py         # FAISS 语义检索
│   ├── string_utils.py      # 字符串处理
│   ├── file_utils.py        # 异步文件操作
│   └── pdf_utils.py         # PDF 转换与加密
├── matchers/                # NoneBot2 事件响应器
│   ├── chat_entry.py        # 聊天入口（@触发 + 随机插嘴）
│   ├── admin_hard.py        # 管理指令（活跃度/潜水/授权）
│   └── event_notice.py      # 戳一戳/进出群事件
├── tests/                   # 契约测试
│   ├── conftest.py          # pytest 路径配置
│   └── test_contract_drift.py  # 跨端 Schema 漂移检测
├── web/
│   └── index.html           # 管理面板前端
└── docs/                    # 项目文档
    ├── ARCHITECTURE.md
    └── CROSS_END_CONTRACT.md
```

---

## 许可证

Private — 仅供内部使用。
