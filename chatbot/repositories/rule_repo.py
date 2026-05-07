"""
RuleRepository — 动态暗号规则仓库（异步 SQLAlchemy 版）

复用 MemoryRepository 的引擎和会话工厂（同一数据库文件）。
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update, delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from nonebot.log import logger

from .models import Base, CustomRule, RuleChangelog
from .memory_repo import MemoryRepository


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_hex() -> str:
    return uuid.uuid4().hex


class RuleRepository:
    """
    动态规则仓库（异步 SQLAlchemy 版）

    复用 MemoryRepository 的引擎，所有表在同一数据库中。
    """

    _instance: Optional["RuleRepository"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        """创建规则相关表结构（复用 MemoryRepository 引擎）。"""
        mem_repo = MemoryRepository()
        if mem_repo._engine is None:
            raise RuntimeError("MemoryRepository engine not initialized. Call MemoryRepository().init_db() first.")
        async with mem_repo._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("[RuleRepo] Tables ensured: custom_rule, rule_changelog")

    def _get_session(self) -> AsyncSession:
        mem_repo = MemoryRepository()
        if mem_repo._session_factory is None:
            raise RuntimeError("MemoryRepository not initialized. Call init_db() first.")
        return mem_repo._session_factory()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_rule(self, rule_data: dict) -> dict:
        """插入新规则，同时写入审计日志。返回创建的规则 dict。"""
        now = _utc_now_iso()
        rule_id = rule_data.get("rule_id") or _uuid_hex()

        row = CustomRule(
            rule_id=rule_id,
            scope_type=rule_data["scope_type"],
            scope_id=rule_data.get("scope_id", ""),
            keywords_hash=rule_data["keywords_hash"],
            keywords=rule_data["keywords"],
            tool_name=rule_data["tool_name"],
            args_extractor=rule_data["args_extractor"],
            pattern_id=rule_data.get("pattern_id"),
            description=rule_data.get("description"),
            examples=rule_data.get("examples"),
            hit_count=rule_data.get("hit_count", 0),
            last_hit=rule_data.get("last_hit"),
            created_at=rule_data.get("created_at", now),
            created_by=rule_data["created_by"],
            updated_at=now,
            ttl_days=rule_data.get("ttl_days", 30),
            active=rule_data.get("active", 1),
            priority=rule_data.get("priority", 0),
            confidence=rule_data.get("confidence", 1.0),
            allow_forced_exec=rule_data.get("allow_forced_exec", 1),
        )

        async with self._get_session() as session:
            async with session.begin():
                session.add(row)
                session.add(RuleChangelog(
                    timestamp=now,
                    action="create",
                    rule_id=rule_id,
                    operator=rule_data["created_by"],
                    scope_type=rule_data["scope_type"],
                    scope_id=rule_data.get("scope_id", ""),
                    old_value=None,
                    new_value=rule_data,
                ))

        logger.debug(f"[RuleRepo] Created rule {rule_id}")
        return self._row_to_dict(row)

    async def update_rule(self, rule_id: str, new_data: dict) -> Optional[dict]:
        """更新规则字段，同时写入审计日志。返回更新后的规则 dict，未找到返回 None。"""
        async with self._get_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(CustomRule).where(CustomRule.rule_id == rule_id)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None

                old_value = self._row_to_dict(row)

                for key, value in new_data.items():
                    if hasattr(row, key) and key != "rule_id":
                        setattr(row, key, value)
                row.updated_at = _utc_now_iso()

                new_value = self._row_to_dict(row)

                session.add(RuleChangelog(
                    timestamp=_utc_now_iso(),
                    action="update",
                    rule_id=rule_id,
                    operator=new_data.get("operator", "system"),
                    scope_type=row.scope_type,
                    scope_id=row.scope_id,
                    old_value=old_value,
                    new_value=new_value,
                ))

        logger.debug(f"[RuleRepo] Updated rule {rule_id}")
        return new_value

    async def delete_rule(self, rule_id: str, operator: str = "system") -> bool:
        """软删除规则（设置 active=0），同时写入审计日志。返回是否成功。"""
        async with self._get_session() as session:
            async with session.begin():
                result = await session.execute(
                    select(CustomRule).where(CustomRule.rule_id == rule_id)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return False

                old_value = self._row_to_dict(row)
                row.active = 0
                row.updated_at = _utc_now_iso()
                new_value = self._row_to_dict(row)

                session.add(RuleChangelog(
                    timestamp=_utc_now_iso(),
                    action="delete",
                    rule_id=rule_id,
                    operator=operator,
                    scope_type=row.scope_type,
                    scope_id=row.scope_id,
                    old_value=old_value,
                    new_value=new_value,
                ))

        logger.debug(f"[RuleRepo] Soft-deleted rule {rule_id}")
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def get_active_rules(self, scope_type: str, scope_id: str) -> list[dict]:
        """按作用域查询活动规则。"""
        async with self._get_session() as session:
            result = await session.execute(
                select(CustomRule).where(
                    CustomRule.scope_type == scope_type,
                    CustomRule.scope_id == scope_id,
                    CustomRule.active == 1,
                )
            )
            rows = result.scalars().all()
            return [self._row_to_dict(r) for r in rows]

    async def find_by_hash(self, scope_type: str, scope_id: str, keywords_hash: str) -> Optional[dict]:
        """冲突检测：按 scope + keywords_hash 查找已有规则。"""
        async with self._get_session() as session:
            result = await session.execute(
                select(CustomRule).where(
                    CustomRule.scope_type == scope_type,
                    CustomRule.scope_id == scope_id,
                    CustomRule.keywords_hash == keywords_hash,
                )
            )
            row = result.scalar_one_or_none()
            return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # 原子递增
    # ------------------------------------------------------------------

    async def increment_hit_count(self, rule_id: str) -> None:
        """原子递增命中计数，绝不读后写。"""
        async with self._get_session() as session:
            async with session.begin():
                await session.execute(
                    update(CustomRule)
                    .where(CustomRule.rule_id == rule_id)
                    .values(hit_count=CustomRule.hit_count + 1, last_hit=_utc_now_iso())
                )

    # ------------------------------------------------------------------
    # 分批 TTL 清理
    # ------------------------------------------------------------------

    async def cleanup_stale_rules(self, batch_size: int = 100) -> int:
        """
        分批清理过期规则。返回总删除行数。

        条件：
          active = 1 AND (
            (last_hit IS NOT NULL AND datetime(last_hit, '+' || ttl_days || ' days') < datetime('now'))
            OR
            (last_hit IS NULL AND datetime(created_at, '+' || ttl_days || ' days') < datetime('now'))
          )
        """
        total_deleted = 0
        sql = text(
            "DELETE FROM custom_rule WHERE rowid IN ("
            "  SELECT rowid FROM custom_rule WHERE active = 1 AND ("
            "    (last_hit IS NOT NULL AND datetime(last_hit, '+' || ttl_days || ' days') < datetime('now'))"
            "    OR"
            "    (last_hit IS NULL AND datetime(created_at, '+' || ttl_days || ' days') < datetime('now'))"
            "  ) LIMIT :batch_size"
            ")"
        )
        async with self._get_session() as session:
            async with session.begin():
                while True:
                    result = await session.execute(sql, {"batch_size": batch_size})
                    deleted = result.rowcount
                    total_deleted += deleted
                    if deleted < batch_size:
                        break

        logger.info(f"[RuleRepo] TTL cleanup: removed {total_deleted} stale rules")
        return total_deleted

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: CustomRule) -> dict:
        return {
            "rule_id": row.rule_id,
            "scope_type": row.scope_type,
            "scope_id": row.scope_id,
            "keywords_hash": row.keywords_hash,
            "keywords": row.keywords,
            "tool_name": row.tool_name,
            "args_extractor": row.args_extractor,
            "pattern_id": row.pattern_id,
            "description": row.description,
            "examples": row.examples,
            "hit_count": row.hit_count,
            "last_hit": row.last_hit,
            "created_at": row.created_at,
            "created_by": row.created_by,
            "updated_at": row.updated_at,
            "ttl_days": row.ttl_days,
            "active": row.active,
            "priority": row.priority,
            "confidence": row.confidence,
            "allow_forced_exec": row.allow_forced_exec,
        }
