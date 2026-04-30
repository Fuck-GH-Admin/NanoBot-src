# src/plugins/chatbot/services/__init__.py

from .agent_service import AgentService
from .image_service import ImageService
from .drawing_service import DrawingService
from .book_service import BookService
from .permission_service import PermissionService

agent_srv = AgentService()
img_srv = ImageService()
draw_srv = DrawingService()
book_srv = BookService()
perm_srv = PermissionService()

__all__ = ["agent_srv", "img_srv", "draw_srv", "book_srv", "perm_srv"]