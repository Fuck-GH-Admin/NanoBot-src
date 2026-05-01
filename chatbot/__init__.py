# src/plugins/chatbot/__init__.py
import os
import sys
import shutil
import asyncio
import threading
import subprocess

from nonebot import get_driver
from nonebot.log import logger
from nonebot.plugin import PluginMetadata

from .config import Config, plugin_config, start_config_web_server

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
# 新增：管理 Node.js 微服务
# ==============================
NODE_SERVER_DIR = Path(__file__).parent / "engine"
NODE_SERVER_SCRIPT = NODE_SERVER_DIR / "server.js"

node_process = None

async def start_node_service():
    """安装依赖并启动 Node.js 服务"""
    global node_process

    # 启动前校验必要配置
    if not plugin_config.node_deepseek_api_key:
        raise ValueError("node_deepseek_api_key 未配置，无法启动 Node.js 微服务")

    node_modules = NODE_SERVER_DIR / "node_modules"

    # 首次运行时自动安装 npm 依赖
    if not node_modules.exists():
        logger.info("正在安装 Node.js 依赖...")
        proc = await asyncio.create_subprocess_shell(
            "npm install",
            cwd=NODE_SERVER_DIR,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        if proc.returncode != 0:
            logger.error("Node 依赖安装失败，请手动运行 npm install")
            return
        logger.info("Node 依赖安装完成")

    logger.info("正在启动 Node.js 微服务...")
    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = plugin_config.node_deepseek_api_key
    env["DEEPSEEK_BASE_URL"] = plugin_config.node_base_url
    env["DEEPSEEK_MODEL"] = plugin_config.node_model
    env["LLM_TEMPERATURE"] = str(plugin_config.node_temperature)

    node_process = subprocess.Popen(
        ["node", str(NODE_SERVER_SCRIPT)],
        cwd=NODE_SERVER_DIR,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr
    )

    # 等待 1 秒检查进程是否存活
    await asyncio.sleep(1)
    if node_process.poll() is None:
        logger.info("Node.js 微服务启动成功")
    else:
        logger.error("Node.js 微服务启动失败，请检查 engine/server.js")

async def stop_node_service():
    """优雅终止 Node.js 进程"""
    global node_process
    if node_process and node_process.poll() is None:
        logger.info("正在关闭 Node.js 微服务...")
        node_process.terminate()
        try:
            node_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            node_process.kill()
        logger.info("Node.js 微服务已停止")

# ---------- Web 配置管理面板 ----------
@driver.on_startup
async def _boot_services():
    # 启动 Node.js 微服务
    asyncio.create_task(start_node_service())

    # 启动记忆压缩消费者协程（仅高可用队列模式需要）
    from .matchers.chat_entry import agent
    if plugin_config.enable_task_queue:
        asyncio.create_task(agent.memory_service.start_consumer())

    # 启动 Web 配置面板（后台线程，daemon 随主进程退出）
    threading.Thread(
        target=start_config_web_server,
        args=(plugin_config, 8081),
        daemon=True,
    ).start()


@driver.on_shutdown
async def _shutdown_services():
    from .matchers.chat_entry import agent

    # 优雅关闭记忆压缩队列（drain → 等待 worker → 关闭 httpx）
    await agent.memory_service.shutdown()

    # 清理 Agent HTTP 连接池
    await agent.close()

    await stop_node_service()