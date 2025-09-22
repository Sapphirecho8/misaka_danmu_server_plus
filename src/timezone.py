import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

from .config import settings

logger = logging.getLogger(__name__)
_app_timezone: Optional[ZoneInfo] = None

def get_app_timezone() -> ZoneInfo:
    """
    获取并缓存由环境变量 TZ 定义的应用程序时区。
    如果 TZ 无效或未设置，则默认为东八区 (Asia/Shanghai)。
    """
    global _app_timezone
    if _app_timezone is None:
        try:
            tz_str = settings.tz
            if not tz_str:
                logger.warning("环境变量 TZ 未设置，将默认使用东八区 (Asia/Shanghai) 时区。")
                tz_str = "Asia/Shanghai"
            _app_timezone = ZoneInfo(tz_str)
        except ZoneInfoNotFoundError:
            logger.error(f"环境变量 TZ 的值 '{settings.tz}' 是一个无效的时区，将默认使用东八区 (Asia/Shanghai)。")
            _app_timezone = ZoneInfo("Asia/Shanghai")
    return _app_timezone

def get_now() -> datetime:
    """
    获取附加了应用程序时区的当前时间，并返回一个不带时区信息的（naive）datetime对象。
    例如，如果UTC时间为00:00，而时区设置为Asia/Shanghai，此函数将返回 08:00:00。
    """
    return datetime.now(get_app_timezone()).replace(tzinfo=None)

def get_timezone_offset_str() -> str:
    """
    获取应用程序时区的UTC偏移量，并格式化为 '+HH:MM' 或 '-HH:MM' 字符串，
    以便与MySQL的 `SET time_zone` 命令兼容。
    """
    tz = get_app_timezone()
    # 我们需要一个 datetime 对象来正确计算偏移量，特别是对于有夏令时的时区
    now = datetime.now(tz)
    offset = tz.utcoffset(now)
    
    if offset is None:
        return "+00:00"

    total_seconds = offset.total_seconds()
    sign = '+' if total_seconds >= 0 else '-'
    hours = int(abs(total_seconds) // 3600)
    minutes = int((abs(total_seconds) % 3600) // 60)
    return f"{sign}{hours:02d}:{minutes:02d}"
