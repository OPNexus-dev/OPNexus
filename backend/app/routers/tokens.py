"""Manage personal API tokens (see services/api_token_service.py for scopes).

Session-only: an API token cannot reach these endpoints (AuthMiddleware), so a
leaked token can never mint a replacement or list/revoke its siblings. A
read_only user may manage their own tokens, but only with read scope (an operator up to deploy).
"""
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.api_token import ApiToken
from app.models.user import AdminUser
from app.routers.auth import _account_failure, _account_success, _check_account_budget, current_user
from app.schemas.api_token import ApiTokenCreate, ApiTokenCreated, ApiTokenRead, TokenScope
from app.services import api_token_service, auth_service

router = APIRouter(prefix="/tokens", tags=["tokens"])


def _read(token: ApiToken, owner: str) -> dict:
    return {
        "id": token.id, "name": token.name, "scope": token.scope, "prefix": token.prefix, "owner": owner,
        "created_at": token.created_at, "expires_at": token.expires_at,
        "last_used_at": token.last_used_at, "last_used_ip": token.last_used_ip,
    }


@router.get("", response_model=list[ApiTokenRead])
async def list_tokens(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Own tokens; an admin sees everybody's (to be able to revoke them)."""
    query = select(ApiToken, AdminUser.username).join(AdminUser, ApiToken.user_id == AdminUser.id)
    if user.role != "admin":
        query = query.where(ApiToken.user_id == user.id)
    rows = (await db.execute(query.order_by(ApiToken.created_at.desc()))).all()
    return [_read(token, owner) for token, owner in rows]


@router.post("", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: ApiTokenCreate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)
):
    allowed = {"admin": (TokenScope.read, TokenScope.deploy, TokenScope.admin), "operator": (TokenScope.read, TokenScope.deploy)}
    if payload.scope not in allowed.get(user.role, (TokenScope.read,)):
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_TOO_HIGH)
    _check_account_budget(user, "create-token")
    if not await auth_service.verify_password_async(user.password_hash, payload.current_password):
        _account_failure(user, "create-token")
        raise api_error(401, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    _account_success(user, "create-token")
    token, raw = await api_token_service.create(
        db, user, name=payload.name.strip(), scope=payload.scope.value, expires_in_days=payload.expires_in_days
    )
    return {**_read(token, user.username), "token": raw}


@router.delete("/{token_id}", status_code=204)
async def revoke_token(
    token_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)
):
    token = await db.get(ApiToken, token_id)
    if token is None or (token.user_id != user.id and user.role != "admin"):
        raise api_error(404, ErrorCode.API_TOKEN_NOT_FOUND)
    await db.delete(token)
    await db.commit()
