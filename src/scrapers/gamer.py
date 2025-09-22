import asyncio
import json
import logging
import re
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import httpx
from bs4 import BeautifulSoup
from opencc import OpenCC

from .. import crud, models
from ..config_manager import ConfigManager
from ..utils import parse_search_keyword
from .base import BaseScraper, get_season_from_title

scraper_responses_logger = logging.getLogger("scraper_responses")


class GamerScraper(BaseScraper):
    provider_name = "gamer"
    handled_domains = ["ani.gamer.com.tw"]
    referer = "https://ani.gamer.com.tw/"
    test_url = "https://ani.gamer.com.tw"
    configurable_fields = {
        "gamerCookie": "巴哈姆特动画疯 Cookie",
        "gamerUserAgent": "巴哈姆特动画疯 User-Agent",
    }
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager):
        super().__init__(session_factory, config_manager)
        self.cc_s2t = OpenCC('s2twp')  # Simplified to Traditional Chinese with phrases
        self.cc_t2s = OpenCC('t2s') # Traditional to Simplified
        self.client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Ensures the httpx client is initialized, with proxy support."""
        # 检查代理配置是否发生变化
        new_proxy_config = await self._get_proxy_for_provider()
        if self.client and new_proxy_config != self._current_proxy_config:
            self.logger.info("Gamer: 代理配置已更改，正在重建HTTP客户端...")
            await self.client.aclose()
            self.client = None

        if self.client is None:
            headers = {"User-Agent": "Mozilla/5.0"} # 初始默认值
            self.client = await self._create_client(headers=headers, timeout=20.0, follow_redirects=True)
        
        # 动态加载并更新最新的 Cookie 和 User-Agent
        cookie = await self.config_manager.get("gamerCookie", "")
        user_agent = await self.config_manager.get("gamerUserAgent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        self.client.headers["User-Agent"] = user_agent
        self.client.cookies.clear()
        if cookie:
            self.client.cookies.update(httpx.Cookies({k.strip(): v.strip() for k, v in (p.split('=', 1) for p in cookie.split(';') if '=' in p)}))
        
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

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """一个简单的请求包装器。"""
        client = await self._ensure_client()
        response = await client.request(method, url, **kwargs)
        if await self._should_log_responses():
            # 截断HTML以避免日志过长
            scraper_responses_logger.debug(f"Gamer Response ({method} {url}): status={response.status_code}, text={response.text[:500]}")
        return response

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def search(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """
        Performs a cached search for Gamer content.
        It caches the base results for a title and then filters them based on season.
        """
        
        parsed = parse_search_keyword(keyword)
        search_title = parsed['title']
        search_season = parsed['season']
        
        trad_search_title = self.cc_s2t.convert(search_title)
        cache_key = f"search_base_{self.provider_name}_{trad_search_title}"
        cached_results = await self._get_from_cache(cache_key)

        if cached_results:
            self.logger.info(f"Gamer: 从缓存中命中基础搜索结果 (title='{search_title}')")
            all_results = [models.ProviderSearchInfo.model_validate(r) for r in cached_results]
        else:
            self.logger.info(f"Gamer: 缓存未命中，正在为标题 '{search_title}' (繁体: '{trad_search_title}') 执行网络搜索...")
            all_results = await self._perform_network_search(trad_search_title, episode_info)
            if all_results:
                await self._set_to_cache(cache_key, [r.model_dump() for r in all_results], 'search_ttl_seconds', 3600)

        if search_season is None:
            return all_results

        # Filter results by season
        final_results = [item for item in all_results if item.season == search_season]
        self.logger.info(f"Gamer: 为 S{search_season} 过滤后，剩下 {len(final_results)} 个结果。")
        return final_results

    async def _perform_network_search(self, trad_keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[models.ProviderSearchInfo]:
        """Performs the actual network search for Gamer."""
        url = "https://ani.gamer.com.tw/search.php"
        params = {"keyword": trad_keyword}
        
        try:
            response = await self._request("GET", url, params=params)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            search_content = soup.find("div", class_="animate-theme-list")
            if not search_content:
                self.logger.warning("Gamer: 未找到主要的 animate-theme-list 容器。")
                return []

            results = []
            for item in search_content.find_all("a", class_="theme-list-main"):
                href = item.get("href")
                sn_match = re.search(r"animeRef\.php\?sn=(\d+)", href)
                if not sn_match:
                    continue
                
                media_id = sn_match.group(1)
                # 修正：巴哈姆特的页面结构已更改，标题现在位于 <p> 标签中。
                title_tag = item.find("p", class_="theme-name")
                if not title_tag:
                    self.logger.warning(f"Gamer: 无法为 media_id={media_id} 解析标题。对应的HTML片段: {item}")
                # 即使找不到标题，也继续处理，但标题会是“未知标题”
                title_trad = title_tag.text.strip() if title_tag else "未知标题"
                title_simp = self.cc_t2s.convert(title_trad)
                
                # 新增：提取年份、集数和海报
                year = None
                time_tag = item.find("p", class_="theme-time")
                if time_tag and time_tag.text:
                    year_match = re.search(r'(\d{4})', time_tag.text)
                    if year_match:
                        year = int(year_match.group(1))

                episode_count = None
                number_tag = item.find("span", class_="theme-number")
                if number_tag and number_tag.text:
                    ep_count_match = re.search(r'(\d+)', number_tag.text)
                    if ep_count_match:
                        episode_count = int(ep_count_match.group(1))

                image_url = None
                img_tag = item.find("img", class_="theme-img")
                if img_tag and img_tag.get("data-src"):
                    image_url = img_tag["data-src"]

                # 根据集数判断媒体类型
                media_type = "movie" if episode_count == 1 else "tv_series"
                
                provider_search_info = models.ProviderSearchInfo(
                    provider=self.provider_name, mediaId=media_id, title=title_simp,
                    type=media_type, season=get_season_from_title(title_simp),
                    year=year,
                    imageUrl=image_url,
                    episodeCount=episode_count,
                    currentEpisodeIndex=episode_info.get("episode") if episode_info else None
                )
                results.append(provider_search_info)
            
            self.logger.info(f"Gamer: 网络搜索 '{trad_keyword}' 完成，找到 {len(results)} 个结果。")
            if results:
                log_results = "\n".join([f"  - {r.title} (ID: {r.mediaId}, 类型: {r.type}, 年份: {r.year or 'N/A'})" for r in results])
                self.logger.info(f"Gamer: 搜索结果列表:\n{log_results}")
            return results

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            self.logger.warning(f"Gamer: 搜索 '{trad_keyword}' 时连接超时或网络错误: {e}")
            return []
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                self.logger.error(f"Gamer: 搜索 '{trad_keyword}' 失败 (403 Forbidden)。这通常是由于无效或过期的 Cookie 导致的。请尝试在“搜索源”设置中更新巴哈姆特动画疯的 Cookie。")
            else:
                self.logger.error(f"Gamer: 搜索 '{trad_keyword}' 时发生 HTTP 错误: {e}", exc_info=True)
            return []
        except Exception as e:
            self.logger.error(f"Gamer: 搜索 '{trad_keyword}' 时发生未知错误: {e}", exc_info=True)
            return []

    async def get_info_from_url(self, url: str) -> Optional[models.ProviderSearchInfo]:
        """从动画疯URL中提取作品信息。"""
        await self._ensure_client()
        self.logger.info(f"Gamer: 正在从URL提取信息: {url}")

        sn_match = re.search(r"sn=(\d+)", url)
        if not sn_match:
            self.logger.warning(f"Gamer: 无法从URL中解析出sn号: {url}")
            return None
        
        sn = sn_match.group(1)

        try:
            response = await self._request("GET", url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
        except Exception as e:
            self.logger.error(f"Gamer: 访问URL失败 {url}: {e}", exc_info=True)
            return None

        # 检查是否是分集页面，如果是，则找到系列页面的链接
        media_id = sn
        if "animeVideo.php" in url:
            back_to_list_link = soup.select_one(".v-info__title a[href*='animeRef.php']")
            if back_to_list_link:
                series_sn_match = re.search(r"sn=(\d+)", back_to_list_link['href'])
                if series_sn_match:
                    media_id = series_sn_match.group(1)
                    self.logger.info(f"Gamer: 从分集页面 (sn={sn}) 找到系列ID: {media_id}")
                    # 获取系列页面的内容
                    series_url = f"https://ani.gamer.com.tw/animeRef.php?sn={media_id}"
                    try:
                        response = await self._request("GET", series_url)
                        response.raise_for_status()
                        soup = BeautifulSoup(response.text, "lxml")
                    except Exception as e:
                        self.logger.error(f"Gamer: 访问系列页面失败 {series_url}: {e}", exc_info=True)
                        return None
                else:
                    self.logger.warning(f"Gamer: 在分集页面上找到了返回链接，但无法解析出系列sn号。")
                    return None
        
        try:
            title_tag = soup.select_one(".anime_name h1")
            if not title_tag:
                self.logger.error(f"Gamer: 无法从系列页面 (sn={media_id}) 解析标题。")
                return None
            title_trad = title_tag.text.strip()
            title_simp = self.cc_t2s.convert(title_trad)

            image_url = None
            img_tag = soup.select_one(".anime_info_pic img")
            if img_tag:
                image_url = img_tag.get("src")

            episode_count = len(soup.select(".season a[href*='animeVideo.php']"))
            media_type = "movie" if episode_count == 1 else "tv_series"

            return models.ProviderSearchInfo(
                provider=self.provider_name, mediaId=media_id, title=title_simp,
                type=media_type, season=get_season_from_title(title_simp),
                imageUrl=image_url, episodeCount=episode_count
            )
        except Exception as e:
            self.logger.error(f"Gamer: 解析系列页面 (sn={media_id}) 时发生错误: {e}", exc_info=True)
            return None

    async def get_id_from_url(self, url: str) -> Optional[str]:
        """从动画疯URL中提取sn号。"""
        sn_match = re.search(r"sn=(\d+)", url)
        if sn_match: return sn_match.group(1)
        return None

    def format_episode_id_for_comments(self, provider_episode_id: Any) -> str:
        """For Gamer, the episode ID is a simple string (sn), so no formatting is needed."""
        return str(provider_episode_id)

    async def get_episodes(self, media_id: str, target_episode_index: Optional[int] = None, db_media_type: Optional[str] = None) -> List[models.ProviderEpisodeInfo]:
        await self._ensure_client()
        self.logger.info(f"Gamer: 正在为 media_id={media_id} 获取分集列表...")
        
        # 修正：直接请求作品集页面(animeRef.php)，而不是依赖于播放页(animeVideo.php)的重定向，这与Lua脚本的逻辑一致，更健壮。
        url = f"https://ani.gamer.com.tw/animeRef.php?sn={media_id}"
        
        try:
            response = await self._request("GET", url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            raw_episodes = []
            season_section = soup.find("section", class_="season")
            if season_section:
                ep_links = season_section.find_all("a")
                for link in ep_links:
                    raw_episodes.append({'link': link, 'title': link.text.strip()})
            else:
                script_content = soup.find("script", string=re.compile("animefun.videoSn"))
                if script_content:
                    sn_match = re.search(r"animefun.videoSn\s*=\s*(\d+);", script_content.string)
                    title_match = re.search(r"animefun.title\s*=\s*'([^']+)';", script_content.string)
                    if sn_match and title_match:
                        raw_episodes.append({'link': None, 'sn': sn_match.group(1), 'title': title_match.group(1)})

            # 统一过滤逻辑
            # 修正：安全地获取并组合黑名单规则
            # 修正：Gamer源只应使用其专属的黑名单，以避免全局规则误杀。
            provider_pattern_str = await self.config_manager.get(f"{self.provider_name}_episode_blacklist_regex", self._PROVIDER_SPECIFIC_BLACKLIST_DEFAULT)
            blacklist_rules = [provider_pattern_str] if provider_pattern_str else []

            if blacklist_rules:
                temp_episodes = []
                filtered_out_log: Dict[str, List[str]] = defaultdict(list)
                for ep in raw_episodes:
                    title_to_check = ep['title']
                    match_rule = next((rule for rule in blacklist_rules if rule and re.search(rule, title_to_check, re.IGNORECASE)), None)
                    if not match_rule:
                        temp_episodes.append(ep)
                    else:
                        filtered_out_log[match_rule].append(title_to_check)
                for rule, titles in filtered_out_log.items():
                    self.logger.info(f"Gamer: 根据黑名单规则 '{rule}' 过滤掉了 {len(titles)} 个分集: {', '.join(titles)}")
                raw_episodes = temp_episodes

            # 过滤后再编号
            episodes = []
            for i, raw_ep in enumerate(raw_episodes):
                if raw_ep.get('link'):
                    href = raw_ep['link'].get("href")
                    sn_match = re.search(r"\?sn=(\d+)", href)
                    if not sn_match: continue
                    episodes.append(models.ProviderEpisodeInfo(
                        provider=self.provider_name, episodeId=sn_match.group(1), title=self.cc_t2s.convert(raw_ep['title']),
                        episodeIndex=i + 1, url=f"https://ani.gamer.com.tw{href}"
                    ))
                else: # 单集视频
                    episodes.append(models.ProviderEpisodeInfo(
                        provider=self.provider_name, episodeId=raw_ep['sn'], title=self.cc_t2s.convert(raw_ep['title']),
                        episodeIndex=1, url=f"https://ani.gamer.com.tw/animeVideo.php?sn={raw_ep['sn']}"
                    ))

            if target_episode_index:
                return [ep for ep in episodes if ep.episodeIndex == target_episode_index]
            
            return episodes

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            self.logger.warning(f"Gamer: 获取分集列表失败 (media_id={media_id})，连接超时或网络错误: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Gamer: 获取分集列表失败 (media_id={media_id}): {e}", exc_info=True)
            return []

    async def get_comments(self, episode_id: str, progress_callback: Optional[Callable] = None) -> List[dict]:
        await self._ensure_client()
        self.logger.info(f"Gamer: 正在为 episode_id={episode_id} 获取弹幕...")
        
        url = "https://ani.gamer.com.tw/ajax/danmuGet.php"
        data = {"sn": episode_id}
        
        try:
            if progress_callback: await progress_callback(10, "正在请求弹幕数据...")
            
            response = await self._request("POST", url, data=data)
            danmu_data = response.json()

            if not isinstance(danmu_data, list):
                self.logger.error(f"Gamer: 刷新Cookie后，弹幕API仍未返回列表 (episode_id={episode_id})")
                return []

            if progress_callback: await progress_callback(50, f"收到 {len(danmu_data)} 条原始弹幕，正在处理...")

            # 新增：按 'sn' (弹幕流水号) 去重
            unique_danmu_map: Dict[str, Dict] = {}
            for c in danmu_data:
                sn = c.get("sn")
                if sn and sn not in unique_danmu_map:
                    unique_danmu_map[sn] = c
            
            unique_danmu_list = list(unique_danmu_map.values())

            # 像Lua脚本一样处理重复弹幕
            grouped_by_content: Dict[str, List[Dict]] = defaultdict(list)
            for c in unique_danmu_list: # 使用去重后的列表
                grouped_by_content[c.get("text")].append(c)

            processed_comments: List[Dict] = []
            for content, group in grouped_by_content.items():
                if len(group) == 1:
                    processed_comments.append(group[0])
                else:
                    first_comment = min(group, key=lambda x: float(x.get("time", 0)))
                    first_comment["text"] = f"{first_comment.get('text', '')} X{len(group)}"
                    processed_comments.append(first_comment)

            formatted_comments = []
            for comment in processed_comments:
                try:
                    text = comment.get("text")
                    # 修正：巴哈姆特API返回的时间单位是十分之一秒，需要除以10
                    time_sec = float(comment.get("time", 0)) / 10.0
                    pos = int(comment.get("position", 0))
                    hex_color = comment.get("color", "#ffffff")
                    
                    mode = 1  # 1: scroll
                    if pos == 1: mode = 5  # 5: top
                    elif pos == 2: mode = 4  # 4: bottom
                    
                    color = int(hex_color.lstrip('#'), 16)
                    
                    # 修正：直接在此处添加字体大小 '25'，确保数据源的正确性
                    p_string = f"{time_sec:.2f},{mode},25,{color},[{self.provider_name}]"
                    
                    formatted_comments.append({
                        # 修正：使用 'sn' (弹幕流水号) 作为唯一的弹幕ID (cid)，而不是 'userid'，以避免同一用户发送多条弹幕时出现重复键错误。
                        "cid": str(comment.get("sn", "0")),
                        "p": p_string,
                        "m": text, # 移除采集时的转换，保持数据原始性
                        "t": round(time_sec, 2)
                    })
                except (ValueError, TypeError) as e:
                    self.logger.warning(f"Gamer: 跳过一条格式错误的弹幕: {comment}, 错误: {e}")
                    continue
            
            if progress_callback: await progress_callback(100, "弹幕处理完成")
            return formatted_comments

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            self.logger.warning(f"Gamer: 获取弹幕失败 (episode_id={episode_id})，连接超时或网络错误: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Gamer: 获取弹幕失败 (episode_id={episode_id}): {e}", exc_info=True)
            return []
