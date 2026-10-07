from typing import Callable

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.database import get_db
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.managed_certificate import ManagedCertificate
from app.models.one_to_one import OneToOneRule
from app.models.openvpn_instance import OpenVpnInstance
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule
from app.models.wireguard_tunnel import WireguardTunnel
from app.schemas.search import SearchHit

router = APIRouter(prefix="/search", tags=["search"])

# Below this, ILIKE '%q%' on a handful of local tables is instant and a
# shorter query mostly returns noise — not a real limit on data volume.
MIN_QUERY_LENGTH = 2

# One spec = one queryable model within a result category. A category with
# several specs (NAT's port-forward/one-to-one/outbound rules; VPN's
# IPsec/OpenVPN/WireGuard) competes on relevance across all of them together
# — see _search_models — rather than giving each sub-type its own `limit`
# slots regardless of how well it actually matches.
Spec = tuple[type, list[ColumnElement], object, Callable[..., SearchHit]]


def _strip_scheme(base_url: str) -> str:
    return base_url.removeprefix("https://").removeprefix("http://")


def _match_score(text: str | None, q: str) -> int:
    """Cheap relevance heuristic — no fuzzy/edit-distance matching, just
    reward tighter matches so e.g. a rule *named* "vpn-uplink" outranks one
    that merely mentions "vpn" in its destination net. `q` must already be
    lowercased; `text` may be `None` for optional columns."""
    if not text:
        return 0
    t = text.lower()
    if t == q:
        return 100
    if t.startswith(q):
        return 80
    idx = t.find(q)
    if idx == -1:
        return 0
    if idx == 0 or not t[idx - 1].isalnum():
        return 60
    return 40


async def _search_models(
    db: AsyncSession,
    specs: list[Spec],
    q: str,
    limit: int,
) -> list[SearchHit]:
    like = f"%{q}%"
    # Widen the per-spec candidate pool beyond `limit` so the cross-spec
    # rescore below has enough to choose from — otherwise an early spec's
    # weaker matches could crowd out a later spec's stronger ones just by
    # being fetched first.
    candidate_limit = min(max(limit * 2, 10), 30)
    scored: list[tuple[int, object, Callable[..., SearchHit]]] = []
    for model, columns, order_by, to_hit in specs:
        rows = (
            await db.scalars(
                select(model).where(or_(*[c.ilike(like) for c in columns])).order_by(order_by).limit(candidate_limit)
            )
        ).all()
        for row in rows:
            score = max(_match_score(getattr(row, c.key), q) for c in columns)
            scored.append((score, row, to_hit))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [to_hit(row) for _, row, to_hit in scored[:limit]]


@router.get("", response_model=list[SearchHit])
async def search(
    q: str = Query(default=""),
    limit: int = Query(default=5, ge=1, le=25),
    db: AsyncSession = Depends(get_db),
):
    q = q.strip().lower()
    if len(q) < MIN_QUERY_LENGTH:
        return []
    hits: list[SearchHit] = []

    hits += await _search_models(
        db,
        [
            (
                OpnsenseHost,
                [OpnsenseHost.name, OpnsenseHost.base_url, OpnsenseHost.cluster_name],
                OpnsenseHost.name,
                lambda host: SearchHit(
                    category="hosts",
                    id=str(host.id),
                    title=host.name,
                    subtitle=host.cluster_name or _strip_scheme(host.base_url),
                    href=f"/hosts/{host.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                FirewallRule,
                [FirewallRule.description, FirewallRule.source_net, FirewallRule.destination_net],
                FirewallRule.updated_at.desc(),
                lambda r: SearchHit(
                    category="rules",
                    id=str(r.id),
                    title=r.description or f"{r.source_net} → {r.destination_net}",
                    subtitle=f"{r.interface} · {r.action}",
                    href=f"/rules?rule={r.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                FirewallAlias,
                [FirewallAlias.name, FirewallAlias.content, FirewallAlias.description],
                FirewallAlias.updated_at.desc(),
                lambda a: SearchHit(
                    category="aliases",
                    id=str(a.id),
                    title=a.name,
                    subtitle=a.content or a.description or None,
                    href=f"/aliases?alias={a.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                PortForwardRule,
                [PortForwardRule.description, PortForwardRule.destination_net, PortForwardRule.target],
                PortForwardRule.updated_at.desc(),
                lambda r: SearchHit(
                    category="nat",
                    id=str(r.id),
                    title=r.description or f"{r.destination_net} → {r.target}",
                    subtitle="Port Forward",
                    href=f"/nat?type=port-forward&rule={r.id}",
                ),
            ),
            (
                OneToOneRule,
                [OneToOneRule.description, OneToOneRule.source_net, OneToOneRule.external],
                OneToOneRule.updated_at.desc(),
                lambda r: SearchHit(
                    category="nat",
                    id=str(r.id),
                    title=r.description or f"{r.source_net} ↔ {r.external}",
                    subtitle="1:1 NAT",
                    href=f"/nat?type=one-to-one&rule={r.id}",
                ),
            ),
            (
                OutboundNatRule,
                [OutboundNatRule.description, OutboundNatRule.source_net, OutboundNatRule.target],
                OutboundNatRule.updated_at.desc(),
                lambda r: SearchHit(
                    category="nat",
                    id=str(r.id),
                    title=r.description or f"{r.source_net} → {r.target}",
                    subtitle="Outbound NAT",
                    href=f"/nat?type=outbound-nat&rule={r.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                IpsecTunnel,
                [IpsecTunnel.description, IpsecTunnel.remote_address, IpsecTunnel.remote_identity],
                IpsecTunnel.updated_at.desc(),
                lambda t: SearchHit(
                    category="vpn",
                    id=str(t.id),
                    title=t.description or t.remote_address,
                    subtitle=f"IPsec · {t.remote_address}",
                    href=f"/ipsec?tunnel={t.id}",
                ),
            ),
            (
                OpenVpnInstance,
                [OpenVpnInstance.description, OpenVpnInstance.remote, OpenVpnInstance.server_network],
                OpenVpnInstance.updated_at.desc(),
                lambda i: SearchHit(
                    category="vpn",
                    id=str(i.id),
                    title=i.description,
                    subtitle=f"OpenVPN · {i.role}",
                    href=f"/openvpn?instance={i.id}",
                ),
            ),
            (
                WireguardTunnel,
                [WireguardTunnel.server_name, WireguardTunnel.peer_name, WireguardTunnel.peer_allowed_ips],
                WireguardTunnel.updated_at.desc(),
                lambda t: SearchHit(
                    category="vpn",
                    id=str(t.id),
                    title=f"{t.server_name} · {t.peer_name}",
                    subtitle="WireGuard",
                    href=f"/wireguard?tunnel={t.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                ManagedCertificate,
                [ManagedCertificate.description, ManagedCertificate.commonname],
                ManagedCertificate.updated_at.desc(),
                lambda c: SearchHit(
                    category="certificates",
                    id=str(c.id),
                    title=c.description or c.commonname,
                    subtitle=c.commonname or None,
                    href=f"/certificates?cert={c.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                DeviceGroup,
                [DeviceGroup.name, DeviceGroup.description],
                DeviceGroup.name,
                lambda g: SearchHit(
                    category="groups",
                    id=str(g.id),
                    title=g.name,
                    subtitle=g.description or None,
                    href=f"/groups?group={g.id}",
                ),
            ),
        ],
        q,
        limit,
    )

    hits += await _search_models(
        db,
        [
            (
                PolicyHierarchyRule,
                [PolicyHierarchyRule.description, PolicyHierarchyRule.source_net, PolicyHierarchyRule.destination_net],
                PolicyHierarchyRule.updated_at.desc(),
                lambda r: SearchHit(
                    category="policy-hierarchy",
                    id=str(r.id),
                    title=r.description or f"{r.source_net} → {r.destination_net}",
                    subtitle=f"{r.rulebase} · {r.interface}",
                    href=f"/policy-hierarchy?group={r.hierarchy_group_id}",
                ),
            ),
        ],
        q,
        limit,
    )

    return hits
