import asyncio
import importlib
import re
import pkgutil
import inspect
import logging
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pathlib import Path
from typing import Dict, List, Optional, Any, Type, Tuple, TYPE_CHECKING
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization, asymmetric

from .scrapers.base import BaseScraper
from .config_manager import ConfigManager
from .models import ProviderSearchInfo, ScraperSetting
from . import crud

if TYPE_CHECKING:
    from .metadata_manager import MetadataSourceManager

class ScraperManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], config_manager: ConfigManager, metadata_manager: "MetadataSourceManager"):
        self.scrapers: Dict[str, BaseScraper] = {}
        self._scraper_classes: Dict[str, Type[BaseScraper]] = {}
        self.scraper_settings: Dict[str, Dict[str, Any]] = {}
        self._session_factory = session_factory
        self._domain_map: Dict[str, str] = {}
        self._public_key = None
        self._search_locks: set[str] = set()
        self._lock = asyncio.Lock()
        self._verified_scrapers: set[str] = set()
        self._verification_enabled: bool = False
        self.config_manager = config_manager
        self.metadata_manager = metadata_manager

    async def acquire_search_lock(self, api_key: str) -> bool:
        """Acquires a search lock for a given API key. Returns False if already locked."""
        async with self._lock:
            if api_key in self._search_locks:
                logging.getLogger(__name__).warning(f"API key '{api_key[:8]}...' tried to start a new search while another was running.")
                return False
            self._search_locks.add(api_key)
            logging.getLogger(__name__).info(f"Search lock acquired for API key '{api_key[:8]}...'.")
            return True

    async def release_search_lock(self, api_key: str):
        """Releases the search lock for a given API key."""
        async with self._lock:
            self._search_locks.discard(api_key)
            logging.getLogger(__name__).info(f"Search lock released for API key '{api_key[:8]}...'.")

    def _load_public_key(self):
        """从 src/public_key.pem 加载公钥。"""
        # 公钥是应用代码的一部分，而不是用户配置。
        key_path = Path("/app/src/public_key.pem")
        if not key_path.exists():
            logging.getLogger(__name__).warning("公钥文件 'src/public_key.pem' 未找到。所有搜索源都将无法通过验证。")
            self._public_key = None
            return
        
        try:
            with open(key_path, "rb") as key_file:
                self._public_key = serialization.load_pem_public_key(key_file.read())
            logging.getLogger(__name__).info("公钥加载成功。")
        except Exception as e:
            logging.getLogger(__name__).error(f"加载公钥失败: {e}", exc_info=True)
            self._public_key = None
    
    async def load_and_sync_scrapers(self):
        """
        动态发现、同步到数据库并根据数据库设置加载搜索源。
        此方法可以被再次调用以重新加载搜索源。
        """
        # 清理现有爬虫以确保全新加载
        await self.close_all()
        self.scrapers.clear()
        self._scraper_classes.clear()
        self.scraper_settings.clear()
        self._verified_scrapers.clear()

        self._domain_map.clear()
        discovered_providers = []
        scraper_classes = {}
        default_configs_to_register: Dict[str, Tuple[Any, str]] = {}

        # 使用 pkgutil 发现模块，这对于 .py, .pyc, .so 文件都有效。
        # 我们需要同时处理源码和编译后的情况。
        scrapers_dir = Path("/app/src/scrapers")
        for file_path in scrapers_dir.iterdir():
            # 我们只关心 .py 文件或已知的二进制扩展名
            if not (file_path.name.endswith(".py") or file_path.name.endswith(".so") or file_path.name.endswith(".pyd")):
                continue
            
            # 忽略签名文件
            if file_path.name.endswith(".sig"):
                continue

            module_name_stem = file_path.stem.split('.')[0] # e.g., 'bilibili.cpython-311-x86_64-linux-gnu' -> 'bilibili'
            if module_name_stem.startswith("_") or module_name_stem == "base":
                continue
            try:
                # --- 新增：代码签名验证逻辑 ---
                is_verified = self.verify_scraper_signature(file_path)
                if self._verification_enabled and not is_verified:
                    logging.getLogger(__name__).warning(f"❌ 搜索源 '{file_path.name}' 验证失败！该源将被禁用。")
                else:
                    if self._verification_enabled:
                        logging.getLogger(__name__).info(f"✅ 搜索源 '{file_path.name}' 验证成功。")
                    self._verified_scrapers.add(module_name_stem)
                # --- 验证逻辑结束 ---

                module_name = f"src.scrapers.{module_name_stem}"
                module = importlib.import_module(module_name)
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                        provider_name = obj.provider_name # 直接访问类属性，避免实例化
                        discovered_providers.append(provider_name)
                        # (新增) 注册该刮削器能处理的域名
                        for domain in getattr(obj, 'handled_domains', []):
                            self._domain_map[domain] = provider_name
                        
                        # 在加载时直接发现并收集提供商特定的默认配置
                        if hasattr(obj, '_PROVIDER_SPECIFIC_BLACKLIST_DEFAULT'):
                            config_key = f"{provider_name}_episode_blacklist_regex"
                            default_value = getattr(obj, '_PROVIDER_SPECIFIC_BLACKLIST_DEFAULT')
                            description = f"{provider_name.capitalize()} 源的特定分集标题黑名单 (正则表达式)。"
                            default_configs_to_register[config_key] = (default_value, description)

                        self._scraper_classes[provider_name] = obj

            except TypeError as e:
                if "couldn't parse file content" in str(e).lower():
                    # 这是一个针对 protobuf 版本不兼容的特殊情况。
                    error_msg = (
                        f"加载搜索源模块 {module_name} 失败，疑似 protobuf 版本不兼容。 "
                        f"请确保已将 'protobuf' 版本固定为 '3.20.3' (在 requirements.txt 中), "
                        f"并且已经通过 'docker-compose build' 命令重新构建了您的 Docker 镜像。"
                    )
                    logging.getLogger(__name__).error(error_msg)
                else:
                    # 正常处理其他 TypeError
                    logging.getLogger(__name__).error(f"加载搜索源模块 {module_name} 失败，已跳过。错误: {e}", exc_info=True)
            except Exception as e:
                # 使用标准日志记录器
                logging.getLogger(__name__).error(f"加载搜索源模块 {module_name} 失败，已跳过。错误: {e}", exc_info=True)
        
        # 在同步数据库之前，注册所有发现的默认配置
        if default_configs_to_register:
            await self.config_manager.register_defaults(default_configs_to_register)
            logging.getLogger(__name__).info(f"已为 {len(default_configs_to_register)} 个搜索源注册默认分集黑名单。")

        # 修正：重构同步逻辑以确保 'custom' 源始终存在，并防止意外删除。
        async with self._session_factory() as session:
            # 1. 仅当发现基于文件的搜索源时，才清理过时的条目。
            #    这是一个安全措施，防止在发现过程失败时意外清空数据库。
            #    我们总是将 'custom' 添加到要保留的列表中。
            if discovered_providers:
                providers_to_keep = discovered_providers + ['custom']
                await crud.remove_stale_scrapers(session, providers_to_keep)
            
            # 2. 确保所有发现的搜索源和 'custom' 源都存在于数据库中。
            #    这会添加任何新的搜索源，包括首次添加 'custom'。
            providers_to_sync = discovered_providers + ['custom']
            await crud.sync_scrapers_to_db(session, providers_to_sync)

            # 3. 重新加载所有设置。
            settings_list = await crud.get_all_scraper_settings(session)
        self.scraper_settings = {s['providerName']: s for s in settings_list}

        # Instantiate all discovered scrapers
        for provider_name, scraper_class in self._scraper_classes.items():
            self.scrapers[provider_name] = scraper_class(self._session_factory, self.config_manager)
            setting = self.scraper_settings.get(provider_name, {})
            
            # 如果源未通过验证，则强制禁用它
            is_verified = provider_name in self._verified_scrapers
            is_enabled_by_user = setting.get('isEnabled', True)
            
            final_status = "已启用" if is_enabled_by_user and is_verified else "已禁用"
            verification_status = "已验证" if is_verified else "未验证"
            
            if setting:
                order = setting.get('displayOrder', 'N/A')
                logging.getLogger(__name__).info(f"已加载搜索源 '{provider_name}' (状态: {final_status}, 顺序: {order}, 验证: {verification_status})。")
            else:
                logging.getLogger(__name__).warning(f"已加载搜索源 '{provider_name}'，但在数据库中未找到其设置。")

    async def initialize(self):
        """
        初始化管理器，包括加载公钥和同步搜索源。
        """
        self._load_public_key()
        # 从配置中读取验证开关的状态
        verification_enabled_str = await self.config_manager.get("scraperVerificationEnabled", "false")
        self._verification_enabled = verification_enabled_str.lower() == 'true'
        if not self._verification_enabled:
            logging.getLogger(__name__).info("搜索源签名验证已禁用。所有搜索源将被视为已验证。")

        await self.load_and_sync_scrapers()

    async def update_settings(self, settings: List[ScraperSetting]):
        """
        更新多个搜索源的设置，并立即重新加载以使更改生效。
        这是更新设置的正确方式，因为它能确保内存中的缓存失效。
        """
        async with self._session_factory() as session:
            # CRUD函数负责处理更新逻辑并提交事务。
            await crud.update_scrapers_settings(session, settings)
        
        # 更新数据库后，重新加载所有搜索源以应用新设置。
        # 这能确保启用/禁用、代理设置等立即生效。
        await self.load_and_sync_scrapers()
        # 使用标准日志记录器
        logging.getLogger(__name__).info("搜索源设置已更新并重新加载。")

    @property
    def has_enabled_scrapers(self) -> bool:
        """检查是否有任何已启用的搜索源。"""
        return any(s.get('isEnabled') for s in self.scraper_settings.values())

    async def search_all(self, keywords: List[str], episode_info: Optional[Dict[str, Any]] = None) -> List[ProviderSearchInfo]:
        """
        在所有已启用的搜索源上并发搜索关键词列表。
        """
        enabled_scrapers = [
            scraper for name, scraper in self.scrapers.items()
            if self.scraper_settings.get(name, {}).get('isEnabled') and name in self._verified_scrapers
        ]

        if not enabled_scrapers:
            return []

        tasks = []
        for keyword in keywords:
            for scraper in enabled_scrapers:
                tasks.append(scraper.search(keyword, episode_info=episode_info))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_results = []
        seen_results = set() # 用于去重

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # This assumes enabled_scrapers order is preserved in tasks
                provider_name = enabled_scrapers[i // len(keywords)].provider_name
                logging.getLogger(__name__).error(f"搜索源 '{provider_name}' 的搜索子任务失败: {result}", exc_info=True)
            elif result:
                for item in result:
                    # 使用 (provider, mediaId) 作为唯一标识符
                    unique_id = (item.provider, item.mediaId)
                    if unique_id not in seen_results:
                        all_results.append(item)
                        seen_results.add(unique_id)

        # 新增：在此处应用全局标题过滤
        cn_pattern_str = await self.config_manager.get("search_result_global_blacklist_cn", "")
        eng_pattern_str = await self.config_manager.get("search_result_global_blacklist_eng", "")

        cn_pattern = re.compile(cn_pattern_str, re.IGNORECASE) if cn_pattern_str else None
        eng_pattern = re.compile(r'(\[|\【|\b)(' + eng_pattern_str + r')(\d{1,2})?(\s|_ALL)?(\]|\】|\b)', re.IGNORECASE) if eng_pattern_str else None

        if not cn_pattern and not eng_pattern:
            return all_results

        filtered_results = []
        for item in all_results:
            is_junk = False
            if cn_pattern and cn_pattern.search(item.title):
                is_junk = True
            if not is_junk and eng_pattern and eng_pattern.search(item.title):
                is_junk = True
            
            if not is_junk:
                filtered_results.append(item)
        
        logging.getLogger(__name__).info(f"全局标题过滤: 从 {len(all_results)} 个结果中保留了 {len(filtered_results)} 个。")
        return filtered_results

    async def search_sequentially(self, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> Optional[tuple[str, List[ProviderSearchInfo]]]:
        """
        按用户定义的顺序，在已启用的搜索源上顺序搜索。
        一旦找到任何结果，立即停止并返回提供方名称和结果列表。
        """
        if not self.scrapers:
            return None, None

        # 使用缓存的设置来获取有序且已启用的搜索源列表
        ordered_providers = sorted(
            # 修正：只有已启用且已验证的源才能参与顺序搜索
            [p for p, s in self.scraper_settings.items() if s.get('isEnabled') and p in self._verified_scrapers],
            key=lambda p: self.scraper_settings[p].get('displayOrder', 99)
        )

        for provider_name in ordered_providers:
            scraper = self.scrapers.get(provider_name)
            if not scraper: continue

            try:
                results = await scraper.search(keyword, episode_info=episode_info)
                if results:
                    return provider_name, results
            except Exception as e:
                logging.getLogger(__name__).error(f"顺序搜索时，提供方 '{provider_name}' 发生错误: {e}", exc_info=True)
        
        return None, None

    async def search(self, provider: str, keyword: str, episode_info: Optional[Dict[str, Any]] = None) -> List[ProviderSearchInfo]:
        """
        在指定的搜索源上搜索，如果失败则尝试故障转移。
        """
        scraper = self.get_scraper(provider)
        results = await scraper.search(keyword, episode_info)
        
        # 如果主搜索源没有结果，则尝试故障转移
        if not results and self.metadata_manager:
            logging.getLogger(__name__).info(f"主搜索源 '{provider}' 未找到结果，正在尝试使用元数据源进行故障转移...")
            try:
                failover_results = await self.metadata_manager.supplement_search_result(provider, keyword, episode_info)
                if failover_results:
                    logging.getLogger(__name__).info(f"通过故障转移找到 {len(failover_results)} 个结果。")
                    return failover_results
            except Exception as e:
                logging.getLogger(__name__).error(f"搜索故障转移过程中发生错误: {e}", exc_info=True)
        
        return results

    async def close_all(self):
        """关闭所有搜索源的客户端。"""
        tasks = [scraper.close() for scraper in self.scrapers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    def get_scraper(self, provider: str) -> BaseScraper:
        """通过名称获取指定的搜索源实例。"""
        scraper = self.scrapers.get(provider)
        if not scraper:
            raise ValueError(f"未找到提供方为 '{provider}' 的搜索源")
        return scraper

    def get_scraper_class(self, provider_name: str) -> Optional[Type[BaseScraper]]:
        """获取刮削器的类，而不实例化它。"""
        return self._scraper_classes.get(provider_name)

    def get_scraper_by_domain(self, url: str) -> Optional[BaseScraper]:
        """
        (新增) 通过URL的域名查找合适的刮削器实例。
        """
        try:
            domain = urlparse(url).netloc
            provider_name = self._domain_map.get(domain)
            return self.get_scraper(provider_name) if provider_name else None
        except Exception:
            return None

    def verify_scraper_signature(self, file_path: Path) -> bool:
        """验证插件文件的签名。"""
        # 如果禁用了验证，则所有插件都视为已验证
        if not self._verification_enabled:
            return True
        # 如果没有公钥，所有需要验证的插件都失败
        if not self._public_key:
            return False

        sig_path = file_path.with_suffix(file_path.suffix + ".sig")
        if not sig_path.exists():
            logging.warning(f"未找到签名文件: '{sig_path.name}'，'{file_path.name}' 无法被验证。")
            return False

        try:
            content = file_path.read_bytes()
            signature = sig_path.read_bytes()

            self._public_key.verify(
                signature,
                content,
                asymmetric.padding.PSS(mgf=asymmetric.padding.MGF1(hashes.SHA256()), salt_length=asymmetric.padding.PSS.MAX_LENGTH),
                hashes.SHA256()
            )
            return True
        except Exception:
            return False
