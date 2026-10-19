import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.schemas.user import UserCreate, UserPasswordReset, UserRead, UserRoleUpdate
from app.services import api_token_service, auth_service, totp_service

router = APIRouter(prefix="/users", tags=["users"])


async def _admin_count(db: AsyncSession, exclude_id: uuid.UUID | None = None) -> int:
    query = select(func.count()).select_from(AdminUser).where(AdminUser.role == "admin")
    if exclude_id is not None:
        query = query.where(AdminUser.id != exclude_id)
    return await db.scalar(query)


@router.get("", response_model=list[UserRead])
async def list_users(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AdminUser).order_by(AdminUser.username))).all()
    return rows


@router.post("", response_model=UserRead, status_code=201)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(AdminUser).where(AdminUser.username == payload.username))
    if existing:
        raise api_error(409, ErrorCode.BENUTZERNAME_BEREITS_VERGEBEN)
    user = AdminUser(
        username=payload.username,
        password_hash=await auth_service.hash_password_async(payload.password),
        role=payload.role.value,
    )
    db.add(user)
    await db.commit()
    return user


@router.patch("/{user_id}/role", response_model=UserRead)
async def update_role(user_id: uuid.UUID, payload: UserRoleUpdate, db: AsyncSession = Depends(get_db)):
    target = await db.get(AdminUser, user_id)
    if target is None:
        raise api_error(404, ErrorCode.BENUTZER_NICHT_GEFUNDEN)
    if target.auth_source == "oidc":
        raise api_error(409, ErrorCode.OIDC_MANAGED_USER)  # the identity provider's groups decide, at every login
    if target.role == "admin" and payload.role.value != "admin" and await _admin_count(db, exclude_id=target.id) == 0:
        raise api_error(409, ErrorCode.MINDESTENS_EIN_ADMIN_ACCOUNT_MUSS_BESTEHEN_BLEIBEN)
    target.role = payload.role.value
    await db.commit()
    return target


@router.post("/{user_id}/reset-2fa", response_model=UserRead)
async def reset_two_factor(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Admin recovery for a user who lost their authenticator and recovery codes."""
    target = await db.get(AdminUser, user_id)
    if target is None:
        raise api_error(404, ErrorCode.BENUTZER_NICHT_GEFUNDEN)
    totp_service.delete_secrets(target.id)
    target.totp_enabled = False
    target.totp_recovery_hashes = None
    target.totp_last_step = 0
    # The account lost its second factor (maybe to someone else): force a fresh login.
    await auth_service.revoke_sessions(db, target.id, commit=False)
    await db.commit()
    return target


@router.post("/{user_id}/reset-password", response_model=UserRead)
async def reset_password(user_id: uuid.UUID, payload: UserPasswordReset, db: AsyncSession = Depends(get_db)):
    target = await db.get(AdminUser, user_id)
    if target is None:
        raise api_error(404, ErrorCode.BENUTZER_NICHT_GEFUNDEN)
    if target.auth_source == "oidc":
        raise api_error(409, ErrorCode.OIDC_MANAGED_USER)
    target.password_hash = await auth_service.hash_password_async(payload.new_password)
    # A reset is the answer to a suspected compromise: every session of the
    # account ends with the old password, atomically with the new hash.
    await auth_service.revoke_sessions(db, target.id, commit=False)
    await api_token_service.revoke_all(db, target.id, commit=False)
    await db.commit()
    return target


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current: AdminUser = Depends(current_user),
):
    if user_id == current.id:
        raise api_error(400, ErrorCode.DER_EIGENE_ACCOUNT_KANN_NICHT_GELOESCHT_WERDEN)
    target = await db.get(AdminUser, user_id)
    if target is None:
        raise api_error(404, ErrorCode.BENUTZER_NICHT_GEFUNDEN)
    # No separate "last admin" guard here: only an admin can reach this
    # endpoint (read_only is rejected earlier, in AuthMiddleware) and the
    # self-delete check above already guarantees target != current — so
    # `current` is always a still-existing admin afterward, meaning the
    # admin count can never reach zero this way.
    await db.delete(target)
    await db.commit()
