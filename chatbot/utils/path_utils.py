# src/plugins/chatbot/utils/path_utils.py
"""
项目根目录动态查找工具。
替代所有硬编码的 .parent 链，兼容任意嵌套深度和操作系统。
"""

from pathlib import Path


def get_project_root(start_path: Path | None = None) -> Path:
    """
    从 start_path 向上搜索，返回第一个包含 config/ 子目录的祖先目录。
    用于定位项目根目录（即 config/ 所在层级）。

    用法：
        from .utils.path_utils import get_project_root
        root = get_project_root(Path(__file__))
        config_dir = root / "config"
    """
    if start_path is None:
        start_path = Path(__file__).resolve()

    p = start_path.resolve() if start_path.is_file() else start_path
    for _ in range(10):
        if (p / "config").is_dir():
            return p
        parent = p.parent
        if parent == p:
            break
        p = parent

    # fallback: 绝不可能到达（除非项目结构被破坏），但保持安全
    return start_path.resolve().parent.parent.parent.parent


# 预计算常用路径（模块级缓存，避免重复遍历）
_PROJECT_ROOT = get_project_root(Path(__file__))
CONFIG_DIR = _PROJECT_ROOT / "config"
WORLDBOOK_PATH = CONFIG_DIR / "worldbook.json"
DRAFT_WORLDBOOK_PATH = CONFIG_DIR / "draft_worldbook.json"
CONFIG_FILE_PATH = CONFIG_DIR / "config_bot_base.yaml"
CHARACTER_PATH = CONFIG_DIR / "character.json"
