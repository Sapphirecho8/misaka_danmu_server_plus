import logging
from typing import Callable
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy import text

from .. import crud, orm_models
from ..config import settings
from ..timezone import get_now
from .base import BaseJob
from ..task_manager import TaskSuccess
from ..database import _get_db_url

logger = logging.getLogger(__name__)

async def _optimize_database(session: AsyncSession, db_type: str) -> str:
    """根据数据库类型执行表优化。"""
    tables_to_optimize = ["comment", "task_history", "token_access_logs", "external_api_logs"]
    
    if db_type == "mysql":
        logger.info("检测到 MySQL，正在执行 OPTIMIZE TABLE...")
        await session.execute(text(f"OPTIMIZE TABLE {', '.join(tables_to_optimize)};"))
        # 提交由调用方（任务）处理
        return "OPTIMIZE TABLE 执行成功。"
    
    elif db_type == "postgresql":
        logger.info("检测到 PostgreSQL，正在执行 VACUUM...")
        # VACUUM 不能在事务块内运行。我们创建一个具有自动提交功能的新引擎来执行此特定操作。
        db_url_obj = _get_db_url()
        engine_args = {
            "isolation_level": "AUTOCOMMIT",
        }
        auto_commit_engine = create_async_engine(db_url_obj, **engine_args)
        try:
            async with auto_commit_engine.connect() as connection:
                await connection.execute(text("VACUUM;"))
            return "VACUUM 执行成功。"
        finally:
            await auto_commit_engine.dispose()
            
    else:
        message = f"不支持的数据库类型 '{db_type}'，跳过优化。"
        logger.warning(message)
        return message

async def _purge_binary_logs(session: AsyncSession, days: int) -> str:
    """
    执行 PURGE BINARY LOGS 命令来清理早于指定天数的 binlog 文件。
    警告：这是一个高风险操作，需要 SUPER 或 BINLOG_ADMIN 权限。
    """
    logger.info(f"准备执行 PURGE BINARY LOGS BEFORE NOW() - INTERVAL {days} DAY...")
    await session.execute(text(f"PURGE BINARY LOGS BEFORE NOW() - INTERVAL {days} DAY"))
    # 这个操作不需要 commit，因为它不是DML
    msg = f"成功执行 PURGE BINARY LOGS，清除了 {days} 天前的日志。"
    logger.info(msg)
    return msg

class DatabaseMaintenanceJob(BaseJob):
    """
    一个用于执行数据库维护的定时任务，包括清理旧日志和优化表。
    """
    job_type = "databaseMaintenance"
    job_name = "数据库维护"

    async def run(self, session: AsyncSession, progress_callback: Callable):
        """
        执行数据库维护的核心任务：清理旧日志和优化表。
        """
        self.logger.info(f"开始执行 [{self.job_name}] 定时任务...")
        
        # --- 1. 应用日志清理 ---
        await progress_callback(10, "正在清理旧日志...")
        
        try:
            # 日志保留天数，默认为30天。
            retention_days_str = await crud.get_config_value(session, "logRetentionDays", "30")
            retention_days = int(retention_days_str)
        except (ValueError, TypeError):
            retention_days = 30
        
        if retention_days > 0:
            self.logger.info(f"将清理 {retention_days} 天前的日志记录。")
            cutoff_date = get_now() - timedelta(days=retention_days)
            
            tables_to_prune = {
                "任务历史": (orm_models.TaskHistory, orm_models.TaskHistory.createdAt),
                "Token访问日志": (orm_models.TokenAccessLog, orm_models.TokenAccessLog.accessTime),
                "外部API访问日志": (orm_models.ExternalApiLog, orm_models.ExternalApiLog.accessTime),
            }
            
            total_deleted = 0
            for name, (model, date_column) in tables_to_prune.items():
                deleted_count: Optional[int] = await crud.prune_logs(session, model, date_column, cutoff_date)
                
                # 修正：增加对 deleted_count 的 None 值检查，以提高代码的健壮性。
                # 这可以防止当底层数据库操作（如某些驱动下的DELETE）不返回行数时，任务意外失败。
                if deleted_count is None:
                    deleted_count = 0

                if deleted_count > 0:
                    self.logger.info(f"从 {name} 表中删除了 {deleted_count} 条旧记录。")
                total_deleted += deleted_count
            await progress_callback(40, f"应用日志清理完成，共删除 {total_deleted} 条记录。")
        else:
            self.logger.info("日志保留天数设为0或无效，跳过清理。")
            await progress_callback(40, "日志保留天数设为0，跳过清理。")

        # --- 2. Binlog 清理 (仅MySQL) ---
        db_type = settings.database.type.lower()
        if db_type == "mysql":
            await progress_callback(50, "正在清理 MySQL Binlog...")
            try:
                # 新增：从配置中读取binlog保留天数
                binlog_retention_days_str = await crud.get_config_value(session, "mysqlBinlogRetentionDays", "3")
                binlog_retention_days = int(binlog_retention_days_str)

                if binlog_retention_days > 0:
                    # 用户指定清理N天前的日志
                    binlog_cleanup_message = await _purge_binary_logs(session, days=binlog_retention_days)
                    self.logger.info(binlog_cleanup_message)
                    await progress_callback(60, binlog_cleanup_message)
                else:
                    binlog_cleanup_message = "Binlog自动清理已禁用。"
                    self.logger.info(binlog_cleanup_message)
                    await progress_callback(60, binlog_cleanup_message)
            except OperationalError as e:
                # 检查是否是权限不足的错误 (MySQL error code 1227)
                if e.orig and hasattr(e.orig, 'args') and len(e.orig.args) > 0 and e.orig.args[0] == 1227:
                    binlog_cleanup_message = "Binlog 清理失败: 数据库用户缺少 SUPER 或 BINLOG_ADMIN 权限。此为正常现象，可安全忽略。"
                    self.logger.warning(binlog_cleanup_message)
                    await progress_callback(60, binlog_cleanup_message)
                else:
                    # 其他操作错误，仍然记录详细信息
                    binlog_cleanup_message = f"Binlog 清理失败: {e}"
                    self.logger.error(binlog_cleanup_message, exc_info=True)
                    await progress_callback(60, binlog_cleanup_message)
            except Exception as e:
                # 记录错误，但不中断任务
                binlog_cleanup_message = f"Binlog 清理失败: {e}"
                self.logger.error(binlog_cleanup_message, exc_info=True)
                await progress_callback(60, binlog_cleanup_message)

        # --- 3. 数据库表优化 ---
        await progress_callback(70, "正在执行数据库表优化...")
        
        try:
            optimization_message = await _optimize_database(session, db_type)
            self.logger.info(f"数据库优化结果: {optimization_message}")
        except Exception as e:
            optimization_message = f"数据库优化失败: {e}"
            self.logger.error(optimization_message, exc_info=True)
            # 即使优化失败，也不应导致整个任务失败，仅记录错误

        await progress_callback(90, optimization_message)

        final_message = f"数据库维护完成。{optimization_message}"
        raise TaskSuccess(final_message)
