import asyncio
import logging
import json
import re
import ipaddress
from typing import List, Optional, Dict, Any
from typing import Callable
from datetime import datetime, timezone
from opencc import OpenCC
from thefuzz import fuzz

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from . import crud, models, orm_models
from .config_manager import ConfigManager
from .timezone import get_now, get_app_timezone
from .database import get_db_session
from .utils import parse_search_keyword
from .scraper_manager import ScraperManager

logger = logging.getLogger(__name__)

# --- Module-level Constants for Type Mappings and Parsing ---
# To avoid repetition and improve maintainability.
DANDAN_TYPE_MAPPING = {
    "tv_series": "tvseries", "movie": "movie", "ova": "ova", "other": "other"
}
DANDAN_TYPE_DESC_MAPPING = {
    "tv_series": "TV动画", "movie": "电影/剧场版", "ova": "OVA", "other": "其他"
}
# 新增：用于清理文件名中常见元数据关键词的正则表达式
METADATA_KEYWORDS_PATTERN = re.compile(
    r'1080p|720p|2160p|4k|bluray|x264|h\s*\.?\s*264|hevc|x265|h\s*\.?\s*265|aac|flac|web-dl|BDRip|WEBRip|TVRip|DVDrip|AVC|CHT|CHS|BIG5|GB|10bit|8bit',
    re.IGNORECASE
)

# 这个子路由将包含所有接口的实际实现。
# 它将被挂载到主路由的不同路径上。
implementation_router = APIRouter()

def _process_comments_for_dandanplay(comments_data: List[Dict[str, Any]]) -> List[models.Comment]:
    """
    将弹幕字典列表处理为符合 dandanplay 客户端规范的格式。
    核心逻辑是移除 p 属性中的字体大小参数，同时保留其他所有部分。
    原始格式: "时间,模式,字体大小,颜色,[来源]"
    目标格式: "时间,模式,颜色,[来源]"
    """
    processed_comments = []
    for i, item in enumerate(comments_data):
        p_attr = item.get("p", "")
        p_parts = p_attr.split(',')

        # 查找可选的用户标签（如[bilibili]），以确定核心参数的数量
        core_parts_count = len(p_parts)
        for j, part in enumerate(p_parts):
            if '[' in part and ']' in part:
                core_parts_count = j
                break
        
        if core_parts_count == 4:
            del p_parts[2] # 移除字体大小 (index 2)
        
        new_p_attr = ','.join(p_parts)
        processed_comments.append(models.Comment(cid=i, p=new_p_attr, m=item.get("m", "")))
    return processed_comments


class DandanApiRoute(APIRoute):
    """
    自定义的 APIRoute 类，用于为 dandanplay 兼容接口定制异常处理。
    捕获 HTTPException，并以 dandanplay API v2 的格式返回错误信息。
    """
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except HTTPException as exc:
                # 简单的 HTTP 状态码到 dandanplay 错误码的映射
                # 1001: 无效的参数
                # 1003: 未授权或资源不可用
                # 404: 未找到
                # 500: 服务器内部错误
                error_code_map = {
                    status.HTTP_400_BAD_REQUEST: 1001,
                    status.HTTP_422_UNPROCESSABLE_ENTITY: 1001,
                    # 新增：将404也映射到1003，对外统一表现为“资源不可用”
                    status.HTTP_404_NOT_FOUND: 1003,
                    status.HTTP_403_FORBIDDEN: 1003,
                    status.HTTP_500_INTERNAL_SERVER_ERROR: 500,
                }
                error_code = error_code_map.get(exc.status_code, 1003) # 默认客户端错误为1003

                # 为常见的错误代码提供更统一的错误消息
                error_message = "请求的资源不可用或您没有权限访问。" if error_code == 1003 else exc.detail

                # 始终返回 200 OK，错误信息在 JSON body 中体现
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content={
                        "success": False,
                        "errorCode": error_code,
                        "errorMessage": error_message,
                    },
                )
        return custom_route_handler

async def get_config_manager(request: Request) -> ConfigManager:
    """依赖项：从应用状态获取配置管理器"""
    return request.app.state.config_manager

# 这是将包含在 main.py 中的主路由。
# 使用自定义的 Route 类来应用特殊的异常处理。
dandan_router = APIRouter(route_class=DandanApiRoute)

class DandanResponseBase(BaseModel):
    """模仿 dandanplay API v2 的基础响应模型"""
    success: bool = True
    errorCode: int = 0
    errorMessage: str = Field("", description="错误信息")


class DandanEpisodeInfo(BaseModel):
    """dandanplay /search/episodes 接口中的分集信息模型"""
    episodeId: int
    episodeTitle: str

class DandanAnimeInfo(BaseModel):
    """dandanplay /search/episodes 接口中的番剧信息模型"""
    animeId: int
    animeTitle: str
    imageUrl: str = ""
    searchKeyword: str = ""
    type: str
    typeDescription: str
    isOnAir: bool = False
    airDay: int = 0
    isFavorited: bool = False
    rating: float = 0.0
    episodes: List[DandanEpisodeInfo]

class DandanSearchEpisodesResponse(DandanResponseBase):
    hasMore: bool = False
    animes: List[DandanAnimeInfo]

# --- Models for /search/anime ---
class DandanSearchAnimeItem(BaseModel):
    animeId: int
    bangumiId: Optional[str] = ""
    animeTitle: str
    type: str
    typeDescription: str
    imageUrl: Optional[str] = None    
    startDate: Optional[str] = None # To keep compatibility, but will be populated from year
    year: Optional[int] = None
    episodeCount: int
    rating: float = 0.0
    isFavorited: bool = False

class DandanSearchAnimeResponse(DandanResponseBase):
    animes: List[DandanSearchAnimeItem]


# --- Models for /bangumi/{anime_id} ---

class BangumiTitle(BaseModel):
    language: str
    title: str

class BangumiEpisodeSeason(BaseModel):
    id: str
    airDate: Optional[datetime] = None
    name: str
    episodeCount: int
    summary: str

class BangumiEpisode(BaseModel):
    seasonId: Optional[str] = None
    episodeId: int
    episodeTitle: str
    episodeNumber: str
    lastWatched: Optional[datetime] = None
    airDate: Optional[datetime] = None

class BangumiIntro(BaseModel):
    animeId: int
    bangumiId: Optional[str] = ""
    animeTitle: str
    imageUrl: Optional[str] = None
    searchKeyword: Optional[str] = None
    isOnAir: bool = False
    airDay: int = 0
    isRestricted: bool = False
    rating: float = 0.0

class BangumiTag(BaseModel):
    id: int
    name: str
    count: int

class BangumiOnlineDatabase(BaseModel):
    name: str
    url: str

class BangumiTrailer(BaseModel):
    id: int
    url: str
    title: str
    imageUrl: str
    date: datetime

class BangumiDetails(BangumiIntro):
    type: str
    typeDescription: str
    titles: List[BangumiTitle] = []
    seasons: List[BangumiEpisodeSeason] = []
    episodes: List[BangumiEpisode] = []
    summary: Optional[str] = ""
    metadata: List[str] = []
    year: Optional[int] = None
    userRating: int = 0
    favoriteStatus: Optional[str] = None
    comment: Optional[str] = None
    ratingDetails: Dict[str, float] = {}
    relateds: List[BangumiIntro] = []
    similars: List[BangumiIntro] = []
    tags: List[BangumiTag] = []
    onlineDatabases: List[BangumiOnlineDatabase] = []
    trailers: List[BangumiTrailer] = []

class BangumiDetailsResponse(DandanResponseBase):
    bangumi: Optional[BangumiDetails] = None

# --- Models for /match ---

class DandanMatchInfo(BaseModel):
    episodeId: int
    animeId: int
    animeTitle: str
    episodeTitle: str
    type: str
    typeDescription: str
    shift: int = 0

class DandanMatchResponse(DandanResponseBase):
    isMatched: bool = False
    matches: List[DandanMatchInfo] = []

# --- Models for /match/batch ---

class DandanBatchMatchRequestItem(BaseModel):
    fileName: str
    fileHash: Optional[str] = None
    fileSize: Optional[int] = None
    videoDuration: Optional[int] = None
    matchMode: Optional[str] = "hashAndFileName"

class DandanBatchMatchRequest(BaseModel):
    requests: List[DandanBatchMatchRequestItem]


async def _search_implementation(
    search_term: str,
    episode: Optional[str],
    session: AsyncSession
) -> DandanSearchEpisodesResponse:
    """搜索接口的通用实现，避免代码重复。"""
    search_term = search_term.strip()
    if not search_term:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing required query parameter: 'anime' or 'keyword'"
        )

    # 修正：调用 utils 中的全局解析函数，以保持逻辑统一
    parsed_info = parse_search_keyword(search_term)
    title_to_search = parsed_info["title"]
    season_to_search = parsed_info.get("season")
    episode_from_title = parsed_info.get("episode")

    # 优先使用独立的 'episode' 参数
    episode_number_from_param = int(episode) if episode and episode.isdigit() else None
    final_episode_to_search = episode_number_from_param if episode_number_from_param is not None else episode_from_title

    # 使用解析后的信息进行数据库查询
    flat_results = await crud.search_episodes_in_library(
        session,
        anime_title=title_to_search,
        episode_number=final_episode_to_search,
        season_number=season_to_search
    )

    grouped_animes: Dict[int, DandanAnimeInfo] = {}

    for res in flat_results:
        anime_id = res['animeId']
        if anime_id not in grouped_animes:
            dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
            dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")

            grouped_animes[anime_id] = DandanAnimeInfo(
                animeId=anime_id,
                animeTitle=res['animeTitle'],
                imageUrl=res.get('imageUrl') or "",
                searchKeyword=search_term or "",
                type=dandan_type,
                typeDescription=dandan_type_desc,
                # isFavorited 字段现在由数据库查询提供
                isFavorited=res.get('isFavorited', False),
                episodes=[]
            )
        
        grouped_animes[anime_id].episodes.append(
            DandanEpisodeInfo(episodeId=res['episodeId'], episodeTitle=res['episodeTitle'])
        )
    
    return DandanSearchEpisodesResponse(animes=list(grouped_animes.values()))

def _parse_filename_for_match(filename: str) -> Optional[Dict[str, Any]]:
    """
    使用正则表达式从文件名中解析出番剧标题和集数。
    这是一个简化的实现，用于 dandanplay 兼容接口。
    """
    # 移除文件扩展名
    name_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename

    # 模式1: SXXEXX 格式 (e.g., "Some.Anime.S01E02.1080p.mkv")
    s_e_pattern = re.compile(
        r"^(?P<title>.+?)"
        r"[\s._-]*"
        r"[Ss](?P<season>\d{1,2})"
        r"[Ee](?P<episode>\d{1,4})"
        r"\b",
        re.IGNORECASE
    )
    match = s_e_pattern.search(name_without_ext)
    if match:
        data = match.groupdict()
        title = data["title"].replace(".", " ").replace("_", " ").strip()
        title = re.sub(r'\[.*?\]', '', title).strip() # 移除字幕组标签
        # 新增：移除标题中的年份并清理多余空格
        title = re.sub(r'\b(19|20)\d{2}\b', '', title).strip()
        title = re.sub(r'\s+', ' ', title).strip(' -')
        return {
            "title": title,
            "season": int(data["season"]),
            "episode": int(data["episode"])
        }

    # 模式2: 只有集数 (e.g., "[Subs] Some Anime - 02 [1080p].mkv")
    ep_only_patterns = [
        re.compile(r"^(?P<title>.+?)\s*[-_]\s*\b(?P<episode>\d{1,4})\b", re.IGNORECASE),
        re.compile(r"^(?P<title>.+?)\s+\b(?P<episode>\d{1,4})\b", re.IGNORECASE),
    ]
    for pattern in ep_only_patterns:
        match = pattern.search(name_without_ext)
        if match:
            data = match.groupdict()
            title = data["title"]
            # 清理标题中的元数据
            title = re.sub(r'\[.*?\]|\(.*?\)|\【.*?\】', '', title).strip()
            title = METADATA_KEYWORDS_PATTERN.sub('', title).strip()
            title = title.replace("_", " ").replace(".", " ").strip()
            # 新增：移除标题中的年份并清理多余空格
            title = re.sub(r'\b(19|20)\d{2}\b', '', title).strip()
            title = re.sub(r'\s+', ' ', title).strip(' -')
            return {
                "title": title,
                "season": None, # 此模式无法识别季度
                "episode": int(data["episode"]),
            }
    
    # 模式3: 电影或单文件视频 (没有集数)
    title = name_without_ext
    title = re.sub(r'\[.*?\]|\(.*?\)|\【.*?\】', '', title).strip()
    title = METADATA_KEYWORDS_PATTERN.sub('', title).strip()
    title = title.replace("_", " ").replace(".", " ").strip()
    # 移除年份, 兼容括号内和独立两种形式
    title = re.sub(r'\(\s*(19|20)\d{2}\s*\)', '', title).strip()
    title = re.sub(r'\b(19|20)\d{2}\b', '', title).strip()
    title = re.sub(r'\s+', ' ', title).strip(' -')
    
    if title:
        return {
            "title": title,
            "season": 1, # 对电影，默认匹配第1季
            "episode": 1, # 和第1集
        }

    return None


async def get_token_from_path(
    request: Request,
    token: str = Path(..., description="路径中的API授权令牌"),
    session: AsyncSession = Depends(get_db_session),
):
    """
    一个 FastAPI 依赖项，用于验证路径中的 token。
    这是为 dandanplay 客户端设计的特殊鉴权方式。
    此函数现在还负责UA过滤和访问日志记录。
    """
    # --- 新增：解析真实客户端IP ---
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
            # 如果没有 X-Forwarded-For，则回退到 X-Real-IP
            client_ip_str = request.headers.get("x-real-ip", client_ip_str)
    # --- IP解析结束 ---

    # 1. 验证 token 是否存在、启用且未过期
    request_path = request.url.path
    log_path = re.sub(r'^/api/v1/[^/]+', '', request_path) # 从路径中移除 /api/v1/{token} 部分

    token_info = await crud.validate_api_token(session, token=token)
    if not token_info: 
        # 尝试记录失败的访问
        token_record = await crud.get_api_token_by_token_str(session, token)
        if token_record:
            expires_at = token_record.get('expiresAt')
            is_expired = False
            if expires_at:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=get_app_timezone())
                is_expired = expires_at < get_now()
            status_to_log = 'denied_expired' if is_expired else 'denied_disabled'
            await crud.create_token_access_log(session, token_record['id'], client_ip_str, request.headers.get("user-agent"), log_status=status_to_log, path=log_path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API token")

    # 2. UA 过滤
    ua_filter_mode = await crud.get_config_value(session, 'uaFilterMode', 'off')
    user_agent = request.headers.get("user-agent", "")

    if ua_filter_mode != 'off':
        ua_rules = await crud.get_ua_rules(session)
        ua_list = [rule['uaString'] for rule in ua_rules]
        
        is_matched = any(rule in user_agent for rule in ua_list)

        if ua_filter_mode == 'blacklist' and is_matched:
            await crud.create_token_access_log(session, token_info['id'], client_ip_str, user_agent, log_status='denied_ua_blacklist', path=log_path)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User-Agent is blacklisted")
        
        if ua_filter_mode == 'whitelist' and not is_matched:
            await crud.create_token_access_log(session, token_info['id'], client_ip_str, user_agent, log_status='denied_ua_whitelist', path=log_path)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User-Agent not in whitelist")

    # 3. 用户级流控检查（基于 Token 归属的用户）
    owner_user_id = token_info.get('ownerUserId') if isinstance(token_info, dict) else None
    if owner_user_id:
        # 获取完整用户信息并执行用户级限额检查
        user_full = await crud.get_user_by_id(session, owner_user_id)
        try:
            await crud.check_and_increment_user_rate(session, user_full or {'id': owner_user_id})
        except PermissionError:
            # 记录被用户限额拒绝的访问
            await crud.create_token_access_log(session, token_info['id'], client_ip_str, request.headers.get("user-agent"), log_status='denied_user_rate_limit', path=log_path)
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="User rate limit exceeded")

    # 4. 增加调用计数 (在所有验证通过后)
    await crud.increment_token_call_count(session, token_info['id'])
    await session.commit()

    # 5. 记录成功访问
    await crud.create_token_access_log(session, token_info['id'], client_ip_str, user_agent, log_status='allowed', path=log_path)

    return token

async def get_scraper_manager(request: Request) -> ScraperManager:
    """依赖项：从应用状态获取 Scraper 管理器"""
    return request.app.state.scraper_manager

@implementation_router.get(
    "/search/episodes",
    response_model=DandanSearchEpisodesResponse,
    summary="[dandanplay兼容] 搜索节目和分集"
)
async def search_episodes_for_dandan(
    anime: str = Query(..., description="节目名称"),
    episode: Optional[str] = Query(None, description="分集标题 (通常是数字)"),
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session)
):
    """
    模拟 dandanplay 的 /api/v2/search/episodes 接口。
    它会搜索 **本地弹幕库** 中的番剧和分集信息。
    """
    search_term = anime.strip()
    return await _search_implementation(search_term, episode, session)

@implementation_router.get(
    "/search/anime",
    response_model=DandanSearchAnimeResponse,
    summary="[dandanplay兼容] 搜索作品"
)
async def search_anime_for_dandan(
    keyword: Optional[str] = Query(None, description="节目名称 (兼容 keyword)"),
    anime: Optional[str] = Query(None, description="节目名称 (兼容 anime)"),
    episode: Optional[str] = Query(None, description="分集标题 (此接口中未使用)"),
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session)
):
    """
    模拟 dandanplay 的 /api/v2/search/anime 接口。
    它会搜索 **本地弹幕库** 中的番剧信息，不包含分集列表。
    """
    search_term = keyword or anime
    if not search_term:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing required query parameter: 'keyword' or 'anime'"
        )

    db_results = await crud.search_animes_for_dandan(session, search_term)
    
    animes = []
    for res in db_results:
        dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
        dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")
        year = res.get('year')
        start_date_str = None
        if year:
            start_date_str = datetime(year, 1, 1, tzinfo=get_app_timezone()).isoformat()
        elif res.get('startDate'):
            start_date_str = res.get('startDate').isoformat()

        animes.append(DandanSearchAnimeItem(
            animeId=res['animeId'],
            bangumiId=res.get('bangumiId') or f"A{res['animeId']}",
            animeTitle=res['animeTitle'],
            type=dandan_type,
            typeDescription=dandan_type_desc,
            imageUrl=res.get('imageUrl'),
            startDate=start_date_str,
            year=year,
            episodeCount=res.get('episodeCount', 0),
            # 显式设置默认值以提高代码清晰度
            rating=0.0,  # 当前系统未实现评级功能
            isFavorited=False  # 搜索结果默认不标记为收藏
        ))
    
    return DandanSearchAnimeResponse(animes=animes)

@implementation_router.get(
    "/bangumi/{bangumiId}",
    response_model=BangumiDetailsResponse,
    summary="[dandanplay兼容] 获取番剧详情"
)
async def get_bangumi_details(
    bangumiId: str = Path(..., description="作品ID, A开头的备用ID, 或真实的Bangumi ID"),
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session)
):
    """
    模拟 dandanplay 的 /api/v2/bangumi/{bangumiId} 接口。
    返回数据库中存储的番剧详细信息。
    """
    anime_id_int: Optional[int] = None
    if bangumiId.startswith('A') and bangumiId[1:].isdigit():
        # 格式1: "A" + animeId, 例如 "A123"
        anime_id_int = int(bangumiId[1:])
    elif bangumiId.isdigit():
        # 格式2: 纯数字的 Bangumi ID, 例如 "148099"
        # 我们需要通过 bangumi_id 找到我们自己数据库中的 anime_id
        anime_id_int = await crud.get_anime_id_by_bangumi_id(session, bangumiId)


    if anime_id_int is None:
        return BangumiDetailsResponse(
            success=True,
            bangumi=None,
            errorMessage=f"找不到与标识符 '{bangumiId}' 关联的作品。"
        )

    details = await crud.get_anime_details_for_dandan(session, anime_id_int)
    if not details:
        return BangumiDetailsResponse(
            success=True,
            bangumi=None,
            errorMessage=f"在数据库中找不到ID为 {anime_id_int} 的作品详情。"
        )

    anime_data = details['anime']
    episodes_data = details['episodes']

    dandan_type = DANDAN_TYPE_MAPPING.get(anime_data.get('type'), "other")
    dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(anime_data.get('type'), "其他")

    formatted_episodes = [
        BangumiEpisode(
            episodeId=ep['episodeId'],
            episodeTitle=ep['episodeTitle'],
            episodeNumber=str(ep['episodeNumber'])
        ) for ep in episodes_data
    ]

    bangumi_id_str = anime_data.get('bangumiId') or f"A{anime_data['animeId']}"

    bangumi_details = BangumiDetails(
        animeId=anime_data['animeId'],
        bangumiId=bangumi_id_str,
        animeTitle=anime_data['animeTitle'],
        imageUrl=anime_data.get('imageUrl'),
        searchKeyword=anime_data['animeTitle'],
        type=dandan_type,
        typeDescription=dandan_type_desc,
        episodes=formatted_episodes,
        year=anime_data.get('year'),
        summary="暂无简介",
    )

    return BangumiDetailsResponse(bangumi=bangumi_details)

async def _get_match_for_item(item: DandanBatchMatchRequestItem, session: AsyncSession) -> DandanMatchResponse:
    """
    通过文件名匹配弹幕库的核心逻辑。此接口不使用文件Hash。
    优先进行库内直接匹配，失败后回退到TMDB剧集组映射。
    """
    logger.info(f"执行匹配逻辑, 文件名: '{item.fileName}'")
    parsed_info = _parse_filename_for_match(item.fileName)
    logger.info(f"文件名解析结果: {parsed_info}")
    if not parsed_info:
        response = DandanMatchResponse(isMatched=False)
        logger.info(f"发送匹配响应 (解析失败): {response.model_dump_json(indent=2)}")
        return response

    # --- 步骤 1: 优先进行库内直接搜索 ---
    logger.info("正在进行库内直接搜索...")
    results = await crud.search_episodes_in_library(
        session, parsed_info["title"], parsed_info["episode"], parsed_info.get("season")
    )
    logger.info(f"直接搜索为 '{parsed_info['title']}' (季:{parsed_info.get('season')} 集:{parsed_info.get('episode')}) 找到 {len(results)} 条记录")
    
    if results:
        # 对结果进行严格的标题过滤，避免模糊匹配带来的问题
        normalized_search_title = parsed_info["title"].replace("：", ":").replace(" ", "")
        exact_matches = []
        for r in results:
            all_titles_to_check = [
                r.get('animeTitle'), r.get('nameEn'), r.get('nameJp'), r.get('nameRomaji'),
                r.get('aliasCn1'), r.get('aliasCn2'), r.get('aliasCn3'),
            ]
            aliases_to_check = {t for t in all_titles_to_check if t}
            if any(fuzz.partial_ratio(alias, parsed_info["title"]) > 85 for alias in aliases_to_check):
                exact_matches.append(r)

        if len(exact_matches) < len(results):
            logger.info(f"过滤掉 {len(results) - len(exact_matches)} 条模糊匹配的结果。")
            results = exact_matches

        if results:
            # 优先处理被精确标记的源
            favorited_results = [r for r in results if r.get('isFavorited')]
            if favorited_results:
                res = favorited_results[0]
                dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
                dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")
                match = DandanMatchInfo(
                    episodeId=res['episodeId'], animeId=res['animeId'], animeTitle=res['animeTitle'],
                    episodeTitle=res['episodeTitle'], type=dandan_type, typeDescription=dandan_type_desc,
                )
                response = DandanMatchResponse(isMatched=True, matches=[match])
                logger.info(f"发送匹配响应 (精确标记匹配): {response.model_dump_json(indent=2)}")
                return response

            # 如果没有精确标记，检查所有匹配项是否都指向同一个番剧ID
            first_animeId = results[0]['animeId']
            all_from_same_anime = all(res['animeId'] == first_animeId for res in results)

            if all_from_same_anime:
                res = results[0]
                dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
                dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")
                match = DandanMatchInfo(
                    episodeId=res['episodeId'], animeId=res['animeId'], animeTitle=res['animeTitle'],
                    episodeTitle=res['episodeTitle'], type=dandan_type, typeDescription=dandan_type_desc,
                )
                response = DandanMatchResponse(isMatched=True, matches=[match])
                logger.info(f"发送匹配响应 (单一作品匹配): {response.model_dump_json(indent=2)}")
                return response

            # 如果匹配到了多个不同的番剧，则返回所有结果让用户选择
            matches = []
            for res in results:
                dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
                dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")
                matches.append(DandanMatchInfo(
                    episodeId=res['episodeId'], animeId=res['animeId'], animeTitle=res['animeTitle'],
                    episodeTitle=res['episodeTitle'], type=dandan_type, typeDescription=dandan_type_desc,
                ))
            response = DandanMatchResponse(isMatched=False, matches=matches)
            logger.info(f"发送匹配响应 (多个匹配): {response.model_dump_json(indent=2)}")
            return response

    # --- 步骤 2: 如果直接搜索无果，则回退到 TMDB 映射 ---
    logger.info("直接搜索未找到精确匹配，回退到 TMDB 映射匹配。")
    potential_animes = await crud.find_animes_for_matching(session, parsed_info["title"])
    logger.info(f"为标题 '{parsed_info['title']}' 找到 {len(potential_animes)} 个可能的库内作品进行TMDB匹配。")

    for anime in potential_animes:
        if anime.get("tmdbId") and anime.get("tmdbEpisodeGroupId"):
            logger.info(f"正在为作品 ID {anime['animeId']} (TMDB ID: {anime['tmdbId']}) 尝试 TMDB 映射匹配...")
            tmdb_results = await crud.find_episode_via_tmdb_mapping(
                session,
                tmdb_id=anime["tmdbId"],
                group_id=anime["tmdbEpisodeGroupId"],
                custom_season=parsed_info.get("season"),
                custom_episode=parsed_info["episode"]
            )
            if tmdb_results:
                logger.info(f"TMDB 映射匹配成功，找到 {len(tmdb_results)} 个结果。")
                res = tmdb_results[0]
                dandan_type = DANDAN_TYPE_MAPPING.get(res.get('type'), "other")
                dandan_type_desc = DANDAN_TYPE_DESC_MAPPING.get(res.get('type'), "其他")
                match = DandanMatchInfo(
                    episodeId=res['episodeId'], animeId=res['animeId'], animeTitle=res['animeTitle'],
                    episodeTitle=res['episodeTitle'], type=dandan_type, typeDescription=dandan_type_desc,
                )
                response = DandanMatchResponse(isMatched=True, matches=[match])
                logger.info(f"发送匹配响应 (TMDB 映射匹配): {response.model_dump_json(indent=2)}")
                return response

    # --- 步骤 3: 如果所有方法都失败 ---
    response = DandanMatchResponse(isMatched=False, matches=[])
    logger.info(f"发送匹配响应 (所有方法均未匹配): {response.model_dump_json(indent=2)}")
    return response

@implementation_router.post(
    "/match",
    response_model=DandanMatchResponse,
    summary="[dandanplay兼容] 匹配单个文件"
)
async def match_single_file(
    request: DandanBatchMatchRequestItem,
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session)
):
    """
    通过文件名匹配弹幕库。此接口不使用文件Hash。
    优先进行库内直接匹配，失败后回退到TMDB剧集组映射。
    """
    return await _get_match_for_item(request, session)


@implementation_router.post(
    "/match/batch",
    response_model=List[DandanMatchResponse],
    summary="[dandanplay兼容] 批量匹配文件"
)
async def match_batch_files(
    request: DandanBatchMatchRequest,
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session)
):
    """
    批量匹配文件。
    """
    if len(request.requests) > 32:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="批量匹配请求不能超过32个文件。")

    tasks = [_get_match_for_item(item, session) for item in request.requests]
    results = await asyncio.gather(*tasks)
    return results


@implementation_router.get(
    "/extcomment",
    response_model=models.CommentResponse,
    summary="[dandanplay兼容] 获取外部弹幕"
)
async def get_external_comments_from_url(
    url: str = Query(..., description="外部视频链接 (支持 Bilibili, 腾讯, 爱奇艺, 优酷, 芒果TV)"),
    chConvert: int = Query(0, description="中文简繁转换。0-不转换，1-转换为简体，2-转换为繁体。"),
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session),
    manager: ScraperManager = Depends(get_scraper_manager)
):
    """
    从外部URL获取弹幕，并转换为dandanplay格式。
    结果会被缓存5小时。
    """
    cache_key = f"ext_danmaku_v2_{url}"
    cached_comments = await crud.get_cache(session, cache_key)
    if cached_comments is not None:
        logger.info(f"外部弹幕缓存命中: {url}")
        comments_data = cached_comments
    else:
        logger.info(f"外部弹幕缓存未命中，正在从网络获取: {url}")
        scraper = manager.get_scraper_by_domain(url)
        if not scraper:
            raise HTTPException(status_code=400, detail="不支持的URL或视频源。")

        try:
            provider_episode_id = await scraper.get_id_from_url(url)
            if not provider_episode_id:
                raise ValueError(f"无法从URL '{url}' 中解析出有效的视频ID。")
            
            episode_id_for_comments = scraper.format_episode_id_for_comments(provider_episode_id)
            comments_data = await scraper.get_comments(episode_id_for_comments)

            # 修正：使用 scraper.provider_name 修复未定义的 'provider' 变量
            if not comments_data: logger.warning(f"未能从 {scraper.provider_name} URL 获取任何弹幕: {url}")

        except Exception as e:
            logger.error(f"处理 {scraper.provider_name} 外部弹幕时出错: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"获取 {scraper.provider_name} 弹幕失败。")

        # 缓存结果5小时 (18000秒)
        await crud.set_cache(session, cache_key, comments_data, 18000)

    # 处理简繁转换
    if chConvert in [1, 2]:
        converter = None
        if chConvert == 1:
            converter = OpenCC('t2s')  # 繁转简
        elif chConvert == 2:
            converter = OpenCC('s2t')  # 简转繁
        
        if converter:
            for comment in comments_data:
                comment['m'] = converter.convert(comment['m'])

    # 修正：使用统一的弹幕处理函数，以确保输出格式符合 dandanplay 客户端规范
    processed_comments = _process_comments_for_dandanplay(comments_data)
    return models.CommentResponse(count=len(processed_comments), comments=processed_comments)

@implementation_router.get(
    "/comment/{episodeId}",
    response_model=models.CommentResponse,
    summary="[dandanplay兼容] 获取弹幕"
)
async def get_comments_for_dandan(
    episodeId: int = Path(..., description="分集ID (来自 /search/episodes 响应中的 episodeId)"),
    chConvert: int = Query(0, description="中文简繁转换。0-不转换，1-转换为简体，2-转换为繁体。"),
    # 'from' 是 Python 的关键字，所以我们必须使用别名
    fromTime: int = Query(0, alias="from", description="弹幕开始时间(秒)"),
    withRelated: bool = Query(True, description="是否包含关联弹幕"),
    token: str = Depends(get_token_from_path),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager)
):
    """
    模拟 dandanplay 的弹幕获取接口。
    """
    # 弹幕聚合功能已移除，直接从文件获取弹幕
    comments_data = await crud.fetch_comments(session, episodeId)

    # 应用输出数量限制
    limit_str = await config_manager.get('danmaku_output_limit_per_source', '-1')
    try:
        limit = int(limit_str)
    except (ValueError, TypeError):
        limit = -1

    if limit > 0 and len(comments_data) > limit:
        logger.info(f"弹幕数量 ({len(comments_data)}) 超出限制 ({limit})，将进行均匀采样。")
        
        def get_timestamp(comment):
            try: return float(comment['p'].split(',')[0])
            except (ValueError, IndexError): return float('inf')

        comments_data.sort(key=get_timestamp)
        
        step = len(comments_data) / limit
        sampled_comments = []
        for i in range(limit):
            index = round(i * step)
            if index < len(comments_data): sampled_comments.append(comments_data[index])
        comments_data = sampled_comments
        logger.info(f"采样后弹幕数量: {len(comments_data)}")

    # 如果客户端请求了繁简转换，则在此处处理
    if chConvert in [1, 2]:
        converter = None
        if chConvert == 1:
            converter = OpenCC('t2s')  # Traditional to Simplified
        elif chConvert == 2:
            converter = OpenCC('s2t')  # Simplified to Traditional
        
        if converter:
            for comment in comments_data:
                comment['m'] = converter.convert(comment['m'])

    # 为了避免日志过长，只打印部分弹幕作为示例
    log_limit = 5
    comments_to_log = comments_data[:log_limit]
    log_message = {
        "total_comments": len(comments_data),
        "comments_sample": comments_to_log
    }
    # UA 已由 get_token_from_path 依赖项记录
    # logger.info(f"弹幕接口响应 (episodeId: {episodeId}):\n{json.dumps(log_message, indent=2, ensure_ascii=False)}")

    # 修正：使用统一的弹幕处理函数，以确保输出格式符合 dandanplay 客户端规范
    processed_comments = _process_comments_for_dandanplay(comments_data)

    return models.CommentResponse(count=len(processed_comments), comments=processed_comments)

# --- 路由挂载 ---
# 将实现路由挂载到主路由上，以支持两种URL结构。

# 2. 挂载以支持兼容路径: /{token}/api/v2/...
dandan_router.include_router(implementation_router, prefix="/{token}/api/v2")
# 1. 挂载以支持直接路径: /{token}/...
dandan_router.include_router(implementation_router, prefix="/{token}")
