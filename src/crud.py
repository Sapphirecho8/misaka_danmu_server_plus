import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any, Type, Tuple
import xml.etree.ElementTree as ET
from pathlib import Path

from sqlalchemy import select, func, delete, update, and_, or_, text, distinct, case, exc
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload, joinedload, aliased, DeclarativeBase
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.sql.elements import ColumnElement

from . import models
from . import orm_models
from .orm_models import ( # noqa: F401
    Anime, AnimeSource, Episode, User, Scraper, AnimeMetadata, Config, CacheData, ApiToken, TokenAccessLog, UaRule, BangumiAuth, OauthState, AnimeAlias, TmdbEpisodeMapping, ScheduledTask, TaskHistory, MetadataSource, ExternalApiLog, WebhookTask
, RateLimitState, UserRateLimitState, UserUsageLog, Invite)
from .config import settings
from .timezone import get_now
from .danmaku_parser import parse_dandan_xml_to_comments

logger = logging.getLogger(__name__)

# --- 新增：文件存储相关常量和辅助函数 ---
DANMAKU_BASE_DIR = Path("/app/config/danmaku")

def _generate_xml_from_comments(
    comments: List[Dict[str, Any]], 
    episode_id: int, 
    provider_name: Optional[str] = "misaka",
    chat_server: Optional[str] = "danmaku.misaka.org"
) -> str:
    """根据弹幕字典列表生成符合dandanplay标准的XML字符串。"""
    root = ET.Element('i')
    ET.SubElement(root, 'chatserver').text = chat_server
    ET.SubElement(root, 'chatid').text = str(episode_id)
    ET.SubElement(root, 'mission').text = '0'
    ET.SubElement(root, 'maxlimit').text = '2000'
    ET.SubElement(root, 'source').text = 'k-v' # 保持与官方格式一致
    # 新增字段
    ET.SubElement(root, 'sourceprovider').text = provider_name
    ET.SubElement(root, 'datasize').text = str(len(comments))
    
    for comment in comments:
        p_attr = str(comment.get('p', ''))
        d = ET.SubElement(root, 'd', p=p_attr)
        d.text = comment.get('m', '')
    return ET.tostring(root, encoding='unicode', xml_declaration=True)

def _get_fs_path_from_web_path(web_path: Optional[str]) -> Optional[Path]:
    """
    将Web路径（例如 /data/danmaku/1/2.xml 或 /danmaku/1/2.xml）转换为文件系统路径。
    这个辅助函数通过查找 '/danmaku/' 标记来健壮地处理新旧两种路径格式。
    """
    if not web_path:
        return None
    
    if '/danmaku/' in web_path:
        relative_part = web_path.split('/danmaku/', 1)[1]
        return DANMAKU_BASE_DIR / relative_part
    
    logger.warning(f"无法从Web路径 '{web_path}' 解析文件系统路径，因为它不包含 '/danmaku/'。")
    return None
# --- Anime & Library ---

async def get_library_anime(session: AsyncSession, keyword: Optional[str] = None, page: int = 1, page_size: int = -1) -> Dict[str, Any]:
    """获取媒体库中的所有番剧及其关联信息（如分集数），支持搜索和分页。"""
    stmt = (
        select(
            Anime.id.label("animeId"),
            Anime.localImagePath.label("localImagePath"),
            Anime.imageUrl.label("imageUrl"),
            Anime.title,
            Anime.type,
            Anime.season,
            Anime.year,
            Anime.createdAt.label("createdAt"),
            case(
                (Anime.type == 'movie', 1),
                else_=func.coalesce(func.max(Episode.episodeIndex), 0)
            ).label("episodeCount"),
            func.count(distinct(AnimeSource.id)).label("sourceCount")
        )
        .join(AnimeSource, Anime.id == AnimeSource.animeId, isouter=True)
        .join(Episode, AnimeSource.id == Episode.sourceId, isouter=True)
        .join(AnimeAlias, Anime.id == AnimeAlias.animeId, isouter=True)
        .group_by(Anime.id)
    )

    if keyword:
        clean_keyword = keyword.strip()
        if clean_keyword:
            normalized_like_keyword = f"%{clean_keyword.replace('：', ':').replace(' ', '')}%"
            like_conditions = [
                func.replace(func.replace(col, '：', ':'), ' ', '').like(normalized_like_keyword)
                for col in [Anime.title, AnimeAlias.nameEn, AnimeAlias.nameJp, AnimeAlias.nameRomaji, AnimeAlias.aliasCn1, AnimeAlias.aliasCn2, AnimeAlias.aliasCn3]
            ]
            stmt = stmt.where(or_(*like_conditions))

    count_subquery = stmt.alias("count_subquery")
    count_stmt = select(func.count()).select_from(count_subquery)
    total_count = (await session.execute(count_stmt)).scalar_one()

    data_stmt = stmt.order_by(Anime.createdAt.desc())
    if page_size > 0:
        offset = (page - 1) * page_size
        data_stmt = data_stmt.offset(offset).limit(page_size)
    
    result = await session.execute(data_stmt)
    items = [dict(row) for row in result.mappings()]
    return {"total": total_count, "list": items}

async def get_library_anime_by_id(session: AsyncSession, anime_id: int) -> Optional[Dict[str, Any]]:
    """
    Gets a single anime from the library by its ID, with counts.
    """
    stmt = (
        select(
            Anime.id.label("animeId"),
            Anime.localImagePath.label("localImagePath"),
            Anime.imageUrl.label("imageUrl"),
            Anime.title,
            Anime.type,
            Anime.season,
            Anime.year,
            Anime.createdAt.label("createdAt"),
            case(
                (Anime.type == 'movie', 1),
                else_=func.coalesce(func.max(Episode.episodeIndex), 0)
            ).label("episodeCount"),
            func.count(distinct(AnimeSource.id)).label("sourceCount")
        )
        .join(AnimeSource, Anime.id == AnimeSource.animeId, isouter=True)
        .join(Episode, AnimeSource.id == Episode.sourceId, isouter=True)
        .where(Anime.id == anime_id)
        .group_by(Anime.id)
    )
    result = await session.execute(stmt)
    row = result.mappings().one_or_none()
    return dict(row) if row else None

async def get_last_episode_for_source(session: AsyncSession, sourceId: int) -> Optional[Dict[str, Any]]:
    """获取指定源的最后一个分集。"""
    stmt = (
        select(Episode.episodeIndex.label("episodeIndex"))
        .where(Episode.sourceId == sourceId)
        .order_by(Episode.episodeIndex.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def get_episode_for_refresh(session: AsyncSession, episodeId: int) -> Optional[Dict[str, Any]]:
    """获取用于刷新的分集信息。"""
    stmt = (
        select(Episode.id, Episode.title, AnimeSource.providerName)
        .join(AnimeSource, Episode.sourceId == AnimeSource.id)
        .where(Episode.id == episodeId)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def get_or_create_anime(session: AsyncSession, title: str, media_type: str, season: int, image_url: Optional[str], local_image_path: Optional[str], year: Optional[int] = None) -> int:
    """通过标题查找番剧，如果不存在则创建。如果存在但缺少海报，则更新海报。返回其ID。"""
    stmt = select(Anime).where(Anime.title == title, Anime.season == season)
    result = await session.execute(stmt)
    anime = result.scalar_one_or_none()

    if anime:
        update_values = {}
        if not anime.imageUrl and image_url:
            update_values["imageUrl"] = image_url
        if not anime.localImagePath and local_image_path:
            update_values["localImagePath"] = local_image_path
        # 新增：如果已有条目没有年份，则更新
        if not anime.year and year:
            update_values["year"] = year
        if update_values:
            await session.execute(update(Anime).where(Anime.id == anime.id).values(**update_values))
            await session.flush() # 使用 flush 代替 commit，以在事务中保持对象状态
        return anime.id

    # Create new anime
    new_anime = Anime(
        title=title, type=media_type, season=season, 
        imageUrl=image_url, localImagePath=local_image_path, 
        year=year, 
        createdAt=get_now()
    )
    session.add(new_anime)
    await session.flush()  # Flush to get the new anime's ID
    
    # Create associated metadata and alias records
    new_metadata = AnimeMetadata(animeId=new_anime.id)
    new_alias = AnimeAlias(animeId=new_anime.id)
    session.add_all([new_metadata, new_alias])
    
    await session.flush() # 使用 flush 获取新ID，但不提交事务
    return new_anime.id

async def create_anime(session: AsyncSession, anime_data: models.AnimeCreate) -> Anime:
    """
    Manually creates a new anime entry in the database, and automatically
    creates and links a default 'custom' source for it.
    """
    # Check if an anime with the same title and season already exists
    existing_anime = await find_anime_by_title_and_season(session, anime_data.title, anime_data.season)
    if existing_anime:
        raise ValueError(f"作品 '{anime_data.title}' (第 {anime_data.season} 季) 已存在。")

    new_anime = Anime(
        title=anime_data.title,
        type=anime_data.type,
        season=anime_data.season,
        year=anime_data.year,
        imageUrl=anime_data.imageUrl,
        createdAt=get_now().replace(tzinfo=None)
    )
    session.add(new_anime)
    await session.flush()
    
    # Create associated metadata and alias records
    new_metadata = AnimeMetadata(animeId=new_anime.id)
    new_alias = AnimeAlias(animeId=new_anime.id)
    session.add_all([new_metadata, new_alias])
    
    # 修正：在创建新作品时，自动为其创建一个'custom'数据源。
    # 这简化了用户操作，并从根源上确保了数据完整性，
    # 因为 link_source_to_anime 会负责在 scrapers 表中创建对应的条目。
    logger.info(f"为新作品 '{anime_data.title}' 自动创建 'custom' 数据源。")
    custom_media_id = f"custom_{new_anime.id}"
    await link_source_to_anime(session, new_anime.id, "custom", custom_media_id)
    
    await session.flush()
    await session.refresh(new_anime)
    return new_anime

async def update_anime_aliases(session: AsyncSession, anime_id: int, payload: Any):
    """
    Updates the aliases for a given anime.
    The payload can be any object with the alias attributes.
    """
    stmt = select(AnimeAlias).where(AnimeAlias.animeId == anime_id)
    result = await session.execute(stmt)
    alias_record = result.scalar_one_or_none()

    if not alias_record:
        alias_record = AnimeAlias(animeId=anime_id)
        session.add(alias_record)
    
    alias_record.nameEn = getattr(payload, 'nameEn', alias_record.nameEn)
    alias_record.nameJp = getattr(payload, 'nameJp', alias_record.nameJp)
    alias_record.nameRomaji = getattr(payload, 'nameRomaji', alias_record.nameRomaji)
    alias_record.aliasCn1 = getattr(payload, 'aliasCn1', alias_record.aliasCn1)
    alias_record.aliasCn2 = getattr(payload, 'aliasCn2', alias_record.aliasCn2)
    alias_record.aliasCn3 = getattr(payload, 'aliasCn3', alias_record.aliasCn3)
    
    await session.flush()

async def update_anime_details(session: AsyncSession, anime_id: int, update_data: models.AnimeDetailUpdate) -> bool:
    """在事务中更新番剧的核心信息、元数据和别名。"""
    anime = await session.get(Anime, anime_id, options=[selectinload(Anime.metadataRecord), selectinload(Anime.aliases)])
    if not anime:
        return False

    # Update Anime table
    anime.title = update_data.title
    anime.type = update_data.type
    anime.season = update_data.season
    anime.episodeCount = update_data.episodeCount
    anime.year = update_data.year
    anime.imageUrl = update_data.imageUrl

    # Update or create AnimeMetadata
    if not anime.metadataRecord:
        anime.metadataRecord = AnimeMetadata(animeId=anime_id)
    anime.metadataRecord.tmdbId = update_data.tmdbId
    anime.metadataRecord.tmdbEpisodeGroupId = update_data.tmdbEpisodeGroupId
    anime.metadataRecord.bangumiId = update_data.bangumiId
    anime.metadataRecord.tvdbId = update_data.tvdbId
    anime.metadataRecord.doubanId = update_data.doubanId
    anime.metadataRecord.imdbId = update_data.imdbId

    # Update or create AnimeAlias
    if not anime.aliases:
        anime.aliases = AnimeAlias(animeId=anime_id)
    anime.aliases.nameEn = update_data.nameEn
    anime.aliases.nameJp = update_data.nameJp
    anime.aliases.nameRomaji = update_data.nameRomaji
    anime.aliases.aliasCn1 = update_data.aliasCn1
    anime.aliases.aliasCn2 = update_data.aliasCn2
    anime.aliases.aliasCn3 = update_data.aliasCn3

    await session.commit()
    return True

async def delete_anime(session: AsyncSession, anime_id: int) -> bool:
    """删除一个作品及其所有关联数据（通过级联删除）。"""
    import shutil
    anime = await session.get(Anime, anime_id)
    if anime:
        await session.delete(anime)
        await session.commit()
        return True
    return False

async def search_anime(session: AsyncSession, keyword: str) -> List[Dict[str, Any]]:
    """在数据库中搜索番剧 (使用FULLTEXT索引)"""
    sanitized_keyword = re.sub(r'[+\-><()~*@"]', ' ', keyword).strip()
    if not sanitized_keyword:
        return []

    # 修正：使用 LIKE 代替 MATCH...AGAINST 以兼容 PostgreSQL
    # 注意：这会比全文索引慢，但提供了跨数据库的兼容性。
    # 对于高性能需求，可以考虑为每个数据库方言实现特定的全文搜索查询。
    stmt = select(Anime.id, Anime.title, Anime.type).where(
        Anime.title.like(f"%{sanitized_keyword}%")
    ).order_by(func.length(Anime.title)) # 按标题长度排序，较短的匹配更相关

    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def search_episodes_in_library(session: AsyncSession, anime_title: str, episode_number: Optional[int], season_number: Optional[int] = None) -> List[Dict[str, Any]]:
    """在本地库中通过番剧标题和可选的集数搜索匹配的分集。"""
    clean_title = anime_title.strip()
    if not clean_title:
        return []

    # Base query
    stmt = (
        select(
            Anime.id.label("animeId"),
            Anime.title.label("animeTitle"),
            Anime.type,
            Anime.imageUrl.label("imageUrl"),
            Anime.createdAt.label("startDate"),
            Episode.id.label("episodeId"),
            case((Anime.type == 'movie', func.concat(Scraper.providerName, ' 源')), else_=Episode.title).label("episodeTitle"),
            AnimeAlias.nameEn,
            AnimeAlias.nameJp,
            AnimeAlias.nameRomaji,
            AnimeAlias.aliasCn1,
            AnimeAlias.aliasCn2,
            AnimeAlias.aliasCn3,
            Scraper.displayOrder,
            AnimeSource.isFavorited.label("isFavorited"),
            AnimeMetadata.bangumiId.label("bangumiId")
        )
        .join(AnimeSource, Anime.id == AnimeSource.animeId)
        .join(Episode, AnimeSource.id == Episode.sourceId)
        .join(Scraper, AnimeSource.providerName == Scraper.providerName)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .join(AnimeAlias, Anime.id == AnimeAlias.animeId, isouter=True)
    )

    # Add conditions
    if episode_number is not None:
        stmt = stmt.where(Episode.episodeIndex == episode_number)
    if season_number is not None:
        stmt = stmt.where(Anime.season == season_number)

    # Title condition
    normalized_like_title = f"%{clean_title.replace('：', ':').replace(' ', '')}%"
    like_conditions = [
        func.replace(func.replace(col, '：', ':'), ' ', '').like(normalized_like_title)
        for col in [Anime.title, AnimeAlias.nameEn, AnimeAlias.nameJp, AnimeAlias.nameRomaji,
                    AnimeAlias.aliasCn1, AnimeAlias.aliasCn2, AnimeAlias.aliasCn3]
    ]
    stmt = stmt.where(or_(*like_conditions))

    # Order and execute
    stmt = stmt.order_by(func.length(Anime.title), Scraper.displayOrder)
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def find_anime_by_title_and_season(session: AsyncSession, title: str, season: int) -> Optional[Dict[str, Any]]:
    """
    通过标题和季度查找番剧，返回一个简化的字典或None。
    """
    stmt = (
        select(
            Anime.id,
            Anime.title,
            Anime.season
        )
        .where(Anime.title == title, Anime.season == season)
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def find_anime_by_metadata_id_and_season(
    session: AsyncSession, 
    id_type: str,
    media_id: str, 
    season: int
) -> Optional[Dict[str, Any]]:
    """
    通过元数据ID和季度号精确查找一个作品。
    """
    id_column = getattr(AnimeMetadata, id_type, None)
    if id_column is None:
        raise ValueError(f"无效的元数据ID类型: {id_type}")

    stmt = (
        select(Anime.id, Anime.title, Anime.season)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId)
        .where(id_column == media_id, Anime.season == season)
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def get_episode_indices_by_anime_title(session: AsyncSession, title: str, season: Optional[int] = None) -> List[int]:
    """根据作品标题和可选的季度号获取已存在的所有分集序号列表。"""
    stmt = (
        select(distinct(Episode.episodeIndex))
        .join(AnimeSource, Episode.sourceId == AnimeSource.id)
        .join(Anime, AnimeSource.animeId == Anime.id)
        .where(Anime.title == title)
    )

    # 如果提供了季度号，则增加过滤条件
    if season is not None:
        stmt = stmt.where(Anime.season == season)

    stmt = stmt.order_by(Episode.episodeIndex)
    
    result = await session.execute(stmt)
    return result.scalars().all()

async def find_favorited_source_for_anime(session: AsyncSession, anime_id: int) -> Optional[Dict[str, Any]]:
    """通过 anime_id 查找已存在于库中且被标记为“精确”的数据源。"""
    stmt = (
        select(
            AnimeSource.providerName.label("providerName"),
            AnimeSource.mediaId.label("mediaId"),
            Anime.id.label("animeId"),
            Anime.title.label("animeTitle"), # 保留标题以用于任务创建
            Anime.type.label("mediaType"),
            Anime.imageUrl.label("imageUrl"),
            Anime.year.label("year") # 新增年份以保持数据完整性
        )
        .join(Anime, AnimeSource.animeId == Anime.id)
        .where(AnimeSource.animeId == anime_id, AnimeSource.isFavorited == True)
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def search_animes_for_dandan(session: AsyncSession, keyword: str) -> List[Dict[str, Any]]:
    """在本地库中通过番剧标题搜索匹配的番剧，用于 /search/anime 接口。"""
    clean_title = keyword.strip()
    if not clean_title:
        return []

    stmt = (
        select(
            Anime.id.label("animeId"),
            Anime.title.label("animeTitle"),
            Anime.type,
            Anime.imageUrl.label("imageUrl"),
            Anime.createdAt.label("startDate"),
            Anime.year,
            func.count(distinct(Episode.id)).label("episodeCount"),
            AnimeMetadata.bangumiId.label("bangumiId")
        )
        .join(AnimeSource, Anime.id == AnimeSource.animeId, isouter=True)
        .join(Episode, AnimeSource.id == Episode.sourceId, isouter=True)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .join(AnimeAlias, Anime.id == AnimeAlias.animeId, isouter=True)
        .group_by(Anime.id, AnimeMetadata.bangumiId)
        .order_by(Anime.id)
    )

    normalized_like_title = f"%{clean_title.replace('：', ':').replace(' ', '')}%"
    like_conditions = [
        func.replace(func.replace(col, '：', ':'), ' ', '').like(normalized_like_title)
        for col in [Anime.title, AnimeAlias.nameEn, AnimeAlias.nameJp, AnimeAlias.nameRomaji,
                    AnimeAlias.aliasCn1, AnimeAlias.aliasCn2, AnimeAlias.aliasCn3]
    ]
    stmt = stmt.where(or_(*like_conditions))
    
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def find_animes_for_matching(session: AsyncSession, title: str) -> List[Dict[str, Any]]:
    """为匹配流程查找可能的番剧，并返回其核心ID以供TMDB映射使用。"""
    title_len_expr = func.length(Anime.title)
    stmt = (
        select(
            Anime.id.label("animeId"),
            AnimeMetadata.tmdbId,
            AnimeMetadata.tmdbEpisodeGroupId,
            Anime.title,
            # 修正：将用于排序的列添加到 SELECT 列表中，以兼容 PostgreSQL 的 DISTINCT 规则
            title_len_expr.label("title_length")
        )
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .join(AnimeAlias, Anime.id == AnimeAlias.animeId, isouter=True)
    )
    
    normalized_like_title = f"%{title.replace('：', ':').replace(' ', '')}%"
    like_conditions = [
        func.replace(func.replace(col, '：', ':'), ' ', '').like(normalized_like_title)
        for col in [Anime.title, AnimeAlias.nameEn, AnimeAlias.nameJp, AnimeAlias.nameRomaji,
                    AnimeAlias.aliasCn1, AnimeAlias.aliasCn2, AnimeAlias.aliasCn3]
    ]
    stmt = stmt.where(or_(*like_conditions)).distinct().order_by(title_len_expr).limit(5)
    
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def find_episode_via_tmdb_mapping(
    session: AsyncSession,
    tmdb_id: str,
    group_id: str,
    custom_season: Optional[int],
    custom_episode: Optional[int]
) -> List[Dict[str, Any]]:
    """
    通过TMDB映射关系查找本地数据库中的分集。
    此实现使用自连接(self-join)来查找与文件名S/E对应的库内S/E。
    """
    # 为 tmdb_episode_mapping 表的自连接创建别名
    MappingFromFile = aliased(TmdbEpisodeMapping)
    MappingToLibrary = aliased(TmdbEpisodeMapping)

    stmt = (
        select(
            Anime.id.label("animeId"), Anime.title.label("animeTitle"), Anime.type, Anime.imageUrl.label("imageUrl"), Anime.createdAt.label("startDate"),
            Episode.id.label("episodeId"), Episode.title.label("episodeTitle"), Scraper.displayOrder, AnimeSource.isFavorited.label("isFavorited"),
            AnimeMetadata.bangumiId.label("bangumiId")
        )
        .select_from(MappingFromFile)
        .join(
            MappingToLibrary,
            and_(
                MappingFromFile.absoluteEpisodeNumber == MappingToLibrary.absoluteEpisodeNumber,
                MappingFromFile.tmdbTvId == MappingToLibrary.tmdbTvId,
                MappingFromFile.tmdbEpisodeGroupId == MappingToLibrary.tmdbEpisodeGroupId
            )
        )
        .join(AnimeMetadata, AnimeMetadata.tmdbId == MappingToLibrary.tmdbTvId)
        .join(Anime, and_(
            Anime.id == AnimeMetadata.animeId,
            Anime.season == MappingToLibrary.customSeasonNumber
        ))
        .join(AnimeSource, Anime.id == AnimeSource.animeId)
        .join(Episode, and_(
            Episode.sourceId == AnimeSource.id,
            Episode.episodeIndex == MappingToLibrary.customEpisodeNumber
        ))
        .join(Scraper, AnimeSource.providerName == Scraper.providerName)
        .where(MappingFromFile.tmdbTvId == tmdb_id, MappingFromFile.tmdbEpisodeGroupId == group_id)
    )

    if custom_season is not None and custom_episode is not None:
        # 增强：同时匹配自定义编号和TMDB官方编号
        stmt = stmt.where(
            or_(
                and_(
                    MappingFromFile.customSeasonNumber == custom_season,
                    MappingFromFile.customEpisodeNumber == custom_episode
                ),
                and_(
                    MappingFromFile.tmdbSeasonNumber == custom_season,
                    MappingFromFile.tmdbEpisodeNumber == custom_episode
                )
            )
        )
    elif custom_episode is not None:
        # 增强：当只有集数时，也同时匹配绝对集数和两种S01EXX的情况
        stmt = stmt.where(
            or_(
                MappingFromFile.absoluteEpisodeNumber == custom_episode,
                and_(MappingFromFile.customSeasonNumber == 1, MappingFromFile.customEpisodeNumber == custom_episode),
                and_(MappingFromFile.tmdbSeasonNumber == 1, MappingFromFile.tmdbEpisodeNumber == custom_episode)
            )
        )
    
    stmt = stmt.order_by(AnimeSource.isFavorited.desc(), Scraper.displayOrder)
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]


async def get_related_episode_ids(session: AsyncSession, anime_id: int, episode_index: int) -> List[int]:
    """
    根据 anime_id 和 episode_index 找到所有关联源的 episode ID。
    """
    stmt = (
        select(Episode.id)
        .join(AnimeSource, Episode.sourceId == AnimeSource.id)
        .where(
            AnimeSource.animeId == anime_id,
            Episode.episodeIndex == episode_index
        )
    )
    result = await session.execute(stmt)
    return result.scalars().all()

async def get_anime_details_for_dandan(session: AsyncSession, anime_id: int) -> Optional[Dict[str, Any]]:
    """获取番剧的详细信息及其所有分集，用于dandanplay API。"""
    anime_stmt = (
        select(
            Anime.id.label("animeId"), Anime.title.label("animeTitle"), Anime.type, Anime.imageUrl.label("imageUrl"),
            Anime.createdAt.label("startDate"), Anime.year,
            func.count(distinct(Episode.id)).label("episodeCount"), AnimeMetadata.bangumiId.label("bangumiId")
        )
        .join(AnimeSource, Anime.id == AnimeSource.animeId, isouter=True)
        .join(Episode, AnimeSource.id == Episode.sourceId, isouter=True)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .where(Anime.id == anime_id)
        .group_by(Anime.id, AnimeMetadata.bangumiId)
    )
    anime_details_res = await session.execute(anime_stmt)
    anime_details = anime_details_res.mappings().first()
    if not anime_details:
        return None

    episodes = []
    if anime_details['type'] == 'movie':
        ep_stmt = (
            select(Episode.id.label("episodeId"), func.concat(AnimeSource.providerName, ' 源').label("episodeTitle"), Scraper.displayOrder.label("episodeNumber"))
            .join(AnimeSource, Episode.sourceId == AnimeSource.id)
            .join(Scraper, AnimeSource.providerName == Scraper.providerName)
            .where(AnimeSource.animeId == anime_id)
            .order_by(Scraper.displayOrder)
        )
    else:
        ep_stmt = (
            select(Episode.id.label("episodeId"), Episode.title.label("episodeTitle"), Episode.episodeIndex.label("episodeNumber"))
            .join(AnimeSource, Episode.sourceId == AnimeSource.id)
            .where(AnimeSource.animeId == anime_id)
            .order_by(Episode.episodeIndex)
        )
    
    episodes_res = await session.execute(ep_stmt)
    episodes = [dict(row) for row in episodes_res.mappings()]
    
    return {"anime": dict(anime_details), "episodes": episodes}

async def get_anime_id_by_bangumi_id(session: AsyncSession, bangumi_id: str) -> Optional[int]:
    """通过 bangumi_id 查找 anime_id。"""
    stmt = select(AnimeMetadata.animeId).where(AnimeMetadata.bangumiId == bangumi_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def get_anime_id_by_tmdb_id(session: AsyncSession, tmdb_id: str) -> Optional[int]:
    """通过 tmdb_id 查找 anime_id。"""
    stmt = select(AnimeMetadata.animeId).where(AnimeMetadata.tmdbId == tmdb_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def get_anime_id_by_tvdb_id(session: AsyncSession, tvdb_id: str) -> Optional[int]:
    """通过 tvdb_id 查找 anime_id。"""
    stmt = select(AnimeMetadata.animeId).where(AnimeMetadata.tvdbId == tvdb_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def get_anime_id_by_imdb_id(session: AsyncSession, imdb_id: str) -> Optional[int]:
    """通过 imdb_id 查找 anime_id。"""
    stmt = select(AnimeMetadata.animeId).where(AnimeMetadata.imdbId == imdb_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def get_anime_id_by_douban_id(session: AsyncSession, douban_id: str) -> Optional[int]:
    """通过 douban_id 查找 anime_id。"""
    stmt = select(AnimeMetadata.animeId).where(AnimeMetadata.doubanId == douban_id)
    result = await session.execute(stmt)
    return result.scalars().first()

async def check_source_exists_by_media_id(session: AsyncSession, provider_name: str, media_id: str) -> bool:
    """检查具有给定提供商和媒体ID的源是否已存在。"""
    stmt = select(AnimeSource.id).where(
        AnimeSource.providerName == provider_name,
        AnimeSource.mediaId == media_id
    ).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

async def link_source_to_anime(session: AsyncSession, anime_id: int, provider_name: str, media_id: str) -> int:
    """将一个外部数据源关联到一个番剧条目，如果关联已存在则直接返回其ID。"""
    # 修正：在链接源之前，确保该提供商在 scrapers 表中存在。
    # 这修复了当创建 'custom' 等非文件型源时，因 scrapers 表中缺少对应条目而导致后续查询失败的问题。
    # 这是比 LEFT JOIN 更根本的解决方案。
    scraper_entry = await session.get(Scraper, provider_name)
    if not scraper_entry:
        logger.info(f"提供商 '{provider_name}' 在 scrapers 表中不存在，将为其创建新条目。")
        max_order_stmt = select(func.max(Scraper.displayOrder))
        max_order = (await session.execute(max_order_stmt)).scalar_one_or_none() or 0
        new_scraper_entry = Scraper(
            providerName=provider_name,
            displayOrder=max_order + 1,
            isEnabled=True, # 自定义源默认启用
            useProxy=False
        )
        session.add(new_scraper_entry)
        await session.flush() # Flush to make it available within the transaction
    stmt = select(AnimeSource.id).where(
        AnimeSource.animeId == anime_id,
        AnimeSource.providerName == provider_name,
        AnimeSource.mediaId == media_id
    )
    result = await session.execute(stmt)
    existing_id = result.scalar_one_or_none()
    if existing_id:
        return existing_id

    # 如果源不存在，则创建一个新的，并为其分配一个持久的、唯一的顺序号
    # 查找此作品当前最大的 sourceOrder
    max_order_stmt = select(func.max(AnimeSource.sourceOrder)).where(AnimeSource.animeId == anime_id)
    max_order_result = await session.execute(max_order_stmt)
    current_max_order = max_order_result.scalar_one_or_none() or 0

    new_source = AnimeSource(
        animeId=anime_id,
        providerName=provider_name,
        mediaId=media_id,
        sourceOrder=current_max_order + 1,
        createdAt=get_now()
    )
    session.add(new_source)
    await session.flush() # 使用 flush 获取新ID，但不提交事务
    return new_source.id

async def update_source_media_id(session: AsyncSession, source_id: int, new_media_id: str):
    """更新指定源的 mediaId。"""
    stmt = update(AnimeSource).where(AnimeSource.id == source_id).values(mediaId=new_media_id)
    await session.execute(stmt)
    # 注意：这里不 commit，由调用方（任务）来决定何时提交事务

async def update_metadata_if_empty(
    session: AsyncSession,
    anime_id: int,
    *,
    tmdb_id: Optional[str] = None,
    imdb_id: Optional[str] = None,
    tvdb_id: Optional[str] = None,
    douban_id: Optional[str] = None,
    bangumi_id: Optional[str] = None,
    tmdb_episode_group_id: Optional[str] = None
):
    """
    如果 anime_metadata 记录中的字段为空，则使用提供的值进行更新。
    如果记录不存在，则创建一个新记录。
    使用关键字参数以提高可读性和安全性。
    """
    stmt = select(AnimeMetadata).where(AnimeMetadata.animeId == anime_id)
    result = await session.execute(stmt)
    metadata_record = result.scalar_one_or_none()

    if not metadata_record:
        metadata_record = AnimeMetadata(animeId=anime_id)
        session.add(metadata_record)
        await session.flush()

    if tmdb_id and not metadata_record.tmdbId: metadata_record.tmdbId = tmdb_id
    if imdb_id and not metadata_record.imdbId: metadata_record.imdbId = imdb_id
    if tvdb_id and not metadata_record.tvdbId: metadata_record.tvdbId = tvdb_id
    if douban_id and not metadata_record.doubanId: metadata_record.doubanId = douban_id
    if bangumi_id and not metadata_record.bangumiId: metadata_record.bangumiId = bangumi_id
    if tmdb_episode_group_id and not metadata_record.tmdbEpisodeGroupId: metadata_record.tmdbEpisodeGroupId = tmdb_episode_group_id

    await session.flush()

# --- User & Auth ---

async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[Dict[str, Any]]:
    """通过ID查找用户"""
    stmt = select(User.id, User.username).where(User.id == user_id)
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def get_user_full_by_id(session: AsyncSession, user_id: int) -> Optional[Dict[str, Any]]:
    stmt = select(User).where(User.id == user_id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        return None
    return {
        'id': user.id,
        'username': user.username,
        'role': getattr(user, 'role', 'user'),
        'canCreateAdmin': getattr(user, 'canCreateAdmin', False),
        'perHourLimit': getattr(user, 'perHourLimit', None),
        'permissionsJson': getattr(user, 'permissionsJson', None),
    }

async def get_first_admin_user_id(session: AsyncSession) -> Optional[int]:
    """返回第一个管理员用户的ID，若不存在则返回 None。"""
    res = await session.execute(select(User.id).where(User.role == 'admin').order_by(User.id.asc()).limit(1))
    row = res.scalar_one_or_none()
    return row

async def get_user_by_username(session: AsyncSession, username: str) -> Optional[Dict[str, Any]]:
    """通过用户名查找用户"""
    stmt = select(User).where(User.username == username)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user:
        return {
            "id": user.id,
            "username": user.username,
            "hashedPassword": user.hashedPassword,
            "token": user.token,
            "role": getattr(user, 'role', 'user'),
            "canCreateAdmin": getattr(user, 'canCreateAdmin', False),
            "perHourLimit": getattr(user, 'perHourLimit', None),
            "permissionsJson": getattr(user, 'permissionsJson', None),
        }
    return None

async def create_user(session: AsyncSession, user: models.UserCreate):
    """创建新用户"""
    from . import security
    hashed_password = security.get_password_hash(user.password)
    new_user = User(username=user.username, hashedPassword=hashed_password, createdAt=get_now())
    session.add(new_user)
    await session.commit()

async def update_user_password(session: AsyncSession, username: str, new_hashed_password: str):
    """更新用户的密码"""
    stmt = update(User).where(User.username == username).values(hashedPassword=new_hashed_password)
    await session.execute(stmt)
    await session.commit()

async def update_user_login_info(session: AsyncSession, username: str, token: str):
    """更新用户的最后登录时间和当前令牌"""
    stmt = update(User).where(User.username == username).values(token=token, tokenUpdate=get_now())
    await session.execute(stmt)
    await session.commit()

# --- Episode & Comment ---

async def find_episode(session: AsyncSession, source_id: int, episode_index: int) -> Optional[Dict[str, Any]]:
    """查找特定源的特定分集"""
    stmt = select(Episode.id, Episode.title).where(Episode.sourceId == source_id, Episode.episodeIndex == episode_index)
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def check_episode_exists(session: AsyncSession, episode_id: int) -> bool:
    """检查指定ID的分集是否存在"""
    stmt = select(Episode.id).where(Episode.id == episode_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

async def fetch_comments(session: AsyncSession, episode_id: int) -> List[Dict[str, Any]]:
    """从XML文件获取弹幕。"""
    episode = await session.get(Episode, episode_id)
    if not episode or not episode.danmakuFilePath:
        return []
    
    try:
        absolute_path = _get_fs_path_from_web_path(episode.danmakuFilePath)
        if not absolute_path:
            return [] # 辅助函数会记录警告
        
        if not absolute_path.exists():
            logger.warning(f"数据库记录了弹幕文件路径，但文件不存在: {absolute_path}")
            return []
            
        xml_content = absolute_path.read_text(encoding='utf-8')
        return parse_dandan_xml_to_comments(xml_content)
    except Exception as e:
        logger.error(f"读取或解析弹幕文件失败: {episode.danmakuFilePath}。错误: {e}", exc_info=True)
        return []

async def create_episode_if_not_exists(session: AsyncSession, anime_id: int, source_id: int, episode_index: int, title: str, url: Optional[str], provider_episode_id: str) -> int:
    """如果分集不存在则创建，并返回其确定性的ID。"""
    # 1. 从数据库获取该源的持久化 sourceOrder
    source_order_stmt = select(AnimeSource.sourceOrder).where(AnimeSource.id == source_id)
    source_order_res = await session.execute(source_order_stmt)
    source_order = source_order_res.scalar_one_or_none()

    if source_order is None:
        # 这是一个重要的回退和迁移逻辑。如果一个旧的源没有 sourceOrder，
        # 我们就为其分配一个新的、持久的序号。
        logger.warning(f"源 ID {source_id} 缺少 sourceOrder，将为其分配一个新的。这通常发生在从旧版本升级后。")
        source_order = await _assign_source_order_if_missing(session, anime_id, source_id)

    new_episode_id_str = f"25{anime_id:06d}{source_order:02d}{episode_index:04d}"
    new_episode_id = int(new_episode_id_str)

    # 2. 直接检查这个ID是否存在
    existing_episode = await session.get(Episode, new_episode_id)
    if existing_episode:
        return existing_episode.id

    # 3. 如果ID不存在，则创建新分集
    new_episode = Episode(
        id=new_episode_id, sourceId=source_id, episodeIndex=episode_index, providerEpisodeId=provider_episode_id,
        title=title, sourceUrl=url, fetchedAt=get_now() # fetchedAt is explicitly set here
    )
    session.add(new_episode)
    await session.flush()
    return new_episode_id

async def _assign_source_order_if_missing(session: AsyncSession, anime_id: int, source_id: int) -> int:
    """一个辅助函数，用于为没有 sourceOrder 的旧记录分配一个新的、持久的序号。"""
    async with session.begin_nested(): # 使用嵌套事务确保操作的原子性
        max_order_stmt = select(func.max(AnimeSource.sourceOrder)).where(AnimeSource.animeId == anime_id)
        max_order_res = await session.execute(max_order_stmt)
        current_max_order = max_order_res.scalar_one_or_none() or 0
        new_order = current_max_order + 1
        
        await session.execute(update(AnimeSource).where(AnimeSource.id == source_id).values(sourceOrder=new_order))
        return new_order

async def save_danmaku_for_episode(
    session: AsyncSession,
    episode_id: int,
    comments: List[Dict[str, Any]]
) -> int:
    """将弹幕写入XML文件，并更新数据库记录，返回新增数量。"""
    if not comments:
        return 0

    episode = await session.get(
        Episode, 
        episode_id, 
        options=[selectinload(Episode.source).selectinload(AnimeSource.anime)]
    )
    if not episode:
        raise ValueError(f"找不到ID为 {episode_id} 的分集")

    anime_id = episode.source.anime.id
    source_id = episode.source.id

    # 新增：获取原始弹幕服务器信息
    provider_name = episode.source.providerName
    # 这是一个简化的映射，您可以根据需要扩展
    chat_server_map = {
        "bilibili": "comment.bilibili.com"
    }
    xml_content = _generate_xml_from_comments(comments, episode_id, provider_name, chat_server_map.get(provider_name, "danmaku.misaka.org"))
    
    # 修正：统一文件路径结构，与 tasks.py 保持一致（不包含 source_id）
    web_path = f"/danmaku/{anime_id}/{episode_id}.xml"
    absolute_path = DANMAKU_BASE_DIR / str(anime_id) / f"{episode_id}.xml"
    
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_text(xml_content, encoding='utf-8')
        logger.info(f"弹幕已成功写入文件: {absolute_path}")
    except OSError as e:
        logger.error(f"写入弹幕文件失败: {absolute_path}。错误: {e}")
        raise
    
    await update_episode_danmaku_info(session, episode_id, web_path, len(comments))
    return len(comments)

# ... (rest of the file needs to be refactored similarly) ...

# This is a placeholder for the rest of the refactored functions.
# The full implementation would involve converting every function in the original crud.py.
# For brevity, I'll stop here, but the pattern is consistent.

async def get_anime_source_info(session: AsyncSession, source_id: int) -> Optional[Dict[str, Any]]:
    stmt = (
        select(
            AnimeSource.id.label("sourceId"), AnimeSource.animeId.label("animeId"), AnimeSource.providerName.label("providerName"),
            AnimeSource.mediaId.label("mediaId"), AnimeSource.sourceOrder.label("sourceOrder"), Anime.year,
            Anime.title, Anime.type, Anime.season, AnimeMetadata.tmdbId.label("tmdbId"), AnimeMetadata.bangumiId.label("bangumiId")
        )
        .join(Anime, AnimeSource.animeId == Anime.id)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .where(AnimeSource.id == source_id)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def get_anime_sources(session: AsyncSession, anime_id: int) -> List[Dict[str, Any]]:
    """获取指定作品的所有数据源，并高效地计算每个源的分集数。"""
    # 步骤1: 创建一个子查询，用于高效地计算每个 source_id 对应的分集数量。
    # 这种方式比在主查询中直接 JOIN 和 COUNT 更快，尤其是在 episode 表很大的情况下。
    episode_count_subquery = (
        select(
            Episode.sourceId,
            func.count(Episode.id).label("episode_count")
        )
        .group_by(Episode.sourceId)
        .subquery()
    )

    # 步骤2: 构建主查询，LEFT JOIN 上面的子查询来获取分集数。
    stmt = (
        select(
            AnimeSource.id.label("sourceId"),
            AnimeSource.providerName.label("providerName"),
            AnimeSource.mediaId.label("mediaId"),
            AnimeSource.isFavorited.label("isFavorited"),
            AnimeSource.incrementalRefreshEnabled.label("incrementalRefreshEnabled"),
            AnimeSource.createdAt.label("createdAt"),
            # 使用 coalesce 确保即使没有分集的源也返回 0 而不是 NULL
            func.coalesce(episode_count_subquery.c.episode_count, 0).label("episodeCount")
        )
        .outerjoin(episode_count_subquery, AnimeSource.id == episode_count_subquery.c.sourceId)
        .where(AnimeSource.animeId == anime_id)
        .order_by(AnimeSource.createdAt)
    )
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def get_episodes_for_source(session: AsyncSession, source_id: int, page: int = 1, page_size: int = 5000) -> Dict[str, Any]:
    """获取指定源的分集列表，支持分页。"""
    # 首先，获取总的分集数量，用于前端分页控件
    count_stmt = select(func.count(Episode.id)).where(Episode.sourceId == source_id)
    total_count = (await session.execute(count_stmt)).scalar_one()

    # 然后，根据分页参数查询特定页的数据
    # 修正：确保返回一个包含完整信息的字典列表，以修复UI中的TypeError
    offset = (page - 1) * page_size
    stmt = (
        select(
            Episode.id.label("episodeId"),
            Episode.title,
            Episode.episodeIndex.label("episodeIndex"),
            Episode.sourceUrl.label("sourceUrl"),
            Episode.fetchedAt.label("fetchedAt"),
            Episode.commentCount.label("commentCount")
        )
        .where(Episode.sourceId == source_id)
        .order_by(Episode.episodeIndex).offset(offset).limit(page_size)
    )
    result = await session.execute(stmt)
    episodes = [dict(row) for row in result.mappings()]
    
    return {"total": total_count, "episodes": episodes}
async def get_episode_provider_info(session: AsyncSession, episode_id: int) -> Optional[Dict[str, Any]]:
    stmt = (
        select(
            AnimeSource.providerName,
            AnimeSource.animeId,
            Episode.providerEpisodeId,
            Episode.danmakuFilePath
        )
        .join(AnimeSource, Episode.sourceId == AnimeSource.id)
        .where(Episode.id == episode_id)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def clear_source_data(session: AsyncSession, source_id: int):
    """Deletes all episodes and their danmaku files for a given source."""
    source = await session.get(AnimeSource, source_id)
    if not source:
        return
    
    # 修正：逐个删除文件，而不是删除一个不存在的目录，以提高健壮性
    episodes_to_delete_res = await session.execute(
        select(Episode.danmakuFilePath).where(Episode.sourceId == source_id)
    )
    for file_path_str in episodes_to_delete_res.scalars().all():
        if fs_path := _get_fs_path_from_web_path(file_path_str):
            if fs_path.is_file():
                fs_path.unlink(missing_ok=True)

    await session.execute(delete(Episode).where(Episode.sourceId == source_id))
    await session.commit()

async def clear_episode_comments(session: AsyncSession, episode_id: int):
    """Deletes the danmaku file for an episode and resets its count."""
    episode = await session.get(Episode, episode_id)
    if not episode:
        return
    
    if episode.danmakuFilePath:
        fs_path = _get_fs_path_from_web_path(episode.danmakuFilePath)
        if fs_path and fs_path.is_file():
            try:
                fs_path.unlink()
            except OSError as e:
                logger.error(f"删除弹幕文件失败: {fs_path}。错误: {e}")
    
    episode.danmakuFilePath = None
    episode.commentCount = 0
    await session.commit()

async def get_anime_full_details(session: AsyncSession, anime_id: int) -> Optional[Dict[str, Any]]:
    stmt = (
        select(
            Anime.id.label("animeId"), Anime.title, Anime.type, Anime.season, Anime.year, Anime.localImagePath.label("localImagePath"),
            Anime.episodeCount.label("episodeCount"), Anime.imageUrl.label("imageUrl"), AnimeMetadata.tmdbId.label("tmdbId"), AnimeMetadata.tmdbEpisodeGroupId.label("tmdbEpisodeGroupId"),
            AnimeMetadata.bangumiId.label("bangumiId"), AnimeMetadata.tvdbId.label("tvdbId"), AnimeMetadata.doubanId.label("doubanId"), AnimeMetadata.imdbId.label("imdbId"),
            AnimeAlias.nameEn.label("nameEn"), AnimeAlias.nameJp.label("nameJp"), AnimeAlias.nameRomaji.label("nameRomaji"), AnimeAlias.aliasCn1.label("aliasCn1"),
            AnimeAlias.aliasCn2.label("aliasCn2"), AnimeAlias.aliasCn3.label("aliasCn3")
        )
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId, isouter=True)
        .join(AnimeAlias, Anime.id == AnimeAlias.animeId, isouter=True)
        .where(Anime.id == anime_id)
    )
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def save_tmdb_episode_group_mappings(session: AsyncSession, tmdb_tv_id: int, group_id: str, group_details: models.TMDBEpisodeGroupDetails):
    await session.execute(delete(TmdbEpisodeMapping).where(TmdbEpisodeMapping.tmdbEpisodeGroupId == group_id))
    
    mappings_to_insert = []
    sorted_groups = sorted(group_details.groups, key=lambda g: g.order)
    for custom_season_group in sorted_groups:
        if not custom_season_group.episodes: continue
        for custom_episode_index, episode in enumerate(custom_season_group.episodes):
            mappings_to_insert.append(
                TmdbEpisodeMapping(
                    tmdbTvId=tmdb_tv_id, tmdbEpisodeGroupId=group_id, tmdbEpisodeId=episode.id,
                    tmdbSeasonNumber=episode.seasonNumber, tmdbEpisodeNumber=episode.episodeNumber,
                    customSeasonNumber=custom_season_group.order, customEpisodeNumber=custom_episode_index + 1,
                    absoluteEpisodeNumber=episode.order + 1
                )
            )
    if mappings_to_insert:
        session.add_all(mappings_to_insert)
    await session.commit()
    logging.info(f"成功为剧集组 {group_id} 保存了 {len(mappings_to_insert)} 条分集映射。")

async def delete_anime_source(session: AsyncSession, source_id: int) -> bool:
    source = await session.get(AnimeSource, source_id)
    if source:
        # 修正：逐个删除文件，而不是删除整个目录，以提高健壮性并与 tasks.py 保持一致
        episodes_to_delete_res = await session.execute(
            select(Episode.danmakuFilePath).where(Episode.sourceId == source_id)
        )
        for file_path_str in episodes_to_delete_res.scalars().all():
            if fs_path := _get_fs_path_from_web_path(file_path_str):
                if fs_path.is_file():
                    fs_path.unlink(missing_ok=True)

        await session.delete(source)
        await session.commit()
        return True
    return False

async def delete_episode(session: AsyncSession, episode_id: int) -> bool:
    """删除一个分集及其弹幕文件。"""
    episode = await session.get(Episode, episode_id)
    if episode:
        if episode.danmakuFilePath:
            fs_path = _get_fs_path_from_web_path(episode.danmakuFilePath)
            if fs_path and fs_path.is_file():
                fs_path.unlink(missing_ok=True)
        await session.delete(episode)
        await session.commit()
        return True
    return False

async def reassociate_anime_sources(session: AsyncSession, source_anime_id: int, target_anime_id: int) -> bool:
    """
    将一个番剧的所有源智能地合并到另一个番剧，然后删除原始番剧。
    - 如果目标番剧已存在相同提供商的源，则合并其下的分集，而不是直接删除。
    - 移动不冲突的源，并同时移动其下的弹幕文件。
    - 在合并后重新为目标番剧的所有源编号，以确保顺序正确。
    """
    if source_anime_id == target_anime_id:
        return False  # 不能将一个作品与它自己合并

    # 1. 高效地预加载所有需要的数据，包括目标作品的分集
    source_anime_stmt = select(Anime).where(Anime.id == source_anime_id).options(
        selectinload(Anime.sources).selectinload(AnimeSource.episodes)
    )
    target_anime_stmt = select(Anime).where(Anime.id == target_anime_id).options(
        selectinload(Anime.sources).selectinload(AnimeSource.episodes)
    )
    source_anime = (await session.execute(source_anime_stmt)).scalar_one_or_none()
    target_anime = (await session.execute(target_anime_stmt)).scalar_one_or_none()

    if not source_anime or not target_anime:
        logger.error(f"重新关联失败：源番剧(ID: {source_anime_id})或目标番剧(ID: {target_anime_id})未找到。")
        return False

    # 2. 识别目标番剧已有的提供商及其源对象，用于冲突检测和分集合并
    target_sources_map = {s.providerName: s for s in target_anime.sources}
    logger.info(f"目标番剧 (ID: {target_anime_id}) 已有源: {list(target_sources_map.keys())}")

    # 3. 遍历源番剧的源，处理冲突或移动
    for source_to_process in list(source_anime.sources):  # 使用副本进行迭代
        provider = source_to_process.providerName
        if provider in target_sources_map:
            # 冲突：合并分集
            target_source = target_sources_map[provider]
            logger.warning(f"发现冲突源: 提供商 '{provider}'。将尝试合并分集到目标源 {target_source.id}。")
            
            target_episode_indices = {ep.episodeIndex for ep in target_source.episodes}

            for episode_to_move in list(source_to_process.episodes):
                if episode_to_move.episodeIndex not in target_episode_indices:
                    # 移动不重复的分集
                    logger.info(f"正在移动分集 {episode_to_move.episodeIndex} (ID: {episode_to_move.id}) 到目标源 {target_source.id}")
                    
                    # 移动弹幕文件
                    if episode_to_move.danmakuFilePath:
                        old_path = _get_fs_path_from_web_path(episode_to_move.danmakuFilePath)
                        new_web_path = f"/danmaku/{target_anime_id}/{episode_to_move.id}.xml"
                        new_fs_path = _get_fs_path_from_web_path(new_web_path)
                        if old_path and old_path.exists() and new_fs_path:
                            new_fs_path.parent.mkdir(parents=True, exist_ok=True)
                            old_path.rename(new_fs_path)
                            episode_to_move.danmakuFilePath = new_web_path
                    
                    episode_to_move.sourceId = target_source.id
                    target_source.episodes.append(episode_to_move)
                else:
                    # 删除重复的分集
                    logger.info(f"分集 {episode_to_move.episodeIndex} 在目标源中已存在，将删除源分集 {episode_to_move.id}")
                    if episode_to_move.danmakuFilePath:
                        fs_path = _get_fs_path_from_web_path(episode_to_move.danmakuFilePath)
                        if fs_path and fs_path.is_file():
                            fs_path.unlink(missing_ok=True)
                    await session.delete(episode_to_move)
            
            # 删除现已为空的源
            await session.delete(source_to_process)
        else:
            # 不冲突：移动此源及其弹幕文件
            logger.info(f"正在将源 '{provider}' (ID: {source_to_process.id}) 移动到目标番剧 (ID: {target_anime_id})。")
            for ep in source_to_process.episodes:
                if ep.danmakuFilePath:
                    old_path = _get_fs_path_from_web_path(ep.danmakuFilePath)
                    new_web_path = f"/danmaku/{target_anime_id}/{ep.id}.xml"
                    new_fs_path = _get_fs_path_from_web_path(new_web_path)
                    if old_path and old_path.exists() and new_fs_path:
                        new_fs_path.parent.mkdir(parents=True, exist_ok=True)
                        old_path.rename(new_fs_path)
                        ep.danmakuFilePath = new_web_path
            source_to_process.animeId = target_anime_id
            target_anime.sources.append(source_to_process)

    # 4. 重新编号目标番剧的所有源的 sourceOrder
    sorted_sources = sorted(target_anime.sources, key=lambda s: s.sourceOrder)
    logger.info(f"正在为目标番剧 (ID: {target_anime_id}) 的 {len(sorted_sources)} 个源重新编号...")
    for i, source in enumerate(sorted_sources):
        new_order = i + 1
        if source.sourceOrder != new_order:
            source.sourceOrder = new_order

    # 5. 删除现已为空的源番剧
    logger.info(f"正在删除现已为空的源番剧 (ID: {source_anime_id})。")
    await session.delete(source_anime)
    await session.commit()
    logger.info("番剧源重新关联成功。")
    return True

async def update_episode_info(session: AsyncSession, episode_id: int, update_data: models.EpisodeInfoUpdate) -> bool:
    """更新单个分集的信息。如果集数被修改，将重命名弹幕文件并更新路径。"""
    # 使用 joinedload 高效地获取关联的 source 和 anime 信息 # type: ignore
    stmt = select(Episode).where(Episode.id == episode_id).options(joinedload(Episode.source).joinedload(AnimeSource.anime))
    result = await session.execute(stmt)
    episode = result.scalar_one_or_none()

    if not episode:
        return False

    # 情况1: 集数未改变，仅更新标题或URL
    if episode.episodeIndex == update_data.episodeIndex:
        episode.title = update_data.title
        episode.sourceUrl = update_data.sourceUrl
        await session.commit()
        return True

    # 情况2: 集数已改变，需要重新生成ID并移动文件
    # 1. 检查新集数是否已存在，避免冲突
    conflict_stmt = select(Episode.id).where(
        Episode.sourceId == episode.sourceId,
        Episode.episodeIndex == update_data.episodeIndex
    )
    if (await session.execute(conflict_stmt)).scalar_one_or_none():
        raise ValueError("该集数已存在，请使用其他集数。")

    # 2. 计算新的确定性ID
    source_order = episode.source.sourceOrder
    if source_order is None:
        # 这是一个重要的回退和迁移逻辑。如果一个旧的源没有 sourceOrder，
        # 我们就为其分配一个新的、持久的序号。
        logger.warning(f"源 ID {episode.sourceId} 缺少 sourceOrder，将为其分配一个新的。")
        source_order = await _assign_source_order_if_missing(session, episode.source.animeId, episode.sourceId)
    new_episode_id_str = f"25{episode.source.animeId:06d}{source_order:02d}{update_data.episodeIndex:04d}"
    new_episode_id = int(new_episode_id_str)

    # 3. 重命名弹幕文件（如果存在）
    new_web_path = None
    if episode.danmakuFilePath:
        old_absolute_path = _get_fs_path_from_web_path(episode.danmakuFilePath)
        
        # 修正：新的Web路径和文件系统路径应与 tasks.py 保持一致（不包含 source_id）
        new_web_path = f"/danmaku/{episode.source.animeId}/{new_episode_id}.xml"
        new_absolute_path = DANMAKU_BASE_DIR / str(episode.source.animeId) / f"{new_episode_id}.xml"
        
        if old_absolute_path and old_absolute_path.exists():
            try:
                new_absolute_path.parent.mkdir(parents=True, exist_ok=True)
                old_absolute_path.rename(new_absolute_path)
                logger.info(f"弹幕文件已重命名: {old_absolute_path} -> {new_absolute_path}")
            except OSError as e:
                logger.error(f"重命名弹幕文件失败: {e}")
                new_web_path = episode.danmakuFilePath # 如果重命名失败，则保留旧路径
    
    # 4. 创建一个新的分集对象
    new_episode = Episode(
        id=new_episode_id, sourceId=episode.sourceId, episodeIndex=update_data.episodeIndex,
        title=update_data.title, sourceUrl=update_data.sourceUrl,
        providerEpisodeId=episode.providerEpisodeId, fetchedAt=episode.fetchedAt, 
        commentCount=episode.commentCount, danmakuFilePath=new_web_path
    )
    session.add(new_episode)
    
    # 5. 删除旧的分集记录 (由于没有弹幕关联，可以直接删除)
    await session.delete(episode)
    await session.commit()
    return True

async def sync_scrapers_to_db(session: AsyncSession, provider_names: List[str]):
    if not provider_names: return
    
    existing_stmt = select(Scraper.providerName)
    existing_providers = set((await session.execute(existing_stmt)).scalars().all())
    
    new_providers = [name for name in provider_names if name not in existing_providers]
    if not new_providers: return

    max_order_stmt = select(func.max(Scraper.displayOrder))
    max_order = (await session.execute(max_order_stmt)).scalar_one_or_none() or 0
    
    session.add_all([
        Scraper(providerName=name, displayOrder=max_order + i + 1, useProxy=False)
        for i, name in enumerate(new_providers)
    ])
    await session.commit()

async def get_scraper_setting_by_name(session: AsyncSession, provider_name: str) -> Optional[Dict[str, Any]]:
    """获取单个搜索源的设置。"""
    scraper = await session.get(Scraper, provider_name)
    if scraper:
        return {
            "providerName": scraper.providerName,
            "isEnabled": scraper.isEnabled,
            "displayOrder": scraper.displayOrder,
            "useProxy": scraper.useProxy
        }
    return None

async def update_scraper_proxy(session: AsyncSession, provider_name: str, use_proxy: bool) -> bool:
    """更新单个搜索源的代理设置。"""
    stmt = update(Scraper).where(Scraper.providerName == provider_name).values(useProxy=use_proxy)
    result = await session.execute(stmt)
    return result.rowcount > 0

async def get_all_scraper_settings(session: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(Scraper).order_by(Scraper.displayOrder)
    result = await session.execute(stmt)
    return [
        {"providerName": s.providerName, "isEnabled": s.isEnabled, "displayOrder": s.displayOrder, "useProxy": s.useProxy}
        for s in result.scalars()
    ]

async def update_scrapers_settings(session: AsyncSession, settings: List[models.ScraperSetting]):
    for s in settings:
        await session.execute(
            update(Scraper)
            .where(Scraper.providerName == s.providerName)
            .values(isEnabled=s.isEnabled, displayOrder=s.displayOrder, useProxy=s.useProxy)
        )
    await session.commit()

async def remove_stale_scrapers(session: AsyncSession, discovered_providers: List[str]):
    if not discovered_providers:
        logging.warning("发现的搜索源列表为空，跳过清理过时源的操作。")
        return
    stmt = delete(Scraper).where(Scraper.providerName.notin_(discovered_providers))
    await session.execute(stmt)
    await session.commit()

# --- Metadata Source Management ---

async def sync_metadata_sources_to_db(session: AsyncSession, provider_names: List[str]):
    if not provider_names: return
    
    existing_stmt = select(MetadataSource.providerName)
    existing_providers = set((await session.execute(existing_stmt)).scalars().all())
    
    new_providers = [name for name in provider_names if name not in existing_providers]
    if not new_providers: return

    max_order_stmt = select(func.max(MetadataSource.displayOrder))
    max_order = (await session.execute(max_order_stmt)).scalar_one_or_none() or 0
    
    session.add_all([
        MetadataSource(
            providerName=name, displayOrder=max_order + i + 1,
            isAuxSearchEnabled=(name == 'tmdb'), useProxy=False
        )
        for i, name in enumerate(new_providers)
    ])
    await session.commit()

async def get_all_metadata_source_settings(session: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(MetadataSource).order_by(MetadataSource.displayOrder)
    result = await session.execute(stmt)
    return [
        {"providerName": s.providerName, "isEnabled": s.isEnabled, "isAuxSearchEnabled": s.isAuxSearchEnabled, "displayOrder": s.displayOrder, "useProxy": s.useProxy, "isFailoverEnabled": s.isFailoverEnabled}
        for s in result.scalars()
    ]

async def update_metadata_sources_settings(session: AsyncSession, settings: List['models.MetadataSourceSettingUpdate']):
    for s in settings:
        is_aux_enabled = True if s.providerName == 'tmdb' else s.isAuxSearchEnabled
        # 新增：确保 isFailoverEnabled 字段被正确处理
        is_failover_enabled = s.isFailoverEnabled if hasattr(s, 'isFailoverEnabled') else False
        await session.execute(
            update(MetadataSource)
            .where(MetadataSource.providerName == s.providerName)
            .values(isAuxSearchEnabled=is_aux_enabled, displayOrder=s.displayOrder, useProxy=s.useProxy, isFailoverEnabled=is_failover_enabled)
        )
    await session.commit()

async def get_enabled_aux_metadata_sources(session: AsyncSession) -> List[Dict[str, Any]]:
    """获取所有已启用辅助搜索的元数据源。"""
    stmt = (
        select(MetadataSource)
        .where(MetadataSource.isAuxSearchEnabled == True)
        .order_by(MetadataSource.displayOrder)
    )
    result = await session.execute(stmt)
    return [
        {"providerName": s.providerName, "isEnabled": s.isEnabled, "isAuxSearchEnabled": s.isAuxSearchEnabled, "displayOrder": s.displayOrder, "useProxy": s.useProxy, "isFailoverEnabled": s.isFailoverEnabled}
        for s in result.scalars()
    ]

async def get_enabled_failover_sources(session: AsyncSession) -> List[Dict[str, Any]]:
    """获取所有已启用故障转移的元数据源。"""
    stmt = (
        select(MetadataSource)
        .where(MetadataSource.isFailoverEnabled == True)
        .order_by(MetadataSource.displayOrder)
    )
    result = await session.execute(stmt)
    return [
        {"providerName": s.providerName, "isEnabled": s.isEnabled, "isAuxSearchEnabled": s.isAuxSearchEnabled, "displayOrder": s.displayOrder, "useProxy": s.useProxy, "isFailoverEnabled": s.isFailoverEnabled}
        for s in result.scalars()
    ]
# --- Config & Cache ---

async def get_config_value(session: AsyncSession, key: str, default: str) -> str:
    stmt = select(Config.configValue).where(Config.configKey == key)
    result = await session.execute(stmt)
    value = result.scalar_one_or_none()
    return value if value is not None else default

async def get_cache(session: AsyncSession, key: str) -> Optional[Any]:
    stmt = select(CacheData.cacheValue).where(CacheData.cacheKey == key, CacheData.expiresAt > func.now())
    result = await session.execute(stmt)
    value = result.scalar_one_or_none()
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None

async def set_cache(session: AsyncSession, key: str, value: Any, ttl_seconds: int, provider: Optional[str] = None):
    json_value = json.dumps(value, ensure_ascii=False)
    expires_at = get_now() + timedelta(seconds=ttl_seconds)

    dialect = session.bind.dialect.name
    values_to_insert = {"cacheProvider": provider, "cacheKey": key, "cacheValue": json_value, "expiresAt": expires_at}

    if dialect == 'mysql':
        stmt = mysql_insert(CacheData).values(values_to_insert)
        stmt = stmt.on_duplicate_key_update(
            cache_provider=stmt.inserted.cache_provider,
            cache_value=stmt.inserted.cache_value,
            expires_at=stmt.inserted.expires_at
        )
    elif dialect == 'postgresql':
        stmt = postgresql_insert(CacheData).values(values_to_insert)
        # 修正：使用 on_conflict_do_update 并通过 index_elements 指定主键列，以提高兼容性
        stmt = stmt.on_conflict_do_update(
            index_elements=['cache_key'],
            set_={"cache_provider": stmt.excluded.cache_provider, "cache_value": stmt.excluded.cache_value, "expires_at": stmt.excluded.expires_at}
        )
    else:
        raise NotImplementedError(f"缓存设置功能尚未为数据库类型 '{dialect}' 实现。")

    await session.execute(stmt)
    await session.commit()

async def update_config_value(session: AsyncSession, key: str, value: str):
    dialect = session.bind.dialect.name
    values_to_insert = {"configKey": key, "configValue": value}

    if dialect == 'mysql':
        stmt = mysql_insert(Config).values(values_to_insert)
        stmt = stmt.on_duplicate_key_update(config_value=stmt.inserted.config_value)
    elif dialect == 'postgresql':
        stmt = postgresql_insert(Config).values(values_to_insert)
        # 修正：使用 on_conflict_do_update 并通过 index_elements 指定主键列，以提高兼容性
        stmt = stmt.on_conflict_do_update(
            index_elements=['config_key'],
            set_={'config_value': stmt.excluded.config_value}
        )
    else:
        raise NotImplementedError(f"配置更新功能尚未为数据库类型 '{dialect}' 实现。")

    await session.execute(stmt)
    await session.commit()

async def increment_user_metric(session: AsyncSession, user_id: int, metric: str, delta: int = 1):
    key = f"user_metrics:{metric}:{user_id}"
    current = await get_config_value(session, key, "0")
    try:
        num = int(current)
    except ValueError:
        num = 0
    num += delta
    await update_config_value(session, key, str(num))

async def get_user_metrics(session: AsyncSession, user_id: int) -> Dict[str, int]:
    result = {}
    for metric in ("search_count", "download_count"):
        val = await get_config_value(session, f"user_metrics:{metric}:{user_id}", "0")
        try:
            result[metric] = int(val)
        except ValueError:
            result[metric] = 0
    return result

async def clear_expired_cache(session: AsyncSession):
    await session.execute(delete(CacheData).where(CacheData.expiresAt <= get_now()))
    await session.commit()

async def clear_expired_oauth_states(session: AsyncSession):
    await session.execute(delete(OauthState).where(OauthState.expiresAt <= get_now()))
    await session.commit()

async def clear_all_cache(session: AsyncSession) -> int:
    result = await session.execute(delete(CacheData))
    await session.commit()
    return result.rowcount

async def delete_cache(session: AsyncSession, key: str) -> bool:
    result = await session.execute(delete(CacheData).where(CacheData.cacheKey == key))
    await session.commit()
    return result.rowcount > 0

async def update_episode_fetch_time(session: AsyncSession, episode_id: int):
    await session.execute(update(Episode).where(Episode.id == episode_id).values(fetchedAt=get_now()))
    await session.commit()

async def update_episode_danmaku_info(session: AsyncSession, episode_id: int, file_path: str, count: int):
    """更新分集的弹幕文件路径和弹幕数量。"""
    stmt = update(Episode).where(Episode.id == episode_id).values(
        danmakuFilePath=file_path, commentCount=count, fetchedAt=get_now()
    )
    await session.execute(stmt) # type: ignore
    await session.flush()

# --- User Usage Aggregation ---
async def log_user_usage(session: AsyncSession, user_id: int, action: str, amount: int = 1):
    """记录用户用量事件。
    注意：调用方应在事务安全点调用，比如单集下载成功写入后。
    """
    log = UserUsageLog(userId=user_id, action=action, amount=amount, createdAt=get_now())
    session.add(log)
    # 使用 flush 即可，批量调用时由外层统一 commit
    await session.flush()

async def aggregate_user_usage(session: AsyncSession, start_time: Optional[datetime], end_time: Optional[datetime]) -> List[Dict[str, Any]]:
    """按用户聚合用量（action='download_episode'），可选时间窗过滤。"""
    stmt = select(UserUsageLog.userId, func.sum(UserUsageLog.amount).label('downloads')).where(UserUsageLog.action == 'download_episode')
    if start_time is not None:
        stmt = stmt.where(UserUsageLog.createdAt >= start_time)
    if end_time is not None:
        stmt = stmt.where(UserUsageLog.createdAt <= end_time)
    stmt = stmt.group_by(UserUsageLog.userId)
    rows = (await session.execute(stmt)).all()
    # 补充用户名
    result: List[Dict[str, Any]] = []
    for uid, downloads in rows:
        u = await session.get(User, uid)
        result.append({
            'userId': uid,
            'username': getattr(u, 'username', str(uid)),
            'downloads': int(downloads or 0),
            'role': getattr(u, 'role', 'user'),
        })
    return result

async def aggregate_token_usage(
    session: AsyncSession,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    *,
    viewer_user_id: Optional[int],
    can_view_all: bool
) -> List[Dict[str, Any]]:
    """按 Token 聚合调用次数（基于 token_access_logs.status='allowed'）。
    - 当 can_view_all=False 时，仅统计 viewer 可见的 Token（全局 + 自己的私有）。
    - 时间窗：若 start_time/end_time 皆为 None，则统计“所有时间”；否则统计区间内。
    返回字段：tokenId, name, ownerUserId, ownerUsername, createdByUserId, createdByUsername, calls
    """
    owner = aliased(User)
    creator = aliased(User)
    l = aliased(TokenAccessLog)

    # 构建连接条件：仅统计允许通过的访问日志
    join_cond = (l.tokenId == ApiToken.id)
    if start_time is not None:
        join_cond = and_(join_cond, l.accessTime >= start_time)
    if end_time is not None:
        join_cond = and_(join_cond, l.accessTime <= end_time)
    join_cond = and_(join_cond, l.status == 'allowed')

    stmt = (
        select(
            ApiToken.id.label('tokenId'),
            ApiToken.name,
            ApiToken.ownerUserId,
            ApiToken.createdByUserId,
            owner.username.label('ownerUsername'),
            creator.username.label('createdByUsername'),
            func.count(l.id).label('calls')
        )
        .join(owner, ApiToken.ownerUserId == owner.id, isouter=True)
        .join(creator, ApiToken.createdByUserId == creator.id, isouter=True)
        .join(l, join_cond, isouter=True)
        .group_by(
            ApiToken.id,
            ApiToken.name,
            ApiToken.ownerUserId,
            ApiToken.createdByUserId,
            owner.username,
            creator.username,
        )
        .order_by(func.count(l.id).desc())
    )

    if not can_view_all and viewer_user_id is not None:
        stmt = stmt.where(or_(ApiToken.ownerUserId == viewer_user_id, ApiToken.ownerUserId.is_(None)))

    rows = (await session.execute(stmt)).all()
    out: List[Dict[str, Any]] = []
    for tokenId, name, ownerUserId, createdByUserId, ownerUsername, createdByUsername, calls in rows:
        out.append({
            'tokenId': int(tokenId),
            'name': name,
            'ownerUserId': ownerUserId,
            'ownerUsername': ownerUsername,
            'createdByUserId': createdByUserId,
            'createdByUsername': createdByUsername,
            'calls': int(calls or 0),
        })
    return out
# --- API Token Management ---

async def get_all_api_tokens(session: AsyncSession, *, owner_user_id: Optional[int] = None, include_global: bool = True, is_admin: bool = False) -> List[Dict[str, Any]]:
    # 左连接拥有者与创建者用户名
    owner = aliased(User)
    creator = aliased(User)
    stmt = (
        select(ApiToken, owner.username, creator.username)
        .join(owner, ApiToken.ownerUserId == owner.id, isouter=True)
        .join(creator, ApiToken.createdByUserId == creator.id, isouter=True)
        .order_by(ApiToken.createdAt.desc())
    )
    if not is_admin and owner_user_id is not None:
        if include_global:
            stmt = stmt.where(or_(ApiToken.ownerUserId == owner_user_id, ApiToken.ownerUserId.is_(None)))
        else:
            stmt = stmt.where(ApiToken.ownerUserId == owner_user_id)
    result = await session.execute(stmt)
    rows = result.all()
    items: List[Dict[str, Any]] = []
    for t, owner_name, creator_name in rows:
        items.append({
            "id": t.id,
            "name": t.name,
            "token": t.token,
            "isEnabled": t.isEnabled,
            "isLocked": getattr(t, "isLocked", False),
            "expiresAt": t.expiresAt,
            "createdAt": t.createdAt,
            "dailyCallLimit": t.dailyCallLimit,
            "dailyCallCount": t.dailyCallCount,
            "ownerUserId": getattr(t, "ownerUserId", None),
            "ownerUsername": owner_name,
            "createdByUserId": getattr(t, "createdByUserId", None),
            "createdByUsername": creator_name,
        })
    return items

async def get_api_token_by_id(session: AsyncSession, token_id: int) -> Optional[Dict[str, Any]]:
    token = await session.get(ApiToken, token_id)
    if not token:
        return None
    owner_name = None
    creator_name = None
    if getattr(token, 'ownerUserId', None) is not None:
        u = await session.get(User, token.ownerUserId)
        owner_name = getattr(u, 'username', None) if u else None
    if getattr(token, 'createdByUserId', None) is not None:
        cu = await session.get(User, token.createdByUserId)
        creator_name = getattr(cu, 'username', None) if cu else None
    return {
        "id": token.id,
        "name": token.name,
        "token": token.token,
        "isEnabled": token.isEnabled,
        "isLocked": getattr(token, "isLocked", False),
        "expiresAt": token.expiresAt,
        "createdAt": token.createdAt,
        "dailyCallLimit": token.dailyCallLimit,
        "dailyCallCount": token.dailyCallCount,
        "ownerUserId": getattr(token, "ownerUserId", None),
        "ownerUsername": owner_name,
        "createdByUserId": getattr(token, "createdByUserId", None),
        "createdByUsername": creator_name,
    }

async def get_api_token_by_token_str(session: AsyncSession, token_str: str) -> Optional[Dict[str, Any]]:
    stmt = select(ApiToken).where(ApiToken.token == token_str)
    result = await session.execute(stmt)
    token = result.scalar_one_or_none()
    if not token:
        return None
    owner_name = None
    creator_name = None
    if getattr(token, 'ownerUserId', None) is not None:
        u = await session.get(User, token.ownerUserId)
        owner_name = getattr(u, 'username', None) if u else None
    if getattr(token, 'createdByUserId', None) is not None:
        cu = await session.get(User, token.createdByUserId)
        creator_name = getattr(cu, 'username', None) if cu else None
    return {
        "id": token.id,
        "name": token.name,
        "token": token.token,
        "isEnabled": token.isEnabled,
        "isLocked": getattr(token, "isLocked", False),
        "expiresAt": token.expiresAt,
        "createdAt": token.createdAt,
        "dailyCallLimit": token.dailyCallLimit,
        "dailyCallCount": token.dailyCallCount,
        "ownerUserId": getattr(token, "ownerUserId", None),
        "ownerUsername": owner_name,
        "createdByUserId": getattr(token, "createdByUserId", None),
        "createdByUsername": creator_name,
    }

async def create_api_token(session: AsyncSession, name: str, token: str, validityPeriod: str, daily_call_limit: int, owner_user_id: Optional[int] = None, created_by_user_id: Optional[int] = None) -> int:
    """创建新的API Token，如果名称已存在则会失败。"""
    # 检查名称是否已存在
    existing_token = await session.execute(select(ApiToken).where(ApiToken.name == name))
    if existing_token.scalar_one_or_none():
        raise ValueError(f"名称为 '{name}' 的Token已存在。")
    
    expires_at = None
    if validityPeriod != "permanent":
        days = int(validityPeriod.replace('d', '')) # type: ignore
        # 修正：确保写入数据库的时间是 naive 的
        expires_at = get_now() + timedelta(days=days)
    new_token = ApiToken(
        name=name, token=token, 
        expiresAt=expires_at, 
        createdAt=get_now(),
        dailyCallLimit=daily_call_limit,
        isLocked=False,
        ownerUserId=owner_user_id,
        createdByUserId=created_by_user_id
    )
    session.add(new_token)
    await session.commit()
    return new_token.id

async def update_api_token(
    session: AsyncSession,
    token_id: int,
    name: str,
    daily_call_limit: int,
    validity_period: str,
    *,
    ignore_lock: bool = False,
) -> bool:
    """更新API Token的名称、调用上限和有效期。"""
    token = await session.get(orm_models.ApiToken, token_id)
    if not token:
        return False
    if getattr(token, "isLocked", False) and not ignore_lock:
        raise PermissionError("Token is locked")

    token.name = name
    token.dailyCallLimit = daily_call_limit

    if validity_period != 'custom':
        if validity_period == 'permanent':
            token.expiresAt = None
        else:
            try:
                days = int(validity_period.replace('d', ''))
                token.expiresAt = get_now() + timedelta(days=days)
            except (ValueError, TypeError):
                logger.warning(f"更新Token时收到无效的有效期格式: '{validity_period}'")

    await session.commit()
    return True

async def delete_api_token(session: AsyncSession, token_id: int, *, ignore_lock: bool = False) -> bool:
    token = await session.get(ApiToken, token_id)
    if token:
        if getattr(token, "isLocked", False) and not ignore_lock:
            raise PermissionError("Token is locked")
        await session.delete(token)
        await session.commit()
        return True
    return False

async def toggle_api_token(session: AsyncSession, token_id: int, *, ignore_lock: bool = False) -> bool:
    token = await session.get(ApiToken, token_id)
    if token:
        if getattr(token, "isLocked", False) and not ignore_lock:
            raise PermissionError("Token is locked")
        token.isEnabled = not token.isEnabled
        await session.commit()
        return True
    return False

async def reset_token_counter(session: AsyncSession, token_id: int, *, ignore_lock: bool = False) -> bool:
    """将指定Token的每日调用次数重置为0。"""
    token = await session.get(orm_models.ApiToken, token_id)
    if not token:
        return False
    if getattr(token, "isLocked", False) and not ignore_lock:
        raise PermissionError("Token is locked")
    
    token.dailyCallCount = 0
    await session.commit()
    return True

async def validate_api_token(session: AsyncSession, token: str) -> Optional[Dict[str, Any]]:
    stmt = select(ApiToken).where(ApiToken.token == token, ApiToken.isEnabled == True)
    result = await session.execute(stmt)
    token_info = result.scalar_one_or_none()
    if not token_info:
        return None
    # 随着 orm_models.py 和 database.py 的修复，SQLAlchemy 现在应返回时区感知的 UTC 日期时间。
    if token_info.expiresAt:
        if token_info.expiresAt < get_now(): # Compare naive datetimes
            return None # Token 已过期
    
    # --- 新增：每日调用限制检查 ---
    now = get_now()
    current_count = token_info.dailyCallCount
    
    # 如果有上次调用记录，且不是今天，则视为计数为0
    if token_info.lastCallAt and token_info.lastCallAt.date() < now.date():
        current_count = 0
        
    if token_info.dailyCallLimit != -1 and current_count >= token_info.dailyCallLimit:
        return None # Token 已达到每日调用上限

    return {
        "id": token_info.id,
        "expiresAt": token_info.expiresAt,
        "dailyCallLimit": token_info.dailyCallLimit,
        "dailyCallCount": token_info.dailyCallCount,
        "ownerUserId": getattr(token_info, 'ownerUserId', None),
    }  # type: ignore

async def increment_token_call_count(session: AsyncSession, token_id: int):
    """为指定的Token增加调用计数。"""
    token = await session.get(ApiToken, token_id)
    if not token:
        return

    # 修正：简化函数职责，现在只负责增加计数。
    # 重置逻辑已移至 validate_api_token 中，以避免竞争条件。
    token.dailyCallCount += 1
    # 总是更新最后调用时间
    token.lastCallAt = get_now()
    # 注意：这里不 commit，由调用方（API端点）来决定何时提交事务

# --- UA Filter and Log Services ---

# --- Invite Management ---
async def create_invite(
    session: AsyncSession,
    *,
    created_by_user_id: int,
    code: str,
    max_uses: int,
    per_hour_limit: Optional[int],
    permissions_json: Optional[str],
    remark: Optional[str]
) -> int:
    inv = Invite(
        code=code,
        createdByUserId=created_by_user_id,
        createdAt=get_now(),
        maxUses=max_uses,
        usedCount=0,
        perHourLimit=per_hour_limit,
        permissionsJson=permissions_json,
        remark=remark,
        isEnabled=True,
    )
    session.add(inv)
    await session.commit()
    return inv.id

async def list_invites(session: AsyncSession, *, created_by_user_id: Optional[int], is_admin: bool) -> List[Dict[str, Any]]:
    stmt = select(Invite)
    if not is_admin and created_by_user_id is not None:
        stmt = stmt.where(Invite.createdByUserId == created_by_user_id)
    stmt = stmt.order_by(Invite.createdAt.desc())
    rows = (await session.execute(stmt)).scalars().all()
    now = get_now()
    return [
        {
            'id': r.id,
            'code': r.code,
            'createdByUserId': r.createdByUserId,
            'createdAt': r.createdAt,
            'maxUses': r.maxUses,
            'usedCount': r.usedCount,
            'expiresAt': getattr(r, 'expiresAt', None),
            'isExpired': (getattr(r, 'expiresAt', None) is not None) and (getattr(r, 'expiresAt') <= now),
            'perHourLimit': r.perHourLimit,
            'remark': r.remark,
            'isEnabled': r.isEnabled,
            'permissions': (json.loads(r.permissionsJson) if r.permissionsJson else None),
        }
        for r in rows
    ]

async def delete_invite(session: AsyncSession, invite_id: int) -> bool:
    inv = await session.get(Invite, invite_id)
    if not inv:
        return False
    await session.delete(inv)
    await session.commit()
    return True

async def get_invite_by_code(session: AsyncSession, code: str) -> Optional[Dict[str, Any]]:
    stmt = select(Invite).where(Invite.code == code, Invite.isEnabled == True)
    r = (await session.execute(stmt)).scalar_one_or_none()
    if not r:
        return None
    # 过期视为无效
    if getattr(r, 'expiresAt', None) is not None and getattr(r, 'expiresAt') <= get_now():
        return None
    return {
        'id': r.id,
        'code': r.code,
        'createdByUserId': r.createdByUserId,
        'createdAt': r.createdAt,
        'maxUses': r.maxUses,
        'usedCount': r.usedCount,
        'expiresAt': getattr(r, 'expiresAt', None),
        'perHourLimit': r.perHourLimit,
        'permissions': (json.loads(r.permissionsJson) if r.permissionsJson else None),
        'remark': r.remark,
        'isEnabled': r.isEnabled,
    }

async def increment_invite_used(session: AsyncSession, invite_id: int) -> None:
    inv = await session.get(Invite, invite_id)
    if not inv:
        return
    inv.usedCount = (inv.usedCount or 0) + 1
    # 自动禁用：到达上限
    if inv.maxUses is not None and inv.usedCount >= inv.maxUses:
        inv.isEnabled = False
    await session.commit()

async def get_ua_rules(session: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(UaRule).order_by(UaRule.createdAt.desc())
    result = await session.execute(stmt)
    return [{"id": r.id, "uaString": r.uaString, "createdAt": r.createdAt} for r in result.scalars()]

async def add_ua_rule(session: AsyncSession, ua_string: str) -> int:
    new_rule = UaRule(uaString=ua_string, createdAt=get_now())
    session.add(new_rule)
    await session.commit()
    return new_rule.id

async def delete_ua_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await session.get(UaRule, rule_id)
    if rule:
        await session.delete(rule)
        await session.commit()
        return True
    return False

async def create_token_access_log(session: AsyncSession, token_id: int, ip_address: str, user_agent: Optional[str], log_status: str, path: Optional[str] = None):
    new_log = TokenAccessLog(
        tokenId=token_id, 
        ipAddress=ip_address, 
        userAgent=user_agent, 
        status=log_status, 
        path=path, 
        accessTime=get_now())
    session.add(new_log)
    await session.commit()

async def get_token_access_logs(session: AsyncSession, token_id: int, *, ignore_lock: bool = False) -> List[Dict[str, Any]]:
    # 如果 Token 被锁定，阻止获取访问日志
    token = await session.get(ApiToken, token_id)
    if token and getattr(token, "isLocked", False) and not ignore_lock:
        raise PermissionError("Token is locked")
    stmt = select(TokenAccessLog).where(TokenAccessLog.tokenId == token_id).order_by(TokenAccessLog.accessTime.desc()).limit(200)
    result = await session.execute(stmt)
    return [
        {"ipAddress": log.ipAddress, "userAgent": log.userAgent, "accessTime": log.accessTime, "status": log.status, "path": log.path}
        for log in result.scalars()
    ]

# ---------------- User-level rate limit & permissions ----------------

async def get_or_create_user_rate_state(session: AsyncSession, user_id: int) -> UserRateLimitState:
    state = await session.get(UserRateLimitState, user_id)
    if state:
        return state
    state = UserRateLimitState(userId=user_id, requestCount=0, lastResetTime=get_now().replace(tzinfo=None))
    session.add(state)
    await session.flush()
    return state

async def check_and_increment_user_rate(session: AsyncSession, user: Dict[str, Any], per_hour_limit_default: int = 1000):
    limit = user.get('perHourLimit') if user else None
    if not limit:
        limit = per_hour_limit_default
    state = await get_or_create_user_rate_state(session, user['id'])
    now = get_now().replace(tzinfo=None)
    if (now - state.lastResetTime).total_seconds() >= 3600:
        state.requestCount = 0
        state.lastResetTime = now
    if limit >= 0 and state.requestCount >= limit:
        raise PermissionError('User rate limit exceeded')
    state.requestCount += 1
    await session.flush()

async def get_user_rate_seconds_until_reset(session: AsyncSession, user_id: int) -> int:
    state = await get_or_create_user_rate_state(session, user_id)
    now = get_now().replace(tzinfo=None)
    elapsed = (now - state.lastResetTime).total_seconds()
    return max(0, int(3600 - elapsed))

async def get_user_by_username_full(session: AsyncSession, username: str) -> Optional[Dict[str, Any]]:
    stmt = select(orm_models.User).where(orm_models.User.username == username)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user:
        return None
    return {
        'id': user.id,
        'username': user.username,
        'role': getattr(user, 'role', 'user'),
        'canCreateAdmin': getattr(user, 'canCreateAdmin', False),
        'perHourLimit': getattr(user, 'perHourLimit', None),
        'permissionsJson': getattr(user, 'permissionsJson', None),
        'remark': getattr(user, 'remark', None),
    }

async def list_users_with_metrics(session: AsyncSession) -> List[Dict[str, Any]]:
    users = (await session.execute(select(orm_models.User))).scalars().all()
    result: List[Dict[str, Any]] = []
    for u in users:
        usage = (await session.execute(select(func.coalesce(func.sum(orm_models.ApiToken.dailyCallCount), 0)).where(orm_models.ApiToken.ownerUserId == u.id))).scalar_one()
        result.append({
            'id': u.id,
            'username': u.username,
            'role': getattr(u,'role','user'),
            'canCreateAdmin': getattr(u,'canCreateAdmin', False),
            'perHourLimit': getattr(u,'perHourLimit', None),
            'permissionsJson': getattr(u,'permissionsJson', None),
            'remark': getattr(u,'remark', None),
            'searchCount': 0,
            'tokenUsage': int(usage or 0),
            'downloadCount': 0,
        })
    return result

async def create_user_with_permissions(session: AsyncSession, username: str, password_hash: str, role: str, per_hour_limit: Optional[int], can_create_admin: bool, permissions_json: Optional[str], remark: Optional[str] = None) -> int:
    user = orm_models.User(username=username, hashedPassword=password_hash, createdAt=get_now(), role=role, canCreateAdmin=can_create_admin, perHourLimit=per_hour_limit, permissionsJson=permissions_json, remark=remark)
    session.add(user)
    await session.commit()
    return user.id

async def update_user_info(session: AsyncSession, user_id: int, *, role: Optional[str]=None, per_hour_limit: Optional[int]=None, can_create_admin: Optional[bool]=None, permissions_json: Optional[str]=None, password_hash: Optional[str]=None, remark: Optional[str]=None) -> bool:
    user = await session.get(orm_models.User, user_id)
    if not user:
        return False
    # 角色策略：仅 username=admin 为 'admin'；其余强制为 'user'
    if role is not None:
        uname = (getattr(user, 'username', '') or '').lower()
        user.role = 'admin' if uname == 'admin' else 'user'
    if per_hour_limit is not None:
        user.perHourLimit = per_hour_limit
    if can_create_admin is not None:
        # 此字段已废弃，后续不再使用；保持为 False
        user.canCreateAdmin = False
    if permissions_json is not None:
        user.permissionsJson = permissions_json
    if password_hash is not None:
        user.hashedPassword = password_hash
    if remark is not None:
        user.remark = remark
    await session.commit()
    return True

async def set_token_owner(session: AsyncSession, token_id: int, owner_user_id: Optional[int], *, ignore_lock: bool = False) -> bool:
    """设置 Token 的归属（None 表示全局）。"""
    token = await session.get(orm_models.ApiToken, token_id)
    if not token:
        return False
    if getattr(token, "isLocked", False) and not ignore_lock:
        raise PermissionError("Token is locked")
    token.ownerUserId = owner_user_id
    await session.commit()
    return True

async def delete_user(session: AsyncSession, user_id: int) -> bool:
    user = await session.get(orm_models.User, user_id)
    if not user:
        return False
    # 解除该用户下的token归属
    await session.execute(update(orm_models.ApiToken).where(orm_models.ApiToken.ownerUserId == user_id).values(ownerUserId=None))
    # 删除用户速率状态与用量日志
    await session.execute(text("DELETE FROM user_rate_limit_state WHERE user_id = :uid"), {"uid": user_id})
    await session.execute(text("DELETE FROM user_usage_logs WHERE user_id = :uid"), {"uid": user_id})
    # 删除 bangumi 授权等（若存在）
    await session.execute(text("DELETE FROM bangumi_auth WHERE user_id = :uid"), {"uid": user_id})
    await session.delete(user)
    await session.commit()
    return True

async def set_token_locked(session: AsyncSession, token_id: int) -> bool:
    token = await session.get(ApiToken, token_id)
    if not token:
        return False
    if getattr(token, "isLocked", False):
        return True
    token.isLocked = True
    await session.commit()
    return True

async def set_token_unlocked(session: AsyncSession, token_id: int) -> bool:
    token = await session.get(ApiToken, token_id)
    if not token:
        return False
    if not getattr(token, "isLocked", False):
        return True
    token.isLocked = False
    await session.commit()
    return True

async def toggle_source_favorite_status(session: AsyncSession, source_id: int) -> Optional[bool]:
    """
    Toggles the favorite status of a source.
    Returns the new favorite status (True/False) on success, or None if not found.
    """
    source = await session.get(AnimeSource, source_id)
    if not source:
        return None

    # Toggle the target source
    source.isFavorited = not source.isFavorited
    
    # If it was favorited, unfavorite all others for the same anime
    if source.isFavorited:
        stmt = (
            update(AnimeSource)
            .where(AnimeSource.animeId == source.animeId, AnimeSource.id != source_id)
            .values(isFavorited=False)
        )
        await session.execute(stmt)
    
    await session.commit()
    return source.isFavorited

async def toggle_source_incremental_refresh(session: AsyncSession, source_id: int) -> bool:
    source = await session.get(AnimeSource, source_id)
    if not source:
        return False
    source.incrementalRefreshEnabled = not source.incrementalRefreshEnabled
    await session.commit()
    return True

async def increment_incremental_refresh_failures(session: AsyncSession, source_id: int) -> int:
    source = await session.get(AnimeSource, source_id)
    if not source:
        return 0
    source.incrementalRefreshFailures += 1
    await session.commit()
    return source.incrementalRefreshFailures

async def reset_incremental_refresh_failures(session: AsyncSession, source_id: int):
    await session.execute(update(AnimeSource).where(AnimeSource.id == source_id).values(incrementalRefreshFailures=0))
    await session.commit()

async def disable_incremental_refresh(session: AsyncSession, source_id: int) -> bool:
    result = await session.execute(update(AnimeSource).where(AnimeSource.id == source_id).values(incrementalRefreshEnabled=False))
    await session.commit()
    return result.rowcount > 0

# --- OAuth State Management ---

async def create_oauth_state(session: AsyncSession, user_id: int) -> str:
    state = secrets.token_urlsafe(32)
    expires_at = get_now() + timedelta(minutes=10)
    new_state = OauthState(stateKey=state, userId=user_id, expiresAt=expires_at)
    session.add(new_state)
    await session.commit()
    return state

async def consume_oauth_state(session: AsyncSession, state: str) -> Optional[int]:
    stmt = select(OauthState).where(OauthState.stateKey == state, OauthState.expiresAt > get_now())
    result = await session.execute(stmt)
    state_obj = result.scalar_one_or_none()
    if state_obj:
        user_id = state_obj.userId
        await session.delete(state_obj)
        await session.commit()
        return user_id
    return None

async def get_bangumi_auth(session: AsyncSession, user_id: int) -> Dict[str, Any]:
    """
    获取用户的Bangumi授权状态。
    注意：此函数现在返回一个为UI定制的字典，而不是完整的认证对象。
    """
    auth = await session.get(BangumiAuth, user_id)
    if auth:
        return {
            "isAuthenticated": True,
            "nickname": auth.nickname,
            "avatarUrl": auth.avatarUrl,
            "bangumiUserId": auth.bangumiUserId,
            "authorizedAt": auth.authorizedAt,
            "expiresAt": auth.expiresAt,
        }
    return {"isAuthenticated": False}

async def save_bangumi_auth(session: AsyncSession, user_id: int, auth_data: Dict[str, Any]):
    auth = await session.get(BangumiAuth, user_id)
    expires_at = auth_data.get('expiresAt')
    if expires_at and hasattr(expires_at, 'tzinfo') and expires_at.tzinfo:
        expires_at = expires_at.replace(tzinfo=None)

    if auth:
        auth.bangumiUserId = auth_data.get('bangumiUserId')
        auth.nickname = auth_data.get('nickname')
        auth.avatarUrl = auth_data.get('avatarUrl')
        auth.accessToken = auth_data.get('accessToken')
        auth.refreshToken = auth_data.get('refreshToken')
        auth.expiresAt = expires_at
        auth.authorizedAt = get_now()
    else:
        auth_data_copy = auth_data.copy()
        auth_data_copy['expiresAt'] = expires_at
        auth = BangumiAuth(userId=user_id, authorizedAt=get_now(), **auth_data_copy)
        session.add(auth)
    await session.commit()

async def delete_bangumi_auth(session: AsyncSession, user_id: int) -> bool:
    auth = await session.get(BangumiAuth, user_id)
    if auth:
        await session.delete(auth)
        await session.commit()
        return True
    return False

async def get_sources_with_incremental_refresh_enabled(session: AsyncSession) -> List[int]:
    stmt = select(AnimeSource.id).where(AnimeSource.incrementalRefreshEnabled == True)
    result = await session.execute(stmt)
    return result.scalars().all()

# --- Scheduled Tasks ---

async def get_animes_with_tmdb_id(session: AsyncSession) -> List[Dict[str, Any]]:
    stmt = (
        select(Anime.id.label("animeId"), Anime.title, AnimeMetadata.tmdbId, AnimeMetadata.tmdbEpisodeGroupId)
        .join(AnimeMetadata, Anime.id == AnimeMetadata.animeId)
        .where(Anime.type == 'tv_series', AnimeMetadata.tmdbId != None, AnimeMetadata.tmdbId != '')
    )
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]

async def update_anime_tmdb_group_id(session: AsyncSession, anime_id: int, group_id: str):
    await session.execute(update(AnimeMetadata).where(AnimeMetadata.animeId == anime_id).values(tmdbEpisodeGroupId=group_id))
    await session.commit()

async def update_anime_aliases_if_empty(session: AsyncSession, anime_id: int, aliases: Dict[str, Any]):
    # 修正：使用 select().where() 而不是 session.get()，因为 anime_id 不是主键
    stmt = select(AnimeAlias).where(AnimeAlias.animeId == anime_id)
    result = await session.execute(stmt)
    alias_record = result.scalar_one_or_none()

    if not alias_record: return

    if not alias_record.nameEn and aliases.get('nameEn'): alias_record.nameEn = aliases['nameEn']
    if not alias_record.nameJp and aliases.get('nameJp'): alias_record.nameJp = aliases['nameJp']
    if not alias_record.nameRomaji and aliases.get('nameRomaji'): alias_record.nameRomaji = aliases['nameRomaji']
    
    cn_aliases = aliases.get('aliases_cn', [])
    if not alias_record.aliasCn1 and len(cn_aliases) > 0: alias_record.aliasCn1 = cn_aliases[0]
    if not alias_record.aliasCn2 and len(cn_aliases) > 1: alias_record.aliasCn2 = cn_aliases[1]
    if not alias_record.aliasCn3 and len(cn_aliases) > 2: alias_record.aliasCn3 = cn_aliases[2]

    await session.flush()
    logging.info(f"为作品 ID {anime_id} 更新了别名字段。")

async def get_scheduled_tasks(session: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(
        ScheduledTask.taskId.label("taskId"),
        ScheduledTask.name.label("name"),
        ScheduledTask.jobType.label("jobType"),
        ScheduledTask.cronExpression.label("cronExpression"),
        ScheduledTask.isEnabled.label("isEnabled"),
        ScheduledTask.lastRunAt.label("lastRunAt"),
        ScheduledTask.nextRunAt.label("nextRunAt")
    ).order_by(ScheduledTask.name)
    result = await session.execute(stmt)
    return [dict(row) for row in result.mappings()]
async def check_scheduled_task_exists_by_type(session: AsyncSession, job_type: str) -> bool:
    stmt = select(ScheduledTask.taskId).where(ScheduledTask.jobType == job_type).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

async def get_scheduled_task_id_by_type(session: AsyncSession, job_type: str) -> Optional[str]:
    """获取指定类型的定时任务ID。"""
    stmt = select(ScheduledTask.taskId).where(ScheduledTask.jobType == job_type).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def get_scheduled_task(session: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
    stmt = select(
        ScheduledTask.taskId.label("taskId"), 
        ScheduledTask.name.label("name"),
        ScheduledTask.jobType.label("jobType"), 
        ScheduledTask.cronExpression.label("cronExpression"),
        ScheduledTask.isEnabled.label("isEnabled"),
        ScheduledTask.lastRunAt.label("lastRunAt"),
        ScheduledTask.nextRunAt.label("nextRunAt")
    ).where(ScheduledTask.taskId == task_id)
    result = await session.execute(stmt)
    row = result.mappings().first()
    return dict(row) if row else None

async def create_scheduled_task(session: AsyncSession, task_id: str, name: str, job_type: str, cron: str, is_enabled: bool):
    new_task = ScheduledTask(taskId=task_id, name=name, jobType=job_type, cronExpression=cron, isEnabled=is_enabled)
    session.add(new_task)
    await session.commit()

async def update_scheduled_task(session: AsyncSession, task_id: str, name: str, cron: str, is_enabled: bool):
    task = await session.get(ScheduledTask, task_id)
    if task:
        task.name = name
        task.cronExpression = cron
        task.isEnabled = is_enabled
        await session.commit()

async def delete_scheduled_task(session: AsyncSession, task_id: str):
    task = await session.get(ScheduledTask, task_id)
    if task:
        await session.delete(task)
        await session.commit()

async def update_scheduled_task_run_times(session: AsyncSession, task_id: str, last_run: Optional[datetime], next_run: Optional[datetime]):
    values_to_update = {
        "lastRunAt": last_run.replace(tzinfo=None) if last_run else None,
        "nextRunAt": next_run.replace(tzinfo=None) if next_run else None
    }
    await session.execute(update(ScheduledTask).where(ScheduledTask.taskId == task_id).values(**values_to_update))
    await session.commit()

# --- Task History ---

async def create_task_in_history(
    session: AsyncSession,
    task_id: str,
    title: str,
    status: str,
    description: str,
    scheduled_task_id: Optional[str] = None,
    unique_key: Optional[str] = None
):
    now = get_now()
    new_task = TaskHistory(
        taskId=task_id, title=title, status=status, 
        description=description, scheduledTaskId=scheduled_task_id,
        createdAt=now, # type: ignore
        uniqueKey=unique_key,
        updatedAt=now
    )
    session.add(new_task)
    await session.commit()

async def update_task_progress_in_history(session: AsyncSession, task_id: str, status: str, progress: int, description: str):
    await session.execute(
        update(TaskHistory)
        .where(TaskHistory.taskId == task_id)
        .values(status=status, progress=progress, description=description, updatedAt=get_now())
    )
    await session.commit()

async def finalize_task_in_history(session: AsyncSession, task_id: str, status: str, description: str):
    await session.execute(
        update(TaskHistory)
        .where(TaskHistory.taskId == task_id)
        .values(status=status, description=description, progress=100, finishedAt=get_now(), updatedAt=get_now())
    )
    await session.commit()

async def update_task_status(session: AsyncSession, task_id: str, status: str):
    await session.execute(update(TaskHistory).where(TaskHistory.taskId == task_id).values(status=status, updatedAt=get_now().replace(tzinfo=None)))
    await session.commit()

async def get_tasks_from_history(session: AsyncSession, search_term: Optional[str], status_filter: str, page: int, page_size: int) -> Dict[str, Any]:
    # 修正：显式选择需要的列，以避免在旧的数据库模式上查询不存在的列（如 scheduled_task_id）
    base_stmt = select(
        TaskHistory.taskId,
        TaskHistory.title,
        TaskHistory.status,
        TaskHistory.progress,
        TaskHistory.description,
        TaskHistory.createdAt
    )
    
    if search_term:
        base_stmt = base_stmt.where(TaskHistory.title.like(f"%{search_term}%"))
    if status_filter == 'in_progress':
        base_stmt = base_stmt.where(TaskHistory.status.in_(['排队中', '运行中', '已暂停']))
    elif status_filter == 'completed':
        base_stmt = base_stmt.where(TaskHistory.status == '已完成')

    count_stmt = select(func.count()).select_from(base_stmt.alias("count_subquery"))
    total_count = (await session.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    data_stmt = base_stmt.order_by(TaskHistory.createdAt.desc()).offset(offset).limit(page_size)
    
    result = await session.execute(data_stmt)
    items = [
        {"taskId": row.taskId, "title": row.title, "status": row.status, "progress": row.progress, "description": row.description, "createdAt": row.createdAt}
        for row in result.mappings()
    ]
    return {"total": total_count, "list": items}

async def get_task_details_from_history(session: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
    """获取单个任务的详细信息。"""
    task = await session.get(TaskHistory, task_id)
    if task:
        return {
            "taskId": task.taskId,
            "title": task.title,
            "status": task.status,
            "progress": task.progress,
            "description": task.description,
            "createdAt": task.createdAt,
        }
    return None

async def get_task_from_history_by_id(session: AsyncSession, task_id: str) -> Optional[Dict[str, Any]]:
    task = await session.get(TaskHistory, task_id)
    if task:
        return {"taskId": task.taskId, "title": task.title, "status": task.status}
    return None

async def delete_task_from_history(session: AsyncSession, task_id: str) -> bool:
    task = await session.get(TaskHistory, task_id)
    if task:
        await session.delete(task)
        await session.commit()
        return True
    return False

async def get_execution_task_id_from_scheduler_task(session: AsyncSession, scheduler_task_id: str) -> Optional[str]:
    """
    从一个调度任务的最终描述中，解析并返回其触发的执行任务ID。
    """
    stmt = select(TaskHistory.description).where(
        TaskHistory.taskId == scheduler_task_id,
        TaskHistory.status == '已完成'
    )
    result = await session.execute(stmt)
    description = result.scalar_one_or_none()
    if description:
        match = re.search(r'执行任务ID:\s*([a-f0-9\-]+)', description)
        if match:
            return match.group(1)
    return None

async def mark_interrupted_tasks_as_failed(session: AsyncSession) -> int:
    stmt = (
        update(TaskHistory)
        .where(TaskHistory.status.in_(['运行中', '已暂停']))
        .values(status='失败', description='因程序重启而中断', finishedAt=get_now(), updatedAt=get_now()) # finishedAt and updatedAt are explicitly set here
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount

# --- Webhook Tasks ---

async def create_webhook_task(
    session: AsyncSession,
    task_title: str,
    unique_key: str,
    payload: Dict[str, Any],
    webhook_source: str,
    is_delayed: bool,
    delay: timedelta
):
    """创建一个新的待处理 Webhook 任务。"""
    now = get_now()
    execute_time = now + delay if is_delayed else now

    try:
        new_task = WebhookTask(
            receptionTime=now,
            executeTime=execute_time,
            webhookSource=webhook_source,
            status="pending",
            payload=json.dumps(payload, ensure_ascii=False),
            uniqueKey=unique_key,
            taskTitle=task_title
        )
        session.add(new_task)
        await session.flush() # Flush to check for unique constraint violation
    except exc.IntegrityError:
        # 如果 uniqueKey 已存在，则忽略此重复请求
        await session.rollback()
        logger.warning(f"检测到重复的 Webhook 请求 (unique_key: {unique_key})，已忽略。")

async def get_webhook_tasks(session: AsyncSession, page: int, page_size: int, search: Optional[str] = None) -> Dict[str, Any]:
    """获取待处理的 Webhook 任务列表，支持分页。"""
    base_stmt = select(WebhookTask)
    if search:
        base_stmt = base_stmt.where(WebhookTask.taskTitle.like(f"%{search}%"))

    count_stmt = select(func.count()).select_from(base_stmt.alias("count_subquery"))
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = base_stmt.order_by(WebhookTask.receptionTime.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return {"total": total, "list": result.scalars().all()}

async def delete_webhook_tasks(session: AsyncSession, task_ids: List[int]) -> int:
    """批量删除指定的 Webhook 任务。"""
    if not task_ids:
        return 0
    stmt = delete(WebhookTask).where(WebhookTask.id.in_(task_ids))
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount

async def get_due_webhook_tasks(session: AsyncSession) -> List[WebhookTask]:
    """获取所有已到执行时间的待处理任务。"""
    now = get_now()
    stmt = select(WebhookTask).where(WebhookTask.status == "pending", WebhookTask.executeTime <= now)
    result = await session.execute(stmt)
    return result.scalars().all()

async def update_webhook_task_status(session: AsyncSession, task_id: int, status: str):
    """更新 Webhook 任务的状态。"""
    await session.execute(update(WebhookTask).where(WebhookTask.id == task_id).values(status=status))

async def get_last_run_result_for_scheduled_task(session: AsyncSession, scheduled_task_id: str) -> Optional[Dict[str, Any]]:
    """获取指定定时任务的最近一次运行结果。"""
    stmt = (
        select(TaskHistory)
        .where(TaskHistory.scheduledTaskId == scheduled_task_id)
        .order_by(TaskHistory.createdAt.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    task_run = result.scalar_one_or_none()
    if not task_run:
        return None
    
    # 返回一个与 models.TaskInfo 兼容的字典
    return {
        "taskId": task_run.taskId,
        "title": task_run.title,
        "status": task_run.status,
        "progress": task_run.progress,
        "description": task_run.description,
        "createdAt": task_run.createdAt,
        "updatedAt": task_run.updatedAt,
        "finishedAt": task_run.finishedAt,
    }

# --- External API Logging ---

async def create_external_api_log(session: AsyncSession, ip_address: str, endpoint: str, status_code: int, message: Optional[str] = None):
    """创建一个外部API访问日志。"""
    new_log = ExternalApiLog(
        accessTime=get_now(),
        ipAddress=ip_address,
        endpoint=endpoint,
        statusCode=status_code,
        message=message
    )
    session.add(new_log)
    await session.commit()

async def get_external_api_logs(session: AsyncSession, limit: int = 100) -> List[ExternalApiLog]:
    stmt = select(ExternalApiLog).order_by(ExternalApiLog.accessTime.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

async def initialize_configs(session: AsyncSession, defaults: Dict[str, tuple[Any, str]]):
    if not defaults: return
    
    existing_stmt = select(Config.configKey)
    existing_keys = set((await session.execute(existing_stmt)).scalars().all())
    
    new_configs = [
        Config(configKey=key, configValue=str(value), description=description)
        for key, (value, description) in defaults.items()
        if key not in existing_keys
    ]
    if new_configs:
        session.add_all(new_configs)
        await session.commit()
        logging.getLogger(__name__).info(f"成功初始化 {len(new_configs)} 个新配置项。")
    logging.getLogger(__name__).info("默认配置检查完成。")

# --- Rate Limiter CRUD ---

async def find_recent_task_by_unique_key(session: AsyncSession, unique_key: str, within_hours: int) -> Optional[TaskHistory]:
    """
    Finds a task by its unique_key that is either currently active 
    or was completed within the specified time window.
    """
    if not unique_key:
        return None

    cutoff_time = get_now() - timedelta(hours=within_hours)
    
    stmt = (
        select(TaskHistory)
        .where(
            TaskHistory.uniqueKey == unique_key,
            or_(
                TaskHistory.status.in_(['排队中', '运行中', '已暂停']),
                TaskHistory.finishedAt >= cutoff_time
            )
        )
        .order_by(TaskHistory.createdAt.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def get_or_create_rate_limit_state(session: AsyncSession, provider_name: str) -> RateLimitState:
    """获取或创建特定提供商的速率限制状态。"""
    stmt = select(RateLimitState).where(RateLimitState.providerName == provider_name)
    result = await session.execute(stmt)
    state = result.scalar_one_or_none()
    if not state:
        state = RateLimitState(
            providerName=provider_name,
            requestCount=0,
            lastResetTime=get_now() # lastResetTime is explicitly set here
        )
        session.add(state)
        await session.flush()

    # 关键修复：无论数据来自数据库还是新创建，都确保返回的时间是 naive 的。
    # 这可以解决 PostgreSQL 驱动返回带时区时间对象的问题。
    if state.lastResetTime and state.lastResetTime.tzinfo:
        state.lastResetTime = state.lastResetTime.replace(tzinfo=None)

    return state

async def get_all_rate_limit_states(session: AsyncSession) -> List[RateLimitState]:
    """获取所有速率限制状态。"""
    result = await session.execute(select(RateLimitState))
    states = result.scalars().all()
    return states

async def reset_all_rate_limit_states(session: AsyncSession):
    """
    重置所有速率限制状态的请求计数和重置时间。
    """
    # 修正：从批量更新改为获取并更新对象。
    # 这确保了会话中已加载的ORM对象的状态能与数据库同步，
    # 解决了在 expire_on_commit=False 的情况下，对象状态陈旧的问题。
    states = (await session.execute(select(RateLimitState))).scalars().all()
    now_naive = get_now()
    for state in states:
        state.requestCount = 0
        state.lastResetTime = now_naive
    # The commit will be handled by the calling function (e.g., RateLimiter.check)

async def increment_rate_limit_count(session: AsyncSession, provider_name: str):
    """为指定的提供商增加请求计数。如果状态不存在，则会创建它。"""
    state = await get_or_create_rate_limit_state(session, provider_name)
    state.requestCount += 1

# --- Database Maintenance ---

async def prune_logs(session: AsyncSession, model: type[DeclarativeBase], date_column: ColumnElement, cutoff_date: datetime) -> int:
    """通用函数，用于删除指定模型中早于截止日期的记录。"""
    stmt = delete(model).where(date_column < cutoff_date)
    result = await session.execute(stmt)
    # 提交由调用方（任务）处理
    return result.rowcount

async def add_comments_from_xml(session: AsyncSession, episode_id: int, xml_content: str) -> int:
    """
    Parses XML content and adds the comments to a given episode.
    Returns the number of comments added.
    """
    comments = parse_dandan_xml_to_comments(xml_content)
    if not comments:
        return 0
    
    added_count = await save_danmaku_for_episode(session, episode_id, comments)
    await session.commit()
    
    return added_count
