import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
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


_ADMIN_LOCK_KEY = 728194032  # next to policy_lock's key; only admin role changes and deletions take it


async def _lock_admins(db: AsyncSession, current: AdminUser) -> None:
    """Serialize every change that can reduce the number of administrators and re-read the caller under the lock.

    The "at least one administrator" check counts rows; two requests that each see the other's admin row would both
    pass it. Held until commit/rollback (transaction-scoped advisory lock). The caller may have been demoted or
    deleted by the request that ran first, so it must still be an admin to proceed."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMIN_LOCK_KEY})
    fresh = await db.scalar(select(AdminUser).where(AdminUser.id == current.id).execution_options(populate_existing=True))
    if fresh is None or fresh.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)


@router.get("", response_model=list[UserRead])
async def list_users(user: AdminUser = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Admins see everyone. Every other role sees only its own account: the full list (names, roles, last login,
    2FA status) is a directory an attacker with any low-privilege account or token could use to pick targets."""
    query = select(AdminUser).order_by(AdminUser.username)
    if user.role != "admin":
        query = query.where(AdminUser.id == user.id)
    return (await db.scalars(query)).all()


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
async def update_role(
    user_id: uuid.UUID, payload: UserRoleUpdate, db: AsyncSession = Depends(get_db),
    current: AdminUser = Depends(current_user),
):
    await _lock_admins(db, current)
    target = await db.get(AdminUser, user_id, populate_existing=True)
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
    await _lock_admins(db, current)
    target = await db.get(AdminUser, user_id, populate_existing=True)
    if target is None:
        raise api_error(404, ErrorCode.BENUTZER_NICHT_GEFUNDEN)
    # Two admins deleting each other (or demoting/deleting in parallel) would each still see the other: the lock above
    # orders them, and the guard below counts what is left without the target.
    if target.role == "admin" and await _admin_count(db, exclude_id=target.id) == 0:
        raise api_error(409, ErrorCode.MINDESTENS_EIN_ADMIN_ACCOUNT_MUSS_BESTEHEN_BLEIBEN)
    await db.delete(target)
    await db.commit()
