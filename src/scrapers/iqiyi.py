import asyncio
import logging
import re
import json
import hashlib
import time
from datetime import datetime, timezone
from typing import ClassVar
import zlib
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Callable
from collections import defaultdict
import httpx
from urllib.parse import urlencode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pydantic import BaseModel, Field, ValidationError, model_validator, ConfigDict, field_validator

import chardet
from typing import List, Optional
from lxml import etree

from ..config_manager import ConfigManager
from .. import models
from ..utils import parse_search_keyword
from .base import BaseScraper, get_season_from_title

scraper_responses_logger = logging.getLogger("scraper_responses")

# --- Pydantic Models for iQiyi Mobile Search API ---

class IqiyiVideoLibMeta(BaseModel):
    douban_id: Optional[int] = Field(None, alias="douban_id")
    filmtv_update_strategy: Optional[str] = Field(None, alias="filmtv_update_strategy")

class IqiyiSearchVideoInfo(BaseModel):
    item_link: str = Field(alias="itemLink")

class IqiyiSearchAlbumInfo(BaseModel):
    album_id: Optional[int] = Field(None, alias="albumId")
    item_total_number: Optional[int] = Field(None, alias="itemTotalNumber")
    site_id: Optional[str] = Field(None, alias="siteId")
    album_link: Optional[str] = Field(None, alias="albumLink")
    video_doc_type: int = Field(alias="videoDocType")
    album_title: Optional[str] = Field(None, alias="albumTitle")
    channel: Optional[str] = None
    release_date: Optional[str] = Field(None, alias="releaseDate")
    album_img: Optional[str] = Field(None, alias="albumImg")
    video_lib_meta: Optional[IqiyiVideoLibMeta] = Field(None, alias="video_lib_meta")
    videoinfos: Optional[List[IqiyiSearchVideoInfo]] = None

    @property
    def link_id(self) -> Optional[str]:
        # 优先使用分集链接，然后是专辑链接
        link_to_parse = None
        if self.videoinfos and self.videoinfos[0].item_link:
            link_to_parse = self.videoinfos[0].item_link
        elif self.album_link:
            link_to_parse = self.album_link

        if not link_to_parse:
            return None
        match = re.search(r"v_(\w+?)\.html", link_to_parse)
        return match.group(1).strip() if match else None

    @property
    def year(self) -> Optional[int]:
        if self.release_date and len(self.release_date) >= 4:
            try:
                return int(self.release_date[:4])
            except ValueError:
                return None
        return None

class IqiyiAlbumDoc(BaseModel):
    score: float
    album_doc_info: IqiyiSearchAlbumInfo = Field(alias="albumDocInfo")

class IqiyiSearchDoc(BaseModel):
    docinfos: List[IqiyiAlbumDoc]

class IqiyiSearchResult(BaseModel):
    data: IqiyiSearchDoc

# --- Pydantic Models for iQiyi Desktop Search API (New) ---

class IqiyiDesktopSearchVideo(BaseModel):
    title: str
    pageUrl: Optional[str] = None
    playUrl: Optional[str] = None

class IqiyiDesktopSearchAlbumInfo(BaseModel):
    qipuId: Optional[str] = None
    playQipuId: Optional[str] = None
    subscriptContent: Optional[str] = None
    title: Optional[str] = None
    channel: Optional[str] = None
    pageUrl: Optional[str] = None
    playUrl: Optional[str] = None
    img: Optional[str] = None
    imgH: Optional[str] = None
    btnText: Optional[str] = None
    videos: Optional[List[IqiyiDesktopSearchVideo]] = None
    year: Optional[Dict[str, Any]] = None
    actors: Optional[Dict[str, Any]] = None
    directors: Optional[Dict[str, Any]] = None

    @field_validator('qipuId', 'playQipuId', mode='before')
    @classmethod
    def coerce_qipu_ids_to_string(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

    @property
    def link_id(self) -> Optional[str]:
        url_to_parse = self.pageUrl or self.playUrl
        if not url_to_parse:
            return None
        match = re.search(r"v_(\w+?)\.html", url_to_parse)
        return match.group(1).strip() if match else None

class IqiyiDesktopSearchTemplate(BaseModel):
    template: int
    albumInfo: Optional[IqiyiDesktopSearchAlbumInfo] = None
    intentAlbumInfos: Optional[List[IqiyiDesktopSearchAlbumInfo]] = None
    intentName: Optional[str] = None

class IqiyiDesktopSearchData(BaseModel):
    templates: List[IqiyiDesktopSearchTemplate] = []

class IqiyiV3EpisodeItem(BaseModel):
    tv_id: int = Field(alias="tv_id")
    name: str
    order: int
    play_url: str = Field(alias="play_url")

class IqiyiV3BaseData(BaseModel):
    model_config = ConfigDict(extra='ignore') # 忽略API返回的未知字段
    qipu_id: Optional[int] = None
    current_video_tvid: Optional[int] = None
    title: Optional[str] = None
    # video_list is not here, it's in the template
    channel_id: Optional[int] = None
    current_video_year: Optional[int] = None
    image_url: Optional[str] = None
    play_url: Optional[str] = None

class IqiyiV3ResponseData(BaseModel):
    base_data: IqiyiV3BaseData
    template: Optional[Dict[str, Any]] = None # Parse template as a raw dict

class IqiyiV3ApiResponse(BaseModel):
    status_code: int
    data: Optional[IqiyiV3ResponseData] = None

class IqiyiDesktopSearchResult(BaseModel):
    data: Optional[IqiyiDesktopSearchData] = None

class IqiyiHtmlAlbumInfo(BaseModel):
    video_count: Optional[int] = Field(None, alias="videoCount")

class IqiyiLegacyVideoInfo(BaseModel):
    # 新增：允许模型通过字段名或别名进行填充，以兼容新旧缓存格式
    model_config = ConfigDict(populate_by_name=True)

    album_id: int = Field(alias="albumId")
    tv_id: Optional[int] = Field(None, alias="tvId")
    video_id: Optional[int] = Field(None, alias="videoId")
    # 修正：新API返回的字段名不同
    video_name: str = Field(alias="name")
    video_url: str = Field(alias="playUrl")
    channel_name: Optional[str] = Field(None, alias="channelName")
    duration: int = Field(alias="durationSec")
    # video_count 不再从此模型获取，但保留字段以兼容旧缓存
    video_count: int = 0 

    @model_validator(mode='after')
    def merge_ids(self) -> 'IqiyiLegacyVideoInfo':
        if self.tv_id is None and self.video_id is not None:
            self.tv_id = self.video_id
        return self

class IqiyiEpisodeInfo(BaseModel):
    tv_id: int = Field(alias="tvId")
    name: str
    order: int
    play_url: str = Field(alias="playUrl")

    @property
    def link_id(self) -> Optional[str]:
        match = re.search(r"v_(\w+?)\.html", self.play_url)
        return match.group(1).strip() if match else None

class IqiyiVideoData(BaseModel):
    epsodelist: List[IqiyiEpisodeInfo]

class IqiyiVideoResult(BaseModel):
    data: IqiyiVideoData

class IqiyiUserInfo(BaseModel):
    uid: str

class IqiyiComment(BaseModel):
    content_id: str = Field(alias="contentId")
    content: str
    show_time: int = Field(alias="showTime")
    color: str
    # user_info 字段在XML中可能不存在，设为可选
    user_info: Optional[IqiyiUserInfo] = Field(None, alias="userInfo")

# --- 新增：用于综艺节目分集获取的模型 ---
class IqiyiAlbumVideoInfo(BaseModel):
    publish_time: int = Field(alias="publishTime")

class IqiyiAlbumBaseInfoData(BaseModel):
    first_video: IqiyiAlbumVideoInfo = Field(alias="firstVideo")
    latest_video: IqiyiAlbumVideoInfo = Field(alias="latestVideo")

class IqiyiAlbumBaseInfoResult(BaseModel):
    data: IqiyiAlbumBaseInfoData

class IqiyiMobileVideo(BaseModel):
    id: int
    vid: str
    short_title: str = Field(alias="shortTitle")
    page_url: str = Field(alias="pageUrl")
    publish_time: int = Field(alias="publishTime")
    duration: str

    @property
    def link_id(self) -> Optional[str]:
        match = re.search(r"v_(\w+?)\.html", self.page_url)
        return match.group(1).strip() if match else None

class IqiyiMobileVideoListData(BaseModel):
    videos: List[IqiyiMobileVideo]

class IqiyiMobileVideoListResult(BaseModel):
    data: IqiyiMobileVideoListData

# --- Main Scraper Class ---

class IqiyiScraper(BaseScraper):
    provider_name = "iqiyi"
    handled_domains = ["www.iqiyi.com"]
    referer = "https://www.iqiyi.com/"
    test_url = "https://www.iqiyi.com"
    _PROVIDER_SPECIFIC_BLACKLIST_DEFAULT = r"^(.*?)(抢先(看|版)?|加更(版)?|花絮|预告|特辑|彩蛋|专访|幕后|直播|纯享|未播|衍生|番外|会员(专享|加长)?|片花|预告|精华|看点|速看|解读|reaction|影评)(.*?)$"
    
    # --- 新增：用于新API的签名和ID转换 ---
    _xor_key: ClassVar[int] = 0x75706971676c
    _secret_key: ClassVar[str] = "howcuteitis"
    _key_name: ClassVar[str] = "secret_key"
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager):
        super().__init__(session_factory, config_manager)
        self.mobile_user_agent = "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36 Edg/136.0.0.0"
        self.reg_video_info = re.compile(r'"videoInfo":(\{.+?\}),')
        self.cookies = {"pgv_pvid": "40b67e3b06027f3d","video_platform": "2","vversion_name": "8.2.95","video_bucketid": "4","video_omgid": "0a1ff6bc9407c0b1cff86ee5d359614d"}
        # 实体引用匹配正则
        self.client: Optional[httpx.AsyncClient] = None
        self.entity_pattern = re.compile(r'&#[xX]?[0-9a-fA-F]+;')

        # XML 1.0规范允许的字符编码范围
        self.valid_codes = set(
            [0x09, 0x0A, 0x0D] +  # 制表符、换行、回车
            list(range(0x20, 0x7E + 1)) +  # 可打印ASCII
            list(range(0x80, 0xFF + 1)) +  # 扩展ASCII
            list(range(0x100, 0xD7FF + 1)) +
            list(range(0xE000, 0xFDCF + 1)) +
            list(range(0xFDE0, 0xFFFD + 1))
         )

    async def _ensure_client(self):
        """Ensures the httpx client is initialized, with proxy support."""
        """确保 httpx 客户端已初始化，并支持代理。"""
        # 检查代理配置是否发生变化
        new_proxy_config = await self._get_proxy_for_provider()
        if self.client and new_proxy_config != self._current_proxy_config:
            self.logger.info("iQiyi: 代理配置已更改，正在重建HTTP客户端...")
            await self.client.aclose()
            self.client = None

        if self.client is None:
            headers = {
                "User-Agent": self.mobile_user_agent,
                "Referer": "https://www.iqiyi.com/",
            }
            # 修正：使用基类中的 _create_client 方法来创建客户端，以支持代理
            self.client = await self._create_client(
                headers=headers, cookies=self.cookies, timeout=30.0, follow_redirects=True
            )
        
        return self.client

    async def get_episode_blacklist_pattern(self) -> Optional[re.Pattern]:
        """
        获取并编译用于过滤分集的正则表达式。
        此方法现在只使用数据库中配置的规则，如果规则为空，则不进行过滤。
        """
        # 1. 构造该源特定的配置键，确保与数据库键名一致
        provider_blacklist_key = f"{self.provider_name}_episode_blacklist_regex"
        
        # 2. 从数据库动态获取用户自定义规则
        custom_blacklist_str = await self.config_manager.get(provider_blacklist_key)

        # 3. 仅当用户配置了非空的规则时才进行过滤
        if custom_blacklist_str and custom_blacklist_str.strip():
            self.logger.info(f"正在为 '{self.provider_name}' 使用数据库中的自定义分集黑名单。")
            try:
                return re.compile(custom_blacklist_str, re.IGNORECASE)
            except re.error as e:
                self.logger.error(f"编译 '{self.provider_name}' 的分集黑名单时出错: {e}。规则: '{custom_blacklist_str}'")
        
        # 4. 如果规则为空或未配置，则不进行过滤
        return None

    async def close(self):
        """关闭HTTP客户端"""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _xor_operation(self, num: int) -> int:
        """实现JavaScript中的异或运算函数"""
        num_binary = bin(num)[2:]
        key_binary = bin(self._xor_key)[2:]
        num_bits = list(num_binary[::-1])
        key_bits = list(key_binary[::-1])
        result_bits = []
        max_len = max(len(num_bits), len(key_bits))
        for i in range(max_len):
            num_bit = num_bits[i] if i < len(num_bits) else '0'
            key_bit = key_bits[i] if i < len(key_bits) else '0'
            if num_bit == '1' and key_bit == '1':
                result_bits.append('0')
            elif num_bit == '1' or key_bit == '1':
                result_bits.append('1')
            else:
                result_bits.append('0')
        result_binary = ''.join(result_bits[::-1])
        return int(result_binary, 2) if result_binary else 0
    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response: # type: ignore
        client = await self._ensure_client()
        return await client.request(method, url, **kwargs)

    def _video_id_to_entity_id(self, video_id: str) -> Optional[str]:
        """将视频ID (v_...中的部分) 转换为entity_id"""
        try:
            base36_decoded = int(video_id, 36)
            xor_result = self._xor_operation(base36_decoded)
            if xor_result < 900000:
                final_result = 100 * (xor_result + 900000)
            else:
                final_result = xor_result
            return str(final_result)
        except Exception as e:
            self.logger.error(f"将 video_id '{video_id}' 转换为 entity_id 时出错: {e}")
            return None

    def _create_sign(self, params: Dict[str, Any]) -> str:
        """为新API生成签名"""
        clean_params = {k: v for k, v in params.items() if k != 'sign'}
        sorted_keys = sorted(clean_params.keys())
        param_parts = []
        for key in sorted_keys:
            value = clean_params[key]
            if value is None:
                value = ""
            param_parts.append(f"{key}={value}")
        
        param_string = "&".join(param_parts)
        sign_string = f"{param_string}&{self._key_name}={self._secret_key}"
        md5_hash = hashlib.md5(sign_string.encode('utf-8')).hexdigest().upper()
        
        return md5_hash

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        await self._ensure_client()
        assert self.client is not None
        return await self.client.request(method, url, **kwargs)

    async def _request_with_retry(self, method: str, url: str, retries: int = 3, **kwargs) -> httpx.Response:
        """
        一个带有重试逻辑的请求包装器，用于处理网络超时等临时性错误。
        """
        last_exception = None
        for attempt in range(retries):
            try:
                response = await self._request(method, url, **kwargs)
                response.raise_for_status()  # 检查 4xx/5xx 错误
                return response
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
                last_exception = e
                self.logger.warning(f"爱奇艺: 请求失败 (尝试 {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    backoff_time = 2 ** attempt  # 1s, 2s, 4s...
                    self.logger.info(f"爱奇艺: 将在 {backoff_time} 秒后重试...")
                    await asyncio.sleep(backoff_time)
            except httpx.HTTPStatusError as e:
                # 对于客户端/服务器错误，通常不重试
                self.logger.error(f"爱奇艺: HTTP 状态错误: {e.response.status_code} for URL {e.request.url}")
                raise  # 立即重新抛出
        
        # 如果所有重试都失败，则抛出最后的异常
        if last_exception:
            raise last_exception
        raise Exception("请求在所有重试后失败，但没有捕获到特定的网络异常。")

    def _filter_entities(self, xml_str: str) -> str:
        """过滤XML中的无效实体引用和字符"""
        original_len = len(xml_str)

        def replace(match):
            entity = match.group()
            try:
                if entity.startswith('&#x') or entity.startswith('&#X'):
                    code = int(entity[3:-1], 16)
                else:
                    code = int(entity[2:-1])
                return entity if code in self.valid_codes else ''
            except:
                return ''

        xml_str = self.entity_pattern.sub(replace, xml_str)
        xml_str = re.sub(
            r'[^\x09\x0A\x0D\x20-\x7E\x80-\xFF\u0100-\uD7FF\uE000-\uFDCF\uFDE0-\uFFFD]',
            '',
            xml_str
        )

        self.logger.debug(f"过滤前后长度变化: {original_len} → {len(xml_str)} (减少 {original_len - len(xml_str)})")
        return xml_str

    def _log_error_context(self, xml_str: str, line: int, col: int):
        """打印错误位置附近的XML内容"""
        lines = xml_str.split('\n')
        start = max(0, line - 3)
        end = min(len(lines), line + 3)

        self.logger.error(f"错误位置上下文（行{line}，列{col}）:")
        for i in range(start, end):
            prefix = "→ " if i == line - 1 else "  "
            self.logger.error(f"{prefix}行{i + 1}: {lines[i][:100]}...")

    async def _search_desktop_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """使用桌面版API进行搜索 (主API)"""
        self.logger.info(f"爱奇艺 (桌面API): 正在搜索 '{keyword}'...")
        url = "https://mesh.if.iqiyi.com/portal/lw/search/homePageV3"
        params = {
            'key': keyword, 'current_page': '1', 'mode': '1', 'source': 'input',
            'suggest': '', 'pcv': '13.074.22699', 'version': '13.074.22699',
            'pageNum': '1', 'pageSize': '25', 'pu': '', 'u': 'f6440fc5d919dca1aea12b6aff56e1c7',
            'scale': '200', 'token': '', 'userVip': '0', 'conduit': '', 'vipType': '-1',
            'os': '', 'osShortName': 'win10', 'dataType': '', 'appMode': '',
            'ad': json.dumps({"lm":3,"azd":1000000000951,"azt":733,"position":"feed"}),
            'adExt': json.dumps({"r":"2.1.5-ares6-pure"})
        }
        headers = {
            'accept': '*/*', 'origin': 'https://www.iqiyi.com', 'referer': 'https://www.iqiyi.com/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        results = []
        try:
            response = await self._request("GET", url, params=params, headers=headers)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi Desktop Search Response (keyword='{keyword}'): {response.text}")
            response.raise_for_status()
            data = IqiyiDesktopSearchResult.model_validate(response.json())

            if not data.data or not data.data.templates:
                return []

            albums_to_process = []
            for template in data.data.templates:
                # 优先处理意图卡片 (template 112)
                if template.template == 112 and template.intentAlbumInfos:
                    self.logger.debug(f"爱奇艺 (桌面API): 找到意图卡片 (template 112)，处理 {len(template.intentAlbumInfos)} 个结果。")
                    albums_to_process.extend(template.intentAlbumInfos)
                # 然后处理普通结果卡片
                elif template.template in [101, 102, 103] and template.albumInfo:
                    self.logger.debug(f"爱奇艺 (桌面API): 找到普通结果卡片 (template {template.template})。")
                    albums_to_process.append(template.albumInfo)

            for album in albums_to_process:
                if not album.title or not album.link_id:
                    continue

                if self._GLOBAL_SEARCH_JUNK_TITLE_PATTERN.search(album.title):
                    self.logger.debug(f"爱奇艺 (桌面API): 根据标题黑名单过滤掉 '{album.title}'")
                    continue
                
                if album.btnText == '外站付费播放':
                    self.logger.debug(f"爱奇艺 (桌面API): 过滤掉外站付费播放内容 '{album.title}'")
                    continue

                channel = album.channel or ""
                if "电影" in channel: media_type = "movie"
                elif "电视剧" in channel or "动漫" in channel: media_type = "tv_series"
                else: continue # 只保留电影、电视剧、动漫

                year_str = (album.year or {}).get("value") or (album.year or {}).get("name")
                year = int(year_str) if isinstance(year_str, str) and year_str.isdigit() and len(year_str) == 4 else None

                episode_count = len(album.videos) if album.videos else None
                if album.subscriptContent:
                    # 修正：使用更精确的正则表达式来解析总集数，避免将日期误认为集数。
                    # 此正则寻找 "更新至XX集/期" 或 "全XX集/期" 格式。
                    count_match = re.search(r'(?:更新至|全|共)\s*(\d+)\s*(?:集|话|期)', album.subscriptContent)
                    if count_match:
                        episode_count = int(count_match.group(1))
                    else:
                        # 如果不匹配特定格式，但内容只包含数字，则可能是总集数
                        simple_count_match = re.fullmatch(r'(\d+)', album.subscriptContent.strip())
                        if simple_count_match:
                            episode_count = int(simple_count_match.group(1))
                cleaned_title = re.sub(r'<[^>]+>', '', album.title).replace(":", "：")
                
                provider_search_info = models.ProviderSearchInfo(
                    provider=self.provider_name,
                    mediaId=album.link_id,
                    title=cleaned_title,
                    type=media_type,
                    season=get_season_from_title(cleaned_title),
                    year=year,
                    imageUrl=album.img or album.imgH, # Use the correct image field
                    episodeCount=episode_count,
                    currentEpisodeIndex=episode_info.get("episode") if episode_info else None,
                )
                results.append(provider_search_info)
        except Exception as e:
            self.logger.error(f"爱奇艺 (桌面API): 搜索 '{keyword}' 失败: {e}", exc_info=True)
        
        return results

    async def search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """
        Performs a cached search for iQiyi content.
        It caches the base results for a title and then filters them based on season.
        """
        parsed = parse_search_keyword(keyword)
        search_title = parsed['title']
        search_season = parsed['season']

        cache_key = f"search_base_{self.provider_name}_{search_title}"
        cached_results = await self._get_from_cache(cache_key)

        if cached_results:
            self.logger.info(f"爱奇艺: 从缓存中命中基础搜索结果 (title='{search_title}')")
            all_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results]
            # 关键修复：当从缓存加载时，必须使用当前请求的集数更新结果。
            # 之前的逻辑会返回缓存中旧的集数，导致此BUG。
            current_episode_from_request = episode_info.get("episode") if episode_info else None
            if current_episode_from_request is not None:
                for result in all_results:
                    result.currentEpisodeIndex = current_episode_from_request
        else:
            self.logger.info(f"爱奇艺: 缓存未命中，正在为标题 '{search_title}' 执行网络搜索...")
            all_results = await self._perform_network_search(search_title, episode_info)
            if all_results:
                await self._set_to_cache(cache_key, [r.model_dump() for r in all_results], 'search_ttl_seconds', 3600)

        if search_season is None:
            return all_results

        # Filter results by season
        final_results = [item for item in all_results if item.season == search_season]
        self.logger.info(f"爱奇艺: 为 S{search_season} 过滤后，剩下 {len(final_results)} 个结果。")
        return final_results

    async def _perform_network_search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """Performs the actual network search for iQiyi."""
        # 并行执行两个搜索API
        desktop_task = self._search_desktop_api(keyword, episode_info)
        mobile_task = self._search_mobile_api(keyword, episode_info)
        
        results_lists = await asyncio.gather(desktop_task, mobile_task, return_exceptions=True)
        
        all_results = []
        for i, res_list in enumerate(results_lists):
            api_name = "桌面API" if i == 0 else "移动API"
            if isinstance(res_list, list):
                all_results.extend(res_list)
            elif isinstance(res_list, Exception):
                # 修正：对常见的网络错误只记录警告，避免在日志中产生大量堆栈跟踪。
                if isinstance(res_list, (httpx.TimeoutException, httpx.ConnectError)):
                    self.logger.warning(f"爱奇艺 ({api_name}): 搜索时连接超时或网络错误: {res_list}")
                else:
                    # 对于其他意外错误，仍然记录完整的堆栈跟踪以供调试。
                    self.logger.error(f"爱奇艺 ({api_name}): 搜索子任务失败: {res_list}", exc_info=True)

        # 基于 mediaId 去重
        unique_results = list({item.mediaId: item for item in all_results}.values())

        self.logger.info(f"爱奇艺 (合并): 网络搜索 '{keyword}' 完成，找到 {len(unique_results)} 个唯一结果。")
        if unique_results:
            log_results = "\n".join([f"  - {r.title} (ID: {r.mediaId}, 类型: {r.type}, 年份: {r.year or 'N/A'})" for r in unique_results])
            self.logger.info(f"爱奇艺 (合并): 网络搜索结果列表:\n{log_results}")

        return unique_results

    async def _search_mobile_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """使用移动版API进行搜索 (备用API)"""
        self.logger.info(f"爱奇艺 (移动API): 正在搜索 '{keyword}'...")
        url = f"https://search.video.iqiyi.com/o?if=html5&key={keyword}&pageNum=1&pageSize=20"
        results = []
        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi Search Response (keyword='{keyword}'): {response.text}")
            response.raise_for_status()
            data = IqiyiSearchResult.model_validate(response.json())

            if not data.data or not data.data.docinfos:
                return []

            for doc in data.data.docinfos:
                if doc.score < 0.7: continue
                
                album = doc.album_doc_info
                if not (album.album_link and "iqiyi.com" in album.album_link and album.site_id == "iqiyi" and album.video_doc_type == 1):
                    continue
                # 修正：增加对 album.channel 的非空检查，并添加对“纪录片”频道的过滤
                if album.channel and ("原创" in album.channel or "教育" in album.channel or "纪录片" in album.channel):
                    self.logger.debug(f"爱奇艺: 根据频道 '{album.channel}' 过滤掉 '{album.album_title}'")
                    continue

                # 新增：根据标题过滤掉非正片内容
                if album.album_title and self._GLOBAL_SEARCH_JUNK_TITLE_PATTERN.search(album.album_title):
                    self.logger.debug(f"爱奇艺: 根据标题黑名单过滤掉 '{album.album_title}'")
                    continue

                douban_id = None
                if album.video_lib_meta and album.video_lib_meta.douban_id:
                    douban_id = str(album.video_lib_meta.douban_id)

                link_id = album.link_id
                if not link_id:
                    continue

                channel_name = album.channel.split(',')[0] if album.channel else ""
                media_type = "movie" if channel_name == "电影" else "tv_series"

                episode_count = album.item_total_number
                if album.video_lib_meta and album.video_lib_meta.filmtv_update_strategy:
                    # 修正：使用更精确的正则表达式来解析总集数，避免将日期误认为集数。
                    count_match = re.search(r'(?:更新至|全|共)\s*(\d+)\s*(?:集|话|期)', album.video_lib_meta.filmtv_update_strategy)
                    if count_match:
                        episode_count = int(count_match.group(1))

                current_episode = episode_info.get("episode") if episode_info else None
                cleaned_title = re.sub(r'<[^>]+>', '', album.album_title).replace(":", "：") if album.album_title else "未知标题"
                provider_search_info = models.ProviderSearchInfo(
                    provider=self.provider_name,
                    mediaId=link_id,
                    title=cleaned_title,
                    type=media_type,
                    season=get_season_from_title(cleaned_title),
                    year=album.year,
                    imageUrl=album.album_img,
                    episodeCount=episode_count,
                    currentEpisodeIndex=current_episode,
                )
                self.logger.debug(f"爱奇艺: 创建的 ProviderSearchInfo: {provider_search_info.model_dump_json(indent=2)}")
                results.append(provider_search_info)
        except Exception as e:
            self.logger.error(f"爱奇艺 (移动API): 搜索 '{keyword}' 失败: {e}", exc_info=True)
        return results

    async def get_info_from_url(self, url: str) -> Optional[models.ProviderSearchInfo]:
        """从爱奇艺URL中提取作品信息。"""
        self.logger.info(f"爱奇艺: 正在从URL提取信息: {url}")
        link_id_match = re.search(r"v_(\w+?)\.html", url)
        if not link_id_match:
            self.logger.warning(f"爱奇艺 (get_info): 无法从URL中解析出link_id: {url}")
            return None
        
        link_id = link_id_match.group(1)

        # 方案1: 尝试新版 v3 API
        v3_info = await self._get_v3_base_info(link_id)
        if v3_info and v3_info.title:
            self.logger.info(f"爱奇艺 (get_info): v3 API 成功获取信息。")
            # channel_id mapping: 1-电影, 2-电视剧, 4-动漫
            media_type = "movie" if v3_info.channel_id == 1 else "tv_series"
            return models.ProviderSearchInfo(
                provider=self.provider_name,
                mediaId=link_id,
                title=v3_info.title,
                type=media_type,
                season=get_season_from_title(v3_info.title),
                year=v3_info.current_video_year,
                imageUrl=v3_info.image_url
            )

        self.logger.warning(f"爱奇艺 (get_info): v3 API 获取信息失败，回退到旧版 API。")
        
        # 方案2: 回退到旧版 API
        base_info = await self._get_legacy_video_base_info(link_id)
        if not base_info:
            return None

        channel = base_info.channel_name or ""
        media_type = "movie" if channel == "电影" else "tv_series"
        return models.ProviderSearchInfo(
            provider=self.provider_name,
            mediaId=link_id,
            title=base_info.video_name,
            type=media_type,
            season=get_season_from_title(base_info.video_name)
        )

    async def _get_v3_full_response(self, media_id: str) -> Optional[IqiyiV3ApiResponse]:
        """
        方案 #0: 使用新的 base_info API 获取完整的视频基础信息，并进行缓存。
        这是所有新版API数据获取的核心。
        """
        cache_key = f"v3_full_response_{media_id}"
        cached_info = await self._get_from_cache(cache_key)
        if cached_info:
            self.logger.info(f"爱奇艺 (v3): 从缓存命中 full_response (media_id={media_id})")
            try:
                return IqiyiV3ApiResponse.model_validate(cached_info)
            except ValidationError as e:
                self.logger.warning(f"爱奇艺 (v3): 缓存的 full_response (media_id={media_id}) 验证失败: {e}")

        self.logger.info(f"爱奇艺 (v3): 正在从网络获取 full_response (media_id={media_id})")
        entity_id = self._video_id_to_entity_id(media_id)
        if not entity_id:
            self.logger.warning(f"爱奇艺 (v3): 无法将 media_id '{media_id}' 转换为 entity_id。")
            return None

        params = {
            'entity_id': entity_id,
            'device_id': 'qd5fwuaj4hunxxdgzwkcqmefeb3ww5hx',
            'auth_cookie': '', 'user_id': '0', 'vip_type': '-1', 'vip_status': '0',
            'conduit_id': '', 'pcv': '13.082.22866', 'app_version': '13.082.22866',
            'ext': '', 'app_mode': 'standard', 'scale': '100',
            'timestamp': str(int(time.time() * 1000)),
            'src': 'pca_tvg', 'os': '',
            'ad_ext': '{"r":"2.2.0-ares6-pure"}'
        }
        params['sign'] = self._create_sign(params)
        url = f"https://www.iqiyi.com/prelw/tvg/v2/lw/base_info?{urlencode(params)}"

        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi BaseInfo API Response (entity_id={entity_id}): {response.text}")
            response.raise_for_status()

            parsed_response = IqiyiV3ApiResponse.model_validate(response.json())
            if parsed_response.status_code == 0:
                await self._set_to_cache(cache_key, parsed_response.model_dump(), 'base_info_ttl_seconds', 1800)
                self.logger.info(f"爱奇艺 (v3): 成功获取并缓存了 full_response (media_id={media_id})")
                return parsed_response
            else:
                self.logger.warning(f"爱奇艺 (v3): API未成功返回数据。状态码: {parsed_response.status_code}")
                return None
        except Exception as e:
            self.logger.error(f"爱奇艺 (v3): 获取 full_response 时发生错误: {e}", exc_info=True)
            return None

    async def _get_v3_base_info(self, media_id: str) -> Optional[IqiyiV3BaseData]:
        """从完整的V3响应中提取并返回基础信息部分。"""
        full_response = await self._get_v3_full_response(media_id)
        if full_response and full_response.data:
            return full_response.data.base_data
        return None

    async def _get_episodes_v3(self, media_id: str) -> List[models.ProviderEpisodeInfo]:
        """方案 #1: 使用新的 base_info API 获取分集列表。"""
        self.logger.info(f"爱奇艺: 正在尝试使用新版API (v3) 获取分集 (media_id={media_id})")
        
        v3_response = await self._get_v3_full_response(media_id)
        if not v3_response or not v3_response.data or not v3_response.data.template:
            self.logger.warning(f"爱奇艺 (v3): 未能获取到包含分集信息的 template 数据。")
            return []

        all_episodes = []
        try:
            tabs = v3_response.data.template.get("tabs", [])
            if not tabs: return []

            blocks = tabs[0].get("blocks", [])
            episode_groups = []
            for block in blocks:
                if block.get("bk_type") == "album_episodes":
                    if data := block.get("data", {}).get("data", []):
                        episode_groups.extend(data)
            
            if not episode_groups:
                self.logger.warning(f"爱奇艺 (v3): 在API响应中未找到 'album_episodes' 块。")
                return []

            for group in episode_groups:
                videos_data = group.get("videos")
                if isinstance(videos_data, str):
                    self.logger.info(f"爱奇艺 (v3): 发现分季URL，正在获取: {videos_data}")
                    try:
                        resp = await self._request("GET", videos_data)
                        videos_data = resp.json()
                    except Exception as e:
                        self.logger.error(f"爱奇艺 (v3): 获取分季数据失败: {e}")
                        continue
                
                if isinstance(videos_data, dict) and "feature_paged" in videos_data:
                    for page_key, paged_list in videos_data["feature_paged"].items():
                        for ep_data in paged_list:
                            if ep_data.get("content_type") != 1: continue
                            
                            play_url = ep_data.get("play_url", "")
                            tvid_match = re.search(r"tvid=(\d+)", play_url)
                            if not tvid_match: continue
                            
                            tvid = tvid_match.group(1)
                            title = ep_data.get("short_display_name") or ep_data.get("title", "未知分集")
                            subtitle = ep_data.get("subtitle")
                            if subtitle and subtitle not in title:
                                title = f"{title} {subtitle}"
                            
                            order = ep_data.get("album_order")
                            page_url = ep_data.get("page_url")

                            if tvid and title and order and page_url:
                                all_episodes.append(models.ProviderEpisodeInfo(
                                    provider=self.provider_name, episodeId=tvid,
                                    title=title, episodeIndex=order, url=page_url
                                ))
        except Exception as e:
            self.logger.error(f"爱奇艺 (v3): 解析分集列表时发生错误: {e}", exc_info=True)
            return []

        unique_episodes = list({ep.episodeId: ep for ep in all_episodes}.values())
        unique_episodes.sort(key=lambda x: x.episodeIndex)

        self.logger.info(f"爱奇艺 (v3): 成功获取 {len(unique_episodes)} 个分集。")
        return unique_episodes

    async def _get_tvid_from_link_id(self, link_id: str) -> Optional[str]:
        """
        新增：使用官方API将视频链接ID解码为tvid。
        这比解析HTML更可靠。
        新增：增加国内API端点作为备用，以提高连接成功率。
        新增：增加tvid缓存，减少不必要的API请求。
        """
        cache_key = f"tvid_{link_id}"
        cached_tvid = await self._get_from_cache(cache_key)
        if cached_tvid:
            self.logger.info(f"爱奇艺: 从缓存中命中 tvid (link_id={link_id})")
            return str(cached_tvid)

        endpoints = [
            f"https://pcw-api.iq.com/api/decode/{link_id}?platformId=3&modeCode=intl&langCode=sg",  # International (main)
            f"https://pcw-api.iqiyi.com/api/decode/{link_id}?platformId=3&modeCode=intl&langCode=sg" # Mainland China (fallback)
        ]

        for i, api_url in enumerate(endpoints):
            try:
                self.logger.info(f"爱奇艺: 正在尝试从端点 #{i+1} 解码 tvid (link_id: {link_id})")
                response = await self._request("GET", api_url)
                if await self._should_log_responses():
                    scraper_responses_logger.debug(f"iQiyi Decode API Response (link_id={link_id}, endpoint=#{i+1}): {response.text}")
                response.raise_for_status()
                data = response.json()
                if data.get("code") in ["A00000", "0"] and data.get("data"):
                    tvid = str(data["data"])
                    self.logger.info(f"爱奇艺: 从端点 #{i+1} 成功解码 tvid = {tvid}。")
                    # 缓存结果。tvid 相对稳定，可以使用与基础信息相同的TTL。
                    await self._set_to_cache(cache_key, tvid, 'base_info_ttl_seconds', 1800)
                    return tvid
                else:
                    self.logger.warning(f"爱奇艺: decode API (端点 #{i+1}) 未成功返回 tvid (link_id: {link_id})。响应: {data}")
                    # Don't return here, let it try the next endpoint
            except Exception as e:
                self.logger.warning(f"爱奇艺: 调用 decode API (端点 #{i+1}) 失败: {e}")
                # Don't re-raise, just continue to the next endpoint
        
        # If all endpoints fail
        self.logger.error(f"爱奇艺: 所有 decode API 端点均调用失败 (link_id: {link_id})。")
        return None

    async def _get_legacy_video_base_info(self, link_id: str) -> Optional[IqiyiLegacyVideoInfo]: # This is the old v2
        # 修正：缓存键必须包含分集信息，以区分对同一标题的不同分集搜索
        cache_key = f"base_info_{link_id}"
        cached_info = await self._get_from_cache(cache_key)
        if cached_info is not None:
            self.logger.info(f"爱奇艺: 从缓存中命中基础信息 (link_id={link_id})")
            try:
                return IqiyiLegacyVideoInfo.model_validate(cached_info)
            except ValidationError as e:
                self.logger.error(f"爱奇艺: 缓存的基础信息 (link_id={link_id}) 验证失败。这可能是一个陈旧或损坏的缓存。")
                self.logger.error(f"导致验证失败的数据: {cached_info}")
                self.logger.error(f"Pydantic 验证错误: {e}")
                return None

        # 主方案：使用API获取信息
        tvid = await self._get_tvid_from_link_id(link_id)
        if not tvid:
            return None

        url = f"https://pcw-api.iqiyi.com/video/video/baseinfo/{tvid}"
        try:
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi BaseInfo Response (tvid={tvid}): {response.text}")
            response.raise_for_status()
            data = response.json()
            if data.get("code") != "A00000" or not data.get("data"):
                self.logger.warning(f"爱奇艺: baseinfo API 未成功返回数据 (tvid: {tvid})。响应: {data}")
                return None
            
            video_info = IqiyiLegacyVideoInfo.model_validate(data["data"])

            info_to_cache = video_info.model_dump()
            await self._set_to_cache(cache_key, info_to_cache, 'base_info_ttl_seconds', 1800)
            return video_info
        except Exception as e:
            self.logger.error(f"爱奇艺: 获取或解析 baseinfo 失败 (tvid: {tvid}): {e}", exc_info=True)
            
        # 备用方案：如果API失败，则尝试解析HTML页面
        self.logger.warning(f"爱奇艺: API获取基础信息失败，正在尝试备用方案 (解析HTML)...")
        try:
            url = f"https://m.iqiyi.com/v_{link_id}.html"
            response = await self._request("GET", url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"iQiyi HTML Fallback Response (link_id={link_id}): {response.text}")
            response.raise_for_status()
            html_content = response.text
            match = self.reg_video_info.search(html_content)
            if match:
                video_json_str = match.group(1)
                video_info = IqiyiLegacyVideoInfo.model_validate(json.loads(video_json_str))
                self.logger.info(f"爱奇艺: 备用方案成功解析到视频信息 (link_id={link_id})")
                info_to_cache = video_info.model_dump()
                await self._set_to_cache(cache_key, info_to_cache, 'base_info_ttl_seconds', 1800)
                return video_info
        except Exception as fallback_e:
            self.logger.error(f"爱奇艺: 备用方案 (解析HTML) 也失败了: {fallback_e}", exc_info=True)
            return None

    async def _get_tv_episodes(self, album_id: int, page_size: int = 200) -> List[IqiyiEpisodeInfo]:
        """
        获取剧集列表，实现主/备API端点回退和分页机制。
        """
        # Base URLs for the endpoints
        base_endpoints = [
            "https://pcw-api.iq.com/api/album/album/avlistinfo",  # International (main)
            "https://pcw-api.iqiyi.com/albums/album/avlistinfo"  # Mainland China (fallback)
        ]

        for i, base_url in enumerate(base_endpoints):
            all_episodes = []
            page_num = 1
            self.logger.info(f"爱奇艺: 正在尝试从端点 #{i+1} ({base_url}) 获取剧集列表 (album_id: {album_id})")
            
            try:
                while True:
                    params = {
                        "aid": album_id,
                        "page": page_num,
                        "size": page_size
                    }
                    url = f"{base_url}?{urlencode(params)}"
                    
                    self.logger.debug(f"爱奇艺: 正在获取第 {page_num} 页...")
                    response = await self._request("GET", url)
                    
                    # 如果第一页就404，说明此端点可能不可用
                    if response.status_code == 404 and page_num == 1:
                        raise httpx.HTTPStatusError(f"端点在第一页返回404", request=response.request, response=response)
                    
                    response.raise_for_status()
                    
                    data = IqiyiVideoResult.model_validate(response.json())
                    
                    if data.data and data.data.epsodelist:
                        episodes_on_page = data.data.epsodelist
                        all_episodes.extend(episodes_on_page)
                        self.logger.debug(f"爱奇艺: 第 {page_num} 页成功获取 {len(episodes_on_page)} 个分集。")
                        
                        # 如果返回的条目数小于请求的页面大小，说明是最后一页
                        if len(episodes_on_page) < page_size:
                            break
                        page_num += 1
                    else:
                        # API未返回更多分集，结束循环
                        break
                
                if all_episodes:
                    self.logger.info(f"爱奇艺: 从端点 #{i+1} 成功获取 {len(all_episodes)} 个分集。")
                    return all_episodes
            except Exception as e:
                self.logger.error(f"爱奇艺: 尝试端点 #{i+1} 时发生错误 (album_id: {album_id}): {e}")
        
        self.logger.error(f"爱奇艺: 所有端点均未能获取到剧集列表 (album_id: {album_id})。")
        return []

    async def _get_zongyi_episodes(self, album_id: int) -> List[IqiyiEpisodeInfo]:
        """新增：专门为综艺节目获取分集列表。"""
        self.logger.info(f"爱奇艺: 检测到综艺节目 (album_id={album_id})，使用按月获取策略。")
        try:
            # 1. 获取节目的开播和最新日期
            url = f"https://pcw-api.iqiyi.com/album/album/baseinfo/{album_id}"
            response = await self._request("GET", url)
            response.raise_for_status()
            album_base_info = IqiyiAlbumBaseInfoResult.model_validate(response.json()).data
            start_date = datetime.fromtimestamp(album_base_info.first_video.publish_time / 1000)
            end_date = datetime.fromtimestamp(album_base_info.latest_video.publish_time / 1000)

            # 2. 逐月获取分集
            all_videos: List[IqiyiMobileVideo] = []
            # 标准化 current_date 为当月第一天，以进行安全的月份迭代
            current_date = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while (current_date.year, current_date.month) <= (end_date.year, end_date.month):
                year = current_date.year
                month = f"{current_date.month:02d}"
                month_url = f"https://pub.m.iqiyi.com/h5/main/videoList/source/month/?sourceId={album_id}&year={year}&month={month}"
                
                self.logger.debug(f"爱奇艺 (综艺): 正在获取 {year}-{month} 的分集...")
                month_response = await self._request("GET", month_url)
                # 如果某个月份没有数据，API可能返回404或空列表，这都是正常情况
                if month_response.status_code == 200:
                    try:
                        # 响应是 JSONP 格式，需要清理
                        jsonp_text = month_response.text
                        json_text = re.sub(r'^[^{]*\(|\)[^}]*$', '', jsonp_text)
                        month_data = IqiyiMobileVideoListResult.model_validate(json.loads(json_text))
                        if month_data.data and month_data.data.videos:
                            all_videos.extend(month_data.data.videos)
                    except (json.JSONDecodeError, ValidationError) as e:
                        self.logger.warning(f"爱奇艺 (综艺): 解析 {year}-{month} 的分集失败: {e}")
                
                # 移至下一个月
                if current_date.month == 12:
                    current_date = current_date.replace(year=current_date.year + 1, month=1)
                else:
                    current_date = current_date.replace(month=current_date.month + 1)
                await asyncio.sleep(0.3) # 礼貌性等待

            # 3. 过滤、排序并转换为标准格式
            filtered_videos = [v for v in all_videos if "精编版" not in v.short_title and "会员版" not in v.short_title]
            filtered_videos.sort(key=lambda v: v.publish_time)

            # 4. 异步获取所有分集的 tvid
            tvid_tasks = [self._get_tvid_from_link_id(v.link_id) for v in filtered_videos if v.link_id]
            tvids = await asyncio.gather(*tvid_tasks)
            
            # 5. 构建最终结果
            final_episodes = []
            for i, video in enumerate(filtered_videos):
                tvid = tvids[i]
                if tvid:
                    final_episodes.append(IqiyiEpisodeInfo(tvId=int(tvid), name=video.short_title, order=i + 1, playUrl=video.page_url))
            
            return final_episodes

        except Exception as e:
            self.logger.error(f"爱奇艺: 获取综艺分集列表失败 (album_id={album_id}): {e}", exc_info=True)
            return []

    async def get_episodes(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        provider_episodes = []
        try:
            # 方案 #1: 使用新的 base_info API
            provider_episodes = await self._get_episodes_v3(media_id)
        except Exception as e:
            self.logger.warning(f"爱奇艺: 新版API (v3) 获取分集时发生错误: {e}", exc_info=True)

        if not provider_episodes:
            self.logger.warning("爱奇艺: 新版API (v3) 未返回分集或失败，正在回退到旧版API...")
            # --- Fallback logic (existing code) ---
            provider_episodes = await self._get_episodes_fallback(media_id, target_episode_index, db_media_type)

        # --- 对所有策略的结果应用通用过滤逻辑 ---
        return await self._filter_and_finalize_episodes(provider_episodes, target_episode_index)

    async def _get_episodes_fallback(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        """旧的分集获取逻辑，现在作为备用方案。"""
        cache_key = f"episodes_{media_id}"
        # 仅当不是强制模式（即初次导入）且请求完整列表时才使用缓存
        if target_episode_index is None and db_media_type is None:
            cached_episodes = await self._get_from_cache(cache_key)
            if cached_episodes is not None:
                self.logger.info(f"爱奇艺: 从缓存中命中分集列表 (media_id={media_id})")
                return [models.ProviderEpisodeInfo.model_validate(e) for e in cached_episodes]
        base_info = await self._get_legacy_video_base_info(media_id)
        if base_info is None:
            return []

        # 修正：更灵活地决定处理模式
        channel = base_info.channel_name or ""
        is_movie_mode = db_media_type == "movie" or (db_media_type is None and channel == "电影")
        is_zongyi_mode = db_media_type == "tv_series" and channel == "综艺"

        if is_movie_mode:
            # 单集（电影）处理逻辑
            episode_data = {
                "tvId": base_info.tv_id or 0,
                "name": base_info.video_name,
                "order": 1,
                "playUrl": base_info.video_url
            }
            episodes: List[IqiyiEpisodeInfo] = [IqiyiEpisodeInfo.model_validate(episode_data)]
        else:
            # 对于电视剧和综艺节目，优先尝试标准剧集接口
            episodes = await self._get_tv_episodes(base_info.album_id)

            # 如果标准接口未返回任何分集，则尝试使用综艺接口作为备用方案
            if not episodes:
                self.logger.info(f"爱奇艺: 标准剧集接口未返回分集，尝试使用综艺节目接口作为备用方案 (album_id={base_info.album_id})。")
                episodes = await self._get_zongyi_episodes(base_info.album_id)

        provider_episodes = [
            models.ProviderEpisodeInfo(
                provider=self.provider_name,
                episodeId=str(ep.tv_id), # Use tv_id for danmaku
                title=ep.name,
                episodeIndex=ep.order,
                url=ep.play_url
            ) for ep in episodes if ep.link_id
        ]

        if provider_episodes:
            episodes_to_cache = [e.model_dump() for e in provider_episodes]
            await self._set_to_cache(cache_key, episodes_to_cache, 'episodes_ttl_seconds', 1800)
        return provider_episodes

    async def _get_duration_for_tvid(self, tvid: str) -> Optional[int]:
        """新增：为指定的tvid获取视频时长。"""
        url = f"https://pcw-api.iqiyi.com/video/video/baseinfo/{tvid}"
        try:
            response = await self._request("GET", url)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == "A00000" and data.get("data"):
                return data["data"].get("durationSec")
        except Exception as e:
            self.logger.warning(f"爱奇艺: 获取视频时长失败 (tvid={tvid}): {e}")
        return None

    async def _filter_and_finalize_episodes(self, episodes: List[models.ProviderEpisodeInfo], target_episode_index: Optional[int]) -> List[models.ProviderEpisodeInfo]:
        """对分集列表应用黑名单过滤并返回最终结果。"""
        # 统一过滤逻辑
        # 修正：iQiyi源只应使用其专属的黑名单，以避免全局规则误杀。
        provider_pattern_str = await self.config_manager.get(
            f"{self.provider_name}_episode_blacklist_regex", self._PROVIDER_SPECIFIC_BLACKLIST_DEFAULT
        )
        blacklist_rules = [provider_pattern_str] if provider_pattern_str else []
        
        filtered_episodes = episodes
        if blacklist_rules:
            original_count = len(episodes)
            temp_episodes = []
            filtered_out_log: Dict[str, List[str]] = defaultdict(list)
            for ep in episodes:
                title_to_check = ep.title
                match_rule = next((rule for rule in blacklist_rules if rule and re.search(rule, title_to_check, re.IGNORECASE)), None)
                if not match_rule:
                    temp_episodes.append(ep)
                else:
                    filtered_out_log[match_rule].append(title_to_check)
            for rule, titles in filtered_out_log.items():
                self.logger.info(f"Iqiyi: 根据黑名单规则 '{rule}' 过滤掉了 {len(titles)} 个分集: {', '.join(titles)}")
            filtered_episodes = temp_episodes

        # 新增：在过滤后重新为分集编号，以确保 episodeIndex 是连续的
        for i, ep in enumerate(filtered_episodes):
            ep.episodeIndex = i + 1

        if target_episode_index:
            return [ep for ep in filtered_episodes if ep.episodeIndex == target_episode_index]
        return filtered_episodes


    async def _get_danmu_content_by_mat(self, tv_id: str, mat: int) -> List[IqiyiComment]:
        """根据tv_id和分段号获取弹幕内容"""
        if len(tv_id) < 4:
            self.logger.warning("tv_id长度不足4位，返回空")
            return []

        # 构建弹幕URL
        s1 = tv_id[-4:-2]
        s2 = tv_id[-2:]
        # 修正：将弹幕服务器地址从 http 改为 https，以修复404错误。
        url = f"https://cmts.iqiyi.com/bullet/{s1}/{s2}/{tv_id}_300_{mat}.z"
        self.logger.debug(f"URL构建: s1={s1}, s2={s2}, 完整URL={url}")

        try:
            # 发送请求
            response = await self._request_with_retry("GET", url)

            # 处理状态码
            if response.status_code == 404:
                self.logger.info(f"未找到分段 {mat}（404）")
                return []
            response.raise_for_status()

            # 解压缩数据
            decompressed_data = zlib.decompress(response.content)

            # 验证解压后的数据是否为空
            if len(decompressed_data) < 10:
                self.logger.warning("解压后数据为空或过小")
                return []

            # 检测编码并处理BOM头
            encoding_result = chardet.detect(decompressed_data)
            encoding = encoding_result['encoding'] or 'utf-8'

            if encoding.lower() == 'utf-8' and decompressed_data.startswith(b'\xef\xbb\xbf'):
                decompressed_data = decompressed_data[3:]
                self.logger.debug("已移除UTF-8 BOM头")

            # 解码为字符串
            xml_str = decompressed_data.decode(encoding, errors='replace')

            # 过滤无效内容
            xml_str = self._filter_entities(xml_str)
            if len(xml_str) < 10:
                self.logger.warning("过滤后XML内容为空或过小")
                return []

            # 关键修复1：使用XML专用解析器，保留原始结构
            parser = etree.XMLParser(recover=True)  # 容错的XML解析器，而非HTML解析器
            try:
                root = etree.fromstring(xml_str.encode('utf-8'), parser=parser)
            except etree.XMLSyntaxError as e:
                self._log_error_context(xml_str, e.lineno, e.position[1])
                raise

            # 提取弹幕信息
            comments = []
            # 遍历绝对路径下的bulletInfo节点
            for item in root.xpath('/danmu/data/entry/list/bulletInfo'):
                content = item.findtext('content')
                show_time = item.findtext('showTime')

                if not (content and show_time):
                    self.logger.debug("跳过缺少content或showTime的弹幕")
                    continue

                comments.append(IqiyiComment(
                    contentId=item.findtext('contentId', default='0'),
                    content=content,
                    showTime=int(show_time),
                    color=item.findtext('color', default='ffffff'),
                    userInfo=IqiyiUserInfo(uid=item.findtext('userInfo/uid'))
                    if item.findtext('userInfo/uid') else None
                ))

            return comments

        except zlib.error:
            self.logger.warning("解压失败（可能是文件损坏）")
        except (etree.XMLSyntaxError, Exception) as e:
            if isinstance(e, etree.XMLSyntaxError):
                self._log_error_context(xml_str, e.lineno, e.position[1])
            self.logger.error(f"处理弹幕分段时发生错误", exc_info=True)

        return []

    async def get_comments(self, episode_id: str, progress_callback: Optional[Callable] = None) -> List[dict]:
        tv_id = episode_id # For iqiyi, episodeId is tvId
        all_comments = []
        
        # 优化：先获取视频总时长，以确定需要请求多少个分段
        duration = await self._get_duration_for_tvid(tv_id)
        if duration and duration > 0:
            total_mats = (duration // 300) + 1
            self.logger.info(f"爱奇艺: 视频时长 {duration}s, 预计弹幕分段数: {total_mats}")
        else:
            total_mats = 100 # 如果获取时长失败，回退到旧的固定循环次数
            self.logger.warning(f"爱奇艺: 未能获取视频时长，将使用默认循环次数 ({total_mats})。")

        for mat in range(1, total_mats + 1):
            if progress_callback:
                progress = int((mat / total_mats) * 100) if total_mats > 0 else 100
                await progress_callback(progress, f"正在获取第 {mat}/{total_mats} 分段")

            comments_in_mat = await self._get_danmu_content_by_mat(tv_id, mat)
            if not comments_in_mat:
                break
            all_comments.extend(comments_in_mat)
            await asyncio.sleep(0.2) # Be nice to the server

        if progress_callback:
            await progress_callback(100, "弹幕整合完成")

        return self._format_comments(all_comments)

    def _format_comments(self, comments: List[IqiyiComment]) -> List[dict]:
        if not comments:
            return []

        # 新增：按 content_id 去重
        unique_comments_map: Dict[str, IqiyiComment] = {}
        for c in comments:
            # 保留第一次出现的弹幕
            if c.content_id not in unique_comments_map:
                unique_comments_map[c.content_id] = c
        
        unique_comments = list(unique_comments_map.values())

        # 1. 按内容对弹幕进行分组
        grouped_by_content: Dict[str, List[IqiyiComment]] = defaultdict(list)
        for c in unique_comments: # 使用去重后的列表
            grouped_by_content[c.content].append(c)

        # 2. 处理重复项
        processed_comments: List[IqiyiComment] = []
        for content, group in grouped_by_content.items():
            if len(group) == 1:
                processed_comments.append(group[0])
            else:
                first_comment = min(group, key=lambda x: x.show_time)
                first_comment.content = f"{first_comment.content} X{len(group)}"
                processed_comments.append(first_comment)

        # 3. 格式化处理后的弹幕列表
        formatted = []
        for c in processed_comments:
            mode = 1 # Default scroll
            try:
                color = int(c.color, 16)
            except (ValueError, TypeError):
                color = 16777215 # Default white

            timestamp = float(c.show_time)
            # 修正：直接在此处添加字体大小 '25'，确保数据源的正确性
            p_string = f"{timestamp:.2f},{mode},25,{color},[{self.provider_name}]"
            formatted.append({
                "cid": c.content_id,
                "p": p_string,
                "m": c.content,
                "t": timestamp
            })
        return formatted

    async def get_id_from_url(self, url: str) -> Optional[str]:
        """
        从爱奇艺视频URL中提取 id (tv_id)。
        优先使用新版 v3 API，失败则回退到旧版。
        """
        link_id_match = re.search(r"v_(\w+?)\.html", url)
        if not link_id_match:
            self.logger.warning(f"爱奇艺 (get_id): 无法从URL中解析出 link_id: {url}")
            return None
        
        link_id = link_id_match.group(1)

        # 方案1: 尝试新版 v3 API
        v3_info = await self._get_v3_base_info(link_id)
        if v3_info and v3_info.current_video_tvid:
            tvid = str(v3_info.current_video_tvid)
            self.logger.info(f"爱奇艺 (get_id): v3 API 成功获取 tvid: {tvid}")
            return tvid

        self.logger.warning(f"爱奇艺 (get_id): v3 API 获取 tvid 失败，回退到旧版 API。")

        # 方案2: 回退到旧版 API
        base_info = await self._get_legacy_video_base_info(link_id)
        if base_info and base_info.tv_id:
            self.logger.info(f"爱奇艺 (get_id): 旧版 API 成功获取 tvid: {base_info.tv_id}")
            return str(base_info.tv_id)
        
        self.logger.warning(f"爱奇艺: 未能从 link_id '{link_id}' 获取到 tvid。")
        
        return None

    def format_episode_id_for_comments(self, provider_episode_id: Any) -> str:
        """For iqiyi, the episode ID is a simple string (tv_id), so no formatting is needed."""
        return str(provider_episode_id)
