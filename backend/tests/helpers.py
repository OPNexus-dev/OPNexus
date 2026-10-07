"""Shared test helpers."""


async def deploy_approved(client, deploy_url: str):
    """VPN deploys require the token of a fresh preview (like the UI's review
    step): fetch it, then POST the deploy with it."""
    preview = await client.get(deploy_url.rsplit("/deploy", 1)[0] + "/preview")
    assert preview.status_code == 200, preview.text
    return await client.post(deploy_url, json={"preview_token": preview.json()["token"]})
