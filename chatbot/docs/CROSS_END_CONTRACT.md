# 跨端契约白皮书

> Python ↔ Node.js 数据通信协议的权威规范。

---

## 1. 契约哲学

### 1.1 为什么需要跨端契约？

本系统的 Python 和 Node.js 进程通过 HTTP JSON 通信。两个进程使用不同的语言、不同的类型系统、不同的校验库。如果没有统一的契约约束，任何一端的字段变更（重命名、类型修改、新增必填项）都会导致另一端静默失败或抛出运行时异常。

### 1.2 单一事实来源 (SSOT)

```
Python Pydantic (schemas.py)  ←── SSOT
        │
        ├──→ Node Zod (schemas.js)  ←── 镜像 + 网关拦截
        │
        └──→ CI 测试 (test_contract_drift.py) ←── 自动化验证
```

- **Python `schemas.py`** 是数据结构的唯一权威定义
- **Node `schemas.js`** 是 Python 定义的 Zod 镜像，用于服务端入口校验
- **CI 测试** 自动验证两端定义是否一致（防止"文档一致、实现漂移"）

### 1.3 防御深度

```
请求流向：
  Python Pydantic 校验 → HTTP 发送 → Node Zod 校验 → 业务逻辑
       (第一道防线)                   (第二道防线)
```

任何一道防线拦截到非法数据，都会返回 `400 Bad Request` 并附带详细的错误信息。

---

## 2. 核心负载结构

### 2.1 ChatRequestPayload

**Python 定义** (`schemas.py`):

```python
class ChatRequestPayload(BaseModel):
    chatHistory: List[Dict[str, Any]]
    memorySnapshot: MemorySnapshot
    tools: List[Dict[str, Any]] = []
    context: Dict[str, Any] = {}
```

**Node 定义** (`schemas.js`):

```javascript
const ChatRequestSchema = z.object({
    chatHistory: z.array(MessageSchema.passthrough()),
    memorySnapshot: MemorySnapshotSchema,
    tools: z.array(z.any()).optional().default([]),
    context: z.record(z.any()).optional().default({})
});
```

### 2.2 MessageSchema (chatHistory 元素)

**Node 定义**（严格约束）:

```javascript
const MessageSchema = z.object({
    role: z.enum(['user', 'assistant', 'system', 'tool']),  // 严格枚举
    content: z.string().optional(),
    name: z.string().optional(),
    user_id: z.string().optional(),
    timestamp: z.string().optional(),
    tool_call_id: z.string().optional(),
    tool_calls: z.array(z.any()).optional(),
});
```

> **注意**：Python 端的 `chatHistory` 使用 `List[Dict[str, Any]]`（宽松），因为 Python 端是数据生产者，已经通过 `_to_openai_message()` 保证了格式正确性。Node 端使用 `MessageSchema.passthrough()` 进行严格校验，因为 Node 端是数据消费者，需要防御性地拒绝非法输入。

### 2.3 MemorySnapshot

**Python 定义**:

```python
class MemorySnapshot(BaseModel):
    summary: str = ""
    profiles: List[UserProfile] = []
    entities: List[Entity] = []
    relations: List[Relation] = []
```

**Node 定义**:

```javascript
const MemorySnapshotSchema = z.object({
    summary: z.string().default(""),
    profiles: z.array(UserProfileSchema).default([]),
    entities: z.array(EntitySchema).default([]),
    relations: z.array(RelationSchema).default([])
});
```

### 2.4 子结构定义

#### UserProfile

```python
# Python
class UserProfile(BaseModel):
    user_id: str
    traits: List[Trait] = []
```

```javascript
// Node
const UserProfileSchema = z.object({
    user_id: z.string(),
    traits: z.array(TraitSchema).default([])
});
```

#### Trait

```python
# Python
class Trait(BaseModel):
    uid: Optional[str] = None
    content: str
    confidence: float = Field(ge=0.0, le=1.0)
    updated_at: str = ""
```

```javascript
// Node
const TraitSchema = z.object({
    uid: z.string().nullable().optional(),
    content: z.string(),
    confidence: z.number().min(0).max(1),
    updated_at: z.string().default("")
});
```

#### Entity

```python
# Python
class Entity(BaseModel):
    entity_id: str
    name: str
    type: str
    attributes: Dict[str, Any] = {}
```

```javascript
// Node
const EntitySchema = z.object({
    entity_id: z.string(),
    name: z.string(),
    type: z.string(),
    attributes: z.record(z.any()).default({})
});
```

#### Relation

```python
# Python
class Relation(BaseModel):
    relation_id: str
    subject_entity: str
    predicate: str
    object_entity: str
    confidence: float = Field(ge=0.0, le=1.0)
```

```javascript
// Node
const RelationSchema = z.object({
    relation_id: z.string(),
    subject_entity: z.string(),
    predicate: z.string(),
    object_entity: z.string(),
    confidence: z.number().min(0).max(1)
});
```

---

## 3. 自动化保障

### 3.1 CI/CD 流水线

```yaml
# .github/workflows/contract-test.yml
name: Contract Drift Test

on:
  push:
    paths:
      - 'src/plugins/chatbot/schemas.py'
      - 'src/plugins/chatbot/engine/schemas.js'
      - 'src/plugins/chatbot/tests/**'

jobs:
  contract-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
      - uses: actions/setup-python@v5

      - name: Start Node Test Server
        run: |
          cd src/plugins/chatbot/engine
          npm install
          DEEPSEEK_API_KEY=fake node server.js &

      - name: Run Contract Drift Tests
        run: |
          cd src/plugins/chatbot
          pytest tests/test_contract_drift.py -v
```

### 3.2 测试用例矩阵

| 测试用例 | 输入 | 预期结果 | 验证目标 |
|----------|------|----------|----------|
| `test_valid_payload_accepted` | 完全合法的 payload | `status != 400` | 合法数据不应被 Schema 拒绝 |
| `test_missing_chatHistory_rejected` | 缺少 `chatHistory` | `status == 400` | 必填字段校验 |
| `test_invalid_confidence_rejected` | `confidence = 1.5` | `status == 400` | 数值范围校验 |
| `test_missing_role_in_history_rejected` | `role = 123` (数字) | `status == 400` | 类型校验（enum） |

### 3.3 漂移检测策略

测试通过以下方式检测契约漂移：

1. **Python 端生成**：使用 `ChatRequestPayload` Pydantic 模型构建合法 payload，通过 `model_dump()` 转为字典
2. **Node 端验证**：将 payload 发送到 Node.js 服务，由 Zod Schema 进行校验
3. **断言检查**：
   - 合法数据必须通过（`status != 400`）
   - 非法数据必须被拦截（`status == 400`）

如果任何一端的 Schema 定义发生变化（如新增必填字段、修改类型约束），测试将自动失败，提醒开发者同步更新另一端。

### 3.4 如何添加新的契约字段

1. 在 Python `schemas.py` 中添加字段（SSOT）
2. 在 Node `schemas.js` 中添加对应的 Zod 定义
3. 在 `tests/test_contract_drift.py` 中添加验证用例
4. 运行 `pytest tests/test_contract_drift.py -v` 确认通过
5. 提交代码，CI 自动验证

---

## 4. 已知限制

1. **Python 端 chatHistory 为宽松类型**：`List[Dict[str, Any]]` 不做字段级校验，依赖 `_to_openai_message()` 的正确性
2. **context 字段为自由结构**：`Dict[str, Any]` / `z.record(z.any())` 不做 Schema 约束，因为 context 是运行时动态组装的
3. **tools 数组元素为 Any**：工具定义的结构由 OpenAI API 规范约束，不在本契约中重复定义
