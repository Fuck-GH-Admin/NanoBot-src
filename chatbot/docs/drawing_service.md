# DrawingService

**文件路径：** `plugins/chatbot/services/drawing_service.py`

**模块职责：** AI 绘图服务。通过 Node.js 大脑优化提示词后调用 SiliconFlow API 生成图片并保存到本地。

---

## 核心类与接口

### `DrawingService`

> **热更新支持：** 本服务不在 `__init__` 中缓存 API 密钥和端点。`_call_siliconflow` 等方法在每次执行时实时读取 `plugin_config.siliconflow_api_key`、`plugin_config.siliconflow_api_url` 等配置项，确保 YAML 配置热重载后立即生效。

| 方法 | 说明 |
|------|------|
| `generate_image` | 生成图片的主入口（权限校验 → Prompt 优化 → API 调用） |

---

### `async def generate_image(simple_prompt: str, user_id: str) -> Tuple[str, str]`

**参数说明：**
- `simple_prompt` — 用户输入的简化提示词
- `user_id` — 用户 QQ 号，用于权限白名单校验

**返回值：**
- 成功：`(本地文件绝对路径, "✅ 绘图完成！...")`
- 权限不足：`("", "❌ 你没有绘图权限...")`
- 失败：`("", "❌ 生成失败..."` 或 `"❌ 发生错误: ...")`

**调用示例：**
```python
service = DrawingService()
path, msg = await service.generate_image("一只猫在太空", user_id="123456")
if path:
    print(f"图片已保存: {path}")
```

---

### 内部方法

#### `async def _enhance_prompt(simple_prompt: str) -> str`

调用 Node.js 大脑将简短描述扩展为详细的英文绘图提示词（画风、光线、构图、细节）。

**参数说明：**
- `simple_prompt` — 用户输入的简短描述

**返回值：**
- 成功：优化后的英文提示词
- 失败：返回原始 `simple_prompt`（降级策略）

---

#### `async def _call_siliconflow(prompt: str, user_id: str) -> str`

异步调用 SiliconFlow API 生成图片并下载到 `data/generated_images/`。

**参数说明：**
- `prompt` — 优化后的提示词
- `user_id` — 用户 QQ 号

**返回值：**
- 成功：本地文件绝对路径
- 失败：`""`
