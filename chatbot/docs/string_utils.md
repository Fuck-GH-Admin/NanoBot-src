# StringUtils

**文件路径：** `plugins/chatbot/utils/string_utils.py`

**模块职责：** 字符串处理工具类。提供模糊匹配、编辑距离计算、文本清洗等功能。

---

## 核心类与接口

### `StringUtils`（纯静态方法）

| 方法 | 说明 |
|------|------|
| `clean_text` | 清洗文本，仅保留中文、字母、数字和下划线 |
| `levenshtein_distance` | 计算莱文斯坦编辑距离（DP 算法） |
| `fuzzy_match` | 模糊匹配判断（包含匹配 → 清洗后匹配 → 编辑距离） |
| `contains_all_chars` | 检查是否包含目标字符串的所有字符（顺序不限） |

---

### `staticmethod def clean_text(text: str) -> str`

**参数说明：** `text` — 原始文本

**返回值：** 清洗后的文本（仅保留 `\w` 和中文字符）

**调用示例：**
```python
cleaned = StringUtils.clean_text("hello, 世界!")  # "hello世界"
```

---

### `staticmethod def levenshtein_distance(s1: str, s2: str) -> int`

**参数说明：**
- `s1` — 字符串 1
- `s2` — 字符串 2

**返回值：** 编辑距离（整数）

**调用示例：**
```python
dist = StringUtils.levenshtein_distance("kitten", "sitting")  # 3
```

---

### `staticmethod def fuzzy_match(text: str, keyword: str, threshold: int = 2) -> bool`

**参数说明：**
- `text` — 用户输入的文本
- `keyword` — 目标关键词
- `threshold` — 允许的最大编辑距离，默认 2

**返回值：** `True` 如果匹配成功

**调用示例：**
```python
match = StringUtils.fuzzy_match("hello world", "world")  # True
match = StringUtils.fuzzy_match("helo", "hello")          # True (编辑距离 1)
```

---

### `staticmethod def contains_all_chars(text: str, keyword: str) -> bool`

**参数说明：**
- `text` — 待检查文本
- `keyword` — 目标关键词

**返回值：** `True` 如果 `text` 包含 `keyword` 的所有字符

**调用示例：**
```python
result = StringUtils.contains_all_chars("abcde", "ace")  # True
```
