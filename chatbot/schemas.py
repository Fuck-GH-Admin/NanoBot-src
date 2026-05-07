"""
跨端统一数据契约（Python 端唯一真源）
所有 Python↔Node 通信负载均经此类校验再发送/解析。
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class Trait(BaseModel):
    """群友特征"""
    uid: Optional[str] = None
    content: str
    confidence: float = Field(ge=0.0, le=1.0)
    updated_at: str = ""


class UserProfile(BaseModel):
    """群友画像"""
    user_id: str
    traits: List[Trait] = []


class Entity(BaseModel):
    """实体节点"""
    entity_id: str
    name: str
    type: str
    attributes: Dict[str, Any] = {}


class Relation(BaseModel):
    """实体间关系"""
    relation_id: str
    subject_entity: str
    predicate: str
    object_entity: str
    confidence: float = Field(ge=0.0, le=1.0)


class MemorySnapshot(BaseModel):
    """记忆快照"""
    summary: str = ""
    profiles: List[UserProfile] = []
    entities: List[Entity] = []
    relations: List[Relation] = []


class ChatRequestPayload(BaseModel):
    """发往 Node 的完整请求体"""
    chatHistory: List[Dict[str, Any]]
    memorySnapshot: MemorySnapshot
    tools: List[Dict[str, Any]] = []
    context: Dict[str, Any] = {}


class CommandResult(BaseModel):
    """管理指令统一返回结构"""
    success: bool
    message: str = ""
    data: Optional[Dict[str, Any]] = None

    @classmethod
    def ok(cls, message: str = "", **kwargs) -> "CommandResult":
        return cls(success=True, message=message, data=kwargs or None)

    @classmethod
    def fail(cls, message: str = "", **kwargs) -> "CommandResult":
        return cls(success=False, message=message, data=kwargs or None)
