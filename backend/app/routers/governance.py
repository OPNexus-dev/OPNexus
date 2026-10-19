"""Separation of duties: with ``operators_may_deploy`` off, only admins roll changes out (AuthMiddleware enforces it)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.governance import GovernanceSettings
from app.models.user import AdminUser
from app.routers.auth import current_user

router = APIRouter(prefix="/settings/governance", tags=["governance"])


class GovernanceRead(BaseModel):
    operators_may_deploy: bool


async def get_settings(db: AsyncSession) -> GovernanceSettings:
    row = await db.get(GovernanceSettings, 1)
    if row is None:
        row = GovernanceSettings(id=1, operators_may_deploy=True)
        db.add(row)
        await db.flush()
    return row


@router.get("", response_model=GovernanceRead)
async def read(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Readable by everybody signed in (the UI explains why a deploy was refused)."""
    return await get_settings(db)


@router.put("", response_model=GovernanceRead)
async def update(payload: GovernanceRead, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    row = await get_settings(db)
    row.operators_may_deploy = payload.operators_may_deploy
    await db.commit()
    return row
