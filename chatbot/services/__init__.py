# src/plugins/chatbot/services/__init__.py

from typing import Optional

from .agent_service import AgentService
from .image_service import ImageService
from .drawing_service import DrawingService
from .book_service import BookService
from .permission_service import PermissionService

_agent_srv: Optional[AgentService] = None
_img_srv: Optional[ImageService] = None
_draw_srv: Optional[DrawingService] = None
_book_srv: Optional[BookService] = None
_perm_srv: Optional[PermissionService] = None


def get_agent_srv() -> AgentService:
    global _agent_srv
    if _agent_srv is None:
        _agent_srv = AgentService()
    return _agent_srv


def get_img_srv() -> ImageService:
    global _img_srv
    if _img_srv is None:
        _img_srv = ImageService()
    return _img_srv


def get_draw_srv() -> DrawingService:
    global _draw_srv
    if _draw_srv is None:
        _draw_srv = DrawingService()
    return _draw_srv


def get_book_srv() -> BookService:
    global _book_srv
    if _book_srv is None:
        _book_srv = BookService()
    return _book_srv


def get_perm_srv() -> PermissionService:
    global _perm_srv
    if _perm_srv is None:
        _perm_srv = PermissionService()
    return _perm_srv


# 保持向后兼容的模块级别名（import 时仍可 from ..services import agent_srv）
agent_srv = get_agent_srv()
img_srv = get_img_srv()
draw_srv = get_draw_srv()
book_srv = get_book_srv()
perm_srv = get_perm_srv()

__all__ = ["get_agent_srv", "get_img_srv", "get_draw_srv", "get_book_srv", "get_perm_srv",
           "agent_srv", "img_srv", "draw_srv", "book_srv", "perm_srv"]
