from abc import ABC, abstractmethod
import logging
import re
from datetime import timedelta
from typing import Any, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from fastapi import Request
from pydantic import BaseModel

from .. import crud
from ..config_manager import ConfigManager
from ..task_manager import TaskManager
from ..rate_limiter import RateLimiter
from ..tasks import webhook_search_and_dispatch_task
from ..scraper_manager import ScraperManager
from ..metadata_manager import MetadataSourceManager

class WebhookPayload(BaseModel):
    """定义 Webhook 负载的通用结构。"""
    event_type: str
    media_title: str
    season_number: Optional[int] = None
    episode_number: Optional[int] = None

class BaseWebhook(ABC):
    """所有 Webhook 处理器的抽象基类。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], task_manager: TaskManager, scraper_manager: ScraperManager, rate_limiter: RateLimiter, metadata_manager: MetadataSourceManager, config_manager: ConfigManager):
        self._session_factory = session_factory
        self.task_manager = task_manager
        self.scraper_manager = scraper_manager
        self.rate_limiter = rate_limiter
        self.metadata_manager = metadata_manager
        self.config_manager = config_manager
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def handle(self, request: Request, webhook_source: str):
        """处理传入的 Webhook 负载。"""
        raise NotImplementedError

    async def dispatch_task(
        self,
        task_title: str,
        unique_key: str,
        payload: Dict[str, Any],
        webhook_source: str
    ):
        """
        将解析后的 Webhook 请求分发到数据库或任务管理器。
        """
        async with self._session_factory() as session:
            # 检查全局开关和过滤规则的逻辑已移至 webhook/tasks.py 中，
            # 以确保即使任务被延时，规则也能在执行时被应用。
            # 修正：将所有检查逻辑移回到这里，在创建任务前执行
            if not (await self.config_manager.get("webhookEnabled", "true")).lower() == 'true':
                self.logger.info("Webhook 功能已全局禁用，忽略请求。")
                return

            # 重新加入过滤逻辑
            filter_mode = await self.config_manager.get("webhookFilterMode", "blacklist")
            filter_regex_str = await self.config_manager.get("webhookFilterRegex", "")
            if filter_regex_str:
                try:
                    filter_pattern = re.compile(filter_regex_str, re.IGNORECASE)
                    anime_title = payload.get("animeTitle", "")
                    if (filter_mode == 'blacklist' and filter_pattern.search(anime_title)) or \
                       (filter_mode == 'whitelist' and not filter_pattern.search(anime_title)):
                        self.logger.info(f"Webhook 请求 '{anime_title}' 因匹配过滤规则而被忽略。")
                        return
                except re.error as e:
                    self.logger.error(f"无效的 Webhook 过滤正则表达式: '{filter_regex_str}'。错误: {e}。将忽略此过滤规则。")

            delayed_enabled = (await self.config_manager.get("webhookDelayedImportEnabled", "false")).lower() == 'true'
            delay_hours_str = await self.config_manager.get("webhookDelayedImportHours", "24")
            delay_hours = int(delay_hours_str) if delay_hours_str.isdigit() else 24
            
            if delayed_enabled:
                # 延时导入开启：将任务存入数据库
                await crud.create_webhook_task(
                    session, task_title, unique_key, payload, webhook_source,
                    True, timedelta(hours=delay_hours)
                )
                await session.commit()
                self.logger.info(f"Webhook 任务 '{task_title}' 已加入延时队列。")
            else:
                # 延时导入关闭：直接提交到 TaskManager
                self.logger.info(f"Webhook 延时导入已关闭，正在立即执行任务 '{task_title}'...")
                task_coro = lambda s, cb: webhook_search_and_dispatch_task(
                    webhookSource=webhook_source,
                    progress_callback=cb,
                    session=s,
                    manager=self.scraper_manager,
                    task_manager=self.task_manager,
                    metadata_manager=self.metadata_manager,
                    config_manager=self.config_manager,
                    rate_limiter=self.rate_limiter,
                    **payload
                )
                await self.task_manager.submit_task(task_coro, task_title, unique_key=unique_key)