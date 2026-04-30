# PDFUtils

**文件路径：** `plugins/chatbot/utils/pdf_utils.py`

**模块职责：** PDF 处理工具类。将 ZIP 中的图片经 Pillow 压缩后通过 img2pdf 合成为 PDF，支持元数据混淆。

---

## 核心类与接口

### `PDFUtils`（纯静态方法）

| 方法 | 说明 |
|------|------|
| `convert_zip_to_pdf` | ZIP → 图片解压 → Pillow 缩放/压缩 → img2pdf 合成 PDF |
| `modify_pdf_metadata` | 混淆 PDF 元数据（注入随机 UUID） |

---

### `staticmethod def convert_zip_to_pdf(zip_path: Union[str, Path], output_dir: Union[str, Path], compress_level: int = 80, max_width: int = 1920) -> str`

**参数说明：**
- `zip_path` — ZIP 压缩包路径
- `output_dir` — 输出目录
- `compress_level` — JPEG 压缩质量（1-100），默认 80
- `max_width` — 最大宽度，超过则等比缩小，`None` 表示不缩放

**返回值：**
- 成功：生成的 PDF 文件路径
- 失败：`""`

**调用示例：**
```python
pdf_path = PDFUtils.convert_zip_to_pdf("books/test.zip", "books/output", compress_level=75, max_width=1920)
```

---

### `staticmethod def modify_pdf_metadata(input_pdf: Union[str, Path], output_pdf: Union[str, Path]) -> bool`

**参数说明：**
- `input_pdf` — 输入 PDF 路径
- `output_pdf` — 输出 PDF 路径

**返回值：** 成功 `True` / 失败 `False`

**调用示例：**
```python
success = PDFUtils.modify_pdf_metadata("input.pdf", "output.pdf")
```
