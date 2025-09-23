import logging
from typing import Callable, List, Optional, Dict, Tuple
import json
import asyncio
import re
import traceback
from pathlib import Path
import shutil
import io
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from thefuzz import fuzz
from sqlalchemy import delete, func, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload
from xml.sax.saxutils import escape as xml_escape

from . import crud, models, orm_models
from .rate_limiter import RateLimiter, RateLimitExceededError
from .config_manager import ConfigManager
from .image_utils import download_image
from .config import settings
from .scraper_manager import ScraperManager
from .metadata_manager import MetadataSourceManager 
from .utils import parse_search_keyword, clean_xml_string
from .crud import DANMAKU_BASE_DIR, _get_fs_path_from_web_path
from .task_manager import TaskManager, TaskSuccess, TaskStatus
from .timezone import get_now
from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

def _parse_xml_content(xml_content: str) -> List[Dict[str, str]]:
    """
    使用 iterparse 高效解析XML弹幕内容，无条数限制，并规范化p属性。
    """
    comments = []
    try:
        # 使用 io.StringIO 将字符串转换为文件流，以便 iterparse 处理
        xml_stream = io.StringIO(xml_content)
        # iterparse 以事件驱动的方式解析，内存效率高，适合大文件
        for event, elem in ET.iterparse(xml_stream, events=('end',)):
            # 当一个 <d> 标签结束时处理它
            if elem.tag == 'd':
                p_attr = elem.get('p')
                text = elem.text
                if p_attr is not None and text is not None:
                    p_parts = p_attr.split(',')
                    if len(p_parts) >= 4:
                        # 提取前4个核心参数: 时间, 模式, 字体大小, 颜色
                        processed_p_attr = f"{p_parts[0]},{p_parts[1]},{p_parts[2]},{p_parts[3]},[custom_xml]"
                        comments.append({'p': processed_p_attr, 'm': text})
                    else:
                        # 如果参数不足4个，保持原样以避免数据损坏
                        comments.append({'p': p_attr, 'm': text})
                # 清理已处理的元素以释放内存
                elem.clear()
    except ET.ParseError as e:
        logger.error(f"解析XML时出错: {e}")
        # 即使解析出错，也可能已经解析了一部分，返回已解析的内容
    return comments

def _generate_episode_range_string(episode_indices: List[int]) -> str:
    """
    将分集编号列表转换为紧凑的字符串表示形式。
    例如: [1, 2, 3, 5, 8, 9, 10] -> "1-3, 5, 8-10"
    """
    if not episode_indices:
        return "无"

    indices = sorted(list(set(episode_indices)))
    if not indices:
        return "无"

    ranges = []
    start = end = indices[0]

    for i in range(1, len(indices)):
        if indices[i] == end + 1:
            end = indices[i]
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = indices[i]
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ", ".join(ranges)

def _generate_dandan_xml(comments: List[dict]) -> str:
    """
    根据弹幕字典列表生成 dandanplay 格式的 XML 字符串。
    """
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<i>',
        '  <chatserver>danmu</chatserver>',
        '  <chatid>0</chatid>',
        '  <mission>0</mission>',
        f'  <maxlimit>{len(comments)}</maxlimit>',
        '  <source>kuyun</source>'
    ]
    for comment in comments:
        content = xml_escape(comment.get('m', ''))
        p_attr_str = comment.get('p', '0,1,25,16777215')
        p_parts = p_attr_str.split(',')
        
        # 强制修复逻辑：确保 p 属性的格式为 时间,模式,字体大小,颜色,...
        core_parts_end_index = len(p_parts)
        for i, part in enumerate(p_parts):
            if '[' in part and ']' in part:
                core_parts_end_index = i
                break
        core_parts = p_parts[:core_parts_end_index]
        optional_parts = p_parts[core_parts_end_index:]

        # 场景1: 缺少字体大小 (e.g., "1.23,1,16777215")
        if len(core_parts) == 3:
            core_parts.insert(2, '25')
        # 场景2: 字体大小为空或无效 (e.g., "1.23,1,,16777215")
        elif len(core_parts) == 4 and (not core_parts[2] or not core_parts[2].strip().isdigit()):
            core_parts[2] = '25'

        final_p_attr = ','.join(core_parts + optional_parts)
        xml_parts.append(f'  <d p="{final_p_attr}">{content}</d>')
    xml_parts.append('</i>')
    return '\n'.join(xml_parts)

def _convert_text_danmaku_to_xml(text_content: str) -> str:
    """
    将非标准的、基于行的纯文本弹幕格式转换为标准的XML格式。
    支持的格式: "时间,模式,?,颜色,... | 弹幕内容"
    """
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<i>',
        '  <chatserver>danmu</chatserver>',
        '  <chatid>0</chatid>',
        '  <mission>0</mission>',
        '  <source>misaka</source>'
    ]
    comments = []
    for line in text_content.strip().split('\n'):
        if '|' not in line:
            continue
        params_str, text = line.split('|', 1)
        params = params_str.split(',')
        if len(params) >= 4:
            # 提取关键参数: 时间, 模式, 颜色
            # 格式: 756.103,1,25,16777215,...
            time_sec = params[0]
            mode     = params[1]
            fontsize = params[2]
            color    = params[3]
            p_attr = f"{time_sec},{mode},{fontsize},{color},[custom_text]"
            escaped_text = xml_escape(text.strip())
            comments.append(f'  <d p="{p_attr}">{escaped_text}</d>')
    xml_parts.insert(5, f'  <maxlimit>{len(comments)}</maxlimit>')
    xml_parts.extend(comments)
    xml_parts.append('</i>')
    return '\n'.join(xml_parts)

def _delete_danmaku_file(danmaku_file_path_str: Optional[str]):
    """根据数据库中存储的Web路径，安全地删除对应的弹幕文件。"""
    if not danmaku_file_path_str:
        return
    try:
        # 修正：使用 crud 中的辅助函数来获取正确的文件系统路径
        fs_path = _get_fs_path_from_web_path(danmaku_file_path_str)
        if fs_path and fs_path.is_file():
            fs_path.unlink(missing_ok=True)
    except (ValueError, FileNotFoundError):
        # 如果路径无效或文件不存在，则忽略
        pass
    except Exception as e:
        logger.error(f"删除弹幕文件 '{danmaku_file_path_str}' 时出错: {e}", exc_info=True)

async def delete_anime_task(animeId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete an anime and all its related data."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await progress_callback(0, f"开始删除 (尝试 {attempt + 1}/{max_retries})...")
            
            # 检查作品是否存在
            anime_exists = await session.get(orm_models.Anime, animeId)
            if not anime_exists:
                raise TaskSuccess("作品未找到，无需删除。")

            # 1. 删除关联的弹幕文件目录
            await progress_callback(50, "正在删除关联的弹幕文件...")
            anime_danmaku_dir = DANMAKU_BASE_DIR / str(animeId)
            if anime_danmaku_dir.exists() and anime_danmaku_dir.is_dir():
                shutil.rmtree(anime_danmaku_dir)
                logger.info(f"已删除作品的弹幕目录: {anime_danmaku_dir}")

            # 2. 删除作品本身 (数据库将通过级联删除所有关联记录)
            await progress_callback(90, "正在删除数据库记录...")
            await session.delete(anime_exists)
            
            await session.commit()
            raise TaskSuccess("删除成功。")
        except OperationalError as e:
            await session.rollback()
            if "Lock wait timeout exceeded" in str(e) and attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1) # 2, 4, 8 seconds
                logger.warning(f"删除作品时遇到锁超时，将在 {wait_time} 秒后重试...")
                await progress_callback(0, f"数据库锁定，将在 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
                continue # Retry the loop
            else:
                logger.error(f"删除作品任务 (ID: {animeId}) 失败: {e}", exc_info=True)
                raise # Re-raise if it's not a lock error or retries are exhausted
        except TaskSuccess:
            raise # Propagate success exception
        except Exception as e:
            await session.rollback()
            logger.error(f"删除作品任务 (ID: {animeId}) 失败: {e}", exc_info=True)
            raise

async def delete_source_task(sourceId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete a source and all its related data."""
    await progress_callback(0, "开始删除...")
    try:
        # 检查源是否存在
        source_exists = await session.get(orm_models.AnimeSource, sourceId)
        if not source_exists:
            raise TaskSuccess("数据源未找到，无需删除。")
        
        # 在删除数据库记录前，先删除关联的物理文件
        episodes_to_delete_res = await session.execute(
            select(orm_models.Episode.danmakuFilePath).where(orm_models.Episode.sourceId == sourceId)
        )
        for file_path in episodes_to_delete_res.scalars().all():
            _delete_danmaku_file(file_path)

        # 删除源记录，数据库将级联删除其下的所有分集记录
        await session.delete(source_exists)
        await session.commit()

        raise TaskSuccess("删除成功。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"删除源任务 (ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def delete_episode_task(episodeId: int, session: AsyncSession, progress_callback: Callable):
    """Background task to delete an episode and its comments."""
    await progress_callback(0, "开始删除...")
    try:
        # 检查分集是否存在
        episode_exists = await session.get(orm_models.Episode, episodeId)
        if not episode_exists:
            raise TaskSuccess("分集未找到，无需删除。")

        # 在删除数据库记录前，先删除物理文件
        _delete_danmaku_file(episode_exists.danmakuFilePath)

        await session.delete(episode_exists)
        await session.commit()
        raise TaskSuccess("删除成功。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"删除分集任务 (ID: {episodeId}) 失败: {e}", exc_info=True)
        raise

async def _import_episodes_iteratively(
    session: AsyncSession,
    scraper: "ScraperManager.BaseScraper",
    rate_limiter: RateLimiter,
    progress_callback: Callable,
    episodes: List[models.ProviderEpisodeInfo],
    anime_id: int,
    source_id: int,
    user_id: Optional[int] = None,
    is_scheduled: bool = False
) -> Tuple[int, List[int], int]:
    """
    一个健壮的辅助函数，用于迭代处理分集列表。
    它为每一集下载弹幕、保存到数据库，并立即提交事务。
    这确保了即使任务中断，已完成的部分也能被保留。

    :return: 一个元组 (新增弹幕总数, 成功处理的分集索引列表, 失败的分集数)
    """
    total_comments_added = 0
    successful_episodes_indices = []
    failed_episodes_count = 0
    total_episodes = len(episodes)
    
    i = 0
    while i < total_episodes:
        # 如果有用户限额，先计算本小时剩余可用配额；为 0 则暂停到下个整点
        if user_id:
            user_full = await crud.get_user_full_by_id(session, user_id)
            # 取限额（-1 表示无限）
            limit = (user_full or {}).get('perHourLimit')
            if limit is None:
                limit = 1000
            try:
                state = await crud.get_or_create_user_rate_state(session, user_id)
                logger.debug(f"[user-rate] user_id={user_id} limit={limit} used={state.requestCount}")
            except Exception:
                # 读取状态失败不阻断流程，但记录日志
                logger.warning(f"读取用户流控状态失败 user_id={user_id}", exc_info=True)
            if limit >= 0:
                now = get_now().replace(tzinfo=None)
                # 自动重置
                if (now - state.lastResetTime).total_seconds() >= 3600:
                    state.requestCount = 0
                    state.lastResetTime = now
                    await session.flush()
                remaining = limit - state.requestCount
                if remaining <= 0:
                    seconds = await crud.get_user_rate_seconds_until_reset(session, user_id)
                    logger.info(f"[user-rate] user_id={user_id} 已达上限 limit={limit}, 将在 {seconds}s 后重试")
                    await progress_callback( base_progress if 'base_progress' in locals() else 0, f"用户流控受限，将在 {seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
                    await asyncio.sleep(max(1, seconds))
                    continue
        episode = episodes[i]
        logger.info(f"--- 开始处理分集 {i+1}/{total_episodes}: '{episode.title}' (ID: {episode.episodeId}) ---")
        base_progress = 20 + int((i / total_episodes) * 75 if total_episodes > 0 else 75)
        await progress_callback(base_progress, f"正在处理: {episode.title} ({i+1}/{total_episodes})")

        try:
            # 调整：仅在“成功保存一集弹幕”后再递增用户用量计数，
            # 避免因获取失败/跳过而造成的统计偏大（例如显示为 11 而实际成功 10 集）。
            await rate_limiter.check(scraper.provider_name)

            async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
                progress_slice = 75 / total_episodes if total_episodes > 0 else 0
                current_total_progress = base_progress + (danmaku_progress / 100) * progress_slice
                await progress_callback(int(current_total_progress), f"处理: {episode.title} - {danmaku_description}")

            comments = await scraper.get_comments(episode.episodeId, progress_callback=sub_progress_callback)
            
            if comments is not None:  # 即使是空列表也表示成功获取
                await rate_limiter.increment(scraper.provider_name)
                
                episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, episode.episodeIndex, episode.title, episode.url, episode.episodeId)
                added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
                await session.commit()  # 为每一集成功处理后提交一次
                # 成功处理一集后：先递增用户配额使用，再记录聚合用量（每集计 1）
                if user_id:
                    try:
                        user_full = await crud.get_user_full_by_id(session, user_id)
                        await crud.check_and_increment_user_rate(session, user_full or {'id': user_id})
                        # 记录递增后的使用量以便追踪
                        try:
                            state_after = await crud.get_or_create_user_rate_state(session, user_id)
                            logger.info(f"[user-rate] incremented user_id={user_id} now used={state_after.requestCount}")
                        except Exception:
                            pass
                        # 记录用户用量日志（仅用于聚合统计）
                        await crud.log_user_usage(session, user_id, action=('download_episode_scheduled' if is_scheduled else 'download_episode'), amount=1)
                        await session.commit()
                    except PermissionError:
                        # 若此处触发限额，说明并发下在成功保存后已达到/超过限额；
                        # 仍然视为本集成功，不回滚；仅在下次循环前进行等待。
                        seconds = await crud.get_user_rate_seconds_until_reset(session, user_id)
                        await progress_callback(base_progress, f"用户流控受限，将在 {seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
                        await asyncio.sleep(max(1, seconds))
                
                total_comments_added += added_count
                successful_episodes_indices.append(episode.episodeIndex)
                logger.info(f"分集 '{episode.title}' (DB ID: {episode_db_id}) 新增 {added_count} 条弹幕并已提交。")
            else:
                failed_episodes_count += 1
                logger.warning(f"分集 '{episode.title}' 获取弹幕失败（返回 None）。")

        except RateLimitExceededError as e:
            logger.warning(f"任务因达到速率限制而暂停: {e}")
            await progress_callback(base_progress, f"速率受限，将在 {e.retry_after_seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
            await asyncio.sleep(e.retry_after_seconds)
            continue  # 重试当前分集
        except Exception as e:
            logger.error(f"获取或保存分集 '{episode.title}' 的弹幕时发生错误: {e}", exc_info=True)
            failed_episodes_count += 1
            await progress_callback(base_progress, f"处理: {episode.title} - 错误，已跳过", status=TaskStatus.RUNNING)
            await session.rollback()  # 回滚此分集的失败操作

        i += 1  # 移动到下一集
    
    return total_comments_added, successful_episodes_indices, failed_episodes_count

async def delete_bulk_episodes_task(episodeIds: List[int], session: AsyncSession, progress_callback: Callable):
    """后台任务：批量删除多个分集。"""
    total = len(episodeIds)
    await progress_callback(5, f"准备删除 {total} 个分集...")
    deleted_count = 0
    try:
        for i, episode_id in enumerate(episodeIds):
            progress = 5 + int(((i + 1) / total) * 90) if total > 0 else 95
            await progress_callback(progress, f"正在删除分集 {i+1}/{total} (ID: {episode_id}) 的数据...")

            episode = await session.get(orm_models.Episode, episode_id)
            if episode:
                _delete_danmaku_file(episode.danmakuFilePath)
                await session.delete(episode)
                deleted_count += 1
                
                # 3. 为每个分集提交一次事务，以尽快释放锁
                await session.commit()
                
                # 短暂休眠，以允许其他数据库操作有机会执行
                await asyncio.sleep(0.1)

        raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")
    except TaskSuccess:
        # 显式地重新抛出 TaskSuccess，以确保它被 TaskManager 正确处理
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"批量删除分集任务失败: {e}", exc_info=True)
        raise

async def generic_import_task(
    provider: str,
    mediaId: str,
    animeTitle: str,
    mediaType: str,
    season: int,
    year: Optional[int],
    currentEpisodeIndex: Optional[int],
    imageUrl: Optional[str],
    doubanId: Optional[str],
    config_manager: ConfigManager,
    metadata_manager: MetadataSourceManager,
    tmdbId: Optional[str],
    imdbId: Optional[str],
    tvdbId: Optional[str],
    bangumiId: Optional[str],
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager, 
    task_manager: TaskManager,
    rate_limiter: RateLimiter,
    user_id: Optional[int] = None,
    is_scheduled: bool = False
):
    """
    后台任务：执行从指定数据源导入弹幕的完整流程。
    """
    # 重构导入逻辑以避免创建空条目
    scraper = manager.get_scraper(provider)
    
    # 使用从前端请求中直接传递的标题和季度，不再进行解析
    title_to_use = animeTitle.strip()
    season_to_use = season

    await progress_callback(10, "正在获取分集列表...")
    episodes = await scraper.get_episodes(
        mediaId,
        target_episode_index=currentEpisodeIndex,
        db_media_type=mediaType
    )
    # 修正：即使 scraper.get_episodes 应该处理过滤，我们在此处再次强制过滤，
    # 以确保由 Webhook 触发的单集导入任务（特别是首次导入）只处理目标分集。
    # 这是一个健壮性修复，以防某些 scraper 未正确实现 target_episode_index 逻辑。
    if currentEpisodeIndex is not None and episodes:
        original_count = len(episodes)
        episodes = [ep for ep in episodes if ep.episodeIndex == currentEpisodeIndex]
        if len(episodes) < original_count:
            logger.info(
                f"已将分集列表从 {original_count} 个强制过滤为 {len(episodes)} 个 (目标集: {currentEpisodeIndex})，以匹配 Webhook 请求。"
            )

    if not episodes:
        # --- FAILOVER LOGIC ---
        logger.info(f"主源 '{provider}' 未能找到分集，尝试故障转移...")
        await progress_callback(15, "主源未找到分集，尝试故障转移...")
        
        user = models.User(id=1, username="scheduled_task") # Create a dummy user for metadata calls
        
        comments = await metadata_manager.get_failover_comments(
            title=animeTitle,
            season=season,
            episode_index=currentEpisodeIndex,
            user=user
        )
        
        if comments:
            logger.info(f"故障转移成功，找到 {len(comments)} 条弹幕。正在保存...")
            await progress_callback(20, f"故障转移成功，找到 {len(comments)} 条弹幕。")
            
            local_image_path = await download_image(imageUrl, session, manager, provider)
            image_download_failed = bool(imageUrl and not local_image_path)
            
            anime_id = await crud.get_or_create_anime(session, title_to_use, mediaType, season_to_use, imageUrl, local_image_path, year)
            await crud.update_metadata_if_empty(
                session, anime_id,
                tmdb_id=tmdbId,
                imdb_id=imdbId,
                tvdb_id=tvdbId,
                douban_id=doubanId,
                bangumi_id=bangumiId
            )
            source_id = await crud.link_source_to_anime(session, anime_id, provider, mediaId)
            
            episode_title = f"第 {currentEpisodeIndex} 集"
            episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, currentEpisodeIndex, episode_title, None, "failover")
            
            added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
            # 计入全局/源使用量（即使全局不限制也应继续计数）
            try:
                await rate_limiter.increment(provider)
            except Exception:
                logger.warning("在故障转移保存后递增全局计数时发生异常", exc_info=True)
            await session.commit()
            
            final_message = f"通过故障转移导入完成，共新增 {added_count} 条弹幕。" + (" (警告：海报图片下载失败)" if image_download_failed else "")
            raise TaskSuccess(final_message)
        else:
            msg = f"未能找到第 {currentEpisodeIndex} 集。" if currentEpisodeIndex else "未能获取到任何分集。"
            logger.error(f"任务失败: {msg} (provider='{provider}', media_id='{mediaId}')")
            raise ValueError(msg)

    if mediaType == "movie" and episodes:
        logger.info(f"检测到媒体类型为电影，将只处理第一个分集 '{episodes[0].title}'。")
        episodes = episodes[:1]

    # --- 数据库写入阶段 (pre-loop) ---
    await progress_callback(15, "正在准备数据库...")
    local_image_path = await download_image(imageUrl, session, manager, provider)
    image_download_failed = bool(imageUrl and not local_image_path)

    anime_id = await crud.get_or_create_anime(session, title_to_use, mediaType, season_to_use, imageUrl, local_image_path, year)
    await crud.update_metadata_if_empty(
        session, anime_id,
        tmdb_id=tmdbId,
        imdb_id=imdbId,
        tvdb_id=tvdbId,
        douban_id=doubanId,
        bangumi_id=bangumiId
    )
    source_id = await crud.link_source_to_anime(session, anime_id, provider, mediaId)
    # 关键：先提交一次，确保 Anime 和 Source 记录已创建
    await session.commit()

    # --- 循环处理每一集 ---
    total_comments_added, successful_episodes_indices, failed_episodes_count = await _import_episodes_iteratively(
        session=session,
        scraper=scraper,
        rate_limiter=rate_limiter,
        progress_callback=progress_callback,
        episodes=episodes,
        anime_id=anime_id,
        source_id=source_id,
        user_id=user_id,
        is_scheduled=is_scheduled
    )

    if not successful_episodes_indices and failed_episodes_count > 0:
        raise TaskSuccess("导入完成，但所有分集弹幕获取失败。")
    
    episode_range_str = _generate_episode_range_string(successful_episodes_indices)
    final_message = f"导入完成，导入集: < {episode_range_str} >，新增 {total_comments_added} 条弹幕。"
    if failed_episodes_count > 0:
        final_message += f" {failed_episodes_count} 个分集因网络或解析错误获取失败。"
    if image_download_failed:
        final_message += " (警告：海报图片下载失败)"
    raise TaskSuccess(final_message)
    
async def edited_import_task(
    request_data: "models.EditedImportRequest",
    progress_callback: Callable,
    session: AsyncSession,
    config_manager: ConfigManager,
    manager: ScraperManager,
    rate_limiter: RateLimiter,
    metadata_manager: MetadataSourceManager,
    user_id: Optional[int] = None
):
    """后台任务：处理编辑后的导入请求。"""
    scraper = manager.get_scraper(request_data.provider)
    
    episodes = request_data.episodes
    if not episodes:
        raise TaskSuccess("没有提供任何分集，任务结束。")

    # 使用从前端请求中直接传递的标题和季度，不再进行解析
    title_to_use = request_data.animeTitle.strip()
    season_to_use = request_data.season
    
    # --- 数据库写入阶段 (pre-loop) ---
    await progress_callback(15, "正在准备数据库...")
    local_image_path = await download_image(request_data.imageUrl, session, manager, request_data.provider)
    image_download_failed = bool(request_data.imageUrl and not local_image_path)

    anime_id = await crud.get_or_create_anime(
        session, title_to_use, request_data.mediaType,
        season_to_use, request_data.imageUrl, local_image_path, request_data.year
    )
    await crud.update_metadata_if_empty(
        session, anime_id,
        tmdb_id=request_data.tmdbId,
        imdb_id=request_data.imdbId,
        tvdb_id=request_data.tvdbId,
        douban_id=request_data.doubanId,
        bangumi_id=request_data.bangumiId,
        tmdb_episode_group_id=request_data.tmdbEpisodeGroupId
    )
    source_id = await crud.link_source_to_anime(session, anime_id, request_data.provider, request_data.mediaId)
    await session.commit()

    # --- 循环处理每一集 ---
    total_comments_added, successful_episodes_indices, failed_episodes_count = await _import_episodes_iteratively(
        session=session,
        scraper=scraper,
        rate_limiter=rate_limiter,
        progress_callback=progress_callback,
        episodes=episodes,
        anime_id=anime_id,
        source_id=source_id,
        user_id=user_id
    )

    if not successful_episodes_indices and failed_episodes_count > 0:
        raise TaskSuccess("导入完成，但所有分集弹幕获取失败。")

    episode_range_str = _generate_episode_range_string(successful_episodes_indices)

    final_message = f"导入完成，导入集: < {episode_range_str} >，新增 {total_comments_added} 条弹幕。"
    if failed_episodes_count > 0:
        final_message += f" {failed_episodes_count} 个分集因网络或解析错误获取失败。"
    if image_download_failed:
        final_message += " (警告：海报图片下载失败)"
    raise TaskSuccess(final_message)

async def full_refresh_task(sourceId: int, session: AsyncSession, scraper_manager: ScraperManager, task_manager: TaskManager, rate_limiter: RateLimiter, progress_callback: Callable, metadata_manager: MetadataSourceManager, user_id: Optional[int] = None):
    """    
    后台任务：全量刷新一个已存在的番剧，采用先获取后删除的安全策略。
    """
    logger.info(f"开始刷新源 ID: {sourceId}")
    try:
        source_info = await crud.get_anime_source_info(session, sourceId)
        if not source_info:
            raise ValueError(f"找不到源ID {sourceId} 的信息。")

        scraper = scraper_manager.get_scraper(source_info["providerName"])

        # 步骤 1: 获取新分集列表的元数据
        await progress_callback(10, "正在获取新分集列表...")
        current_media_id = source_info["mediaId"]
        new_episodes_meta = await scraper.get_episodes(current_media_id, db_media_type=source_info.get("type"))
        
        # --- 故障转移逻辑 ---
        if not new_episodes_meta:
            logger.info(f"主源 '{source_info['providerName']}' 未能找到分集，尝试故障转移...")
            await progress_callback(15, "主源未找到分集，尝试故障转移...")
            new_media_id = await metadata_manager.find_new_media_id(source_info)
            if new_media_id and new_media_id != current_media_id:
                logger.info(f"通过故障转移为 '{source_info['title']}' 找到新的 mediaId: '{new_media_id}'，将重试。")
                await progress_callback(18, f"找到新的媒体ID，正在重试...")
                await crud.update_source_media_id(session, sourceId, new_media_id)
                await session.commit() # 提交 mediaId 的更新
                new_episodes_meta = await scraper.get_episodes(new_media_id)

        if not new_episodes_meta:
            raise TaskSuccess("刷新失败：未能从源获取任何分集信息。旧数据已保留。")

        # 步骤 2: 迭代地导入/更新分集
        total_comments_added, successful_indices, failed_count = await _import_episodes_iteratively(
            session=session,
            scraper=scraper,
            rate_limiter=rate_limiter,
            progress_callback=progress_callback,
            episodes=new_episodes_meta,
            anime_id=source_info["animeId"],
            source_id=sourceId,
            user_id=user_id
        )

        # 步骤 3: 在所有导入/更新操作完成后，清理过时的分集
        await progress_callback(95, "正在清理过时分集...")
        new_provider_ids = {ep.episodeId for ep in new_episodes_meta}
        old_episodes_res = await session.execute(
            select(orm_models.Episode).where(orm_models.Episode.sourceId == sourceId)
        )
        episodes_to_delete = [ep for ep in old_episodes_res.scalars().all() if ep.providerEpisodeId not in new_provider_ids]

        if episodes_to_delete:
            logger.info(f"全量刷新：找到 {len(episodes_to_delete)} 个过时的分集，正在删除...")
            for ep in episodes_to_delete:
                _delete_danmaku_file(ep.danmakuFilePath)
                await session.delete(ep)
            await session.commit()
            logger.info("过时的分集已删除。")

        # 步骤 4: 构造最终的成功消息
        episode_range_str = _generate_episode_range_string(successful_indices)
        final_message = f"全量刷新完成，处理了 {len(new_episodes_meta)} 个分集，新增 {total_comments_added} 条弹幕。"
        if failed_count > 0:
            final_message += f" {failed_count} 个分集获取失败。"
        if episodes_to_delete:
            final_message += f" 删除了 {len(episodes_to_delete)} 个过时分集。"
        raise TaskSuccess(final_message)

    except TaskSuccess:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"全量刷新任务 (源ID: {sourceId}) 失败: {e}", exc_info=True)
        raise    

async def delete_bulk_sources_task(sourceIds: List[int], session: AsyncSession, progress_callback: Callable):
    """Background task to delete multiple sources."""
    total = len(sourceIds)
    deleted_count = 0
    for i, sourceId in enumerate(sourceIds):
        progress = int((i / total) * 100)
        await progress_callback(progress, f"正在删除源 {i+1}/{total} (ID: {sourceId})...")
        try:
            source = await session.get(orm_models.AnimeSource, sourceId)
            if source:
                await session.delete(source)
                await session.commit()
                deleted_count += 1
        except Exception as e:
            logger.error(f"批量删除源任务中，删除源 (ID: {sourceId}) 失败: {e}", exc_info=True)
            # Continue to the next one
    await session.commit()
    raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")

async def refresh_episode_task(episodeId: int, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter, progress_callback: Callable, user_id: Optional[int] = None):
    """后台任务：刷新单个分集的弹幕"""
    logger.info(f"开始刷新分集 ID: {episodeId}")
    try:
        await progress_callback(0, "正在获取分集信息...")
        # 1. 获取分集的源信息
        info = await crud.get_episode_provider_info(session, episodeId)
        if not info or not info.get("providerName") or not info.get("providerEpisodeId"):
            logger.error(f"刷新失败：在数据库中找不到分集 ID: {episodeId} 的源信息")
            await progress_callback(100, "失败: 找不到源信息")
            return

        provider_name = info["providerName"]
        provider_episode_id = info["providerEpisodeId"]
        scraper = manager.get_scraper(provider_name)
        # 用户级限额等待
        while True:
            if user_id:
                user_full = await crud.get_user_full_by_id(session, user_id)
                try:
                    await crud.check_and_increment_user_rate(session, user_full or {'id': user_id})
                    break
                except PermissionError:
                    seconds = await crud.get_user_rate_seconds_until_reset(session, user_id)
                    await progress_callback(20, f"用户流控受限，将在 {seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
                    await asyncio.sleep(max(1, seconds))
            else:
                break
        try:
            await rate_limiter.check(provider_name)
        except RateLimitExceededError as e:
            raise TaskSuccess(f"达到速率限制。请在 {e.retry_after_seconds:.0f} 秒后重试。")

        await progress_callback(30, "正在从源获取新弹幕...")

        async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
            # 30% for setup, 65% for download, 5% for db write
            current_total_progress = 30 + (danmaku_progress / 100) * 65
            await progress_callback(current_total_progress, danmaku_description)

        all_comments_from_source = await scraper.get_comments(provider_episode_id, progress_callback=sub_progress_callback)

        if not all_comments_from_source:
            await crud.update_episode_fetch_time(session, episodeId)
            raise TaskSuccess("未找到任何弹幕。")

        await rate_limiter.increment(provider_name)

        await progress_callback(96, f"正在写入 {len(all_comments_from_source)} 条新弹幕...")
        
        # 获取 animeId 用于文件路径
        anime_id = info["animeId"]
        added_count = await crud.save_danmaku_for_episode(session, episodeId, all_comments_from_source)
        
        await session.commit()
        raise TaskSuccess(f"刷新完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        # 任务成功完成，直接重新抛出，由 TaskManager 处理
        raise
    except Exception as e:
        logger.error(f"刷新分集 ID: {episodeId} 时发生严重错误: {e}", exc_info=True)
        raise # Re-raise so the task manager catches it and marks as FAILED

async def reorder_episodes_task(sourceId: int, session: AsyncSession, progress_callback: Callable):
    """后台任务：重新编号一个源的所有分集，并同步更新其ID和物理文件。"""
    logger.info(f"开始重整源 ID: {sourceId} 的分集顺序。")
    await progress_callback(0, "正在获取分集列表...")

    dialect_name = session.bind.dialect.name
    is_mysql = dialect_name == 'mysql'
    is_postgres = dialect_name == 'postgresql'

    try:
        # 根据数据库方言，暂时禁用外键检查
        if is_mysql:
            await session.execute(text("SET FOREIGN_KEY_CHECKS=0;"))
        elif is_postgres:
            await session.execute(text("SET session_replication_role = 'replica';"))
        
        # 在某些数据库/驱动中，执行此类命令后需要提交
        await session.commit()

        try:
            # 1. 获取计算新ID所需的信息
            source_info = await crud.get_anime_source_info(session, sourceId)
            if not source_info:
                raise ValueError(f"找不到源ID {sourceId} 的信息。")
            anime_id = source_info['animeId']
            source_order = source_info.get('sourceOrder')

            if source_order is None:
                # 如果由于某种原因（例如，非常旧的数据）没有 sourceOrder，则不允许重整
                raise ValueError(f"源 ID {sourceId} 没有持久化的 sourceOrder，无法重整。请尝试重新添加此源。")

            # 2. 获取所有分集ORM对象，按现有顺序排序
            episodes_orm_res = await session.execute(
                select(orm_models.Episode)
                .where(orm_models.Episode.sourceId == sourceId)
                .order_by(orm_models.Episode.episodeIndex, orm_models.Episode.id)
            )
            episodes_to_migrate = episodes_orm_res.scalars().all()

            if not episodes_to_migrate:
                raise TaskSuccess("没有找到分集，无需重整。")

            await progress_callback(10, "正在计算新的分集编号...")

            old_episodes_to_delete = []
            new_episodes_to_add = []
            
            for i, old_ep in enumerate(episodes_to_migrate):
                new_index = i + 1
                new_id = int(f"25{anime_id:06d}{source_order:02d}{new_index:04d}")
                
                if old_ep.id == new_id and old_ep.episodeIndex == new_index:
                    continue

                # 修正：使用正确的Web路径格式，并使用辅助函数进行文件路径转换
                new_danmaku_web_path = f"/danmaku/{anime_id}/{new_id}.xml" if old_ep.danmakuFilePath else None
                if old_ep.danmakuFilePath:
                    old_full_path = _get_fs_path_from_web_path(old_ep.danmakuFilePath)
                    new_full_path = _get_fs_path_from_web_path(new_danmaku_web_path)
                    if old_full_path.is_file() and old_full_path != new_full_path:
                        new_full_path.parent.mkdir(parents=True, exist_ok=True)
                        old_full_path.rename(new_full_path)

                new_episodes_to_add.append(orm_models.Episode(id=new_id, sourceId=old_ep.sourceId, episodeIndex=new_index, title=old_ep.title, sourceUrl=old_ep.sourceUrl, providerEpisodeId=old_ep.providerEpisodeId, fetchedAt=old_ep.fetchedAt, commentCount=old_ep.commentCount, danmakuFilePath=new_danmaku_web_path))
                old_episodes_to_delete.append(old_ep)

            if not old_episodes_to_delete:
                raise TaskSuccess("所有分集顺序和ID都正确，无需重整。")

            await progress_callback(30, f"准备迁移 {len(old_episodes_to_delete)} 个分集...")

            for old_ep in old_episodes_to_delete:
                await session.delete(old_ep)
            await session.flush()
            session.add_all(new_episodes_to_add)
            
            await session.commit()
            raise TaskSuccess(f"重整完成，共迁移了 {len(new_episodes_to_add)} 个分集的记录。")
        except Exception as e:
            await session.rollback()
            logger.error(f"重整分集任务 (源ID: {sourceId}) 事务中失败: {e}", exc_info=True)
            raise
        finally:
            # 务必重新启用外键检查/恢复会话角色
            if is_mysql:
                await session.execute(text("SET FOREIGN_KEY_CHECKS=1;"))
            elif is_postgres:
                await session.execute(text("SET session_replication_role = 'origin';"))
            await session.commit()
    except Exception as e:
        logger.error(f"重整分集任务 (源ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def offset_episodes_task(episode_ids: List[int], offset: int, session: AsyncSession, progress_callback: Callable):
    """后台任务：对选中的分集进行集数偏移，并同步更新其ID和物理文件。"""
    if not episode_ids:
        raise TaskSuccess("没有选中任何分集。")

    logger.info(f"开始集数偏移任务，偏移量: {offset}, 分集IDs: {episode_ids}")
    await progress_callback(0, "正在验证偏移操作...")

    dialect_name = session.bind.dialect.name
    is_mysql = dialect_name == 'mysql'
    is_postgres = dialect_name == 'postgresql'

    try:
        # --- Validation Phase ---
        # 1. Fetch all selected episodes and ensure they belong to the same source
        selected_episodes_res = await session.execute(
            select(orm_models.Episode)
            .where(orm_models.Episode.id.in_(episode_ids))
            .options(selectinload(orm_models.Episode.source))
        )
        selected_episodes = selected_episodes_res.scalars().all()

        if len(selected_episodes) != len(set(episode_ids)):
            raise ValueError("部分选中的分集未找到。")

        first_ep = selected_episodes[0]
        source_id = first_ep.sourceId
        anime_id = first_ep.source.animeId
        source_order = first_ep.source.sourceOrder

        if any(ep.sourceId != source_id for ep in selected_episodes):
            raise ValueError("选中的分集必须属于同一个数据源。")
        
        if source_order is None:
            raise ValueError(f"源 ID {source_id} 没有持久化的 sourceOrder，无法进行偏移操作。")

        # 2. Check for conflicts
        selected_indices = {ep.episodeIndex for ep in selected_episodes}
        new_indices = {idx + offset for idx in selected_indices}

        if any(idx <= 0 for idx in new_indices):
            # 此检查作为最后的安全防线，API层应已进行初步验证
            raise ValueError("偏移后的集数必须大于0。")

        all_source_episodes_res = await session.execute(
            select(orm_models.Episode.episodeIndex).where(orm_models.Episode.sourceId == source_id)
        )
        all_existing_indices = set(all_source_episodes_res.scalars().all())
        unselected_indices = all_existing_indices - selected_indices

        conflicts = new_indices.intersection(unselected_indices)
        if conflicts:
            raise ValueError(f"操作将导致集数冲突，无法执行。冲突集数: {sorted(list(conflicts))}")

        await progress_callback(20, "验证通过，准备迁移数据...")

        # --- Execution Phase ---
        # Temporarily disable foreign key checks
        if is_mysql:
            await session.execute(text("SET FOREIGN_KEY_CHECKS=0;"))
        elif is_postgres:
            await session.execute(text("SET session_replication_role = 'replica';"))
        await session.commit()

        try:
            old_episodes_to_delete = []
            new_episodes_to_add = []
            
            total_to_migrate = len(selected_episodes)
            for i, old_ep in enumerate(selected_episodes):
                await progress_callback(20 + int((i / total_to_migrate) * 70), f"正在处理分集 {i+1}/{total_to_migrate}...")

                new_index = old_ep.episodeIndex + offset
                new_id = int(f"25{anime_id:06d}{source_order:02d}{new_index:04d}")
                
                new_danmaku_web_path = None
                if old_ep.danmakuFilePath:
                    new_danmaku_web_path = f"/danmaku/{anime_id}/{new_id}.xml"
                    old_full_path = _get_fs_path_from_web_path(old_ep.danmakuFilePath)
                    new_full_path = _get_fs_path_from_web_path(new_danmaku_web_path)
                    if old_full_path and old_full_path.is_file() and old_full_path != new_full_path:
                        new_full_path.parent.mkdir(parents=True, exist_ok=True)
                        old_full_path.rename(new_full_path)

                new_episodes_to_add.append(orm_models.Episode(
                    id=new_id,
                    sourceId=old_ep.sourceId,
                    episodeIndex=new_index,
                    title=old_ep.title,
                    sourceUrl=old_ep.sourceUrl,
                    providerEpisodeId=old_ep.providerEpisodeId,
                    fetchedAt=old_ep.fetchedAt,
                    commentCount=old_ep.commentCount,
                    danmakuFilePath=new_danmaku_web_path
                ))
                old_episodes_to_delete.append(old_ep)

            # Perform DB operations
            for old_ep in old_episodes_to_delete:
                await session.delete(old_ep)
            await session.flush()
            
            session.add_all(new_episodes_to_add)
            await session.commit()

            raise TaskSuccess(f"集数偏移完成，共迁移了 {len(new_episodes_to_add)} 个分集。")

        except Exception as e:
            await session.rollback()
            logger.error(f"集数偏移任务 (源ID: {source_id}) 事务中失败: {e}", exc_info=True)
            raise
        finally:
            # Re-enable foreign key checks
            if is_mysql:
                await session.execute(text("SET FOREIGN_KEY_CHECKS=1;"))
            elif is_postgres:
                await session.execute(text("SET session_replication_role = 'origin';"))
            await session.commit()

    except ValueError as e:
        # Catch validation errors and report them as task failures
        logger.error(f"集数偏移任务验证失败: {e}")
        raise TaskSuccess(f"操作失败: {e}")
    except Exception as e:
        logger.error(f"集数偏移任务失败: {e}", exc_info=True)
        raise

async def incremental_refresh_task(sourceId: int, nextEpisodeIndex: int, session: AsyncSession, manager: ScraperManager, task_manager: TaskManager, rate_limiter: RateLimiter, metadata_manager: MetadataSourceManager, progress_callback: Callable, animeTitle: str, user_id: Optional[int] = None):
    """后台任务：增量刷新一个已存在的番剧。"""
    logger.info(f"开始增量刷新源 ID: {sourceId}，尝试获取第{nextEpisodeIndex}集")
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        progress_callback(100, "失败: 找不到源信息")
        logger.error(f"刷新失败：在数据库中找不到源 ID: {sourceId}")
        return
    try:
        # 重新执行通用导入逻辑, 只导入指定的一集
        await generic_import_task(
            provider=source_info["providerName"], mediaId=source_info["mediaId"],
            animeTitle=animeTitle, mediaType=source_info["type"],
            season=source_info.get("season", 1), year=source_info.get("year"),
            currentEpisodeIndex=nextEpisodeIndex, imageUrl=source_info.get("imageUrl"),
            doubanId=None, tmdbId=source_info.get("tmdbId"), config_manager=config_manager, metadata_manager=metadata_manager,
            imdbId=None, tvdbId=None, bangumiId=source_info.get("bangumiId"),
            progress_callback=progress_callback,
            session=session,
            manager=manager, # type: ignore
            task_manager=task_manager,
            rate_limiter=rate_limiter,
            user_id=user_id)
    except Exception as e:
        logger.error(f"增量刷新源任务 (ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

async def manual_import_task(
    sourceId: int, animeId: int, title: Optional[str], episodeIndex: int, content: str, providerName: str,
    progress_callback: Callable, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter,
    user_id: Optional[int] = None
):
    """后台任务：从URL手动导入弹幕。"""
    logger.info(f"开始手动导入任务: sourceId={sourceId}, title='{title or '未提供'}' ({providerName})")
    await progress_callback(10, "正在准备导入...")
    
    try:
        # Case 1: Custom source with XML data
        if providerName == 'custom':
            # 新增：自动检测内容格式。如果不是XML，则尝试从纯文本格式转换。
            content_to_parse = content.strip()
            if not content_to_parse.startswith('<'):
                logger.info("检测到非XML格式的自定义内容，正在尝试从纯文本格式转换...")
                content_to_parse = _convert_text_danmaku_to_xml(content_to_parse)
            await progress_callback(20, "正在解析XML文件...")
            cleaned_content = clean_xml_string(content_to_parse)
            comments = _parse_xml_content(cleaned_content)
            if not comments:
                raise TaskSuccess("未从XML中解析出任何弹幕。")
            
            await progress_callback(80, "正在写入数据库...")
            # 用户级配额：自定义XML导入也计入用户用量
            if user_id:
                try:
                    user_full = await crud.get_user_full_by_id(session, user_id)
                    await crud.check_and_increment_user_rate(session, user_full or {'id': user_id})
                except PermissionError:
                    seconds = await crud.get_user_rate_seconds_until_reset(session, user_id)
                    raise TaskSuccess(f"达到用户每小时配额，请在 {seconds:.0f} 秒后重试。")
            final_title = title if title else f"第 {episodeIndex} 集"
            episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, episodeIndex, final_title, "from_xml", "custom_xml")
            added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
            await session.commit()
            raise TaskSuccess(f"手动导入完成，从XML新增 {added_count} 条弹幕。")

        # Case 2: Scraper source with URL
        scraper = manager.get_scraper(providerName)
        if not hasattr(scraper, 'get_id_from_url'):
            raise NotImplementedError(f"搜索源 '{providerName}' 不支持从URL手动导入。")

        provider_episode_id = await scraper.get_id_from_url(content)
        if not provider_episode_id:
            raise ValueError(f"无法从URL '{content}' 中解析出有效的视频ID。")

        episode_id_for_comments = scraper.format_episode_id_for_comments(provider_episode_id)
        await progress_callback(20, f"已解析视频ID: {episode_id_for_comments}")

        # Auto-generate title if not provided
        final_title = title
        if not final_title:
            if hasattr(scraper, 'get_title_from_url'):
                try:
                    final_title = await scraper.get_title_from_url(content)
                except Exception:
                    pass # Ignore errors, fallback to default
            if not final_title:
                final_title = f"第 {episodeIndex} 集"

        try:
            await rate_limiter.check(providerName)
        except RateLimitExceededError as e:
            raise TaskSuccess(f"达到速率限制。请在 {e.retry_after_seconds:.0f} 秒后重试。")

        comments = await scraper.get_comments(episode_id_for_comments, progress_callback=progress_callback)
        if not comments:
            raise TaskSuccess("未找到任何弹幕。")

        await rate_limiter.increment(providerName)

        await progress_callback(90, "正在写入数据库...")
        # 用户级配额：手动URL导入也计入用户用量
        if user_id:
            try:
                user_full = await crud.get_user_full_by_id(session, user_id)
                await crud.check_and_increment_user_rate(session, user_full or {'id': user_id})
            except PermissionError:
                seconds = await crud.get_user_rate_seconds_until_reset(session, user_id)
                raise TaskSuccess(f"达到用户每小时配额，请在 {seconds:.0f} 秒后重试。")
        episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, episodeIndex, final_title, content, episode_id_for_comments)
        added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
        await session.commit()
        raise TaskSuccess(f"手动导入完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        raise
    except Exception as e:
        logger.error(f"手动导入任务失败: {e}", exc_info=True)
        raise

async def run_webhook_tasks_directly_manual(
    session: AsyncSession,
    task_ids: List[int],
    task_manager: "TaskManager",
    scraper_manager: "ScraperManager",
    metadata_manager: "MetadataSourceManager",
    config_manager: "ConfigManager",
    rate_limiter: "RateLimiter"
) -> int:
    """直接获取并执行指定的待处理Webhook任务。"""
    if not task_ids:
        return 0

    stmt = select(orm_models.WebhookTask).where(orm_models.WebhookTask.id.in_(task_ids), orm_models.WebhookTask.status == "pending")
    tasks_to_run = (await session.execute(stmt)).scalars().all()

    submitted_count = 0
    for task in tasks_to_run:
        try:
            payload = json.loads(task.payload)
            task_coro = lambda s, cb: webhook_search_and_dispatch_task(
                webhookSource=task.webhookSource, progress_callback=cb, session=s,
                manager=scraper_manager, task_manager=task_manager,
                metadata_manager=metadata_manager, config_manager=config_manager,
                rate_limiter=rate_limiter, **payload
            )
            await task_manager.submit_task(task_coro, task.taskTitle, unique_key=task.uniqueKey)
            await session.delete(task)
            await session.commit()  # 为每个成功提交的任务单独提交删除操作
            submitted_count += 1
        except Exception as e:
            logger.error(f"手动执行 Webhook 任务 (ID: {task.id}) 时失败: {e}", exc_info=True)
            await session.rollback()
    return submitted_count

def _is_movie_by_title(title: str) -> bool:
    """
    通过标题中的关键词（如“剧场版”）判断是否为电影。
    """
    if not title:
        return False
    # 关键词列表，不区分大小写
    movie_keywords = ["剧场版", "劇場版", "movie", "映画"]
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in movie_keywords)


async def webhook_search_and_dispatch_task(
    animeTitle: str,
    mediaType: str,
    season: int,
    currentEpisodeIndex: int,
    searchKeyword: str,
    doubanId: Optional[str],
    tmdbId: Optional[str],
    imdbId: Optional[str],
    tvdbId: Optional[str],
    bangumiId: Optional[str],
    webhookSource: str,
    year: Optional[int],
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager,
    task_manager: TaskManager, # type: ignore
    metadata_manager: MetadataSourceManager,
    config_manager: ConfigManager,
    rate_limiter: RateLimiter
):
    """
    Webhook 触发的后台任务：搜索所有源，找到最佳匹配，并为该匹配分发一个新的、具体的导入任务。
    """
    try:
        logger.info(f"Webhook 任务: 开始为 '{animeTitle}' (S{season:02d}E{currentEpisodeIndex:02d}) 查找最佳源...")
        progress_callback(5, "正在检查已收藏的源...")

        # 1. 优先查找已收藏的源 (Favorited Source)
        existing_anime = await crud.find_anime_by_title_and_season(session, animeTitle, season)
        if existing_anime:
            anime_id = existing_anime['id']
            favorited_source = await crud.find_favorited_source_for_anime(session, anime_id)
            if favorited_source:
                logger.info(f"Webhook 任务: 找到已收藏的源 '{favorited_source['providerName']}'，将直接使用此源。")
                progress_callback(10, f"找到已收藏的源: {favorited_source['providerName']}")

                task_title = f"Webhook自动导入: {favorited_source['animeTitle']} - S{season:02d}E{currentEpisodeIndex:02d} ({favorited_source['providerName']})"
                unique_key = f"import-{favorited_source['providerName']}-{favorited_source['mediaId']}-ep{currentEpisodeIndex}"
                task_coro = lambda session, cb: generic_import_task(
                    provider=favorited_source['providerName'], mediaId=favorited_source['mediaId'], animeTitle=favorited_source['animeTitle'], year=year,
                    mediaType=favorited_source['mediaType'], season=season, currentEpisodeIndex=currentEpisodeIndex,
                    imageUrl=favorited_source['imageUrl'], doubanId=doubanId, tmdbId=tmdbId, imdbId=imdbId, tvdbId=tvdbId, config_manager=config_manager, metadata_manager=metadata_manager,
                    bangumiId=bangumiId, rate_limiter=rate_limiter,
                    progress_callback=cb, session=session, manager=manager,
                    task_manager=task_manager
                )
                await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
                raise TaskSuccess(f"Webhook: 已为收藏源 '{favorited_source['providerName']}' 创建导入任务。")

        # 2. 如果没有收藏源，则并发搜索所有启用的源
        logger.info(f"Webhook 任务: 未找到收藏源，开始并发搜索所有启用的源...")
        progress_callback(20, "并发搜索所有源...")

        parsed_keyword = parse_search_keyword(searchKeyword)
        search_title_only = parsed_keyword["title"]
        logger.info(f"Webhook 任务: 已将搜索词 '{searchKeyword}' 解析为标题 '{search_title_only}' 进行搜索。")

        all_search_results = await manager.search_all(
            [search_title_only], episode_info={"season": season, "episode": currentEpisodeIndex}
        )

        if not all_search_results:
            raise ValueError(f"未找到 '{animeTitle}' 的任何可用源。")

        # 3. 从所有源的返回结果中，根据类型、季度和标题相似度选择最佳匹配项
        ordered_settings = await crud.get_all_scraper_settings(session)
        provider_order = {s['providerName']: s['displayOrder'] for s in ordered_settings}

        valid_candidates = [item for item in all_search_results if (item.type == mediaType) and ((item.season == season) if mediaType == 'tv_series' else True)]

        if not valid_candidates:
            raise ValueError(f"未找到 '{animeTitle}' 的精确匹配项。")

        valid_candidates.sort(key=lambda item: (fuzz.token_set_ratio(animeTitle, item.title), -provider_order.get(item.provider, 999)), reverse=True)
        best_match = valid_candidates[0]

        logger.info(f"Webhook 任务: 在所有源中找到最佳匹配项 '{best_match.title}' (来自: {best_match.provider})，将为其创建导入任务。")
        progress_callback(50, f"在 {best_match.provider} 中找到最佳匹配项")

        current_time = get_now().strftime("%H:%M:%S")
        task_title = f"Webhook（{webhookSource}）自动导入：{best_match.title} - S{season:02d}E{currentEpisodeIndex:02d} ({best_match.provider}) [{current_time}]" if mediaType == "tv_series" else f"Webhook（{webhookSource}）自动导入：{best_match.title} ({best_match.provider}) [{current_time}]"
        unique_key = f"import-{best_match.provider}-{best_match.mediaId}-ep{currentEpisodeIndex}"
        task_coro = lambda session, cb: generic_import_task(
            provider=best_match.provider, mediaId=best_match.mediaId, year=year,
            animeTitle=best_match.title, mediaType=best_match.type,
            season=best_match.season, currentEpisodeIndex=currentEpisodeIndex, imageUrl=best_match.imageUrl, config_manager=config_manager, metadata_manager=metadata_manager,
            doubanId=doubanId, tmdbId=tmdbId, imdbId=imdbId, tvdbId=tvdbId, bangumiId=bangumiId, rate_limiter=rate_limiter,
            progress_callback=cb, session=session, manager=manager,
            task_manager=task_manager
        )
        await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        raise TaskSuccess(f"Webhook: 已为源 '{best_match.provider}' 创建导入任务。")
    except TaskSuccess:
        raise
    except Exception as e:
        logger.error(f"Webhook 搜索与分发任务发生严重错误: {e}", exc_info=True)
        raise

async def batch_manual_import_task(
    sourceId: int, animeId: int, providerName: str, items: List[models.BatchManualImportItem],
    progress_callback: Callable, session: AsyncSession, manager: ScraperManager, rate_limiter: RateLimiter
):
    """后台任务：批量手动导入弹幕。"""
    total_items = len(items)
    logger.info(f"开始批量手动导入任务: sourceId={sourceId}, provider='{providerName}', items={total_items}")
    await progress_callback(5, f"准备批量导入 {total_items} 个条目...")

    total_added_comments = 0
    failed_items = 0
    skipped_items = 0

    i = 0
    while i < total_items:
        item = items[i]
        progress = 5 + int(((i + 1) / total_items) * 90) if total_items > 0 else 95
        # 修正：使用 getattr 安全地访问可能不存在的 'title' 属性，
        # 以修复当请求体中的项目不包含 title 字段时引发的 AttributeError。
        # 这提供了向后兼容性，并使 title 字段成为可选。
        item_desc = getattr(item, 'title', None) or f"第 {item.episodeIndex} 集"
        await progress_callback(progress, f"正在处理: {item_desc} ({i+1}/{total_items})")

        try:
            if providerName == 'custom':
                # 新增：在处理前，先检查分集是否已存在
                existing_episode_stmt = select(orm_models.Episode.id).where(
                    orm_models.Episode.sourceId == sourceId,
                    orm_models.Episode.episodeIndex == item.episodeIndex
                )
                existing_episode_res = await session.execute(existing_episode_stmt)
                if existing_episode_res.scalar_one_or_none() is not None:
                    logger.warning(f"批量导入条目 '{item_desc}' (集数: {item.episodeIndex}) 已存在，已跳过。")
                    skipped_items += 1
                    i += 1
                    continue

                content_to_parse = item.content.strip()
                if not content_to_parse.startswith('<'):
                    logger.info(f"批量导入条目 '{item_desc}' 检测到非XML格式，正在尝试从纯文本格式转换...")
                    content_to_parse = _convert_text_danmaku_to_xml(content_to_parse)

                cleaned_content = clean_xml_string(content_to_parse)
                comments = _parse_xml_content(cleaned_content)
                
                if comments:
                    final_title = getattr(item, 'title', None) or f"第 {item.episodeIndex} 集"
                    episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, item.episodeIndex, final_title, "from_xml_batch", "custom_xml")

                    added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
                    total_added_comments += added_count
                else:
                    logger.warning(f"批量导入条目 '{item_desc}' 解析失败或不含弹幕，已跳过。")
                    failed_items += 1
            else:
                scraper = manager.get_scraper(providerName)
                provider_episode_id = await scraper.get_id_from_url(item.content)
                if not provider_episode_id: raise ValueError("无法解析ID")
                episode_id_for_comments = scraper.format_episode_id_for_comments(provider_episode_id)
                final_title = getattr(item, 'title', None) or f"第 {item.episodeIndex} 集"
                
                await rate_limiter.check(providerName)
                comments = await scraper.get_comments(episode_id_for_comments)
                
                if comments:
                    await rate_limiter.increment(providerName)
                    episode_db_id = await crud.create_episode_if_not_exists(session, animeId, sourceId, item.episodeIndex, final_title, item.content, episode_id_for_comments)
                    added_count = await crud.save_danmaku_for_episode(session, episode_db_id, comments)
                    total_added_comments += added_count
            
            await session.commit()
            i += 1 # 成功处理，移动到下一个
        except RateLimitExceededError as e:
            logger.warning(f"批量导入任务因达到速率限制而暂停: {e}")
            await progress_callback(progress, f"速率受限，将在 {e.retry_after_seconds:.0f} 秒后自动重试...", status=TaskStatus.PAUSED)
            await asyncio.sleep(e.retry_after_seconds)
            continue # 不增加 i，以便重试当前条目
        except Exception as e:
            logger.error(f"处理批量导入条目 '{item_desc}' 时失败: {e}", exc_info=True)
            failed_items += 1
            await session.rollback()
            i += 1 # 处理失败，移动到下一个
    
    final_message = f"批量导入完成。共处理 {total_items} 个条目，新增 {total_added_comments} 条弹幕。"
    if skipped_items > 0:
        final_message += f" {skipped_items} 个因已存在而被跳过。"
    if failed_items > 0:
        final_message += f" {failed_items} 个条目处理失败。"
    raise TaskSuccess(final_message)

async def auto_search_and_import_task(
    payload: "models.ControlAutoImportRequest",
    progress_callback: Callable,
    session: AsyncSession,
    config_manager: ConfigManager,
    scraper_manager: ScraperManager,
    metadata_manager: MetadataSourceManager,
    task_manager: TaskManager,
    rate_limiter: Optional[RateLimiter] = None,
    api_key: Optional[str] = None,
    user_id: Optional[int] = None,
):
    """
    全自动搜索并导入的核心任务逻辑。
    """
    try:
        # 防御性检查：确保 rate_limiter 已被正确传递。
        if rate_limiter is None:
            error_msg = "任务启动失败：内部错误（速率限制器未提供）。请检查任务提交处的代码。"
            logger.error(f"auto_search_and_import_task was called without a rate_limiter. This is a bug. Payload: {payload}")
            raise ValueError(error_msg)

        search_type = payload.searchType
        search_term = payload.searchTerm
        media_type = payload.mediaType
        season = payload.season
        
        await progress_callback(5, f"开始处理，类型: {search_type}, 搜索词: {search_term}")

        aliases = {search_term}
        main_title = search_term
        image_url = None
        tmdb_id, bangumi_id, douban_id, tvdb_id, imdb_id = None, None, None, None, None

        # 为后台任务创建一个虚拟用户对象
        user = models.User(id=1, username="admin")

        # 1. 获取元数据和别名
        details: Optional[models.MetadataDetailsResponse] = None
        
        # 智能检测：如果 searchType 是 keyword 但 searchTerm 是数字，则尝试将其作为 TMDB ID 处理
        effective_search_type = search_type.value
        if search_type == "keyword" and search_term.isdigit():
            logger.info(f"检测到关键词 '{search_term}' 为数字，将尝试作为TMDB ID进行元数据获取...")
            effective_search_type = "tmdb"

        if effective_search_type != "keyword":
            provider_media_type = None
            if media_type:
                if effective_search_type == 'tmdb':
                    provider_media_type = 'tv' if media_type == 'tv_series' else 'movie'
                elif effective_search_type == 'tvdb':
                    provider_media_type = 'series' if media_type == 'tv_series' else 'movies'

            try:
                await progress_callback(10, f"正在从 {effective_search_type.upper()} 获取元数据...")
                
                # --- 修正：当 mediaType 未提供时，智能地尝试两种类型 ---
                provider_media_type_to_try = None
                if media_type:
                    if effective_search_type == 'tmdb':
                        provider_media_type_to_try = 'tv' if media_type == 'tv_series' else 'movie'
                    elif effective_search_type == 'tvdb':
                        provider_media_type_to_try = 'series' if media_type == 'tv_series' else 'movies'

                if provider_media_type_to_try:
                    details = await metadata_manager.get_details(
                        provider=effective_search_type, item_id=search_term, user=user, mediaType=provider_media_type_to_try
                    )
                else:
                    # 如果无法推断，则依次尝试 TV 和 Movie
                    logger.info(f"未提供 mediaType，将依次尝试 TV 和 Movie 类型...")
                    tv_type = 'tv' if effective_search_type == 'tmdb' else 'series'
                    details = await metadata_manager.get_details(provider=effective_search_type, item_id=search_term, user=user, mediaType=tv_type)
                    if not details:
                        logger.info(f"作为 TV/Series 未找到，正在尝试作为 Movie...")
                        movie_type = 'movie' if effective_search_type == 'tmdb' else 'movies'
                        details = await metadata_manager.get_details(provider=effective_search_type, item_id=search_term, user=user, mediaType=movie_type)
                # --- 修正结束 ---
                if not details and search_type == "keyword":
                    logger.info(f"作为TMDB ID获取元数据失败，将按原样作为关键词处理。")
            except Exception as e:
                logger.error(f"从 {effective_search_type.upper()} 获取元数据失败: {e}\n{traceback.format_exc()}")
                if search_type == "keyword":
                    logger.warning(f"尝试将关键词作为TMDB ID处理时出错，将按原样作为关键词处理。")

        if details:
            main_title = details.title or main_title
            image_url = details.imageUrl
            aliases.add(main_title)
            aliases.update(details.aliasesCn or [])
            aliases.add(details.nameEn)
            aliases.add(details.nameJp)
            tmdb_id, bangumi_id, douban_id, tvdb_id, imdb_id = (
                details.tmdbId, details.bangumiId, details.doubanId,
                details.tvdbId, details.imdbId
            )
            if hasattr(details, 'type') and details.type:
                media_type = details.type
            
            logger.info(f"正在为 '{main_title}' 从其他源获取更多别名...")
            enriched_aliases = await metadata_manager.search_aliases_from_enabled_sources(main_title, user)
            if enriched_aliases:
                aliases.update(enriched_aliases)
                logger.info(f"别名已扩充: {aliases}")

        # 2. 检查媒体库中是否已存在
        existing_anime: Optional[Dict[str, Any]] = None
        await progress_callback(20, "正在检查媒体库...")
        
        # 步骤 2a: 优先通过元数据ID和季度号进行精确查找
        if search_type != "keyword" and season is not None:
            id_column_map = {
                "tmdb": "tmdbId", "tvdb": "tvdbId", "imdb": "imdbId",
                "douban": "doubanId", "bangumi": "bangumiId"
            }
            id_type = id_column_map.get(search_type.value)
            if id_type:
                logger.info(f"正在通过 {search_type.upper()} ID '{search_term}' 和季度 {season} 精确查找...")
                existing_anime = await crud.find_anime_by_metadata_id_and_season(
                    session, id_type, search_term, season
                )
                if existing_anime:
                    logger.info(f"精确查找到已存在的作品: {existing_anime['title']} (ID: {existing_anime['id']})")

        # 步骤 2b: 如果精确查找未找到，则回退到按标题和季度查找
        if not existing_anime:
            if search_type != "keyword":
                logger.info("通过元数据ID+季度未找到匹配项，回退到按标题查找...")
            # 如果通过ID未找到，或不是按ID搜索，则回退到按标题和季度查找
            existing_anime = await crud.find_anime_by_title_and_season(session, main_title, season)

        if existing_anime:
            # 修正：从 existing_anime 字典中安全地获取ID。
            # 不同的查询路径可能返回 'id' 或 'animeId' 作为键。
            # 此更改确保无论哪个键存在，我们都能正确获取ID。
            anime_id_to_use = existing_anime.get('id') or existing_anime.get('animeId')
            if not anime_id_to_use:
                raise ValueError("在已存在的作品记录中未能找到有效的ID。")

            favorited_source = await crud.find_favorited_source_for_anime(session, anime_id_to_use)
            if favorited_source:
                source_to_use = favorited_source
                logger.info(f"媒体库中已存在作品，并找到精确标记源: {source_to_use['providerName']}")
            else:
                all_sources = await crud.get_anime_sources(session, anime_id_to_use)
                if all_sources:
                    ordered_settings = await crud.get_all_scraper_settings(session)
                    provider_order = {s['providerName']: s['displayOrder'] for s in ordered_settings}
                    all_sources.sort(key=lambda s: provider_order.get(s['providerName'], 999))
                    source_to_use = all_sources[0]
                    logger.info(f"媒体库中已存在作品，选择优先级最高的源: {source_to_use['providerName']}")
                else: source_to_use = None
            
            if source_to_use:
                await progress_callback(30, f"已存在，使用源: {source_to_use['providerName']}")
                # 修正：在unique_key中包含season和episode信息，避免重复任务检测问题
                unique_key_parts = ["import", source_to_use['providerName'], source_to_use['mediaId']]
                if season is not None:
                    unique_key_parts.append(f"s{season}")
                if payload.episode is not None:
                    unique_key_parts.append(f"e{payload.episode}")
                unique_key = "-".join(unique_key_parts)
                task_coro = lambda s, cb: generic_import_task(
                provider=source_to_use['providerName'], mediaId=source_to_use['mediaId'], config_manager=config_manager,
                    animeTitle=main_title, mediaType=media_type, season=season,
                    year=source_to_use.get('year'), currentEpisodeIndex=payload.episode, imageUrl=image_url,
                    metadata_manager=metadata_manager,
                    doubanId=douban_id, tmdbId=tmdb_id, imdbId=imdb_id, tvdbId=tvdb_id, bangumiId=bangumi_id,
                    progress_callback=cb, session=s, manager=scraper_manager, task_manager=task_manager,
                    rate_limiter=rate_limiter
                )
                # 修正：提交执行任务，并将其ID作为调度任务的结果
                # 修正：为任务标题添加季/集信息，以确保其唯一性，防止因任务名重复而提交失败。
                title_parts = [f"自动导入 (库内): {main_title}"]
                if payload.season is not None:
                    title_parts.append(f"S{payload.season:02d}")
                if payload.episode is not None:
                    title_parts.append(f"E{payload.episode:02d}")
                task_title = " ".join(title_parts)

                execution_task_id, _ = await task_manager.submit_task(
                    task_coro, 
                    task_title, 
                    unique_key=unique_key
                )
                final_message = f"作品已在库中，已为已有源创建导入任务。执行任务ID: {execution_task_id}"
                raise TaskSuccess(final_message)

        # 3. 如果库中不存在，则进行全网搜索
        await progress_callback(40, "媒体库未找到，开始全网搜索...")
        episode_info = {"season": season, "episode": payload.episode} if payload.episode else {"season": season}
        
        # 使用主标题进行搜索
        logger.info(f"将使用主标题 '{main_title}' 进行全网搜索...")
        all_results = await scraper_manager.search_all([main_title], episode_info=episode_info)
        logger.info(f"直接搜索完成，找到 {len(all_results)} 个原始结果。")

        # 使用所有别名进行过滤
        def normalize_for_filtering(title: str) -> str:
            if not title: return ""
            title = re.sub(r'[\[【(（].*?[\]】)）]', '', title)
            return title.lower().replace(" ", "").replace("：", ":").strip()

        normalized_filter_aliases = {normalize_for_filtering(alias) for alias in aliases if alias}
        filtered_results = []
        for item in all_results:
            normalized_item_title = normalize_for_filtering(item.title)
            if not normalized_item_title: continue
            # 修正：使用更智能的模糊匹配来提高准确率，与UI搜索逻辑保持一致
            if any(fuzz.partial_ratio(normalized_item_title, alias) > 85 for alias in normalized_filter_aliases):
                filtered_results.append(item)
        logger.info(f"别名过滤: 从 {len(all_results)} 个原始结果中，保留了 {len(filtered_results)} 个相关结果。")
        all_results = filtered_results

        if not all_results:
            raise ValueError("全网搜索未找到任何结果。")

        # 4. 选择最佳源
        ordered_settings = await crud.get_all_scraper_settings(session)
        provider_order = {s['providerName']: s['displayOrder'] for s in ordered_settings}
        
        # 修正：使用更智能的排序逻辑来选择最佳匹配
        # 1. 媒体类型是否匹配 (最优先)
        # 2. 如果请求指定了季度，季度是否匹配 (次优先)
        # 3. 标题相似度
        # 4. 标题长度惩罚 (标题越长，越可能是特别篇，得分越低)
        # 5. 用户设置的源优先级 (最后)
        all_results.sort(
            key=lambda item: (
                1 if item.type == media_type else 0,
                1 if season is not None and item.season == season else 0,
                fuzz.token_set_ratio(main_title, item.title),
                -abs(len(item.title) - len(main_title)), # 惩罚标题长度差异大的结果
                -provider_order.get(item.provider, 999)
            ),
            reverse=True # 按得分从高到低排序
        )
        best_match = all_results[0]

        # 记录选择的最佳匹配项，以便调试
        logger.info(f"自动导入：经过排序后，选择的最佳匹配项为: '{best_match.title}' (Provider: {best_match.provider}, MediaID: {best_match.mediaId}, Season: {best_match.season})")

        await progress_callback(80, f"选择最佳源: {best_match.provider}")

        # 修正：如果初始搜索是基于关键词，我们没有预先获取元数据。
        # 在这种情况下，使用从搜索结果中找到的最佳匹配项的海报URL。
        if not image_url:
            image_url = best_match.imageUrl
            logger.info(f"使用最佳匹配源 '{best_match.provider}' 的海报URL: {image_url}")

        # 修正：在unique_key中包含season和episode信息，避免重复任务检测问题
        unique_key_parts = ["import", best_match.provider, best_match.mediaId]
        if season is not None:
            unique_key_parts.append(f"s{season}")
        if payload.episode is not None:
            unique_key_parts.append(f"e{payload.episode}")
        unique_key = "-".join(unique_key_parts)
        task_coro = lambda s, cb: generic_import_task(
            provider=best_match.provider, mediaId=best_match.mediaId,
            animeTitle=best_match.title, mediaType=best_match.type, season=best_match.season, year=best_match.year,
            config_manager=config_manager, metadata_manager=metadata_manager,
            currentEpisodeIndex=payload.episode, imageUrl=image_url, # 现在 imageUrl 已被正确填充
            doubanId=douban_id, tmdbId=tmdb_id, imdbId=imdb_id, tvdbId=tvdb_id, bangumiId=bangumi_id,
            progress_callback=cb, session=s, manager=scraper_manager, task_manager=task_manager,
            rate_limiter=rate_limiter, user_id=user_id
        )
        # 修正：提交执行任务，并将其ID作为调度任务的结果
        # 修正：为任务标题添加季/集信息，以确保其唯一性，防止因任务名重复而提交失败。
        title_parts = [f"自动导入 (库内): {main_title}"]
        if media_type == 'movie':
            # 对于电影，添加源和ID以确保唯一性，因为电影没有季/集
            if search_type != "keyword":
                title_parts.append(f"({payload.searchType.value}:{payload.searchTerm})")
        else:
            # 对于电视剧，添加季/集信息
            if payload.season is not None:
                title_parts.append(f"S{payload.season:02d}")
            if payload.episode is not None:
                title_parts.append(f"E{payload.episode:02d}")
        task_title = " ".join(title_parts)

        execution_task_id, _ = await task_manager.submit_task(
            task_coro, 
                    task_title, 
            unique_key=unique_key
        )
        final_message = f"已为最佳匹配源创建导入任务。执行任务ID: {execution_task_id}"
        raise TaskSuccess(final_message)
    finally:
        if api_key:
            await scraper_manager.release_search_lock(api_key)
            logger.info(f"自动导入任务已为 API key 释放搜索锁。")
