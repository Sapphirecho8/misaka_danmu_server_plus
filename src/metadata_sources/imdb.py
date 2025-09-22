import asyncio
import logging
import re
import json
from typing import Any, Dict, List, Optional, Set

import httpx
from pydantic import BaseModel, Field
from fastapi import HTTPException, status

from .. import models
from .. import crud
from .base import BaseMetadataSource, HTTPStatusError

logger = logging.getLogger(__name__)

# --- Pydantic Models for IMDb JSON API ---
class ImdbApiImage(BaseModel):
    height: int
    imageUrl: str
    width: int

class ImdbApiResultItem(BaseModel):
    id: str
    l: str  # title
    q: Optional[str] = None  # type like "feature"
    s: Optional[str] = None  # actors
    y: Optional[int] = None  # year
    i: Optional[ImdbApiImage] = None

class ImdbApiResponse(BaseModel):
    d: List[ImdbApiResultItem] = []


class ImdbMetadataSource(BaseMetadataSource):
    provider_name = "imdb"
    test_url = "https://www.imdb.com"

    async def _create_client(self) -> httpx.AsyncClient:
        """Creates an httpx.AsyncClient with IMDb headers and proxy settings."""
        proxy_url = await self.config_manager.get("proxyUrl", "")
        proxy_enabled_globally = (await self.config_manager.get("proxyEnabled", "false")).lower() == 'true'

        async with self._session_factory() as session:
            metadata_settings = await crud.get_all_metadata_source_settings(session)

        provider_setting = next((s for s in metadata_settings if s['providerName'] == self.provider_name), None)
        use_proxy_for_this_provider = provider_setting.get('use_proxy', False) if provider_setting else False

        proxy_to_use = proxy_url if proxy_enabled_globally and use_proxy_for_this_provider and proxy_url else None
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        }
        return httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True, proxy=proxy_to_use)

    async def search(self, keyword: str, user: models.User, mediaType: Optional[str] = None) -> List[models.MetadataDetailsResponse]:
        self.logger.info(f"IMDb: 正在使用JSON API搜索 '{keyword}'")
        formatted_keyword = keyword.strip().lower()
        if not formatted_keyword:
            return []
        
        search_url = f"https://v3.sg.media-imdb.com/suggestion/titles/x/{formatted_keyword}.json"
        try:
            async with await self._create_client() as client:
                response = await client.get(search_url)
                response.raise_for_status()
                data = ImdbApiResponse.model_validate(response.json())

                results = []
                for item in data.d:
                    if item.q not in ["feature", "tvSeries", "tvMovie", "tvMiniSeries", "video", "tvSpecial"]:
                        continue
                    
                    details_parts = []
                    if item.y:
                        details_parts.append(f"年份: {item.y}")
                    if item.s:
                        details_parts.append(f"演员: {item.s}")
                    
                    results.append(models.MetadataDetailsResponse(
                        id=item.id, imdbId=item.id, title=item.l,
                        details=" / ".join(details_parts),
                        imageUrl=item.i.imageUrl if item.i else None
                    ))
                return results
        except httpx.ConnectError as e:
            self.logger.error(f"IMDb API 搜索失败: 无法连接到服务器。 {e}", exc_info=True)
            raise HTTPException(status_code=503, detail="无法连接到 IMDb 服务，请检查网络或代理设置。")
        except httpx.TimeoutException as e:
            self.logger.error(f"IMDb API 搜索失败: 请求超时。 {e}", exc_info=True)
            raise HTTPException(status_code=504, detail="连接 IMDb 服务超时。")
        except httpx.HTTPStatusError as e:
            self.logger.error(f"IMDb API 搜索失败: HTTP 状态码 {e.response.status_code}。响应: {e.response.text[:200]}", exc_info=True)
            raise HTTPException(status_code=502, detail=f"IMDb 服务返回错误状态码: {e.response.status_code}")
        except Exception as e:
            self.logger.error(f"IMDb API 搜索失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"IMDb API 搜索失败: {e}")

    async def get_details(self, item_id: str, user: models.User, mediaType: Optional[str] = None) -> Optional[models.MetadataDetailsResponse]:
        self.logger.info(f"IMDb: 正在获取详情 item_id={item_id}")
        details_url = f"https://www.imdb.com/title/{item_id}/"
        try:
            async with await self._create_client() as client:
                response = await client.get(details_url)
                response.raise_for_status()
                html = response.text

                name_en: Optional[str] = None
                aliases_cn: List[str] = []

                next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
                if next_data_match:
                    try:
                        next_data = json.loads(next_data_match.group(1))
                        main_data = next_data.get("props", {}).get("pageProps", {}).get("mainColumnData", {})

                        name_en = main_data.get("titleText", {}).get("text")
                        original_title = main_data.get("originalTitleText", {}).get("text")

                        akas = main_data.get("akas", {})
                        if akas and akas.get("edges"):
                            for edge in akas["edges"]:
                                node = edge.get("node", {})
                                if node.get("text"):
                                    aliases_cn.append(node["text"])

                        if name_en and original_title and name_en != original_title:
                            aliases_cn.append(original_title)

                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        self.logger.warning(f"解析 IMDb __NEXT_DATA__ 失败，将回退到正则匹配。错误: {e}")
                        name_en = None # 重置 name_en 以确保触发回退

                # 如果 __NEXT_DATA__ 解析失败或未找到标题，则回退到正则
                if not name_en:
                    self.logger.info(f"IMDb: 正在为 item_id={item_id} 使用正则回退方案。")
                    title_match = re.search(r'<h1.*?><span.*?>(.*?)</span></h1>', html)
                    name_en = title_match.group(1).strip() if title_match else None

                    # 仅在回退模式下使用正则解析别名
                    akas_section_match = re.search(r'<div data-testid="akas".*?>(.*?)</div>', html, re.DOTALL)
                    if akas_section_match:
                        alias_matches = re.findall(r'<li.*?<a.*?>(.*?)</a>', akas_section_match.group(1), re.DOTALL)
                        aliases_cn.extend([alias.strip() for alias in alias_matches])

                # 在创建响应对象之前进行最终检查
                if not name_en:
                    self.logger.error(f"IMDb: 无法从详情页解析出标题 (item_id={item_id})")
                    return None

                return models.MetadataDetailsResponse(
                    id=item_id, imdbId=item_id, title=name_en,
                    nameEn=name_en,
                    aliasesCn=list(dict.fromkeys(filter(None, aliases_cn)))
                )
        except Exception as e:
            self.logger.error(f"解析 IMDb 详情页时发生错误: {e}", exc_info=True)
            return None

    async def search_aliases(self, keyword: str, user: models.User) -> Set[str]:
        self.logger.info(f"IMDb: 正在为 '{keyword}' 搜索别名")
        local_aliases: Set[str] = set()
        try:
            search_results = await self.search(keyword, user)
            if not search_results:
                return local_aliases

            best_match_id = search_results[0].id
            details = await self.get_details(best_match_id, user)

            if details:
                if details.nameEn:
                    local_aliases.add(details.nameEn)
                if details.aliasesCn:
                    local_aliases.update(details.aliasesCn)

            self.logger.info(f"IMDb辅助搜索成功，找到别名: {[a for a in local_aliases if a]}")
        except Exception as e:
            self.logger.warning(f"IMDb辅助搜索失败: {e}")

        return {alias for alias in local_aliases if alias}

    async def check_connectivity(self) -> str:
        try:
            # 修正：在创建客户端之前就确定是否使用代理，以避免AttributeError
            proxy_url = await self.config_manager.get("proxyUrl", "")
            proxy_enabled_globally = (await self.config_manager.get("proxyEnabled", "false")).lower() == 'true'
            async with self._session_factory() as session:
                metadata_settings = await crud.get_all_metadata_source_settings(session)
            provider_setting = next((s for s in metadata_settings if s['providerName'] == self.provider_name), None)
            use_proxy_for_this_provider = provider_setting.get('useProxy', False) if provider_setting else False
            is_using_proxy = proxy_enabled_globally and use_proxy_for_this_provider and proxy_url
            if is_using_proxy:
                self.logger.debug(f"IMDb: 连接性检查将使用代理: {proxy_url}")
            async with await self._create_client() as client:
                response = await client.get("https://www.imdb.com", timeout=10.0)
                if response.status_code == 200:
                    return "通过代理连接成功" if is_using_proxy else "连接成功"
                else:
                    return f"通过代理连接失败 ({response.status_code})" if is_using_proxy else f"连接失败 ({response.status_code})"
        except Exception as e:
            return f"连接失败: {e}" # 代理信息已包含在异常中

    async def execute_action(self, action_name: str, payload: Dict, user: models.User) -> Any:
        """IMDb source does not support custom actions."""
        raise NotImplementedError(f"源 '{self.provider_name}' 不支持任何自定义操作。")