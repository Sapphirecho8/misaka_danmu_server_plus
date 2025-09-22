from typing import Optional, List, Dict, Union, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .. import crud, models, security
from ..orm_models import Invite as InviteModel
from sqlalchemy import update as sa_update
from ..database import get_db_session
from ..timezone import get_now

router = APIRouter()

class InviteCreate(BaseModel):
    maxUses: int = Field(..., ge=1, le=1000)
    perHourLimit: Optional[int] = None
    # 接受 'allow'/'deny' 或 True/False，后续统一转换为布尔映射
    permissions: Optional[Dict[str, Union[bool, str]]] = None
    remark: Optional[str] = None
    expiresAt: Optional[datetime] = None

class InviteInfo(BaseModel):
    id: int
    code: str
    createdByUserId: int
    createdAt: datetime
    maxUses: int
    usedCount: int
    expiresAt: Optional[datetime] = None
    isExpired: Optional[bool] = None
    perHourLimit: Optional[int] = None
    permissions: Optional[Dict[str, bool]] = None
    remark: Optional[str] = None
    isEnabled: bool

class RegisterRequest(BaseModel):
    code: str
    username: str
    password: str

class InviteValidateResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None  # not_found | expired | no_remaining | disabled
    message: str
    maxUses: Optional[int] = None
    usedCount: Optional[int] = None
    expiresAt: Optional[datetime] = None

def _ensure_create_users(full: Dict, username: str):
    try:
        from ..config import settings as _app
        super_admins = set(filter(None, {(_app.admin.initial_user or '').lower()}))
    except Exception:
        super_admins = {'admin'}
    import json as _json
    perms = {}
    try:
        if full and full.get('permissionsJson'):
            perms = _json.loads(full['permissionsJson']) or {}
    except Exception:
        perms = {}
    if not ((username.lower() in super_admins) or (perms.get('createUsers') is True)):
        raise HTTPException(status_code=403, detail="Not allowed to manage invites")

@router.get("/invites", response_model=List[InviteInfo], summary="列出邀请链接（需新增用户权限）")
async def list_invites_api(
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    full = await crud.get_user_by_username_full(session, current_user.username)
    _ensure_create_users(full or {}, current_user.username)
    is_admin = (full or {}).get('role') == 'admin'
    data = await crud.list_invites(session, created_by_user_id=(full or {}).get('id'), is_admin=is_admin)
    return [InviteInfo.model_validate(x) for x in data]

@router.get("/invites/validate", response_model=InviteValidateResponse, summary="校验邀请码（公开接口）")
async def validate_invite(
    code: str,
    session: AsyncSession = Depends(get_db_session)
):
    row = (await session.execute(select(InviteModel).where(InviteModel.code == code))).scalar_one_or_none()
    if not row:
        return InviteValidateResponse(valid=False, reason="not_found", message="邀请码不存在")
    # expired
    if getattr(row, 'expiresAt', None) is not None and row.expiresAt <= get_now():
        return InviteValidateResponse(valid=False, reason="expired", message="邀请码已过期", maxUses=row.maxUses, usedCount=row.usedCount, expiresAt=row.expiresAt)
    # no remaining
    if row.maxUses is not None and (row.usedCount or 0) >= row.maxUses:
        return InviteValidateResponse(valid=False, reason="no_remaining", message="邀请码无可用次数", maxUses=row.maxUses, usedCount=row.usedCount, expiresAt=row.expiresAt)
    # disabled treated as not found for public
    if getattr(row, 'isEnabled', True) is False:
        return InviteValidateResponse(valid=False, reason="disabled", message="邀请码不存在")
    return InviteValidateResponse(valid=True, message="ok", maxUses=row.maxUses, usedCount=row.usedCount, expiresAt=row.expiresAt)

@router.post("/invites", response_model=InviteInfo, status_code=201, summary="创建邀请链接（需新增用户权限）")
async def create_invite_api(
    payload: InviteCreate,
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    full = await crud.get_user_by_username_full(session, current_user.username)
    _ensure_create_users(full or {}, current_user.username)
    from json import dumps as _jdumps
    from string import ascii_letters, digits
    from random import choices
    code = ''.join(choices(ascii_letters + digits, k=16))
    # 规范化权限：'allow'->True, 'deny'->False, 其他/None 跳过
    perm_json = None
    if payload.permissions is not None:
        norm: Dict[str, bool] = {}
        for k, v in payload.permissions.items():
            if isinstance(v, bool):
                norm[k] = v
            elif isinstance(v, str):
                vv = v.strip().lower()
                if vv == 'allow':
                    norm[k] = True
                elif vv == 'deny':
                    norm[k] = False
                # 'inherit' 或其他值跳过
        perm_json = _jdumps(norm, ensure_ascii=False)
    inv_id = await crud.create_invite(
        session,
        created_by_user_id=(full or {}).get('id'),
        code=code,
        max_uses=payload.maxUses,
        # 注意：crud.create_invite 当前签名尚未包含 expires_at，这里临时在下面统一更新
        per_hour_limit=payload.perHourLimit,
        permissions_json=perm_json,
        remark=payload.remark,
    )
    # 单独更新过期时间
    if payload.expiresAt is not None:
        await session.execute(sa_update(InviteModel).where(InviteModel.id == inv_id).values(expiresAt=payload.expiresAt))
        await session.commit()
    # 直接按ID读取，避免因过期/状态过滤导致的失配
    row = await session.get(InviteModel, inv_id)
    if not row:
        raise HTTPException(status_code=500, detail="invite create failed")
    # 构造返回（尽量与列表结构保持一致）
    import json as _json
    try:
        perms = _json.loads(getattr(row, 'permissionsJson', '') or '{}')
    except Exception:
        perms = None
    return InviteInfo(
        id=row.id,
        code=row.code,
        createdByUserId=row.createdByUserId,
        createdAt=row.createdAt,
        maxUses=row.maxUses,
        usedCount=row.usedCount or 0,
        expiresAt=getattr(row, 'expiresAt', None),
        isExpired=(getattr(row, 'expiresAt', None) is not None and row.expiresAt <= get_now()),
        perHourLimit=getattr(row, 'perHourLimit', None),
        permissions=perms,
        remark=getattr(row, 'remark', None),
        isEnabled=getattr(row, 'isEnabled', True)
    )

@router.delete("/invites/{inviteId}", status_code=204, summary="删除邀请链接（需新增用户权限）")
async def delete_invite_api(
    inviteId: int,
    session: AsyncSession = Depends(get_db_session),
    current_user: models.User = Depends(security.get_current_user)
):
    full = await crud.get_user_by_username_full(session, current_user.username)
    _ensure_create_users(full or {}, current_user.username)
    ok = await crud.delete_invite(session, inviteId)
    if not ok:
        raise HTTPException(status_code=404, detail="Invite not found")
    return

# 公共注册：挂在 auth 路由前缀下更合适，由 ui_api.py 注册 auth_router。这里只提供函数，实在不方便直接挂载。
@router.post("/auth/register", status_code=201, summary="受邀请注册")
async def invited_register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_db_session)
):
    # 先直接读取原始记录，以便精确反馈过期/人数用尽的原因
    row = (await session.execute(select(InviteModel).where(InviteModel.code == payload.code))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail="邀请码不存在")
    # 过期判断
    if getattr(row, 'expiresAt', None) is not None and row.expiresAt <= get_now():
        raise HTTPException(status_code=400, detail="邀请码已过期")
    # 数量用尽
    if row.maxUses is not None and (row.usedCount or 0) >= row.maxUses:
        raise HTTPException(status_code=400, detail="邀请码无可用次数")
    # 启用状态校验（禁用视为不可用）
    if getattr(row, 'isEnabled', True) is False:
        raise HTTPException(status_code=400, detail="邀请码不存在")
    # 转换为通用字典用于后续创建
    inv = {
        'id': row.id,
        'perHourLimit': getattr(row, 'perHourLimit', None),
        'permissions': None,
        'remark': getattr(row, 'remark', None),
    }
    try:
        import json as _json
        inv['permissions'] = _json.loads(getattr(row, 'permissionsJson', '') or '{}')
    except Exception:
        inv['permissions'] = None
    if (payload.username or '').lower() == 'admin':
        raise HTTPException(status_code=403, detail="Cannot register super admin username")
    existing = await crud.get_user_by_username(session, payload.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    from ..security import get_password_hash
    import json as _json
    perm_str = _json.dumps(inv.get('permissions') or {}, ensure_ascii=False) if inv.get('permissions') is not None else None
    user_id = await crud.create_user_with_permissions(
        session,
        username=payload.username,
        password_hash=get_password_hash(payload.password),
        role='user',
        per_hour_limit=inv.get('perHourLimit'),
        can_create_admin=False,
        permissions_json=perm_str,
        remark=inv.get('remark')
    )
    await crud.increment_invite_used(session, inv['id'])
    return {"id": user_id}
