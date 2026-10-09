from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.host import OpnsenseHost
from app.services import network_drift_service

router = APIRouter(prefix="/network", tags=["network"])


@router.get("/drift")
async def network_drift(db: AsyncSession = Depends(get_db)):
    """Per cluster: differences in the network configuration between its nodes (live read of every node)."""
    hosts = (await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)).order_by(OpnsenseHost.name))).all()
    by_cluster: dict[str, list[OpnsenseHost]] = defaultdict(list)
    for host in hosts:
        if host.cluster_name:
            by_cluster[host.cluster_name].append(host)
    clusters = []
    for name, members in sorted(by_cluster.items()):
        if len(members) < 2:
            continue
        differences, compared, unreachable = await network_drift_service.compute_cluster(members)
        clusters.append({"name": name, "hosts": [h.name for h in members], "compared": compared,
                         "unreachable": unreachable, "differences": differences})
    return {"clusters": clusters}
