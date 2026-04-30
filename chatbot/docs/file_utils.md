# AsyncFileUtils

**文件路径：** `plugins/chatbot/utils/file_utils.py`

**模块职责：** 异步文件操作工具类。封装 `aiofiles` 以提供非阻塞的 JSON 读写操作。

---

## 核心类与接口

### `AsyncFileUtils`（纯静态方法）

| 方法 | 说明 |
|------|------|
| `read_json` | 异步读取 JSON 文件 |
| `write_json` | 异步写入 JSON 文件 |

---

### `static async def read_json(path: Union[str, Path], default: Any = None) -> Any`

**参数说明：**
- `path` — 文件路径
- `default` — 文件不存在或解析失败时的默认值

**返回值：** 解析后的 Python 对象，失败返回 `default` 或 `{}`

**调用示例：**
```python
data = await AsyncFileUtils.read_json("data/config.json", default={"key": "val"})
```

---

### `static async def write_json(path: Union[str, Path], data: Any) -> bool`

**参数说明：**
- `path` — 文件路径
- `data` — 要写入的数据

**返回值：** 成功 `True` / 失败 `False`

**调用示例：**
```python
success = await AsyncFileUtils.write_json("data/output.json", {"result": "ok"})
```
