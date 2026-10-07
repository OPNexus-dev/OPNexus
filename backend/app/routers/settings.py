from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from app.config import settings
from app.database import get_db
from app.models.preferences import UserPreferences
from app.models.user import AdminUser
from app.routers.auth import current_user

router=APIRouter(prefix='/settings',tags=['settings'])

class Preferences(BaseModel):
    theme: Literal['light','dark','system'] | None = None
    language: Literal['de','en'] | None = None

@router.get('')
async def read(user: AdminUser=Depends(current_user),db:AsyncSession=Depends(get_db)):
    preferences=await db.get(UserPreferences,user.id)
    return {'username':user.username,'theme':preferences.theme if preferences else 'system',
            'language':preferences.language if preferences else 'de',
            'deploy_confirm_window_seconds':settings.deploy_confirm_window_seconds,
            'session_lifetime_seconds':settings.session_lifetime}

@router.put('')
async def update(payload:Preferences,user:AdminUser=Depends(current_user),db:AsyncSession=Depends(get_db)):
    username = user.username
    current = await db.get(UserPreferences, user.id)
    theme = payload.theme or (current.theme if current else 'system')
    language = payload.language or (current.language if current else 'de')
    await db.execute(insert(UserPreferences).values(user_id=user.id,theme=theme,language=language).on_conflict_do_update(index_elements=['user_id'],set_={'theme':theme,'language':language}))
    await db.commit()
    return {'username':username,'theme':theme,'language':language,
            'deploy_confirm_window_seconds':settings.deploy_confirm_window_seconds,
            'session_lifetime_seconds':settings.session_lifetime}
