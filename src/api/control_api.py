import logging
import secrets
import uuid
import re
import hashlib
import ipaddress
from enum import Enum
from typing import List, Optional, Dict, Any, Callable, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Path
from fastapi.security import APIKeyQuery
from thefuzz import fuzz
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import exc

from .. import crud, models, tasks, utils, security
from ..rate_limiter import RateLimiter
from ..config_manager import ConfigManager
from ..database import get_db_session
from ..metadata_manager import MetadataSourceManager
from ..scheduler import SchedulerManager
from ..scraper_manager import ScraperManager
from ..task_manager import TaskManager, TaskSuccess, TaskStatus

from ..timezone import get_now
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def _normalize_for_filtering(title: str) -> str:
    """Removes brackets and standardizes a title for fuzzy matching."""
    if not title:
        return ""
    # Remove content in brackets
    title = re.sub(r'[\[【(（].*?[\]】)）]', '', title)
    # Normalize to lowercase, remove spaces, and standardize colons
    return title.lower().replace(" ", "").replace("：", ":").strip()

def _is_movie_by_title(title: str) -> bool:
    """Checks if a title likely represents a movie based on keywords."""
    if not title:
        return False
    return any(kw in title.lower() for kw in ["剧场版", "劇場版", "movie", "映画"])

# --- 依赖项 ---

def get_scraper_manager(request: Request) -> ScraperManager:
    """依赖项：从应用状态获取 Scraper 管理器"""
    return request.app.state.scraper_manager

def get_metadata_manager(request: Request) -> MetadataSourceManager:
    """依赖项：从应用状态获取元数据源管理器"""
    return request.app.state.metadata_manager

def get_task_manager(request: Request) -> TaskManager:
    """依赖项：从应用状态获取任务管理器"""
    return request.app.state.task_manager

def get_scheduler_manager(request: Request) -> SchedulerManager:
    """依赖项：从应用状态获取定时任务调度器"""
    return request.app.state.scheduler_manager

def get_config_manager(request: Request) -> ConfigManager:
    """依赖项：从应用状态获取配置管理器"""
    return request.app.state.config_manager

def get_rate_limiter(request: Request) -> RateLimiter:
    """依赖项：从应用状态获取速率限制器"""
    return request.app.state.rate_limiter

# 新增：定义API Key的安全方案，这将自动在Swagger UI中生成“Authorize”按钮
api_key_scheme = APIKeyQuery(name="api_key", auto_error=False, description="用于所有外部控制API的访问密钥。")

async def verify_api_key(
    request: Request,
    api_key: str = Depends(api_key_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """依赖项：验证API密钥并记录请求。如果验证成功，返回 API Key。"""
    # --- 新增：解析真实客户端IP，支持CIDR ---
    config_manager: ConfigManager = request.app.state.config_manager
    trusted_proxies_str = await config_manager.get("trustedProxies", "")
    trusted_networks = []
    if trusted_proxies_str:
        for proxy_entry in trusted_proxies_str.split(','):
            try:
                trusted_networks.append(ipaddress.ip_network(proxy_entry.strip()))
            except ValueError:
                logger.warning(f"无效的受信任代理IP或CIDR: '{proxy_entry.strip()}'，已忽略。")
    
    client_ip_str = request.client.host if request.client else "127.0.0.1"
    is_trusted = False
    if trusted_networks:
        try:
            client_addr = ipaddress.ip_address(client_ip_str)
            is_trusted = any(client_addr in network for network in trusted_networks)
        except ValueError:
            logger.warning(f"无法将客户端IP '{client_ip_str}' 解析为有效的IP地址。")

    if is_trusted:
        x_forwarded_for = request.headers.get("x-forwarded-for")
        if x_forwarded_for:
            client_ip_str = x_forwarded_for.split(',')[0].strip()
        else:
            client_ip_str = request.headers.get("x-real-ip", client_ip_str)
    # --- IP解析结束 ---

    endpoint = request.url.path

    if not api_key:
        await crud.create_external_api_log(
            session, client_ip_str, endpoint, status.HTTP_401_UNAUTHORIZED, "API Key缺失"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: API Key is missing.",
        )

    stored_key = await config_manager.get("externalApiKey", "")

    if not stored_key or not secrets.compare_digest(api_key, stored_key):
        await crud.create_external_api_log(
            session, client_ip_str, endpoint, status.HTTP_401_UNAUTHORIZED, "无效的API密钥"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的API密钥"
        )
    # 记录成功的API Key验证
    await crud.create_external_api_log(
        session, client_ip_str, endpoint, status.HTTP_200_OK, "API Key验证通过"
    )
    return api_key

router = APIRouter(dependencies=[Depends(verify_api_key)])

# --- Pydantic 模型 ---

class AutoImportSearchType(str, Enum):
    KEYWORD = "keyword"
    TMDB = "tmdb"
    TVDB = "tvdb"
    DOUBAN = "douban"
    IMDB = "imdb"
    BANGUMI = "bangumi"

class AutoImportMediaType(str, Enum):
    TV_SERIES = "tv_series"
    MOVIE = "movie"

class ControlActionResponse(BaseModel):
    """通用操作成功响应模型"""
    message: str

class ControlTaskResponse(BaseModel):
    """任务提交成功响应模型"""
    message: str
    taskId: str

class ExecutionTaskResponse(BaseModel):
    """用于返回执行任务ID的响应模型"""
    schedulerTaskId: str
    executionTaskId: Optional[str] = None

class ControlSearchResultItem(models.ProviderSearchInfo):
    resultIndex: int = Field(..., alias="result_index", description="结果在列表中的顺序索引，从0开始")

    class Config:
        populate_by_name = True

class ControlSearchResponse(BaseModel):
    searchId: str = Field(..., description="本次搜索操作的唯一ID，用于后续操作")
    results: List[ControlSearchResultItem] = Field(..., description="搜索结果列表")

class ControlDirectImportRequest(BaseModel):
    searchId: str = Field(..., description="来自搜索响应的searchId")
    resultIndex: int = Field(..., alias="result_index", ge=0, description="要导入的结果的索引 (从0开始)")
    # 修正：将可选的元数据ID移到模型末尾，以改善文档显示顺序
    tmdbId: Optional[str] = ""
    tvdbId: Optional[str] = ""
    bangumiId: Optional[str] = ""
    imdbId: Optional[str] = ""
    doubanId: Optional[str] = ""

    class Config:
        populate_by_name = True

    @model_validator(mode='before')
    @classmethod
    def empty_str_for_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if key in cls.model_fields and value is None:
                    data[key] = ""
        return data

class ControlAnimeCreateRequest(BaseModel):
    """用于外部API自定义创建影视条目的请求模型"""
    title: str = Field(..., description="作品主标题")
    type: AutoImportMediaType = Field(..., description="媒体类型")
    season: Optional[int] = Field(None, description="季度号 (tv_series 类型必需)")
    year: Optional[int] = Field(None, description="年份")
    nameEn: Optional[str] = Field(None, description="英文标题")
    nameJp: Optional[str] = Field(None, description="日文标题")
    nameRomaji: Optional[str] = Field(None, description="罗马音标题")
    aliasCn1: Optional[str] = Field(None, description="中文别名1")
    aliasCn2: Optional[str] = Field(None, description="中文别名2")
    aliasCn3: Optional[str] = Field(None, description="中文别名3")
    # 修正：将可选的元数据ID移到模型末尾，以改善文档显示顺序
    tmdbId: Optional[str] = ""
    tvdbId: Optional[str] = ""
    bangumiId: Optional[str] = ""
    imdbId: Optional[str] = ""
    doubanId: Optional[str] = ""

    @model_validator(mode='after')
    def check_season_for_tv_series(self):
        if self.type == 'tv_series' and self.season is None:
            raise ValueError('对于电视节目 (tv_series)，季度 (season) 是必需的。')
        return self
    
    @model_validator(mode='before')
    @classmethod
    def empty_str_for_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if key in cls.model_fields and value is None:
                    data[key] = ""
        return data

class ControlEditedImportRequest(BaseModel):
    searchId: str = Field(..., description="来自搜索响应的searchId")
    resultIndex: int = Field(..., alias="result_index", ge=0, description="要编辑的结果的索引 (从0开始)")
    title: Optional[str] = Field(None, description="覆盖原始标题")
    episodes: List[models.ProviderEpisodeInfo] = Field(..., description="编辑后的分集列表")
    # 修正：将可选的元数据ID移到模型末尾，以改善文档显示顺序
    tmdbId: Optional[str] = ""
    tvdbId: Optional[str] = ""
    bangumiId: Optional[str] = ""
    imdbId: Optional[str] = ""
    doubanId: Optional[str] = ""
    tmdbEpisodeGroupId: Optional[str] = Field("", description="强制指定TMDB剧集组ID")

    class Config:
        populate_by_name = True

    @model_validator(mode='before')
    @classmethod
    def empty_str_for_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if key in cls.model_fields and value is None:
                    data[key] = ""
        return data

class ControlUrlImportRequest(BaseModel):
    """用于外部API通过URL导入到指定源的请求模型"""
    sourceId: int = Field(..., description="要导入到的目标数据源ID")
    episodeIndex: int = Field(..., alias="episode_index", description="要导入的特定集数", gt=0)
    url: str = Field(..., description="包含弹幕的视频页面的URL")
    title: Optional[str] = Field(None, description="（可选）强制指定分集标题")

    class Config:
        populate_by_name = True

class ControlXmlImportRequest(BaseModel):
    """用于外部API通过XML/文本导入到指定源的请求模型"""
    sourceId: int = Field(..., description="要导入到的目标数据源ID")
    episodeIndex: int = Field(..., alias="episode_index", description="要导入的特定集数", gt=0)
    content: str = Field(..., description="XML或纯文本格式的弹幕内容")
    title: Optional[str] = Field(None, description="（可选）强制指定分集标题")

    class Config:
        populate_by_name = True

class DanmakuOutputSettings(BaseModel):
    limitPerSource: int = Field(..., alias="limit_per_source")
    aggregationEnabled: bool = Field(..., alias="aggregation_enabled")

    class Config:
        populate_by_name = True

class ControlAnimeDetailsResponse(BaseModel):
    """用于外部API的番剧详情响应模型，不包含冗余的anime_id。"""
    title: str
    type: str
    season: int
    year: Optional[int] = None
    episodeCount: Optional[int] = None
    localImagePath: Optional[str] = None
    imageUrl: Optional[str] = None
    tmdbId: Optional[str] = None
    tmdbEpisodeGroupId: Optional[str] = None
    bangumiId: Optional[str] = None
    tvdbId: Optional[str] = None
    doubanId: Optional[str] = None
    imdbId: Optional[str] = None
    nameEn: Optional[str] = None
    nameJp: Optional[str] = None
    nameRomaji: Optional[str] = None
    aliasCn1: Optional[str] = None
    aliasCn2: Optional[str] = None
    aliasCn3: Optional[str] = None

class ControlAutoImportRequest(BaseModel):
    searchType: AutoImportSearchType
    searchTerm: str
    season: Optional[int] = None
    episode: Optional[int] = None
    mediaType: Optional[AutoImportMediaType] = None

class ControlMetadataSearchResponse(BaseModel):
    """用于外部API的元数据搜索响应模型"""
    results: List[models.MetadataDetailsResponse]

# --- Helpers: API virtual user ---
async def _get_or_create_api_user_id(session: AsyncSession) -> int:
    """Return the user id for a special 'API' user used to aggregate external API usage.
    Creates the user with unlimited per-hour limit (-1) if missing.
    """
    user = await crud.get_user_by_username_full(session, "API")
    if user and user.get("id"):
        if user.get("perHourLimit") != -1:
            await crud.update_user_info(session, int(user["id"]), per_hour_limit=-1)
        return int(user["id"])
    # create it
    import secrets as _secrets
    pwd = _secrets.token_urlsafe(16)
    password_hash = security.get_password_hash(pwd)
    user_id = await crud.create_user_with_permissions(
        session,
        username="API",
        password_hash=password_hash,
        role="user",
        per_hour_limit=-1,
        can_create_admin=False,
        permissions_json=None,
        remark="External API usage aggregator"
    )
    return int(user_id)

# --- API 路由 ---

@router.post("/import/auto", status_code=status.HTTP_202_ACCEPTED, summary="全自动搜索并导入", response_model=ControlTaskResponse)
async def auto_import(
    request: Request,
    searchType: AutoImportSearchType = Query(..., description="搜索类型。可选值: 'keyword', 'tmdb', 'tvdb', 'douban', 'imdb', 'bangumi'。"),
    searchTerm: str = Query(..., description="搜索内容。根据 searchType 的不同，这里应填入关键词或对应的平台ID。"),
    season: Optional[int] = Query(None, description="季度号。如果未提供，将自动推断或默认为1。"),
    episode: Optional[int] = Query(None, description="集数。如果提供，将只导入单集（此时必须提供季度）。"),
    mediaType: Optional[AutoImportMediaType] = Query(None, description="媒体类型。当 searchType 为 'keyword' 时必填。如果留空，将根据有无 'season' 参数自动推断。"),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    config_manager: ConfigManager = Depends(get_config_manager),
    api_key: str = Depends(verify_api_key)
):
    """
    ### 功能
    这是一个强大的“全自动搜索并导入”接口，它能根据不同的ID类型（如TMDB ID、Bangumi ID等）或关键词进行搜索，并根据一系列智能规则自动选择最佳的数据源进行弹幕导入。

    ### 工作流程
    1.  **元数据获取**: 如果使用ID搜索（如`tmdb`, `bangumi`），接口会首先从对应的元数据网站获取作品的官方标题和别名。
    2.  **媒体库检查**: 检查此作品是否已存在于您的弹幕库中。
        -   如果存在且有精确标记的源，则优先使用该源。
        -   如果存在但无精确标记，则使用已有关联源中优先级最高的那个。
    3.  **全网搜索**: 如果媒体库中不存在，则使用获取到的标题和别名在所有已启用的弹幕源中进行搜索。
    4.  **智能选择**: 从搜索结果中，根据您在“搜索源”页面设置的优先级，选择最佳匹配项。
    5.  **任务提交**: 为最终选择的源创建一个后台导入任务。

    ### 参数使用说明
    -   `searchType`:
        -   `keyword`: 按关键词搜索。此时 `mediaType` 字段**必填**。
        -   `tmdb`, `tvdb`, `douban`, `imdb`, `bangumi`: 按对应平台的ID进行精确搜索。
    -   `season` & `episode`:
        -   **电视剧/番剧**:
            -   提供 `season`，不提供 `episode`: 导入 `season` 指定的整季。
            -   提供 `season` 和 `episode`: 只导入指定的单集。
        -   **电影**:
            -   `season` 和 `episode` 参数会被忽略。
    -   `mediaType`:
        -   当 `searchType` 为 `keyword` 时，此字段为**必填项**。
        -   当 `searchType` 为其他ID类型时，此字段为可选项。如果留空，系统将根据 `season` 参数是否存在来自动推断媒体类型（有 `season` 则为电视剧，否则为电影）。
    """
    if episode is not None and season is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="当提供 'episode' 参数时，'season' 参数也必须提供。")

    payload = ControlAutoImportRequest(
        searchType=searchType,
        searchTerm=searchTerm,
        season=season,
        episode=episode,
        mediaType=mediaType
    )

    # 修正：不再强制将非关键词搜索的 mediaType 设为 None。
    # 允许用户在调用 TMDB 等ID搜索时，预先指定媒体类型，以避免错误的类型推断。
    if payload.searchType == AutoImportSearchType.KEYWORD and not payload.mediaType:
        raise HTTPException(status_code=400, detail="使用 keyword 搜索时，mediaType 字段是必需的。")

    if not await manager.acquire_search_lock(api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="已有搜索或自动导入任务正在进行中，请稍后再试。"
        )

    # 修正：将 season、episode 和 mediaType 纳入 unique_key，以允许同一作品不同季/集的导入
    unique_key_parts = [payload.searchType.value, payload.searchTerm]
    if payload.season is not None:
        unique_key_parts.append(f"s{payload.season}")
    if payload.episode is not None:
        unique_key_parts.append(f"e{payload.episode}")
    # 始终包含 mediaType 以区分同名但不同类型的作品，避免重复任务检测问题
    if payload.mediaType is not None:
        unique_key_parts.append(payload.mediaType.value)
    unique_key = f"auto-import-{'-'.join(unique_key_parts)}"

    # 新增：检查最近是否有重复任务
    config_manager = get_config_manager(request)
    threshold_hours_str = await config_manager.get("externalApiDuplicateTaskThresholdHours", "3")
    try:
        threshold_hours = int(threshold_hours_str)
    except (ValueError, TypeError):
        threshold_hours = 3

    if threshold_hours > 0:
        session_factory = request.app.state.db_session_factory
        async with session_factory() as session:
            recent_task = await crud.find_recent_task_by_unique_key(session, unique_key, threshold_hours)
            if recent_task:
                time_since_creation = get_now() - recent_task.createdAt
                hours_ago = time_since_creation.total_seconds() / 3600
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"一个相似的任务在 {hours_ago:.1f} 小时前已被提交 (状态: {recent_task.status})。请在 {threshold_hours} 小时后重试。")
    # 修正：为任务标题添加季/集信息，以确保其唯一性，防止因任务名重复而提交失败。
    title_parts = [f"外部API自动导入: {payload.searchTerm} (类型: {payload.searchType})"]
    if payload.season is not None:
        title_parts.append(f"S{payload.season:02d}")
    if payload.episode is not None:
        title_parts.append(f"E{payload.episode:02d}")
    task_title = " ".join(title_parts)

    try:
        # resolve the special API user id for usage accounting
        async with request.app.state.db_session_factory() as s:
            api_user_id = await _get_or_create_api_user_id(s)
        task_coro = lambda session, cb: tasks.auto_search_and_import_task(
            payload, cb, session, config_manager, manager, metadata_manager, task_manager,
            rate_limiter=rate_limiter,
            api_key=api_key,
            user_id=api_user_id
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "自动导入任务已提交", "taskId": task_id}
    except HTTPException as e:
        # 捕获已知的冲突错误并重新抛出
        raise e
    except Exception as e:
        # 捕获任何在任务提交阶段发生的异常，并确保释放锁
        logger.error(f"提交自动导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")
    finally:
        # 确保释放锁
        await manager.release_search_lock(api_key)

@router.get("/search", response_model=ControlSearchResponse, summary="搜索媒体")
async def search_media(
    keyword: str,
    season: Optional[int] = Query(None, description="要搜索的季度 (可选)"),
    episode: Optional[int] = Query(None, description="要搜索的集数 (可选)"),
    session: AsyncSession = Depends(get_db_session),
    manager: ScraperManager = Depends(get_scraper_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    api_key: str = Depends(verify_api_key)
):
    """
    ### 功能
    根据关键词从所有启用的弹幕源搜索媒体。这是执行导入操作的第一步。

    ### 工作流程
    1.  接收关键词，以及可选的季度和集数。
    2.  并发地在所有已启用的弹幕源上进行搜索。
    3.  返回一个包含`searchId`和结果列表的响应。`searchId`是本次搜索的唯一标识，用于后续的导入操作。
    4.  搜索结果会在服务器上缓存10分钟。

    ### 参数使用说明
    -   `keyword`: 必需的搜索关键词。
    -   `season`: (可选) 如果提供，搜索将更倾向于或只返回电视剧类型的结果。
    -   `episode`: (可选) 必须与`season`一同提供，用于更精确的单集匹配。
    """
    if episode is not None and season is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="指定集数时必须同时提供季度信息。"
        )

    if not await manager.acquire_search_lock(api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="已有搜索或自动导入任务正在进行中，请稍后再试。"
        )
    try:
        # --- Start of new logic, copied and adapted from ui_api.py ---
        parsed_keyword = utils.parse_search_keyword(keyword)
        search_title = parsed_keyword["title"]
        # Prioritize explicit query params over parsed ones
        final_season = season if season is not None else parsed_keyword.get("season")
        final_episode = episode if episode is not None else parsed_keyword.get("episode")

        episode_info = {"season": final_season, "episode": final_episode} if final_season is not None or final_episode is not None else None

        # Create a dummy user for metadata calls, as this API is not user-specific
        user = models.User(id=0, username="control_api")

        logger.info(f"Control API 正在搜索: '{keyword}' (解析为: title='{search_title}', season={final_season}, episode={final_episode})")
        if not manager.has_enabled_scrapers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="没有启用的弹幕搜索源，请在“搜索源”页面中启用至少一个。"
            )

        # Get aliases from metadata sources
        all_possible_aliases = await metadata_manager.search_aliases_from_enabled_sources(search_title, user)
        
        # Validate aliases
        validated_aliases = set()
        for alias in all_possible_aliases:
            if fuzz.token_set_ratio(search_title, alias) > 70:
                validated_aliases.add(alias)
            else:
                logger.debug(f"别名验证：已丢弃低相似度的别名 '{alias}' (与 '{search_title}' 相比)")
        
        filter_aliases = validated_aliases
        filter_aliases.add(search_title)
        logger.info(f"所有辅助搜索完成，最终别名集大小: {len(filter_aliases)}")
        logger.info(f"用于过滤的别名列表: {list(filter_aliases)}")

        # Search all scrapers using the main title
        all_results = await manager.search_all([search_title], episode_info=episode_info)

        # Filter with aliases
        normalized_filter_aliases = {_normalize_for_filtering(alias) for alias in filter_aliases if alias}
        filtered_results = []
        for item in all_results:
            normalized_item_title = _normalize_for_filtering(item.title)
            if not normalized_item_title: continue
            if any(fuzz.partial_ratio(normalized_item_title, alias) > 85 for alias in normalized_filter_aliases):
                filtered_results.append(item)
        
        logger.info(f"别名过滤: 从 {len(all_results)} 个原始结果中，保留了 {len(filtered_results)} 个相关结果。")
        results = filtered_results

        for item in results:
            if item.type == 'tv_series' and _is_movie_by_title(item.title):
                item.type = 'movie'

        if final_season:
            original_count = len(results)
            filtered_by_type = [item for item in results if item.type == 'tv_series']
            results = [item for item in filtered_by_type if item.season == final_season]
            logger.info(f"根据指定的季度 ({final_season}) 进行过滤，从 {original_count} 个结果中保留了 {len(results)} 个。")

        source_settings = await crud.get_all_scraper_settings(session)
        source_order_map = {s['providerName']: s['displayOrder'] for s in source_settings}

        def sort_key(item: models.ProviderSearchInfo):
            return (source_order_map.get(item.provider, 999), -fuzz.token_set_ratio(keyword, item.title))

        sorted_results = sorted(results, key=sort_key)

        search_id = str(uuid.uuid4())
        indexed_results = [ControlSearchResultItem(**r.model_dump(), resultIndex=i) for i, r in enumerate(sorted_results)]
        await crud.set_cache(session, f"control_search_{search_id}", [r.model_dump() for r in sorted_results], 600)
        return ControlSearchResponse(searchId=search_id, results=indexed_results)
    finally:
        await manager.release_search_lock(api_key)

@router.post("/import/direct", status_code=status.HTTP_202_ACCEPTED, summary="直接导入搜索结果", response_model=ControlTaskResponse)
async def direct_import(
    payload: ControlDirectImportRequest,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    config_manager: ConfigManager = Depends(get_config_manager),
):
    """
    ### 功能
    在执行`/search`后，使用返回的`searchId`和您选择的结果索引（`resultIndex`）来直接导入弹幕。

    ### 工作流程
    这是一个简单、直接的导入方式。它会为选定的媒体创建一个后台导入任务。您也可以在请求中附加元数据ID（如`tmdbId`）来覆盖或补充作品信息。
    """
    cache_key = f"control_search_{payload.searchId}"
    cached_results_raw = await crud.get_cache(session, cache_key)
    
    if cached_results_raw is None:
        raise HTTPException(status_code=404, detail="搜索会话已过期或无效，请重新搜索。")
    
    try:
        cached_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results_raw]
    except Exception:
        raise HTTPException(status_code=500, detail="无法解析缓存的搜索结果。")

    if not (0 <= payload.resultIndex < len(cached_results)):
        raise HTTPException(status_code=400, detail="提供的 result_index 无效。")
        
    item_to_import = cached_results[payload.resultIndex]

    # 新增：在提交任务前，检查媒体库中是否已存在同名同季度的作品
    existing_anime = await crud.find_anime_by_title_and_season(session, item_to_import.title, item_to_import.season)
    if existing_anime:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"作品 '{item_to_import.title}' (第 {item_to_import.season} 季) 已存在于媒体库中，无需重复导入。"
        )

    # 修正：为任务标题添加季/集信息，以确保其唯一性，防止因任务名重复而提交失败。
    title_parts = [f"外部API导入: {item_to_import.title} ({item_to_import.provider})"]
    if item_to_import.currentEpisodeIndex is not None and item_to_import.season is not None:
        title_parts.append(f"S{item_to_import.season:02d}E{item_to_import.currentEpisodeIndex:02d}")
    task_title = " ".join(title_parts)

    # 修正：为单集导入任务生成更具体的唯一键，以允许对同一作品的不同单集进行排队。
    # 这修复了在一次单集导入完成后，立即为同一作品提交另一次单集导入时，因任务键冲突而被拒绝的问题。
    unique_key = f"import-{item_to_import.provider}-{item_to_import.mediaId}"
    if item_to_import.currentEpisodeIndex is not None:
        unique_key += f"-ep{item_to_import.currentEpisodeIndex}"

    try:
        api_user_id = await _get_or_create_api_user_id(session)
        task_coro = lambda session, cb: tasks.generic_import_task(
            provider=item_to_import.provider,
            mediaId=item_to_import.mediaId,
            animeTitle=item_to_import.title, 
            mediaType=item_to_import.type,
            season=item_to_import.season, 
            currentEpisodeIndex=item_to_import.currentEpisodeIndex,
            imageUrl=item_to_import.imageUrl, 
            year=item_to_import.year, doubanId=payload.doubanId,
            config_manager=config_manager,
            metadata_manager=metadata_manager, tmdbId=payload.tmdbId, imdbId=payload.imdbId,
            tvdbId=payload.tvdbId, bangumiId=payload.bangumiId,
            progress_callback=cb, session=session, manager=manager, task_manager=task_manager,
            rate_limiter=rate_limiter, user_id=api_user_id
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "导入任务已提交", "taskId": task_id}
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"提交直接导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")

@router.get("/episodes", response_model=List[models.ProviderEpisodeInfo], summary="获取搜索结果的分集列表")
async def get_episodes(
    searchId: str = Query(..., description="来自/search接口的searchId"),
    result_index: int = Query(..., ge=0, description="要获取分集的结果的索引"),
    session: AsyncSession = Depends(get_db_session),
    manager: ScraperManager = Depends(get_scraper_manager),
):
    """
    ### 功能
    在执行`/search`后，获取指定搜索结果的完整分集列表。

    ### 工作流程
    此接口主要用于“编辑后导入”的场景。您可以先获取原始的分集列表，在您的客户端进行修改（例如，删除预告、调整顺序），然后再通过`/import/edited`接口提交修改后的列表进行导入。
    """
    cache_key = f"control_search_{searchId}"
    cached_results_raw = await crud.get_cache(session, cache_key)
    
    if cached_results_raw is None:
        raise HTTPException(status_code=404, detail="搜索会话已过期或无效，请重新搜索。")
    
    try:
        cached_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results_raw]
    except Exception:
        raise HTTPException(status_code=500, detail="无法解析缓存的搜索结果。")

    if not (0 <= result_index < len(cached_results)):
        raise HTTPException(status_code=400, detail="提供的 result_index 无效。")
        
    item_to_fetch = cached_results[result_index]
    
    scraper = manager.get_scraper(item_to_fetch.provider)
    try:
        return await scraper.get_episodes(item_to_fetch.mediaId, db_media_type=item_to_fetch.type)
    except httpx.RequestError as e:
        logger.error(f"获取分集列表时发生网络错误 (provider={item_to_fetch.provider}, media_id={item_to_fetch.mediaId}): {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"从 {item_to_fetch.provider} 获取分集列表时发生网络错误: {e}")

@router.post("/import/edited", status_code=status.HTTP_202_ACCEPTED, summary="导入编辑后的分集列表", response_model=ControlTaskResponse)
async def edited_import(
    payload: ControlEditedImportRequest,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    config_manager: ConfigManager = Depends(get_config_manager),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """
    ### 功能
    导入一个经过用户编辑和调整的分集列表。

    ### 工作流程
    这是最灵活的导入方式。它允许您完全控制要导入的分集，包括标题、顺序等。您可以在请求中覆盖作品标题和附加元数据ID。
    """
    cache_key = f"control_search_{payload.searchId}"
    cached_results_raw = await crud.get_cache(session, cache_key)
    
    if cached_results_raw is None:
        raise HTTPException(status_code=404, detail="搜索会话已过期或无效，请重新搜索。")
    
    try:
        cached_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results_raw]
    except Exception:
        raise HTTPException(status_code=500, detail="无法解析缓存的搜索结果。")

    if not (0 <= payload.resultIndex < len(cached_results)):
        raise HTTPException(status_code=400, detail="提供的 result_index 无效。")
        
    item_info = cached_results[payload.resultIndex]

    # Construct the full EditedImportRequest for the task
    task_payload = models.EditedImportRequest(
        provider=item_info.provider,
        mediaId=item_info.mediaId,
        animeTitle=payload.title or item_info.title, # type: ignore
        year=item_info.year,
        mediaType=item_info.type,
        season=item_info.season,
        imageUrl=item_info.imageUrl,
        doubanId=payload.doubanId,
        tmdbId=payload.tmdbId,
        imdbId=payload.imdbId,
        tvdbId=payload.tvdbId,
        bangumiId=payload.bangumiId,
        tmdbEpisodeGroupId=payload.tmdbEpisodeGroupId,
        episodes=payload.episodes
    )

    # 修正：为任务标题添加季/集信息，以确保其唯一性，防止因任务名重复而提交失败。
    title_parts = [f"外部API编辑后导入: {task_payload.animeTitle} ({task_payload.provider})"]
    if task_payload.season is not None:
        title_parts.append(f"S{task_payload.season:02d}")
    if payload.episodes:
        episode_indices = sorted([ep.episodeIndex for ep in payload.episodes])
        if len(episode_indices) == 1:
            title_parts.append(f"E{episode_indices[0]:02d}")
        else:
            title_parts.append(f"({len(episode_indices)}集)")
    task_title = " ".join(title_parts)

    # 修正：使 unique_key 更具体，以允许对同一媒体的不同分集列表进行排队导入。
    # 这解决了在一次导入完成后，无法立即为同一媒体提交另一次导入的问题。
    episode_indices_str = ",".join(sorted([str(ep.episodeIndex) for ep in payload.episodes]))
    episodes_hash = hashlib.md5(episode_indices_str.encode('utf-8')).hexdigest()[:8]
    unique_key = f"import-{task_payload.provider}-{task_payload.mediaId}-{episodes_hash}"

    try:
        api_user_id = await _get_or_create_api_user_id(session)
        task_coro = lambda session, cb: tasks.edited_import_task(
            request_data=task_payload, progress_callback=cb, session=session,
            config_manager=config_manager, manager=manager, rate_limiter=rate_limiter,
            metadata_manager=metadata_manager, user_id=api_user_id
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "编辑后导入任务已提交", "taskId": task_id}
    except HTTPException as e:
        # 修正：捕获正确的 HTTPException 并重新抛出
        raise e
    except Exception as e:
        # 捕获其他意外错误
        logger.error(f"提交编辑后导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")

@router.post("/import/xml", status_code=status.HTTP_202_ACCEPTED, summary="从XML/文本导入弹幕", response_model=ControlTaskResponse)
async def xml_import(
    payload: ControlXmlImportRequest,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter)
):
    """
    ### 功能
    为一个已存在的数据源，导入指定集数的弹幕（通过XML或纯文本内容）。
    ### 工作流程
    1.  您需要提供一个已存在于系统中的 `sourceId`。
    2.  提供要导入的 `episodeIndex` (集数) 和弹幕 `content`。
    3.  系统会为该数据源创建一个后台任务，将内容解析并导入到指定分集。
    
    此接口非常适合用于对已有的数据源进行单集补全或更新。
    """
    source_info = await crud.get_anime_source_info(session, payload.sourceId)
    if not source_info:
        raise HTTPException(status_code=404, detail=f"数据源 ID: {payload.sourceId} 未找到。")

    # This type of import should only be for 'custom' provider
    if source_info["providerName"] != 'custom':
        raise HTTPException(status_code=400, detail=f"XML/文本导入仅支持 'custom' 类型的源，但目标源类型为 '{source_info['providerName']}'。")

    anime_id = source_info["animeId"]
    anime_title = source_info["title"]

    task_title = f"外部API XML导入: {anime_title} - 第 {payload.episodeIndex} 集"
    unique_key = f"manual-import-{payload.sourceId}-{payload.episodeIndex}"

    try:
        api_user_id = await _get_or_create_api_user_id(session)
        task_coro = lambda s, cb: tasks.manual_import_task(
            sourceId=payload.sourceId,
            animeId=anime_id,
            title=payload.title,
            episodeIndex=payload.episodeIndex,
            content=payload.content,
            providerName='custom',
            progress_callback=cb,
            session=s,
            manager=manager,
            rate_limiter=rate_limiter,
            user_id=api_user_id
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "XML导入任务已提交", "taskId": task_id}
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"提交XML导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")


@router.post("/import/url", status_code=status.HTTP_202_ACCEPTED, summary="从URL导入", response_model=ControlTaskResponse)
async def url_import(
    payload: ControlUrlImportRequest,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter)
):
    """
    ### 功能
    为一个已存在的数据源，导入指定集数的弹幕。
    ### 工作流程
    1.  您需要提供一个已存在于系统中的 `sourceId`。
    2.  提供要导入的 `episodeIndex` (集数) 和包含弹幕的视频页面 `url`。
    3.  系统会为该数据源创建一个后台任务，精确地获取并导入指定集数的弹幕。
    
    此接口非常适合用于对已有的数据源进行单集补全或更新。
    """
    source_info = await crud.get_anime_source_info(session, payload.sourceId)
    if not source_info:
        raise HTTPException(status_code=404, detail=f"数据源 ID: {payload.sourceId} 未找到。")

    provider_name = source_info["providerName"]
    anime_id = source_info["animeId"]
    anime_title = source_info["title"]

    scraper = manager.get_scraper(provider_name)
    if not hasattr(scraper, 'get_info_from_url'):
        raise HTTPException(status_code=400, detail=f"数据源 '{provider_name}' 不支持从URL导入。")

    task_title = f"外部API URL导入: {anime_title} - 第 {payload.episodeIndex} 集 ({provider_name})"
    unique_key = f"manual-import-{payload.sourceId}-{payload.episodeIndex}"

    try:
        api_user_id = await _get_or_create_api_user_id(session)
        task_coro = lambda s, cb: tasks.manual_import_task(
            sourceId=payload.sourceId,
            animeId=anime_id,
            title=payload.title,
            episodeIndex=payload.episodeIndex,
            content=payload.url,
            providerName=provider_name,
            progress_callback=cb,
            session=s,
            manager=manager,
            rate_limiter=rate_limiter,
            user_id=api_user_id
        )
        task_id, _ = await task_manager.submit_task(task_coro, task_title, unique_key=unique_key)
        return {"message": "URL导入任务已提交", "taskId": task_id}
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"提交URL导入任务时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="提交任务时发生内部错误。")

# --- 元信息搜索 ---

@router.get("/metadata/search", response_model=ControlMetadataSearchResponse, summary="查找元数据信息")
async def search_metadata_source(
    provider: str = Query(..., description="要查询的元数据源，例如: 'tmdb', 'bangumi'。"),
    keyword: Optional[str] = Query(None, description="按关键词搜索。'keyword' 和 'id' 必须提供一个。"),
    id: Optional[str] = Query(None, description="按ID精确查找。'keyword' 和 'id' 必须提供一个。"),
    mediaType: Optional[AutoImportMediaType] = Query(None, description="媒体类型。可选值: 'tv_series', 'movie'。"),
    metadata_manager: MetadataSourceManager = Depends(get_metadata_manager)
):
    """
    ### 功能
    从指定的元数据源（如TMDB, Bangumi）中查找媒体信息。

    ### 工作流程
    1.  提供 `provider` 来指定要查询的源。
    2.  提供 `keyword` 或 `id` 中的一个来进行搜索。
    3.  对于某些源（如TMDB），可能需要提供 `mediaType` 来区分电视剧和电影。

    ### 返回
    返回一个包含元数据详情的列表。如果通过ID查找且成功，列表中将只有一个元素。
    """
    if not keyword and not id:
        raise HTTPException(status_code=400, detail="必须提供 'keyword' 或 'id' 参数之一。")
    if keyword and id:
        raise HTTPException(status_code=400, detail="不能同时提供 'keyword' 和 'id' 参数。")

    # --- 新增：将通用媒体类型映射到特定于提供商的类型 ---
    provider_media_type: Optional[str] = None
    if mediaType:
        if provider == 'tmdb':
            provider_media_type = 'tv' if mediaType == AutoImportMediaType.TV_SERIES else 'movie'
        elif provider == 'tvdb':
            provider_media_type = 'series' if mediaType == AutoImportMediaType.TV_SERIES else 'movies'
        # 对于其他源，如果它们使用 'tv_series'/'movie'，则无需映射
        # 否则，需要在此处添加更多 case
    # --- 映射结束 ---

    # 创建一个虚拟用户，因为元数据管理器的核心方法需要它
    user = models.User(id=0, username="control_api")
    results = []

    try:
        if id:
            details = await metadata_manager.get_details(provider, id, user, mediaType=provider_media_type)
            if details:
                results.append(details)
        elif keyword:
            results = await metadata_manager.search(provider, keyword, user, mediaType=provider_media_type)
    except HTTPException as e:
        raise e # 重新抛出已知的HTTP异常
    except Exception as e:
        logger.error(f"从元数据源 '{provider}' 搜索时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"从元数据源 '{provider}' 搜索时发生内部错误。")

    return ControlMetadataSearchResponse(results=results)

# --- 媒体库管理 ---

@router.get("/library", response_model=List[models.LibraryAnimeInfo], summary="获取媒体库列表")
async def get_library(session: AsyncSession = Depends(get_db_session)):
    """获取当前弹幕库中所有已收录的作品列表。"""
    paginated_results = await crud.get_library_anime(session)
    return [models.LibraryAnimeInfo.model_validate(item) for item in paginated_results["list"]]

@router.get("/library/search", response_model=List[models.LibraryAnimeInfo], summary="搜索媒体库")
async def search_library(
    keyword: str = Query(..., description="搜索关键词"),
    session: AsyncSession = Depends(get_db_session)
):
    """根据关键词搜索弹幕库中已收录的作品。"""
    paginated_results = await crud.get_library_anime(session, keyword=keyword)
    return [models.LibraryAnimeInfo.model_validate(item) for item in paginated_results["list"]]

@router.post("/library/anime", response_model=ControlAnimeDetailsResponse, status_code=status.HTTP_201_CREATED, summary="自定义创建影视条目")
async def create_anime_entry(
    payload: ControlAnimeCreateRequest,
    session: AsyncSession = Depends(get_db_session)
):
    """
    ### 功能
    在数据库中手动创建一个新的影视作品条目。
    ### 工作流程
    1.  接收作品的标题、类型、季度等基本信息。
    2.  （可选）接收TMDB、Bangumi等元数据ID和其他别名。
    3.  在数据库中创建对应的 `anime`, `anime_metadata`, `anime_aliases` 记录。
    4.  返回新创建的作品的完整信息。
    """
    # Check for duplicates first
    # 修正：为非电视剧类型使用默认季度1进行重复检查
    season_for_check = payload.season if payload.type == AutoImportMediaType.TV_SERIES else 1
    existing_anime = await crud.find_anime_by_title_and_season(session, payload.title, season_for_check)
    if existing_anime:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已存在同名同季度的作品。"
        )
    
    # 修正：为非电视剧类型使用默认季度1进行创建
    season_for_create = payload.season if payload.type == AutoImportMediaType.TV_SERIES else 1
    new_anime_id = await crud.get_or_create_anime(
        session,
        title=payload.title,
        media_type=payload.type.value,
        season=season_for_create,
        year=payload.year,
        image_url=None, # No image URL when creating manually
        local_image_path=None
    )

    await crud.update_metadata_if_empty(
        session, new_anime_id,
        tmdb_id=payload.tmdbId, imdb_id=payload.imdbId, tvdb_id=payload.tvdbId,
        douban_id=payload.doubanId, bangumi_id=payload.bangumiId
    )
    
    await crud.update_anime_aliases(session, new_anime_id, payload)
    await session.commit()

    new_details = await crud.get_anime_full_details(session, new_anime_id)
    if not new_details:
        raise HTTPException(status_code=500, detail="创建作品后无法获取其详细信息。")

    return ControlAnimeDetailsResponse.model_validate(new_details)

@router.get("/library/anime/{animeId}", response_model=ControlAnimeDetailsResponse, summary="获取作品详情")
async def get_anime_details(animeId: int, session: AsyncSession = Depends(get_db_session)):
    """获取弹幕库中单个作品的完整详细信息，包括所有元数据ID和别名。"""
    details = await crud.get_anime_full_details(session, animeId)
    if not details: raise HTTPException(404, "作品未找到")
    return ControlAnimeDetailsResponse.model_validate(details)

@router.get("/library/anime/{animeId}/sources", response_model=List[models.SourceInfo], summary="获取作品的所有数据源")
async def get_anime_sources(animeId: int, session: AsyncSession = Depends(get_db_session)):
    """获取指定作品已关联的所有弹幕源列表。"""
    # First, check if the anime exists to provide a proper 404.
    anime_exists = await crud.get_anime_full_details(session, animeId)
    if not anime_exists:
        raise HTTPException(status_code=404, detail="作品未找到")
    return await crud.get_anime_sources(session, animeId)

@router.post("/library/anime/{animeId}/sources", response_model=models.SourceInfo, status_code=status.HTTP_201_CREATED, summary="为作品添加数据源")
async def add_source(
    animeId: int,
    payload: models.SourceCreate,
    session: AsyncSession = Depends(get_db_session)
):
    """
    ### 功能
    为一个已存在的作品手动关联一个新的数据源。

    ### 工作流程
    1.  提供一个已存在于弹幕库中的 `animeId`。
    2.  在请求体中提供 `providerName` 和 `mediaId`。
    3.  系统会将此数据源关联到指定的作品。

    ### 使用场景
    -   **添加自定义源**: 您可以为任何作品添加一个 `custom` 类型的源，以便后续通过 `/import/xml` 接口为其上传弹幕文件。
        -   `providerName`: "custom"
        -   `mediaId`: 任意唯一的字符串，例如 `custom_123`。
    -   **手动关联刮削源**: 如果自动搜索未能找到正确的结果，您可以通过此接口手动将一个已知的 `providerName` 和 `mediaId` 关联到作品上。
    """
    anime = await crud.get_anime_full_details(session, animeId)
    if not anime:
        raise HTTPException(status_code=404, detail="作品未找到")

    try:
        source_id = await crud.link_source_to_anime(session, animeId, payload.providerName, payload.mediaId)
        await session.commit()
        # After committing, fetch the full source info including counts
        all_sources = await crud.get_anime_sources(session, animeId)
        newly_created_source = next((s for s in all_sources if s['sourceId'] == source_id), None)
        if not newly_created_source:
            # This should not happen if creation was successful
            raise HTTPException(status_code=500, detail="创建数据源后无法立即获取其信息。")
        return models.SourceInfo.model_validate(newly_created_source)
    except exc.IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该数据源已存在于此作品下，无法重复添加。")

@router.put("/library/anime/{animeId}", response_model=ControlActionResponse, summary="编辑作品信息")
async def edit_anime(animeId: int, payload: models.AnimeDetailUpdate, session: AsyncSession = Depends(get_db_session)):
    """更新弹幕库中单个作品的详细信息。"""
    if not await crud.update_anime_details(session, animeId, payload):
        raise HTTPException(404, "作品未找到")
    return {"message": "作品信息更新成功。"}

@router.delete("/library/anime/{animeId}", status_code=202, summary="删除作品", response_model=ControlTaskResponse)
async def delete_anime(animeId: int, session: AsyncSession = Depends(get_db_session), task_manager: TaskManager = Depends(get_task_manager)):
    """提交一个后台任务，以删除弹幕库中的一个作品及其所有关联的数据源、分集和弹幕。"""
    details = await crud.get_anime_full_details(session, animeId)
    if not details: raise HTTPException(404, "作品未找到")
    try:
        unique_key = f"delete-anime-{animeId}"
        task_id, _ = await task_manager.submit_task(
            lambda s, cb: tasks.delete_anime_task(animeId, s, cb),
            f"外部API删除作品: {details['title']}",
            unique_key=unique_key, run_immediately=True
        )
        return {"message": "删除作品任务已提交", "taskId": task_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.delete("/library/source/{sourceId}", status_code=202, summary="删除数据源", response_model=ControlTaskResponse)
async def delete_source(sourceId: int, session: AsyncSession = Depends(get_db_session), task_manager: TaskManager = Depends(get_task_manager)):
    """提交一个后台任务，以删除一个已关联的数据源及其所有分集和弹幕。"""
    info = await crud.get_anime_source_info(session, sourceId)
    if not info: raise HTTPException(404, "数据源未找到")
    try:
        unique_key = f"delete-source-{sourceId}"
        task_id, _ = await task_manager.submit_task(
            lambda s, cb: tasks.delete_source_task(sourceId, s, cb),
            f"外部API删除源: {info['title']} ({info['providerName']})",
            unique_key=unique_key, run_immediately=True
        )
        return {"message": "删除源任务已提交", "taskId": task_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.put("/library/source/{sourceId}/favorite", response_model=ControlActionResponse, summary="精确标记数据源")
async def favorite_source(sourceId: int, session: AsyncSession = Depends(get_db_session)):
    """切换数据源的“精确标记”状态。一个作品只能有一个精确标记的源，它将在自动匹配时被优先使用。"""
    new_status = await crud.toggle_source_favorite_status(session, sourceId)
    if new_status is None:
        raise HTTPException(404, "数据源未找到")
    message = "数据源已标记为精确。" if new_status else "数据源已取消精确标记。"
    return {"message": message}

@router.get("/library/source/{sourceId}/episodes", response_model=List[models.EpisodeDetail], summary="获取源的分集列表")
async def get_source_episodes(sourceId: int, session: AsyncSession = Depends(get_db_session)):
    """获取指定数据源下所有已收录的分集列表。"""
    paginated_result = await crud.get_episodes_for_source(session, sourceId)
    return paginated_result.get("episodes", [])

@router.put("/library/episode/{episodeid}", response_model=ControlActionResponse, summary="编辑分集信息")
async def edit_episode(episodeid: int, payload: models.EpisodeInfoUpdate, session: AsyncSession = Depends(get_db_session)):
    """更新单个分集的标题、集数和官方链接。"""
    if not await crud.update_episode_info(session, episodeid, payload):
        raise HTTPException(404, "分集未找到")
    return {"message": "分集信息更新成功。"}

@router.post("/library/episode/{episodeId}/refresh", status_code=202, summary="刷新分集弹幕", response_model=ControlTaskResponse)
async def refresh_episode(
    episodeId: int,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
    manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
):
    """提交一个后台任务，为单个分集重新从其源网站获取最新的弹幕。"""
    info = await crud.get_episode_for_refresh(session, episodeId)
    if not info: raise HTTPException(404, "分集未找到")
    api_user_id = await _get_or_create_api_user_id(session)
    task_id, _ = await task_manager.submit_task(
        lambda s, cb: tasks.refresh_episode_task(episodeId, s, manager, rate_limiter, cb, user_id=api_user_id),
        f"外部API刷新分集: {info['title']}"
    )
    return {"message": "刷新分集任务已提交", "taskId": task_id}

@router.delete("/library/episode/{episodeId}", status_code=202, summary="删除分集", response_model=ControlTaskResponse)
async def delete_episode(episodeId: int, session: AsyncSession = Depends(get_db_session), task_manager: TaskManager = Depends(get_task_manager)):
    """提交一个后台任务，以删除单个分集及其所有弹幕。"""
    info = await crud.get_episode_for_refresh(session, episodeId)
    if not info: raise HTTPException(404, "分集未找到")
    try:
        unique_key = f"delete-episode-{episodeId}"
        task_id, _ = await task_manager.submit_task(
            lambda s, cb: tasks.delete_episode_task(episodeId, s, cb),
            f"外部API删除分集: {info['title']}",
            unique_key=unique_key, run_immediately=True
        )
        return {"message": "删除分集任务已提交", "taskId": task_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

# --- 弹幕管理 ---

@router.get("/danmaku/{episodeId}", response_model=models.CommentResponse, summary="获取弹幕")
async def get_danmaku(episodeId: int, session: AsyncSession = Depends(get_db_session)):
    """获取指定分集的所有弹幕，返回dandanplay兼容格式。"""
    if not await crud.check_episode_exists(session, episodeId): raise HTTPException(404, "分集未找到")
    comments = await crud.fetch_comments(session, episodeId)
    return models.CommentResponse(count=len(comments), comments=[models.Comment.model_validate(c) for c in comments])

@router.post("/danmaku/{episodeId}", status_code=202, summary="覆盖弹幕", response_model=ControlTaskResponse)
async def overwrite_danmaku(episodeId: int, payload: models.DanmakuUpdateRequest, task_manager: TaskManager = Depends(get_task_manager)):
    """提交一个后台任务，用请求体中提供的弹幕列表完全覆盖指定分集的现有弹幕。"""
    async def overwrite_task(session: AsyncSession, cb: Callable):
        await cb(10, "清空中...")
        await crud.clear_episode_comments(session, episodeId)
        await cb(50, f"插入 {len(payload.comments)} 条新弹幕...")
        
        comments_to_insert = []
        for c in payload.comments:
            comment_dict = c.model_dump()
            try:
                # 从 'p' 字段解析时间戳，并添加到字典中
                timestamp_str = comment_dict['p'].split(',')[0]
                comment_dict['t'] = float(timestamp_str)
            except (IndexError, ValueError):
                comment_dict['t'] = 0.0 # 如果解析失败，则默认为0
            comments_to_insert.append(comment_dict)

        added = await crud.bulk_insert_comments(session, episodeId, comments_to_insert)
        raise TaskSuccess(f"弹幕覆盖完成，新增 {added} 条。")
    try:
        task_id, _ = await task_manager.submit_task(overwrite_task, f"外部API覆盖弹幕 (分集ID: {episodeId})")
        return {"message": "弹幕覆盖任务已提交", "taskId": task_id}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

# --- Token 管理 ---

@router.get("/tokens", response_model=List[models.ApiTokenInfo], summary="获取所有Token")
async def get_tokens(session: AsyncSession = Depends(get_db_session)):
    """获取所有为dandanplay客户端创建的API Token。"""
    tokens = await crud.get_all_api_tokens(session)
    return [models.ApiTokenInfo.model_validate(t) for t in tokens]

@router.post("/tokens", response_model=models.ApiTokenInfo, status_code=201, summary="创建Token")
async def create_token(payload: models.ApiTokenCreate, session: AsyncSession = Depends(get_db_session)):
    """创建一个新的API Token。支持自定义或随机生成。"""
    # 自定义 token 校验与重复检查
    if getattr(payload, 'token', None):
        custom = payload.token.strip()
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', custom):
            raise HTTPException(status_code=400, detail="无效的Token格式（8-64位，仅允许字母/数字/下划线/中横线）")
        exists = await crud.get_api_token_by_token_str(session, custom)
        if exists:
            raise HTTPException(status_code=409, detail="该Token已存在")
        token_str = custom
    else:
        token_str = secrets.token_urlsafe(16)
    try:
        # 通过外部控制创建的 Token 不绑定创建者（无法识别用户上下文）。
        token_id = await crud.create_api_token(session, payload.name, token_str, payload.validityPeriod, payload.dailyCallLimit, owner_user_id=None, created_by_user_id=None)
        new_token = await crud.get_api_token_by_id(session, token_id)
        return models.ApiTokenInfo.model_validate(new_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

@router.get("/tokens/{tokenId}", response_model=models.ApiTokenInfo, summary="获取单个Token详情")
async def get_token(tokenId: int, session: AsyncSession = Depends(get_db_session)):
    """获取单个API Token的详细信息。"""
    token = await crud.get_api_token_by_id(session, tokenId)
    if not token: raise HTTPException(404, "Token未找到")
    return models.ApiTokenInfo.model_validate(token)

@router.get("/tokens/{tokenId}/logs", response_model=List[models.TokenAccessLog], summary="获取Token访问日志")
async def get_token_logs(tokenId: int, session: AsyncSession = Depends(get_db_session)):
    """获取单个API Token最近的访问日志。"""
    logs = await crud.get_token_access_logs(session, tokenId)
    return [models.TokenAccessLog.model_validate(log) for log in logs]

@router.put("/tokens/{tokenId}/toggle", response_model=ControlActionResponse, summary="启用/禁用Token")
async def toggle_token(tokenId: int, session: AsyncSession = Depends(get_db_session)):
    """切换API Token的启用/禁用状态。"""
    new_status = await crud.toggle_api_token(session, tokenId)
    if new_status is None:
        raise HTTPException(404, "Token未找到")
    message = "Token 已启用。" if new_status else "Token 已禁用。"
    return {"message": message}

class ControlApiTokenUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Token的描述性名称")
    dailyCallLimit: int = Field(..., description="每日调用次数限制, -1 表示无限")
    validityPeriod: str = Field("custom", description="新的有效期: 'permanent', 'custom', '30d' 等。'custom' 表示不改变当前有效期。")

@router.put("/tokens/{tokenId}", response_model=ControlActionResponse, summary="更新Token信息")
async def update_token(
    tokenId: int,
    payload: ControlApiTokenUpdate,
    session: AsyncSession = Depends(get_db_session)
):
    """更新指定API Token的名称、每日调用上限和有效期。"""
    updated = await crud.update_api_token(
        session,
        token_id=tokenId,
        name=payload.name,
        daily_call_limit=payload.dailyCallLimit,
        validity_period=payload.validityPeriod
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return {"message": "Token信息更新成功。"}

@router.post("/tokens/{tokenId}/reset", response_model=ControlActionResponse, summary="重置Token调用次数")
async def reset_token_counter(
    tokenId: int,
    session: AsyncSession = Depends(get_db_session)
):
    """将指定API Token的今日调用次数重置为0。"""
    reset_ok = await crud.reset_token_counter(session, tokenId)
    if not reset_ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    return {"message": "Token调用次数已重置为0。"}


@router.delete("/tokens/{tokenId}", response_model=ControlActionResponse, summary="删除Token")
async def delete_token(tokenId: int, session: AsyncSession = Depends(get_db_session)):
    """删除一个API Token。"""
    if not await crud.delete_api_token(session, tokenId):
        raise HTTPException(404, "Token未找到")
    return {"message": "Token 删除成功。"}

# --- 设置管理 ---

@router.get("/settings/danmaku-output", response_model=DanmakuOutputSettings, summary="获取弹幕输出设置")
async def get_danmaku_output_settings(session: AsyncSession = Depends(get_db_session)):
    """获取全局的弹幕输出设置，如数量限制和是否聚合。"""
    limit = await crud.get_config_value(session, 'danmaku_output_limit_per_source', '-1')
    enabled = await crud.get_config_value(session, 'danmaku_aggregation_enabled', 'true')
    return DanmakuOutputSettings(limit_per_source=int(limit), aggregation_enabled=(enabled.lower() == 'true'))

@router.put("/settings/danmaku-output", response_model=ControlActionResponse, summary="更新弹幕输出设置")
async def update_danmaku_output_settings(payload: DanmakuOutputSettings, session: AsyncSession = Depends(get_db_session), config_manager: ConfigManager = Depends(get_config_manager)):
    """更新全局的弹幕输出设置。"""
    await crud.update_config_value(session, 'danmaku_output_limit_per_source', str(payload.limitPerSource)) # type: ignore
    await crud.update_config_value(session, 'danmaku_aggregation_enabled', str(payload.aggregationEnabled).lower()) # type: ignore
    config_manager.invalidate('danmaku_output_limit_per_source')
    config_manager.invalidate('danmaku_aggregation_enabled')
    return {"message": "弹幕输出设置已更新。"}

# --- 任务管理 ---

@router.get("/tasks", response_model=List[models.TaskInfo], summary="获取后台任务列表")
async def get_tasks(
    search: Optional[str] = Query(None, description="按标题搜索"),
    status: str = Query("all", description="按状态过滤: all, in_progress, completed"),
    session: AsyncSession = Depends(get_db_session),
):
    """获取后台任务的列表和状态，支持按标题搜索和按状态过滤。"""
    # 修正：为 get_tasks_from_history 提供分页参数，以匹配更新后的函数签名。
    paginated_result = await crud.get_tasks_from_history(session, search, status, page=1, page_size=1000)
    return [models.TaskInfo.model_validate(t) for t in paginated_result["list"]]

@router.get("/tasks/{taskId}", response_model=models.TaskInfo, summary="获取单个任务状态")
async def get_task_status(
    taskId: str,
    session: AsyncSession = Depends(get_db_session)
):
    """获取单个后台任务的详细状态。"""
    task_details = await crud.get_task_details_from_history(session, taskId)
    if not task_details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务未找到。")
    return models.TaskInfo.model_validate(task_details)

@router.delete("/tasks/{taskId}", response_model=ControlActionResponse, summary="删除一个历史任务")
async def delete_task(
    taskId: str,
    session: AsyncSession = Depends(get_db_session),
    task_manager: TaskManager = Depends(get_task_manager),
):
    """
    ### 功能
    删除一个后台任务。
    - **排队中**: 从队列中移除。
    - **运行中/已暂停**: 尝试中止任务，然后删除。
    - **已完成/失败**: 从历史记录中删除。
    """
    task = await crud.get_task_from_history_by_id(session, taskId)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务未找到。")

    task_status = task['status']

    if task_status == TaskStatus.PENDING:
        if await task_manager.cancel_pending_task(taskId):
            logger.info(f"已从队列中取消待处理任务 {taskId}。")
    elif task_status in [TaskStatus.RUNNING, TaskStatus.PAUSED]:
        if await task_manager.abort_current_task(taskId):
            logger.info(f"已发送中止信号到任务 {taskId}。")

    if await crud.delete_task_from_history(session, taskId):
        return {"message": f"删除任务 {taskId} 的请求已处理。"}
    else:
        return {"message": "任务可能已被处理或不存在于历史记录中。"}

@router.post("/tasks/{taskId}/abort", response_model=ControlActionResponse, summary="中止正在运行的任务")
async def abort_task(taskId: str, task_manager: TaskManager = Depends(get_task_manager)):
    """尝试中止一个当前正在运行或已暂停的任务。此操作会向任务发送一个取消信号，任务将在下一个检查点安全退出。"""
    if not await task_manager.abort_current_task(taskId):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="中止任务失败，可能任务已完成或不是当前正在执行的任务。")
    return {"message": "中止任务的请求已发送。"}

@router.post("/tasks/{taskId}/pause", response_model=ControlActionResponse, summary="暂停正在运行的任务")
async def pause_task(taskId: str, task_manager: TaskManager = Depends(get_task_manager)):
    """暂停一个当前正在运行的任务。任务将在下一次进度更新时暂停。"""
    if not await task_manager.pause_task(taskId):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="暂停任务失败，可能任务未在运行。")
    return {"message": "任务已暂停。"}

@router.post("/tasks/{taskId}/resume", response_model=ControlActionResponse, summary="恢复已暂停的任务")
async def resume_task(taskId: str, task_manager: TaskManager = Depends(get_task_manager)):
    """恢复一个已暂停的任务。"""
    if not await task_manager.resume_task(taskId):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="恢复任务失败，可能任务未被暂停。")
    return {"message": "任务已恢复。"}

@router.get("/tasks/{taskId}/execution", response_model=ExecutionTaskResponse, summary="获取调度任务触发的执行任务ID")
async def get_execution_task_id(
    taskId: str,
    session: AsyncSession = Depends(get_db_session)
):
    """
    在调用 `/import/auto` 等接口后，使用返回的调度任务ID来查询其触发的、
    真正执行下载导入工作的任务ID。
    如果调度任务尚未完成或未成功触发执行任务，`executionTaskId` 将为 `null`。
    您可以轮询此接口，直到获取到 `executionTaskId`。
    """
    execution_id = await crud.get_execution_task_id_from_scheduler_task(session, taskId)
    return ExecutionTaskResponse(schedulerTaskId=taskId, executionTaskId=execution_id)

@router.get("/rate-limit/status", response_model=models.ControlRateLimitStatusResponse, summary="获取流控状态")
async def get_rate_limit_status(
    session: AsyncSession = Depends(get_db_session),
    scraper_manager: ScraperManager = Depends(get_scraper_manager),
    rate_limiter: RateLimiter = Depends(get_rate_limiter)
):
    """
    获取所有流控规则的当前状态，包括全局和各源的配额使用情况。
    """
    # 在获取状态前，先触发一次全局流控的检查，这会强制重置过期的计数器
    try:
        await rate_limiter.check("__control_api_status_check__")
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
        time_since_reset = get_now().replace(tzinfo=None) - global_state.lastResetTime
        seconds_until_reset = max(0, int(period_seconds - time_since_reset.total_seconds()))

    provider_items = []
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
        provider_items.append(models.ControlRateLimitProviderStatus(providerName=provider_name, requestCount=provider_state.requestCount if provider_state else 0, quota=quota))

    # 修正：将秒数转换为可读的字符串以匹配响应模型
    global_period_str = f"{period_seconds} 秒"

    return models.ControlRateLimitStatusResponse(
        globalEnabled=global_enabled, 
        globalRequestCount=global_state.requestCount if global_state else 0, 
        globalLimit=global_limit, globalPeriod=global_period_str, 
        secondsUntilReset=seconds_until_reset, providers=provider_items)


# --- 定时任务管理 ---

@router.get("/scheduler/tasks", response_model=List[Dict[str, Any]], summary="获取所有定时任务")
async def list_scheduled_tasks(
    scheduler_manager: SchedulerManager = Depends(get_scheduler_manager),
):
    """获取所有已配置的定时任务及其当前状态。"""
    tasks = await scheduler_manager.get_all_tasks()
    # 修正：将 'id' 键重命名为 'taskId' 以保持API一致性
    return tasks

@router.get("/scheduler/{taskId}/last_result", response_model=models.TaskInfo, summary="获取定时任务的最近一次运行结果")
async def get_scheduled_task_last_result(
    taskId: str = Path(..., description="定时任务的ID"),
    session: AsyncSession = Depends(get_db_session),
):
    """
    获取指定定时任务的最近一次运行结果。
    如果任务从未运行过，将返回 404 Not Found。
    """
    result = await crud.get_last_run_result_for_scheduled_task(session, taskId)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到该定时任务的运行记录。")
    return models.TaskInfo.model_validate(result)
