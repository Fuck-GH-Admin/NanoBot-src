import time
import asyncio
from nonebot.log import logger


class MemoryCircuitBreaker:
    """记忆压缩队列的压力熔断，保护核心聊天链路。"""

    def __init__(self, memory_service, config):
        self.memory_service = memory_service
        self.config = config
        self._state = "CLOSED"
        # 回调：当 Worker 挂掉时调用，传入 memory_service
        self.on_worker_dead = None  # 类型：async Callable

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, new_state: str) -> None:
        if new_state not in ("CLOSED", "HALF_OPEN", "OPEN"):
            raise ValueError(f"Invalid state: {new_state}")
        logger.info(f"[CircuitBreaker] {self._state} -> {new_state}")
        self._state = new_state

    def allow_new_task(self) -> bool:
        """agent_service 调用此方法决定是否将压缩消息入队"""
        return self.state == "CLOSED"

    async def monitor_loop(self) -> None:
        """由 asyncio.create_task 启动，每 10 秒运行一次"""
        while True:
            await asyncio.sleep(10)
            qsize = self.memory_service._queue.qsize()

            # 状态 CLOSED 下的熔断触发
            if self.state == "CLOSED" and qsize > 50:
                logger.warning(f"[CircuitBreaker] 队列深度 {qsize} > 50，进入 HALF_OPEN")
                self.state = "HALF_OPEN"

            # 状态 HALF_OPEN 下的检查
            elif self.state == "HALF_OPEN":
                worker = self.memory_service._worker_task
                if worker is None or worker.done():
                    logger.error("[CircuitBreaker] Worker 协程已退出，进入 OPEN")
                    self.state = "OPEN"
                    if self.on_worker_dead:
                        await self.on_worker_dead(self.memory_service)
                elif qsize < 10:
                    logger.info(f"[CircuitBreaker] 队列深度 {qsize} < 10，恢复 CLOSED")
                    self.state = "CLOSED"

            # 状态 OPEN 下的恢复检测
            elif self.state == "OPEN":
                worker = self.memory_service._worker_task
                if worker is not None and not worker.done():
                    logger.warning("[CircuitBreaker] Worker 意外恢复，转为 HALF_OPEN")
                    self.state = "HALF_OPEN"


class EventLoopMonitor:
    """
    事件循环健康监控器。

    通过不断 await asyncio.sleep(1.0) 并测量实际耗时，
    检测主线程是否因同步阻塞任务导致事件循环卡顿。
    """

    def __init__(self, drift_threshold: float = 1.5):
        self.drift_threshold = drift_threshold

    async def start_monitor(self) -> None:
        """启动监控协程。该协程应作为后台任务运行，永不返回。"""
        logger.info(
            "[EventLoopMonitor] 事件循环监控已启动，漂移阈值: %.1fs",
            self.drift_threshold,
        )
        while True:
            t1 = time.monotonic()
            await asyncio.sleep(1.0)
            t2 = time.monotonic()
            drift = (t2 - t1) - 1.0
            if drift > self.drift_threshold:
                logger.warning(
                    "[EventLoopMonitor] 检测到事件循环阻塞！"
                    "计划睡眠 1s，实际耗时 %.2fs，漂移 %.2fs",
                    t2 - t1,
                    drift,
                )
            if drift > 5.0:
                logger.error(
                    "[EventLoopMonitor] 严重阻塞！事件循环停滞 %.2fs，"
                    "系统响应能力严重下降",
                    drift,
                )
