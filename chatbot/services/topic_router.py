# src/plugins/chatbot/services/topic_router.py

import asyncio
import json
import math
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import httpx
from nonebot.log import logger

from ..config import plugin_config


# ─────────────────── 社交熵硬闸 (Social Entropy Hard Gate) ───────────────────

@dataclass
class SocialWindow:
    """滑动窗口：追踪每个群最近 W 秒内的发言者，判断是否处于高频热聊。"""
    events: deque = field(default_factory=deque)  # deque[(timestamp, sender_id)]
    window_size: float = 60.0  # 窗口大小（秒）

    def add_event(self, sender_id: str) -> None:
        now = time.time()
        self.events.append((now, sender_id))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_size
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def unique_senders(self) -> int:
        now = time.time()
        self._prune(now)
        return len(set(sender for _, sender in self.events))

    def interaction_density(self) -> float:
        now = time.time()
        self._prune(now)
        if not self.events:
            return 0.0
        return len(self.events) / self.window_size


# 全局实例：按 group_id 隔离
_social_windows: dict[int, SocialWindow] = {}

# 硬闸触发阈值：≥2 个不同发言者 且 密度 > 0.1 msg/s（即 60s 内 > 6 条）
_HARD_GATE_MIN_SENDERS = 2
_HARD_GATE_MIN_DENSITY = 0.1


def is_hot_conversation(group_id: int) -> bool:
    """检测指定群是否处于高频交互对状态（硬闸触发条件）。"""
    window = _social_windows.get(group_id)
    if not window:
        return False
    return (
        window.unique_senders() >= _HARD_GATE_MIN_SENDERS
        and window.interaction_density() > _HARD_GATE_MIN_DENSITY
    )


def record_social_event(group_id: int, sender_id: str) -> None:
    """记录一条社交事件到滑动窗口。"""
    if group_id not in _social_windows:
        _social_windows[group_id] = SocialWindow()
    _social_windows[group_id].add_event(sender_id)


@dataclass
class EntityPool:
    """每个 Topic 维护一个实体词池，仅保留强特征实体，激进衰减。"""
    entities: dict[str, float] = field(default_factory=dict)  # 强特征实体词 -> 权重
    last_updated: float = field(default_factory=time.time)

    # 严禁加入实体词池的通用扮演称谓
    PROHIBITED_GENERIC_TERMS: ClassVar[set[str]] = {
        "姐姐", "妹妹", "哥哥", "弟弟", "妈妈", "爸爸", "主人", "指挥官",
        "大人", "前辈", "老师", "长官", "兄弟", "朋友",
    }

    def add(self, word: str) -> None:
        """仅当词不在禁止列表、长度>=2、非纯数字时才加入。"""
        if (
            word not in self.PROHIBITED_GENERIC_TERMS
            and len(word) >= 2
            and not word.isdigit()
        ):
            self.entities[word] = self.entities.get(word, 0.0) + 1.0
            self.last_updated = time.time()

    def decay(self, half_life: float = 120.0) -> None:
        """激进指数衰减：半衰期默认 2 分钟，保证只在紧凑连贯对话中生效。"""
        now = time.time()
        elapsed = now - self.last_updated
        if elapsed <= 0:
            return
        factor = 0.5 ** (elapsed / half_life)
        self.entities = {k: v * factor for k, v in self.entities.items() if v * factor > 0.01}
        self.last_updated = now

    def score_intersection(self, candidate_entities: set[str]) -> float:
        """返回候选实体集与本池的交集加权分。先执行衰减。"""
        self.decay()
        return sum(self.entities.get(e, 0.0) for e in candidate_entities if e in self.entities)


@dataclass
class ActiveTopic:
    topic_id: str
    centroid: list[float]
    last_active: float
    participants: set[str] = field(default_factory=set)
    entity_pool: EntityPool = field(default_factory=EntityPool)


# 内存特征池：极度轻量，单个群大约占用 10KB
# 结构: { session_id: [ ActiveTopic1, ActiveTopic2... ] }
ACTIVE_TOPICS_POOL: dict[str, list[ActiveTopic]] = {}

# 池大小上限
MAX_POOL_SIZE = 10

# 时间衰减常数（秒）：10 分钟
TIME_DECAY_TAU = 600.0

# Embedding API 超时
EMBEDDING_TIMEOUT = 2.0

# 噪音消息判定（与 chat_entry 复用）
_MIN_SUBSTANTIVE_LEN = 2


def cosine_sim(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _is_low_entropy(text: str) -> bool:
    """极短文本或纯标点/表情，不做向量计算"""
    _NOISE_PATTERN = re.compile(
        r'^[\s\U00010000-\U0010ffff'
        r' -⁯⸀-⹿'
        r'　-〿＀-￯'
        r'!?!。，、；：""''…—\-_.…|/\\()（）\[\]【】{}<>《》@#$%^&*+=~`\'"]+$'
    )
    stripped = _NOISE_PATTERN.sub('', text).strip()
    if len(stripped) < _MIN_SUBSTANTIVE_LEN:
        return True
    # Short repeated chars like "哈哈", "嗯嗯", "呵呵" are noise
    if len(stripped) <= 4 and len(set(stripped)) <= 2:
        return True
    return False


# ─────────────────── 实体词提取 (Hybrid Routing L2) ───────────────────

# 预编译正则：动作标记（全角/半角括号内 2-20 字符）
_RE_ACTION_FULL = re.compile(r'（(.{2,20})）')
_RE_ACTION_HALF = re.compile(r'\((.{2,20})\)')

# 预编译正则：长数字/ID（5 位及以上连续数字）
_RE_LONG_NUMBER = re.compile(r'\d{5,}')

# 通用扮演称谓（与 EntityPool.PROHIBITED_GENERIC_TERMS 同源）
_GENERIC_TERMS = EntityPool.PROHIBITED_GENERIC_TERMS

from ..utils.path_utils import WORLDBOOK_PATH as _WORLDBOOK_PATH, DRAFT_WORLDBOOK_PATH as _DRAFT_WORLDBOOK_PATH

# 动态强实体缓存
_strong_entities_cache: set[str] = set()
_strong_entities_cache_ts: float = 0.0
_STRONG_ENTITIES_CACHE_TTL = 60.0  # 60 秒刷新一次


def _load_worldbook_keys(path: Path) -> set[str]:
    """从单个世界书 JSON 文件中提取所有 key 关键词。"""
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        for entry in entries:
            entry_keys = entry.get("key") or entry.get("keys") or []
            if isinstance(entry_keys, str):
                entry_keys = [k.strip() for k in entry_keys.split(",") if k.strip()]
            for k in entry_keys:
                k = str(k).strip()
                if k and len(k) >= 2:
                    keys.add(k)
    except Exception as e:
        logger.warning(f"[TopicRouter] 读取世界书失败 {path.name}: {e}")
    return keys


def get_dynamic_strong_entities() -> set[str]:
    """从 worldbook.json + draft_worldbook.json 动态提取强实体关键词集合，带 60s TTL 缓存。"""
    global _strong_entities_cache, _strong_entities_cache_ts
    now = time.time()
    if now - _strong_entities_cache_ts < _STRONG_ENTITIES_CACHE_TTL:
        return _strong_entities_cache
    _strong_entities_cache = _load_worldbook_keys(_WORLDBOOK_PATH) | _load_worldbook_keys(_DRAFT_WORLDBOOK_PATH)
    _strong_entities_cache_ts = now
    logger.debug(f"[TopicRouter] 强实体词典已刷新: {len(_strong_entities_cache)} 个关键词")
    return _strong_entities_cache


def invalidate_strong_entities_cache() -> None:
    """热更新接口：外部调用可立即失效缓存，下次请求时重新加载。"""
    global _strong_entities_cache_ts
    _strong_entities_cache_ts = 0.0


def extract_routing_features(text: str) -> tuple[set[str], set[str]]:
    """
    轻量级本地实体词提取，纯字符串操作，< 1ms（缓存命中时）。

    返回: (strong_entities, generic_terms)
      - strong_entities: 强特征实体词集合（用于 Topic 路由匹配）
      - generic_terms:   通用扮演称谓集合（用于 SOFT_SUSPEND 精准唤醒）
    """
    strong: set[str] = set()

    # 1. 动作标记：全角括号
    for m in _RE_ACTION_FULL.finditer(text):
        strong.add(m.group(1).strip())
    # 动作标记：半角括号
    for m in _RE_ACTION_HALF.finditer(text):
        strong.add(m.group(1).strip())

    # 2. 长数字/ID
    for m in _RE_LONG_NUMBER.finditer(text):
        strong.add(m.group(0))

    # 3. 动态强名词词典子串匹配（来源：worldbook.json + draft_worldbook.json）
    for word in get_dynamic_strong_entities():
        if word in text:
            strong.add(word)

    # 4. 通用扮演称谓检测
    generic: set[str] = set()
    for term in _GENERIC_TERMS:
        if term in text:
            generic.add(term)

    return strong, generic


class TopicRouter:
    def __init__(self):
        self.http_client = httpx.AsyncClient()
        self._embedding_cache: dict[str, list[float]] = {}

    async def close(self):
        await self.http_client.aclose()

    async def get_embedding(self, text: str) -> list[float] | None:
        """调用硅基流动获取句向量，带简单缓存"""
        if not text.strip():
            return None

        # 短文本缓存（避免重复请求）
        cache_key = text[:100]
        if cache_key in self._embedding_cache:
            return self._embedding_cache[cache_key]

        try:
            resp = await self.http_client.post(
                f"{plugin_config.siliconflow_api_url}/embeddings",
                headers={"Authorization": f"Bearer {plugin_config.siliconflow_api_key}"},
                json={"model": plugin_config.embedding_model_name, "input": text},
                timeout=EMBEDDING_TIMEOUT,
            )
            if resp.status_code == 200:
                vec = resp.json()["data"][0]["embedding"]
                self._embedding_cache[cache_key] = vec
                # 防止缓存无限增长
                if len(self._embedding_cache) > 200:
                    # 清除一半旧缓存
                    keys = list(self._embedding_cache.keys())
                    for k in keys[:100]:
                        del self._embedding_cache[k]
                return vec
            else:
                logger.warning(f"[TopicRouter] Embedding API 返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"[TopicRouter] Embedding API 失联: {e}")

        return None

    async def resolve_topic(
        self,
        session_id: str,
        text: str,
        user_id: str,
        reply_msg_topic_id: str | None = None,
        group_id: int = 0,
        is_explicit_trigger: bool = False,
    ) -> tuple[str, bool]:
        """
        混合路由分配（Hybrid Routing）。

        层级 1: 物理逻辑门（Reply 链 / @Bot / 唤醒词 → 强制继承原 Topic）
        层级 2: 实体词力场检测（强特征实体匹配 Topic 实体词池）
        层级 3: 语义向量匹配（Embedding API，作为降级兜底）
        层级 4: 新建 Topic

        返回: (topic_id, is_new_topic)
        """
        pool = ACTIVE_TOPICS_POOL.setdefault(session_id, [])
        now = time.time()

        # ── 层级 1: 物理强连通 (O(1)) ──
        # 显式触发（@Bot / 唤醒词 / Reply Bot）→ 强制继承
        if is_explicit_trigger and pool:
            pool.sort(key=lambda x: x.last_active, reverse=True)
            latest = pool[0]
            latest.last_active = now
            latest.participants.add(user_id)
            logger.info(f"[TopicRouter] L1 显式触发继承: topic_id={latest.topic_id}")
            return latest.topic_id, False

        # Reply 链继承
        if reply_msg_topic_id:
            for topic in pool:
                if topic.topic_id == reply_msg_topic_id:
                    topic.last_active = now
                    topic.participants.add(user_id)
                    logger.info(f"[TopicRouter] L1 Reply链命中: topic_id={topic.topic_id}")
                    return topic.topic_id, False

        # ── 层级 1.5: 低熵收容所 ──
        if _is_low_entropy(text):
            if pool:
                pool.sort(key=lambda x: x.last_active, reverse=True)
                latest = pool[0]
                latest.last_active = now
                latest.participants.add(user_id)
                logger.info(f"[TopicRouter] L1.5 低熵搭便车: topic_id={latest.topic_id}")
                return latest.topic_id, False
            new_id = uuid.uuid4().hex
            logger.info(f"[TopicRouter] L1.5 低熵无池，新建: topic_id={new_id}")
            return new_id, True

        # ── 层级 2: 实体词力场检测 (O(N), 毫秒级) ──
        strong_entities, generic_terms = extract_routing_features(text)

        if strong_entities and pool:
            best_topic = None
            best_score = 0.0

            for topic in pool:
                score = topic.entity_pool.score_intersection(strong_entities)
                if score > best_score:
                    best_score = score
                    best_topic = topic

            if best_topic and best_score > 0:
                # 吸附：更新实体词池 + 刷新活跃时间
                for word in strong_entities:
                    best_topic.entity_pool.add(word)
                best_topic.last_active = now
                best_topic.participants.add(user_id)
                logger.info(
                    f"[TopicRouter] L2 实体词命中: topic_id={best_topic.topic_id}, "
                    f"score={best_score:.2f}, entities={strong_entities}"
                )
                return best_topic.topic_id, False

        # ── 层级 3: 语义向量匹配 (网络 I/O，降级兜底) ──
        vec = await self.get_embedding(text)

        if vec and pool:
            best_topic = None
            best_score = -1.0

            for topic in pool:
                time_decay = math.exp(-(now - topic.last_active) / TIME_DECAY_TAU)
                sim = cosine_sim(vec, topic.centroid)
                score = sim * time_decay

                if score > best_score:
                    best_score = score
                    best_topic = topic

            logger.info(
                f"[TopicRouter] L3 语义匹配: best_topic_id={best_topic.topic_id if best_topic else None}, "
                f"score={best_score:.4f}, threshold={plugin_config.similarity_threshold}"
            )
            if best_score > plugin_config.similarity_threshold and best_topic is not None:
                # EMA 更新话题中心 + 归一化
                merged = [
                    old * plugin_config.ema_old_weight + new * plugin_config.ema_new_weight
                    for old, new in zip(best_topic.centroid, vec)
                ]
                best_topic.centroid = _normalize(merged)
                best_topic.last_active = now
                best_topic.participants.add(user_id)
                # 同时更新实体词池
                for word in (strong_entities or set()):
                    best_topic.entity_pool.add(word)
                return best_topic.topic_id, False

        # ── 层级 4: 新建 Topic ──
        new_topic_id = uuid.uuid4().hex
        logger.info(f"[TopicRouter] L4 新建话题: topic_id={new_topic_id}, session_id={session_id}")
        if vec:
            pool.append(ActiveTopic(new_topic_id, vec, now, {user_id}))
            # 更新新话题的实体词池
            if strong_entities:
                for word in strong_entities:
                    pool[-1].entity_pool.add(word)
            # 控制内存池大小
            if len(pool) > MAX_POOL_SIZE:
                pool.sort(key=lambda x: x.last_active, reverse=True)
                ACTIVE_TOPICS_POOL[session_id] = pool[:MAX_POOL_SIZE]
        elif strong_entities:
            # 无向量但有实体词：也创建 Topic（用零向量占位）
            pool.append(ActiveTopic(new_topic_id, [0.0] * 1024, now, {user_id}))
            for word in strong_entities:
                pool[-1].entity_pool.add(word)
            if len(pool) > MAX_POOL_SIZE:
                pool.sort(key=lambda x: x.last_active, reverse=True)
                ACTIVE_TOPICS_POOL[session_id] = pool[:MAX_POOL_SIZE]

        return new_topic_id, True


topic_router = TopicRouter()
