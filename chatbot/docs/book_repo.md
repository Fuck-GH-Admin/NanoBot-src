# BookRepository

**文件路径：** `plugins/chatbot/repositories/book_repo.py`

**模块职责：** 书籍仓库。管理本地文件路径（书架目录、输出目录），扫描和查找现有书籍文件，提供文件生成的标准路径。

---

## 核心类与接口

### `BookRepository`

| 方法 | 说明 |
|------|------|
| `get_all_books` | 获取书架目录下所有支持格式的文件 |
| `get_random_book` | 随机获取一本书 |
| `find_book_by_id_or_name` | 根据 ID 或名称关键词查找书籍 |
| `get_pdf_output_path` | 生成标准 PDF 输出路径 |
| `get_encrypted_output_path` | 生成加密 PDF 输出路径 |
| `get_zip_save_path` | 生成 ZIP 下载保存路径 |

---

### `def get_all_books() -> List[Path]`

**返回值：** 支持格式的文件路径列表（`.pdf`, `.epub`, `.mobi`, `.azw3`, `.txt`, `.docx`, `.cbr`, `.cbz`, `.zip`, `.rar`, `.7z`）

**调用示例：**
```python
repo = BookRepository()
books = repo.get_all_books()
for b in books:
    print(b.name)
```

---

### `def get_random_book() -> Optional[Path]`

**返回值：** 随机一个文件路径，书架为空则返回 `None`

**调用示例：**
```python
book = repo.get_random_book()
if book:
    print(book)
```

---

### `def find_book_by_id_or_name(keyword: str) -> Optional[Path]`

**参数说明：** `keyword` — 文件名中包含的 ID 或关键词

**返回值：** 匹配的文件路径，未找到返回 `None`

**调用示例：**
```python
path = repo.find_book_by_id_or_name("350234")
```

---

### `def get_pdf_output_path(source_file: Path) -> Path`

**参数说明：** `source_file` — 源文件路径

**返回值：** `output_dir / {源文件名}.pdf`

**调用示例：**
```python
out = repo.get_pdf_output_path(Path("books/foo.zip"))
```

---

### `def get_encrypted_output_path(source_file: Path) -> Path`

**参数说明：** `source_file` — 源文件路径

**返回值：** `output_dir / {源文件名}_enc.pdf`

---

### `def get_zip_save_path(title: str) -> Path`

**参数说明：** `title` — 书籍标题

**返回值：** `books_dir / {清洗后的标题}.zip`
