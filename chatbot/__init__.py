# src/plugins/chatbot/__init__.py
import shutil
import asyncio
import threading
from pathlib import Path

from nonebot import get_driver
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from loguru import logger as loguru_logger

from .config import Config, plugin_config, start_config_web_server
from .guardian import MemoryCircuitBreaker, EventLoopMonitor

# 导入所有事件响应器
from .matchers import (
    admin_hard,
    chat_entry,
    event_notice
)

__plugin_meta__ = PluginMetadata(
    name="Chatbot B",
    description="重构后的聊天机器人插件",
    usage="直接 @Bot 聊天，或发送指令",
    config=Config,
)

driver = get_driver()


# ==============================
# 统一日志接管：文件轮转 sink
# ==============================
@driver.on_startup
async def _setup_log_rotation():
    loguru_logger.add(
        "logs/chatbot.log",
        rotation="10 MB",
        retention=20,
        encoding="utf-8",
        enqueue=True,
    )
    logger.info("[Lifecycle] 日志轮转已启用: logs/chatbot.log (10MB/10天)")


# ==============================
# 原有：启动时清空临时下载目录
# ==============================
@driver.on_startup
async def clear_temp_directory():
    temp_dir = Path(plugin_config.jm_download_dir)
    if temp_dir.exists():
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"[Lifecycle] 已清理临时目录: {temp_dir}")
        except Exception as e:
            logger.warning(f"[Lifecycle] 清理临时目录失败: {e}")
    temp_dir.mkdir(parents=True, exist_ok=True)


# ==============================
# 熔断器全局对象
# ==============================
circuit_breaker: MemoryCircuitBreaker = None


async def _restart_memory_worker(mem_srv):
    """熔断器回调：重启记忆压缩 Worker"""
    await mem_srv.shutdown()
    await asyncio.sleep(0.5)
    await mem_srv.start_consumer()


@driver.on_startup
async def _boot_services():
    global circuit_breaker

    # 1. 初始化熔断器，注册 Worker 重启回调
    from .services import agent_srv
    circuit_breaker = MemoryCircuitBreaker(agent_srv.memory_service, plugin_config)
    circuit_breaker.on_worker_dead = _restart_memory_worker

    # 2. 启动熔断器监控循环
    asyncio.create_task(circuit_breaker.monitor_loop())

    # 3. 启动事件循环阻塞监控
    event_loop_monitor = EventLoopMonitor(drift_threshold=1.5)
    asyncio.create_task(event_loop_monitor.start_monitor())

    # 4. 启动记忆压缩消费者协程（仅高可用队列模式需要）
    if plugin_config.enable_task_queue:
        asyncio.create_task(agent_srv.memory_service.start_consumer())

    # 5. 启动 Web 配置面板（后台线程，daemon 随主进程退出）
    threading.Thread(
        target=start_config_web_server,
        args=(plugin_config, 8081),
        daemon=True,
    ).start()


@driver.on_shutdown
async def _shutdown_services():
    from .services import agent_srv

    # 1. 优雅关闭记忆压缩队列（drain → 等待 worker → 关闭 httpx）
    await agent_srv.memory_service.shutdown()

    # 2. 清理 Agent HTTP 连接池
    await agent_srv.close()
