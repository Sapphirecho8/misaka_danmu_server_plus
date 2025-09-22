import logging
import secrets
from typing import Dict
import json

from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import crud
from ..config_manager import ConfigManager
from ..database import get_db_session
from ..webhook_manager import WebhookManager

# 新增：获取专用的 webhook_raw 日志记录器
webhook_raw_logger = logging.getLogger("webhook_raw")

logger = logging.getLogger(__name__)
router = APIRouter()


async def get_webhook_manager(request: Request) -> WebhookManager:
    """依赖项：从应用状态获取 Webhook 管理器"""
    return request.app.state.webhook_manager

async def get_config_manager(request: Request) -> ConfigManager:
    """依赖项：从应用状态获取配置管理器"""
    return request.app.state.config_manager


@router.post("/{webhook_type}", status_code=status.HTTP_202_ACCEPTED, summary="接收外部服务的Webhook通知")
async def handle_webhook(
    webhook_type: str,
    request: Request,
    api_key: str = Query(..., description="Webhook安全密钥"),
    session: AsyncSession = Depends(get_db_session),
    config_manager: ConfigManager = Depends(get_config_manager),
    webhook_manager: WebhookManager = Depends(get_webhook_manager),
):
    """统一的Webhook入口，用于接收来自Sonarr, Radarr等服务的通知。"""
    # 修正：数据库中存储的键名是驼峰命名法的 "webhookApiKey"
    stored_key = await crud.get_config_value(session, "webhookApiKey", "")
    # 修正：使用 secrets.compare_digest 防止时序攻击，并处理 stored_key 为空的情况
    if not stored_key or not secrets.compare_digest(api_key, stored_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的Webhook API Key")

    # 新增：记录原始请求体
    log_raw_request = (await config_manager.get("webhookLogRawRequest", "false")).lower() == 'true'
    if log_raw_request:
        # 为了让后续的 request.json() 能够工作，我们需要读取body后，再通过一个技巧将其“放回”请求流中
        raw_body = await request.body()
        webhook_raw_logger.info(f"Webhook 原始请求体 ({webhook_type}):\n{raw_body.decode(errors='ignore')}")
        
        async def receive():
            return {"type": "http.request", "body": raw_body}
        request._receive = receive

    # API 端点现在变得更简单。
    # 它只负责找到正确的处理器，并将原始请求传递给它。
    # 处理器现在负责解析请求体。
    try:
        handler = webhook_manager.get_handler(webhook_type)
        # 将整个请求对象传递给处理器
        await handler.handle(request, webhook_source=webhook_type)
    except ValueError as e:
        # 捕获在 get_handler 中当 webhook_type 无效时抛出的 ValueError
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except HTTPException:
        # 重新抛出处理器中产生的 HTTPException
        raise
    except Exception as e:
        # 捕获处理器中任何其他未预料到的错误
        logger.error(f"处理 Webhook '{webhook_type}' 时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="处理 Webhook 时发生内部错误。")

    return {"message": "Webhook received and is being processed."}