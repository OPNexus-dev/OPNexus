"""Shared test helpers."""


async def deploy_approved(client, deploy_url: str):
    """VPN deploys require the token of a fresh preview (like the UI's review
    step): fetch it, then POST the deploy with it."""
    preview = await client.get(deploy_url.rsplit("/deploy", 1)[0] + "/preview")
    assert preview.status_code == 200, preview.text
    return await client.post(deploy_url, json={"preview_token": preview.json()["token"]})


async def delete_host(client, host_id):
    """DELETE /api/hosts/{id} with the confirmation the API now requires (T4): the host's own name, read from the DB
    so the helper does not touch the firewall mocks."""
    import uuid as _uuid

    from app.database import SessionLocal
    from app.models.host import OpnsenseHost

    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, _uuid.UUID(str(host_id)))
    return await client.delete(f"/api/hosts/{host_id}", params={"confirm_name": host.name if host else ""})
