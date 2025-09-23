import re
from typing import Optional, List, Any, Dict, Callable, Union
import asyncio
import secrets
import hashlib
import importlib
import string
import time
from urllib.parse import urlparse, urlunparse, quote, unquote
import logging

from datetime import datetime, timedelta
from sqlalchemy import update, select, func, exc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import httpx
from ..rate_limiter import RateLimiter, RateLimitExceededError
from ..config_manager import ConfigManager
from pydantic import BaseModel, Field, model_validator
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status, Response
from fastapi.security import OAuth2PasswordRequestForm

from .. import crud, models, orm_models, security, scraper_manager
from src import models as api_models
from ..log_manager import get_logs
from ..task_manager import TaskManager, TaskSuccess, TaskStatus
from ..metadata_manager import MetadataSourceManager
from ..scraper_manager import ScraperManager
from .. import tasks
from ..utils import parse_search_keyword
from ..webhook_manager import WebhookManager
from ..image_utils import download_image
from ..scheduler import SchedulerManager
from thefuzz import fuzz
from ..config import settings
from ..timezone import get_now
from ..database import get_db_session

router = APIRouter()
auth_router = APIRouter()
logger = logging.getLogger(__name__)

class UITaskResponse(BaseModel):
    message: str
    taskId: str

async def get_scraper_manager(request: Request) -> ScraperManager:
    """依赖项：从应用状态获取 Scraper 管理器"""
    return request.app.state.scraper_manager

async def get_task_manager(request: Request) -> TaskManager:
    """依赖项：从应用状态获取任务管理器"""
    return request.app.state.task_manager

async def get_scheduler_manager(request: Request) -> SchedulerManager:
    """依赖项：从应用状态获取 Scheduler 管理器"""
    return request.app.state.scheduler_manager

async def get_webhook_manager(request: Request) -> WebhookManager:
    """依赖项：从应用状态获取 Webhook 管理器"""
    return request.app.state.webhook_manager

async def get_metadata_manager(request: Request) -> MetadataSourceManager:
    """依赖项：从应用状态获取元数据源管理器"""
    return request.app.state.metadata_manager

async def get_config_manager(request: Request) -> ConfigManager:
    """依赖项：从应用状态获取配置管理器"""
    return request.app.state.config_manager

async def get_rate_limiter(request: Request) -> RateLimiter:
    """依赖项：从应用状态获取速率限制器"""
    return request.app.state.rate_limiter

@router.get(
    "/search/anime",
    response_model=models.AnimeSearchResponse,
    summary="搜索本地数据库中的节目信息",
)
async def search_anime_local(
    keyword: str = Query(..., min_length=1, description="搜索关键词"),
    session: AsyncSession = Depends(get_db_session)
):
    db_results = await crud.search_anime(session, keyword)
    animes = [
        models.AnimeInfo(animeId=item["id"], animeTitle=item["title"], type=item["type"])
        for item in db_results
    ]
    return models.AnimeSearchResponse(animes=animes)

class UIProviderSearchResponse(models.ProviderSearchResponse):
    """扩展了 ProviderSearchResponse 以包含原始搜索的上下文。"""
    search_season: Optional[int] = None
    search_episode: Optional[int] = None
@router.get("/search/provider", response_model=UIProviderSearchResponse, summary="从外部数据源搜索节目")
async def search_anime_provider(
    keyword: str = Query(..., min_length=1, description="搜索关键词"),
    manager: ScraperManager = Depends(get_scraper_manager),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """
    从所有已配置的数据源（如腾讯、B站等）搜索节目信息。
    此接口实现了智能的按季缓存机制，并保留了原有的别名搜索、过滤和排序逻辑。
    """
    try:
        # 用户级流控检查与计数
        user_full = await crud.get_user_by_username_full(session, current_user.username)
        try:
            await crud.check_and_increment_user_rate(session, user_full or {'id': current_user.id})
        except PermissionError:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="User rate limit exceeded")
        # 增加用户搜索计数
        await crud.increment_user_metric(session, (user_full or {'id': current_user.id})['id'], 'search_count', 1)
        parsed_keyword = parse_search_keyword(keyword)
        search_title = parsed_keyword["title"]
        season_to_filter = parsed_keyword["season"]
        episode_to_filter = parsed_keyword["episode"]

        # --- 新增：按季缓存逻辑 ---
        # 缓存键基于核心标题和季度，允许在同一季的不同分集搜索中复用缓存
        cache_key = f"provider_search_{search_title}_{season_to_filter or 'all'}"
        cached_results_data = await crud.get_cache(session, cache_key)

        if cached_results_data is not None:
            logger.info(f"搜索缓存命中: '{cache_key}'")
            # 缓存数据已排序和过滤，只需更新当前请求的集数信息
            results = [models.ProviderSearchInfo.model_validate(item) for item in cached_results_data]
            for item in results:
                item.currentEpisodeIndex = episode_to_filter
            
            return UIProviderSearchResponse(
                results=results,
                search_season=season_to_filter,
                search_episode=episode_to_filter
            )
        
        logger.info(f"搜索缓存未命中: '{cache_key}'，正在执行完整搜索流程...")
        # --- 缓存逻辑结束 ---

        episode_info = {
            "season": season_to_filter,
            "episode": episode_to_filter
        } if episode_to_filter is not None else None

        logger.info(f"用户 '{current_user.username}' 正在搜索: '{keyword}' (解析为: title='{search_title}', season={season_to_filter}, episode={episode_to_filter})")
        if not manager.has_enabled_scrapers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="没有启用的弹幕搜索源，请在“搜索源”页面中启用至少一个。"
            )

        # --- 原有的复杂搜索流程开始 ---
        tmdb_api_key = await crud.get_config_value(session, "tmdb_api_key", "")
        enabled_aux_sources = await crud.get_enabled_aux_metadata_sources(session)

        if not enabled_aux_sources or (len(enabled_aux_sources) == 1 and enabled_aux_sources[0]['providerName'] == 'tmdb' and not tmdb_api_key):
            logger.info("未配置或未启用任何有效的辅助搜索源，直接进行全网搜索。")
            results = await manager.search_all([search_title], episode_info=episode_info)
            logger.info(f"直接搜索完成，找到 {len(results)} 个原始结果。")
        else:
            logger.info("一个或多个元数据源已启用辅助搜索，开始执行...")
            # 修正：增加一个“防火墙”来验证从元数据源返回的别名，防止因模糊匹配导致的结果污染。
            # 1. 获取所有可能的别名
            all_possible_aliases = await metadata_manager.search_aliases_from_enabled_sources(search_title, current_user)
            
            # 2. 验证每个别名与原始搜索词的相似度
            validated_aliases = set()
            for alias in all_possible_aliases:
                # 使用 token_set_ratio 并设置一个合理的阈值（例如70），以允许小的差异但过滤掉完全不相关的结果。
                if fuzz.token_set_ratio(search_title, alias) > 70:
                    validated_aliases.add(alias)
                else:
                    logger.debug(f"别名验证：已丢弃低相似度的别名 '{alias}' (与 '{search_title}' 相比)")
            
            # 3. 使用经过验证的别名列表进行后续操作
            filter_aliases = validated_aliases
            filter_aliases.add(search_title) # 确保原始搜索词总是在列表中
            logger.info(f"所有辅助搜索完成，最终别名集大小: {len(filter_aliases)}")

            # 新增：根据您的要求，打印最终的别名列表以供调试
            logger.info(f"用于过滤的别名列表: {list(filter_aliases)}")

            logger.info(f"将使用解析后的标题 '{search_title}' 进行全网搜索...")
            all_results = await manager.search_all([search_title], episode_info=episode_info)

            def normalize_for_filtering(title: str) -> str:
                if not title: return ""
                title = re.sub(r'[\[【(（].*?[\]】)）]', '', title)
                return title.lower().replace(" ", "").replace("：", ":").strip()

            # 修正：采用更智能的两阶段过滤策略
            # 阶段1：基于原始搜索词进行初步、宽松的过滤，以确保所有相关系列（包括不同季度和剧场版）都被保留。
            # 只有当用户明确指定季度时，我们才进行更严格的过滤。
            normalized_filter_aliases = {normalize_for_filtering(alias) for alias in filter_aliases if alias}
            filtered_results = []
            for item in all_results:
                normalized_item_title = normalize_for_filtering(item.title)
                if not normalized_item_title: continue
                
                # 检查搜索结果是否与任何一个别名匹配
                # token_set_ratio 擅长处理单词顺序不同和部分单词匹配的情况。
                # 修正：使用 partial_ratio 来更好地匹配续作和外传 (e.g., "刀剑神域" vs "刀剑神域外传")
                # 85 的阈值可以在保留强相关的同时，过滤掉大部分无关结果。
                if any(fuzz.partial_ratio(normalized_item_title, alias) > 85 for alias in normalized_filter_aliases):
                    filtered_results.append(item)

            logger.info(f"别名过滤: 从 {len(all_results)} 个原始结果中，保留了 {len(filtered_results)} 个相关结果。")
            results = filtered_results
    except httpx.RequestError as e:
        error_message = f"搜索 '{keyword}' 时发生网络错误: {e}"
        logger.error(error_message, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error_message)

    # 辅助函数，用于根据标题修正媒体类型
    def is_movie_by_title(title: str) -> bool:
        if not title:
            return False
        # 关键词列表，不区分大小写
        movie_keywords = ["剧场版", "劇場版", "movie", "映画"]
        title_lower = title.lower()
        return any(keyword in title_lower for keyword in movie_keywords)

    # 新增逻辑：根据标题关键词修正媒体类型
    for item in results:
        if item.type == 'tv_series' and is_movie_by_title(item.title):
            logger.info(f"标题 '{item.title}' 包含电影关键词，类型从 'tv_series' 修正为 'movie'。")
            item.type = 'movie'

    # 如果用户在搜索词中明确指定了季度，则对结果进行过滤
    if season_to_filter:
        original_count = len(results)
        # 当指定季度时，我们只关心电视剧类型
        filtered_by_type = [item for item in results if item.type == 'tv_series']
        
        # 然后在电视剧类型中，我们按季度号过滤
        filtered_by_season = []
        for item in filtered_by_type:
            # 使用模型中已解析好的 season 字段进行比较
            if item.season == season_to_filter:
                filtered_by_season.append(item)
        
        logger.info(f"根据指定的季度 ({season_to_filter}) 进行过滤，从 {original_count} 个结果中保留了 {len(filtered_by_season)} 个。")
        results = filtered_by_season

    # 修正：在返回结果前，确保 currentEpisodeIndex 与本次请求的 episode_info 一致。
    # 这可以防止因缓存或其他原因导致的状态泄露。
    current_episode_index_for_this_request = episode_info.get("episode") if episode_info else None
    for item in results:
        item.currentEpisodeIndex = current_episode_index_for_this_request

    # 新增：根据搜索源的显示顺序和标题相似度对结果进行排序
    source_settings = await crud.get_all_scraper_settings(session)
    source_order_map = {s['providerName']: s['displayOrder'] for s in source_settings}

    def sort_key(item: models.ProviderSearchInfo):
        provider_order = source_order_map.get(item.provider, 999)
        # 使用 token_set_ratio 来获得更鲁棒的标题相似度评分
        similarity_score = fuzz.token_set_ratio(search_title, item.title)
        # 主排序键：源顺序（升序）；次排序键：相似度（降序）
        return (provider_order, -similarity_score)

    sorted_results = sorted(results, key=sort_key)

    # --- 新增：在返回前缓存最终结果 ---
    # 我们缓存的是整季的结果，所以在存入前清除特定集数的信息
    results_to_cache = []
    for item in sorted_results:
        item_copy = item.model_copy(deep=True)
        item_copy.currentEpisodeIndex = None
        results_to_cache.append(item_copy.model_dump())

    if sorted_results:
        await crud.set_cache(session, cache_key, results_to_cache, ttl_seconds=10800) # 缓存3小时
    # --- 缓存逻辑结束 ---

    return UIProviderSearchResponse(
        results=sorted_results,
        search_season=season_to_filter,
        search_episode=episode_to_filter
    )

@router.post("/library/anime", response_model=models.LibraryAnimeInfo, status_code=201, summary="创建自定义作品条目")
async def create_anime_entry(
    payload: models.AnimeCreate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """
    手动创建一个新的作品条目，不关联任何数据源。
    """
    try:
        new_anime = await crud.create_anime(session, payload)
        await session.commit()
        # We need to return a LibraryAnimeInfo, so we fetch the full details
        # with counts.
        details = await crud.get_library_anime_by_id(session, new_anime.id)
        if not details:
            # This should not happen, but as a fallback
            raise HTTPException(status_code=500, detail="创建作品后无法立即获取其信息。")
        return models.LibraryAnimeInfo.model_validate(details)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.get("/library/episodes-by-title", response_model=List[int], summary="根据作品标题获取已存在的分集序号")
async def get_existing_episode_indices(
    title: str = Query(..., description="要查询的作品标题"),
    season: Optional[int] = Query(None, description="要查询的季度号"),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """
    根据一个作品的标题和季度，查询弹幕库中该作品已存在的所有分集的序号列表。
    用于在“编辑导入”界面实现增量导入。
    """
    return await crud.get_episode_indices_by_anime_title(session, title, season=season)



@router.get("/search/episodes", response_model=List[models.ProviderEpisodeInfo], summary="获取搜索结果的分集列表")
async def get_episodes_for_search_result(
    provider: str = Query(...),
    media_id: str = Query(...),
    media_type: Optional[str] = Query(None), # Pass media_type to help scraper
    manager: ScraperManager = Depends(get_scraper_manager),
    current_user: models.User = Depends(security.get_current_user)
):
    """为指定的搜索结果获取完整的分集列表。"""
    try:
        scraper = manager.get_scraper(provider)
        # 将 db_media_type 传递给 get_episodes 以帮助需要它的刮削器（如 mgtv）
        episodes = await scraper.get_episodes(media_id, db_media_type=media_type)
        return episodes
    except httpx.RequestError as e:
        # 新增：捕获网络错误
        error_message = f"从 {provider} 获取分集列表时发生网络错误: {e}"
        logger.error(f"获取分集列表失败 (provider={provider}, media_id={media_id}): {error_message}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error_message)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"获取分集列表失败 (provider={provider}, media_id={media_id}): {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取分集列表失败。")


@router.get("/library", response_model=models.LibraryResponse, summary="获取媒体库内容")
async def get_library(
    keyword: Optional[str] = Query(None, description="按标题搜索"),
    page: int = Query(1, ge=1, description="页码"),
    pageSize: int = Query(10, ge=1, description="每页数量"),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """获取数据库中所有已收录的番剧信息，支持搜索和分页。"""
    paginated_result = await crud.get_library_anime(session, keyword=keyword, page=page, page_size=pageSize)
    return models.LibraryResponse(
        total=paginated_result["total"],
        list=[models.LibraryAnimeInfo.model_validate(item) for item in paginated_result["list"]]
    )

@router.get("/library/anime/{animeId}/details", response_model=models.AnimeFullDetails, summary="获取影视完整详情")
async def get_anime_full_details(
    animeId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """获取指定番剧的完整信息，包括所有元数据ID。"""
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to view library details")
    details = await crud.get_anime_full_details(session, animeId)
    if not details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anime not found")
    
    # 修正：如果 crud 函数没有返回年份，手动从 Anime 表中获取并添加到响应中
    # 这确保了即使在 `get_anime_full_details` 的实现中忘记包含年份，API也能正确返回。
    if 'year' not in details or details.get('year') is None:
        anime_record = await session.get(orm_models.Anime, animeId)
        if anime_record:
            details['year'] = anime_record.year

    return models.AnimeFullDetails.model_validate(details)

@router.put("/library/anime/{animeId}", status_code=status.HTTP_204_NO_CONTENT, summary="编辑影视信息")
async def edit_anime_info(
    animeId: int,
    update_data: models.AnimeDetailUpdate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """更新指定番剧的标题、季度和元数据。"""
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to edit library")
    updated = await crud.update_anime_details(session, animeId, update_data)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品未找到或更新失败")
    logger.info(f"用户 '{current_user.username}' 更新了番剧 ID: {animeId} 的详细信息。")

    # 新增：如果提供了TMDB ID和剧集组ID，则更新映射表
    if update_data.tmdbId and update_data.tmdbEpisodeGroupId:
        logger.info(f"检测到TMDB ID和剧集组ID，开始更新映射表...")
        try:
            await metadata_manager.update_tmdb_mappings(
                tmdb_tv_id=int(update_data.tmdbId),
                group_id=update_data.tmdbEpisodeGroupId,
                user=current_user
            )
        except Exception as e:
            # 仅记录错误，不中断主流程，因为核心信息已保存
            logger.error(f"更新TMDB映射失败: {e}", exc_info=True)
    return

class RefreshPosterRequest(models.BaseModel):
    imageUrl: str

@router.post("/library/anime/{animeId}/refresh-poster", summary="刷新并缓存影视海报")
async def refresh_anime_poster(
    animeId: int,
    request_data: RefreshPosterRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """根据提供的URL，重新下载并缓存海报，更新数据库记录。"""
    new_local_path = await download_image(request_data.imageUrl, session, scraper_manager)
    if not new_local_path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="图片下载失败，请检查URL或服务器日志。")

    stmt = update(orm_models.Anime).where(orm_models.Anime.id == animeId).values(imageUrl=request_data.imageUrl, localImagePath=new_local_path)
    result = await session.execute(stmt)
    await session.commit()
    affected_rows = result.rowcount

    if affected_rows == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="作品未找到。")
    return {"new_path": new_local_path}

@router.post("/library/anime/{animeId}/sources", response_model=models.SourceInfo, status_code=201, summary="为作品新增数据源")
async def add_source_to_anime(
    animeId: int,
    payload: models.SourceCreate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """
    为一个已存在的作品手动关联一个新的数据源。
    """
    anime = await crud.get_anime_full_details(session, animeId)
    if not anime:
        raise HTTPException(status_code=404, detail="作品未找到")

    try:
        source_id = await crud.link_source_to_anime(session, animeId, payload.providerName, payload.mediaId)
        await session.commit()
        all_sources = await crud.get_anime_sources(session, animeId)
        newly_created_source = next((s for s in all_sources if s['sourceId'] == source_id), None)
        if not newly_created_source:
            raise HTTPException(status_code=500, detail="创建数据源后无法立即获取其信息。")
        return models.SourceInfo.model_validate(newly_created_source)
    except exc.IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该数据源已存在于此作品下，无法重复添加。")

@router.get("/library/source/{sourceId}/details", response_model=models.SourceDetailsResponse, summary="获取单个数据源的详情")
async def get_source_details(
    sourceId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """获取指定数据源的详细信息，包括其提供方名称。"""
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return models.SourceDetailsResponse.model_validate(source_info)

class ReassociationRequest(models.BaseModel):
    targetAnimeId: int

@router.post("/library/anime/{sourceAnimeId}/reassociate", status_code=status.HTTP_204_NO_CONTENT, summary="重新关联作品的数据源")
async def reassociate_anime_sources(
    sourceAnimeId: int,
    request_data: ReassociationRequest = Body(...),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """将一个作品的所有数据源移动到另一个作品，并删除原作品。"""
    if sourceAnimeId == request_data.targetAnimeId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="源作品和目标作品不能相同。")

    success = await crud.reassociate_anime_sources(session, sourceAnimeId, request_data.targetAnimeId)
    if not success:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="源作品或目标作品未找到，或操作失败。")
    logger.info(f"用户 '{current_user.username}' 将作品 ID {sourceAnimeId} 的源关联到了 ID {request_data.targetAnimeId}。")
    return

@router.delete("/library/source/{sourceId}", status_code=status.HTTP_202_ACCEPTED, summary="提交删除指定数据源的任务")
async def delete_source_from_anime(
    sourceId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """提交一个后台任务来删除一个数据源及其所有关联的分集和弹幕。"""
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    task_title = f"删除源: {source_info['title']} ({source_info['providerName']})"
    unique_key = f"delete-source-{sourceId}"
    task_coro = lambda session, callback: tasks.delete_source_task(sourceId, session, callback)
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key, run_immediately=True)

    logger.info(f"用户 '{current_user.username}' 提交了删除源 ID: {sourceId} 的任务 (Task ID: {task_id})。")
    return {"message": f"删除源 '{source_info['providerName']}' 的任务已提交。", "taskId": task_id}

class BulkDeleteEpisodesRequest(models.BaseModel):
    episodeIds: List[int] = Field(..., alias="episode_ids")

    class Config:
        populate_by_name = True

@router.post("/library/episodes/delete-bulk", status_code=status.HTTP_202_ACCEPTED, summary="提交批量删除分集的任务", response_model=UITaskResponse)
async def delete_bulk_episodes(
    request_data: BulkDeleteEpisodesRequest,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """提交一个后台任务来批量删除多个分集。"""
    if not request_data.episodeIds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Episode IDs list cannot be empty.")

    task_title = f"批量删除 {len(request_data.episodeIds)} 个分集"
    ids_str = ",".join(sorted([str(eid) for eid in request_data.episodeIds]))
    unique_key = f"delete-bulk-episodes-{hashlib.md5(ids_str.encode('utf-8')).hexdigest()[:8]}"
    
    # 注意：这里我们将整个列表传递给任务
    task_coro = lambda session, callback: tasks.delete_bulk_episodes_task(request_data.episodeIds, session, callback)
    
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key, run_immediately=True)

    logger.info(f"用户 '{current_user.username}' 提交了批量删除 {len(request_data.episodeIds)} 个分集的任务 (Task ID: {task_id})。")
    return {"message": task_title + "的任务已提交。", "taskId": task_id}


@router.put("/library/source/{sourceId}/favorite", status_code=status.HTTP_204_NO_CONTENT, summary="切换数据源的精确标记状态")
async def toggle_source_favorite(
    sourceId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """切换指定数据源的精确标记状态。一个作品只能有一个精确标记的源。"""
    new_status = await crud.toggle_source_favorite_status(session, sourceId)
    if new_status is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return # 204 No Content, so no body is needed

@router.put("/library/source/{sourceId}/toggle-incremental-refresh", status_code=status.HTTP_204_NO_CONTENT, summary="切换数据源的定时增量更新状态")
async def toggle_source_incremental_refresh(
    sourceId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """切换指定数据源的定时增量更新的启用/禁用状态。"""
    toggled = await crud.toggle_source_incremental_refresh(session, sourceId)
    if not toggled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    logger.info(f"用户 '{current_user.username}' 切换了源 ID {sourceId} 的定时增量更新状态。")
@router.get("/library/anime/{animeId}/sources", response_model=List[models.SourceInfo], summary="获取作品的所有数据源")
async def get_anime_sources_for_anime(
    animeId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """获取指定作品关联的所有数据源列表。"""
    return await crud.get_anime_sources(session, animeId)

@router.get("/library/source/{sourceId}/episodes", response_model=models.PaginatedEpisodesResponse, summary="获取数据源的所有分集")
async def get_source_episodes(
    sourceId: int,
    page: int = Query(1, ge=1, description="页码"),
    pageSize: int = Query(25, ge=1, description="每页数量"),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """获取指定数据源下的所有已收录分集列表。"""
    paginated_result = await crud.get_episodes_for_source(session, sourceId, page, pageSize)
    # 修正：返回完整的分页响应对象
    return models.PaginatedEpisodesResponse(
        total=paginated_result["total"],
        list=paginated_result.get("episodes", [])
    )

@router.put("/library/episode/{episodeId}", status_code=status.HTTP_204_NO_CONTENT, summary="编辑分集信息")
async def edit_episode_info(
    episodeId: int,
    update_data: models.EpisodeInfoUpdate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """更新指定分集的标题、集数和链接。对于自定义源，链接是可选的。"""
    # 1. 获取分集及其源提供商信息
    # 修正：在更新前先获取分集信息，以进行条件验证
    episode_info = await crud.get_episode_provider_info(session, episodeId)
    if not episode_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    # 2. 根据源类型验证输入
    provider_name = episode_info.get("providerName")
    if provider_name != 'custom':
        # 对于非自定义源，链接是必需的
        if not update_data.sourceUrl:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="对于非自定义源，分集链接(sourceUrl)是必需的。"
            )

    # 3. 执行更新
    try:
        updated = await crud.update_episode_info(session, episodeId, update_data)
        if not updated:
            # This case might be redundant if get_episode_provider_info already confirmed existence, but it's safe.
            logger.warning(f"尝试更新一个不存在的分集 (ID: {episodeId})，操作被拒绝。")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
        logger.info(f"用户 '{current_user.username}' 更新了分集 ID: {episodeId} 的信息。")
        return
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.post("/library/source/{sourceId}/reorder-episodes", status_code=status.HTTP_202_ACCEPTED, summary="重整指定源的分集顺序")
async def reorder_source_episodes(
    sourceId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """提交一个后台任务，按当前顺序重新编号指定数据源的所有分集。"""
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    task_title = f"重整集数: {source_info['title']} ({source_info['providerName']})"
    task_coro = lambda session, callback: tasks.reorder_episodes_task(sourceId, session, callback)
    task_id, _ = await task_manager.submit_task(task_coro, task_title)

    logger.info(f"用户 '{current_user.username}' 提交了重整源 ID: {sourceId} 集数的任务 (Task ID: {task_id})。")
    return {"message": f"重整集数任务 '{task_title}' 已提交。", "taskId": task_id}

@router.post("/library/episodes/offset", status_code=status.HTTP_202_ACCEPTED, summary="偏移选中分集的集数", response_model=UITaskResponse)
async def offset_episodes(
    request_data: models.EpisodeOffsetRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """提交一个后台任务，对选中的分集进行集数偏移。"""
    if not request_data.episodeIds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="episodeIds 列表不能为空。")
    
    # 1. 在提交任务前，先进行快速验证，以提供即时反馈
    min_index_res = await session.execute(
        select(func.min(orm_models.Episode.episodeIndex))
        .where(orm_models.Episode.id.in_(request_data.episodeIds))
    )
    min_index = min_index_res.scalar_one_or_none()

    if min_index is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到任何一个选中的分集。")

    # 2. 检查偏移后的集数是否会小于1
    if (min_index + request_data.offset) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"操作无效：偏移后的最小集数将为 {min_index + request_data.offset}，集数必须大于0。"
        )


    # 获取一个代表性的标题用于任务日志
    first_episode = await session.get(
        orm_models.Episode, 
        request_data.episodeIds[0], 
        options=[selectinload(orm_models.Episode.source).selectinload(orm_models.AnimeSource.anime)]
    )
    # 此检查现在是多余的，因为上面的 min_index 检查已经覆盖了，但保留它以防万一
    if not first_episode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到任何一个选中的分集。")

    anime_title = first_episode.source.anime.title
    provider_name = first_episode.source.providerName
    offset_str = f"+{request_data.offset}" if request_data.offset >= 0 else str(request_data.offset)

    task_title = f"集数偏移 ({offset_str}): {anime_title} ({provider_name})"
    task_coro = lambda session, callback: tasks.offset_episodes_task(
        request_data.episodeIds, request_data.offset, session, callback
    )
    
    unique_key = f"modify-episodes-{first_episode.sourceId}"
    try:
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
    except HTTPException as e:
        # 重新抛出由 task_manager 引发的异常 (例如，任务已在运行)
        raise e

    logger.info(f"用户 '{current_user.username}' 提交了集数偏移任务 (Task ID: {task_id})。")
    return {"message": f"集数偏移任务 '{task_title}' 已提交。", "taskId": task_id}


@router.delete("/library/episode/{episodeId}", status_code=status.HTTP_202_ACCEPTED, summary="提交删除指定分集的任务", response_model=UITaskResponse)
async def delete_episode_from_source(
    episodeId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """提交一个后台任务来删除一个分集及其所有关联的弹幕。"""
    episode_info = await crud.get_episode_for_refresh(session, episodeId)
    if not episode_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    provider_name = episode_info.get('providerName', '未知源')
    task_title = f"删除分集: {episode_info['title']} - [{provider_name}]"
    unique_key = f"delete-episode-{episodeId}"
    task_coro = lambda session, callback: tasks.delete_episode_task(episodeId, session, callback)
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key, run_immediately=True)

    logger.info(f"用户 '{current_user.username}' 提交了删除分集 ID: {episodeId} 的任务 (Task ID: {task_id})。")
    return {"message": f"删除分集 '{episode_info['title']}' 的任务已提交。", "taskId": task_id}

@router.post("/library/episode/{episodeId}/refresh", status_code=status.HTTP_202_ACCEPTED, summary="刷新单个分集的弹幕", response_model=UITaskResponse)
async def refresh_single_episode(
    episodeId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    task_manager: TaskManager = Depends(get_task_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter)
):
    """为指定分集启动一个后台任务，重新获取其弹幕。"""
    # 检查分集是否存在，以提供更友好的404错误
    episode = await crud.get_episode_for_refresh(session, episodeId)
    if not episode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")
    
    logger.info(f"用户 '{current_user.username}' 请求刷新分集 ID: {episodeId} ({episode['title']})")

    provider_name = episode.get('providerName', '未知源')
    task_title = f"刷新分集: {episode['title']} - [{provider_name}]"
    task_coro = lambda session, callback: tasks.refresh_episode_task(episodeId, session, scraper_manager, rate_limiter, callback, user_id=current_user.id)
    task_id, _ = await task_manager.submit_task(task_coro, task_title)

    return {"message": f"分集 '{episode['title']}' 的刷新任务已提交。", "taskId": task_id}

@router.post("/library/source/{sourceId}/refresh", status_code=status.HTTP_202_ACCEPTED, summary="刷新指定数据源 (全量或增量)", response_model=UITaskResponse)
async def refresh_anime(
    sourceId: int,
    mode: str = Query("full", description="刷新模式: 'full' (全量) 或 'incremental' (增量)"),
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    task_manager: TaskManager = Depends(get_task_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """
    为指定的数据源启动一个刷新任务。
    - full: 清空并重新抓取所有分集和弹幕。
    - incremental: 尝试抓取最新一集。
    """
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info or not source_info.get("providerName") or not source_info.get("mediaId"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anime not found or missing source information for refresh.")
    
    unique_key = ""
    if mode == "incremental":
        logger.info(f"用户 '{current_user.username}' 为番剧 '{source_info['title']}' (源ID: {sourceId}) 启动了增量刷新任务。")
        # 修正：crud.get_episodes_for_source 现在返回一个带分页的字典
        paginated_result = await crud.get_episodes_for_source(session, sourceId, page_size=9999) # 获取所有分集以找到最大集数
        latest_episode_index = max((ep['episodeIndex'] for ep in paginated_result.get("episodes", [])), default=0)
        next_episode_index = latest_episode_index + 1
        
        unique_key = f"import-{source_info['providerName']}-{source_info['mediaId']}-ep{next_episode_index}"
        task_title = f"增量刷新: {source_info['title']} ({source_info['providerName']}) - 尝试第{next_episode_index}集"
        task_coro = lambda s, cb: tasks.incremental_refresh_task(
            sourceId=sourceId, nextEpisodeIndex=next_episode_index, session=s, manager=scraper_manager,
            task_manager=task_manager, progress_callback=cb, animeTitle=source_info["title"],
            rate_limiter=rate_limiter, metadata_manager=metadata_manager, user_id=current_user.id
        )
        message_to_return = f"番剧 '{source_info['title']}' 的增量刷新任务已提交。"
    elif mode == "full":
        logger.info(f"用户 '{current_user.username}' 为番剧 '{source_info['title']}' (源ID: {sourceId}) 启动了全量刷新任务。")
        unique_key = f"full-refresh-{sourceId}"
        task_title = f"全量刷新: {source_info['title']} ({source_info['providerName']})"
        task_coro = lambda s, cb: tasks.full_refresh_task(sourceId, s, scraper_manager, task_manager, rate_limiter, cb, metadata_manager, user_id=current_user.id)
        message_to_return = f"番剧 '{source_info['title']}' 的全量刷新任务已提交。"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无效的刷新模式，必须是 'full' 或 'incremental'。")

    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
    return {"message": message_to_return, "taskId": task_id}

@router.delete("/library/anime/{animeId}", status_code=status.HTTP_202_ACCEPTED, summary="提交删除媒体库中番剧的任务", response_model=UITaskResponse)
async def delete_anime_from_library(
    animeId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """提交一个后台任务来删除一个番剧及其所有关联数据。"""
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to delete from library")
    # Get title for task name
    anime_details = await crud.get_anime_full_details(session, animeId)
    if not anime_details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anime not found")
    
    # 修正：为删除任务添加基于 animeId 的唯一键，以防止因作品重名导致的任务冲突。
    task_title = f"删除作品: {anime_details['title']}"
    unique_key = f"delete-anime-{animeId}"
    task_coro = lambda session, callback: tasks.delete_anime_task(animeId, session, callback)
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key, run_immediately=True)

    logger.info(f"用户 '{current_user.username}' 提交了删除作品 ID: {animeId} 的任务 (Task ID: {task_id})。")
    return {"message": f"删除作品 '{anime_details['title']}' 的任务已提交。", "taskId": task_id}

class BulkDeleteRequest(models.BaseModel):
    sourceIds: List[int] = Field(..., alias="source_ids")

    class Config:
        populate_by_name = True

@router.post("/library/sources/delete-bulk", status_code=status.HTTP_202_ACCEPTED, summary="提交批量删除数据源的任务", response_model=UITaskResponse)
async def delete_bulk_sources(
    request_data: BulkDeleteRequest,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager)
):
    if not (await _has_perm_live(session, current_user.username, 'editLibrary')):
        raise HTTPException(status_code=403, detail="No permission to operate library")
    """提交一个后台任务来批量删除多个数据源。"""
    if not request_data.sourceIds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source IDs list cannot be empty.")

    task_title = f"批量删除 {len(request_data.sourceIds)} 个数据源"
    ids_str = ",".join(sorted([str(sid) for sid in request_data.sourceIds]))
    unique_key = f"delete-bulk-sources-{hashlib.md5(ids_str.encode('utf-8')).hexdigest()[:8]}"
    task_coro = lambda session, callback: tasks.delete_bulk_sources_task(request_data.sourceIds, session, callback)
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key, run_immediately=True)

    logger.info(f"用户 '{current_user.username}' 提交了批量删除 {len(request_data.sourceIds)} 个源的任务 (Task ID: {task_id})。")
    return {"message": task_title + "的任务已提交。", "taskId": task_id}

@router.get("/scrapers", response_model=List[models.ScraperSettingWithConfig], summary="获取所有搜索源的设置")
async def get_scraper_settings(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    manager: ScraperManager = Depends(get_scraper_manager),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    # 权限：允许编辑搜索源（含查看）
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to access scrapers settings")
    """获取所有可用搜索源的列表及其配置（启用状态、顺序、可配置字段）。"""
    all_settings = await crud.get_all_scraper_settings(session)

    # 修正：不应在UI中显示 'custom' 源，因为它不是一个真正的刮削器
    settings = [s for s in all_settings if s.get('providerName') != 'custom']
    
    # 获取验证开关的全局状态
    verification_enabled_str = await config_manager.get("scraper_verification_enabled", "false")
    verification_enabled = verification_enabled_str.lower() == 'true'

    full_settings = []
    for s in settings:
        provider_name = s['providerName']
        scraper_class = manager.get_scraper_class(provider_name)
        
        # Create a new dictionary with all required fields before validation
        full_setting_data = s.copy()
        
        # 如果验证被禁用，则所有源都应显示为已验证
        full_setting_data['isVerified'] = True if not verification_enabled else (provider_name in manager._verified_scrapers)
        
        if scraper_class:
            full_setting_data['isLoggable'] = getattr(scraper_class, "is_loggable", False)
            # 关键修复：复制类属性以避免修改共享的可变字典
            base_fields = getattr(scraper_class, "configurable_fields", None)
            configurable_fields = base_fields.copy() if base_fields is not None else {}

            # 为当前源动态添加其专属的黑名单配置字段
            blacklist_key = f"{provider_name}_episode_blacklist_regex"
            configurable_fields[blacklist_key] = "分集标题黑名单 (正则)"
            full_setting_data['configurableFields'] = configurable_fields
        else:
            # Provide defaults if scraper_class is not found to prevent validation errors
            full_setting_data['isLoggable'] = False
            full_setting_data['configurableFields'] = {}

        s_with_config = models.ScraperSettingWithConfig.model_validate(full_setting_data)
        full_settings.append(s_with_config)
            
    return full_settings

@router.get("/metadata-sources", response_model=List[models.MetadataSourceStatusResponse], summary="获取所有元数据源的设置")
async def get_metadata_source_settings(
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager),
    session: AsyncSession = Depends(get_db_session)
):
    """获取所有元数据源及其当前状态（配置、连接性等）。"""
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to access metadata sources")
    return await manager.get_sources_with_status()

@router.put("/metadata-sources", status_code=status.HTTP_204_NO_CONTENT, summary="更新元数据源的设置")
async def update_metadata_source_settings(
    settings: List[models.MetadataSourceSettingUpdate],
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager),
    session: AsyncSession = Depends(get_db_session)
):
    """批量更新元数据源的启用状态、辅助搜索状态和显示顺序。"""
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to edit metadata sources")
    # 修正：调用管理器的专用更新方法，而不是直接操作CRUD。
    # 管理器将负责更新数据库并刷新其内部缓存，确保设置立即生效。
    await manager.update_source_settings(settings)
    logger.info(f"用户 '{current_user.username}' 更新了元数据源设置，已重新加载。")

@router.put("/scrapers", status_code=status.HTTP_204_NO_CONTENT, summary="更新搜索源的设置")
async def update_scraper_settings(
    settings: List[models.ScraperSetting],
    current_user: models.User = Depends(security.get_current_user),
    manager: ScraperManager = Depends(get_scraper_manager),
    session: AsyncSession = Depends(get_db_session)
):
    """批量更新搜索源的启用状态和显示顺序。"""
    # 权限：允许编辑搜索源
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to edit scrapers settings")
    # 修正：与元数据源类似，调用管理器的专用方法来确保实时性。
    # 这需要您在 ScraperManager 中也添加一个类似的 update_settings 方法。
    await manager.update_settings(settings)
    logger.info(f"用户 '{current_user.username}' 更新了搜索源设置，已重新加载。")
    return

class ProxyTestResult(BaseModel):
    status: str  # 'success' or 'failure'
    latency: Optional[float] = None # in ms
    error: Optional[str] = None
@router.get("/config/proxy", response_model=models.ProxySettingsResponse, summary="获取代理配置")

async def get_proxy_settings(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """获取全局代理配置。"""
    proxy_url_task = crud.get_config_value(session, "proxyUrl", "")
    proxy_enabled_task = crud.get_config_value(session, "proxyEnabled", "false")
    proxy_url, proxy_enabled_str = await asyncio.gather(proxy_url_task, proxy_enabled_task)

    proxy_enabled = proxy_enabled_str.lower() == 'true'
    
    # Parse the URL into components
    protocol, host, port, username, password = "http", None, None, None, None
    if proxy_url:
        try:
            p = urlparse(proxy_url)
            protocol = p.scheme or "http"
            host = p.hostname
            port = p.port
            username = unquote(p.username) if p.username else None
            password = unquote(p.password) if p.password else None
        except Exception as e:
            logger.error(f"解析存储的代理URL '{proxy_url}' 失败: {e}")

    return models.ProxySettingsResponse(
        proxyProtocol=protocol,
        proxyHost=host,
        proxyPort=port,
        proxyUsername=username,
        proxyPassword=password,
        proxyEnabled=proxy_enabled
    )

@router.put("/config/proxy", status_code=status.HTTP_204_NO_CONTENT, summary="更新代理配置")
async def update_proxy_settings(
    payload: models.ProxySettingsUpdate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """更新全局代理配置。"""
    proxy_url = ""
    if payload.proxyHost and payload.proxyPort:
        # URL-encode username and password to handle special characters like '@'
        userinfo = ""
        if payload.proxyUsername:
            userinfo = quote(payload.proxyUsername)
            if payload.proxyPassword:
                userinfo += ":" + quote(payload.proxyPassword)
            userinfo += "@"
        
        proxy_url = f"{payload.proxyProtocol}://{userinfo}{payload.proxyHost}:{payload.proxyPort}"

    await crud.update_config_value(session, "proxyUrl", proxy_url)
    config_manager.invalidate("proxyUrl")
    
    await crud.update_config_value(session, "proxyEnabled", str(payload.proxyEnabled).lower())
    config_manager.invalidate("proxyEnabled")

    await crud.update_config_value(session, "proxySslVerify", str(payload.proxySslVerify).lower())
    config_manager.invalidate("proxySslVerify")
    logger.info(f"用户 '{current_user.username}' 更新了代理配置。")

class ProxyTestRequest(BaseModel):
    proxy_url: Optional[str] = None

class FullProxyTestResponse(BaseModel):
    proxy_connectivity: ProxyTestResult
    target_sites: Dict[str, ProxyTestResult]

@router.post("/proxy/test", response_model=FullProxyTestResponse, summary="测试代理连接和延迟")
async def test_proxy_latency(
    request: ProxyTestRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """
    测试代理连接和到各源的延迟。
    - 如果提供了代理URL，则通过代理测试。
    - 如果未提供代理URL，则直接连接。
    """
    # 权限：需要 editProxy 或主管理员
    try:
        from ..config import settings as _app
        super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = set()
    perms = getattr(current_user, 'permissions', None) or {}
    if not ((current_user.username.lower() in super_admins) or (perms.get('editProxy') is True)):
        raise HTTPException(status_code=403, detail="No permission to test proxy settings")
    proxy_url = request.proxy_url
    proxy_to_use = proxy_url if proxy_url else None

    # --- 步骤 1: 测试与代理服务器本身的连通性 ---
    proxy_connectivity_result: ProxyTestResult
    if not proxy_url:
        proxy_connectivity_result = ProxyTestResult(status="skipped", error="未配置代理，跳过测试")
    else:
        # 使用一个已知的高可用、轻量级端点进行测试
        test_url_google = "http://www.google.com/generate_204"
        try:
            async with httpx.AsyncClient(proxy=proxy_to_use, timeout=10.0, follow_redirects=False) as client:
                start_time = time.time()
                response = await client.get(test_url_google)
                latency = (time.time() - start_time) * 1000
                # 204 No Content 是成功的标志
                if response.status_code == 204:
                    proxy_connectivity_result = ProxyTestResult(status="success", latency=latency)
                else:
                    proxy_connectivity_result = ProxyTestResult(
                        status="failure",
                        error=f"连接成功但状态码异常: {response.status_code}"
                    )
        except Exception as e:
            proxy_connectivity_result = ProxyTestResult(status="failure", error=str(e))

    # --- 步骤 2: 动态构建要测试的目标域名列表 ---
    target_sites_results: Dict[str, ProxyTestResult] = {}
    test_domains = set()
    
    # 统一获取所有源的设置
    enabled_metadata_settings = await crud.get_all_metadata_source_settings(session)
    scraper_settings = await crud.get_all_scraper_settings(session)

    # 合并所有源的设置，并添加一个获取实例的函数
    all_sources_settings = [
        (s, lambda name=s['providerName']: metadata_manager.get_source(name)) for s in enabled_metadata_settings
    ] + [
        (s, lambda name=s['providerName']: scraper_manager.get_scraper(name)) for s in scraper_settings
    ]

    log_message = "代理未启用，将测试所有已启用的源。" if not proxy_url else "代理已启用，将仅测试已配置代理的源。"
    logger.info(log_message)

    for setting, get_instance_func in all_sources_settings:
        # 新增：定义一个始终需要测试的源列表
        always_test_providers = {'tmdb', 'imdb', 'tvdb', 'douban', '360', 'bangumi'}
        provider_name = setting.get('providerName')

        # 核心逻辑：
        # 1. 如果是始终测试的源（无论是否需要代理），只要它启用就测试。
        # 2. 对于其他源，如果代理启用，则只测试勾选了 useProxy 的源；如果代理未启用，则测试所有启用的源。
        should_test = setting['isEnabled'] and (provider_name in always_test_providers or (not proxy_url or setting['useProxy']))

        if should_test:
            try:
                instance = get_instance_func()
                # 修正：支持异步获取 test_url
                if hasattr(instance, 'test_url'):
                    test_url_attr = getattr(instance, 'test_url')
                    # 检查 test_url 是否是一个异步属性
                    if asyncio.iscoroutine(test_url_attr):
                        test_domains.add(await test_url_attr)
                    else:
                        test_domains.add(test_url_attr)
            except ValueError:
                pass

    # --- 步骤 3: 并发执行所有测试 ---
    async def test_domain(domain: str, client: httpx.AsyncClient) -> tuple[str, ProxyTestResult]:
        try:
            start_time = time.time()
            # 使用 HEAD 请求以提高效率，我们只关心连通性
            await client.head(domain, timeout=10.0)
            latency = (time.time() - start_time) * 1000
            return domain, ProxyTestResult(status="success", latency=latency)
        except Exception as e:
            # 简化错误信息，避免在UI上显示过长的堆栈跟踪
            error_str = f"{type(e).__name__}"
            return domain, ProxyTestResult(status="failure", error=error_str)

    if test_domains:
        async with httpx.AsyncClient(proxy=proxy_to_use, timeout=10.0) as client:
            tasks = [test_domain(domain, client) for domain in test_domains]
            results = await asyncio.gather(*tasks)
            for domain, result in results:
                target_sites_results[domain] = result

    return FullProxyTestResponse(
        proxy_connectivity=proxy_connectivity_result,
        target_sites=target_sites_results
    )

@router.get("/scrapers/{providerName}/config", response_model=Dict[str, Any], summary="获取指定搜索源的配置")
async def get_scraper_config(
    providerName: str,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    manager: ScraperManager = Depends(get_scraper_manager)
):
    # 权限：允许编辑搜索源（含查看）
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to access scraper config")
    """
    获取单个搜索源的详细配置，包括其在 `scrapers` 表中的设置（如 useProxy）
    和在 `config` 表中的键值对（如 cookie）。
    """
    scraper_class = manager.get_scraper_class(providerName)
    if not scraper_class:
        raise HTTPException(status_code=404, detail="该搜索源不存在。")

    response_data = {}

    # 1. 从 scrapers 表获取 useProxy 设置
    scraper_settings = await crud.get_scraper_setting_by_name(session, providerName)
    response_data['useProxy'] = scraper_settings.get('useProxy', False) if scraper_settings else False

    # 2. 从 config 表获取其他可配置字段
    config_keys_snake = []
    if hasattr(scraper_class, 'configurable_fields'):
        config_keys_snake.extend(scraper_class.configurable_fields.keys())
    if getattr(scraper_class, 'is_loggable', False):
        config_keys_snake.append(f"scraper_{providerName}_log_responses")
    config_keys_snake.append(f"{providerName}_episode_blacklist_regex")

    # 辅助函数，用于将 snake_case 转换为 camelCase
    def to_camel(snake_str):
        components = snake_str.split('_')
        return components[0] + ''.join(x.title() for x in components[1:])

    for db_key in config_keys_snake:
        value = await crud.get_config_value(session, db_key, "")
        camel_key = to_camel(db_key)
        # 对布尔值进行特殊处理，以匹配前端Switch组件的期望
        if "log_responses" in db_key:
            response_data[camel_key] = value.lower() == 'true'
        else:
            response_data[camel_key] = value

    return response_data

@router.put("/scrapers/{providerName}/config", status_code=status.HTTP_204_NO_CONTENT, summary="更新指定搜索源的配置")
async def update_scraper_config(
    providerName: str,
    payload: Dict[str, Any],
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager),
    manager: ScraperManager = Depends(get_scraper_manager)
):
    if not (await _has_perm_live(session, current_user.username, 'editScrapers')):
        raise HTTPException(status_code=403, detail="No permission to edit scraper config")
    """更新指定搜索源的配置，包括代理设置和其他可配置字段。"""
    try:
        scraper_class = manager.get_scraper_class(providerName)
        if not scraper_class:
            raise HTTPException(status_code=404, detail="该搜索源不存在。")

        # 1. 单独处理 useProxy 字段，它更新的是 scrapers 表
        if 'useProxy' in payload:
            use_proxy_value = bool(payload.pop('useProxy', False))
            await crud.update_scraper_proxy(session, providerName, use_proxy_value)

        # 2. 构建所有期望处理的配置键 (camelCase)
        # 修正：使用一个映射来处理前端camelCase键到后端DB键的转换，以兼容混合命名法
        expected_camel_keys = set()
        camel_to_db_key_map = {}

        def to_camel(snake_str):
            components = snake_str.split('_')
            return components[0] + ''.join(x.title() for x in components[1:])

        if hasattr(scraper_class, 'configurable_fields'):
            for db_key in scraper_class.configurable_fields.keys():
                camel_key = to_camel(db_key)
                expected_camel_keys.add(camel_key)
                camel_to_db_key_map[camel_key] = db_key
        
        if getattr(scraper_class, 'is_loggable', False):
            camel_key = f"scraper{providerName.capitalize()}LogResponses"
            db_key = f"scraper_{providerName}_log_responses"
            expected_camel_keys.add(camel_key)
            camel_to_db_key_map[camel_key] = db_key
        
        camel_key = f"{providerName}EpisodeBlacklistRegex"
        db_key = f"{providerName}_episode_blacklist_regex"
        expected_camel_keys.add(camel_key)
        camel_to_db_key_map[camel_key] = db_key

        # 3. 遍历 payload，只处理期望的键
        for camel_key, value in payload.items():
            if camel_key in expected_camel_keys:
                db_key = camel_to_db_key_map[camel_key]
                value_to_save = str(value) if not isinstance(value, bool) else str(value).lower()
                await crud.update_config_value(session, db_key, value_to_save)
                config_manager.invalidate(db_key)
        
        # 4. 在所有数据库操作完成后，统一提交事务
        await session.commit()
        logger.info(f"用户 '{current_user.username}' 更新了搜索源 '{providerName}' 的配置。")
    except Exception as e:
        await session.rollback()
        logger.error(f"更新搜索源 '{providerName}' 配置时出错: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="更新配置时发生内部错误。")

@router.get("/logs", response_model=List[str], summary="获取最新的服务器日志")
async def get_server_logs(current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    """获取存储在内存中的最新日志条目（需 viewHomeStatus 或主管理员）。"""
    if not (await _has_perm_live(session, current_user.username, 'viewHomeStatus')):
        raise HTTPException(status_code=403, detail="No permission to view home status")
    return get_logs()

@router.get("/metadata/{provider}/search", response_model=List[models.MetadataDetailsResponse], summary="从元数据源搜索")
async def search_metadata(
    provider: str,
    keyword: str,
    mediaType: Optional[str] = Query(None),
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    return await manager.search(provider, keyword, current_user, mediaType=mediaType)

@router.get("/metadata/{provider}/details/{item_id}", response_model=models.MetadataDetailsResponse, summary="获取元数据详情")
async def get_metadata_details(
    provider: str,
    item_id: str,
    mediaType: Optional[str] = Query(None),
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    details = await manager.get_details(provider, item_id, current_user, mediaType=mediaType)
    if not details:
        raise HTTPException(status_code=404, detail="未找到详情")
    return details

@router.get("/metadata/{provider}/details/{mediaType}/{item_id}", response_model=models.MetadataDetailsResponse, summary="获取元数据详情 (带媒体类型)", include_in_schema=False)
async def get_metadata_details_with_type(
    provider: str,
    mediaType: str,
    item_id: str,
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """
    一个兼容性路由，允许将 mediaType 作为路径的一部分。
    """
    details = await manager.get_details(provider, item_id, current_user, mediaType=mediaType)
    if not details:
        raise HTTPException(status_code=404, detail="未找到详情")
    return details

@router.post("/metadata/{provider}/actions/{action_name}", summary="执行元数据源的自定义操作")
async def execute_metadata_action(
    provider: str,
    action_name: str,
    request: Request,
    payload: Optional[Dict[str, Any]] = Body(None),
    current_user: models.User = Depends(security.get_current_user),
    manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    try:
        return await manager.execute_action(provider, action_name, payload or {}, current_user, request=request)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@router.post("/scrapers/{providerName}/actions/{actionName}", summary="执行搜索源的自定义操作")
async def execute_scraper_action(
    providerName: str,
    actionName: str,
    payload: Dict[str, Any] = None, # FastAPI will parse JSON body into a dict
    current_user: models.User = Depends(security.get_current_user),
    manager: ScraperManager = Depends(get_scraper_manager)
):
    """
    执行指定搜索源的特定操作。
    例如，Bilibili的登录流程可以通过调用 'get_login_info', 'generate_qrcode', 'poll_login' 等操作来驱动。
    """
    try:
        scraper = manager.get_scraper(providerName)
        result = await scraper.execute_action(actionName, payload or {})
        return result
    except httpx.RequestError as e:
        # 新增：捕获所有 httpx 网络错误 (连接超时, 读取超时等)
        error_message = f"与 {providerName} 通信时发生网络错误: {e}"
        logger.error(f"执行搜索源 '{providerName}' 的操作 '{actionName}' 时出错: {error_message}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error_message)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"执行搜索源 '{providerName}' 的操作 '{actionName}' 时出错: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="执行操作时发生内部错误。")

@router.post("/cache/clear", status_code=status.HTTP_200_OK, summary="清除所有缓存")

async def clear_all_caches(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
): #noqa
    """清除数据库中存储的所有缓存数据（如搜索结果、分集列表）。"""
    deleted_count = await crud.clear_all_cache(session)
    logger.info(f"用户 '{current_user.username}' 清除了所有缓存，共 {deleted_count} 条。")
    return {"message": f"成功清除了 {deleted_count} 条缓存记录。"}

@router.get("/tasks", response_model=models.PaginatedTasksResponse, summary="获取所有后台任务的状态")
async def get_all_tasks(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    search: Optional[str] = Query(None, description="按标题搜索"),
    status: Optional[str] = Query("all", description="按状态过滤: all, in_progress, completed"),
    page: int = Query(1, ge=1, description="页码"),
    pageSize: int = Query(20, ge=1, description="每页数量")
):
    """获取后台任务的列表和状态，支持搜索和过滤。"""
    paginated_result = await crud.get_tasks_from_history(session, search, status, page, pageSize)
    return models.PaginatedTasksResponse(
        total=paginated_result["total"],
        list=[models.TaskInfo.model_validate(t) for t in paginated_result["list"]]
    )


@router.post("/tasks/{task_id}/pause", status_code=status.HTTP_204_NO_CONTENT, summary="暂停一个正在运行的任务")
async def pause_task_endpoint(
    task_id: str,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """暂停一个正在运行的任务。"""
    paused = await task_manager.pause_task(task_id)
    if not paused:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务未找到或无法暂停。")
    return


@router.post("/tasks/{task_id}/resume", status_code=status.HTTP_204_NO_CONTENT, summary="恢复一个已暂停的任务")
async def resume_task_endpoint(
    task_id: str,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """恢复一个已暂停的任务。"""
    resumed = await task_manager.resume_task(task_id)
    if not resumed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务未找到或无法恢复。")
    return


@router.post("/tasks/{task_id}/abort", status_code=status.HTTP_204_NO_CONTENT, summary="中止一个正在运行的任务")
async def abort_task_endpoint(
    task_id: str,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """中止一个正在运行或暂停的任务。"""
    aborted = await task_manager.abort_current_task(task_id)
    if not aborted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务未找到或无法中止。")
    return

@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除一个历史任务")
async def delete_task_from_history_endpoint(
    task_id: str,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager)
):
    """从历史记录中删除一个任务。如果任务正在运行或暂停，会先尝试中止它。"""
    task = await crud.get_task_from_history_by_id(session, task_id)
    if not task:
        # 如果任务不存在，直接返回成功，因为最终状态是一致的
        return

    status = task['status']

    if status == TaskStatus.PENDING:
        await task_manager.cancel_pending_task(task_id)
    elif status in [TaskStatus.RUNNING, TaskStatus.PAUSED]:
        aborted = await task_manager.abort_current_task(task_id)
        if not aborted:
            # 这可能是一个竞态条件：在我们检查和中止之间，任务可能已经完成。
            # 重新检查数据库中的状态以确认。
            task_after_check = await crud.get_task_from_history_by_id(session, task_id)
            if task_after_check and task_after_check['status'] in [TaskStatus.RUNNING, TaskStatus.PAUSED]:
                # 如果它仍然在运行/暂停，说明中止失败，可能因为它不是当前任务。
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="中止任务失败，可能它不是当前正在执行的任务。")
            logger.info(f"任务 {task_id} 在中止前已完成，将直接删除历史记录。")

    deleted = await crud.delete_task_from_history(session, task_id)
    if not deleted:
        # 这不是一个严重错误，可能意味着任务在处理过程中已被删除。
        logger.info(f"在尝试删除时，任务 {task_id} 已不存在于历史记录中。")
        return
    logger.info(f"用户 '{current_user.username}' 删除了任务 ID: {task_id} (原状态: {status})。")
    return

@router.get("/tokens", response_model=List[models.ApiTokenInfo], summary="获取所有弹幕API Token")
async def get_all_api_tokens(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """获取所有为第三方播放器创建的 API Token。
    可见性规则：
    - 拥有权限 editDanmakuOutput（或主管理员）可查看所有 Token（全局 + 他人私有 + 自己私有）。
    - 否则，仅可查看“全局Token（ownerUserId 为空）+ 自己创建的私有Token（ownerUserId 为自己）”。
    """
    user_full = await crud.get_user_by_username_full(session, current_user.username)
    owner_id = (user_full or {}).get('id')
    # 允许查看所有的条件：主管理员 或 拥有 editDanmakuOutput 权限
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    tokens = await crud.get_all_api_tokens(session, owner_user_id=owner_id, include_global=True, is_admin=allow_all)
    return [models.ApiTokenInfo.model_validate(t) for t in tokens]

@router.post("/tokens", response_model=models.ApiTokenInfo, status_code=status.HTTP_201_CREATED, summary="创建一个新的API Token")
async def create_new_api_token(
    token_data: models.ApiTokenCreate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """创建一个新的 API Token。支持自定义或随机生成。"""
    # 若提供了自定义 token，先做简单校验与重复性检查
    if getattr(token_data, 'token', None):
        custom = token_data.token.strip()
        # 简单校验：长度 8-64，由字母数字和 - _ 组成（可按需调整）
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', custom):
            raise HTTPException(status_code=400, detail="无效的Token格式（8-64位，仅允许字母/数字/下划线/中横线）")
        # 重复性检查
        exists = await crud.get_api_token_by_token_str(session, custom)
        if exists:
            raise HTTPException(status_code=409, detail="该Token已存在")
        new_token_str = custom
    else:
        # 生成一个由大小写字母和数字组成的20位随机字符串
        alphabet = string.ascii_letters + string.digits
        new_token_str = ''.join(secrets.choice(alphabet) for _ in range(20))
    try:
        user_full = await crud.get_user_by_username_full(session, current_user.username)
        owner_id = (user_full or {}).get('id')
        # scope: 'global' or 'private'（未提供则默认私有）
        owner_for_create: Optional[int] = owner_id
        desired_scope = getattr(token_data, 'scope', None)
        if desired_scope == 'global':
            allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
            if not allow_all:
                raise HTTPException(status_code=403, detail="No permission to create global token")
            owner_for_create = None
        token_id = await crud.create_api_token(
            session,
            token_data.name,
            new_token_str,
            token_data.validityPeriod,
            token_data.dailyCallLimit,
            owner_user_id=owner_for_create,
            created_by_user_id=owner_id,
        )
        # 重新从数据库获取以包含所有字段
        new_token = await crud.get_api_token_by_id(session, token_id)
        return models.ApiTokenInfo.model_validate(new_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除一个API Token")
async def delete_api_token(
    token_id: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """根据ID删除一个 API Token。
    权限规则：
    - 主管理员或拥有 editDanmakuOutput 可删除任意 Token；
    - 普通用户仅可删除自己创建的私有 Token（禁止删除全局Token和他人私有Token）。
    """
    tok = await crud.get_api_token_by_id(session, token_id)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all:
        if tok.get('ownerUserId') != getattr(current_user, 'id', None):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to delete this token")
    try:
        deleted = await crud.delete_api_token(session, token_id)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Token is locked")
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return

@router.put("/tokens/{token_id}/toggle", status_code=status.HTTP_204_NO_CONTENT, summary="切换API Token的启用状态")
async def toggle_api_token_status(
    token_id: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """切换指定 API Token 的启用/禁用状态。
    非“编辑 弹幕token”用户仅能操作自己创建的私有 Token。
    """
    tok = await crud.get_api_token_by_id(session, token_id)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all and tok.get('ownerUserId') != getattr(current_user, 'id', None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to modify this token")
    try:
        toggled = await crud.toggle_api_token(session, token_id)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Token is locked")
    if not toggled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return

class ApiTokenUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Token的描述性名称")
    dailyCallLimit: int = Field(..., description="每日调用次数限制, -1 表示无限")
    validityPeriod: str = Field(..., description="新的有效期: 'permanent', 'custom', '30d' 等")
    scope: Optional[str] = Field(None, description="可选：'global' 或 'private'，仅有权限用户可修改")

@router.put("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, summary="更新API Token信息")
async def update_api_token(
    token_id: int,
    payload: ApiTokenUpdate,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """更新指定API Token的名称、每日调用上限和有效期。
    非“编辑 弹幕token”用户仅能编辑自己创建的私有 Token。
    """
    tok = await crud.get_api_token_by_id(session, token_id)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all and tok.get('ownerUserId') != getattr(current_user, 'id', None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to edit this token")
    try:
        updated = await crud.update_api_token(
            session,
            token_id=token_id,
            name=payload.name,
            daily_call_limit=payload.dailyCallLimit,
            validity_period=payload.validityPeriod
        )
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Token is locked")
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    # 处理范围修改（仅有权限用户可改；global=owner=None, private=owner=当前用户）
    if payload.scope is not None:
        allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
        if not allow_all:
            raise HTTPException(status_code=403, detail="No permission to modify token scope")
        new_owner: Optional[int] = None if payload.scope == 'global' else getattr(current_user, 'id', None)
        try:
            ok_owner = await crud.set_token_owner(session, token_id, new_owner)
        except PermissionError:
            raise HTTPException(status_code=423, detail="Token is locked")
        if not ok_owner:
            raise HTTPException(status_code=404, detail="Token not found")
    logger.info(f"用户 '{current_user.username}' 更新了 Token (ID: {token_id}) 的信息。")


@router.post("/tokens/{token_id}/reset", status_code=status.HTTP_204_NO_CONTENT, summary="重置API Token的调用次数")
async def reset_api_token_counter(
    token_id: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """将指定API Token的今日调用次数重置为0。
    非“编辑 弹幕token”用户仅能重置自己创建的私有 Token。
    """
    tok = await crud.get_api_token_by_id(session, token_id)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all and tok.get('ownerUserId') != getattr(current_user, 'id', None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to reset this token")
    try:
        reset_ok = await crud.reset_token_counter(session, token_id)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Token is locked")
    if not reset_ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    logger.info(f"用户 '{current_user.username}' 重置了 Token (ID: {token_id}) 的调用次数。")

@router.get("/config/{config_key}", response_model=Dict[str, str], summary="获取指定配置项的值")
async def get_config_item(
    config_key: str,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    """获取数据库中单个配置项的值。"""
    value = await crud.get_config_value(session, config_key, "") # 默认为空字符串
    return {"key": config_key, "value": value}

@router.put("/config/{config_key}", status_code=status.HTTP_204_NO_CONTENT, summary="更新指定配置项的值")
async def update_config_item(
    config_key: str,
    payload: Dict[str, str],
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """更新数据库中单个配置项的值。"""
    value = payload.get("value")
    if value is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing 'value' in request body")
    
    await crud.update_config_value(session, config_key, value)
    config_manager.invalidate(config_key)
    logger.info(f"用户 '{current_user.username}' 更新了配置项 '{config_key}'。")

@router.post("/config/webhookApiKey/regenerate", response_model=Dict[str, str], summary="重新生成Webhook API Key")
async def regenerate_webhook_api_key(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """生成一个新的、随机的Webhook API Key并保存到数据库。"""
    alphabet = string.ascii_letters + string.digits
    new_key = ''.join(secrets.choice(alphabet) for _ in range(20))
    await crud.update_config_value(session, "webhookApiKey", new_key)
    config_manager.invalidate("webhookApiKey")
    logger.info(f"用户 '{current_user.username}' 重新生成了 Webhook API Key。")
    return {"key": "webhookApiKey", "value": new_key}

@router.post("/config/externalApiKey/regenerate", response_model=Dict[str, str], summary="重新生成外部API Key")
async def regenerate_external_api_key(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    if not (await _has_perm_live(session, current_user.username, 'editControl')):
        raise HTTPException(status_code=403, detail="No permission to regenerate external API key")
    """生成一个新的、随机的外部API Key并保存到数据库。"""
    alphabet = string.ascii_letters + string.digits
    new_key = ''.join(secrets.choice(alphabet) for _ in range(32)) # 增加长度以提高安全性
    await crud.update_config_value(session, "externalApiKey", new_key)
    config_manager.invalidate("externalApiKey")
    logger.info(f"用户 '{current_user.username}' 重新生成了外部 API Key。")
    return {"key": "externalApiKey", "value": new_key}

@router.get("/external-logs", response_model=List[models.ExternalApiLogInfo], summary="获取最新的外部API访问日志")
async def get_external_api_logs(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editControl')):
        raise HTTPException(status_code=403, detail="No permission to view external logs")
    logs = await crud.get_external_api_logs(session)
    return [models.ExternalApiLogInfo.model_validate(log) for log in logs]

@router.get("/ua-rules", response_model=List[models.UaRule], summary="获取所有UA规则")
async def get_ua_rules(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 权限：允许全局 User-Agent 过滤（含查看）
    if not (await _has_perm_live(session, current_user.username, 'editUaFilter')):
        raise HTTPException(status_code=403, detail="No permission to view UA filter settings")
    rules = await crud.get_ua_rules(session)
    return [models.UaRule.model_validate(r) for r in rules]


@router.post("/ua-rules", response_model=models.UaRule, status_code=201, summary="添加UA规则")
async def add_ua_rule(
    ruleData: models.UaRuleCreate,
    currentUser: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editUaFilter')):
        raise HTTPException(status_code=403, detail="No permission to edit UA filter settings")
    try:
        rule_id = await crud.add_ua_rule(session, ruleData.uaString)
        # This is a bit inefficient but ensures we return the full object
        rules = await crud.get_ua_rules(session)
        new_rule = next((r for r in rules if r['id'] == rule_id), None)
        return models.UaRule.model_validate(new_rule)
    except Exception:
        raise HTTPException(status_code=409, detail="该UA规则已存在。")

@router.delete("/ua-rules/{ruleId}", status_code=204, summary="删除UA规则")
async def delete_ua_rule(
    ruleId: str,
    currentUser: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editUaFilter')):
        raise HTTPException(status_code=403, detail="No permission to edit UA filter settings")
    deleted = await crud.delete_ua_rule(session, ruleId)
    if not deleted:
        raise HTTPException(status_code=404, detail="找不到指定的规则ID。")

@router.get("/config/provider/{providerName}", response_model=Dict[str, Any], summary="获取指定元数据源的配置")
async def get_provider_settings(
    providerName: str,
    current_user: models.User = Depends(security.get_current_user),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """获取指定元数据源的所有相关配置。"""
    # 访问控制：各元数据源配置仅保留“编辑”权限；拥有编辑权限即拥有查看权限
    if providerName in ('tmdb','bangumi','douban','tvdb'):
        try:
            from ..config import settings
            super_admins = set(filter(None, {(settings.admin.initial_user or "").lower()}))
        except Exception:
            super_admins = set()
        perms = getattr(current_user, 'permissions', None) or {}
        perm_map = {
            'tmdb': 'editTmdb',
            'bangumi': 'editBangumi',
            'douban': 'editDouban',
            'tvdb': 'editTvdb',
        }
        key = perm_map.get(providerName)
        allowed = (current_user.username.lower() in super_admins) or (perms.get(key) is True)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"No permission to access {providerName} config")
    try:
        return await metadata_manager.getProviderConfig(providerName)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

@router.put("/config/provider/{providerName}", status_code=status.HTTP_204_NO_CONTENT, summary="更新指定元数据源的配置")
async def set_provider_settings(
    providerName: str,
    settings: Dict[str, Any],
    current_user: models.User = Depends(security.get_current_user),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """批量更新指定元数据源的相关配置。"""
    # 访问控制：各元数据源配置编辑需要显式授权（或主管理员）
    if providerName in ('tmdb','bangumi','douban','tvdb'):
        try:
            from ..config import settings as app_settings
            super_admins = set(filter(None, {(app_settings.admin.initial_user or "").lower()}))
        except Exception:
            super_admins = set()
        perms = getattr(current_user, 'permissions', None) or {}
        perm_map = {
            'tmdb': 'editTmdb',
            'bangumi': 'editBangumi',
            'douban': 'editDouban',
            'tvdb': 'editTvdb',
        }
        key = perm_map.get(providerName)
        allowed = (current_user.username.lower() in super_admins) or (perms.get(key) is True)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"No permission to edit {providerName} config")
    config_keys_map = {
        "tmdb": ["tmdbApiKey", "tmdbApiBaseUrl", "tmdbImageBaseUrl"],
        "bangumi": ["bangumiClientId", "bangumiClientSecret", "bangumiToken"],
        "douban": "doubanCookie", # 单值配置
        "tvdb": "tvdbApiKey",   # 单值配置
    }

    if providerName in ["douban", "tvdb"]:
        key = config_keys_map.get(providerName)
        if key:
            await config_manager.setValue(configKey=key, configValue=settings.get("value", ""))
    else:
        keys_to_update = config_keys_map.get(providerName, [])
        tasks = [
            config_manager.setValue(configKey=key, configValue=settings[key])
            for key in keys_to_update if key in settings
        ]
        if tasks:
            await asyncio.gather(*tasks)
    logger.info(f"用户 '{current_user.username}' 更新了元数据源 '{providerName}' 的配置。")

@router.get("/tokens/{tokenId}/logs", response_model=List[models.TokenAccessLog], summary="获取Token的访问日志")
async def get_token_logs(
    tokenId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 权限：仅“编辑 弹幕token”用户可查看所有日志；否则仅能查看自己私有 Token 的日志
    tok = await crud.get_api_token_by_id(session, tokenId)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all and tok.get('ownerUserId') != getattr(current_user, 'id', None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to view logs for this token")
    try:
        logs = await crud.get_token_access_logs(session, tokenId, ignore_lock=allow_all)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="Token is locked")
    return [models.TokenAccessLog.model_validate(log) for log in logs]

@router.put("/tokens/{token_id}/lock", status_code=status.HTTP_204_NO_CONTENT, summary="锁定API Token")
async def lock_api_token(
    token_id: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 权限：仅“编辑 弹幕token”用户可锁定全局或他人私有 Token；普通用户仅能锁定自己私有 Token
    tok = await crud.get_api_token_by_id(session, token_id)
    if not tok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all and tok.get('ownerUserId') != getattr(current_user, 'id', None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No permission to lock this token")
    ok = await crud.set_token_locked(session, token_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    logger.info(f"用户 '{current_user.username}' 锁定了 Token (ID: {token_id})。")
    return

@router.put("/tokens/{token_id}/unlock", summary="解除Token锁定（需编辑弹幕token权限或主管理员）")
async def unlock_api_token(
    token_id: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 仅有 editDanmakuOutput 或 主管理员可解除锁定
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all:
        raise HTTPException(status_code=403, detail="No permission to unlock token")
    ok = await crud.set_token_unlocked(session, token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    logger.info(f"用户 '{current_user.username}' 解除锁定 Token (ID: {token_id})。")
    return {"ok": True, "id": token_id}

@router.get(
    "/comment/{episodeId}",
    response_model=models.PaginatedCommentResponse,
    summary="获取指定分集的弹幕",
)
async def get_comments(
    episodeId: int,
    page: int = Query(1, ge=1, description="页码"),
    pageSize: int = Query(100, ge=1, description="每页数量"),
    session: AsyncSession = Depends(get_db_session)
):
    # 检查episode是否存在，如果不存在则返回404
    if not await crud.check_episode_exists(session, episodeId):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    comments_data = await crud.fetch_comments(session, episodeId)
    
    total = len(comments_data)
    start = (page - 1) * pageSize
    end = start + pageSize
    paginated_data = comments_data[start:end]

    comments = [
        models.Comment(cid=i + start, p=item.get("p", ""), m=item.get("m", ""))
        for i, item in enumerate(paginated_data)
    ]
    return models.PaginatedCommentResponse(total=total, list=comments)

@router.get("/webhooks/available", response_model=List[str], summary="获取所有可用的Webhook类型")
async def get_available_webhook_types(
    current_user: models.User = Depends(security.get_current_user),
    webhook_manager: WebhookManager = Depends(get_webhook_manager)
):
    """获取所有已成功加载的、可供用户选择的Webhook处理器类型。"""
    return webhook_manager.get_available_handlers()

async def delete_bulk_episodes_task(episode_ids: List[int], session: AsyncSession, progress_callback: Callable):
    """后台任务：批量删除多个分集。"""
    total = len(episode_ids)
    deleted_count = 0
    for i, episode_id in enumerate(episode_ids):
        progress = int((i / total) * 100)
        await progress_callback(progress, f"正在删除分集 {i+1}/{total} (ID: {episode_id})...")
        try:
            episode = await session.get(orm_models.Episode, episode_id)
            if episode:
                await session.delete(episode)
                await session.commit()
                deleted_count += 1
        except Exception as e:
            logger.error(f"批量删除分集任务中，删除分集 (ID: {episode_id}) 失败: {e}", exc_info=True)
    raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")

async def generic_import_task(
    provider: str, # noqa: F821
    media_id: str,
    anime_title: str,
    media_type: str,
    season: int,
    year: Optional[int],
    current_episode_index: Optional[int],
    image_url: Optional[str],
    douban_id: Optional[str],
    tmdb_id: Optional[str],
    imdb_id: Optional[str],
    tvdb_id: Optional[str],
    bangumi_id: Optional[str],
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager, 
    task_manager: TaskManager
):
    """
    后台任务：执行从指定数据源导入弹幕的完整流程。
    """
    # 重构导入逻辑以避免创建空条目
    scraper = manager.get_scraper(provider)
    normalized_title = anime_title.replace(":", "：")

    await progress_callback(10, "正在获取分集列表...")
    episodes = await scraper.get_episodes(
        media_id,
        target_episode_index=current_episode_index,
        db_media_type=media_type
    )
    if not episodes:
        msg = f"未能找到第 {current_episode_index} 集。" if current_episode_index else "未能获取到任何分集。"
        logger.warning(f"任务终止: {msg} (provider='{provider}', media_id='{media_id}')")
        raise TaskSuccess(msg)

    if media_type == "movie" and episodes:
        logger.info(f"检测到媒体类型为电影，将只处理第一个分集 '{episodes[0].title}'。")
        episodes = episodes[:1]

    anime_id: Optional[int] = None
    source_id: Optional[int] = None
    total_comments_added = 0
    total_episodes = len(episodes)

    for i, episode in enumerate(episodes):
        logger.info(f"--- 开始处理分集 {i+1}/{total_episodes}: '{episode.title}' (ID: {episode.episodeId}) ---")
        base_progress = 10 + int((i / total_episodes) * 85)
        await progress_callback(base_progress, f"正在处理: {episode.title} ({i+1}/{total_episodes})")

        async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
            current_total_progress = base_progress + (danmaku_progress / 100) * (85 / total_episodes)
            await progress_callback(current_total_progress, f"处理: {episode.title} - {danmaku_description}")

        comments = await scraper.get_comments(episode.episodeId, progress_callback=sub_progress_callback)

        if comments and anime_id is None:
            logger.info("首次成功获取弹幕，正在创建数据库主条目...")
            await progress_callback(base_progress + 1, "正在创建数据库主条目...")
            local_image_path = await download_image(image_url, session, manager, provider)
            anime_id = await crud.get_or_create_anime(session, normalized_title, media_type, season, image_url, local_image_path, year)
            await crud.update_metadata_if_empty(session, anime_id, tmdb_id, imdb_id, tvdb_id, douban_id)
            source_id = await crud.link_source_to_anime(session, anime_id, provider, media_id)
            logger.info(f"主条目创建完成 (Anime ID: {anime_id}, Source ID: {source_id})。")

        if anime_id and source_id:
            episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, episode.episodeIndex, episode.title, episode.url, episode.episodeId)
            if not comments:
                logger.info(f"分集 '{episode.title}' (DB ID: {episode_db_id}) 未找到弹幕，但已创建分集记录。")
                continue
            added_count = await crud.bulk_insert_comments(session, episode_db_id, comments)
            total_comments_added += added_count
            logger.info(f"分集 '{episode.title}' (DB ID: {episode_db_id}) 新增 {added_count} 条弹幕。")
        else:
            logger.info(f"分集 '{episode.title}' 未找到弹幕，跳过创建主条目。")

    if total_comments_added == 0:
        raise TaskSuccess("导入完成，但未找到任何新弹幕。")
    else:
        raise TaskSuccess(f"导入完成，共新增 {total_comments_added} 条弹幕。")
    
async def edited_import_task(
    request_data: "EditedImportRequest",
    progress_callback: Callable,
    session: AsyncSession,
    manager: ScraperManager
):
    """后台任务：处理编辑后的导入请求。"""
    scraper = manager.get_scraper(request_data.provider)
    normalized_title = request_data.anime_title.replace(":", "：")
    
    episodes = request_data.episodes
    if not episodes:
        raise TaskSuccess("没有提供任何分集，任务结束。")

    anime_id: Optional[int] = None
    source_id: Optional[int] = None
    total_comments_added = 0
    total_episodes = len(episodes)

    for i, episode in enumerate(episodes):
        await progress_callback(10 + int((i / total_episodes) * 85), f"正在处理: {episode.title} ({i+1}/{total_episodes})")
        comments = await scraper.get_comments(episode.episodeId)

        if comments and anime_id is None:
            local_image_path = await download_image(request_data.image_url, session, request_data.provider)
            anime_id = await crud.get_or_create_anime(session, normalized_title, request_data.media_type, request_data.season, request_data.image_url, local_image_path)
            await crud.update_metadata_if_empty(session, anime_id, request_data.tmdb_id, None, None, request_data.douban_id)
            source_id = await crud.link_source_to_anime(session, anime_id, request_data.provider, request_data.media_id)

        if anime_id and source_id:
            episode_db_id = await crud.create_episode_if_not_exists(session, anime_id, source_id, episode.episodeIndex, episode.title, episode.url, episode.episodeId)
            if comments:
                total_comments_added += await crud.bulk_insert_comments(session, episode_db_id, comments)

    if total_comments_added == 0: raise TaskSuccess("导入完成，但未找到任何新弹幕。")
    else: raise TaskSuccess(f"导入完成，共新增 {total_comments_added} 条弹幕。")

async def full_refresh_task(source_id: int, session: AsyncSession, manager: ScraperManager, task_manager: TaskManager, progress_callback: Callable):
    """后台任务：全量刷新一个已存在的番剧，采用先获取后删除的安全策略。"""
    logger.info(f"开始全量刷新源 ID: {source_id}")
    source_info = await crud.get_anime_source_info(session, source_id)
    if not source_info:
        logger.error(f"刷新失败：在数据库中找不到源 ID: {source_id}")
        raise TaskSuccess("刷新失败: 找不到源信息")

    scraper = manager.get_scraper(source_info["providerName"])

    # 步骤 1: 获取所有新数据，但不写入数据库
    await progress_callback(10, "正在获取新分集列表...")
    new_episodes = await scraper.get_episodes(source_info["mediaId"])
    if not new_episodes:
        raise TaskSuccess("刷新失败：未能从源获取任何分集信息。旧数据已保留。")

    await progress_callback(20, f"获取到 {len(new_episodes)} 个新分集，正在获取弹幕...")
    
    new_data_package = []
    total_comments_fetched = 0
    total_episodes = len(new_episodes)

    for i, episode in enumerate(new_episodes):
        base_progress = 20 + int((i / total_episodes) * 70) if total_episodes > 0 else 90
        
        async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
            current_sub_progress = (danmaku_progress / 100) * (70 / total_episodes)
            await progress_callback(base_progress + current_sub_progress, f"处理: {episode.title} - {danmaku_description}")

        comments = await scraper.get_comments(episode.episodeId, progress_callback=sub_progress_callback)
        new_data_package.append((episode, comments))
        if comments:
            total_comments_fetched += len(comments)

    if total_comments_fetched == 0:
        raise TaskSuccess("刷新完成，但未找到任何新弹幕。旧数据已保留。")

    # 步骤 2: 数据获取成功，现在在一个事务中执行清空和写入操作
    await progress_callback(95, "数据获取成功，正在清空旧数据并写入新数据...")
    try:
        await crud.clear_source_data(session, source_id)
        
        for episode_info, comments in new_data_package:
            episode_db_id = await crud.create_episode_if_not_exists(
                session, source_info["animeId"], source_id, 
                episode_info.episodeIndex, episode_info.title, 
                episode_info.url, episode_info.episodeId
            )
            if comments:
                await crud.bulk_insert_comments(session, episode_db_id, comments)
        
        await session.commit()
        raise TaskSuccess(f"全量刷新完成，共导入 {len(new_episodes)} 个分集，{total_comments_fetched} 条弹幕。")
    except Exception as e:
        await session.rollback()
        logger.error(f"全量刷新源 {source_id} 时数据库写入失败: {e}", exc_info=True)
        raise

async def delete_bulk_sources_task(source_ids: List[int], session: AsyncSession, progress_callback: Callable):
    """Background task to delete multiple sources."""
    total = len(source_ids)
    deleted_count = 0
    for i, source_id in enumerate(source_ids):
        progress = int((i / total) * 100)
        await progress_callback(progress, f"正在删除源 {i+1}/{total} (ID: {source_id})...")
        try:
            source = await session.get(orm_models.AnimeSource, source_id)
            if source:
                await session.delete(source)
                await session.commit()
                deleted_count += 1
        except Exception as e:
            logger.error(f"批量删除源任务中，删除源 (ID: {source_id}) 失败: {e}", exc_info=True)
            # Continue to the next one
    raise TaskSuccess(f"批量删除完成，共处理 {total} 个，成功删除 {deleted_count} 个。")

async def refresh_episode_task(episode_id: int, session: AsyncSession, manager: ScraperManager, progress_callback: Callable):
    """后台任务：刷新单个分集的弹幕"""
    logger.info(f"开始刷新分集 ID: {episode_id}")
    try:
        await progress_callback(0, "正在获取分集信息...")
        # 1. 获取分集的源信息
        info = await crud.get_episode_provider_info(session, episode_id)
        if not info or not info.get("provider_name") or not info.get("provider_episode_id"):
            logger.error(f"刷新失败：在数据库中找不到分集 ID: {episode_id} 的源信息")
            progress_callback(100, "失败: 找不到源信息")
            return

        provider_name = info["provider_name"]
        provider_episode_id = info["provider_episode_id"]
        scraper = manager.get_scraper(provider_name)

        # 3. 获取新弹幕并插入
        await progress_callback(30, "正在从源获取新弹幕...")
        
        async def sub_progress_callback(danmaku_progress: int, danmaku_description: str):
            # 30% for setup, 65% for download, 5% for db write
            current_total_progress = 30 + (danmaku_progress / 100) * 65
            await progress_callback(current_total_progress, danmaku_description)

        all_comments_from_source = await scraper.get_comments(provider_episode_id, progress_callback=sub_progress_callback)

        if not all_comments_from_source:
            await crud.update_episode_fetch_time(session, episode_id)
            raise TaskSuccess("未找到任何弹幕。")

        # 新增：在插入前，先筛选出数据库中不存在的新弹幕，以避免产生大量的“重复条目”警告。
        await progress_callback(95, "正在比对新旧弹幕...")
        existing_cids = await crud.get_existing_comment_cids(session, episode_id)
        new_comments = [c for c in all_comments_from_source if str(c.get('cid')) not in existing_cids]

        if not new_comments:
            await crud.update_episode_fetch_time(session, episode_id)
            raise TaskSuccess("刷新完成，没有新增弹幕。")

        await progress_callback(96, f"正在写入 {len(new_comments)} 条新弹幕...")
        added_count = await crud.bulk_insert_comments(session, episode_id, new_comments)
        await crud.update_episode_fetch_time(session, episode_id)
        logger.info(f"分集 ID: {episode_id} 刷新完成，新增 {added_count} 条弹幕。")
        raise TaskSuccess(f"刷新完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        # 任务成功完成，直接重新抛出，由 TaskManager 处理
        raise
    except Exception as e:
        logger.error(f"刷新分集 ID: {episode_id} 时发生严重错误: {e}", exc_info=True)
        raise # Re-raise so the task manager catches it and marks as FAILED

async def reorder_episodes_task(source_id: int, session: AsyncSession, progress_callback: Callable):
    """后台任务：重新编号一个源的所有分集。"""
    logger.info(f"开始重整源 ID: {source_id} 的分集顺序。")
    await progress_callback(0, "正在获取分集列表...")
    
    try:
        # 获取所有分集，按现有顺序排序
        episodes = await crud.get_episodes_for_source(session, source_id)
        if not episodes:
            raise TaskSuccess("没有找到分集，无需重整。")

        total_episodes = len(episodes)
        updated_count = 0
        
        # 开始事务
        try:
            for i, episode_data in enumerate(episodes):
                new_index = i + 1
                if episode_data['episode_index'] != new_index:
                    await session.execute(update(orm_models.Episode).where(orm_models.Episode.id == episode_data['id']).values(episode_index=new_index))
                    updated_count += 1
                await progress_callback(int(((i + 1) / total_episodes) * 100), f"正在处理分集 {i+1}/{total_episodes}...")
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"重整源 ID {source_id} 时数据库事务失败: {e}", exc_info=True)
            raise
        raise TaskSuccess(f"重整完成，共更新了 {updated_count} 个分集的集数。")
    except Exception as e:
        logger.error(f"重整分集任务 (源ID: {source_id}) 失败: {e}", exc_info=True)
        raise

async def incremental_refresh_task(sourceId: int, nextEpisodeIndex: int, session: AsyncSession, manager: ScraperManager, task_manager: TaskManager, progress_callback: Callable, animeTitle: str):
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
            provider=source_info["providerName"], media_id=source_info["mediaId"],
            anime_title=animeTitle, media_type=source_info["type"],
            season=source_info.get("season", 1), year=source_info.get("year"),
            current_episode_index=nextEpisodeIndex, image_url=None,
            douban_id=None, tmdb_id=source_info.get("tmdbId"),
            imdb_id=None, tvdb_id=None, bangumi_id=source_info.get("bangumiId"),
            progress_callback=progress_callback,
            session=session, manager=manager, task_manager=task_manager)
    except Exception as e:
        logger.error(f"增量刷新源任务 (ID: {sourceId}) 失败: {e}", exc_info=True)
        raise

@router.post("/library/source/{source_id}/manual-import", status_code=status.HTTP_202_ACCEPTED, summary="手动导入单个分集弹幕")
async def manual_import_episode(
    source_id: int,
    request_data: models.ManualImportRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    task_manager: TaskManager = Depends(get_task_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter)
):
    """提交一个后台任务，从给定的URL手动导入弹幕。"""
    source_info = await crud.get_anime_source_info(session, source_id)
    if not source_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    provider_name = source_info['providerName']
    
    # 修正：使用 url 或 content 字段，优先使用 content
    content_to_use = request_data.content if request_data.content is not None else request_data.url

    # 仅对非自定义源验证URL
    if provider_name != 'custom':
        if not content_to_use: # Should be caught by validator, but for safety
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="URL is required for non-custom sources.")
        url_prefixes = {
            'bilibili': 'bilibili.com', 'tencent': 'v.qq.com', 'iqiyi': 'iqiyi.com', 'youku': 'youku.com',
            'mgtv': 'mgtv.com', 'acfun': 'acfun.cn', 'renren': 'rrsp.com.cn'
        }
        expected_prefix = url_prefixes.get(provider_name)
        if not expected_prefix or expected_prefix not in content_to_use:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"提供的URL与当前源 '{provider_name}' 不匹配。")

    task_title = f"手动导入: {source_info['title']} - {request_data.title or f'第 {request_data.episodeIndex} 集'} - [{provider_name}]"
    
    # 生成unique_key以防止重复任务
    unique_key = f"manual-import-{source_id}-{request_data.episodeIndex}-{provider_name}"
    
    task_coro = lambda session, callback: tasks.manual_import_task(
        sourceId=source_id, animeId=source_info['animeId'], title=request_data.title,
        episodeIndex=request_data.episodeIndex, content=content_to_use, providerName=provider_name,
        progress_callback=callback, session=session, manager=scraper_manager, rate_limiter=rate_limiter,
        user_id=current_user.id
    )
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
    return {"message": f"手动导入任务 '{task_title}' 已提交。", "taskId": task_id}

@router.post("/library/source/{sourceId}/batch-import", status_code=status.HTTP_202_ACCEPTED, summary="批量手动导入分集", response_model=UITaskResponse)
async def batch_manual_import(
    sourceId: int,
    payload: models.BatchManualImportRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    """
    为指定的数据源批量手动导入分集。
    - 对于普通数据源，请求体中的 'content' 应为视频URL。
    - 对于 'custom' 数据源，'content' 应为dandanplay格式的XML弹幕文件内容。
    """
    source_info = await crud.get_anime_source_info(session, sourceId)
    if not source_info:
        raise HTTPException(status_code=404, detail="数据源未找到")

    task_title = f"批量手动导入: {source_info['title']} ({source_info['providerName']})"
    unique_key = f"batch-manual-import-{sourceId}"
    try:
        task_coro = lambda s, cb: tasks.batch_manual_import_task(
            sourceId=sourceId,
            animeId=source_info['animeId'],
            providerName=source_info['providerName'],
            items=payload.items,
            progress_callback=cb,
            session=s,
            manager=scraper_manager,
            rate_limiter=rate_limiter
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "批量手动导入任务已提交", "taskId": task_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

async def manual_import_task(
    source_id: int, title: str, episode_index: int, url: str, provider_name: str,
    progress_callback: Callable, session: AsyncSession, manager: ScraperManager
):
    """后台任务：从URL手动导入弹幕。"""
    logger.info(f"开始手动导入任务: source_id={source_id}, title='{title}', url='{url}'")
    await progress_callback(10, "正在准备导入...")
    
    try:
        scraper = manager.get_scraper(provider_name)
        
        provider_episode_id = None
        if hasattr(scraper, 'get_ids_from_url'): provider_episode_id = await scraper.get_ids_from_url(url)
        elif hasattr(scraper, 'get_danmaku_id_from_url'): provider_episode_id = await scraper.get_danmaku_id_from_url(url)
        elif hasattr(scraper, 'get_tvid_from_url'): provider_episode_id = await scraper.get_tvid_from_url(url)
        elif hasattr(scraper, 'get_vid_from_url'): provider_episode_id = await scraper.get_vid_from_url(url)
        
        if not provider_episode_id: raise ValueError(f"无法从URL '{url}' 中解析出有效的视频ID。")

        # 修正：处理 Bilibili 和 MGTV 返回的字典ID，并将其格式化为 get_comments 期望的字符串格式。
        episode_id_for_comments = provider_episode_id
        if isinstance(provider_episode_id, dict):
            if provider_name == 'bilibili':
                episode_id_for_comments = f"{provider_episode_id.get('aid')},{provider_episode_id.get('cid')}"
            elif provider_name == 'mgtv':
                # MGTV 的 get_comments 期望 "cid,vid"
                episode_id_for_comments = f"{provider_episode_id.get('cid')},{provider_episode_id.get('vid')}"
            else:
                # 对于其他可能的字典返回，将其字符串化
                episode_id_for_comments = str(provider_episode_id)

        await progress_callback(20, f"已解析视频ID: {episode_id_for_comments}")
        comments = await scraper.get_comments(episode_id_for_comments, progress_callback=progress_callback)
        if not comments: raise TaskSuccess("未找到任何弹幕。")

        await progress_callback(90, "正在写入数据库...")
        episode_db_id = await crud.get_or_create_episode(session, source_id, episode_index, title, url, episode_id_for_comments)
        added_count = await crud.bulk_insert_comments(session, episode_db_id, comments)
        raise TaskSuccess(f"手动导入完成，新增 {added_count} 条弹幕。")
    except TaskSuccess:
        raise
    except Exception as e:
        logger.error(f"手动导入任务失败: {e}", exc_info=True)
        raise

@router.post("/import", status_code=status.HTTP_202_ACCEPTED, summary="从指定数据源导入弹幕", response_model=UITaskResponse)
async def import_from_provider(
    request_data: models.ImportRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    task_manager: TaskManager = Depends(get_task_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    try:
        # 在启动任务前检查provider是否存在
        scraper_manager.get_scraper(request_data.provider)
        logger.info(f"用户 '{current_user.username}' 正在从 '{request_data.provider}' 导入 '{request_data.animeTitle}' (media_id={request_data.mediaId})")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    # 只有在全量导入（非单集导入）时才执行此检查
    if request_data.currentEpisodeIndex is None:
        source_exists = await crud.check_source_exists_by_media_id(session, request_data.provider, request_data.mediaId)
        if source_exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该数据源已存在于弹幕库中，无需重复导入。"
            )

    # 创建一个将传递给任务管理器的协程工厂 (lambda)
    task_coro = lambda session, callback: tasks.generic_import_task(
        provider=request_data.provider,
        mediaId=request_data.mediaId,
        animeTitle=request_data.animeTitle,
        mediaType=request_data.type,
        season=request_data.season,
        year=request_data.year,
        currentEpisodeIndex=request_data.currentEpisodeIndex,
        imageUrl=request_data.imageUrl,
        doubanId=request_data.doubanId,
        config_manager=config_manager,
        tmdbId=request_data.tmdbId,
        imdbId=None, 
        tvdbId=None, # 手动导入时这些ID为空,
        bangumiId=request_data.bangumiId,
        metadata_manager=metadata_manager,
        task_manager=task_manager, # 传递 task_manager
        progress_callback=callback,
        session=session,
        manager=scraper_manager,
        rate_limiter=rate_limiter,
        user_id=current_user.id
    )
    
    # 构造任务标题
    task_title = f"导入: {request_data.animeTitle} ({request_data.provider})"
    # 如果是电视剧且指定了单集导入，则在标题中追加季和集信息
    if request_data.type == "tv_series" and request_data.currentEpisodeIndex is not None and request_data.season is not None:
        task_title += f" - S{request_data.season:02d}E{request_data.currentEpisodeIndex:02d}"

    # 生成unique_key以避免重复任务
    unique_key_parts = [request_data.provider, request_data.mediaId]
    if request_data.season is not None:
        unique_key_parts.append(f"season-{request_data.season}")
    if request_data.currentEpisodeIndex is not None:
        unique_key_parts.append(f"episode-{request_data.currentEpisodeIndex}")
    if request_data.type:
        unique_key_parts.append(request_data.type)
    unique_key = f"ui-import-{'-'.join(unique_key_parts)}"

    # 提交任务并获取任务ID
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)

    return {"message": f"'{request_data.animeTitle}' 的导入任务已提交。请在任务管理器中查看进度。", "taskId": task_id}

@router.post("/import/edited", status_code=status.HTTP_202_ACCEPTED, summary="导入编辑后的分集列表", response_model=UITaskResponse)
async def import_edited_episodes(
    request_data: models.EditedImportRequest,
    current_user: models.User = Depends(security.get_current_user),
    task_manager: TaskManager = Depends(get_task_manager),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """提交一个后台任务，使用用户在前端编辑过的分集列表进行导入。"""
    task_title = f"编辑后导入: {request_data.animeTitle} ({request_data.provider})"
    task_coro = lambda session, callback: tasks.edited_import_task(
        request_data=request_data,
        progress_callback=callback,
        session=session,
        manager=scraper_manager,
        config_manager=config_manager,
        rate_limiter=rate_limiter,
        metadata_manager=metadata_manager,
        user_id=current_user.id
    )
    # 修正：为编辑后导入任务添加一个唯一的键，以防止重复提交，同时允许对同一作品的不同分集范围进行排队。
    # 这个键基于提供商、媒体ID和正在导入的分集索引列表的哈希值。
    # 这修复了在一次导入完成后，立即为同一作品提交另一次导入时，因任务标题相同而被拒绝的问题。
    episode_indices_str = ",".join(sorted([str(ep.episodeIndex) for ep in request_data.episodes]))
    episodes_hash = hashlib.md5(episode_indices_str.encode('utf-8')).hexdigest()[:8]
    unique_key = f"import-{request_data.provider}-{request_data.mediaId}-{episodes_hash}"

    try:
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
    except HTTPException as e:
        # 重新抛出由 task_manager 引发的冲突错误
        raise e
    except Exception as e:
        logger.error(f"提交编辑后导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")
    return {"message": f"'{request_data.animeTitle}' 的编辑导入任务已提交。", "taskId": task_id}

@auth_router.post("/token", response_model=models.Token, summary="用户登录获取令牌")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_db_session)
):
    user = await crud.get_user_by_username(session, form_data.username)
    if not user or not security.verify_password(form_data.password, user["hashedPassword"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = await security.create_access_token(
        data={"sub": user["username"]}, session=session
    )
    # 更新用户的登录信息
    await crud.update_user_login_info(session, user["username"], access_token)

    return {"accessToken": access_token, "tokenType": "bearer"}


@auth_router.get("/users/me", response_model=models.User, summary="获取当前用户信息")
async def read_users_me(current_user: models.User = Depends(security.get_current_user)):
    return current_user

# ---------------- Admin: User management ----------------

def _parse_permissions(perms_json: Optional[str]) -> Dict[str, Any]:
    if not perms_json:
        return {}
    try:
        import json as _json
        return _json.loads(perms_json)
    except Exception:
        return {}

def _require_can_create_users(user_full: Dict[str, Any]):
    """允许进入账户管理/创建用户：
    - 主管理员（用户名 admin）始终允许；
    - 或显式拥有 permissions.createUsers == True。
    """
    username = (user_full or {}).get('username', '')
    if username and username.lower() == 'admin':
        return
    perms = _parse_permissions((user_full or {}).get('permissionsJson'))
    if perms.get('createUsers') is True:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to manage accounts")

def _require_admin(user_full: Dict[str, Any]):
    if (user_full or {}).get('role') != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

def _is_super_admin_name(username: str) -> bool:
    try:
        from ..config import settings as _cfg
        super_admin = (_cfg.admin.initial_user or '').lower()
    except Exception:
        super_admin = 'admin'
    return (username or '').lower() == super_admin

async def _has_perm_live(session: AsyncSession, username: str, perm_key: str) -> bool:
    """Reload user's permissions from DB and check a specific key, super admin always true."""
    if _is_super_admin_name(username):
        return True
    full = await crud.get_user_by_username_full(session, username)
    perms = {}
    try:
        if full and full.get('permissionsJson'):
            import json as _json
            perms = _json.loads(full['permissionsJson']) or {}
    except Exception:
        perms = {}
    return perms.get(perm_key) is True

@router.get("/users", response_model=List[models.UserInfoWithMetrics], summary="列出所有用户（管理员）")
async def list_users(
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    user_full = await crud.get_user_by_username_full(session, current_user.username)
    _require_can_create_users(user_full or {})
    data = await crud.list_users_with_metrics(session)
    # 解析权限JSON
    items: List[models.UserInfoWithMetrics] = []
    import json as _json
    for u in data:
        perms = None
        if u.get('permissionsJson'):
            try:
                perms = _json.loads(u['permissionsJson'])
            except Exception:
                perms = None
        items.append(models.UserInfoWithMetrics(
            id=u['id'], username=u['username'], role=u.get('role','user'), perHourLimit=u.get('perHourLimit'), canCreateAdmin=u.get('canCreateAdmin', False), permissions=perms,
            searchCount=u.get('searchCount',0), tokenUsage=u.get('tokenUsage',0), downloadCount=u.get('downloadCount',0), remark=u.get('remark')
        ))
    return items

@router.post("/users", status_code=status.HTTP_201_CREATED, summary="创建用户（管理员）")
async def create_user_admin(
    payload: models.UserCreateRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    user_full = await crud.get_user_by_username_full(session, current_user.username)
    _require_can_create_users(user_full or {})
    # 不允许创建新的主管理员，唯一的主管理员为 username=admin
    if (payload.username or '').lower() == 'admin':
        raise HTTPException(status_code=403, detail="Cannot create super admin user")
    # 默认权限
    perms = payload.permissions or {
        'view.home': True,
        'view.library': True,
        'view.tasks.running': True,
        'view.tasks.ratepanel': True,
        'view.bullet.tokens': True,
        'view.settings.security': True,
    }
    import json as _json
    from ..security import get_password_hash
    perm_str = _json.dumps(perms, ensure_ascii=False)
    # 统一将角色设置为 user
    user_id = await crud.create_user_with_permissions(session, payload.username, get_password_hash(payload.password), 'user', payload.perHourLimit, False, perm_str, remark=getattr(payload, 'remark', None))
    return {"id": user_id}

@router.put("/users/{userId}", status_code=status.HTTP_204_NO_CONTENT, summary="更新用户角色/权限（管理员）")
async def update_user_admin(
    userId: int,
    payload: models.UserUpdateRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    user_full = await crud.get_user_by_username_full(session, current_user.username)
    # 允许范围：主管理员（username == admin）或拥有 editUsers 权限的用户
    is_super_admin = _is_super_admin_name(current_user.username)
    perms_cur = _parse_permissions((user_full or {}).get('permissionsJson'))
    if not (is_super_admin or perms_cur.get('editUsers') is True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to edit user permissions")
    # 权限编辑限制：
    # - 不允许用户修改自己的权限（可允许其修改自己密码由另处控制）。
    # - 不允许任何人修改主管理员（admin）的权限。
    # - 主管理员可修改任意非主管理员用户的权限；
    # - 非主管理员需要 editUsers 权限才能修改他人权限。
    # 仅当本次请求包含 permissions 更新时进行上述校验。
    target_user = await session.get(orm_models.User, userId)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        from ..config import settings as _app_settings
        super_admins = set(filter(None, {(_app_settings.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = set()
    is_super_admin = (current_user.username or '').lower() in super_admins
    # 如果仅修改密码，限制非主管理员修改主管理员密码
    if payload.password:
        try:
            super_admin_name = (settings.admin.initial_user or 'admin').lower()
        except Exception:
            super_admin_name = 'admin'
        if getattr(target_user, 'username', '').lower() == super_admin_name and not is_super_admin:
            raise HTTPException(status_code=403, detail="Cannot modify super admin password")
    # If attempting to change permissions
    if payload.permissions is not None:
        # 任何用户不能修改主管理员的权限
        try:
            from ..config import settings as _cfg
            super_admin_names = set(filter(None, {(_cfg.admin.initial_user or '').lower()}))
        except Exception:
            super_admin_names = {'admin'}
        if getattr(target_user, 'username', '').lower() in super_admin_names:
            raise HTTPException(status_code=403, detail="Cannot modify super admin permissions")
        if current_user.id == getattr(target_user, 'id', None):
            raise HTTPException(status_code=403, detail="Cannot modify your own permissions")
        perms = getattr(current_user, 'permissions', None) or {}
        if not (is_super_admin or perms.get('editUsers') is True):
            raise HTTPException(status_code=403, detail="Not allowed to edit user permissions")
    import json as _json
    # 合并权限：仅更新提交的键，不丢失既有键
    perm_str = None
    if payload.permissions is not None:
        try:
            # 读取目标用户现有权限
            _existing_perms = {}
            if getattr(target_user, 'permissionsJson', None):
                _existing_perms = _json.loads(getattr(target_user, 'permissionsJson')) or {}
        except Exception:
            _existing_perms = {}
        _merged = dict(_existing_perms)
        _merged.update(payload.permissions or {})
        perm_str = _json.dumps(_merged, ensure_ascii=False)
    pw_hash = None
    if payload.password:
        from ..security import get_password_hash
        pw_hash = get_password_hash(payload.password)
    # 角色与 canCreateAdmin 已废弃：后端将强制规范为非主管理员皆为 user
    ok = await crud.update_user_info(session, userId, role='user' if getattr(target_user, 'username','').lower() != 'admin' else 'admin', per_hour_limit=payload.perHourLimit, can_create_admin=False, permissions_json=perm_str, password_hash=pw_hash, remark=getattr(payload, 'remark', None))
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return

@router.delete("/users/{userId}", status_code=status.HTTP_204_NO_CONTENT, summary="删除用户（管理员）")
async def delete_user_admin(
    userId: int,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 主管理员或拥有 editUsers 权限
    user_full = await crud.get_user_by_username_full(session, current_user.username)
    is_super_admin = _is_super_admin_name(current_user.username)
    perms_cur = _parse_permissions((user_full or {}).get('permissionsJson'))
    if not (is_super_admin or perms_cur.get('editUsers') is True):
        raise HTTPException(status_code=403, detail="Not allowed to delete users")
    # 不能删除自己或超级管理员
    target = await session.get(orm_models.User, userId)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        super_admin_name = (settings.admin.initial_user or 'admin').lower()
    except Exception:
        super_admin_name = 'admin'
    if getattr(target, 'username', '').lower() == super_admin_name:
        raise HTTPException(status_code=403, detail="Cannot delete super admin")
    if current_user.id == userId:
        raise HTTPException(status_code=403, detail="Cannot delete yourself")
    deleted = await crud.delete_user(session, userId)
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return

@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="用户登出")
async def logout():
    """
    用户登出。前端应清除本地存储的token。
    """
    return

@router.get("/scheduled-tasks/available-jobs", response_model=List[api_models.AvailableJobInfo])
async def get_available_jobs(request: Request, current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    """获取所有已加载的可用任务类型及其名称。"""
    if not (await _has_perm_live(session, current_user.username, 'editScheduledTasks')):
        raise HTTPException(status_code=403, detail="No permission to view scheduled jobs")
    scheduler_manager = request.app.state.scheduler_manager
    jobs = scheduler_manager.get_available_jobs()
    logger.info(f"可用的任务类型: {jobs}")
    return jobs

@router.get("/scheduled-tasks", response_model=List[models.ScheduledTaskInfo], summary="获取所有定时任务")
async def get_scheduled_tasks(
    current_user: models.User = Depends(security.get_current_user),
    scheduler: SchedulerManager = Depends(get_scheduler_manager)
):
    tasks = await scheduler.get_all_tasks()
    return [models.ScheduledTaskInfo.model_validate(t) for t in tasks]

@router.post("/scheduled-tasks", response_model=models.ScheduledTaskInfo, status_code=201, summary="创建定时任务")
async def create_scheduled_task(
    task_data: models.ScheduledTaskCreate,
    current_user: models.User = Depends(security.get_current_user),
    scheduler: SchedulerManager = Depends(get_scheduler_manager)
):
    try:
        new_task = await scheduler.add_task(task_data.name, task_data.jobType, task_data.cronExpression, task_data.isEnabled)
        return models.ScheduledTaskInfo.model_validate(new_task)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"创建定时任务失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="创建定时任务时发生内部错误")

class ImportFromUrlRequest(models.BaseModel):
    provider: str
    url: str
    title: str
    media_type: str
    season: int

@router.post("/import-from-url", status_code=status.HTTP_202_ACCEPTED, summary="从URL导入弹幕", response_model=UITaskResponse)
async def import_from_url(
    request_data: ImportFromUrlRequest,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    task_manager: TaskManager = Depends(get_task_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    provider = request_data.provider
    url = request_data.url
    title = request_data.title
    
    try:
        scraper = scraper_manager.get_scraper(provider)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    media_id_for_scraper = None

    try:
        if provider == 'bilibili':
            bvid_match = re.search(r'video/(BV[a-zA-Z0-9]+)', url)
            ssid_match = re.search(r'bangumi/play/ss(\d+)', url)
            epid_match = re.search(r'bangumi/play/ep(\d+)', url)
            if ssid_match:
                media_id_for_scraper = f"ss{ssid_match.group(1)}"
            elif bvid_match:
                media_id_for_scraper = f"bv{bvid_match.group(1)}"
            elif epid_match:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                    resp.raise_for_status()
                    html_text = resp.text
                    ssid_match_from_page = re.search(r'"season_id":(\d+)', html_text)
                    if ssid_match_from_page:
                        media_id_for_scraper = f"ss{ssid_match_from_page.group(1)}"
        elif provider == 'tencent':
            cid_match = re.search(r'/cover/([^/]+)', url)
            if cid_match:
                media_id_for_scraper = cid_match.group(1)
        elif provider == 'iqiyi':
            linkid_match = re.search(r'v_(\w+)\.html', url)
            if linkid_match:
                media_id_for_scraper = linkid_match.group(1)
        elif provider == 'youku':
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                resp.raise_for_status()
                html_text = resp.text
                showid_match = re.search(r'showid:"(\d+)"', html_text)
                if showid_match:
                    media_id_for_scraper = showid_match.group(1)
        elif provider == 'mgtv':
            cid_match = re.search(r'/b/(\d+)/', url)
            if cid_match:
                media_id_for_scraper = cid_match.group(1)
    except Exception as e:
        logger.error(f"从URL解析媒体ID时出错: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="从URL解析媒体ID时出错")

    if not media_id_for_scraper:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"无法从URL '{url}' 中为提供商 '{provider}' 解析出媒体ID。")

    task_coro = lambda session, callback: tasks.generic_import_task(
        provider=provider,
        mediaId=media_id_for_scraper,
        animeTitle=title,
        mediaType=request_data.media_type,
        season=request_data.season,
        year=None,
        currentEpisodeIndex=None,
        imageUrl=None,
        doubanId=None,
        tmdbId=None,
        imdbId=None,
        tvdbId=None,
        bangumiId=None,
        config_manager=config_manager,
        metadata_manager=metadata_manager,
        task_manager=task_manager,
        progress_callback=callback,
        session=session,
        manager=scraper_manager,
        rate_limiter=rate_limiter,
        user_id=current_user.id
    )
    
    # 生成unique_key以避免重复任务
    unique_key_parts = ["url-import", provider, media_id_for_scraper, request_data.media_type]
    if request_data.season:
        unique_key_parts.append(f"season-{request_data.season}")
    unique_key = "-".join(unique_key_parts)
    
    task_title = f"URL导入: {title} ({provider})"
    task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)

    return {"message": f"'{title}' 的URL导入任务已提交。", "taskId": task_id}

@router.put("/scheduled-tasks/{taskId}", response_model=models.ScheduledTaskInfo, summary="更新定时任务")
async def update_scheduled_task(
    taskId: str,
    task_data: models.ScheduledTaskUpdate,
    current_user: models.User = Depends(security.get_current_user),
    scheduler: SchedulerManager = Depends(get_scheduler_manager),
    session: AsyncSession = Depends(get_db_session)
):
    if not (await _has_perm_live(session, current_user.username, 'editScheduledTasks')):
        raise HTTPException(status_code=403, detail="No permission to edit scheduled tasks")
    updated_task = await scheduler.update_task(taskId, task_data.name, task_data.cronExpression, task_data.isEnabled)
    if not updated_task:
        raise HTTPException(status_code=404, detail="找不到指定的任务ID")
    return models.ScheduledTaskInfo.model_validate(updated_task)

@router.delete("/scheduled-tasks/{taskId}", status_code=204, summary="删除定时任务")
async def delete_scheduled_task(taskId: str, current_user: models.User = Depends(security.get_current_user), scheduler: SchedulerManager = Depends(get_scheduler_manager), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editScheduledTasks')):
        raise HTTPException(status_code=403, detail="No permission to delete scheduled tasks")
    await scheduler.delete_task(taskId)

@router.post("/scheduled-tasks/{taskId}/run", status_code=202, summary="立即运行一次定时任务")
async def run_scheduled_task_now(taskId: str, current_user: models.User = Depends(security.get_current_user), scheduler: SchedulerManager = Depends(get_scheduler_manager), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editScheduledTasks')):
        raise HTTPException(status_code=403, detail="No permission to run scheduled tasks")
    try:
        await scheduler.run_task_now(taskId)
        return {"message": "任务已触发运行"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

class GlobalFilterSettings(BaseModel):
    cn: str
    eng: str

@router.get("/settings/global-filter", response_model=GlobalFilterSettings, summary="获取全局标题过滤规则")
async def get_global_filter_settings(
    config: ConfigManager = Depends(get_config_manager),
    current_user: models.User = Depends(security.get_current_user)
):
    """获取用于过滤搜索结果的全局中文和英文黑名单正则表达式。"""
    cn_filter = await config.get("search_result_global_blacklist_cn", "")
    eng_filter = await config.get("search_result_global_blacklist_eng", "")
    return GlobalFilterSettings(cn=cn_filter, eng=eng_filter)

@router.put("/settings/global-filter", summary="更新全局标题过滤规则")
async def update_global_filter_settings(
    payload: GlobalFilterSettings,
    config: ConfigManager = Depends(get_config_manager),
    current_user: models.User = Depends(security.get_current_user)
):
    """更新全局的中文和英文标题过滤黑名单。"""
    await config.setValue("search_result_global_blacklist_cn", payload.cn)
    await config.setValue("search_result_global_blacklist_eng", payload.eng)
    return {"message": "全局过滤规则已更新。"}

@auth_router.put("/users/me/password", status_code=status.HTTP_204_NO_CONTENT, summary="修改当前用户密码")
async def change_current_user_password(
    request: Request,
    current_user: models.User = Depends(security.get_current_user),
    session: AsyncSession = Depends(get_db_session)
):
    # 权限检查：主管理员（初始管理员）始终允许；其余当 permissions.changePasswordSelf 为 False 时禁止。
    try:
        from ..config import settings  # 延迟导入避免循环
        super_admins = set(filter(None, {(settings.admin.initial_user or "").lower()}))
    except Exception:
        super_admins = set()
    if current_user.username.lower() not in super_admins:
        perms = getattr(current_user, 'permissions', None) or {}
        if perms.get('changePasswordSelf') is False:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password change is disabled by admin.")
    # 1. 兼容解析 JSON 与 x-www-form-urlencoded 两种提交
    try:
        body: Dict[str, Any]
        ctype = (request.headers.get('content-type') or '').lower()
        if 'application/json' in ctype:
            body = await request.json()
        elif 'application/x-www-form-urlencoded' in ctype or 'multipart/form-data' in ctype:
            form = await request.form()
            body = {k: str(v) for k, v in form.items()}
        else:
            # 尝试按 JSON 解析，失败则置空
            try:
                body = await request.json()
            except Exception:
                body = {}
        old_password = (body.get('oldPassword') or '').strip()
        new_password = (body.get('newPassword') or '').strip()
    except Exception:
        old_password = ''
        new_password = ''
    if not old_password or not new_password:
        raise HTTPException(status_code=400, detail="Missing oldPassword or newPassword")
    if len(new_password) < 8 and new_password != "":
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    # 2. 从数据库获取完整的用户信息，包括哈希密码
    user_in_db = await crud.get_user_by_username(session, current_user.username)
    if not user_in_db:
        # 理论上不会发生，因为 get_current_user 已经验证过
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # 3. 验证旧密码是否正确
    if not security.verify_password(old_password, user_in_db["hashedPassword"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect old password")

    # 4. 更新密码
    new_hashed_password = security.get_password_hash(new_password)
    await crud.update_user_password(session, current_user.username, new_hashed_password)

# --- Rate Limiter API ---

class RateLimitProviderStatus(BaseModel):
    providerName: str
    requestCount: int
    quota: Union[int, str]  # Can be a number or "∞"

class UserRateStatus(BaseModel):
    userId: int
    username: str
    used: int
    limit: Optional[int] = None
    secondsUntilReset: int

class RateLimitStatusResponse(BaseModel):
    globalEnabled: bool
    verificationFailed: bool = Field(False, description="配置文件验证是否失败")
    globalRequestCount: int
    globalLimit: int
    globalPeriod: str
    secondsUntilReset: int
    providers: List[RateLimitProviderStatus]
    # 当前用户的个人配额状态（所有用户都可见，仅包含自己）
    currentUser: Optional[UserRateStatus] = None
    # 仅管理员可见：所有用户的配额状态
    users: Optional[List[UserRateStatus]] = None

class UsageAggregateItem(BaseModel):
    userId: int
    username: str
    downloads: int
    role: Optional[str] = None

class UsageAggregateResponse(BaseModel):
    window: str  # e.g., "24h", "1mo", "all"
    startTime: Optional[datetime] = None
    endTime: Optional[datetime] = None
    items: List[UsageAggregateItem]

# --- Token Usage Aggregate ---
class TokenUsageAggregateItem(BaseModel):
    tokenId: int
    name: str
    calls: int
    ownerUserId: Optional[int] = None
    ownerUsername: Optional[str] = None
    createdByUserId: Optional[int] = None
    createdByUsername: Optional[str] = None

class TokenUsageAggregateResponse(BaseModel):
    window: str
    startTime: Optional[datetime] = None
    endTime: Optional[datetime] = None
    items: List[TokenUsageAggregateItem]

@router.get("/rate-limit/status", response_model=RateLimitStatusResponse, summary="获取所有流控规则的状态")
async def get_rate_limit_status(
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    current_user: models.User = Depends(security.get_current_user)
):
    """获取所有流控规则的当前状态，包括全局和各源的配额使用情况。"""
    # 在获取状态前，先触发一次全局流控的检查，这会强制重置过期的计数器
    try:
        await rate_limiter.check("__ui_status_check__")
    except RateLimitExceededError:
        # 我们只关心检查和重置的副作用，不关心它是否真的超限，所以忽略此错误
        pass
    except Exception as e:
        # 记录其他潜在错误，但不中断状态获取
        logger.error(f"在获取流控状态时，检查全局流控失败: {e}")

    global_enabled = rate_limiter.enabled
    global_limit = rate_limiter.global_limit
    period_seconds = rate_limiter.global_period_seconds

    all_states = await crud.get_all_rate_limit_states(session)
    states_map = {s.providerName: s for s in all_states}

    global_state = states_map.get("__global__")
    seconds_until_reset = 0
    if global_state:
        # 使用 get_now() 确保时区一致性
        time_since_reset = get_now().replace(tzinfo=None) - global_state.lastResetTime
        seconds_until_reset = max(0, int(period_seconds - time_since_reset.total_seconds()))

    provider_items = []
    # 修正：从数据库获取所有已配置的搜索源，而不是调用一个不存在的方法
    all_scrapers_raw = await crud.get_all_scraper_settings(session)
    # 修正：在显示流控状态时，排除不产生网络请求的 'custom' 源
    all_scrapers = [s for s in all_scrapers_raw if s['providerName'] != 'custom']
    for scraper_setting in all_scrapers:
        provider_name = scraper_setting['providerName']
        provider_state = states_map.get(provider_name)
        
        quota: Union[int, str] = "∞"
        try:
            scraper_instance = scraper_manager.get_scraper(provider_name)
            provider_quota = getattr(scraper_instance, 'rate_limit_quota', None)
            if provider_quota is not None and provider_quota > 0:
                quota = provider_quota
        except ValueError:
            pass

        provider_items.append(RateLimitProviderStatus(
            providerName=provider_name,
            requestCount=provider_state.requestCount if provider_state else 0,
            quota=quota
        ))

    # 修正：将秒数转换为可读的字符串以匹配响应模型
    global_period_str = f"{period_seconds} 秒"

    # 构建当前用户的配额状态
    current_user_status: Optional[UserRateStatus] = None
    try:
        cur_full = await crud.get_user_full_by_id(session, current_user.id)
        cur_state = await crud.get_or_create_user_rate_state(session, current_user.id)
        cur_seconds_left = await crud.get_user_rate_seconds_until_reset(session, current_user.id)
        # 将 -1（不限制）显示为 ∞（前端对 None 渲染为 ∞）
        _lim = (cur_full or {}).get('perHourLimit')
        _lim = None if (_lim is None or (_lim is not None and _lim < 0)) else _lim
        current_user_status = UserRateStatus(
            userId=current_user.id,
            username=current_user.username,
            used=cur_state.requestCount,
            limit=_lim,
            secondsUntilReset=cur_seconds_left
        )
    except Exception as e:
        logger.error(f"获取当前用户流控状态失败: {e}")

    # 如果是主管理员或拥有管理权限的用户，获取所有用户的配额状态
    users_status: Optional[List[UserRateStatus]] = None
    try:
        # 允许范围：
        # - 主管理员（settings.admin.initial_user）
        # - 或拥有 viewGlobalRate 权限的用户（以数据库中最新权限为准）
        try:
            from ..config import settings as _app
            super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
        except Exception:
            super_admins = {'admin'}
        # 重新从数据库加载完整用户，避免令牌缓存导致的权限不同步
        _full = await crud.get_user_by_username_full(session, current_user.username)
        from json import loads as _jloads
        _perms = {}
        try:
            if _full and _full.get('permissionsJson'):
                _perms = _jloads(_full['permissionsJson']) or {}
        except Exception:
            _perms = {}
        if (current_user.username.lower() in super_admins) or (_perms.get('viewGlobalRate') is True):
            raw_users = await crud.list_users_with_metrics(session)
            users_status = []
            for u in raw_users:
                try:
                    state = await crud.get_or_create_user_rate_state(session, u['id'])
                    seconds_left = await crud.get_user_rate_seconds_until_reset(session, u['id'])
                    # 将 -1（不限制）显示为 ∞（前端对 None 渲染为 ∞）
                    _ulim = u.get('perHourLimit')
                    _ulim = None if (_ulim is None or (_ulim is not None and _ulim < 0)) else _ulim
                    users_status.append(UserRateStatus(
                        userId=u['id'],
                        username=u['username'],
                        used=state.requestCount,
                        limit=_ulim,
                        secondsUntilReset=seconds_left
                    ))
                except Exception:
                    # 万一某个用户状态异常，跳过但不影响整体
                    continue
    except Exception as e:
        logger.error(f"获取所有用户流控状态失败: {e}")

    return RateLimitStatusResponse(
        globalEnabled=global_enabled,
        verificationFailed=rate_limiter._verification_failed,
        globalRequestCount=global_state.requestCount if global_state else 0,
        globalLimit=global_limit,
        globalPeriod=global_period_str,
        secondsUntilReset=seconds_until_reset,
        providers=provider_items,
        currentUser=current_user_status,
        users=users_status
    )

@router.get("/usage/aggregate", response_model=UsageAggregateResponse, summary="按用户聚合用量（管理员）")
async def get_usage_aggregate(
    windowHours: Optional[int] = Query(None, ge=1, description="时间窗（小时）。为空时与 all 配合使用"),
    all: bool = Query(False, description="是否统计全部时间"),
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    # 仅主管理员或拥有 viewGlobalRate 的用户可见（以数据库中最新权限为准）
    try:
        from ..config import settings as _app
        super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = {'admin'}
    _full = await crud.get_user_by_username_full(session, current_user.username)
    from json import loads as _jloads
    _perms = {}
    try:
        if _full and _full.get('permissionsJson'):
            _perms = _jloads(_full['permissionsJson']) or {}
    except Exception:
        _perms = {}
    if not ((current_user.username.lower() in super_admins) or (_perms.get('viewGlobalRate') is True)):
        raise HTTPException(status_code=403, detail="Admin only")
    end = get_now().replace(tzinfo=None)
    start = None if all else (end - timedelta(hours=(windowHours or 24)))
    rows = await crud.aggregate_user_usage(session, start, end if not all else None)
    items = [UsageAggregateItem.model_validate(r) for r in rows]
    # 生成窗口标签
    if all:
        window_label = "all"
    elif windowHours == 24 or windowHours is None:
        window_label = "24h"
    elif windowHours == 720:
        window_label = "1mo"
    else:
        window_label = f"{windowHours}h"
    return UsageAggregateResponse(window=window_label, startTime=start, endTime=None if all else end, items=items)

@router.get("/tokens/usage/aggregate", response_model=TokenUsageAggregateResponse, summary="按Token聚合使用量（需权限）")
async def get_token_usage_aggregate(
    windowHours: Optional[int] = Query(None, ge=1, description="时间窗（小时）。为空时与 all 配合使用"),
    all: bool = Query(False, description="是否统计全部时间"),
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    # 权限：仅主管理员或拥有 editDanmakuOutput 的用户可查看该聚合
    allow_all = await _has_perm_live(session, current_user.username, 'editDanmakuOutput')
    if not allow_all:
        raise HTTPException(status_code=403, detail="No permission to view token usage aggregate")
    end = get_now().replace(tzinfo=None)
    start = None if all else (end - timedelta(hours=(windowHours or 24)))
    rows = await crud.aggregate_token_usage(
        session,
        start,
        None if all else end,
        viewer_user_id=getattr(current_user, 'id', None),
        can_view_all=True
    )
    items = [TokenUsageAggregateItem.model_validate(r) for r in rows]
    if all:
        window_label = "all"
    elif windowHours == 24 or windowHours is None:
        window_label = "24h"
    elif windowHours == 720:
        window_label = "1mo"
    else:
        window_label = f"{windowHours}h"
    return TokenUsageAggregateResponse(window=window_label, startTime=start, endTime=None if all else end, items=items)

class WebhookSettings(BaseModel):
    webhookEnabled: bool
    webhookDelayedImportEnabled: bool
    webhookDelayedImportHours: int
    webhookCustomDomain: str
    webhookFilterMode: str
    webhookFilterRegex: str
    webhookLogRawRequest: bool

@router.get("/settings/webhook", response_model=WebhookSettings, summary="获取Webhook设置")
async def get_webhook_settings(
    config: ConfigManager = Depends(get_config_manager),
    current_user: models.User = Depends(security.get_current_user)
):
    # 权限：需要 editWebhook 或主管理员
    try:
        from ..config import settings as _app
        super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = set()
    perms = getattr(current_user, 'permissions', None) or {}
    if not ((current_user.username.lower() in super_admins) or (perms.get('editWebhook') is True)):
        raise HTTPException(status_code=403, detail="No permission to access webhook settings")
    # 使用 asyncio.gather 并发获取所有配置项
    (
        enabled_str, delayed_enabled_str, delay_hours_str, custom_domain_str,
        filter_mode, filter_regex, log_raw_request_str
    ) = await asyncio.gather(
        config.get("webhookEnabled", "true"),
        config.get("webhookDelayedImportEnabled", "false"),
        config.get("webhookDelayedImportHours", "24"),
        config.get("webhookCustomDomain", ""),
        config.get("webhookFilterMode", "blacklist"),
        config.get("webhookFilterRegex", ""),
        config.get("webhookLogRawRequest", "false")
    )
    return WebhookSettings(
        webhookEnabled=enabled_str.lower() == 'true',
        webhookDelayedImportEnabled=delayed_enabled_str.lower() == 'true',
        webhookDelayedImportHours=int(delay_hours_str) if delay_hours_str.isdigit() else 24,
        webhookCustomDomain=custom_domain_str,
        webhookFilterMode=filter_mode,
        webhookFilterRegex=filter_regex,
        webhookLogRawRequest=log_raw_request_str.lower() == 'true'
    )

@router.put("/settings/webhook", status_code=status.HTTP_204_NO_CONTENT, summary="更新Webhook设置")
async def update_webhook_settings(
    payload: WebhookSettings,
    config: ConfigManager = Depends(get_config_manager),
    current_user: models.User = Depends(security.get_current_user)
):
    # 权限：需要 editWebhook 或主管理员
    try:
        from ..config import settings as _app
        super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = set()
    perms = getattr(current_user, 'permissions', None) or {}
    if not ((current_user.username.lower() in super_admins) or (perms.get('editWebhook') is True)):
        raise HTTPException(status_code=403, detail="No permission to edit webhook settings")
    # 使用 asyncio.gather 并发保存所有配置项
    await asyncio.gather(
        config.setValue("webhookEnabled", str(payload.webhookEnabled).lower()),
        config.setValue("webhookDelayedImportEnabled", str(payload.webhookDelayedImportEnabled).lower()),
        config.setValue("webhookDelayedImportHours", str(payload.webhookDelayedImportHours)),
        config.setValue("webhookCustomDomain", payload.webhookCustomDomain),
        config.setValue("webhookFilterMode", payload.webhookFilterMode),
        config.setValue("webhookFilterRegex", payload.webhookFilterRegex),
        config.setValue("webhookLogRawRequest", str(payload.webhookLogRawRequest).lower())
    )
    return

class WebhookTaskItem(BaseModel):
    id: int
    receptionTime: datetime
    executeTime: datetime
    webhookSource: str
    status: str
    taskTitle: str

    class Config:
        from_attributes = True

class PaginatedWebhookTasksResponse(BaseModel):
    total: int
    list: List[WebhookTaskItem]

@router.get("/webhook-tasks", response_model=PaginatedWebhookTasksResponse, summary="获取待处理的Webhook任务列表")
async def get_webhook_tasks(
    page: int = Query(1, ge=1),
    pageSize: int = Query(100, ge=1),
    search: Optional[str] = Query(None, description="按任务标题搜索"),
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    if not (await _has_perm_live(session, current_user.username, 'editWebhookTasks')):
        raise HTTPException(status_code=403, detail="No permission to view webhook tasks")
    result = await crud.get_webhook_tasks(session, page, pageSize, search)
    return PaginatedWebhookTasksResponse.model_validate(result)

@router.post("/webhook-tasks/delete-bulk", summary="批量删除Webhook任务")
async def delete_bulk_webhook_tasks(payload: Dict[str, List[int]], session: AsyncSession = Depends(get_db_session), current_user: models.User = Depends(security.get_current_user)):
    if not (await _has_perm_live(session, current_user.username, 'editWebhookTasks')):
        raise HTTPException(status_code=403, detail="No permission to delete webhook tasks")
    deleted_count = await crud.delete_webhook_tasks(session, payload.get("ids", []))
    return {"message": f"成功删除 {deleted_count} 个任务。"}

@router.post("/webhook-tasks/run-now", summary="立即执行选中的Webhook任务")
async def run_webhook_tasks_now(
    payload: Dict[str, List[int]],
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    config_manager: ConfigManager = Depends(get_config_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    current_user: models.User = Depends(security.get_current_user)
):
    if not (await _has_perm_live(session, current_user.username, 'editWebhookTasks')):
        raise HTTPException(status_code=403, detail="No permission to run webhook tasks")
    """立即执行指定的待处理Webhook任务。"""
    task_ids = payload.get("ids", [])
    if not task_ids:
        return {"message": "没有选中任何任务。"}

    submitted_count = await tasks.run_webhook_tasks_directly_manual(
        session=session,
        task_ids=task_ids,
        task_manager=task_manager,
        scraper_manager=scraper_manager,
        metadata_manager=metadata_manager,
        config_manager=config_manager,
        rate_limiter=rate_limiter
    )

    if submitted_count > 0:
        return {"message": f"已成功提交 {submitted_count} 个任务到执行队列。"}
    else:
        return {"message": "没有找到可执行的待处理任务。"}



@router.get("/config/custom_api_domain", response_model=Dict[str, str], summary="获取自定义域名设置")
async def get_custom_api_domain(current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editCustomDomain')):
        raise HTTPException(status_code=403, detail="No permission to view custom domain")
    val = await crud.get_config_value(session, "custom_api_domain", "")
    return {"value": val}

@router.put("/config/custom_api_domain", status_code=status.HTTP_204_NO_CONTENT, summary="更新自定义域名设置")
async def set_custom_api_domain(payload: Dict[str, str], current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editCustomDomain')):
        raise HTTPException(status_code=403, detail="No permission to edit custom domain")
    val = (payload or {}).get("value", "")
    await crud.update_config_value(session, "custom_api_domain", val)
    return

@router.get("/config/uaFilterMode", response_model=Dict[str, str], summary="获取全局 UA 过滤模式")
async def get_ua_filter_mode(current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editUaFilter')):
        raise HTTPException(status_code=403, detail="No permission to view UA filter mode")
    val = await crud.get_config_value(session, "uaFilterMode", "disabled")
    return {"value": val}

@router.put("/config/uaFilterMode", status_code=status.HTTP_204_NO_CONTENT, summary="设置全局 UA 过滤模式")
async def set_ua_filter_mode(payload: Dict[str, str], current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editUaFilter')):
        raise HTTPException(status_code=403, detail="No permission to edit UA filter mode")
    val = (payload or {}).get("value", "disabled")
    await crud.update_config_value(session, "uaFilterMode", val)
    return


@router.get("/config/trustedProxies", response_model=Dict[str, str], summary="获取受信任的反向代理设置")
async def get_trusted_proxies(current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editTrustedProxies')):
        raise HTTPException(status_code=403, detail="No permission to view trusted proxies")
    val = await crud.get_config_value(session, "trustedProxies", "")
    return {"value": val}

@router.put("/config/trustedProxies", status_code=status.HTTP_204_NO_CONTENT, summary="更新受信任的反向代理设置")
async def set_trusted_proxies(payload: Dict[str, str], current_user: models.User = Depends(security.get_current_user), session: AsyncSession = Depends(get_db_session)):
    if not (await _has_perm_live(session, current_user.username, 'editTrustedProxies')):
        raise HTTPException(status_code=403, detail="No permission to edit trusted proxies")
    val = (payload or {}).get("value", "")
    # 规范化：去除多余空白并用英文逗号分隔
    if isinstance(val, str) and val:
        parts = [p.strip() for p in val.split(',')]
        val = ",".join([p for p in parts if p])
    else:
        val = ""
    await crud.update_config_value(session, "trustedProxies", val)
    return
