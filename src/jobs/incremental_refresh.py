import logging
from typing import Callable
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession # type: ignore
from sqlalchemy import select, func

from fastapi import HTTPException, status
from .. import crud, orm_models
from .base import BaseJob
from ..task_manager import TaskSuccess
from ..tasks import generic_import_task


class IncrementalRefreshJob(BaseJob):
    job_type = "incrementalRefresh"
    job_name = "定时增量追更"

    async def run(self, session: AsyncSession, progress_callback: Callable):
        """定时任务的核心逻辑: 按最新分集ID+1 抓取新集"""
        await progress_callback(0, "正在获取所有启用的追更源...")
        source_ids = await crud.get_sources_with_incremental_refresh_enabled(session)
        total_sources = len(source_ids)
        if not total_sources:
            raise TaskSuccess("没有找到任何启用的追更源，任务结束。")

        self.logger.info(f"定时追更：找到 {total_sources} 个源，将为每个源创建独立的导入任务。")
        await progress_callback(10, f"找到 {total_sources} 个源，正在创建任务...")

        submitted_count = 0
        # 为未绑定用户的定时任务指定一个默认归属（管理员）用于配额统计
        default_owner_user_id = await crud.get_first_admin_user_id(session)
        for i, source_id in enumerate(source_ids):
            # 为每个任务使用独立的会话，避免主会话被长时间占用
            async with self._session_factory() as task_session:
                source_info = await crud.get_anime_source_info(task_session, source_id)
                if not source_info:
                    self.logger.warning(f"无法找到数据源(id={source_id})的信息，跳过。")
                    continue

                # 修正：直接从数据库查询最大分集编号，这比获取所有分集再计算要高效得多，
                # 并且修复了因 `crud.get_episodes_for_source` 返回格式改变而导致的 TypeError。
                stmt = select(func.max(orm_models.Episode.episodeIndex)).where(orm_models.Episode.sourceId == source_id)
                latest_episode_index = (await task_session.execute(stmt)).scalar_one_or_none() or 0
                
                next_episode_index = latest_episode_index + 1
                self.logger.info(f"为 '{source_info['title']}' (源ID: {source_id}) 尝试获取第 {next_episode_index} 集...")

                unique_key = f"import-{source_info['providerName']}-{source_info['mediaId']}-ep{next_episode_index}"
                task_title = f"定时追更: {source_info['title']} - S{source_info.get('season', 1):02d}E{next_episode_index:02d}"
                
                # 生成unique_key用于重复任务检测
                unique_key = f"incremental-refresh:{source_info['providerName']}:{source_info['mediaId']}:{source_info.get('season', 1)}:{next_episode_index}"

                # 使用闭包捕获当前循环的变量
                def create_task_coro_factory(info, next_ep):
                    return lambda s, cb: generic_import_task(
                        provider=info["providerName"], mediaId=info["mediaId"], animeTitle=info["title"],
                        mediaType=info["type"], season=info.get("season", 1), year=info.get("year"),
                        currentEpisodeIndex=next_ep, imageUrl=None,
                        doubanId=None, tmdbId=info.get("tmdbId"), imdbId=None, tvdbId=None,
                        bangumiId=info.get("bangumiId"), config_manager=self.config_manager, metadata_manager=self.metadata_manager,
                        progress_callback=cb, session=s, manager=self.scraper_manager,
                        task_manager=self.task_manager, rate_limiter=self.rate_limiter,
                        user_id=default_owner_user_id, is_scheduled=True
                    )
                
                try:
                    await self.task_manager.submit_task(create_task_coro_factory(source_info, next_episode_index), task_title, unique_key=unique_key)
                    submitted_count += 1
                except HTTPException as e:
                    if e.status_code == status.HTTP_409_CONFLICT:
                        self.logger.info(f"跳过创建任务 '{task_title}'，因为它已在队列中或正在运行。")
                    else:
                        self.logger.error(f"为源 '{source_info['title']}' (ID: {source_id}) 创建追更任务时发生HTTP错误: {e.detail}")
                except Exception as e:
                    self.logger.error(f"为源 '{source_info['title']}' (ID: {source_id}) 创建追更任务时失败: {e}")

            progress = 10 + int(((i + 1) / total_sources) * 90)
            await progress_callback(progress, f"已处理 {i+1}/{total_sources} 个源")

        raise TaskSuccess(f"定时追更任务分发完成，共为 {submitted_count} 个源创建了新的导入任务。")
