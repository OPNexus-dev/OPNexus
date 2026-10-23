"""Firewall aliases for populating the Source/Destination autocomplete —
sourced live via `firewall/alias/searchItem`, the same list OPNsense's own
rule-edit UI suggests from. Covers user-defined aliases (hosts, networks,
ports, ...) as well as OPNsense's auto-generated `__<if>_network` aliases
("this interface's subnet").
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.models.host import OpnsenseHost
from app.schemas.host import AliasOption
from app.services import opnsense_client
from app.services.deploy_flow import unsettled
from app.services.opnsense_client import OpnsenseAPIError


async def _rows(host: OpnsenseHost) -> list[dict]:
    data = await opnsense_client.search_aliases(host)
    return data.get("rows", []) if isinstance(data, dict) else []


async def get_aliases(host: OpnsenseHost) -> list[AliasOption]:
    return [
        AliasOption(
            name=row["name"],
            type=row.get("type", ""),
            description=row.get("description") or "",
            content=row.get("content") or "",
            enabled=row.get("enabled") in ("1", 1, True),
        )
        for row in await _rows(host)
        if row.get("name")
    ]


async def check_alias_consistency(hosts: list[OpnsenseHost], value: str) -> dict[str, str]:
    """For a rule pushed to more than one host: if `value` names an alias
    that exists on more than one target, returns {host_name: content} so
    the caller can tell whether it's the *same* alias content everywhere
    (the aliases in this app's own fleet are config-synced, so in practice
    this is nearly always consistent — but nothing enforces that, and nothing
    else would catch it silently drifting). A `value` that isn't a known
    alias name anywhere (a literal "any"/CIDR/IP) simply returns {}, since
    there's nothing host-specific to compare — see check_interface_consistency
    for the same pattern applied to interface identifiers.
    """
    contents: dict[str, str] = {}
    for host in hosts:
        try:
            rows = await _rows(host)
        except OpnsenseAPIError:
            continue
        match = next((row for row in rows if row.get("name") == value), None)
        if match is not None:
            contents[host.name] = match.get("content") or ""
    return contents


def describe_alias_contents(contents: dict[str, str]) -> str:
    """Human-readable form of check_alias_consistency's result for error
    messages: OPNsense stores alias content newline-separated, which a plain
    repr() would render as literal "\\n" in the UI."""
    return "; ".join(
        f"{name}: {', '.join(line.strip() for line in content.splitlines() if line.strip()) or '—'}"
        for name, content in sorted(contents.items())
    )


async def ensure_aliases_settled(db: AsyncSession, hosts: list[OpnsenseHost], values: list[str | None]) -> None:
    """Refuses to deploy something that references an OPNexus-managed alias
    whose own deployment on one of `hosts` is still unconfirmed (pending) or
    has a failed rollback. A pending alias is auto-reverted when its confirm
    window runs out; if a rule already uses it by then, OPNsense refuses to
    delete it ("Alias in use"), the alias is stuck in revert_failed and the
    rule's host can no longer be pushed to. Aliases that are not managed by
    OPNexus (hand-made, `__lan_network`, literal IPs) have no deployment row
    and are not affected."""
    names = {value for value in values if value}
    if not names or not hosts:
        return
    rows = (await db.execute(
        select(FirewallAlias.name, OpnsenseHost.name)
        .join(AliasDeployment, AliasDeployment.alias_id == FirewallAlias.id)
        .join(OpnsenseHost, OpnsenseHost.id == AliasDeployment.host_id)
        .where(
            FirewallAlias.name.in_(names),
            AliasDeployment.host_id.in_([host.id for host in hosts]),
            unsettled(AliasDeployment),
        )
        .order_by(FirewallAlias.name, OpnsenseHost.name)
    )).all()
    if rows:
        aliases = ", ".join(sorted({alias for alias, _ in rows}))
        host_names = ", ".join(sorted({host for _, host in rows}))
        raise api_error(409, ErrorCode.ALIAS_NOCH_NICHT_BESTAETIGT, alias=aliases, hosts=host_names)
