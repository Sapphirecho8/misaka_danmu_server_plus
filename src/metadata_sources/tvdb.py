import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import httpx
from fastapi import HTTPException, status

from .. import crud, models
from .base import BaseMetadataSource, HTTPStatusError

logger = logging.getLogger(__name__)

class TvdbMetadataSource(BaseMetadataSource):
    provider_name = "tvdb"
    test_url = "https://api4.thetvdb.com"

    async def _get_tvdb_token(self, client: httpx.AsyncClient) -> str:
        """获取一个有效的TVDB令牌，如果需要则从数据库或API刷新。"""
        # 1. 尝试从数据库配置中获取缓存的token和过期时间
        token = await self.config_manager.get("tvdbJwtToken")
        expires_at_str = await self.config_manager.get("tvdbTokenExpiresAt")

        if token and expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                if expires_at > datetime.utcnow():
                    self.logger.debug("TVDB: 使用数据库中缓存的有效token。")
                    return token
            except ValueError:
                self.logger.warning("TVDB: 数据库中的过期时间格式无效，将重新获取token。")

        self.logger.info("TVDB token 已过期或未找到，正在请求新的令牌。")
        api_key = await self.config_manager.get("tvdbApiKey", "")
        if not api_key:
            raise ValueError("TVDB API Key 未配置。")

        try:
            # 登录端点不需要认证头
            login_response = await client.post("/login", json={"apikey": api_key})
            login_response.raise_for_status()
            new_token = login_response.json().get("data", {}).get("token")
            if not new_token:
                raise ValueError("登录响应中未包含令牌。")

            # 令牌有效期为一个月，我们设置一个29天的缓存
            new_expires_at = datetime.utcnow() + timedelta(days=29)
            await self.config_manager.setValue("tvdbJwtToken", new_token)
            await self.config_manager.setValue("tvdbTokenExpiresAt", new_expires_at.isoformat())
            
            self.logger.info("成功获取并缓存新的TVDB令牌。")
            return new_token
        except Exception as e:
            self.logger.error(f"获取TVDB令牌失败: {e}", exc_info=True)
            raise ValueError("TVDB认证失败。")

    async def _create_client(self) -> httpx.AsyncClient:
        # 1. 获取代理配置
        async with self._session_factory() as session:
            proxy_url = await crud.get_config_value(session, "proxyUrl", "")
            proxy_enabled_str = await crud.get_config_value(session, "proxyEnabled", "false")
            proxy_enabled_globally = proxy_enabled_str.lower() == 'true'
            metadata_settings = await crud.get_all_metadata_source_settings(session)
        provider_setting = next((s for s in metadata_settings if s['providerName'] == self.provider_name), None)
        use_proxy_for_this_provider = provider_setting.get('use_proxy', False) if provider_setting else False

        proxy_to_use = proxy_url if proxy_enabled_globally and use_proxy_for_this_provider and proxy_url else None

        # 2. 创建一个基础客户端用于登录
        base_client = httpx.AsyncClient(base_url="https://api4.thetvdb.com/v4", timeout=20.0, follow_redirects=True, proxy=proxy_to_use)

        # 3. 使用基础客户端获取认证Token
        token = await self._get_tvdb_token(base_client)

        # 4. 将认证Token更新到客户端的请求头中
        base_client.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": "DanmuApiServer/1.0"
        })
        return base_client

    async def search(self, keyword: str, user: models.User, mediaType: Optional[str] = None) -> List[models.MetadataDetailsResponse]:
        try:
            async with await self._create_client() as client:
                params = {"query": keyword}
                if mediaType:
                    params["type"] = mediaType

                response = await client.get("/search", params=params)
                response.raise_for_status()
                data = response.json().get("data", [])

                results = []
                for item in data:
                    results.append(models.MetadataDetailsResponse(
                        id=item['tvdb_id'], tvdbId=item['tvdb_id'],
                        title=item.get('name'), imageUrl=item.get('image_url'),
                        details=f"Year: {item.get('year')}",
                        type="tv_series" if item.get("type") == "series" else item.get("type", "other")
                    ))
                return results
        except ValueError as e:
            self.logger.error(f"TVDB搜索失败，配置错误: {e}")
            return []
        except httpx.HTTPStatusError as e:
            self.logger.error(f"TVDB搜索失败，HTTP错误: {e.response.status_code} for URL: {e.request.url}")
            return []
        except Exception as e:
            self.logger.error(f"TVDB搜索失败，发生意外错误: {e}", exc_info=True)
            return []

    async def get_details(self, item_id: str, user: models.User, mediaType: Optional[str] = None) -> Optional[models.MetadataDetailsResponse]:
        try:
            async with await self._create_client() as client:
                async def _fetch_and_parse(entity_type: str) -> Optional[models.MetadataDetailsResponse]:
                    try:
                        self.logger.info(f"TVDB: 正在尝试将ID {item_id} 作为 '{entity_type}' 获取...")
                        response = await client.get(f"/{entity_type}/{item_id}/extended")
                        if response.status_code == 404:
                            self.logger.debug(f"TVDB: ID {item_id} 未被找到为 '{entity_type}'。")
                            return None
                        response.raise_for_status()
                        details = response.json().get("data", {})
                        if not details: return None
                        imdb_id = None
                        if remote_ids := details.get('remoteIds'):
                            imdb_entry = next((rid for rid in remote_ids if rid.get('sourceName') == 'IMDB'), None)
                            if imdb_entry: imdb_id = imdb_entry.get('id')
                        return models.MetadataDetailsResponse(
                            id=str(details['id']), tvdbId=str(details['id']), title=details.get('name'),
                            imageUrl=details.get('image'), details=details.get('overview'), imdbId=imdb_id,
                            type='movie' if entity_type == 'movies' else 'tv_series',
                            year=int(details['year']) if details.get('year') and details['year'].isdigit() else None
                        )
                    except httpx.HTTPStatusError as e:
                        self.logger.error(f"TVDB: 获取 {entity_type} (ID: {item_id}) 时发生HTTP错误: {e.response.status_code}")
                        return None
                    except Exception as e:
                        self.logger.error(f"TVDB: 处理 {entity_type} (ID: {item_id}) 时发生未知错误: {e}", exc_info=True)
                        return None

                details = None
                if mediaType == "movie":
                    details = await _fetch_and_parse("movies")
                elif mediaType == "series":
                    details = await _fetch_and_parse("series")
                else:
                    details = await _fetch_and_parse("movies")
                    if not details:
                        self.logger.info(f"TVDB: 作为电影获取失败，正在尝试作为电视剧获取...")
                        details = await _fetch_and_parse("series")
                return details
        except ValueError as e:
            self.logger.error(f"TVDB获取详情失败 (item_id={item_id}): {e}")
            raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(e))
        except Exception as e:
            self.logger.error(f"TVDB获取详情失败: {e}", exc_info=True)
            return None

    async def search_aliases(self, keyword: str, user: models.User) -> Set[str]:
        return set()

    async def check_connectivity(self) -> str:
        try:
            is_using_proxy = False
            api_key = await self.config_manager.get("tvdbApiKey")
            if not api_key:
                return "未配置API Key"
            proxy_url = await self.config_manager.get("proxyUrl", "")
            proxy_enabled_globally = (await self.config_manager.get("proxyEnabled", "false")).lower() == 'true'
            async with self._session_factory() as session:
                metadata_settings = await crud.get_all_metadata_source_settings(session)
            provider_setting = next((s for s in metadata_settings if s['providerName'] == self.provider_name), None)
            use_proxy_for_this_provider = provider_setting.get('useProxy', False) if provider_setting else False
            proxy_to_use = proxy_url if proxy_enabled_globally and use_proxy_for_this_provider and proxy_url else None
            if proxy_to_use:
                is_using_proxy = True
                self.logger.debug(f"TVDB: 连接性检查将使用代理: {proxy_to_use}")

            async with httpx.AsyncClient(timeout=10.0, proxy=proxy_to_use) as client:
                response = await client.post("https://api4.thetvdb.com/v4/login", json={"apikey": api_key})
                if response.status_code == 200:
                    return "通过代理连接成功" if is_using_proxy else "连接成功"
                else:
                    return f"通过代理连接失败 ({response.status_code})" if is_using_proxy else f"连接失败 ({response.status_code})"
        except Exception as e:
            self.logger.error(f"TVDB: 连接性检查失败: {e}", exc_info=True)
            return "连接失败" # 代理信息已包含在异常中
    async def execute_action(self, action_name: str, payload: Dict, user: models.User) -> Any:
        """TVDB source does not support custom actions."""
        raise NotImplementedError(f"源 '{self.provider_name}' 不支持任何自定义操作。")
