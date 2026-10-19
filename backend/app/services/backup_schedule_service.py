"""Scheduled config backups: take, keep the newest N, copy encrypted offsite.

Called by a poll loop (main.py). Per enabled host a new snapshot is taken when the newest one
is older than the interval; the oldest beyond ``keep_count`` are deleted. When an offsite target
is set, every local snapshot that is not offsite yet is encrypted (backup_crypto) and uploaded
(s3_client); the ledger ``backup_uploads`` remembers what is already there. Nothing is ever
deleted offsite: use the bucket's lifecycle rules for that.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backup_settings import BackupSettings, BackupUpload
from app.models.host import OpnsenseHost
from app.services import backup_crypto, backup_store, notification_secret_store
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError
from app.services.s3_client import S3Client, S3Error

logger = logging.getLogger(__name__)

SINGLETON_ID = 1
S3_SECRET, PASSPHRASE = "backup_s3_secret", "backup_passphrase"
MAX_UPLOADS_PER_RUN = 20


async def get_settings(db: AsyncSession) -> BackupSettings:
    row = await db.get(BackupSettings, SINGLETON_ID)
    if row is None:
        row = BackupSettings(
            id=SINGLETON_ID, scheduled_enabled=True, interval_hours=24, keep_count=30, offsite_enabled=False,
            s3_region="us-east-1", s3_prefix="opnexus", s3_path_style=True, s3_secret_set=False, passphrase_set=False,
        )
        db.add(row)
        await db.flush()
    return row


def offsite_ready(cfg: BackupSettings) -> bool:
    return bool(cfg.offsite_enabled and cfg.s3_endpoint and cfg.s3_bucket and cfg.s3_access_key and cfg.s3_secret_set and cfg.passphrase_set)


def s3_for(cfg: BackupSettings) -> S3Client:
    secret = notification_secret_store.read_secret(S3_SECRET)
    if not (cfg.s3_endpoint and cfg.s3_bucket and cfg.s3_access_key and secret):
        raise S3Error("Offsite-Ziel unvollständig (Endpunkt, Bucket, Zugangsdaten)")
    return S3Client(endpoint=cfg.s3_endpoint, region=cfg.s3_region, bucket=cfg.s3_bucket, access_key=cfg.s3_access_key,
                    secret_key=secret, path_style=cfg.s3_path_style)


def remote_key(cfg: BackupSettings, host: OpnsenseHost, snapshot_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", host.name).strip("-") or "host"
    prefix = cfg.s3_prefix.strip("/")
    return "/".join(p for p in (prefix, f"{safe}-{str(host.id)[:8]}", snapshot_id.removesuffix(".xml") + ".opxbak") if p)


async def _take(host: OpnsenseHost) -> str:
    content = await client.download_backup(host)
    if b"<opnsense" not in content[:4096]:
        raise OpnsenseAPIError("Antwort ist keine OPNsense-Konfiguration")
    return backup_store.save_snapshot(host.id, content)


async def _prune(db: AsyncSession, host: OpnsenseHost, keep: int) -> int:
    old = backup_store.list_snapshots(host.id)[keep:]
    for snapshot in old:
        backup_store.delete_snapshot(host.id, snapshot["id"])
    if old:
        rows = (await db.scalars(select(BackupUpload).where(BackupUpload.host_id == host.id, BackupUpload.snapshot_id.in_([s["id"] for s in old])))).all()
        for row in rows:
            await db.delete(row)
    return len(old)


async def upload_pending(db: AsyncSession, cfg: BackupSettings, hosts: list[OpnsenseHost]) -> int:
    """Encrypt and upload local snapshots that are not offsite yet (newest first, bounded per run)."""
    passphrase = notification_secret_store.read_secret(PASSPHRASE)
    if not passphrase:
        raise S3Error("Passphrase fehlt oder ist nicht lesbar – bitte neu setzen")
    s3 = s3_for(cfg)
    done, budget = 0, MAX_UPLOADS_PER_RUN
    for host in hosts:
        have = set((await db.scalars(select(BackupUpload.snapshot_id).where(BackupUpload.host_id == host.id))).all())
        for snapshot in backup_store.list_snapshots(host.id):
            if snapshot["id"] in have:
                continue
            if budget == 0:
                return done
            content = backup_store.read_snapshot(host.id, snapshot["id"])
            if content is None:
                continue
            key = remote_key(cfg, host, snapshot["id"])
            await s3.put(key, backup_crypto.encrypt(content, passphrase))
            db.add(BackupUpload(host_id=host.id, snapshot_id=snapshot["id"], remote_key=key, uploaded_at=datetime.now(timezone.utc)))
            await db.commit()
            budget -= 1
            done += 1
    return done


async def run_due(db: AsyncSession, *, force: bool = False) -> dict:
    cfg = await get_settings(db)
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)).order_by(OpnsenseHost.name))).all())
    taken = failed = pruned = 0
    if cfg.scheduled_enabled or force:
        now = datetime.now(timezone.utc)
        for host in hosts:
            snapshots = backup_store.list_snapshots(host.id)
            if not force and snapshots and now - snapshots[0]["created_at"] < timedelta(hours=cfg.interval_hours):
                pruned += await _prune(db, host, cfg.keep_count)
                continue
            try:
                await _take(host)
                taken += 1
            except OpnsenseAPIError as exc:
                failed += 1
                logger.warning("Geplantes Backup von %s fehlgeschlagen: %s", host.name, exc)
            pruned += await _prune(db, host, cfg.keep_count)
        cfg.last_run_at = now
    uploaded = 0
    if cfg.offsite_enabled:
        try:
            if not offsite_ready(cfg):
                raise S3Error("Offsite-Ziel unvollständig (Endpunkt, Bucket, Zugangsdaten, Passphrase)")
            uploaded = await upload_pending(db, cfg, hosts)
            cfg.last_offsite_error = None
            if uploaded or cfg.last_offsite_ok_at is None:
                cfg.last_offsite_ok_at = datetime.now(timezone.utc)
        except S3Error as exc:
            cfg.last_offsite_error = str(exc)[:300]
            logger.warning("Offsite-Backup: %s", exc)
    await db.commit()
    return {"hosts": len(hosts), "taken": taken, "failed": failed, "pruned": pruned, "uploaded": uploaded}
