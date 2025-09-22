import asyncio
import httpx
import re
import logging
import time
import html
import json
from typing import List, Dict, Any, Optional, Union, Callable, Tuple
from urllib.parse import quote
from pydantic import BaseModel, Field, ValidationError, field_validator
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from datetime import datetime, timezone
from ..config_manager import ConfigManager
from ..utils import parse_search_keyword
from .base import BaseScraper, get_season_from_title
from .. import models, crud

scraper_responses_logger = logging.getLogger("scraper_responses")

# --- Pydantic 模型，用于解析腾讯API的响应 ---

# --- Models for Get Comments API ---
class TencentCommentContentStyle(BaseModel):
    color: Optional[str] = None
    position: Optional[int] = None
    gradient_colors: Optional[List[str]] = None

class TencentEpisode(BaseModel):
    vid: str = Field(..., description="分集视频ID")
    title: str = Field(..., description="分集标题")
    is_trailer: str = Field("0", alias="is_trailer")
    union_title: Optional[str] = None

class TencentComment(BaseModel):
    id: str = Field(..., description="弹幕ID")
    # API 返回的是字符串，我们直接接收字符串，在后续处理中转为数字
    time_offset: str = Field(..., description="弹幕时间偏移(毫秒)")
    content: str = Field(..., description="弹幕内容")
    # API 对普通弹幕返回空字符串 ""，对特殊弹幕返回对象。Union可以同时处理这两种情况。
    content_style: Union[TencentCommentContentStyle, str, None] = Field(None)

    @field_validator("content_style", mode="before")
    @classmethod
    def _validate_content_style(cls, v: Any) -> Any:
        """在Pydantic验证前，尝试将JSON字符串解析为字典。"""
        if isinstance(v, str) and v.startswith('{') and v.endswith('}'):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                # 如果解析失败，则返回原始字符串，保持健壮性
                return v
        return v


# --- 用于搜索API的新模型 ---
class TencentSubjectDoc(BaseModel):
    video_num: int = Field(0, alias="videoNum")

class TencentSearchVideoInfo(BaseModel):
    title: str
    year: Optional[int] = None
    type_name: str = Field(alias="typeName")
    img_url: Optional[str] = Field(None, alias="imgUrl")
    subject_doc: Optional[TencentSubjectDoc] = Field(None, alias="subjectDoc") # type: ignore
    play_sites: Optional[List[Dict[str, Any]]] = Field(None, alias="playSites") # Added for filtering
    episode_sites: Optional[List[Dict[str, Any]]] = Field(None, alias="episodeSites")
    sub_title: Optional[str] = Field(None, alias="subTitle")
    play_flag: Optional[int] = Field(None, alias="playFlag")

class TencentSearchDoc(BaseModel):
    id: str  # 这是 cid

class TencentSearchItem(BaseModel):
    video_info: Optional[TencentSearchVideoInfo] = Field(None, alias="videoInfo")
    doc: TencentSearchDoc

class TencentSearchItemList(BaseModel):
    item_list: List[TencentSearchItem] = Field(alias="itemList")

class TencentSearchData(BaseModel):
    normal_list: Optional[TencentSearchItemList] = Field(None, alias="normalList")

class TencentSearchResult(BaseModel):
    data: Optional[TencentSearchData] = None

# --- Models for the new MultiTerminalSearch API (from JS file) ---
class TencentAreaBox(BaseModel):
    boxId: str
    itemList: Optional[List[TencentSearchItem]] = None

class TencentSearchDataV2(BaseModel):
    areaBoxList: Optional[List[TencentAreaBox]] = None
    normalList: Optional[TencentSearchItemList] = None

class TencentSearchResultV2(BaseModel):
    data: Optional[TencentSearchDataV2] = None
    ret: int
    msg: Optional[str] = None

# --- Models for GetPageData API (New) ---

class TencentEpisodeTabInfo(BaseModel):
    begin: int
    end: int
    page_context: str = Field(alias="page_context")

class TencentModuleParams(BaseModel):
    tabs: Optional[str] = None # This is a JSON string

class TencentItemParams(BaseModel):
    vid: Optional[str] = None
    title: str
    is_trailer: str = Field("0", alias="is_trailer")
    union_title: Optional[str] = None

class TencentItemData(BaseModel):
    item_params: Optional[TencentItemParams] = Field(None, alias="item_params")

class TencentItemDataLists(BaseModel):
    item_datas: List[TencentItemData] = Field(default_factory=list, alias="item_datas")

class TencentModuleData(BaseModel):
    module_params: Optional[TencentModuleParams] = Field(None, alias="module_params")
    item_data_lists: Optional[TencentItemDataLists] = Field(None, alias="item_data_lists")

class TencentModuleListData(BaseModel):
    module_datas: List[TencentModuleData] = Field(default_factory=list, alias="module_datas")

class TencentPageData(BaseModel):
    module_list_datas: List[TencentModuleListData] = Field(default_factory=list, alias="module_list_datas")

class TencentPageResult(BaseModel):
    ret: int
    data: Optional[TencentPageData] = None

# --- 用于搜索API的请求模型 (参考C#代码) ---
class TencentSearchRequest(BaseModel):
    query: str
    version: str = ""
    filter_value: str = Field("firstTabid=150", alias="filterValue")
    retry: int = 0
    pagenum: int = 0
    pagesize: int = 20
    query_from: int = Field(4, alias="queryFrom")
    is_need_qc: bool = Field(True, alias="isneedQc")
    ad_request_info: str = Field("", alias="adRequestInfo")
    sdk_request_info: str = Field("", alias="sdkRequestInfo")
    scene_id: int = Field(21, alias="sceneId")
    platform: str = "23"

# --- 新的搜索API请求模型 (参考JS代码) ---
class TencentExtraInfo(BaseModel):
    isNewMarkLabel: str = "1"
    multi_terminal_pc: str = "1"
    themeType: str = "1"
    sugRelatedIds: str = "{}"
    appVersion: str = ""

class TencentMultiTerminalSearchRequest(BaseModel):
    version: str = "25071701"
    clientType: int = 1
    filterValue: str = ""
    uuid: str = "0379274D-05A0-4EB6-A89C-878C9A460426"
    query: str
    retry: int = 0
    pagenum: int = 0
    isPrefetch: bool = True
    pagesize: int = 30
    queryFrom: int = 0
    searchDatakey: str = ""
    transInfo: str = ""
    isneedQc: bool = True
    preQid: str = ""
    adClientInfo: str = ""
    extraInfo: TencentExtraInfo = Field(default_factory=TencentExtraInfo)

# --- 腾讯API客户端 ---

class TencentScraper(BaseScraper):
    """
    用于从腾讯视频抓取分集信息和弹幕的客户端。
    """
    provider_name = "tencent"
    handled_domains = ["v.qq.com"]
    referer = "https://v.qq.com/"
    test_url = "https://v.qq.com"
    # 基于JS参考实现，提供一个更通用和全面的分集黑名单。
    # 修正：使用一个更简洁且高效的正则表达式，而不是对每个关键词进行 re.escape。
    # 这提高了可读性，并允许使用正则表达式的特性（如 `|`）。
    _PROVIDER_SPECIFIC_BLACKLIST_DEFAULT = (
        r"^(.*?)(vlog|reaction|纯享|加更|抢先|预告|花絮|特辑|彩蛋|专访|幕后|直播|未播|衍生|番外|会员|片花|精华|看点|速看|解读|影评|解说|吐槽|盘点|"
        r"拍摄花絮|制作花絮|幕后花絮|未播花絮|独家花絮|花絮特辑|先导预告|终极预告|正式预告|官方预告|彩蛋片段|删减片段|未播片段|番外彩蛋|"
        r"精彩片段|精彩看点|精彩回顾|精彩集锦|看点解析|看点预告|NG镜头|NG花絮|番外篇|番外特辑|制作特辑|拍摄特辑|幕后特辑|导演特辑|演员特辑|"
        r"片尾曲|插曲|主题曲|背景音乐|OST|音乐MV|歌曲MV|前季回顾|剧情回顾|往期回顾|内容总结|剧情盘点|精选合集|剪辑合集|混剪视频|"
        r"独家专访|演员访谈|导演访谈|主创访谈|媒体采访|发布会采访|抢先看|抢先版|试看版|短剧|精编|会员版|Plus|独家版|特别版|短片|合唱)(.*?)$"
    )

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager):
        super().__init__(session_factory, config_manager)
        # 用于从标题中提取集数的正则表达式
        self._EPISODE_INDEX_PATTERN = re.compile(r"^(?:第)?(\d+)(?:集|话)?$")
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://v.qq.com/",
        }
        # 根据C#代码，这个特定的cookie对于成功请求至关重要
        self.cookies = {"pgv_pvid": "40b67e3b06027f3d", "video_platform": "2", "vversion_name": "8.2.95", "video_bucketid": "4", "video_omgid": "0a1ff6bc9407c0b1cff86ee5d359614d"}
        
        # Headers and cookies for the new MultiTerminalSearch API
        self.multiterminal_headers = {
            'Content-Type': 'application/json',
            'Origin': 'https://v.qq.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'H38': '220496a1fb1498325e9be6d938',
            'H42': '335a00a80ab9bbbef56793d8e7a97e87b9341dee34ebd83d61afc0cdb303214caaece3',
            'Uk': '8e91af25d3af99d0f0640327e7307666',
        }
        self.multiterminal_cookies = {'tvfe_boss_uuid': 'ee8f05103d59226f', 'pgv_pvid': '3155633511', 'video_platform': '2', 'ptag': 'v_qq_com', 'main_login': 'qq'}

        # httpx.AsyncClient 是 Python 中功能强大的异步HTTP客户端，等同于 C# 中的 HttpClient
        # 此处通过 cookies 参数传入字典，httpx 会自动将其格式化为正确的 Cookie 请求头，效果与C#代码一致
        self.client: Optional[httpx.AsyncClient] = None

        self._api_lock = asyncio.Lock()
        self._last_request_time = 0
        self._min_interval = 0.5 # A reasonable default

        self.episodes_api_url = "https://pbaccess.video.qq.com/trpc.universal_backend_service.page_server_rpc.PageServer/GetPageData?video_appid=3000010&vversion_name=8.2.96&vversion_platform=2"

    def _get_episode_headers(self, cid: str) -> Dict[str, str]: # type: ignore
        """获取用于分集API请求的移动端头部。"""
        return {
            'Content-Type': 'application/json',
            'Origin': 'https://v.qq.com',
            'Referer': f"https://v.qq.com/x/cover/{cid}.html",
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Accept': 'application/json',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Ensures the httpx client is initialized, with proxy support."""
        # 检查代理配置是否发生变化
        new_proxy_config = await self._get_proxy_for_provider()
        if self.client and new_proxy_config != self._current_proxy_config:
            self.logger.info("Tencent: 代理配置已更改，正在重建HTTP客户端...")
            await self.client.aclose()
            self.client = None

        if self.client is None:
            self.client = await self._create_client(
                headers=self.base_headers, cookies=self.cookies, timeout=20.0
            )
        return self.client

    async def get_episode_blacklist_pattern(self) -> Optional[re.Pattern]:
        """
        获取并编译用于过滤分集的正则表达式。
        此方法现在只使用数据库中配置的规则，如果规则为空，则不进行过滤。
        """
        # 1. 构造该源特定的配置键
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

    async def _request_with_rate_limit(self, method: str, url: str, **kwargs) -> httpx.Response:
        """封装了速率限制的请求方法。"""
        await self._ensure_client()
        assert self.client is not None
        async with self._api_lock:
            now = time.time()
            time_since_last = now - self._last_request_time
            if time_since_last < self._min_interval:
                sleep_duration = self._min_interval - time_since_last
                self.logger.debug(f"Tencent: 速率限制，等待 {sleep_duration:.2f} 秒...")
                await asyncio.sleep(sleep_duration)

            response = await self.client.request(method, url, **kwargs)
            self._last_request_time = time.time()
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"Tencent Response ({method} {url}): status={response.status_code}, text={response.text[:500]}")
            return response

    # Porting TITLE_MAPPING from JS
    _TITLE_MAPPING = {
        '斗破苍穹年番': '斗破苍穹 第5季'
    }

    def _apply_title_mapping(self, title: str) -> str:
        """Ported from JS: applyTitleMapping."""
        if not title:
            return title
        mapped_title = self._TITLE_MAPPING.get(title)
        if mapped_title:
            self.logger.debug(f"应用标题映射: '{title}' -> '{mapped_title}'")
            return mapped_title
        return title

    async def close(self):
        """关闭HTTP客户端"""
        if self.client:
            await self.client.aclose()
            self.client = None

    async def _filter_search_item(self, item: TencentSearchItem, keyword: str) -> Optional[models.ProviderSearchInfo]:
        """
        Ported from JS: processSearchItemQuick, focusing on filtering.
        Processes a single search item and applies filtering rules.
        """
        if not item.video_info or not item.doc:
            self.logger.debug("跳过无效项目: 缺少video_info或doc")
            return None

        video_info = item.video_info
        media_id = item.doc.id # 对于搜索结果，这个ID是cid

        # 关键修正：参考旧代码，过滤掉没有年份信息的条目。
        # 这通常是无效的或非正片内容（如“安利向”、“二创合集”等）。
        if not video_info.year or video_info.year == 0:
            self.logger.debug(f"跳过无年份信息的项目: {video_info.title}")
            return None

        # 新增：过滤掉“全网搜”等非直接播放的结果
        if video_info.sub_title == "全网搜" or video_info.play_flag == 2:
            self.logger.debug(f"跳过“全网搜”或非直接播放的结果: {video_info.title}")
            return None

        # Extract and clean title
        title = video_info.title.replace('<em>', '').replace('</em>', '')
        title = self._apply_title_mapping(title)

        # Basic validation
        if not title or not media_id:
            self.logger.debug(f"跳过无效项目: title={title}, media_id={media_id}")
            return None

        # Apply intelligent filtering based on keywords
        if self._GLOBAL_SEARCH_JUNK_TITLE_PATTERN.search(title):
            self.logger.debug(f"跳过不相关内容 (Global Junk Pattern Filter): {title}")
            return None

        # 内容类型过滤与映射
        content_type = video_info.type_name
        if "短剧" in content_type:
            self.logger.debug(f"跳过短剧类型: {title}")
            return None
        
        # 将腾讯的类型映射到系统内部标准类型
        type_mapping = {
            "电视剧": "tv_series", "动漫": "tv_series",
            "电影": "movie",
            "纪录片": "tv_series",
            "综艺": "tv_series", "综艺节目": "tv_series",
        }
        internal_media_type = type_mapping.get(content_type)

        if not internal_media_type:
            self.logger.debug(f"跳过不支持的内容类型: {content_type}")
            return None

        # Filter non-QQ platform content
        all_sites = (video_info.play_sites or []) + (video_info.episode_sites or [])
        if all_sites and not any(site.get("enName") == 'qq' for site in all_sites):
            self.logger.debug(f"跳过非腾讯视频内容: {title} (无qq平台)")
            return None

        # New logic to cache chapterInfo from episodeSites
        qq_episode_site = next((site for site in (video_info.episode_sites or []) if site.get("enName") == 'qq'), None)
        if qq_episode_site and qq_episode_site.get("chapterInfo"):
            chapter_info = qq_episode_site["chapterInfo"]
            if chapter_info:
                chapter_cache_key = f"chapter_info_{media_id}"
                await self._set_to_cache(chapter_cache_key, chapter_info, 'episodes_ttl_seconds', 1800)
                self.logger.info(f"Tencent: Cached chapterInfo for media_id={media_id}")

        # Filter movie-like non-formal content (e.g., documentaries, behind-the-scenes)
        if content_type == "电影":
            non_formal_keywords = [ "花絮", "彩蛋", "幕后", "独家", "解说", "特辑", "探班", "拍摄", "制作", "导演", "记录", "回顾", "盘点", "混剪", "解析", "抢先"]
            if any(kw in title for kw in non_formal_keywords):
                self.logger.debug(f"检测到非正片电影内容，跳过处理: {title}")
                return None

        # Extract other info
        year = str(video_info.year) if video_info.year else None
        cover_url = video_info.img_url

        # 修正：如果内容是电影，则总集数应为1，而不是依赖API可能返回的0。
        episode_count = 1 if internal_media_type == 'movie' else (video_info.subject_doc.video_num if video_info.subject_doc else None)

        # Build ProviderSearchInfo
        return models.ProviderSearchInfo(
            provider=self.provider_name,
            mediaId=media_id,
            title=title,
            type=internal_media_type, # 使用映射后的标准类型
            season=get_season_from_title(title),
            year=int(year) if year else None,
            imageUrl=cover_url,
            episodeCount=episode_count,
            currentEpisodeIndex=None # This is a quick search, detailed episode info is fetched later
        )

    async def _search_with_payload(self, keyword: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> List[models.ProviderSearchInfo]:
        url = "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.HttpMobileRecall/MbSearchHttp"
        results = []
        try:
            api_name = "移动API" if headers else "桌面API"
            self.logger.info(f"Tencent ({api_name}): 正在搜索 '{keyword}'...")
            response = await self._request_with_rate_limit("POST", url, json=payload, headers=headers)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"Tencent Search Response ({api_name}, keyword='{keyword}'): {response.text}")

            response.raise_for_status()
            response_json = response.json()
            data = TencentSearchResult.model_validate(response_json)

            if data.data and data.data.normal_list:
                tasks = [self._filter_search_item(item, keyword) for item in data.data.normal_list.item_list]
                filtered_items = await asyncio.gather(*tasks)
                for filtered_item in filtered_items:
                    if filtered_item:
                        results.append(filtered_item)
        except httpx.HTTPStatusError as e:
            self.logger.error(f"Tencent ({api_name}): 搜索请求失败: {e}")
        except (ValidationError, KeyError) as e:
            self.logger.error(f"Tencent ({api_name}): 解析搜索结果失败: {e}", exc_info=True)
        return results

    async def _search_desktop_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """通过腾讯桌面搜索API查找番剧。"""
        request_model = TencentSearchRequest(query=keyword)
        payload = request_model.model_dump(by_alias=True)
        return await self._search_with_payload(keyword, payload)

    async def _search_mobile_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """通过腾讯移动端搜索API查找番剧。"""
        request_model = TencentSearchRequest(query=keyword)
        payload = request_model.model_dump(by_alias=True)
        headers = self.base_headers.copy()
        headers['User-Agent'] = 'live4iphoneRel/9.01.46 (iPhone; iOS 18.5; Scale/3.00)'
        return await self._search_with_payload(keyword, payload, headers)

    async def _search_multiterminal_api(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """通过腾讯新的 MultiTerminalSearch API 查找番剧 (基于JS代码)。"""
        url = "https://pbaccess.video.qq.com/trpc.videosearch.mobile_search.MultiTerminalSearch/MbSearch?vplatform=2"
        request_model = TencentMultiTerminalSearchRequest(query=keyword)
        payload = request_model.model_dump(by_alias=False)
        
        headers = self.multiterminal_headers.copy()
        encoded_keyword = quote(keyword)
        headers['Referer'] = f"https://v.qq.com/x/search/?q={encoded_keyword}&stag=&smartbox_ab="

        results = []
        try:
            self.logger.info(f"Tencent (MultiTerminal API): 正在搜索 '{keyword}'...")
            # 修正：为这个特殊的API调用也应用代理设置
            proxy_to_use = await self._get_proxy_for_provider()
            async with httpx.AsyncClient(headers=headers, cookies=self.multiterminal_cookies, timeout=20.0, proxy=proxy_to_use) as client:
                response = await client.post(url, json=payload)

            if await self._should_log_responses():
                scraper_responses_logger.debug(f"Tencent MultiTerminal Search Response (keyword='{keyword}'): {response.text}")

            response.raise_for_status()
            response_json = response.json()
            data = TencentSearchResultV2.model_validate(response_json)

            if data.ret != 0:
                self.logger.error(f"Tencent (MultiTerminal API): API返回错误: {data.msg} (ret: {data.ret})")
                return []

            items_to_process = []
            if data.data and data.data.areaBoxList:
                for box in data.data.areaBoxList:
                    if box.boxId == "MainNeed" and box.itemList:
                        self.logger.debug(f"Tencent (MultiTerminal API): 从 MainNeed box 找到 {len(box.itemList)} 个项目。")
                        items_to_process.extend(box.itemList)
                        break
            
            if not items_to_process and data.data and data.data.normalList and data.data.normalList.item_list:
                self.logger.debug("Tencent (MultiTerminal API): MainNeed box 未找到或为空, 回退到 normalList。")
                items_to_process.extend(data.data.normalList.item_list)

            tasks = [self._filter_search_item(item, keyword) for item in items_to_process]
            filtered_items = await asyncio.gather(*tasks)
            for filtered_item in filtered_items:
                if filtered_item:
                    results.append(filtered_item)
        except (httpx.HTTPStatusError, ValidationError, KeyError, json.JSONDecodeError) as e:
            self.logger.error(f"Tencent (MultiTerminal API): 解析或请求失败: {e}", exc_info=True)
        return results

    async def search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """使用 MultiTerminal API 作为主API进行搜索，并在失败时回退到其他API。"""
        # 智能缓存逻辑
        parsed = parse_search_keyword(keyword)
        search_title = parsed['title']
        search_season = parsed['season']

        cache_key = f"search_base_{self.provider_name}_{search_title}"
        cached_results = await self._get_from_cache(cache_key)

        if cached_results:
            self.logger.info(f"Tencent: 从缓存中命中基础搜索结果 (title='{search_title}')")
            all_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results]
        else:
            self.logger.info(f"Tencent: 缓存未命中，正在为标题 '{search_title}' 执行网络搜索...")
            all_results = await self._perform_network_search(search_title, episode_info)
            if all_results:
                await self._set_to_cache(cache_key, [r.model_dump() for r in all_results], 'search_ttl_seconds', 3600)

        if search_season is None:
            return all_results

        # Filter results by season
        final_results = [item for item in all_results if item.season == search_season]
        self.logger.info(f"Tencent: 为 S{search_season} 过滤后，剩下 {len(final_results)} 个结果。")
        return final_results

    async def _perform_network_search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """
        实际执行网络搜索的内部方法。
        优先使用 MultiTerminal API，如果失败或无结果，则回退到其他API。
        """
        # 1. 优先尝试 MultiTerminal API
        self.logger.info(f"Tencent: 正在尝试使用主API (MultiTerminal) 搜索 '{keyword}'...")
        try:
            multiterminal_results = await self._search_multiterminal_api(keyword, episode_info)
            if multiterminal_results:
                self.logger.info(f"Tencent: 主API (MultiTerminal) 成功找到 {len(multiterminal_results)} 个结果。")
                # 基于 mediaId 去重
                unique_results = list({item.mediaId: item for item in multiterminal_results}.values())
                if unique_results:
                    log_results = "\n".join([f"  - {r.title} (ID: {r.mediaId}, 类型: {r.type}, 年份: {r.year or 'N/A'})" for r in unique_results])
                    self.logger.info(f"Tencent (主API): 搜索结果列表:\n{log_results}")
                return unique_results
        except Exception as e:
            self.logger.warning(f"Tencent: 主API (MultiTerminal) 搜索失败: {e}", exc_info=True)

        # 2. 如果主API失败或无结果，则回退到备用API
        self.logger.info(f"Tencent: 主API未找到结果或失败，正在回退到备用API (桌面/移动)...")
        desktop_task = self._search_desktop_api(keyword, episode_info)
        mobile_task = self._search_mobile_api(keyword, episode_info)
        
        results_lists = await asyncio.gather(desktop_task, mobile_task, return_exceptions=True)
        
        all_fallback_results = []
        api_names = ["桌面API", "移动API"]
        for i, res_list in enumerate(results_lists):
            api_name = api_names[i]
            if isinstance(res_list, list):
                all_fallback_results.extend(res_list)
            elif isinstance(res_list, Exception):
                if isinstance(res_list, (httpx.TimeoutException, httpx.ConnectError)):
                    self.logger.warning(f"Tencent (备用 - {api_name}): 搜索时连接超时或网络错误: {res_list}")
                else:
                    self.logger.error(f"Tencent (备用 - {api_name}): 搜索子任务失败", exc_info=res_list)

        # 基于 mediaId 去重
        unique_fallback_results = list({item.mediaId: item for item in all_fallback_results}.values())
        return unique_fallback_results

    async def get_info_from_url(self, url: str) -> Optional[models.ProviderSearchInfo]:
        """从腾讯视频URL中提取作品信息。"""
        self.logger.info(f"Tencent: 正在从URL提取信息: {url}")
        cid_match = re.search(r'/cover/([^/]+?)(/|\.html|$)', url)
        if not cid_match:
            self.logger.warning(f"Tencent: 无法从URL中解析出cid: {url}")
            return None
        
        cid = cid_match.group(1)
        
        try:
            response = await self._request_with_rate_limit("GET", url)
            response.raise_for_status()
            html_content = response.text

            title_match = re.search(r'<title>(.*?)</title>', html_content)
            title = title_match.group(1).split('-')[0].strip() if title_match else "未知标题"

            # 尝试从页面JSON中获取更精确的信息
            json_match = re.search(r'window\.video_next_list\s*=\s*({.*?});', html_content)
            media_type = "tv_series" # 默认
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    if data.get("type") == "1": media_type = "movie"
                except json.JSONDecodeError:
                    pass

            return models.ProviderSearchInfo(
                provider=self.provider_name, mediaId=cid, title=title, type=media_type, season=get_season_from_title(title)
            )
        except Exception as e:
            self.logger.error(f"Tencent: 从URL '{url}' 提取信息失败: {e}", exc_info=True)
            return None

    async def _get_movie_vid_from_api(self, cid: str) -> Optional[str]:
        """对于电影，优先尝试使用API获取其vid，这比解析HTML更可靠。"""
        self.logger.info(f"正在尝试使用API获取电影 (cid={cid}) 的 vid...")
        payload = {
            "page_params": {
                "cid": cid,
                "page_type": "detail_operation",
                "page_id": "vsite_episode_list_search",
                "id_type": "1",
                "page_size": "10",  # 我们只需要一个，但请求几个以防万一
                "lid": "",
                "req_from": "web_vsite",
                "page_context": f"cid={cid}&req_from=web_vsite",
                "page_num": "0"
            }
        }
        try:
            response = await self._request_with_rate_limit("POST", self.episodes_api_url, json=payload)
            response.raise_for_status()
            result = TencentPageResult.model_validate(response.json())

            if result.data and result.data.module_list_datas:
                for module_list_data in result.data.module_list_datas:
                    for module_data in module_list_data.module_datas:
                        if module_data.item_data_lists:
                            for item in module_data.item_data_lists.item_datas:
                                # 找到第一个不是预告片的有效vid
                                if item.item_params and item.item_params.vid and item.item_params.is_trailer != "1":
                                    vid = item.item_params.vid
                                    self.logger.info(f"通过API成功获取到电影 (cid={cid}) 的 vid: {vid}")
                                    return vid
        except Exception as e:
            self.logger.error(f"通过API获取电影vid失败 (cid={cid}): {e}", exc_info=False)
        
        self.logger.warning(f"通过API未能获取到电影 (cid={cid}) 的 vid。")
        return None
    async def _get_cover_info(self, cid: str) -> Optional[Dict[str, Any]]:
        """获取封面信息，主要为了其中的 chapter_info (季/章信息)。"""
        # 修正：使用更可靠的 page_id 和 payload 来获取包含 chapter_info 的数据
        # 这个 payload 模拟了分页获取第一页的请求，该请求的响应中包含了所有章节信息或tabs信息。
        page_context_str = f"cid={cid}&req_from=web_vsite"
        payload = {
            "page_params": {
                "req_from": "web_vsite",
                "page_id": "vsite_episode_list", # 使用这个 page_id，与JS实现一致
                "page_type": "detail_operation",
                "id_type": "1",
                "cid": cid,
                "page_context": page_context_str,
                "page_num": "0",
                "page_size": "30" # 请求一页分集以获取章节信息
            }
        }
        try:
            headers = self._get_episode_headers(cid)
            response = await self._request_with_rate_limit("POST", self.episodes_api_url, json=payload, headers=headers)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"Tencent Cover Info Response (cid={cid}): {response.text}")
            response.raise_for_status()
            result = response.json()
            if result.get("ret") == 0 and result.get("data"):
                for module_list_data in result["data"].get("module_list_datas", []):
                    for module_data in module_list_data.get("module_datas", []):
                        # 优先从 module_params 中获取 tabs (新版API的分页信息)
                        if module_data.get("module_params", {}).get("tabs"):
                            self.logger.info(f"Tencent: 成功从 module_params 获取到 tabs (cid={cid})")
                            return {"chapters": json.loads(module_data["module_params"]["tabs"])} # 包装成chapters格式
                        # 如果没有tabs，再尝试获取 chapter_info
                        if chapter_info := module_data.get("module_params", {}).get("chapter_info"):
                            self.logger.info(f"Tencent: 成功从 module_params 获取到 chapter_info (cid={cid})")
                            return chapter_info
        except Exception as e:
            self.logger.error(f"Tencent: 获取封面信息(chapterInfo)失败 (cid={cid}): {e}", exc_info=True)
        return None
    
    async def get_episodes(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        """
        获取分集列表，优先使用新的“分页卡片”策略。
        """
        # 仅当请求完整列表时才使用缓存
        # 修正：缓存键应表示缓存的是原始数据
        cache_key = f"episodes_raw_{media_id}"
        
        raw_episodes: List[TencentEpisode] = []

        # 仅当请求完整列表时才尝试从缓存获取
        if target_episode_index is None:
            cached_episodes = await self._get_from_cache(cache_key)
            if cached_episodes is not None:
                self.logger.info(f"Tencent: 从缓存中命中原始分集列表 (media_id={media_id})")
                raw_episodes = [TencentEpisode.model_validate(e) for e in cached_episodes]

        # 如果缓存未命中或不需要缓存，则从网络获取
        if not raw_episodes:
            self.logger.info(f"Tencent: 缓存未命中或需要特定分集，正在为 media_id={media_id} 执行网络获取...")
            network_episodes = []
            try:
                # 策略1: 优先尝试从搜索时缓存的 chapter_info 获取分集
                chapter_info = await self._get_from_cache(f"chapter_info_{media_id}")
                if chapter_info and chapter_info.get("chapters"):
                    self.logger.info(f"Tencent: 从缓存中找到 {len(chapter_info['chapters'])} 个季/章，将分别获取分集。")
                    all_episodes_from_chapters: Dict[str, TencentEpisode] = {}
                    
                    chapter_tabs = [
                        TencentEpisodeTabInfo(begin=0, end=0, page_context=chap['pageContext'])
                        for chap in chapter_info['chapters'] if 'pageContext' in chap
                    ]
                    
                    tasks = [self._fetch_episodes_by_tab(media_id, tab) for tab in chapter_tabs]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for res in results:
                        if isinstance(res, list):
                            for ep in res:
                                all_episodes_from_chapters[ep.vid] = ep
                        elif isinstance(res, Exception):
                            self.logger.error(f"获取分集章节时出错: {res}")
                    
                    network_episodes = list(all_episodes_from_chapters.values())

                # 策略2: 如果缓存策略失败或无缓存，则执行完整的网络获取流程
                if not network_episodes:
                    if chapter_info: # Log if we tried cache and it failed
                        self.logger.info("Tencent: 缓存的章节信息未能获取到分集，回退到完整网络获取。")
                    
                    # 遵循JS逻辑：优先尝试分页卡片API，然后是通用分页API，最后是旧版API。
                    self.logger.info(f"Tencent: 正在尝试使用分页卡片API获取分集 (cid={media_id})")
                    network_episodes = await self._fetch_episodes_with_tabs(media_id)
                    
                    if network_episodes:
                        self.logger.info(f"Tencent: 分页卡片API成功获取 {len(network_episodes)} 集。")
                    else:
                        self.logger.info(f"Tencent: 分页卡片API未获取到数据，回退到通用分页方法。")
                        network_episodes = await self._fetch_episodes_paginated(media_id, db_media_type)
                        if network_episodes:
                            self.logger.info(f"Tencent: 通用分页方法成功获取 {len(network_episodes)} 集。")
                        else:
                            self.logger.info(f"Tencent: 通用分页方法未获取到数据。")

            except Exception as e:
                self.logger.error(f"Tencent: 获取分集列表时发生未知错误 (cid={media_id}): {e}", exc_info=True)
            
            raw_episodes = network_episodes
            # 仅当请求完整列表且成功获取到数据时，才缓存原始数据
            if raw_episodes and target_episode_index is None:
                await self._set_to_cache(cache_key, [e.model_dump() for e in raw_episodes], 'episodes_ttl_seconds', 1800)

        provider_episodes = await self._process_and_format_tencent_episodes(raw_episodes, db_media_type, media_id)

        if target_episode_index:
            return [ep for ep in provider_episodes if ep.episodeIndex == target_episode_index]
        return provider_episodes

    def _get_episode_index_from_title(self, title: str) -> Optional[int]:
        """
        从分集标题（如 "01", "第01集"）中解析出集数。
        """
        if not title:
            return None
        match = self._EPISODE_INDEX_PATTERN.match(title.strip())
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                return None
        return None

    async def _get_episode_tabs_info(self, cid: str) -> Optional[List[TencentEpisodeTabInfo]]:
        """获取分集列表的分页卡片信息。"""
        payload = {
            "has_cache": 1,
            "page_params": {
                "req_from": "web_vsite", "page_id": "vsite_episode_list", "page_type": "detail_operation",
                "id_type": "1", "page_size": "", "cid": cid, "vid": "", "lid": "", "page_num": "",
                "page_context": f"cid={cid}&detail_page_type=1&req_from=web_vsite&req_from_second_type=&req_type=0",
                "detail_page_type": "1"
            }
        }
        headers = self._get_episode_headers(cid)
        response = await self._request_with_rate_limit("POST", self.episodes_api_url, json=payload, headers=headers)
        response.raise_for_status()
        result = TencentPageResult.model_validate(response.json())

        if result.data and result.data.module_list_datas:
            for module_list_data in result.data.module_list_datas:
                for module_data in module_list_data.module_datas:
                    if module_data.module_params and module_data.module_params.tabs:
                        tabs_str = module_data.module_params.tabs
                        try:
                            return [TencentEpisodeTabInfo.model_validate(t) for t in json.loads(tabs_str)]
                        except json.JSONDecodeError:
                            self.logger.error(f"Tencent: 解析分集卡片JSON失败: {tabs_str}")
        return None

    async def _fetch_episodes_by_tab(self, cid: str, tab: TencentEpisodeTabInfo) -> List[TencentEpisode]:
        """根据单个分页卡片信息获取分集。"""
        payload = {
            "has_cache": 1,
            "page_params": {
                "req_from": "web_vsite", "page_id": "vsite_episode_list", "page_type": "detail_operation",
                "id_type": "1", "page_size": "", "cid": cid, "vid": "", "lid": "", "page_num": "",
                "page_context": tab.page_context, "detail_page_type": "1"
            }
        }
        headers = self._get_episode_headers(cid)
        # 修正：使用 self._request 方法以确保代理和通用头部被应用
        response = await self._request_with_rate_limit("POST", self.episodes_api_url, json=payload, headers=headers)
        response.raise_for_status()
        result = TencentPageResult.model_validate(response.json())
        
        episodes = []
        if result.data and result.data.module_list_datas:
            for module_list_data in result.data.module_list_datas:
                for module_data in module_list_data.module_datas:
                    if module_data.item_data_lists:
                        for item in module_data.item_data_lists.item_datas:
                            if item.item_params and item.item_params.vid:
                                episodes.append(TencentEpisode.model_validate(item.item_params.model_dump()))
        return episodes

    async def _fetch_episodes_with_tabs(self, cid: str) -> List[TencentEpisode]:
        """主策略：使用分页卡片获取所有分集。"""
        tabs = await self._get_episode_tabs_info(cid)
        if not tabs:
            return []

        self.logger.info(f"Tencent: 找到 {len(tabs)} 个分集卡片，开始并发获取...")
        all_episodes: Dict[str, TencentEpisode] = {}
        # 新增：批处理逻辑，参考JS实现，避免一次性发送过多请求
        batch_size = 3
        for i in range(0, len(tabs), batch_size):
            batch = tabs[i:i + batch_size]
            self.logger.debug(f"Tencent: 正在处理分集卡片批次 {i//batch_size + 1}...")
            tasks = [self._fetch_episodes_by_tab(cid, tab) for tab in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, list):
                    for ep in res:
                        all_episodes[ep.vid] = ep
                elif isinstance(res, Exception):
                    self.logger.error(f"获取分集卡片时出错: {res}")
            
            # 批次间延时
            if i + batch_size < len(tabs):
                await asyncio.sleep(0.3)

        return list(all_episodes.values())

    def _build_page_context(self, cid: str, config: Dict[str, Any]) -> str:
        """构建page_context参数，参考 tx.py。"""
        context_params = {
            'chapter_name': '',
            'cid': cid,
            'detail_page_type': '1',
            'episode_begin': str(config['episode_begin']),
            'episode_end': str(config['episode_end']),
            'episode_step': str(config['episode_step']),
            'filter_rule_id': '',
            'id_type': '1',
            'is_nocopyright': 'false',
            'is_skp_style': 'false',
            'lid': '',
            'list_page_context': '',
            'mvl_strategy_id': '',
            'need_tab': '1',
            'order': '',
            'page_num': str(config['page_num']),
            'page_size': str(config['page_size']),
            'req_from': 'web_vsite',
            'req_from_second_type': '',
            'req_type': '0',
            'siteName': '',
            'tab_type': '1',
            'title_style': '',
            'ui_type': 'null',
            'un_strategy_id': '13dc6f30819942eb805250fb671fb082',
            'watch_together_pay_status': '0',
            'year': ''
        }
        return "&".join([f"{key}={value}" for key, value in context_params.items()])

    async def _fetch_episodes_paginated(self, cid: str, db_media_type: Optional[str] = None) -> List[TencentEpisode]:
        """
        Fallback method to fetch episodes using a simpler, more robust pagination logic.
        This is based on the logic from the provided tx.py script and replaces the previous
        search-based pagination.
        """
        cache_key = f"episodes_v1_{cid}"
        cached_episodes = await self._get_from_cache(cache_key)
        if cached_episodes is not None:
            self.logger.info(f"Tencent (v1 style): 从缓存中命中分集列表 (cid={cid})")
            return [TencentEpisode.model_validate(e) for e in cached_episodes]

        self.logger.info(f"Tencent: Using fallback pagination for cid='{cid}'...")
        
        # 智能请求策略 - 支持到1000集
        request_configs = [
            {'episode_begin': 1, 'episode_end': 100, 'episode_step': 100, 'page_size': 100, 'page_num': 0},
            {'episode_begin': 101, 'episode_end': 200, 'episode_step': 100, 'page_size': 100, 'page_num': 1},
            {'episode_begin': 201, 'episode_end': 300, 'episode_step': 100, 'page_size': 100, 'page_num': 2},
            {'episode_begin': 301, 'episode_end': 400, 'episode_step': 100, 'page_size': 100, 'page_num': 3},
            {'episode_begin': 401, 'episode_end': 500, 'episode_step': 100, 'page_size': 100, 'page_num': 4},
            {'episode_begin': 501, 'episode_end': 600, 'episode_step': 100, 'page_size': 100, 'page_num': 5},
            {'episode_begin': 601, 'episode_end': 700, 'episode_step': 100, 'page_size': 100, 'page_num': 6},
            {'episode_begin': 701, 'episode_end': 800, 'episode_step': 100, 'page_size': 100, 'page_num': 7},
            {'episode_begin': 801, 'episode_end': 900, 'episode_step': 100, 'page_size': 100, 'page_num': 8},
            {'episode_begin': 901, 'episode_end': 1000, 'episode_step': 100, 'page_size': 100, 'page_num': 9}
        ]

        all_episodes: Dict[str, TencentEpisode] = {}

        for i, config in enumerate(request_configs):
            self.logger.debug(f"Tencent (v1 style): 请求页面 {i+1}/{len(request_configs)}...")
            page_context = self._build_page_context(cid, config)
            payload = {
                "page_params": {
                    "req_from": "web_vsite", "page_id": "vsite_episode_list", "page_type": "detail_operation",
                    "id_type": "1", "page_size": "", "cid": cid, "vid": "", "lid": "", "page_num": "",
                    "page_context": page_context, "detail_page_type": "1"
                },
                "has_cache": 1
            }
            try:
                headers = self._get_episode_headers(cid)
                response = await self._request_with_rate_limit("POST", self.episodes_api_url, json=payload, headers=headers)
                if await self._should_log_responses():
                    scraper_responses_logger.debug(f"Tencent Paginated Episodes (v1 style) Response (cid={cid}, page={i}): {response.text}")
                response.raise_for_status()
                result = TencentPageResult.model_validate(response.json())
                if not result.data:
                    self.logger.info(f"Tencent (v1 style): 页面 {i+1} 未返回数据，停止分页。")
                    break

                new_episodes_this_page = 0
                for module_list_data in result.data.module_list_datas:
                    for module_data in module_list_data.module_datas:
                        if module_data.item_data_lists:
                            for item in module_data.item_data_lists.item_datas:
                                if item.item_params and item.item_params.vid:
                                    episode = TencentEpisode.model_validate(item.item_params.model_dump())
                                    if episode.vid not in all_episodes:
                                        all_episodes[episode.vid] = episode
                                        new_episodes_this_page += 1
                
                if new_episodes_this_page == 0:
                    self.logger.info(f"Tencent (v1 style): 页面 {i+1} 未返回新分集，停止分页。")
                    break
                
                await asyncio.sleep(0.3)

            except Exception as e:
                self.logger.error(f"请求分集列表失败 (v1 style, cid={cid}, page={i}): {e}", exc_info=True)
                break

        final_episodes = list(all_episodes.values())
        if final_episodes:
            await self._set_to_cache(cache_key, [e.model_dump() for e in final_episodes], 'episodes_ttl_seconds', 1800)
        return final_episodes

    async def _process_and_format_tencent_episodes(self, tencent_episodes: List[TencentEpisode], db_media_type: Optional[str], cid: str) -> List[models.ProviderEpisodeInfo]:
        """
        将原始腾讯分集列表处理并格式化为通用格式。
        参考了JS实现，为综艺和普通剧集应用不同的、更精确的过滤和排序逻辑。
        """
        # 步骤 1: 初始过滤
        pre_filtered = [ep for ep in tencent_episodes if ep.is_trailer != "1" and ep.vid]

        # 步骤 2: 根据媒体类型应用不同的处理策略
        is_variety_show = False
        episodes_to_format: List[TencentEpisode]

        if db_media_type == 'movie':
            self.logger.info(f"检测到电影类型，正在尝试将正片置于列表首位 (cid={cid})")
            main_feature_vid = await self._get_movie_vid_from_api(cid)

            sorted_episodes = []
            if main_feature_vid:
                main_feature_ep = next((ep for ep in pre_filtered if ep.vid == main_feature_vid), None)
                if main_feature_ep:
                    sorted_episodes.append(main_feature_ep)
                    # 添加所有其他非正片的分集
                    sorted_episodes.extend([ep for ep in pre_filtered if ep.vid != main_feature_vid])
                    self.logger.info(f"已将正片 (vid={main_feature_vid}) 置于列表首位。")
                else:
                    self.logger.warning(f"通过API找到了正片vid '{main_feature_vid}'，但在分集列表中未找到对应条目。")
                    sorted_episodes = pre_filtered
            else:
                self.logger.warning(f"无法通过API获取电影正片vid，将使用原始顺序。")
                sorted_episodes = pre_filtered
            episodes_to_format = sorted_episodes
        else:
            # 步骤 2a: 判断是否为综艺节目 (仅当不是电影时)
            # 修正：增加对日期格式的判断，以更准确地识别综艺节目
            if db_media_type == 'tv_series' or not db_media_type: # 如果类型未知，也进行猜测
                qi_count = sum(1 for ep in pre_filtered if "期" in (ep.union_title or ep.title or ""))
                date_count = sum(1 for ep in pre_filtered if re.search(r'\d{4}-\d{2}-\d{2}', ep.union_title or ep.title or ""))
                if pre_filtered and (qi_count > len(pre_filtered) / 3 or date_count > len(pre_filtered) / 3):
                    is_variety_show = True
            
            # 步骤 2b: 根据类型进行处理
            if is_variety_show:
                self.logger.info("检测到综艺节目，正在应用特殊排序和过滤规则...")
                
                episode_infos = []
                for ep in pre_filtered:
                    title = ep.union_title or ep.title or ""
                    qi_match = re.search(r'第(\d+)期', title)
                    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', title)
                    part_match = re.search(r'([上下])$', title)

                    if qi_match:
                        episode_infos.append({'ep': ep, 'type': 'qi', 'num': int(qi_match.group(1)), 'part': part_match.group(1) if part_match else ''})
                    elif date_match:
                        episode_infos.append({'ep': ep, 'type': 'date', 'num': date_match.group(1), 'part': part_match.group(1) if part_match else ''})
                    else:
                        # 对于无法解析出期数或日期的，标记为 'other'
                        episode_infos.append({'ep': ep, 'type': 'other', 'num': 0, 'part': ''})

                # 排序
                def sort_key_variety(e: Dict) -> Tuple:
                    part_order = {'上': 1, '': 2, '下': 3}
                    if e['type'] == 'qi':
                        return (1, e['num'], part_order.get(e['part'], 99))
                    elif e['type'] == 'date':
                        return (2, e['num'], part_order.get(e['part'], 99))
                    else: # 'other'
                        return (3, 0, 0) # 所有其他类型排在最后
                
                episode_infos.sort(key=sort_key_variety)
                
                # URL去重，选择最佳标题
                url_to_episodes: Dict[str, List[Dict]] = defaultdict(list)
                for info in episode_infos:
                    # 使用 vid 作为唯一键进行去重
                    url_to_episodes[info['ep'].vid].append(info)

                final_ep_infos = []
                for url, infos in url_to_episodes.items():
                    if len(infos) == 1:
                        final_ep_infos.append(infos[0]['ep'])
                    else:
                        # 选择最佳标题：优先不带日期的
                        no_date_infos = [info for info in infos if not re.search(r'\d{4}-\d{2}-\d{2}', info['ep'].union_title or info['ep'].title or "")]
                        best_info = no_date_infos[0] if no_date_infos else infos[0]
                        final_ep_infos.append(best_info['ep'])
                
                episodes_to_format = final_ep_infos
            else:
                # 普通电视剧/动漫处理 (现在只负责排序)
                episodes_to_format = sorted(pre_filtered, key=lambda ep: self._get_episode_index_from_title(ep.union_title or ep.title or "") or float('inf'))

        # 步骤 3: 统一应用黑名单过滤 (包含启发式正片保护)
        # 修正：恢复了启发式规则，以防止黑名单误杀正片。
        # 修正：腾讯视频源只应使用其专属的黑名单，以避免全局规则（如 "版"）误杀正片。
        provider_pattern_str = await self.config_manager.get(
            f"{self.provider_name}_episode_blacklist_regex", self._PROVIDER_SPECIFIC_BLACKLIST_DEFAULT
        )
        blacklist_rules = [provider_pattern_str] if provider_pattern_str else []

        if blacklist_rules:
            original_count = len(episodes_to_format)
            
            temp_episodes = []
            filtered_out_log: Dict[str, List[str]] = defaultdict(list)

            for ep in episodes_to_format:
                title_to_check = ep.union_title or ep.title or ""
                
                # 启发式规则：如果标题是纯数字或包含“第”字，则认为是正片，不应用黑名单。
                # 检查 `ep.title` (通常是纯数字) 是否为数字，或检查 `title_to_check` (完整标题) 是否包含 "第" 字。
                is_likely_main_episode = bool(re.fullmatch(r'\d+', ep.title.strip())) or '第' in title_to_check

                if is_likely_main_episode:
                    temp_episodes.append(ep)
                    continue

                # 如果不是明显的正片，则检查是否匹配黑名单
                match_rule = next((rule for rule in blacklist_rules if rule and re.search(rule, title_to_check, re.IGNORECASE)), None)
                if not match_rule:
                    temp_episodes.append(ep)
                else:
                    filtered_out_log[match_rule].append(title_to_check)
            
            for rule, titles in filtered_out_log.items():
                self.logger.info(f"Tencent: 根据黑名单规则 '{rule}' 过滤掉了 {len(titles)} 个非正片分集: {', '.join(titles)}")

            episodes_to_format = temp_episodes

        # 步骤 3.5: 二次过滤
        # 在初步处理和启发式过滤后，强制应用一次黑名单，以确保如“第x期加更”等内容被彻底移除。
        # 修正：确保二次过滤使用与第一次过滤相同的、完整的黑名单规则列表。
        if blacklist_rules:
            original_count = len(episodes_to_format)

            secondary_filtered_out_log: Dict[str, List[str]] = defaultdict(list)
            
            # 使用一个 lambda 函数来封装过滤逻辑，以避免代码重复
            def is_blacklisted(title: str) -> Optional[str]:
                return next((rule for rule in blacklist_rules if rule and re.search(rule, title, re.IGNORECASE)), None)

            final_filtered_episodes = []
            for ep in episodes_to_format:
                title_to_check = ep.union_title or ep.title or ""
                if match_rule := is_blacklisted(title_to_check):
                    secondary_filtered_out_log[match_rule].append(title_to_check)
                else:
                    final_filtered_episodes.append(ep)

            for rule, titles in secondary_filtered_out_log.items():
                if titles:
                    log_message = f"Tencent: 二次过滤，根据黑名单规则 '{rule}' 过滤掉了 {len(titles)} 个分集:\n"
                    log_message += "\n".join([f"  - {title}" for title in titles])
                    self.logger.info(log_message)
            
            episodes_to_format = final_filtered_episodes

        # 步骤 4: 最终格式化 (后编号)
        final_episodes = []
        for i, ep in enumerate(episodes_to_format):
            display_title = ep.union_title or ep.title or ""
            # 对纯数字标题进行重命名
            if re.fullmatch(r'\d+', display_title.strip()):
                display_title = f"第{display_title}集"
                
            final_episodes.append(models.ProviderEpisodeInfo(
                provider=self.provider_name,
                episodeId=ep.vid,
                title=display_title,
                episodeIndex=i + 1, # 关键：使用过滤后列表的连续索引
                url=f"https://v.qq.com/x/cover/{cid}/{ep.vid}.html"
            ))

        return final_episodes

    async def _internal_get_comments(self, vid: str, progress_callback: Optional[Callable] = None) -> List[TencentComment]:
        """
        获取指定vid的所有弹幕。
        分两步：先获取弹幕分段索引，再逐个获取分段内容。
        """
        all_comments: List[TencentComment] = []
        # 1. 获取弹幕分段索引
        index_url = f"https://dm.video.qq.com/barrage/base/{vid}"
        try:
            response = await self._request_with_rate_limit("GET", index_url)
            if await self._should_log_responses():
                scraper_responses_logger.debug(f"Tencent Danmaku Index Response (vid={vid}): {response.text}")
            response.raise_for_status()
            index_data = response.json()
            segment_index = index_data.get("segment_index", {})
            if not segment_index: # 如果视频没有弹幕，这里会是空的
                self.logger.info(f"vid='{vid}' 没有找到弹幕分段索引。")
                return []
        except Exception as e:
            self.logger.error(f"获取弹幕索引失败 (vid={vid}): {e}", exc_info=True)
            return []

        # 2. 遍历分段，获取弹幕内容
        total_segments = len(segment_index)
        self.logger.debug(f"为 vid='{vid}' 找到 {total_segments} 个弹幕分段，开始获取...")
        if progress_callback:
            await progress_callback(5, f"找到 {total_segments} 个弹幕分段")

        # 与C#代码不同，这里我们直接遍历所有分段以获取全部弹幕，而不是抽样
        # 按key（时间戳）排序，确保弹幕顺序正确
        sorted_keys = sorted(segment_index.keys(), key=int)
        for i, key in enumerate(sorted_keys):
            segment = segment_index[key]
            segment_name = segment.get("segment_name")
            if not segment_name:
                continue
            
            if progress_callback:
                # 5%用于获取索引，90%用于下载，5%用于格式化
                progress = 5 + int(((i + 1) / total_segments) * 90)
                await progress_callback(progress, f"正在下载分段 {i+1}/{total_segments}")

            segment_url = f"https://dm.video.qq.com/barrage/segment/{vid}/{segment_name}"
            try:
                response = await self._request_with_rate_limit("GET", segment_url)
                if await self._should_log_responses():
                    scraper_responses_logger.debug(f"Tencent Danmaku Segment Response (vid={vid}, segment={segment_name}): status={response.status_code}")
                response.raise_for_status()
                comment_data = response.json()
                
                barrage_list = comment_data.get("barrage_list", [])
                for comment_item in barrage_list:
                    try:
                        all_comments.append(TencentComment.model_validate(comment_item))
                    except ValidationError as e:
                        # 腾讯的弹幕列表里有时会混入非弹幕数据（如广告、推荐等），这些数据结构不同
                        # 我们在这里捕获验证错误，记录并跳过这些无效数据，以保证程序健壮性
                        self.logger.warning(f"跳过一个无效的弹幕项目，因为它不符合预期的格式。原始数据: {comment_item}, 错误: {e}")

            except Exception as e:
                self.logger.error(f"获取分段 {segment_name} 失败 (vid={vid}): {e}", exc_info=True)
                continue
        
        if progress_callback:
            await progress_callback(100, "弹幕整合完成")

        self.logger.info(f"vid='{vid}' 弹幕获取完成，共 {len(all_comments)} 条。")
        return all_comments

    async def get_comments(self, episode_id: str, progress_callback: Optional[Callable] = None) -> List[dict]:
        """ 
        获取指定vid的所有弹幕。
        episode_id 对于腾讯来说就是 vid。
        返回一个字典列表，可直接用于批量插入数据库。
        """ 
        tencent_comments = await self._internal_get_comments(episode_id, progress_callback)

        if not tencent_comments:
            return []

        # 新增：按弹幕ID去重
        unique_tencent_comments = list({c.id: c for c in tencent_comments}.values())

        # 1. 按内容对弹幕进行分组
        grouped_by_content: Dict[str, List[TencentComment]] = defaultdict(list)
        for c in unique_tencent_comments: # 使用去重后的列表
            grouped_by_content[c.content].append(c)

        # 2. 处理重复项
        processed_comments: List[TencentComment] = []
        for content, group in grouped_by_content.items():
            if len(group) == 1:
                processed_comments.append(group[0])
            else:
                first_comment = min(group, key=lambda x: int(x.time_offset))
                first_comment.content = f"{first_comment.content} X{len(group)}"
                processed_comments.append(first_comment)

        # 3. 格式化处理后的弹幕列表
        formatted_comments = []
        for c in processed_comments:
            # 默认值
            mode = 1  # 滚动
            color = 16777215  # 白色

            # 增强的样式处理：只有当 content_style 是一个真正的对象时才处理
            if isinstance(c.content_style, TencentCommentContentStyle):
                if c.content_style.position == 2:
                    mode = 5  # 顶部
                elif c.content_style.position == 3:
                    mode = 4  # 底部
                
                # 确定颜色：优先使用渐变色中的第一个颜色，如果存在的话
                target_color_hex = None
                if c.content_style.gradient_colors and len(c.content_style.gradient_colors) > 0:
                    target_color_hex = c.content_style.gradient_colors[0]
                elif c.content_style.color:
                    target_color_hex = c.content_style.color
                
                if target_color_hex:
                    try:
                        # 颜色值是十六进制字符串，需要转换为十进制整数
                        color = int(target_color_hex.lstrip('#'), 16)
                    except (ValueError, TypeError):
                        pass # 转换失败则使用默认白色
            
            # 将字符串类型的 time_offset 转为浮点数秒
            timestamp = int(c.time_offset) / 1000.0
            # 格式: 时间,模式,字体大小,颜色,[来源]
            p_string = f"{timestamp:.2f},{mode},25,{color},[{self.provider_name}]"
            formatted_comments.append({"cid": c.id, "p": p_string, "m": c.content, "t": round(timestamp, 2)})

        return formatted_comments

    async def get_id_from_url(self, url: str) -> Optional[str]:
        """
        从腾讯视频URL中提取视频ID (vid)。
        对于手动导入，我们总是需要 vid 来获取弹幕。
        """
        # 模式1: /cover/{cid}/{vid}.html (最常见，直接包含vid)
        vid_match = re.search(r'/cover/[^/]+/([a-zA-Z0-9]+)\.html', url)
        if vid_match:
            # 检查URL路径段数来确认最后一个部分是vid而不是cid
            from urllib.parse import urlparse
            path_parts = urlparse(url).path.split('/')
            if len(path_parts) >= 5 and path_parts[-2] != 'cover':
                vid = vid_match.group(1)
                self.logger.info(f"Tencent: 从URL {url} 直接解析到 vid: {vid}")
                return vid

        # 模式2: /page/{vid}.html 或 /x/page/{vid}.html (独立视频)
        page_match = re.search(r'/(?:x/)?page/([a-zA-Z0-9]+)\.html', url)
        if page_match:
            vid = page_match.group(1)
            self.logger.info(f"Tencent: 从URL {url} 解析到 vid: {vid}")
            return vid

        # 模式3: /cover/{cid}.html (不含vid的封面页)
        # 这种情况下，我们需要获取分集列表并返回第一集的vid
        cid_match = re.search(r'/cover/([^/]+?)(?:\.html|$)', url)
        if cid_match:
            cid = cid_match.group(1)
            self.logger.info(f"Tencent: URL是封面页 (cid={cid})，正在尝试获取第一集vid...")
            try:
                episodes = await self.get_episodes(media_id=cid)
                if episodes:
                    first_episode_vid = episodes[0].episodeId
                    self.logger.info(f"Tencent: 成功获取到第一集的 vid: {first_episode_vid}")
                    return first_episode_vid
                else:
                    self.logger.warning(f"Tencent: 无法为封面页 (cid={cid}) 获取任何分集。")
            except Exception as e:
                self.logger.error(f"Tencent: 为封面页 (cid={cid}) 获取分集时出错: {e}", exc_info=True)

        self.logger.error(f"Tencent: 无法从URL中解析出有效的视频ID: {url}")
        return None

    def format_episode_id_for_comments(self, provider_episode_id: Any) -> str:
        """For Tencent, the episode ID is a simple string (vid), so no formatting is needed."""
        return str(provider_episode_id)
