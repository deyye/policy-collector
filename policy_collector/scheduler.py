"""定时运行器：周期性触发指定来源采集入库。

初版实现为阻塞式轮询（适合演示/内网常驻）；
生产部署建议改用系统 cron / systemd timer 调用 cli run-source，职责不变。
"""
from __future__ import annotations

import logging
import signal
import time

from .config import AppConfig
from .pipeline import Pipeline

logger = logging.getLogger("policy.scheduler")


class Scheduler:
    def __init__(self, cfg: AppConfig, interval_minutes: int | None = None):
        self.cfg = cfg
        self.interval = interval_minutes or cfg.schedule_interval_minutes
        self._stop = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_):
        logger.info("收到退出信号，当前轮结束后停止")
        self._stop = True

    def run_forever(self, source_names: list[str] | None = None, prefer: str = "llm") -> None:
        pipeline = Pipeline(self.cfg)
        sources = source_names or list(self.cfg.sources.keys())
        logger.info("定时器启动：来源=%s 间隔=%d分钟", sources, self.interval)
        while not self._stop:
            for name in sources:
                if self._stop:
                    break
                try:
                    stats = pipeline.run_source(name, prefer=prefer)
                    logger.info("[%s] 完成: %s", name, stats.to_dict())
                except Exception as e:  # noqa: BLE001
                    logger.error("[%s] 运行失败: %s", name, e)
            # 等待下一轮（可被信号打断）
            for _ in range(self.interval * 60):
                if self._stop:
                    break
                time.sleep(1)
        logger.info("定时器已停止")
