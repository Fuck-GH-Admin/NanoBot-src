# src/plugins/chatbot/services/memory_service.py
#
# 长期记忆沉淀中枢：仅保留 Topic Harvester（话题归档时触发 LLM 提炼）。
# 旧的 15 条消息触发压缩机制已移除（双重抽税问题）。

import asyncio
import json
import time
from pathlib import Path

import httpx
from nonebot.log import logger

from ..config import plugin_config
from ..utils.path_utils import get_project_root, DRAFT_WORLDBOOK_PATH
from ..repositories.memory_repo import MemoryRepository


# ─────────────────── 压缩机制已废弃 ───────────────────
# 旧的 15 条消息触发压缩（GroupMemory/UserTrait/Entity/Relation）已移除。
# 唯一的长期记忆沉淀中枢是 Topic Harvester（话题归档时触发）。
# MemoryService 类保留骨架以兼容 __init__.py 中的启动/关闭代码。

class MemoryService:
    """记忆服务骨架（压缩逻辑已移除，仅保留生命周期接口供 __init__.py 调用）。"""

    def __init__(self):
        self.repo = MemoryRepository()
        self.http_client = httpx.AsyncClient(timeout=90.0)
        # 保留 _queue 以兼容 __init__.py 中 circuit_breaker 对 qsize() 的访问
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False
        self._worker_task: asyncio.Task | None = None

    async def start_consumer(self) -> None:
        """保留接口（no-op）。压缩机制已废弃，无消费者需要启动。"""
        self._running = True
        logger.info("[MemoryService] 压缩机制已废弃，消费者接口保留但不启动 worker")

    async def shutdown(self) -> None:
        """优雅关闭 httpx 连接池。"""
        self._running = False
        await self.http_client.aclose()
        logger.info("[MemoryService] 已关闭")

    async def close(self):
        """兼容旧接口，委托给 shutdown。"""
        await self.shutdown()


# ─────────────────── 话题收割机 (Topic Harvester) ───────────────────

# 话题状态机常量
TOPIC_ACTIVE_TIMEOUT = 600       # ACTIVE → SUSPENDED：10 分钟无活动
TOPIC_ARCHIVE_TIMEOUT = 1800     # SUSPENDED → ARCHIVED：30 分钟无活动
HARVESTER_SCAN_INTERVAL = 60     # 扫描间隔（秒）

# 归档限流：同时最多 2 个 LLM 摘要任务
_ARCHIVE_SEMAPHORE = asyncio.Semaphore(2)
# 归档冷却（秒）：每个任务完成后强制等待，防止 API 速率限制
_ARCHIVE_COOLDOWN = 2.0


async def _generate_topic_summary(topic_id: str, messages: list) -> str:
    """调用 LLM 为话题生成归档摘要"""
    api_key = plugin_config.deepseek_api_key
    api_url = plugin_config.deepseek_api_url
    model = plugin_config.deepseek_memory_model_name

    if not api_key or not messages:
        return ""

    # 构建摘要请求：只取关键消息，控制 token 用量
    condensed = []
    for msg in messages[-50:]:  # 最多取最后 50 条
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            condensed.append(f"[{role}] {content[:300]}")

    prompt_text = "\n".join(condensed)

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个摘要生成器。请用简洁的中文总结以下对话的核心内容、关键结论和参与者。不超过 200 字。"},
            {"role": "user", "content": prompt_text},
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 300,
        "thinking": {"type": "disabled"},
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                api_url,
                json=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                if choices:
                    summary = choices[0].get("message", {}).get("content", "")
                    logger.info(f"[Harvester] 话题 {topic_id[:8]} 摘要生成成功 ({len(summary)} 字符)")
                    return summary
            else:
                logger.warning(f"[Harvester] 话题 {topic_id[:8]} 摘要 API 错误: {resp.status_code}")
    except Exception as e:
        logger.warning(f"[Harvester] 话题 {topic_id[:8]} 摘要生成失败: {e}")

    return ""


def _collect_existing_keys(session_id: str) -> set[str]:
    """从 worldbook.json + draft_worldbook.json 中收集当前作用域（含 global）的所有已知关键词。"""
    base = get_project_root(Path(__file__)) / "config"
    keys: set[str] = set()
    for fname in ("worldbook.json", "draft_worldbook.json"):
        fpath = base / fname
        if not fpath.exists():
            continue
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            entries = data.get("entries", []) if isinstance(data, dict) else data
            for e in entries:
                scope = e.get("custom_scope", "global")
                if scope == "global" or scope == session_id:
                    for k in (e.get("key") or e.get("keys") or []):
                        if isinstance(k, str) and len(k) >= 2:
                            keys.add(k)
        except Exception as e:
            logger.warning(f"[Harvester] 读取 {fname} 提取已知关键词失败: {e}")
    return keys


async def _extract_lore_from_topic(topic_id: str, messages: list, session_id: str = "") -> list:
    """
    从话题对话中提炼客观设定（世界观、人物特征等）。
    返回符合 SillyTavern 世界书格式的条目列表，无新设定则返回空列表。
    """
    api_key = plugin_config.deepseek_api_key
    api_url = plugin_config.deepseek_api_url
    model = plugin_config.deepseek_memory_model_name

    if not api_key or not messages:
        logger.info(f"[Harvester] 话题 {topic_id[:8]} 跳过提炼: api_key={'有' if api_key else '无'}, messages={len(messages)}")
        return []

    condensed = []
    for msg in messages[-50:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            condensed.append(f"[{role}] {content[:500]}")
    prompt_text = "\n".join(condensed)

    # 收集已知关键词，注入提示词实现源头去重
    existing_keys = _collect_existing_keys(session_id) if session_id else set()
    existing_keys_block = ""
    if existing_keys:
        keys_str = "、".join(sorted(existing_keys))
        existing_keys_block = (
            f"\n\n【已知设定关键词（本群已收录）】\n{keys_str}\n"
        )

    logger.info(
        f"[Harvester] 话题 {topic_id[:8]} 开始提炼: "
        f"{len(messages)} 条消息, {len(condensed)} 条有效, "
        f"prompt 约 {len(prompt_text)} 字符, "
        f"已知关键词 {len(existing_keys)} 个"
    )

    system_prompt = (
        "你是一个从群聊对话中提炼持久化设定的助手。\n"
        "请从以下对话中提取值得长期记住的设定信息。\n\n"
        "【值得提取的内容】\n"
        "- 世界观设定：地名、组织、势力、规则体系、历史背景\n"
        "- 人物设定：角色名、身份、能力、外貌、性格特征、所属阵营\n"
        "- 专有名词：特殊术语、道具、技能名及其含义\n"
        "- 关系网络：角色间的固定关系（师徒、敌对、队友等）\n"
        "- 重要事件：有长期影响的事件（非日常琐事）\n\n"
        "【不需要提取的】\n"
        "- 纯粹的打招呼、表情包、无意义回复\n"
        "- 明显的一次性闲聊（如\"今天好困\"）\n\n"
        "【宁可多提，不要漏提】\n"
        "如果不确定是否算设定，倾向于提取。宁可让管理员审核时丢弃，也不要遗漏重要设定。\n"
        f"{existing_keys_block}\n"
        "【防重复约束 — 极度严格】\n"
        "- **绝对禁止提取已知设定**：如果对话中提到的实体属于上述已知关键词，"
        "且没有提供革命性的、打破常规的新细节，直接忽略，不允许生成条目。\n"
        "- **补充优于新建**：如果对话为已知设定提供了重要补充，请提取一个包含原有核心关键词的新条目，"
        "并在 content 中着重描写【补充细节】。\n"
        "- **精准去重**：本次提取的多个 entries 内部，绝对不能出现描述同一实体的多条设定，"
        "必须自行在输出前合并。\n\n"
        "【输出格式】\n"
        "你必须输出一个 JSON 对象，包含 `entries` 键，值为数组。每个元素格式：\n"
        '{"entries": [{"key": ["关键词1", "关键词2"], "content": "设定描述", "constant": false}]}\n'
        '如果没有值得提取的内容，输出：{"entries": []}'
    )

    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 1000,
        "thinking": {"type": "disabled"},
    }

    raw = ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                api_url,
                json=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[Harvester] 话题 {topic_id[:8]} API 错误: "
                    f"status={resp.status_code}, body={resp.text[:200]}"
                )
                return []

            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                logger.warning(f"[Harvester] 话题 {topic_id[:8]} API 返回空 choices")
                return []

            raw = choices[0].get("message", {}).get("content", "")
            raw = raw.strip()
            logger.info(f"[Harvester] 话题 {topic_id[:8]} LLM 原始输出 ({len(raw)} 字符): {raw[:300]}")

            parsed = json.loads(raw)
            entries = parsed.get("entries", [])
            # 容错：模型偶尔返回单个对象而非数组
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                logger.warning(f"[Harvester] 话题 {topic_id[:8]} entries 非列表: {type(entries)}")
                return []

            # 字段映射 + 验证
            valid = []
            for i, e in enumerate(entries):
                if not isinstance(e, dict):
                    continue
                keys = e.get("key") or e.get("keys") or []
                if isinstance(keys, str):
                    keys = [k.strip() for k in keys.split(",") if k.strip()]
                content = str(e.get("content", "")).strip()

                if not content:
                    continue
                if not keys:
                    keys = [content[:20]]

                valid.append({
                    "key": keys if isinstance(keys, list) else [str(keys)],
                    "content": content,
                    "constant": bool(e.get("constant", False)),
                })

            logger.info(
                f"[Harvester] 话题 {topic_id[:8]} 提炼完成: "
                f"LLM 返回 {len(entries)} 条, 有效 {len(valid)} 条"
            )
            return valid

    except json.JSONDecodeError as e:
        logger.error(
            f"[Harvester] 话题 {topic_id[:8]} JSON 解析失败: {e}\n"
            f"原始输出: {raw[:500] if raw else 'N/A'}"
        )
    except Exception as e:
        logger.error(f"[Harvester] 话题 {topic_id[:8]} 提炼异常: {e}", exc_info=True)

    return []


async def _archive_topic(topic_id: str, session_id: str, repo) -> None:
    """安全归档单个话题（受信号量限流），并行提炼设定 + 写草稿 + 通知管理员"""
    async with _ARCHIVE_SEMAPHORE:
        try:
            # 1. 拉取话题全量消息
            messages = await repo.get_messages_by_topic(topic_id)
            if not messages:
                await repo.update_topic_status(topic_id, "ARCHIVED", summary="(空话题)")
                logger.info(f"[Harvester] 空话题 {topic_id[:8]} 已直接归档")
                return

            # 2. 并行：摘要 + 设定提炼
            summary_result, lore_entries = await asyncio.gather(
                _generate_topic_summary(topic_id, messages),
                _extract_lore_from_topic(topic_id, messages, session_id),
                return_exceptions=True,
            )

            # 3. 处理摘要（异常降级）
            if isinstance(summary_result, Exception):
                logger.warning(f"[Harvester] 话题 {topic_id[:8]} 摘要异常: {summary_result}")
                summary = f"(摘要生成失败，话题包含 {len(messages)} 条消息)"
            else:
                summary = summary_result or f"(摘要生成失败，话题包含 {len(messages)} 条消息)"

            # 4. 处理设定提炼（异常静默吞掉，绝不阻塞归档）
            if isinstance(lore_entries, Exception):
                logger.error(f"[Harvester] 话题 {topic_id[:8]} 设定提炼异常: {lore_entries}", exc_info=lore_entries)
            elif lore_entries:
                logger.info(f"[Harvester] 话题 {topic_id[:8]} 提炼出 {len(lore_entries)} 条设定: {[e.get('key',[]) for e in lore_entries]}")
            else:
                logger.info(f"[Harvester] 话题 {topic_id[:8]} 未提炼出设定（空结果）")

            if lore_entries and not isinstance(lore_entries, Exception):
                # 写入草稿箱
                from .world_book import DraftWorldBook
                draft_wb = DraftWorldBook(str(DRAFT_WORLDBOOK_PATH))
                for entry in lore_entries:
                    entry["custom_scope"] = session_id
                    await draft_wb.append_entry(entry)

                # 通知管理员（带重试，Bot 实例可能尚未就绪）
                keys_str = ", ".join(k for e in lore_entries for k in e.get("key", []))
                content_preview = lore_entries[0].get("content", "")[:80]
                notify_msg = (
                    f"[Worldbook Auto-Lore]\n"
                    f"已从 {session_id} 的对话中提炼出 {len(lore_entries)} 条新设定草稿：\n"
                    f"关键词：{keys_str}\n"
                    f"内容：{content_preview}...\n"
                    f"请前往 Web 控制台「世界书」标签审核。"
                )
                superusers = list(plugin_config.superusers)
                if not superusers:
                    logger.warning("[Harvester] 无 superusers 配置，跳过通知")
                else:
                    bot = None
                    for attempt in range(3):
                        try:
                            from nonebot import get_bot
                            bot = get_bot()
                            break
                        except Exception as e:
                            logger.warning(f"[Harvester] 获取 Bot 实例失败 (attempt {attempt+1}/3): {e}")
                            await asyncio.sleep(5)
                    if bot:
                        for uid in superusers:
                            try:
                                await bot.send_private_msg(user_id=int(uid), message=notify_msg)
                                logger.info(f"[Harvester] 已通知管理员 {uid}")
                            except Exception as e:
                                logger.warning(f"[Harvester] 通知管理员 {uid} 失败: {e}")
                    else:
                        logger.error("[Harvester] 3 次获取 Bot 实例均失败，管理员通知未发送")

            # 5. 更新话题状态
            await repo.update_topic_status(topic_id, "ARCHIVED", summary=summary)
            logger.info(f"[Harvester] 话题 {topic_id[:8]} 已归档，摘要: {summary[:60]}...")

        except Exception as e:
            logger.error(f"[Harvester] 话题 {topic_id[:8]} 归档失败: {e}")
        finally:
            await asyncio.sleep(_ARCHIVE_COOLDOWN)


async def topic_harvester_daemon(repo) -> None:
    """
    话题收割机守护进程。

    - 每 60 秒扫描一次 ACTIVE 话题池
    - 将超过 10 分钟无活动的 ACTIVE 话题移入 SUSPENDED
    - 将超过 30 分钟的 SUSPENDED 话题归档（调用 LLM 生成摘要）
    """
    from .topic_router import ACTIVE_TOPICS_POOL

    logger.info("[Harvester] 话题收割机守护进程已启动")
    await asyncio.sleep(30)  # 启动后等待 30 秒，让系统先稳定

    while True:
        try:
            await asyncio.sleep(HARVESTER_SCAN_INTERVAL)
            now = time.time()

            suspended_this_round = 0
            archived_count = 0

            # ── 阶段 1：ACTIVE → SUSPENDED（内存池扫描）──
            for session_id, pool in list(ACTIVE_TOPICS_POOL.items()):
                expired = []
                for topic in pool:
                    if now - topic.last_active > TOPIC_ACTIVE_TIMEOUT:
                        expired.append(topic)

                for topic in expired:
                    # 从内存池移除
                    pool.remove(topic)
                    # 更新数据库状态
                    await repo.upsert_topic_thread(
                        topic_id=topic.topic_id,
                        session_id=session_id,
                        status="SUSPENDED",
                    )
                    suspended_this_round += 1
                    logger.info(f"[Harvester] 话题 {topic.topic_id[:8]} 已挂起 (session={session_id})")

                # 清理空池
                if not pool:
                    del ACTIVE_TOPICS_POOL[session_id]

            # ── 阶段 2：SUSPENDED → ARCHIVED（数据库扫描）──
            suspended_topics = await repo.get_suspended_topics(stale_seconds=TOPIC_ARCHIVE_TIMEOUT)

            if suspended_topics:
                archive_tasks = []
                for topic_info in suspended_topics:
                    tid = topic_info["topic_id"]
                    sid = topic_info["session_id"]
                    archive_tasks.append(
                        asyncio.create_task(_archive_topic(tid, sid, repo))
                    )

                # 批量等待本轮归档任务
                if archive_tasks:
                    await asyncio.gather(*archive_tasks, return_exceptions=True)
                    archived_count = len(archive_tasks)
                    logger.info(f"[Harvester] 本轮归档完成，共处理 {archived_count} 个话题")

            # ── 本轮扫描汇总 ──
            active_pool_count = sum(len(p) for p in ACTIVE_TOPICS_POOL.values())
            logger.info(
                f"[Harvester] 本轮扫描汇总: 活跃话题池={active_pool_count}, "
                f"本次挂起={suspended_this_round}, 归档={archived_count}"
            )

        except Exception as e:
            logger.error(f"[Harvester] 收割机异常: {e}")
            await asyncio.sleep(10)  # 异常后短暂等待再重试
