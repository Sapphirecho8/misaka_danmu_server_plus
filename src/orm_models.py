from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer,
    String, TEXT, TIMESTAMP, TypeDecorator, UniqueConstraint, DECIMAL, func
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from .timezone import get_now

class NaiveDateTime(TypeDecorator):
    """
    自定义数据库类型，确保无论数据库驱动返回何种datetime对象，
    在应用层面我们得到的都是不带时区信息的（naive）datetime。
    这解决了PostgreSQL驱动返回带时区时间，而MySQL驱动返回不带时区时间的不一致性问题。
    """
    impl = TIMESTAMP
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        """在写入数据库时，移除时区信息。"""
        if value is not None and value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        """从数据库读取时，移除时区信息。"""
        if value is not None and value.tzinfo is not None:
            return value.replace(tzinfo=None)
        return value

class Base(DeclarativeBase):
    pass

class Anime(Base):
    __tablename__ = "anime"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(Enum('tv_series', 'movie', 'ova', 'other', name="anime_type"), default='tv_series')
    imageUrl: Mapped[Optional[str]] = mapped_column("image_url", String(512))
    localImagePath: Mapped[Optional[str]] = mapped_column("local_image_path", String(512))
    season: Mapped[int] = mapped_column(Integer, default=1)
    episodeCount: Mapped[Optional[int]] = mapped_column("episode_count", Integer)
    year: Mapped[Optional[int]] = mapped_column("year", Integer)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)

    sources: Mapped[List["AnimeSource"]] = relationship(back_populates="anime", cascade="all, delete-orphan")
    metadataRecord: Mapped["AnimeMetadata"] = relationship(back_populates="anime", cascade="all, delete-orphan", uselist=False)
    aliases: Mapped["AnimeAlias"] = relationship(back_populates="anime", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        Index('idx_title_fulltext', 'title'),
    )

class AnimeSource(Base):
    __tablename__ = "anime_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    animeId: Mapped[int] = mapped_column("anime_id", ForeignKey("anime.id", ondelete="CASCADE"))
    sourceOrder: Mapped[int] = mapped_column("source_order", Integer)
    providerName: Mapped[str] = mapped_column("provider_name", String(50))
    mediaId: Mapped[str] = mapped_column("media_id", String(255))
    isFavorited: Mapped[bool] = mapped_column("is_favorited", Boolean, default=False)
    incrementalRefreshEnabled: Mapped[bool] = mapped_column("incremental_refresh_enabled", Boolean, default=False)
    incrementalRefreshFailures: Mapped[int] = mapped_column("incremental_refresh_failures", Integer, default=0)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)

    anime: Mapped["Anime"] = relationship(back_populates="sources")
    episodes: Mapped[List["Episode"]] = relationship(back_populates="source", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint('anime_id', 'provider_name', 'media_id', name='idx_anime_provider_media_unique'),
        UniqueConstraint('anime_id', 'source_order', name='idx_anime_source_order_unique'),
    )

class Episode(Base):
    __tablename__ = "episode"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sourceId: Mapped[int] = mapped_column("source_id", ForeignKey("anime_sources.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    episodeIndex: Mapped[int] = mapped_column("episode_index", Integer)
    providerEpisodeId: Mapped[Optional[str]] = mapped_column("provider_episode_id", String(255))
    sourceUrl: Mapped[Optional[str]] = mapped_column("source_url", TEXT)
    danmakuFilePath: Mapped[Optional[str]] = mapped_column("danmaku_file_path", String(1024))
    fetchedAt: Mapped[Optional[datetime]] = mapped_column("fetched_at", NaiveDateTime)
    commentCount: Mapped[int] = mapped_column("comment_count", Integer, default=0)

    source: Mapped["AnimeSource"] = relationship(back_populates="episodes")

    __table_args__ = (UniqueConstraint('source_id', 'episode_index', name='idx_source_episode_unique'),)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    hashedPassword: Mapped[str] = mapped_column("hashed_password", String(255))
    token: Mapped[Optional[str]] = mapped_column(TEXT)
    tokenUpdate: Mapped[Optional[datetime]] = mapped_column("token_update", NaiveDateTime)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)
    role: Mapped[str] = mapped_column(String(20), default="user")  # user | admin
    canCreateAdmin: Mapped[bool] = mapped_column("can_create_admin", Boolean, default=False)
    perHourLimit: Mapped[Optional[int]] = mapped_column("per_hour_limit", Integer)
    permissionsJson: Mapped[Optional[str]] = mapped_column("permissions_json", TEXT)
    # 用户备注，仅对可访问账户管理列表的用户可见
    remark: Mapped[Optional[str]] = mapped_column("remark", TEXT)

class Scraper(Base):
    __tablename__ = "scrapers"
    providerName: Mapped[str] = mapped_column("provider_name", String(50), primary_key=True)
    isEnabled: Mapped[bool] = mapped_column("is_enabled", Boolean, default=True)
    displayOrder: Mapped[int] = mapped_column("display_order", Integer, default=0)
    useProxy: Mapped[bool] = mapped_column("use_proxy", Boolean, default=False)

class MetadataSource(Base):
    __tablename__ = "metadata_sources"
    providerName: Mapped[str] = mapped_column("provider_name", String(50), primary_key=True)
    isEnabled: Mapped[bool] = mapped_column("is_enabled", Boolean, default=True)
    isAuxSearchEnabled: Mapped[bool] = mapped_column("is_aux_search_enabled", Boolean, default=True)
    displayOrder: Mapped[int] = mapped_column("display_order", Integer, default=0)
    useProxy: Mapped[bool] = mapped_column("use_proxy", Boolean, default=False)
    isFailoverEnabled: Mapped[bool] = mapped_column("is_failover_enabled", Boolean, default=False)

class AnimeMetadata(Base):
    __tablename__ = "anime_metadata"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    animeId: Mapped[int] = mapped_column("anime_id", ForeignKey("anime.id", ondelete="CASCADE"), unique=True)
    tmdbId: Mapped[Optional[str]] = mapped_column("tmdb_id", String(50))
    tmdbEpisodeGroupId: Mapped[Optional[str]] = mapped_column("tmdb_episode_group_id", String(50))
    imdbId: Mapped[Optional[str]] = mapped_column("imdb_id", String(50))
    tvdbId: Mapped[Optional[str]] = mapped_column("tvdb_id", String(50))
    doubanId: Mapped[Optional[str]] = mapped_column("douban_id", String(50))
    bangumiId: Mapped[Optional[str]] = mapped_column("bangumi_id", String(50))

    anime: Mapped["Anime"] = relationship(back_populates="metadataRecord")

class Config(Base):
    __tablename__ = "config"
    configKey: Mapped[str] = mapped_column("config_key", String(100), primary_key=True)
    configValue: Mapped[str] = mapped_column("config_value", TEXT().with_variant(MEDIUMTEXT, "mysql"))
    description: Mapped[Optional[str]] = mapped_column(TEXT)

class CacheData(Base):
    __tablename__ = "cache_data"
    cacheProvider: Mapped[Optional[str]] = mapped_column("cache_provider", String(50))
    cacheKey: Mapped[str] = mapped_column("cache_key", String(255), primary_key=True)
    cacheValue: Mapped[str] = mapped_column("cache_value", TEXT().with_variant(MEDIUMTEXT, "mysql"))
    expiresAt: Mapped[datetime] = mapped_column("expires_at", NaiveDateTime, index=True)

class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    token: Mapped[str] = mapped_column(String(50), unique=True)
    isEnabled: Mapped[bool] = mapped_column("is_enabled", Boolean, default=True)
    isLocked: Mapped[bool] = mapped_column("is_locked", Boolean, default=False)
    ownerUserId: Mapped[Optional[int]] = mapped_column("owner_user_id", BigInteger, index=True, nullable=True)
    createdByUserId: Mapped[Optional[int]] = mapped_column("created_by_user_id", BigInteger, index=True, nullable=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)
    expiresAt: Mapped[Optional[datetime]] = mapped_column("expires_at", NaiveDateTime)
    dailyCallLimit: Mapped[int] = mapped_column("daily_call_limit", Integer, default=500, server_default="500", nullable=False)
    dailyCallCount: Mapped[int] = mapped_column("daily_call_count", Integer, default=0, server_default="0", nullable=False)
    lastCallAt: Mapped[Optional[datetime]] = mapped_column("last_call_at", NaiveDateTime)

class TokenAccessLog(Base):
    __tablename__ = "token_access_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tokenId: Mapped[int] = mapped_column("token_id", Integer)
    ipAddress: Mapped[str] = mapped_column("ip_address", String(45))
    userAgent: Mapped[Optional[str]] = mapped_column("user_agent", TEXT)
    accessTime: Mapped[datetime] = mapped_column("access_time", NaiveDateTime)
    status: Mapped[str] = mapped_column(String(50))
    path: Mapped[Optional[str]] = mapped_column(String(512))

    __table_args__ = (Index('idx_token_id_time', 'token_id', 'access_time'),)

class UaRule(Base):
    __tablename__ = "ua_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uaString: Mapped[str] = mapped_column("ua_string", String(255), unique=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)

class BangumiAuth(Base):
    __tablename__ = "bangumi_auth"
    userId: Mapped[int] = mapped_column("user_id", BigInteger, primary_key=True)
    bangumiUserId: Mapped[Optional[int]] = mapped_column("bangumi_user_id", Integer)
    nickname: Mapped[Optional[str]] = mapped_column(String(255))
    avatarUrl: Mapped[Optional[str]] = mapped_column("avatar_url", String(512))
    accessToken: Mapped[str] = mapped_column("access_token", TEXT)
    refreshToken: Mapped[Optional[str]] = mapped_column("refresh_token", TEXT)
    expiresAt: Mapped[Optional[datetime]] = mapped_column("expires_at", NaiveDateTime)
    authorizedAt: Mapped[Optional[datetime]] = mapped_column("authorized_at", NaiveDateTime)

class OauthState(Base):
    __tablename__ = "oauth_states"
    stateKey: Mapped[str] = mapped_column("state_key", String(100), primary_key=True)
    userId: Mapped[int] = mapped_column("user_id", BigInteger)
    expiresAt: Mapped[datetime] = mapped_column("expires_at", NaiveDateTime, index=True)

class AnimeAlias(Base):
    __tablename__ = "anime_aliases"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    animeId: Mapped[int] = mapped_column("anime_id", ForeignKey("anime.id", ondelete="CASCADE"), unique=True)

# 新增：用户用量日志表（用于按时间窗聚合统计）
    nameEn: Mapped[Optional[str]] = mapped_column("name_en", String(255))
    nameJp: Mapped[Optional[str]] = mapped_column("name_jp", String(255))
    nameRomaji: Mapped[Optional[str]] = mapped_column("name_romaji", String(255))
    aliasCn1: Mapped[Optional[str]] = mapped_column("alias_cn_1", String(255))
    aliasCn2: Mapped[Optional[str]] = mapped_column("alias_cn_2", String(255))
    aliasCn3: Mapped[Optional[str]] = mapped_column("alias_cn_3", String(255))

    anime: Mapped["Anime"] = relationship(back_populates="aliases")

# 新增：用户用量日志表（用于按时间窗聚合统计）
class UserUsageLog(Base):
    __tablename__ = "user_usage_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    userId: Mapped[int] = mapped_column("user_id", BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(50))  # 例如: 'download_episode', 'search'
    amount: Mapped[int] = mapped_column(Integer, default=1)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime, index=True, default=get_now)

    __table_args__ = (
        Index('idx_user_time', 'user_id', 'created_at'),
    )

class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    createdByUserId: Mapped[int] = mapped_column("created_by_user_id", BigInteger, index=True)
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)
    maxUses: Mapped[int] = mapped_column("max_uses", Integer)
    usedCount: Mapped[int] = mapped_column("used_count", Integer, default=0, server_default="0")
    expiresAt: Mapped[Optional[datetime]] = mapped_column("expires_at", NaiveDateTime, nullable=True)
    perHourLimit: Mapped[Optional[int]] = mapped_column("per_hour_limit", Integer, nullable=True)
    permissionsJson: Mapped[Optional[str]] = mapped_column("permissions_json", TEXT)
    remark: Mapped[Optional[str]] = mapped_column(TEXT)
    isEnabled: Mapped[bool] = mapped_column("is_enabled", Boolean, default=True)

class TmdbEpisodeMapping(Base):
    __tablename__ = "tmdb_episode_mapping"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tmdbTvId: Mapped[int] = mapped_column("tmdb_tv_id", Integer)
    tmdbEpisodeGroupId: Mapped[str] = mapped_column("tmdb_episode_group_id", String(50))
    tmdbEpisodeId: Mapped[int] = mapped_column("tmdb_episode_id", Integer)
    tmdbSeasonNumber: Mapped[int] = mapped_column("tmdb_season_number", Integer)
    tmdbEpisodeNumber: Mapped[int] = mapped_column("tmdb_episode_number", Integer)
    customSeasonNumber: Mapped[int] = mapped_column("custom_season_number", Integer)
    customEpisodeNumber: Mapped[int] = mapped_column("custom_episode_number", Integer)
    absoluteEpisodeNumber: Mapped[int] = mapped_column("absolute_episode_number", Integer)

    __table_args__ = (
        UniqueConstraint('tmdb_episode_group_id', 'tmdb_episode_id', name='idx_group_episode_unique'),
        Index('idx_custom_season_episode', 'tmdb_tv_id', 'tmdb_episode_group_id', 'custom_season_number', 'custom_episode_number'),
        Index('idx_absolute_episode', 'tmdb_tv_id', 'tmdb_episode_group_id', 'absolute_episode_number'),
    )

class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    # 修正：将Python属性名从 'id' 改为 'taskId'，以匹配API响应模型，同时保持数据库列名为 'id'
    taskId: Mapped[str] = mapped_column("id", String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    jobType: Mapped[str] = mapped_column("job_type", String(50))
    cronExpression: Mapped[str] = mapped_column("cron_expression", String(100))
    isEnabled: Mapped[bool] = mapped_column("is_enabled", Boolean, default=True)
    lastRunAt: Mapped[Optional[datetime]] = mapped_column("last_run_at", NaiveDateTime)
    nextRunAt: Mapped[Optional[datetime]] = mapped_column("next_run_at", NaiveDateTime)

class WebhookTask(Base):
    __tablename__ = "webhook_tasks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    receptionTime: Mapped[datetime] = mapped_column("reception_time", NaiveDateTime, index=True)
    executeTime: Mapped[datetime] = mapped_column("execute_time", NaiveDateTime, index=True)
    webhookSource: Mapped[str] = mapped_column("webhook_source", String(50))
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True) # pending, processing, failed, submitted
    payload: Mapped[str] = mapped_column(TEXT().with_variant(MEDIUMTEXT, "mysql"))
    uniqueKey: Mapped[str] = mapped_column("unique_key", String(255), unique=True)
    taskTitle: Mapped[str] = mapped_column("task_title", String(255))

    __table_args__ = (Index('idx_status_execute_time', 'status', 'execute_time'),)

class TaskHistory(Base):
    __tablename__ = "task_history"
    # 修正：将Python属性名从 'id' 改为 'taskId'，以匹配Pydantic模型，同时保持数据库列名为 'id'
    taskId: Mapped[str] = mapped_column("id", String(100), primary_key=True)
    scheduledTaskId: Mapped[Optional[str]] = mapped_column("scheduled_task_id", ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[Optional[str]] = mapped_column(TEXT().with_variant(MEDIUMTEXT, "mysql"))
    createdAt: Mapped[datetime] = mapped_column("created_at", NaiveDateTime)
    updatedAt: Mapped[datetime] = mapped_column("updated_at", NaiveDateTime)
    finishedAt: Mapped[Optional[datetime]] = mapped_column("finished_at", NaiveDateTime)
    uniqueKey: Mapped[Optional[str]] = mapped_column("unique_key", String(255), index=True)

    __table_args__ = (Index('idx_created_at', 'created_at'),)

class ExternalApiLog(Base):
    __tablename__ = "external_api_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    accessTime: Mapped[datetime] = mapped_column("access_time", NaiveDateTime)
    ipAddress: Mapped[str] = mapped_column("ip_address", String(45))
    endpoint: Mapped[str] = mapped_column(String(255))
    statusCode: Mapped[int] = mapped_column("status_code", Integer)
    message: Mapped[Optional[str]] = mapped_column(TEXT().with_variant(MEDIUMTEXT, "mysql"))

class RateLimitState(Base):
    __tablename__ = "rate_limit_state"
    providerName: Mapped[str] = mapped_column("provider_name", String(50), primary_key=True)
    requestCount: Mapped[int] = mapped_column("request_count", Integer, default=0)
    lastResetTime: Mapped[datetime] = mapped_column("last_reset_time", NaiveDateTime)

class UserRateLimitState(Base):
    __tablename__ = "user_rate_limit_state"
    userId: Mapped[int] = mapped_column("user_id", BigInteger, primary_key=True)
    requestCount: Mapped[int] = mapped_column("request_count", Integer, default=0)
    lastResetTime: Mapped[datetime] = mapped_column("last_reset_time", NaiveDateTime)
