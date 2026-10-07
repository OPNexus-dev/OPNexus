from fastapi import APIRouter

from app.services import update_check_service

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/version-check")
async def version_check() -> dict:
    """Ist eine neuere OPNexus-Version veröffentlicht? Aus, solange
    UPDATE_CHECK_URL nicht gesetzt ist (dann ohne ausgehende Anfrage)."""
    return await update_check_service.check()
