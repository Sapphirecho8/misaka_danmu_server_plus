import logging
from typing import Callable
from sqlalchemy.ext.asyncio import AsyncSession

from ..jobs.base import BaseJob
from .. import crud

logger = logging.getLogger(__name__)

class TokenResetJob(BaseJob):
    """系统内置任务：每日重置API Token调用次数"""
    
    job_type = "tokenReset"
    job_name = "重置API Token每日调用次数"
    is_system_task = True  # 硬编码：标记为系统内置任务
    
    def __init__(self, session_factory, **kwargs):
        self.session_factory = session_factory
    
    async def run(self, session: AsyncSession, progress_callback: Callable):
        """执行Token重置任务"""
        try:
            await progress_callback(0, "开始重置API Token每日调用次数...")
            
            reset_count = await crud.reset_all_token_daily_counts(session)
            
            await progress_callback(100, f"重置完成，共重置了 {reset_count} 个Token的调用次数")
            logger.info(f"定时任务：已重置 {reset_count} 个API Token的每日调用次数")
            
        except Exception as e:
            logger.error(f"重置API Token调用次数失败: {e}")
            await progress_callback(100, f"重置失败: {str(e)}")
            raise