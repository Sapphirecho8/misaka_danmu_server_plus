import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

async def _check_migration_flag(conn: AsyncConnection, migration_id: str) -> bool:
    """检查迁移是否已执行过。"""
    check_flag_sql = text("SELECT 1 FROM config WHERE config_key = :key")
    flag_exists = (await conn.execute(check_flag_sql, {"key": migration_id})).scalar_one_or_none() is not None
    if flag_exists:
        logger.info(f"迁移 '{migration_id}' 已执行过，跳过。")
        return True
    return False

async def _set_migration_flag(conn: AsyncConnection, migration_id: str):
    """设置迁移完成的标志位。"""
    description = f"标志位，表示已完成数据库迁移: {migration_id}"
    if conn.dialect.name == 'mysql':
        stmt = text("INSERT INTO config (config_key, config_value, description) VALUES (:key, 'true', :desc) ON DUPLICATE KEY UPDATE config_value = 'true'")
    else: # postgresql
        stmt = text("INSERT INTO config (config_key, config_value, description) VALUES (:key, 'true', :desc) ON CONFLICT (config_key) DO UPDATE SET config_value = 'true'")
    await conn.execute(stmt, {"key": migration_id, "desc": description})
    logger.info(f"成功设置迁移标志 '{migration_id}'。")

async def _run_migration(conn: AsyncConnection, migration_id: str, migration_func, *args):
    """一个包装器，用于运行单个迁移任务并处理标志位。"""
    if not await _check_migration_flag(conn, migration_id):
        logger.warning(f"将执行一次性数据库迁移 '{migration_id}'...")
        try:
            await migration_func(conn, *args)
            await _set_migration_flag(conn, migration_id)
            logger.info(f"迁移任务 '{migration_id}' 成功完成。")
        except Exception as e:
            logger.error(f"执行迁移 '{migration_id}' 时发生错误: {e}", exc_info=True)
            # 向上抛出异常以中断应用启动，防止在不完整的数据库结构上运行
            raise

async def _migrate_utc_to_local_datetime_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 将可能被错误存储为UTC时间的datetime字段，转换为服务器的本地时间。"""
    tables_and_columns = {
        "anime": ["created_at"], "anime_sources": ["created_at"], "episode": ["fetched_at"],
        "users": ["token_update", "created_at"], "api_tokens": ["created_at", "expires_at"],
        "token_access_logs": ["access_time"], "ua_rules": ["created_at"],
        "bangumi_auth": ["expires_at", "authorized_at"], "oauth_states": ["expires_at"],
        "scheduled_tasks": ["last_run_at", "next_run_at"],
        "task_history": ["created_at", "updated_at", "finished_at"],
        "external_api_logs": ["access_time"], "rate_limit_state": ["last_reset_time"],
        "cache_data": ["expires_at"],
    }
    for table, columns in tables_and_columns.items():
        for column in columns:
            logger.info(f"正在迁移 {table}.{column}...")
            if db_type == "mysql":
                update_sql = text(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` DATETIME;")
            else: # postgresql
                update_sql = text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE USING "{column}"::timestamp;')
            await conn.execute(update_sql)

async def _migrate_add_source_order_task(conn: AsyncConnection, db_type: str, db_name: str):
    """迁移任务: 确保 anime_sources 表有持久化的 source_order 字段。"""
    if db_type == "mysql":
        check_column_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'anime_sources' AND column_name = 'source_order'")
        add_column_sql = text("ALTER TABLE anime_sources ADD COLUMN `source_order` INT NULL")
    else: # postgresql
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'anime_sources' AND column_name = 'source_order'")
        add_column_sql = text('ALTER TABLE anime_sources ADD COLUMN "source_order" INT NULL')

    if not (await conn.execute(check_column_sql)).scalar_one_or_none():
        await conn.execute(add_column_sql)
        distinct_anime_ids = (await conn.execute(text("SELECT DISTINCT anime_id FROM anime_sources"))).scalars().all()
        for anime_id in distinct_anime_ids:
            sources_ids = (await conn.execute(text("SELECT id FROM anime_sources WHERE anime_id = :anime_id ORDER BY id"), {"anime_id": anime_id})).scalars().all()
            for i, source_id in enumerate(sources_ids):
                await conn.execute(text("UPDATE anime_sources SET source_order = :order WHERE id = :source_id"), {"order": i + 1, "source_id": source_id})
        if db_type == "mysql":
            await conn.execute(text("ALTER TABLE anime_sources MODIFY COLUMN `source_order` INT NOT NULL"))
        else: # postgresql
            await conn.execute(text('ALTER TABLE anime_sources ALTER COLUMN "source_order" SET NOT NULL'))

    if db_type == "mysql":
        check_constraint_sql = text(f"SELECT 1 FROM information_schema.table_constraints WHERE table_schema = '{db_name}' AND table_name = 'anime_sources' AND constraint_name = 'idx_anime_source_order_unique'")
        add_constraint_sql = text("ALTER TABLE anime_sources ADD CONSTRAINT idx_anime_source_order_unique UNIQUE (anime_id, source_order)")
    else: # postgresql
        check_constraint_sql = text("SELECT 1 FROM pg_constraint WHERE conname = 'idx_anime_source_order_unique'")
        add_constraint_sql = text('ALTER TABLE anime_sources ADD CONSTRAINT idx_anime_source_order_unique UNIQUE (anime_id, source_order)')

    if not (await conn.execute(check_constraint_sql)).scalar_one_or_none():
        await conn.execute(add_constraint_sql)

async def _migrate_add_danmaku_file_path_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 确保 episode 表有 danmaku_file_path 字段。"""
    if db_type == "mysql":
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'episode' AND column_name = 'danmaku_file_path'")
        add_column_sql = text("ALTER TABLE episode ADD COLUMN `danmaku_file_path` VARCHAR(1024) NULL DEFAULT NULL")
    else: # postgresql
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'episode' AND column_name = 'danmaku_file_path'")
        add_column_sql = text('ALTER TABLE episode ADD COLUMN "danmaku_file_path" VARCHAR(1024) NULL DEFAULT NULL')

    if not (await conn.execute(check_column_sql)).scalar_one_or_none():
        await conn.execute(add_column_sql)

async def _migrate_text_to_mediumtext_task(conn: AsyncConnection, db_type: str, db_name: str):
    """迁移任务: 将多个表中可能存在的 TEXT 字段修改为 MEDIUMTEXT (仅MySQL)。"""
    if db_type != "mysql":
        return
    tables_and_columns = {
        "cache_data": "cache_value", "config": "config_value",
        "task_history": "description", "external_api_logs": "message"
    }
    for table, column in tables_and_columns.items():
        check_sql = text(f"SELECT DATA_TYPE FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = '{table}' AND column_name = '{column}'")
        current_type = (await conn.execute(check_sql)).scalar_one_or_none()
        if current_type and current_type.lower() == 'text':
            await conn.execute(text(f"ALTER TABLE {table} MODIFY COLUMN `{column}` MEDIUMTEXT"))

async def _migrate_add_source_url_to_episode_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 确保 episode 表有 source_url 字段，并处理旧的命名。"""
    old_name, new_name, table = "sourceUrl", "source_url", "episode"
    if db_type == "mysql":
        check_old_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table}' AND column_name = '{old_name}'")
        check_new_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table}' AND column_name = '{new_name}'")
        rename_sql = text(f"ALTER TABLE `{table}` CHANGE COLUMN `{old_name}` `{new_name}` TEXT NULL")
        add_sql = text(f"ALTER TABLE `{table}` ADD COLUMN `{new_name}` TEXT NULL")
    else: # postgresql
        check_old_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table}' AND column_name = '{old_name}'")
        check_new_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table}' AND column_name = '{new_name}'")
        rename_sql = text(f'ALTER TABLE "{table}" RENAME COLUMN "{old_name}" TO "{new_name}"')
        add_sql = text(f'ALTER TABLE "{table}" ADD COLUMN "{new_name}" TEXT NULL')

    old_exists = (await conn.execute(check_old_sql)).scalar_one_or_none()
    new_exists = (await conn.execute(check_new_sql)).scalar_one_or_none()

    if old_exists and not new_exists:
        await conn.execute(rename_sql)
    elif not old_exists and not new_exists:
        await conn.execute(add_sql)

async def _migrate_clear_rate_limit_state_task(conn: AsyncConnection):
    """迁移任务: 清理旧的速率限制状态和配置。"""
    await conn.execute(text("TRUNCATE TABLE rate_limit_state;"))
    await conn.execute(text("DELETE FROM config WHERE config_key IN ('globalRateLimitEnabled', 'globalRateLimitCount', 'globalRateLimitPeriod')"))

async def _migrate_add_unique_key_to_task_history_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 确保 task_history 表有 unique_key 字段和索引。"""
    if db_type == "mysql":
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'task_history' AND column_name = 'unique_key'")
        add_column_sql = text("ALTER TABLE task_history ADD COLUMN `unique_key` VARCHAR(255) NULL, ADD INDEX `idx_unique_key` (`unique_key`)")
    else: # postgresql
        check_column_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'task_history' AND column_name = 'unique_key'")
        add_column_sql = text('ALTER TABLE task_history ADD COLUMN "unique_key" VARCHAR(255) NULL')
        add_index_sql = text('CREATE INDEX IF NOT EXISTS idx_task_history_unique_key ON task_history (unique_key)')

    if not (await conn.execute(check_column_sql)).scalar_one_or_none():
        await conn.execute(add_column_sql)
        # 仅当数据库是 PostgreSQL 时，才单独执行创建索引的语句
        if db_type == "postgresql":
            await conn.execute(add_index_sql)

async def _migrate_api_token_to_daily_limit_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 将 api_tokens 表的调用限制升级为每日限制。"""
    # 检查新列是否存在，如果存在则认为迁移已完成
    if db_type == "mysql":
        check_new_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'daily_call_limit'")
    else: # postgresql
        check_new_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'daily_call_limit'")

    if (await conn.execute(check_new_col_sql)).scalar_one_or_none():
        logger.info("检测到 'daily_call_limit' 列，跳过 api_tokens 表的迁移。")
        return

    # 检查旧列是否存在
    if db_type == "mysql":
        check_old_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'call_limit'")
    else: # postgresql
        check_old_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'call_limit'")

    if (await conn.execute(check_old_col_sql)).scalar_one_or_none():
        logger.info("检测到旧的 'call_limit' 列，正在执行重命名和添加新列操作...")
        if db_type == "mysql":
            await conn.execute(text("ALTER TABLE api_tokens CHANGE COLUMN `call_limit` `daily_call_limit` INT NOT NULL DEFAULT 500;"))
            await conn.execute(text("ALTER TABLE api_tokens CHANGE COLUMN `call_count` `daily_call_count` INT NOT NULL DEFAULT 0;"))
            await conn.execute(text("ALTER TABLE api_tokens ADD COLUMN `last_call_at` DATETIME NULL DEFAULT NULL;"))
        else: # postgresql
            await conn.execute(text('ALTER TABLE api_tokens RENAME COLUMN "call_limit" TO "daily_call_limit";'))
            await conn.execute(text('ALTER TABLE api_tokens RENAME COLUMN "call_count" TO "daily_call_count";'))
            await conn.execute(text('ALTER TABLE api_tokens ADD COLUMN "last_call_at" TIMESTAMP NULL DEFAULT NULL;'))
        logger.info("成功将 api_tokens 表升级到每日限制模式。")
    else:
        logger.info("未检测到旧的 'call_limit' 列，可能是新安装，将直接添加新列...")
        if db_type == "mysql":
            await conn.execute(text("ALTER TABLE api_tokens ADD COLUMN `daily_call_limit` INT NOT NULL DEFAULT 500;"))
            await conn.execute(text("ALTER TABLE api_tokens ADD COLUMN `daily_call_count` INT NOT NULL DEFAULT 0;"))
            await conn.execute(text("ALTER TABLE api_tokens ADD COLUMN `last_call_at` DATETIME NULL DEFAULT NULL;"))
        else: # postgresql
            await conn.execute(text('ALTER TABLE api_tokens ADD COLUMN "daily_call_limit" INTEGER NOT NULL DEFAULT 500;'))
            await conn.execute(text('ALTER TABLE api_tokens ADD COLUMN "daily_call_count" INTEGER NOT NULL DEFAULT 0;'))
            await conn.execute(text('ALTER TABLE api_tokens ADD COLUMN "last_call_at" TIMESTAMP NULL DEFAULT NULL;'))
        logger.info("成功为 api_tokens 表添加每日限制相关列。")


async def _migrate_add_api_token_lock_task(conn: AsyncConnection, db_type: str):
    """迁移任务: 为 api_tokens 表添加 is_locked 列。"""
    if db_type == "mysql":
        check_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'is_locked'")
        add_col_sql = text("ALTER TABLE api_tokens ADD COLUMN `is_locked` TINYINT(1) NOT NULL DEFAULT 0;")
    else:  # postgresql
        check_col_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'is_locked'")
        add_col_sql = text('ALTER TABLE api_tokens ADD COLUMN "is_locked" BOOLEAN NOT NULL DEFAULT FALSE;')

    if not (await conn.execute(check_col_sql)).scalar_one_or_none():
        await conn.execute(add_col_sql)
        logger.info("已为 api_tokens 表添加 is_locked 列。")
    else:
        logger.info("检测到 api_tokens.is_locked 已存在，跳过添加。")

async def _migrate_add_user_columns_and_user_rate_limit_task(conn: AsyncConnection, db_type: str, db_name: str):
    """为 users 表添加角色/权限相关列；为 api_tokens 增加 owner_user_id；创建 user_rate_limit_state 表。"""
    # users 表新增列
    if db_type == 'mysql':
        def col_exists(table, col):
            return conn.execute(text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = '{table}' AND column_name = '{col}'"))
    else:
        def col_exists(table, col):
            return conn.execute(text(f"SELECT 1 FROM information_schema.columns WHERE table_name = '{table}' AND column_name = '{col}'"))

    # users.role
    if not (await col_exists('users','role')).scalar_one_or_none():
        await conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'"))
    # users.can_create_admin
    if not (await col_exists('users','can_create_admin')).scalar_one_or_none():
        if db_type=='mysql':
            await conn.execute(text("ALTER TABLE users ADD COLUMN `can_create_admin` TINYINT(1) NOT NULL DEFAULT 0"))
        else:
            await conn.execute(text('ALTER TABLE users ADD COLUMN "can_create_admin" BOOLEAN NOT NULL DEFAULT FALSE'))
    # users.per_hour_limit
    if not (await col_exists('users','per_hour_limit')).scalar_one_or_none():
        await conn.execute(text("ALTER TABLE users ADD COLUMN per_hour_limit INT NULL"))
    # users.permissions_json
    if not (await col_exists('users','permissions_json')).scalar_one_or_none():
        if db_type=='mysql':
            await conn.execute(text("ALTER TABLE users ADD COLUMN `permissions_json` MEDIUMTEXT NULL"))
        else:
            await conn.execute(text('ALTER TABLE users ADD COLUMN "permissions_json" TEXT NULL'))

    # api_tokens.owner_user_id
    if not (await col_exists('api_tokens','owner_user_id')).scalar_one_or_none():
        await conn.execute(text("ALTER TABLE api_tokens ADD COLUMN owner_user_id BIGINT NULL"))
        # 创建索引：PostgreSQL 支持 IF NOT EXISTS，MySQL 不支持，简单跳过或由DBA后续加索引
        if db_type != 'mysql':
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_api_tokens_owner ON api_tokens (owner_user_id)"))

    # user_rate_limit_state 表
    if db_type=='mysql':
        create_sql = text(
            """
            CREATE TABLE IF NOT EXISTS user_rate_limit_state (
                user_id BIGINT PRIMARY KEY,
                request_count INT NOT NULL DEFAULT 0,
                last_reset_time DATETIME NOT NULL
            )
            """
        )
    else:
        create_sql = text(
            """
            CREATE TABLE IF NOT EXISTS user_rate_limit_state (
                user_id BIGINT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                last_reset_time TIMESTAMP NOT NULL
            )
            """
        )
    await conn.execute(create_sql)
    logger.info("users与api_tokens结构以及user_rate_limit_state已确保存在/更新。")

async def _migrate_add_user_remark_task(conn: AsyncConnection, db_type: str, db_name: str):
    """迁移任务: 为 users 表添加 remark 备注列。"""
    if db_type == 'mysql':
        check_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'users' AND column_name = 'remark'")
        add_sql = text("ALTER TABLE users ADD COLUMN `remark` MEDIUMTEXT NULL")
    else:
        check_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'remark'")
        add_sql = text('ALTER TABLE users ADD COLUMN "remark" TEXT NULL')
    exists = (await conn.execute(check_sql)).scalar_one_or_none()
    if not exists:
        await conn.execute(add_sql)
        logger.info("已为 users 表添加 remark 列。")

async def _migrate_add_api_token_creator_task(conn: AsyncConnection, db_type: str, db_name: str):
    """迁移任务: 为 api_tokens 表添加 created_by_user_id 列，并尽量回填创建者。
    回填策略：
    - 若 owner_user_id 不为空且 created_by_user_id 为空，则设置 created_by_user_id = owner_user_id（私有 token 视为创建者即拥有者）。
    - 若仍为空，尝试将创建者设置为用户名为 'admin' 的用户（若存在）。
    """
    # 添加列
    if db_type == 'mysql':
        check_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'api_tokens' AND column_name = 'created_by_user_id'")
        add_sql = text("ALTER TABLE api_tokens ADD COLUMN created_by_user_id BIGINT NULL")
    else:
        check_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'api_tokens' AND column_name = 'created_by_user_id'")
        add_sql = text('ALTER TABLE api_tokens ADD COLUMN "created_by_user_id" BIGINT NULL')
    exists = (await conn.execute(check_sql)).scalar_one_or_none()
    if not exists:
        await conn.execute(add_sql)
        # 可选索引
        if db_type != 'mysql':
            await conn.execute(text('CREATE INDEX IF NOT EXISTS idx_api_tokens_created_by ON api_tokens (created_by_user_id)'))
        logger.info("已为 api_tokens 表添加 created_by_user_id 列。")
    # 回填：私有 token 创建者 = 拥有者
    await conn.execute(text("UPDATE api_tokens SET created_by_user_id = owner_user_id WHERE created_by_user_id IS NULL AND owner_user_id IS NOT NULL"))
    # 回填：其余设置为 admin（若存在）
    admin_id = (await conn.execute(text("SELECT id FROM users WHERE LOWER(username) = 'admin' LIMIT 1"))).scalar_one_or_none()
    if admin_id is not None:
        await conn.execute(text("UPDATE api_tokens SET created_by_user_id = :aid WHERE created_by_user_id IS NULL"), {"aid": admin_id})
    logger.info("api_tokens.created_by_user_id 回填完成。")


async def run_migrations(conn: AsyncConnection, db_type: str, db_name: str):
    """
    按顺序执行所有数据库架构迁移。
    """
    logger.info("开始执行数据库迁移检查...")

    # 迁移任务列表，按依赖顺序列出
    async def _migrate_add_user_usage_logs(conn: AsyncConnection, db_type: str):
        if db_type == 'mysql':
            create_sql = text(
                """
                CREATE TABLE IF NOT EXISTS user_usage_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    user_id BIGINT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    amount INT NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    INDEX idx_user_time (user_id, created_at)
                )
                """
            )
        else:
            create_sql = text(
                """
                CREATE TABLE IF NOT EXISTS user_usage_logs (
                    id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
                    user_id BIGINT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_time ON user_usage_logs (user_id, created_at);
                """
            )
        await conn.execute(create_sql)
        logger.info("user_usage_logs 表已确保存在。")

    migrations = [
        ("migrate_utc_to_local_datetime_v2", _migrate_utc_to_local_datetime_task, (db_type,)),
        ("migrate_clear_rate_limit_state_v1", _migrate_clear_rate_limit_state_task, ()),
        ("migrate_add_source_order_v1", _migrate_add_source_order_task, (db_type, db_name)),
        ("migrate_add_danmaku_file_path_v1", _migrate_add_danmaku_file_path_task, (db_type,)),
        ("migrate_text_to_mediumtext_v1", _migrate_text_to_mediumtext_task, (db_type, db_name)),
        ("migrate_add_source_url_to_episode_v1", _migrate_add_source_url_to_episode_task, (db_type,)),
        ("migrate_add_unique_key_to_task_history_v1", _migrate_add_unique_key_to_task_history_task, (db_type,)),
        ("migrate_api_token_to_daily_limit_v1", _migrate_api_token_to_daily_limit_task, (db_type,)),
        ("migrate_add_api_token_lock_v1", _migrate_add_api_token_lock_task, (db_type,)),
        ("migrate_add_user_columns_and_user_rate_limit_v1", _migrate_add_user_columns_and_user_rate_limit_task, (db_type, db_name)),
        ("migrate_add_user_usage_logs_v1", _migrate_add_user_usage_logs, (db_type,)),
        ("migrate_add_user_remark_v1", _migrate_add_user_remark_task, (db_type, db_name)),
        ("migrate_add_api_token_creator_v1", _migrate_add_api_token_creator_task, (db_type, db_name)),
        ("migrate_create_invites_v1", _migrate_create_invites_task, (db_type, db_name)),
        ("migrate_alter_invites_add_expires_at_v1", _migrate_alter_invites_add_expires_at_task, (db_type, db_name)),
    ]

    for migration_id, migration_func, args in migrations:
        await _run_migration(conn, migration_id, migration_func, *args)

    logger.info("所有数据库迁移检查完成。")

async def _migrate_create_invites_task(conn: AsyncConnection, db_type: str, db_name: str):
    """创建 invites 表。"""
    if db_type == 'mysql':
        exists_sql = text(f"SELECT 1 FROM information_schema.tables WHERE table_schema = '{db_name}' AND table_name = 'invites'")
        create_sql = text(
            """
            CREATE TABLE IF NOT EXISTS invites (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                code VARCHAR(64) NOT NULL UNIQUE,
                created_by_user_id BIGINT NOT NULL,
                created_at DATETIME NOT NULL,
                max_uses INT NOT NULL,
                used_count INT NOT NULL DEFAULT 0,
                per_hour_limit INT NULL,
                permissions_json MEDIUMTEXT NULL,
                remark MEDIUMTEXT NULL,
                is_enabled TINYINT(1) NOT NULL DEFAULT 1,
                INDEX idx_invites_code (code),
                INDEX idx_invites_creator (created_by_user_id)
            )
            """
        )
    else:
        exists_sql = text("SELECT 1 FROM information_schema.tables WHERE table_name = 'invites'")
        create_sql = text(
            """
            CREATE TABLE IF NOT EXISTS invites (
                id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
                code VARCHAR(64) UNIQUE NOT NULL,
                created_by_user_id BIGINT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                per_hour_limit INTEGER NULL,
                permissions_json TEXT NULL,
                remark TEXT NULL,
                is_enabled BOOLEAN NOT NULL DEFAULT TRUE
            );
            CREATE INDEX IF NOT EXISTS idx_invites_code ON invites (code);
            CREATE INDEX IF NOT EXISTS idx_invites_creator ON invites (created_by_user_id);
            """
        )
    exists = (await conn.execute(exists_sql)).scalar_one_or_none()
    if not exists:
        await conn.execute(create_sql)
        logger.info("已创建 invites 表。")
    else:
        logger.info("检测到 invites 已存在，跳过创建。")

async def _migrate_alter_invites_add_expires_at_task(conn: AsyncConnection, db_type: str, db_name: str):
    if db_type == 'mysql':
        check_sql = text(f"SELECT 1 FROM information_schema.columns WHERE table_schema = '{db_name}' AND table_name = 'invites' AND column_name = 'expires_at'")
        alter_sql = text("ALTER TABLE invites ADD COLUMN `expires_at` DATETIME NULL")
    else:
        check_sql = text("SELECT 1 FROM information_schema.columns WHERE table_name = 'invites' AND column_name = 'expires_at'")
        alter_sql = text('ALTER TABLE invites ADD COLUMN "expires_at" TIMESTAMP NULL')
    exists = (await conn.execute(check_sql)).scalar_one_or_none()
    if not exists:
        await conn.execute(alter_sql)
        logger.info("已为 invites 表添加 expires_at 列。")
    else:
        logger.info("检测到 invites.expires_at 已存在，跳过添加。")
