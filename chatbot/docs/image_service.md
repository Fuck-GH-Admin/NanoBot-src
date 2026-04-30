# ImageService

**文件路径：** `plugins/chatbot/services/image_service.py`

**模块职责：** 图片检索与抗风控处理服务。负责解析用户指令、从仓库查询图片、多图选取以及生成抗平台审查的图片副本。

---

## 核心类与接口

### `ImageService`

| 方法 | 说明 |
|------|------|
| `get_image` | 获取单张图片（含搜索逻辑） |
| `get_multi_images` | 解析多图指令（如"图1-3"）并返回多张图片 |
| `generate_stealth` | 生成抗风控副本（像素微调 / 噪点 / 旋转 / 重编码） |

---

### `async def get_image(text: str, allow_r18: bool = False) -> Tuple[Optional[str], str]`

**参数说明：**
- `text` — 用户输入的文本，包含关键词和分类触发词
- `allow_r18` — 是否允许 R18 内容，默认为 `False`

**返回值：**
- 成功：`(图片文件路径, 图片信息文本)`
- 失败：`(None, 错误提示信息)`

**调用示例：**
```python
service = ImageService()
path, info = await service.get_image("来张图 风景 不要ai", allow_r18=False)
if path:
    print(f"图片路径: {path}, 信息: {info}")
```

---

### `async def get_multi_images(text: str, allow_r18: bool) -> Tuple[List[str], str]`

**参数说明：**
- `text` — 用户输入，支持 `图1-3` / `图1 图2` 语法
- `allow_r18` — 是否允许 R18 内容

**返回值：**
- 成功：`([图片路径列表], 汇总信息文本)`
- 无结果：`([], 错误提示)`

**调用示例：**
```python
paths, info = await service.get_multi_images("图1-3 风景", allow_r18=False)
for p in paths:
    print(p)
```

---

### `async def generate_stealth(original_path: str, strategy: int = 0) -> str`

**参数说明：**
- `original_path` — 原图路径
- `strategy` — 抗风控策略编号（0: 像素微调+元数据, 1: 稀疏噪点, 2: 微旋转, 3: JPEG→PNG 重编码）

**返回值：**
- 成功：处理后的图片路径
- 失败：返回原始路径（降级）

**调用示例：**
```python
stealth_path = await service.generate_stealth("/data/images/foo.png", strategy=1)
```
