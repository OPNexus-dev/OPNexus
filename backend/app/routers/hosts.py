import logging
import uuid
from app.errors import ErrorCode, api_error
from app.models.port_forward_deployment import PortForwardDeployment
from app.models.one_to_one_deployment import OneToOneDeployment
from app.models.outbound_nat_deployment import OutboundNatDeployment

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.services import audit_service
from app.services.policy_service import policy_lock
from app.models.host import OpnsenseHost
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.rule_deployment import RuleDeployment
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.models.managed_certificate import ManagedCertificate
from app.models.acme_account import AcmeAccount
from app.models.acme_certificate_request import AcmeCertificateRequest
from app.models.alias_deployment import AliasDeployment
from app.schemas.host import AliasOption, FirmwareInfo, HostBackup, HostCa, HostCertificate, HostCreate, HostRead, HostUpdate, TlsProbeRequest, TlsProbeResult, InterfaceOption, LiveRule
from app.services import backup_store, opnsense_client, opnsense_secret_store, tls_pin_service
from app.services.alias_service import get_aliases
from app.services.firmware_service import get_firmware_info
from app.services.host_status_service import get_statuses
from app.services.interface_service import get_interfaces, get_protocols
from app.services.live_certificates_service import get_live_cas, get_live_certificates
from app.services.live_rules_service import get_live_rules
from app.services.opnsense_client import OpnsenseAPIError

router = APIRouter(prefix="/hosts", tags=["hosts"])
logger = logging.getLogger(__name__)


def _upstream_error(host: OpnsenseHost, exc: OpnsenseAPIError) -> HTTPException:
    """OpnsenseAPIError's message embeds the raw upstream response body
    (see opnsense_client._do_request) — useful in a log, not something to
    hand an authenticated-but-otherwise-unprivileged (read_only) caller.
    Full detail goes to the backend log (visible via `docker compose logs
    backend` since the logging fix), client gets a generic 502."""
    logger.warning("OPNsense-Anfrage an Host '%s' fehlgeschlagen: %s", host.name, exc)
    return api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name)


def _to_read(host: OpnsenseHost, info: dict) -> HostRead:
    return HostRead(
        id=host.id,
        name=host.name,
        base_url=host.base_url,
        verify_tls=host.verify_tls,
        tls_pinned=bool(host.tls_ca_pem),
        tls_fingerprint=tls_pin_service.fingerprint_of(host.tls_ca_pem),
        enabled=host.enabled,
        cluster_name=host.cluster_name,
        policy_hierarchy_group_id=host.policy_hierarchy_group_id,
        created_at=host.created_at,
        status=info.get("status", "offline"),
        carp_status=info.get("carp_status"),
        status_detail=info.get("status_detail"),
    )


async def _get_host_or_404(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.get("", response_model=list[HostRead])
async def list_hosts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OpnsenseHost).order_by(OpnsenseHost.name))
    hosts = result.scalars().all()
    statuses = await get_statuses(hosts)
    return [_to_read(host, statuses.get(host.id, {})) for host in hosts]


async def _checked_pin(base_url: str, pem: str) -> str:
    """Validate the PEM and prove the firewall really presents it."""
    try:
        tls_pin_service.parse_pem(pem)
    except tls_pin_service.TlsPinError:
        raise api_error(422, ErrorCode.TLS_PEM_INVALID)
    try:
        await tls_pin_service.verify_matches(base_url, pem)
    except tls_pin_service.TlsPinError as exc:
        logger.warning("TLS-Pin für %s abgelehnt: %s", base_url, exc)
        raise api_error(422, ErrorCode.TLS_PIN_MISMATCH)
    return pem.strip() + "\n"


@router.post("/tls-probe", response_model=TlsProbeResult)
async def tls_probe(payload: TlsProbeRequest):
    """Read (not trust) the certificate a firewall presents, so a human can compare
    its fingerprint with the one shown on the firewall before pinning it."""
    try:
        return await tls_pin_service.probe(payload.base_url.rstrip("/"))
    except tls_pin_service.TlsPinError as exc:
        logger.warning("TLS-Probe %s fehlgeschlagen: %s", payload.base_url, exc)
        raise api_error(502, ErrorCode.TLS_PROBE_FAILED)


@router.post("", response_model=HostRead, status_code=201)
async def create_host(payload: HostCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(OpnsenseHost).where(OpnsenseHost.name == payload.name))
    if existing:
        raise api_error(409, ErrorCode.HOST_MIT_DIESEM_NAMEN_EXISTIERT_BEREITS)
    if payload.policy_hierarchy_group_id and not await db.get(PolicyHierarchyGroup, payload.policy_hierarchy_group_id):
        raise api_error(400, ErrorCode.POLICY_HIERARCHIE_GRUPPE_NICHT_GEFUNDEN)

    base_url = payload.base_url.rstrip("/")
    pinned_pem = await _checked_pin(base_url, payload.tls_ca_pem) if payload.tls_ca_pem else None
    host = OpnsenseHost(
        name=payload.name,
        base_url=base_url,
        api_key=payload.api_key,
        verify_tls=payload.verify_tls or bool(pinned_pem),
        tls_ca_pem=pinned_pem,
        enabled=payload.enabled,
        cluster_name=payload.cluster_name or None,
        policy_hierarchy_group_id=payload.policy_hierarchy_group_id,
    )
    db.add(host)
    await db.flush()
    opnsense_secret_store.save_secret(host.id, payload.api_secret)
    await db.commit()

    statuses = await get_statuses([host])
    return _to_read(host, statuses.get(host.id, {}))


@router.get("/{host_id}", response_model=HostRead)
async def get_host(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    statuses = await get_statuses([host])
    return _to_read(host, statuses.get(host.id, {}))


@router.patch("/{host_id}", response_model=HostRead)
async def update_host(host_id: uuid.UUID, payload: HostUpdate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await _get_host_or_404(db, host_id)

    if payload.name is not None:
        existing = await db.scalar(
            select(OpnsenseHost).where(OpnsenseHost.name == payload.name, OpnsenseHost.id != host_id)
        )
        if existing:
            raise api_error(409, ErrorCode.HOST_MIT_DIESEM_NAMEN_EXISTIERT_BEREITS)
        host.name = payload.name
    if payload.base_url is not None:
        host.base_url = payload.base_url
    if payload.verify_tls is not None:
        host.verify_tls = payload.verify_tls
    if payload.clear_tls_pin:
        host.tls_ca_pem = None
    elif payload.tls_ca_pem:
        host.tls_ca_pem = await _checked_pin(host.base_url, payload.tls_ca_pem)
        host.verify_tls = True
    if payload.enabled is not None:
        host.enabled = payload.enabled
    if payload.clear_cluster_name:
        host.cluster_name = None
    elif payload.cluster_name is not None:
        host.cluster_name = payload.cluster_name
    if payload.clear_policy_hierarchy_group_id:
        host.policy_hierarchy_group_id = None
    elif payload.policy_hierarchy_group_id is not None:
        if not await db.get(PolicyHierarchyGroup, payload.policy_hierarchy_group_id):
            raise api_error(400, ErrorCode.POLICY_HIERARCHIE_GRUPPE_NICHT_GEFUNDEN)
        host.policy_hierarchy_group_id = payload.policy_hierarchy_group_id

    await db.commit()
    statuses = await get_statuses([host])
    return _to_read(host, statuses.get(host.id, {}))


@router.delete("/{host_id}", status_code=204)
async def delete_host(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    # Both deployment tables FK to opnsense_hosts with ondelete=CASCADE, so
    # deleting the host would silently drop OPNexus's only record of what
    # it pushed there — the rule/alias itself stays live on the actual
    # firewall, now completely untracked. Block until those are cleared
    # (delete the rule/alias, or remove it as a target and clean up the
    # deployment under "Prüfen & Push").
    if await db.scalar(select(RuleDeployment.id).where(RuleDeployment.host_id == host_id).limit(1)):
        raise api_error(409, ErrorCode.HOST_HAT_NOCH_AKTIVE_REGEL_DEPLOYMENTS_DIESE_ZUERST_0BF41721)
    if await db.scalar(select(AliasDeployment.id).where(AliasDeployment.host_id == host_id).limit(1)):
        raise api_error(409, ErrorCode.HOST_HAT_NOCH_AKTIVE_ALIAS_DEPLOYMENTS_DIESE_ZUERST_F4A3ED46)
    for deployment_model in (PortForwardDeployment, OneToOneDeployment, OutboundNatDeployment):
        if await db.scalar(select(deployment_model.id).where(deployment_model.host_id == host_id).limit(1)):
            raise api_error(409, ErrorCode.HOST_HAT_NOCH_NAT_DEPLOYMENTS_DIESE_ZUERST_GEZIELT_0C2A9E85)
    if await db.scalar(select(PolicyHierarchyRuleDeployment.id).where(PolicyHierarchyRuleDeployment.host_id == host_id).limit(1)):
        raise api_error(409, ErrorCode.HOST_HAT_NOCH_AKTIVE_POLICY_HIERARCHIE_DEPLOYMENTS_DIESE_ADD78845)
    # VPN tunnels, certificates and ACME objects likewise cascade away with the host
    # while staying live on the firewall: block until they are removed there.
    for deployment_model in (IpsecTunnelDeployment, OpenVpnInstanceDeployment, WireguardTunnelDeployment):
        if await db.scalar(select(deployment_model.id).where(deployment_model.host_id == host_id).limit(1)):
            raise api_error(409, ErrorCode.HOST_HAT_NOCH_VPN_ODER_ZERTIFIKATS_OBJEKTE_DIESE_ZUERST_ENTFERNEN)
    for cert_model in (ManagedCertificate, AcmeAccount, AcmeCertificateRequest):
        # A never-deployed draft only exists locally and may go with the host.
        if await db.scalar(
            select(cert_model.id).where(
                cert_model.host_id == host_id,
                (cert_model.opnsense_uuid.is_not(None)) | (cert_model.status == "success"),
            ).limit(1)
        ):
            raise api_error(409, ErrorCode.HOST_HAT_NOCH_VPN_ODER_ZERTIFIKATS_OBJEKTE_DIESE_ZUERST_ENTFERNEN)
    from app.models.host_operation import HostOperation
    from app.services.host_maintenance_service import ACTIVE
    if await db.scalar(select(HostOperation.id).where(HostOperation.host_id == host_id, HostOperation.status.in_(ACTIVE)).limit(1)):
        raise api_error(409, ErrorCode.HOST_HAT_EINE_OFFENE_WARTUNGSAKTION_ZUERST_PRUEFEN_UND_8CC5FBE0)
    # Local drafts cascade away with the host; their file-backed secrets must not outlive them.
    from app.services import acme_secret_store, cert_key_store
    draft_certs = (await db.scalars(select(ManagedCertificate.id).where(ManagedCertificate.host_id == host_id))).all()
    draft_accounts = (await db.scalars(select(AcmeAccount.id).where(AcmeAccount.host_id == host_id))).all()
    await db.delete(host)
    await db.commit()
    opnsense_secret_store.delete_secret(host_id)
    for cert_id in draft_certs:
        cert_key_store.delete_secret(cert_id)
    for account_id in draft_accounts:
        acme_secret_store.delete_secret(account_id)


@router.get("/{host_id}/live-rules", response_model=list[LiveRule])
async def live_rules(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_live_rules(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/{host_id}/certificates", response_model=list[HostCertificate])
async def certificates(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_live_certificates(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/{host_id}/cas", response_model=list[HostCa])
async def cas(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_live_cas(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.post("/{host_id}/backups", response_model=HostBackup, status_code=201)
async def trigger_backup(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        content = await opnsense_client.download_backup(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc
    backup_id = backup_store.save_snapshot(host_id, content)
    return next(s for s in backup_store.list_snapshots(host_id) if s["id"] == backup_id)


@router.get("/{host_id}/backups", response_model=list[HostBackup])
async def list_host_backups(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _get_host_or_404(db, host_id)
    return backup_store.list_snapshots(host_id)


@router.get("/{host_id}/backups/{backup_id}")
async def download_host_backup(
    host_id: uuid.UUID, backup_id: str,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user),
):
    """Raw config.xml snapshot — contains password hashes, VPN keys and other
    firewall secrets, so unlike the (metadata-only) listing it is not part of
    the "read_only may GET everything" allowance. Every attempt is audited
    (who/which snapshot, never the content)."""
    host = await _get_host_or_404(db, host_id)
    event = {"kind": "backup", "action": "download", "entity_id": backup_id[:36], "host_id": str(host_id), "host_name": host.name}
    if user.role != "admin":
        await audit_service.persist(**event, status="denied", message="Rohdownload nur für Administratoren")
        raise api_error(403, ErrorCode.READ_ONLY_ACCESS)
    content = backup_store.read_snapshot(host_id, backup_id)
    if content is None:
        raise api_error(404, ErrorCode.BACKUP_NICHT_GEFUNDEN)
    await audit_service.persist(**event, status="accepted")
    return Response(
        content=content,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{backup_id}"'},
    )


@router.get("/{host_id}/firmware-info", response_model=FirmwareInfo)
async def firmware_info(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_firmware_info(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/{host_id}/interfaces", response_model=list[InterfaceOption])
async def interfaces(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_interfaces(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/{host_id}/protocols", response_model=list[InterfaceOption])
async def protocols(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_protocols(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc


@router.get("/{host_id}/aliases", response_model=list[AliasOption])
async def aliases(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _get_host_or_404(db, host_id)
    try:
        return await get_aliases(host)
    except OpnsenseAPIError as exc:
        raise _upstream_error(host, exc) from exc
