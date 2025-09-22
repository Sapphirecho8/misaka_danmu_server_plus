import uvicorn
import asyncio
import secrets
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Depends, status
from fastapi.openapi.docs import get_swagger_ui_html
import logging
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, Response # noqa: F401
from fastapi.middleware.cors import CORSMiddleware  # 新增：处理跨域
import json
from .config_manager import ConfigManager
from .database import init_db_tables, close_db_engine, create_initial_admin_user # type: ignore
from .api import api_router, control_router
from .dandan_api import dandan_router
from .task_manager import TaskManager
from .metadata_manager import MetadataSourceManager
from .scraper_manager import ScraperManager
from .webhook_manager import WebhookManager
from .scheduler import SchedulerManager
from .config import settings
from . import crud, security
from .log_manager import setup_logging
from .rate_limiter import RateLimiter

print(f"当前环境: {settings.environment}") 

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理器。
    - `yield` 之前的部分在应用启动时执行。 
    - `yield` 之后的部分在应用关闭时执行。
    """
    # --- Startup Logic ---
    setup_logging()

    # init_db_tables 现在处理数据库创建、引擎和会话工厂的创建
    await init_db_tables(app)
    session_factory = app.state.db_session_factory

    # 新增：在启动时清理任何未完成的任务
    async with session_factory() as session:
        interrupted_count = await crud.mark_interrupted_tasks_as_failed(session)
        if interrupted_count > 0:
            logging.getLogger(__name__).info(f"已将 {interrupted_count} 个中断的任务标记为失败。")
    
    # 新增：初始化配置管理器
    app.state.config_manager = ConfigManager(session_factory)
    # 新增：集中定义所有默认配置
    default_configs = {
        # 缓存 TTL
        'jwtSecretKey': (secrets.token_hex(32), '用于签名JWT令牌的密钥，在首次启动时自动生成。'),
        'searchTtlSeconds': (10800, '搜索结果的缓存时间（秒），最低3小时。'),
        'episodesTtlSeconds': (10800, '分集列表的缓存时间（秒），最低3小时。'),
        'baseInfoTtlSeconds': (10800, '基础媒体信息（如爱奇艺）的缓存时间（秒），最低3小时。'),
        'metadataSearchTtlSeconds': (10800, '元数据（如TMDB, Bangumi）搜索结果的缓存时间（秒），最低3小时。'),
        # API 和 Webhook
        'customApiDomain': ('', '用于拼接弹幕API地址的自定义域名。'),
        'webhookApiKey': ('', '用于Webhook调用的安全密钥。'),
        'trustedProxies': ('', '受信任的反向代理IP列表，用逗号分隔。当请求来自这些IP时，将从 X-Forwarded-For 或 X-Real-IP 头中解析真实客户端IP。'),
        'webhookEnabled': ('true', '是否全局启用 Webhook 功能。'),
        'webhookDelayedImportEnabled': ('false', '是否为 Webhook 触发的导入启用延时。'),
        'webhookDelayedImportHours': ('24', 'Webhook 延时导入的小时数。'),
        'webhookFilterMode': ('blacklist', 'Webhook 标题过滤模式 (blacklist/whitelist)。'),
        'webhookFilterRegex': ('', '用于过滤 Webhook 标题的正则表达式。'),
        'webhookLogRawRequest': ('false', '是否记录 Webhook 的原始请求体。'),
        'externalApiKey': ('', '用于外部API调用的安全密钥。'),
        'externalApiDuplicateTaskThresholdHours': (3, '（外部API）重复任务提交阈值（小时）。在此时长内，不允许为同一媒体提交重复的自动导入任务。0为禁用。'),
        'webhookCustomDomain': ('', '用于拼接Webhook URL的自定义域名。'),
        # 认证
        # 代理
        'proxyUrl': ('', '全局HTTP/HTTPS/SOCKS5代理地址。'),
        'proxyEnabled': ('false', '是否全局启用代理。'),
        'proxySslVerify': ('true', '使用HTTPS代理时是否验证SSL证书。设为false可解决自签名证书问题。'),
        'jwtExpireMinutes': (settings.jwt.access_token_expire_minutes, 'JWT令牌的有效期（分钟）。-1 表示永不过期。'),
        # 元数据源
        'tmdbApiKey': ('', '用于访问 The Movie Database API 的密钥。'),
        'tmdbApiBaseUrl': ('https://api.themoviedb.org', 'TMDB API 的基础域名。'),
        'tmdbImageBaseUrl': ('https://image.tmdb.org', 'TMDB 图片服务的基础 URL。'),
        'tvdbApiKey': ('', '用于访问 TheTVDB API 的密钥。'),
        'bangumiClientId': ('', '用于Bangumi OAuth的App ID。'),
        'bangumiClientSecret': ('', '用于Bangumi OAuth的App Secret。'),
        'doubanCookie': ('', '用于访问豆瓣API的Cookie。'),
        # 弹幕源
        'danmakuOutputLimitPerSource': ('-1', '单源弹幕输出总数限制。-1为无限制。'),
        'danmakuAggregationEnabled': ('true', '是否启用跨源弹幕聚合功能。'),
        'scraperVerificationEnabled': ('false', '是否启用搜索源签名验证。'),
        'bilibiliCookie': ('', '用于访问B站API的Cookie，特别是buvid3。'),
        'gamerCookie': ('', '用于访问巴哈姆特动画疯的Cookie。'),
        'gamerUserAgent': ('', '用于访问巴哈姆特动画疯的User-Agent。'),
        # 全局过滤
        'search_result_global_blacklist_cn': (r'特典|预告|广告|菜单|花絮|特辑|速看|资讯|彩蛋|直拍|直播回顾|片头|片尾|幕后|映像|番外篇|纪录片|访谈|番外|短片|加更|走心|解忧|纯享|解读|揭秘|赏析', '用于过滤搜索结果标题的全局中文黑名单(正则表达式)。'),
        'search_result_global_blacklist_eng': (r'NC|OP|ED|SP|OVA|OAD|CM|PV|MV|BDMenu|Menu|Bonus|Recap|Teaser|Trailer|Preview|CD|Disc|Scan|Sample|Logo|Info|EDPV|SongSpot|BDSpot', '用于过滤搜索结果标题的全局英文黑名单(正则表达式)。'),
        'mysqlBinlogRetentionDays': (3, '（仅MySQL）自动清理多少天前的二进制日志（binlog）。0为不清理。需要SUPER或BINLOG_ADMIN权限。'),
    }
    await app.state.config_manager.register_defaults(default_configs)

    # --- 新的初始化顺序以解决循环依赖 ---
    # 1. 初始化元数据管理器，但暂时不传入 scraper_manager
    app.state.metadata_manager = MetadataSourceManager(session_factory, app.state.config_manager, None) # type: ignore

    # 2. 初始化搜索源管理器，并传入元数据管理器
    app.state.scraper_manager = ScraperManager(session_factory, app.state.config_manager, app.state.metadata_manager)

    # 3. 将 scraper_manager 实例回填到 metadata_manager 中
    app.state.metadata_manager.scraper_manager = app.state.scraper_manager

    # 4. 现在可以安全地初始化所有管理器
    await app.state.scraper_manager.initialize()
    await app.state.metadata_manager.initialize()

    # 5. 初始化其他依赖于上述管理器的组件
    app.state.rate_limiter = RateLimiter(session_factory, app.state.scraper_manager)

    app.include_router(app.state.metadata_manager.router, prefix="/api/metadata")



    app.state.task_manager = TaskManager(session_factory, app.state.config_manager)
    app.state.webhook_manager = WebhookManager(
        session_factory, app.state.task_manager, app.state.scraper_manager, app.state.rate_limiter, app.state.metadata_manager, app.state.config_manager
    )
    app.state.task_manager.start()
    await create_initial_admin_user(app)
    app.state.cleanup_task = asyncio.create_task(cleanup_task(app))
    app.state.scheduler_manager = SchedulerManager(session_factory, app.state.task_manager, app.state.scraper_manager, app.state.rate_limiter, app.state.metadata_manager, app.state.config_manager)
    await app.state.scheduler_manager.start()
    
    # --- 前端服务 (生产环境) ---
    # 在所有API路由注册完毕后，再挂载前端服务，以确保API路由优先匹配。
    # 在生产环境中，我们需要挂载 Vite 构建后的静态资源目录
    # 并且需要一个“捕获所有”的路由来始终提供 index.html，以支持前端路由。
    if settings.environment == "development":
        # 开发环境：所有非API请求都重定向到Vite开发服务器
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_react_app_dev(request: Request, full_path: str):
            base_url = f"http://{settings.client.host}:{settings.client.port}"
            return RedirectResponse(url=f"{base_url}/{full_path}" if full_path else base_url)
    else:
        # 生产环境：显式挂载静态资源目录
        app.mount("/assets", StaticFiles(directory="web/dist/assets"), name="assets")
        # 修正：挂载前端的静态图片 (如 logo)，使其指向正确的 'web/dist/images' 目录
        app.mount("/images", StaticFiles(directory="web/dist/images"), name="images")
        # dist挂载
        app.mount("/dist", StaticFiles(directory="web/dist"), name="dist")
        # 挂载用户缓存的图片 (如海报)
        # 兼容两种部署目录：优先 /app/config/image（容器/一键安装布局），否则回退到相对路径 config/image
        image_dir = "/app/config/image" if Path("/app/config/image").is_dir() else "config/image"
        app.mount("/data/images", StaticFiles(directory=image_dir), name="cached_images")
        # 然后，为所有其他路径提供 index.html 以支持前端路由
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(request: Request, full_path: str):
            return FileResponse("web/dist/index.html")
    
    yield
    
    # --- Shutdown Logic ---
    if hasattr(app.state, "cleanup_task"):
        app.state.cleanup_task.cancel()
        try:
            await app.state.cleanup_task
        except asyncio.CancelledError:
            pass
    await close_db_engine(app)
    if hasattr(app.state, "scraper_manager"):
        await app.state.scraper_manager.close_all()
    if hasattr(app.state, "task_manager"):
        await app.state.task_manager.stop()
    # 新增：在关闭时也关闭元数据管理器
    if hasattr(app.state, "metadata_manager"):
        await app.state.metadata_manager.close_all()
    if hasattr(app.state, "scheduler_manager"):
        await app.state.scheduler_manager.stop()

app = FastAPI(
    title="Misaka Danmaku External Control API",
    description="用于外部自动化和集成的API。所有端点都需要通过 `?api_key=` 进行鉴权。",
    version="1.0.0",
    lifespan=lifespan,
    # 禁用默认的 docs_url，我们将使用自定义的本地化版本
    docs_url=None,
    redoc_url=None         # 禁用ReDoc
)

# 健康检查端点（兼容反向代理/探活会发 HEAD）
@app.get("/healthz", include_in_schema=False)
async def healthz_get():
    return {"status": "ok"}

@app.head("/healthz", include_in_schema=False)
async def healthz_head():
    return Response(status_code=200)

# --- 新增：自定义本地化的 Swagger UI 文档路由 ---
@app.get("/api/control/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """提供一个使用本地静态资源的 Swagger UI 页面。"""
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - API Docs",
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon-32x32.png"
    )

# 新增：配置CORS，允许前端开发服务器访问API
app.add_middleware(
    CORSMiddleware,
    # 允许所有来源。对于生产环境，建议替换为您的前端域名列表。
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 新增：全局异常处理器，以优雅地处理网络错误
@app.exception_handler(httpx.ConnectError)
async def httpx_connect_error_handler(request: Request, exc: httpx.ConnectError):
    """处理无法连接到外部服务的错误。"""
    logger.error(f"网络连接错误: 无法连接到 {exc.request.url}。错误: {exc}")
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": f"无法连接到外部服务 ({exc.request.url.host})。请检查您的网络连接、代理设置，或确认目标服务未屏蔽您的服务器IP。"},
    )

@app.exception_handler(httpx.TimeoutException)
async def httpx_timeout_error_handler(request: Request, exc: httpx.TimeoutException):
    """处理外部服务请求超时的错误。"""
    logger.error(f"网络超时错误: 请求 {exc.request.url} 超时。错误: {exc}")
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"detail": f"连接外部服务 ({exc.request.url.host}) 超时。请稍后重试。"},
    )




@app.middleware("http")
async def log_not_found_requests(request: Request, call_next):
    """
    中间件：捕获所有请求。
    - 如果是未找到的API路径 (404)，则返回 403 Forbidden，避免路径枚举。
    - 对其他 404 错误，记录详细信息以供调试。
    """
    response = await call_next(request)
    if response.status_code == 404:
        # 如果是 API 路径未找到，返回 403
        if request.url.path.startswith("/api/"):
            logger.warning(
                f"API路径未找到 (返回403): {request.method} {request.url.path} from {request.client.host}"
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Forbidden"}
            )
        
        # 对于非 API 路径的 404 (例如，如果静态文件服务被错误配置)，记录详细信息
        scope = request.scope
        serializable_scope = {
            "type": scope.get("type"),
            "http_version": scope.get("http_version"),
            "server": scope.get("server"),
            "client": scope.get("client"),
            "scheme": scope.get("scheme"),
            "method": scope.get("method"),
            "root_path": scope.get("root_path"),
            "path": scope.get("path"),
            "raw_path": scope.get("raw_path", b"").decode("utf-8", "ignore"),
            "query_string": scope.get("query_string", b"").decode("utf-8", "ignore"),
            "headers": {h[0].decode("utf-8", "ignore"): h[1].decode("utf-8", "ignore") for h in scope.get("headers", [])},
        }
        log_details = {
            "message": "HTTP 404 Not Found - 未找到匹配的路由或文件",
            "url": str(request.url),
            "raw_request_scope": serializable_scope
        }
        logging.getLogger(__name__).warning("未处理的请求详情 (原始请求范围):\n%s", json.dumps(log_details, indent=2, ensure_ascii=False))
    return response

async def cleanup_task(app: FastAPI):
    """定期清理过期缓存和OAuth states的后台任务。"""
    session_factory = app.state.db_session_factory
    while True:
        try:
            await asyncio.sleep(3600) # 每小时清理一次
            async with session_factory() as session:
                await crud.clear_expired_cache(session)
                await crud.clear_expired_oauth_states(session)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).error(f"缓存清理任务出错: {e}")





# 新增：显式地挂载外部控制API路由，以确保其优先级
app.include_router(control_router, prefix="/api/control", tags=["External Control API"])

app.include_router(dandan_router, prefix="/api/v1", tags=["DanDanPlay Compatible"], include_in_schema=False)

# 包含所有非 dandanplay 的 API 路由
app.include_router(api_router, prefix="/api")

# --- 新增：挂载 Swagger UI 的静态文件目录 ---
# 修正：使用相对于工作目录 /app 的绝对路径，使其不再受 __file__ 路径影响
STATIC_DIR = Path("/app/static/swagger-ui")
app.mount("/static/swagger-ui", StaticFiles(directory=STATIC_DIR), name="swagger-ui-static")

# 添加一个运行入口，以便直接从配置启动
# 这样就可以通过 `python -m src.main` 来运行，并自动使用 config.yml 中的端口和主机
if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.environment == "development"  # 开发环境启用自动重载
    )
