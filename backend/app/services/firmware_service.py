"""Extracts product_id/product_version from core/firmware/info — that
response also includes a `package` array with hundreds of entries (every
installed and available package), which is discarded here rather than
ever being sent to the frontend."""
from app.models.host import OpnsenseHost
from app.schemas.host import FirmwareInfo
from app.services import opnsense_client


async def get_firmware_info(host: OpnsenseHost) -> FirmwareInfo:
    data = await opnsense_client.get_firmware_info(host)
    return FirmwareInfo(
        product_id=data.get("product_id", "opnsense"),
        product_version=data.get("product_version", "?"),
    )
