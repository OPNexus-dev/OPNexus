from typing import Any, Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from app import runtime_settings
from app.config import settings
from app.database import get_db
from app.models.preferences import UserPreferences
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.errors import ErrorCode, api_error
from app.services import audit_service

router=APIRouter(prefix='/settings',tags=['settings'])

class Preferences(BaseModel):
    theme: Literal['light','dark','system'] | None = None
    language: Literal['de','en'] | None = None
    setup_done: bool | None = None

@router.get('')
async def read(user: AdminUser=Depends(current_user),db:AsyncSession=Depends(get_db)):
    preferences=await db.get(UserPreferences,user.id)
    return {'username':user.username,'theme':preferences.theme if preferences else 'system',
            'language':preferences.language if preferences else 'de',
            'setup_done':preferences.setup_done if preferences else False,
            'deploy_confirm_window_seconds':runtime_settings.value("deploy_confirm_window_seconds"),
            'session_lifetime_seconds':runtime_settings.value("session_lifetime_days")}

@router.put('')
async def update(payload:Preferences,user:AdminUser=Depends(current_user),db:AsyncSession=Depends(get_db)):
    username = user.username
    current = await db.get(UserPreferences, user.id)
    theme = payload.theme or (current.theme if current else 'system')
    language = payload.language or (current.language if current else 'de')
    setup_done = payload.setup_done if payload.setup_done is not None else bool(current.setup_done if current else False)
    await db.execute(insert(UserPreferences).values(user_id=user.id,theme=theme,language=language,setup_done=setup_done).on_conflict_do_update(index_elements=['user_id'],set_={'theme':theme,'language':language,'setup_done':setup_done}))
    await db.commit()
    return {'username':username,'theme':theme,'language':language,'setup_done':setup_done,
            'deploy_confirm_window_seconds':runtime_settings.value("deploy_confirm_window_seconds"),
            'session_lifetime_seconds':runtime_settings.value("session_lifetime_days")}



# ── Settings › Betrieb: values that used to need the .env (app/runtime_settings.py) ──────────────────────────

class SystemSettingsUpdate(BaseModel):
    # key -> new value; null (or "") resets the setting to its default.
    values: dict[str, Any] = Field(max_length=50)


@router.get('/system')
async def read_system(user: AdminUser = Depends(current_user)):
    return {'settings': runtime_settings.describe()}


@router.put('/system')
async def update_system(payload: SystemSettingsUpdate, user: AdminUser = Depends(current_user),
                        db: AsyncSession = Depends(get_db)):
    if user.role != 'admin':
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    before = {item['key']: item for item in runtime_settings.describe()}
    changes = {}
    for key, new in payload.values.items():
        spec = runtime_settings.BY_KEY.get(key)
        if spec is None:
            continue  # rejected by save() below with a proper error
        if spec.kind in ('secret_url', 'pem'):  # never put the secret (or a whole certificate) into the audit trail
            changes[key] = {'old': 'gesetzt' if before[key].get('is_set') else 'leer', 'new': 'leer' if not new else 'gesetzt'}
        else:
            changes[key] = {'old': before[key]['value'], 'new': new if new not in ('', None) else before[key]['default']}
    try:
        if changes:
            db.add(audit_service.make_event(
                kind='settings', action='system_settings', status='accepted', entity_id='system',
                message='Betriebseinstellungen geändert', details={'changes': changes}))
        await runtime_settings.save(db, payload.values, user.username)
    except runtime_settings.SettingError as exc:
        await db.rollback()
        raise api_error(422, ErrorCode.SETTING_INVALID, setting=exc.key, reason=exc.reason) from None
    return {'settings': runtime_settings.describe()}
