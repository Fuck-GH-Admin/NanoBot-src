"""
MemoryRepository — 基于 SQLAlchemy 2.0 (async) + aiosqlite 的结构化记忆存储

六张表：
- ChatHistory:       对话流水账（每条消息一行）
- GroupMemory:       群组宏观摘要（每 session 一行）
- UserTrait:         群友精细画像图谱（每条特征一行）
- CompactionJournal: 记忆压缩任务流水账（含重试状态 & 死信）
- Entity:            知识图谱实体节点
- Relation:          知识图谱实体间关系（三元组）

所有数据库操作均为异步。
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from sqlalchemy import select, update, delete, text, func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from nonebot.log import logger

from .models import Base, ChatHistory, GroupMemory, UserTrait, CompactionJournal, Entity, Relation, ToolExecutionLog, TopicThread


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class MemoryRepository:
    """
    结构化记忆仓库（异步 SQLAlchemy 版）

    使用方式：
        repo = MemoryRepository()
        await repo.init_db()            # 启动时调用一次，建表
        await repo.add_message(...)     # 写入消息
        msgs = await repo.get_recent_messages(...)  # 读取消息
    """

    _instance: Optional["MemoryRepository"] = None
    _engine: Optional[AsyncEngine] = None
    _session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    DEFAULT_DB_PATH = "data/chatbot_memory.db"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def init_db(self, db_url: Optional[str] = None) -> None:
        """
        异步创建数据库引擎和所有表结构。

        :param db_url: 数据库 URL，默认 sqlite+aiosqlite:///data/chatbot_memory.db
        """
        if db_url is None:
            db_path = os.path.abspath(self.DEFAULT_DB_PATH)
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            db_url = f"sqlite+aiosqlite:///{db_path}"

        self._engine = create_async_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            # ── 平滑迁移：SQLite 的 create_all 不会为已存在的表加新列 ──
            # 检查 chat_history 是否有 topic_id 列，没有则自动 ALTER TABLE
            result = await conn.execute(text("PRAGMA table_info(chat_history)"))
            columns = [row[1] for row in result.fetchall()]
            if "topic_id" not in columns:
                await conn.execute(text("ALTER TABLE chat_history ADD COLUMN topic_id VARCHAR(32)"))
                logger.info("[MemoryRepo] 迁移：chat_history 已添加 topic_id 列")

        logger.info(f"[MemoryRepo] Database initialized: {db_url}")

    def _get_session(self) -> AsyncSession:
        if self._session_factory is None:
            raise RuntimeError("MemoryRepository not initialized. Call init_db() first.")
        return self._session_factory()

    # ------------------------------------------------------------------
    # 对话流水账 (ChatHistory)
    # ------------------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        topic_id: Optional[str] = None,
        user_id: Optional[str] = None,
        name: Optional[str] = None,
        timestamp: Optional[str] = None,
        tool_calls: Optional[dict] = None,
        message_fingerprint: Optional[str] = None,
    ) -> Optional[int]:
        """
        插入单条历史记录（幂等）。

        若提供 message_fingerprint 且该 (session_id, fingerprint) 已存在，
        则跳过插入并返回 None（利用 UNIQUE 约束的 ON CONFLICT DO NOTHING）。

        :return: 新记录的自增 ID，或 None 表示重复跳过
        """
        async with self._get_session() as session:
            if message_fingerprint:
                stmt = (
                    insert(ChatHistory)
                    .values(
                        session_id=session_id,
                        topic_id=topic_id,
                        role=role,
                        content=content,
                        user_id=user_id,
                        name=name,
                        timestamp=timestamp or _utc_now_iso(),
                        is_summarized=False,
                        tool_calls=tool_calls,
                        message_fingerprint=message_fingerprint,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["session_id", "message_fingerprint"],
                    )
                )
                result = await session.execute(stmt)
                await session.commit()
                if result.rowcount == 0:
                    return None  # Duplicate fingerprint — skipped
                # Fetch the inserted row's ID
                stmt_id = (
                    select(ChatHistory.id)
                    .where(
                        ChatHistory.session_id == session_id,
                        ChatHistory.message_fingerprint == message_fingerprint,
                    )
                    .order_by(ChatHistory.id.desc())
                    .limit(1)
                )
                row = await session.execute(stmt_id)
                return row.scalar_one_or_none()
            else:
                # No fingerprint — legacy path, direct insert
                msg = ChatHistory(
                    session_id=session_id,
                    topic_id=topic_id,
                    role=role,
                    content=content,
                    user_id=user_id,
                    name=name,
                    timestamp=timestamp or _utc_now_iso(),
                    is_summarized=False,
                    tool_calls=tool_calls,
                )
                session.add(msg)
                await session.commit()
                await session.refresh(msg)
                return msg.id

    async def get_recent_messages(
        self, session_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        按时间倒序获取最近的 N 条消息（含已总结和未总结的）。

        :return: 消息字典列表，按 timestamp 正序排列（旧→新）
        """
        async with self._get_session() as session:
            stmt = (
                select(ChatHistory)
                .where(ChatHistory.session_id == session_id)
                .order_by(ChatHistory.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        # 反转为正序（旧→新），并转为字典
        rows.reverse()
        return [self._row_to_dict(r) for r in rows]

    async def get_messages_by_topic(
        self, topic_id: str
    ) -> List[Dict[str, Any]]:
        """提取属于特定蚂蚁洞的所有历史，彻底屏蔽外界噪音"""
        async with self._get_session() as session:
            stmt = (
                select(ChatHistory)
                .where(ChatHistory.topic_id == topic_id)
                .order_by(ChatHistory.id.asc())
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [self._row_to_dict(r) for r in rows]

    async def get_topic_id_by_message_id(self, platform_msg_id: str) -> Optional[str]:
        """根据平台的原始消息 ID 反查对应的 topic_id"""
        async with self._get_session() as session:
            stmt = select(ChatHistory.topic_id).where(
                ChatHistory.message_fingerprint == platform_msg_id
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # 话题线程管理 (TopicThread)
    # ------------------------------------------------------------------

    async def upsert_topic_thread(
        self,
        topic_id: str,
        session_id: str,
        status: str = "ACTIVE",
        participants: Optional[list] = None,
    ) -> None:
        """创建或更新话题线程"""
        async with self._get_session() as session:
            now = _utc_now_iso()
            existing = await session.execute(
                select(TopicThread).where(TopicThread.topic_id == topic_id)
            )
            row = existing.scalar_one_or_none()
            if row:
                row.status = status
                row.last_active_at = now
                if participants:
                    existing_set = set(row.participants or [])
                    row.participants = list(existing_set | set(participants))
            else:
                session.add(TopicThread(
                    topic_id=topic_id,
                    session_id=session_id,
                    status=status,
                    participants=participants or [],
                    created_at=now,
                    last_active_at=now,
                ))
            await session.commit()

    async def get_topic_thread(self, topic_id: str) -> Optional[Dict[str, Any]]:
        """获取单个话题线程"""
        async with self._get_session() as session:
            result = await session.execute(
                select(TopicThread).where(TopicThread.topic_id == topic_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "topic_id": row.topic_id,
                "session_id": row.session_id,
                "status": row.status,
                "summary": row.summary,
                "participants": row.participants or [],
                "created_at": row.created_at,
                "last_active_at": row.last_active_at,
            }

    async def update_topic_status(
        self, topic_id: str, status: str, summary: str = ""
    ) -> None:
        """更新话题状态（含可选摘要）"""
        async with self._get_session() as session:
            stmt = (
                update(TopicThread)
                .where(TopicThread.topic_id == topic_id)
                .values(status=status, summary=summary, last_active_at=_utc_now_iso())
            )
            await session.execute(stmt)
            await session.commit()

    async def get_suspended_topics(self, stale_seconds: int = 1800) -> List[Dict[str, Any]]:
        """获取所有 SUSPENDED 且超时的话题，用于归档"""
        cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        async with self._get_session() as session:
            stmt = select(TopicThread).where(
                TopicThread.status == "SUSPENDED",
                TopicThread.last_active_at < cutoff_iso,
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [
            {
                "topic_id": r.topic_id,
                "session_id": r.session_id,
                "last_active_at": r.last_active_at,
            }
            for r in rows
        ]

    async def get_unsummarized_messages(
        self, session_id: str
    ) -> List[Dict[str, Any]]:
        """
        获取所有 is_summarized == False 的消息（用于触发后台总结机制）。

        :return: 消息字典列表，按 timestamp 正序排列
        """
        async with self._get_session() as session:
            stmt = (
                select(ChatHistory)
                .where(
                    ChatHistory.session_id == session_id,
                    ChatHistory.is_summarized == False,  # noqa: E712
                )
                .order_by(ChatHistory.id.asc())
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [self._row_to_dict(r) for r in rows]

    async def mark_messages_summarized(self, message_ids: List[int]) -> int:
        """
        批量更新指定 ID 的消息状态为已总结。

        :return: 实际更新的行数
        """
        if not message_ids:
            return 0

        async with self._get_session() as session:
            stmt = (
                update(ChatHistory)
                .where(ChatHistory.id.in_(message_ids))
                .values(is_summarized=True)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # 群组宏观摘要 (GroupMemory)
    # ------------------------------------------------------------------

    async def upsert_group_summary(
        self, session_id: str, summary: str
    ) -> None:
        """
        插入或更新群组宏观摘要（session_id 为主键，冲突时覆盖 summary）。
        """
        async with self._get_session() as session:
            stmt = select(GroupMemory).where(GroupMemory.session_id == session_id)
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.summary = summary
                existing.updated_at = _utc_now_iso()
            else:
                session.add(GroupMemory(
                    session_id=session_id,
                    summary=summary,
                    updated_at=_utc_now_iso(),
                ))
            await session.commit()

    async def get_group_summary(self, session_id: str) -> str:
        """获取群组宏观摘要，不存在则返回空字符串。"""
        async with self._get_session() as session:
            stmt = select(GroupMemory.summary).where(
                GroupMemory.session_id == session_id
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row or ""

    # ------------------------------------------------------------------
    # 群友精细画像 (UserTrait)
    # ------------------------------------------------------------------

    async def upsert_user_traits(
        self,
        session_id: str,
        user_id: str,
        traits_list: List[Dict[str, Any]],
    ) -> int:
        """
        批量插入或更新 UserTrait 记录（基于 UNIQUE 约束的原子 upsert）。

        traits_list 中每个元素应包含：
        - content: str (特征内容)
        - confidence: float (置信度，可选，默认 0.5)
        - source_msg_id: int (关联消息 ID，可选)

        如果 session_id + user_id + content 已存在，则更新 confidence（取较高值）和 updated_at；
        否则插入新行。

        :return: 实际写入/更新的行数
        """
        if not traits_list:
            return 0

        now = _utc_now_iso()
        count = 0

        async with self._get_session() as session:
            for trait in traits_list:
                content = trait.get("content", "").strip()
                if not content:
                    continue

                confidence = float(trait.get("confidence", 0.5))
                source_msg_id = trait.get("source_msg_id")

                stmt = (
                    insert(UserTrait)
                    .values(
                        trait_id=_uuid_hex(),
                        session_id=session_id,
                        user_id=user_id,
                        content=content,
                        confidence=confidence,
                        source_msg_id=source_msg_id,
                        is_active=True,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["session_id", "user_id", "content"],
                        set_={
                            "confidence": func.max(UserTrait.confidence, confidence),
                            "updated_at": now,
                            "source_msg_id": source_msg_id or UserTrait.source_msg_id,
                        },
                    )
                )
                await session.execute(stmt)
                count += 1

            await session.commit()

        return count

    async def get_active_profiles(
        self, session_id: str, user_ids: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        传入一组 user_id，提取他们 is_active == True 的结构化特征列表。

        :return: { "user_id_1": [{"content": "...", "confidence": 0.9, "updated_at": "..."}], ... }
        """
        if not user_ids:
            return {}

        async with self._get_session() as session:
            stmt = select(UserTrait).where(
                UserTrait.session_id == session_id,
                UserTrait.user_id.in_(user_ids),
                UserTrait.is_active == True,  # noqa: E712
            ).order_by(UserTrait.confidence.desc())

            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        profiles: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            uid = row.user_id
            if uid not in profiles:
                profiles[uid] = []
            profiles[uid].append({
                "content": row.content,
                "confidence": row.confidence,
                "updated_at": row.updated_at,
            })

        return profiles

    async def deactivate_user_traits(
        self, session_id: str, user_id: str, trait_ids: Optional[List[str]] = None
    ) -> int:
        """
        逻辑删除用户特征。

        - 若提供 trait_ids，仅标记这些特征为 is_active=False。
        - 若不提供 trait_ids，标记该用户在该 session 下所有特征为 is_active=False。

        :return: 实际更新的行数
        """
        async with self._get_session() as session:
            conditions = [
                UserTrait.session_id == session_id,
                UserTrait.user_id == user_id,
            ]
            if trait_ids:
                conditions.append(UserTrait.trait_id.in_(trait_ids))

            stmt = (
                update(UserTrait)
                .where(*conditions)
                .values(is_active=False, updated_at=_utc_now_iso())
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # 压缩任务流水账 (CompactionJournal)
    # ------------------------------------------------------------------

    async def insert_compaction_journal(
        self, journal_id: str, session_id: str, max_retries: int = 3
    ) -> None:
        """插入一条新的压缩任务记录（status=pending）。"""
        async with self._get_session() as session:
            session.add(CompactionJournal(
                journal_id=journal_id,
                session_id=session_id,
                status="pending",
                retry_count=0,
                max_retries=max_retries,
                created_at=_utc_now_iso(),
                updated_at=_utc_now_iso(),
            ))
            await session.commit()

    async def update_compaction_journal(
        self,
        journal_id: str,
        *,
        status: Optional[str] = None,
        retry_count: Optional[int] = None,
        last_error: Optional[str] = None,
    ) -> None:
        """更新压缩任务的状态 / 重试次数 / 最后错误。"""
        async with self._get_session() as session:
            values: Dict[str, Any] = {"updated_at": _utc_now_iso()}
            if status is not None:
                values["status"] = status
            if retry_count is not None:
                values["retry_count"] = retry_count
            if last_error is not None:
                values["last_error"] = last_error

            stmt = (
                update(CompactionJournal)
                .where(CompactionJournal.journal_id == journal_id)
                .values(**values)
            )
            await session.execute(stmt)
            await session.commit()

    async def get_stale_journals(self, stale_seconds: int = 300) -> List[Dict[str, Any]]:
        """
        获取所有 status='running' 且 updated_at 超过 stale_seconds 秒的"僵尸"任务。
        这些任务可能因进程崩溃而未完成，需要重新入队。
        """
        cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

        async with self._get_session() as session:
            stmt = select(CompactionJournal).where(
                CompactionJournal.status == "running",
                CompactionJournal.updated_at < cutoff_iso,
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [
            {
                "journal_id": r.journal_id,
                "session_id": r.session_id,
                "retry_count": r.retry_count,
                "max_retries": r.max_retries,
            }
            for r in rows
        ]

    async def mark_journals_failed(self, journal_ids: List[str]) -> int:
        """批量将任务标记为 dead（死信），不再重试。"""
        if not journal_ids:
            return 0
        async with self._get_session() as session:
            stmt = (
                update(CompactionJournal)
                .where(CompactionJournal.journal_id.in_(journal_ids))
                .values(status="dead", updated_at=_utc_now_iso())
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # 知识图谱：实体 (Entity)
    # ------------------------------------------------------------------

    async def upsert_entities(self, entities: List[Dict[str, Any]]) -> int:
        """
        批量 upsert 实体（基于 entity_id 主键）。

        entities 中每个元素应包含：
        - entity_id: str
        - session_id: str
        - name: str
        - type: str
        - attributes: dict (可选)

        已存在则更新 name / type / attributes / updated_at；
        否则插入新行。

        :return: 实际写入/更新的行数
        """
        if not entities:
            return 0

        now = _utc_now_iso()
        count = 0

        async with self._get_session() as session:
            for ent in entities:
                eid = ent.get("entity_id", "").strip()
                if not eid:
                    continue

                stmt = (
                    insert(Entity)
                    .values(
                        entity_id=eid,
                        session_id=ent["session_id"],
                        name=ent.get("name", ""),
                        type=ent.get("type", ""),
                        attributes=ent.get("attributes") or {},
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["entity_id"],
                        set_={
                            "name": ent.get("name", Entity.name),
                            "type": ent.get("type", Entity.type),
                            "attributes": ent.get("attributes", Entity.attributes),
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(stmt)
                count += 1

            await session.commit()

        return count

    async def get_active_entities(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定 session 的所有实体，按 updated_at 降序。"""
        async with self._get_session() as session:
            stmt = (
                select(Entity)
                .where(Entity.session_id == session_id)
                .order_by(Entity.updated_at.desc())
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [
            {
                "entity_id": r.entity_id,
                "session_id": r.session_id,
                "name": r.name,
                "type": r.type,
                "attributes": r.attributes or {},
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 知识图谱：关系 (Relation)
    # ------------------------------------------------------------------

    async def upsert_relations(self, relations: List[Dict[str, Any]]) -> int:
        """
        批量 upsert 关系三元组（基于 uq_relation_triple 唯一约束）。

        relations 中每个元素应包含：
        - session_id: str
        - subject_entity: str (entity_id)
        - predicate: str
        - object_entity: str (entity_id)
        - confidence: float (可选，默认 0.5)
        - evidence_msg_ids: list (可选)

        冲突时更新 confidence（取较高值）、合并 evidence_msg_ids、刷新 updated_at。

        :return: 实际写入/更新的行数
        """
        if not relations:
            return 0

        now = _utc_now_iso()
        count = 0

        async with self._get_session() as session:
            for rel in relations:
                sid = rel.get("session_id", "")
                subj = rel.get("subject_entity", "")
                pred = rel.get("predicate", "")
                obj = rel.get("object_entity", "")
                if not (sid and subj and pred and obj):
                    continue

                confidence = float(rel.get("confidence", 0.5))
                evidence = rel.get("evidence_msg_ids") or []

                # 先查询是否已存在（用于合并 evidence_msg_ids）
                existing_stmt = select(Relation).where(
                    Relation.session_id == sid,
                    Relation.subject_entity == subj,
                    Relation.predicate == pred,
                    Relation.object_entity == obj,
                )
                existing_result = await session.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                if existing:
                    # 合并 evidence_msg_ids（去重）
                    old_evidence = existing.evidence_msg_ids or []
                    merged_evidence = list(set(old_evidence + evidence))
                    new_confidence = max(existing.confidence, confidence)

                    update_stmt = (
                        update(Relation)
                        .where(Relation.relation_id == existing.relation_id)
                        .values(
                            confidence=new_confidence,
                            evidence_msg_ids=merged_evidence,
                            updated_at=now,
                        )
                    )
                    await session.execute(update_stmt)
                else:
                    session.add(Relation(
                        relation_id=_uuid_hex(),
                        session_id=sid,
                        subject_entity=subj,
                        predicate=pred,
                        object_entity=obj,
                        confidence=confidence,
                        evidence_msg_ids=evidence,
                        updated_at=now,
                    ))

                count += 1

            await session.commit()

        return count

    async def get_relations(
        self, session_id: str, entity_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        返回指定 session 的所有关系，或仅涉及给定 entity_ids 的关系。

        :return: 关系字典列表
        """
        async with self._get_session() as session:
            conditions = [Relation.session_id == session_id]
            if entity_ids:
                conditions.append(
                    (Relation.subject_entity.in_(entity_ids)) |
                    (Relation.object_entity.in_(entity_ids))
                )
            stmt = select(Relation).where(*conditions).order_by(Relation.updated_at.desc())
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        return [
            {
                "relation_id": r.relation_id,
                "session_id": r.session_id,
                "subject_entity": r.subject_entity,
                "predicate": r.predicate,
                "object_entity": r.object_entity,
                "confidence": r.confidence,
                "evidence_msg_ids": r.evidence_msg_ids or [],
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def get_relations_with_decay(
        self,
        session_id: str,
        entity_ids: Optional[List[str]] = None,
        half_life_days: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """
        带时间衰减的关系查询。

        衰减公式：effective_confidence = confidence * 0.5 ^ (age_days / half_life_days)
        低于 0.15 的关系自动过滤。

        返回字典列表（非 ORM 对象），包含 decayed_confidence 字段。
        """
        relations = await self.get_relations(session_id, entity_ids)
        now = datetime.now(timezone.utc)

        filtered = []
        for r in relations:
            decayed = r["confidence"]
            try:
                updated = datetime.fromisoformat(r["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_days = (now - updated).total_seconds() / 86400
                decayed = round(r["confidence"] * (0.5 ** (age_days / half_life_days)), 3)
            except Exception:
                pass

            if decayed >= 0.15:
                r["decayed_confidence"] = decayed
                filtered.append(r)

        return filtered

    # ------------------------------------------------------------------
    # 记忆快照聚合
    # ------------------------------------------------------------------

    async def get_memory_snapshot(self, session_id: str) -> Dict[str, Any]:
        """
        从数据库聚合 summary、profiles、entities、relations（含衰减），
        返回符合 MemorySnapshot schema 的字典。
        """
        from ..schemas import MemorySnapshot

        # 1. 群组摘要
        summary = await self.get_group_summary(session_id)

        # 2. 群友画像
        recent_msgs = await self.get_recent_messages(session_id, limit=200)
        all_user_ids = list({m.get("user_id") for m in recent_msgs if m.get("user_id")})
        profiles_raw = await self.get_active_profiles(session_id, all_user_ids)
        profiles = [
            {
                "user_id": uid,
                "traits": [
                    {"content": t["content"], "confidence": t.get("confidence", 0.5)}
                    for t in traits
                ],
            }
            for uid, traits in profiles_raw.items()
        ]

        # 3. 实体
        entities_raw = await self.get_active_entities(session_id)
        entities = [
            {
                "entity_id": e["entity_id"],
                "name": e["name"],
                "type": e["type"],
                "attributes": e["attributes"],
            }
            for e in entities_raw
        ]

        # 4. 关系（含衰减）
        relations_raw = await self.get_relations_with_decay(session_id)
        relations = [
            {
                "relation_id": r["relation_id"],
                "subject_entity": r["subject_entity"],
                "predicate": r["predicate"],
                "object_entity": r["object_entity"],
                "confidence": r["decayed_confidence"],
            }
            for r in relations_raw
        ]

        snapshot = MemorySnapshot(
            summary=summary,
            profiles=profiles,
            entities=entities,
            relations=relations,
        )
        return snapshot.model_dump()

    # ------------------------------------------------------------------
    # 兼容接口（供 agent_service.py 过渡期使用）
    # ------------------------------------------------------------------

    async def load_memory(self, session_id: str) -> Dict[str, Any]:
        """
        兼容旧接口：返回 { "history": [...], "profile": { "summary": ..., "user_profiles": ... } }

        内部从三张表聚合数据，供 agent_service.py 过渡期调用。
        """
        # 1. 历史消息
        history_rows = await self.get_recent_messages(session_id, limit=200)
        history = []
        for row in history_rows:
            msg: Dict[str, Any] = {
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            if row["user_id"]:
                msg["user_id"] = row["user_id"]
            if row["name"]:
                msg["name"] = row["name"]
            if row["tool_calls"]:
                msg["tool_calls"] = row["tool_calls"]
            history.append(msg)

        # 2. 群组摘要
        summary = await self.get_group_summary(session_id)

        # 3. 群友画像（聚合为 { user_id: "特征1; 特征2" } 格式）
        if history:
            all_user_ids = list({m["user_id"] for m in history if m.get("user_id")})
        else:
            all_user_ids = []
        profiles_raw = await self.get_active_profiles(session_id, all_user_ids)
        user_profiles = {
            uid: "; ".join(t["content"] for t in traits)
            for uid, traits in profiles_raw.items()
        }

        return {
            "history": history,
            "profile": {
                "summary": summary,
                "user_profiles": user_profiles,
            },
        }

    async def save_memory(
        self, session_id: str, history: List[dict], profile: dict
    ) -> bool:
        """
        兼容旧接口：将 history 和 profile 写入数据库。

        内部逐条写入 ChatHistory（依赖 fingerprint 去重），更新 GroupMemory 和 UserTrait。
        供 agent_service.py 过渡期调用。
        """
        try:
            # 1. 写入历史消息（逐条 add_message，依赖 fingerprint 幂等去重）
            for msg in history:
                fp = msg.get("message_fingerprint")
                await self.add_message(
                    session_id=session_id,
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    user_id=msg.get("user_id"),
                    name=msg.get("name"),
                    timestamp=msg.get("timestamp"),
                    tool_calls=msg.get("tool_calls"),
                    message_fingerprint=fp,
                )

            # 2. 更新群组摘要
            summary = profile.get("summary", "")
            if summary:
                await self.upsert_group_summary(session_id, summary)

            # 3. 更新群友画像
            user_profiles = profile.get("user_profiles") or {}
            for uid, traits_str in user_profiles.items():
                if traits_str and isinstance(traits_str, str):
                    traits_list = [
                        {"content": t.strip(), "confidence": 0.5}
                        for t in traits_str.split(";")
                        if t.strip()
                    ]
                    await self.upsert_user_traits(session_id, uid, traits_list)

            return True
        except Exception as e:
            logger.error(f"[MemoryRepo] save_memory failed for {session_id}: {e}")
            return False

    async def clear_history(self, session_id: str) -> bool:
        """仅清空历史记录，保留画像和摘要。"""
        try:
            async with self._get_session() as session:
                stmt = delete(ChatHistory).where(
                    ChatHistory.session_id == session_id
                )
                await session.execute(stmt)
                await session.commit()
            return True
        except Exception as e:
            logger.error(f"[MemoryRepo] clear_history failed for {session_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: ChatHistory) -> Dict[str, Any]:
        """将 ChatHistory ORM 对象转为字典。"""
        d: Dict[str, Any] = {
            "id": row.id,
            "session_id": row.session_id,
            "role": row.role,
            "content": row.content,
            "timestamp": row.timestamp,
            "is_summarized": row.is_summarized,
        }
        if row.user_id:
            d["user_id"] = row.user_id
        if row.name:
            d["name"] = row.name
        if row.tool_calls:
            d["tool_calls"] = row.tool_calls
        return d

    async def _message_exists(
        self, session_id: str, timestamp: str, role: str, content: str
    ) -> bool:
        """简单去重：检查相同 (session_id, timestamp, role, content) 的消息是否已存在。"""
        async with self._get_session() as session:
            stmt = select(ChatHistory.id).where(
                ChatHistory.session_id == session_id,
                ChatHistory.timestamp == timestamp,
                ChatHistory.role == role,
                ChatHistory.content == content,
            ).limit(1)
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # 工具执行审计日志 (ToolExecutionLog)
    # ------------------------------------------------------------------

    async def insert_tool_log(
        self,
        session_id: str,
        request_id: str,
        step: int,
        trigger: str,
        tool_name: str,
        arguments: dict | None = None,
        result_summary: str = "",
        error: str | None = None,
    ) -> None:
        """记录一次工具执行到审计日志表。"""
        try:
            async with self._get_session() as session:
                session.add(ToolExecutionLog(
                    session_id=session_id,
                    request_id=request_id,
                    step=step,
                    trigger=trigger,
                    tool_name=tool_name,
                    arguments=arguments,
                    result_summary=result_summary[:2000],
                    error=error[:2000] if error else None,
                ))
                await session.commit()
        except Exception as e:
            logger.error(f"[MemoryRepo] 写入工具日志失败: {e}")

    async def get_recent_tool_logs(
        self,
        session_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """查询最近的突变日志。"""
        async with self._get_session() as session:
            stmt = (
                select(ToolExecutionLog)
                .where(ToolExecutionLog.session_id == session_id)
                .order_by(ToolExecutionLog.id.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "session_id": r.session_id,
                    "request_id": r.request_id,
                    "step": r.step,
                    "trigger": r.trigger,
                    "tool_name": r.tool_name,
                    "arguments": r.arguments,
                    "result_summary": r.result_summary,
                    "error": r.error,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
