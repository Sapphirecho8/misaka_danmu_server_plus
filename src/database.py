import secrets
import string
import logging
from fastapi import FastAPI, Request
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError # Import specific SQLAlchemy exceptions
from .config import settings
from .orm_models import Base # type: ignore
from .timezone import get_app_timezone, get_timezone_offset_str, get_now
from .migrations import run_migrations
# 使用模块级日志记录器
logger = logging.getLogger(__name__)

def _get_db_url(include_db_name: bool = True, for_server: bool = False) -> URL:
    """
    根据配置生成数据库连接URL。
    :param include_db_name: URL中是否包含数据库名称。
    :param for_server: 是否为连接到服务器（而不是特定数据库）生成URL，主要用于PostgreSQL。
    """
    db_type = settings.database.type.lower()
    
    if db_type == "mysql":
        drivername = "mysql+aiomysql"
        query = {"charset": "utf8mb4"}
        database = settings.database.name if include_db_name else None
    elif db_type == "postgresql":
        drivername = "postgresql+asyncpg"
        # 修正：移除在连接URL中设置时区的逻辑。
        # 这确保了应用与数据库的交互在时间处理上是“时区无关”的，
        # 避免了因驱动程序自动转换时区而导致的数据不一致问题。
        query = None # 确保不通过查询参数传递时区
        if for_server:
            database = "postgres"
        else:
            database = settings.database.name if include_db_name else None
    else:
        raise ValueError(f"不支持的数据库类型: '{db_type}'。请使用 'mysql' 或 'postgresql'。")

    return URL.create(
        drivername=drivername, username=settings.database.user, password=settings.database.password,
        host=settings.database.host, port=settings.database.port, database=database, query=query,
    )

def _log_db_connection_error(context_message: str, e: Exception):
    """
    Logs a standardized, detailed error message for database connection failures,
    attempting to diagnose specific issues like connection refused or permission denied.
    """
    logger.error("="*60)
    logger.error(f"=== {context_message}失败，应用无法启动。 ===")
    logger.error(f"=== 错误类型: {type(e).__name__}")
    logger.error(f"=== 错误详情: {e}")
    logger.error("---")
    logger.error("--- 诊断与排查建议: ---")

    # Attempt to provide more specific diagnostics based on the exception type
    specific_diagnosis = ""
    if isinstance(e, OperationalError):
        # For MySQL (pymysql) errors, e.orig often has an errno
        if hasattr(e.orig, 'errno'):
            if e.orig.errno == 2003:
                specific_diagnosis = "无法连接到数据库服务器。请检查主机和端口配置，以及网络防火墙。"
            elif e.orig.errno == 1044:
                specific_diagnosis = f"数据库用户 '{settings.database.user}' 权限不足，无法创建或访问数据库 '{settings.database.name}'。"
            elif e.orig.errno == 1045:
                specific_diagnosis = f"数据库用户 '{settings.database.user}' 的密码错误。"
            else:
                specific_diagnosis = f"数据库操作错误 (MySQL 错误码: {e.orig.errno})。"
        # For PostgreSQL (asyncpg) errors, check the original exception type/message
        elif isinstance(e.orig, Exception):
            err_str = str(e.orig).lower()
            if "connection refused" in err_str or "could not connect" in err_str:
                specific_diagnosis = "无法连接到数据库服务器。请检查主机和端口配置，以及网络防火墙。"
            elif "permission denied" in err_str or "privilege" in err_str:
                specific_diagnosis = f"数据库用户 '{settings.database.user}' 权限不足。"
            elif "password authentication failed" in err_str:
                specific_diagnosis = f"数据库用户 '{settings.database.user}' 的密码错误。"
    elif isinstance(e, ProgrammingError):
        specific_diagnosis = "数据库编程错误，可能是SQL语法或权限问题。"

    if specific_diagnosis:
        logger.error(f"--- [!] 精确诊断: {specific_diagnosis}")
        logger.error("---")

    logger.error("--- 通用检查列表: ---")
    logger.error("--- 1. 数据库服务是否正在运行？")
    logger.error(f"--- 2. 数据库配置是否正确？ (主机: {settings.database.host}, 端口: {settings.database.port}, 用户: {settings.database.user})")
    logger.error("--- 3. 数据库密码是否正确？")
    logger.error("--- 4. 数据库用户是否有足够的权限？")
    logger.error("--- 5. 应用与数据库之间的网络是否通畅？")
    logger.error("="*60)

async def create_db_engine_and_session(app: FastAPI):
    """创建数据库引擎和会话工厂，并存储在 app.state 中"""
    try:
        db_url = _get_db_url()
        db_type = settings.database.type.lower()
        engine_args = {
            "echo": False,
            "pool_recycle": 3600,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 30
        }

        engine = create_async_engine(db_url, **engine_args)
        app.state.db_engine = engine
        app.state.db_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        logger.info("数据库引擎和会话工厂创建成功。")
    except Exception as e:
        # 修正：调用标准化的错误日志函数，并提供更精确的上下文
        _log_db_connection_error(f"连接目标数据库 '{settings.database.name}'", e)
        raise

async def _create_db_if_not_exists():
    """如果数据库不存在，则使用 SQLAlchemy 引擎创建它。"""
    db_type = settings.database.type.lower()
    db_name = settings.database.name

    if db_type == "mysql":
        server_url = _get_db_url(include_db_name=False)
        check_sql = text(f"SHOW DATABASES LIKE '{db_name}'")
        create_sql = text(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    elif db_type == "postgresql":
        # 对于PostgreSQL，连接到默认的 'postgres' 数据库来执行创建操作
        server_url = _get_db_url(for_server=True)
        check_sql = text(f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'")
        create_sql = text(f'CREATE DATABASE "{db_name}"')
    else:
        logger.warning(f"不支持为数据库类型 '{db_type}' 自动创建数据库。请确保数据库已手动创建。")
        return

    # 设置隔离级别以允许 DDL 语句
    engine_args = {
        "echo": False,
        "isolation_level": "AUTOCOMMIT"
    }

    engine = create_async_engine(server_url, **engine_args)
    try:
        async with engine.connect() as conn:
            # 检查数据库是否存在
            result = await conn.execute(check_sql)
            if result.scalar_one_or_none() is None:
                logger.info(f"数据库 '{db_name}' 不存在，正在创建...")
                await conn.execute(create_sql)
                logger.info(f"数据库 '{db_name}' 创建成功。")
            else:
                logger.info(f"数据库 '{db_name}' 已存在，跳过创建。")
    except Exception as e:
        # 修正：调用标准化的错误日志函数，并提供更精确的上下文
        _log_db_connection_error("检查或创建数据库时连接服务器", e)
        raise
    finally:
        await engine.dispose()

async def get_db_session(request: Request) -> AsyncSession:
    """依赖项：从应用状态获取数据库会话"""
    session_factory = request.app.state.db_session_factory
    async with session_factory() as session:
        yield session

async def close_db_engine(app: FastAPI):
    """关闭数据库引擎"""
    if hasattr(app.state, "db_engine"):
        await app.state.db_engine.dispose()
        logger.info("数据库引擎已关闭。")

async def create_initial_admin_user(app: FastAPI):
    """在应用启动时创建初始管理员用户（如果已配置且不存在）"""
    # 将导入移到函数内部以避免循环导入
    from . import crud
    from . import models

    admin_user = settings.admin.initial_user
    if not admin_user:
        return

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        existing_user = await crud.get_user_by_username(session, admin_user)

    if existing_user:
        logger.info(f"管理员用户 '{admin_user}' 已存在，跳过创建。")
        return

    # 用户不存在，开始创建
    admin_pass = settings.admin.initial_password
    if not admin_pass:
        # 生成一个安全的16位随机密码
        alphabet = string.ascii_letters + string.digits
        admin_pass = ''.join(secrets.choice(alphabet) for _ in range(16))
        logger.info("未提供初始管理员密码，已生成随机密码。")

    user_to_create = models.UserCreate(username=admin_user, password=admin_pass)
    async with session_factory() as session:
        await crud.create_user(session, user_to_create)

    # 打印凭据信息。
    # 注意：，
    # 以确保敏感的初始密码只输出到控制台，而不会被写入到持久化的日志文件中，从而提高安全性。     
    logger.info("\n" + "="*60)
    logger.info(f"=== 初始管理员账户已创建 (用户: {admin_user}) ".ljust(56) + "===")
    logger.info(f"=== 请使用以下随机生成的密码登录: {admin_pass} ".ljust(56) + "===")
    logger.info("="*60 + "\n")
    print("\n" + "="*60)
    print(f"=== 初始管理员账户已创建 (用户: {admin_user}) ".ljust(56) + "===")
    print(f"=== 请使用以下随机生成的密码登录: {admin_pass} ".ljust(56) + "===")
    print("="*60 + "\n")

async def init_db_tables(app: FastAPI):
    """初始化数据库和表"""
    await _create_db_if_not_exists()
    await create_db_engine_and_session(app)

    engine = app.state.db_engine
    async with engine.begin() as conn:
        # 1. 首先，确保所有基于模型的表都已创建。
        # `create_all` 会安全地跳过已存在的表。
        logger.info("正在同步数据库模型，创建新表...")
        await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库模型同步完成。")

        # 2. 然后，在已存在的表结构上运行统一的迁移任务。
        await run_migrations(conn, settings.database.type.lower(), settings.database.name)
    logger.info("数据库初始化完成。")