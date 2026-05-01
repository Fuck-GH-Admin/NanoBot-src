"""
SQLAlchemy 2.0 异步声明式模型

六张核心表：
- ChatHistory:       对话流水账（每条消息一行）
- GroupMemory:       群组宏观摘要（每 session 一行）
- UserTrait:         群友精细画像图谱（每条特征一行）
- CompactionJournal: 记忆压缩任务流水账（含重试状态 & 死信）
- Entity:            知识图谱实体节点
- Relation:          知识图谱实体间关系（三元组）
"""

from datetime import datetime, timezone
from sqlalchemy import String, Integer, Text, Float, Boolean, JSON, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatHistory(Base):
    """对话流水账：每条消息独立一行，支持溯源与增量总结"""
    __tablename__ = "chat_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user/assistant/system/tool
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timestamp: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)
    is_summarized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tool_calls: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_chat_history_session_ts", "session_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<ChatHistory id={self.id} session={self.session_id} role={self.role}>"


class GroupMemory(Base):
    """群组宏观摘要：每个 session 一行，upsert 更新"""
    __tablename__ = "group_memory"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)

    def __repr__(self) -> str:
        return f"<GroupMemory session={self.session_id}>"


class UserTrait(Base):
    """群友精细画像图谱：每条特征独立一行，支持置信度和溯源"""
    __tablename__ = "user_trait"

    trait_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # UUID hex
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(512), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    source_msg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)

    __table_args__ = (
        Index("ix_user_trait_session_user", "session_id", "user_id"),
        UniqueConstraint("session_id", "user_id", "content", name="uq_trait_key"),
    )

    def __repr__(self) -> str:
        return f"<UserTrait id={self.trait_id} user={self.user_id} active={self.is_active}>"


class CompactionJournal(Base):
    """记忆压缩任务流水账：记录每次压缩任务的状态，支持重试和死信追踪"""
    __tablename__ = "compaction_journal"

    journal_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # UUID hex
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/running/success/failed/dead
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)

    __table_args__ = (
        Index("ix_compaction_journal_session", "session_id"),
        Index("ix_compaction_journal_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<CompactionJournal id={self.journal_id} session={self.session_id} status={self.status}>"


class Entity(Base):
    """知识图谱实体节点"""
    __tablename__ = "entity"

    entity_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # UUID hex
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)

    __table_args__ = (
        Index("ix_entity_session", "session_id"),
    )

    def __repr__(self) -> str:
        return f"<Entity id={self.entity_id} name={self.name} type={self.type}>"


class Relation(Base):
    """知识图谱实体间关系（三元组）"""
    __tablename__ = "relation"

    relation_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # UUID hex
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_entity: Mapped[str] = mapped_column(String(32), nullable=False)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    object_entity: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence_msg_ids: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=_utc_now_iso)

    __table_args__ = (
        Index("ix_relation_triple", "session_id", "subject_entity", "object_entity"),
        UniqueConstraint("session_id", "subject_entity", "predicate", "object_entity",
                         name="uq_relation_triple"),
    )

    def __repr__(self) -> str:
        return f"<Relation id={self.relation_id} {self.subject_entity}--{self.predicate}--{self.object_entity}>"
