import sys
from pathlib import Path

# 将 src/ 目录加入 sys.path，使 'plugins.chatbot.schemas' 可被导入
_src_dir = str(Path(__file__).resolve().parent.parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
