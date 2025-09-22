import asyncio
import logging
import json
from typing import Callable
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from ..tasks import webhook_search_and_dispatch_task
from .base import BaseJob

logger = logging.getLogger(__name__)

class WebhookProcessorJob(BaseJob):
    job_type = "webhookProcessor"
    job_name = "Webhook 延时任务处理器"

    async def run(self, session: AsyncSession, progress_callback: Callable):
        """
        执行 Webhook 延时任务处理。
        """
        await progress_callback(0, "开始检查待处理的 Webhook 任务...")
        
        due_tasks = await crud.get_due_webhook_tasks(session)
        if not due_tasks:
            await progress_callback(100, "没有需要处理的 Webhook 任务。")
            return

        total_tasks = len(due_tasks)
        logger.info(f"找到 {total_tasks} 个待处理的 Webhook 任务，开始执行...")

        for i, task in enumerate(due_tasks):
            progress = int(((i + 1) / total_tasks) * 100)
            await progress_callback(progress, f"正在处理任务 {i+1}/{total_tasks}: {task.taskTitle}")

            try:
                # 解析 payload 并提交到 TaskManager
                payload = task.payload
                if isinstance(payload, str):
                    payload = json.loads(payload)
                
                # 使用 webhook_search_and_dispatch_task 的逻辑
                task_coro = lambda s, cb: webhook_search_and_dispatch_task(
                    webhookSource=task.webhookSource,
                    progress_callback=cb,
                    session=s,
                    manager=self.scraper_manager,
                    task_manager=self.task_manager,
                    metadata_manager=self.metadata_manager,
                    config_manager=self.config_manager,
                    rate_limiter=self.rate_limiter,
                    **payload
                )
                await self.task_manager.submit_task(task_coro, task.taskTitle, unique_key=task.uniqueKey)
                # 修正：只有在任务成功提交后才删除记录
                await session.delete(task)
            except Exception as e:
                logger.error(f"处理 Webhook 任务 (ID: {task.id}) 时失败: {e}", exc_info=True)
                # 如果提交失败，则将任务标记为失败，以便用户可以手动重试
                await crud.update_webhook_task_status(session, task.id, "failed")
            finally:
                await session.commit() # 确保状态更新或删除被提交