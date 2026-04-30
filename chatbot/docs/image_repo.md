# ImageRepository

**文件路径：** `plugins/chatbot/repositories/image_repo.py`

**模块职责：** 图片元数据仓库。基于 Pandas 提供高效的 Excel 数据查询和文件路径管理。

---

## 核心类与接口

### `ImageRepository`

| 方法 | 说明 |
|------|------|
| `refresh` | 手动刷新 Excel 数据 |
| `query_images` | 多条件查询图片（关键词 OR 匹配 + 分类过滤 + AI 过滤） |
| `get_image_by_id` | 根据图片 UID 精确获取单张图片 |

---

### `def query_images(keywords: List[str], classification: Optional[str] = None, no_ai: bool = False, limit: int = 50) -> List[Dict[str, str]]`

**参数说明：**
- `keywords` — 关键词列表（OR 逻辑，匹配越多排序越前）
- `classification` — 分类筛选，可选 `"R18"`, `"R18G"`, `"Artist"`
- `no_ai` — 是否过滤 AI 作品
- `limit` — 最大返回数量

**返回值：**
```python
[
    {
        "path": str,   # 图片绝对路径
        "info": str,   # 图片信息文本（画师、ID、标题、标签、分类、是否AI）
        "uid": str,    # 图片文件 UID
        "tags": str    # 图片标签
    },
    ...
]
```
- 失败：`[]`

**调用示例：**
```python
repo = ImageRepository()
results = repo.query_images(["风景", "山"], classification="Artist", no_ai=True, limit=10)
for r in results:
    print(r["path"], r["info"])
```

---

### `def get_image_by_id(uid: str) -> Optional[Dict]`

**参数说明：** `uid` — 图片文件 UID

**返回值：**
```python
{
    "path": str,
    "info": str,
    "uid": str
}
```
- 未找到：`None`

**调用示例：**
```python
img = repo.get_image_by_id("ABC123")
if img:
    print(img["path"])
```
