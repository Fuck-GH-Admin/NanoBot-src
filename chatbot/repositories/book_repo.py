# src/plugins/chatbot/repositories/book_repo.py

import random
from pathlib import Path
from typing import List, Optional
from nonebot.log import logger

from ..config import plugin_config

class BookRepository:
    """
    书籍仓库
    职责：
    1. 管理本地文件路径（书架目录、输出目录）。
    2. 扫描和查找现有的书籍文件。
    3. 提供文件生成的标准路径（告诉 Service 文件该存哪）。
    """
    
    # 支持的书籍源格式
    SUPPORTED_EXTS = {
        ".pdf", ".epub", ".mobi", ".azw3", ".txt", ".docx",
        ".cbr", ".cbz", ".zip", ".rar", ".7z"
    }

    def __init__(self):
        self.books_dir = Path(plugin_config.books_folder)
        # 统一输出目录：books/output
        self.output_dir = self.books_dir / "output"
        
        self._ensure_dirs()

    def _ensure_dirs(self):
        if not self.books_dir.exists():
            logger.warning(f"[BookRepo] Books folder created: {self.books_dir}")
            self.books_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.output_dir.exists():
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def get_all_books(self) -> List[Path]:
        """获取书架目录下所有支持格式的文件"""
        if not self.books_dir.exists():
            return []
        files = []
        for p in self.books_dir.iterdir():
            if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXTS:
                files.append(p)
        return files

    def get_random_book(self) -> Optional[Path]:
        """随机获取一本书"""
        books = self.get_all_books()
        return random.choice(books) if books else None

    def find_book_by_id_or_name(self, keyword: str) -> Optional[Path]:
        """
        尝试在本地查找包含特定 ID 或关键词的书籍
        """
        books = self.get_all_books()
        keyword = str(keyword).lower()
        for b in books:
            # 匹配文件名 (包含ID或名称)
            if keyword in b.name.lower():
                return b
        return None

    # --- 路径生成逻辑 (统一由 Repo 管理) ---

    def get_pdf_output_path(self, source_file: Path) -> Path:
        """
        根据源文件，生成标准的 PDF 输出路径
        规则：/books/output/{文件名}.pdf
        """
        return self.output_dir / f"{source_file.stem}.pdf"

    def get_encrypted_output_path(self, source_file: Path) -> Path:
        """
        根据源文件，生成加密后的 PDF 输出路径
        规则：/books/output/{文件名}_enc.pdf
        """
        # 如果源文件已经是 output 里的文件，直接加后缀
        # 如果源文件是书架里的，也放到 output
        return self.output_dir / f"{source_file.stem}_enc.pdf"

    def get_zip_save_path(self, title: str) -> Path:
        """生成 ZIP 下载保存路径"""
        # 这里简单处理，Service 层可能需要根据下载逻辑微调，
        # 但原则上 Repo 应该知道文件存哪。
        clean_title = "".join(c for c in title if c not in '<>:"/\\|?*')
        return self.books_dir / f"{clean_title}.zip"