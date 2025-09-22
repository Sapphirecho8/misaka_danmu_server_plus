import logging
import time
import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Type, TYPE_CHECKING
from typing import Union
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import crud
from .. import models

if TYPE_CHECKING:
    from ..config_manager import ConfigManager

def _roman_to_int(s: str) -> int:
    """将罗马数字字符串转换为整数。"""
    roman_map = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}
    s = s.upper()
    result = 0
    i = 0
    while i < len(s):
        # 处理减法规则 (e.g., IV, IX)
        if i + 1 < len(s) and roman_map[s[i]] < roman_map[s[i+1]]:
            result += roman_map[s[i+1]] - roman_map[s[i]]
            i += 2
        else:
            result += roman_map[s[i]]
            i += 1
    return result

def get_season_from_title(title: str) -> int:
    """从标题中解析季度信息，返回季度数。"""
    if not title:
        return 1

    # A map for Chinese numerals, including formal and simple.
    chinese_num_map = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
        '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5, '陆': 6, '柒': 7, '捌': 8, '玖': 9, '拾': 10
    }

    # 模式的顺序很重要
    patterns = [
        # 格式: S01, Season 1
        (re.compile(r"(?:S|Season)\s*(\d+)", re.I), lambda m: int(m.group(1))),
        # 格式: 第 X 季/部/幕 (支持中文和阿拉伯数字)
        (re.compile(r"第\s*([一二三四五六七八九十壹贰叁肆伍陆柒捌玖拾\d])\s*[季部幕]", re.I),
         lambda m: chinese_num_map.get(m.group(1)) if not m.group(1).isdigit() else int(m.group(1))),
        # 格式: X之章 (支持简繁中文数字)
        (re.compile(r"([一二三四五六七八九十壹贰叁肆伍陆柒捌玖拾])\s*之\s*章", re.I),
         lambda m: chinese_num_map.get(m.group(1))),
        # 格式: Unicode 罗马数字, e.g., Ⅲ
        (re.compile(r"\s+([Ⅰ-Ⅻ])(?=\s|$)", re.I), 
         lambda m: {'Ⅰ': 1, 'Ⅱ': 2, 'Ⅲ': 3, 'Ⅳ': 4, 'Ⅴ': 5, 'Ⅵ': 6, 'Ⅶ': 7, 'Ⅷ': 8, 'Ⅸ': 9, 'Ⅹ': 10, 'Ⅺ': 11, 'Ⅻ': 12}.get(m.group(1).upper())),
        # 格式: ASCII 罗马数字, e.g., III
        (re.compile(r"\s+([IVXLCDM]+)\b", re.I), lambda m: _roman_to_int(m.group(1))),
    ]

    for pattern, handler in patterns:
        match = pattern.search(title)
        if match:
            try:
                season = handler(match)
                if season is not None: return season
            except (ValueError, KeyError, IndexError):
                continue
    return 1 # Default to season 1

class BaseScraper(ABC):
    """
    所有搜索源的抽象基类。
    定义了搜索媒体、获取分集和获取弹幕的通用接口。
    """
    # 新增：全局搜索结果过滤规则，适用于所有源
    _GLOBAL_SEARCH_JUNK_TITLE_PATTERN = re.compile(
        r'纪录片|预告|花絮|专访|直拍|直播回顾|加更|走心|解忧|纯享|节点|解读|揭秘|赏析|速看|资讯|访谈|番外|短片|'
        r'拍摄花絮|制作花絮|幕后花絮|未播花絮|独家花絮|花絮特辑|'
        r'预告片|先导预告|终极预告|正式预告|官方预告|'
        r'彩蛋片段|删减片段|未播片段|番外彩蛋|'
        r'精彩片段|精彩看点|精彩回顾|精彩集锦|看点解析|看点预告|'
        r'NG镜头|NG花絮|番外篇|番外特辑|'
        r'制作特辑|拍摄特辑|幕后特辑|导演特辑|演员特辑|'
        r'片尾曲|插曲|主题曲|背景音乐|OST|音乐MV|歌曲MV|'
        r'前季回顾|剧情回顾|往期回顾|内容总结|剧情盘点|精选合集|剪辑合集|混剪视频|'
        r'独家专访|演员访谈|导演访谈|主创访谈|媒体采访|发布会采访|'
        r'抢先看|抢先版|试看版|即将上线',
        re.IGNORECASE
    )

    # 新增：全局分集标题过滤规则的默认值
    _GLOBAL_EPISODE_BLACKLIST_DEFAULT = r"^(.*?)((.+?版)|(特(别|典))|((导|演)员|嘉宾|角色)访谈|福利|彩蛋|花絮|预告|特辑|专访|访谈|幕后|周边|资讯|看点|速看|回顾|盘点|合集|PV|MV|CM|OST|ED|OP|BD|特典|SP|NCOP|NCED|MENU|Web-DL|rip|x264|x265|aac|flac)(.*?)$"
    _PROVIDER_SPECIFIC_BLACKLIST_DEFAULT: str = ""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: "ConfigManager"):
        self._session_factory = session_factory
        self.config_manager = config_manager
        self.logger = logging.getLogger(self.__class__.__name__)
        # 新增：用于跟踪当前客户端实例所使用的代理配置
        self._current_proxy_config: Optional[str] = None

    async def _get_proxy_for_provider(self) -> Optional[str]:
        """Helper to get the configured proxy URL for the current provider, if any."""
        proxy_url = await self.config_manager.get("proxyUrl", "")
        proxy_enabled_globally = (await self.config_manager.get("proxyEnabled", "false")).lower() == 'true'

        if not proxy_enabled_globally or not proxy_url:
            return None

        async with self._session_factory() as session:
            scraper_settings = await crud.get_all_scraper_settings(session)
        
        provider_setting = next((s for s in scraper_settings if s['providerName'] == self.provider_name), None)
        use_proxy_for_this_provider = provider_setting.get('useProxy', False) if provider_setting else False

        return proxy_url if use_proxy_for_this_provider else None
    async def _log_proxy_usage(self, proxy_url: Optional[str]):
        if proxy_url:
            self.logger.debug(f"通过代理 '{proxy_url}' 发起请求...")

    async def _create_client(self, **kwargs) -> httpx.AsyncClient: # type: ignore
        """
        创建 httpx.AsyncClient，并根据配置应用代理。
        子类可以传递额外的 httpx.AsyncClient 参数。
        """
        proxy_to_use = await self._get_proxy_for_provider()
        await self._log_proxy_usage(proxy_to_use)
        
        # 关键：在创建客户端后，记录下当前使用的代理配置
        self._current_proxy_config = proxy_to_use
        
        client_kwargs = {"proxy": proxy_to_use, "timeout": 20.0, "follow_redirects": True, **kwargs}
        return httpx.AsyncClient(**client_kwargs)

    async def _get_from_cache(self, key: str) -> Optional[Any]:
        """从数据库缓存中获取数据。"""
        async with self._session_factory() as session:
            return await crud.get_cache(session, key)

    async def _set_to_cache(self, key: str, value: Any, config_key: str, default_ttl: int):
        """将数据存入数据库缓存，TTL从配置中读取。"""
        ttl_str = await self.config_manager.get(config_key, str(default_ttl))
        ttl = int(ttl_str)
        if ttl > 0:
            async with self._session_factory() as session:
                await crud.set_cache(session, key, value, ttl, provider=self.provider_name)

    # 每个子类都必须覆盖这个类属性
    provider_name: str

    # (可选) 子类可以覆盖此字典来声明其可配置的字段
    configurable_fields: Dict[str, str] = {}

    # (新增) 子类应覆盖此列表，声明它们可以处理的域名
    handled_domains: List[str] = []

    # (新增) 子类可以覆盖此属性，以提供一个默认的 Referer
    referer: Optional[str] = None

    # (新增) 子类可以覆盖此属性，以表明其是否支持日志记录
    is_loggable: bool = True

    rate_limit_quota: Optional[int] = None # 新增：特定源的配额
    
    async def _should_log_responses(self) -> bool:
        """动态检查是否应记录原始响应，确保配置实时生效。"""
        if not self.is_loggable:
            return False

        # 修正：使用特定于提供商的配置键，例如 'scraper_tencent_log_responses'
        config_key = f"scraper_{self.provider_name}_log_responses"
        is_enabled_str = await self.config_manager.get(config_key, "false")
        # 健壮性检查：同时处理布尔值和字符串 "true"，以防配置值类型不确定。
        if isinstance(is_enabled_str, bool):
            return is_enabled_str
        return str(is_enabled_str).lower() == 'true'

    async def get_episode_blacklist_pattern(self) -> Optional[re.Pattern]:
        """
        获取最终用于过滤分集标题的正则表达式对象。
        它会合并全局黑名单和特定于提供商的黑名单。
        """
        # 1. 获取全局黑名单，如果用户未配置，则使用内置默认值
        global_pattern_str = await self.config_manager.get(
            "episode_blacklist_regex",
            self._GLOBAL_EPISODE_BLACKLIST_DEFAULT
        )

        # 2. 获取特定于提供商的黑名单
        provider_key = f"{self.provider_name}_episode_blacklist_regex"
        # 注意：这里不提供默认值。如果数据库中没有（即用户从未保存过，且启动时也未注册上），
        # 则返回 None，我们只使用全局黑名单。
        provider_pattern_str = await self.config_manager.get(provider_key, None)

        # 3. 合并两个正则表达式
        final_patterns = []
        if global_pattern_str and global_pattern_str.strip():
            final_patterns.append(f"({global_pattern_str})")
        
        if provider_pattern_str and provider_pattern_str.strip():
            final_patterns.append(f"({provider_pattern_str})")

        if not final_patterns:
            return None

        final_regex_str = "|".join(final_patterns)
        try:
            return re.compile(final_regex_str, re.IGNORECASE)
        except re.error as e:
            self.logger.error(f"编译分集黑名单正则表达式失败: '{final_regex_str}'. 错误: {e}")
        return None

    async def execute_action(self, action_name: str, payload: Dict[str, Any]) -> Any:
        """
        执行一个指定的操作。
        子类应重写此方法来处理其声明的操作。
        :param action_name: 要执行的操作的名称。
        :param payload: 包含操作所需参数的字典。
        """
        raise NotImplementedError(f"操作 '{action_name}' 在 {self.provider_name} 中未实现。")

    @abstractmethod
    async def search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """
        根据关键词搜索媒体。
        episode_info: 可选字典，包含 'season' 和 'episode'。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_info_from_url(self, url: str) -> Optional[models.ProviderSearchInfo]:
        """
        (新增) 从一个作品的URL中提取信息，并返回一个 ProviderSearchInfo 对象。
        这用于支持从URL直接导入整个作品。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_id_from_url(self, url: str) -> Optional[Union[str, Dict[str, str]]]:
        """
        (新增) 统一的从URL解析ID的接口。
        子类应重写此方法以支持从URL直接导入。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_episodes(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        """
        获取给定媒体ID的所有分集。
        如果提供了 target_episode_index，则可以优化为只获取到该分集为止。
        db_media_type: 从数据库中读取的媒体类型 ('movie', 'tv_series')，可用于指导刮削策略。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_comments(self, episode_id: str, progress_callback: Optional[Callable] = None) -> List[dict]:
        """
        获取给定分集ID的所有弹幕。
        返回的字典列表应与 crud.bulk_insert_comments 的期望格式兼容。
        """
        raise NotImplementedError

    def format_episode_id_for_comments(self, provider_episode_id: Any) -> str:
        """
        (新增) 将 get_comments 所需的 episode_id 格式化为字符串。
        大多数源直接返回字符串，但Bilibili和MGTV需要特殊处理。
        """
        return str(provider_episode_id)

    @abstractmethod
    async def close(self):
        """关闭所有打开的资源，例如HTTP客户端。"""
        pass