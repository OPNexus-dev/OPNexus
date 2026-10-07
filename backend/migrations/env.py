"""Alembic-Umgebung für OPNexus.

Zwei Betriebsarten:
- In-Process (der Normalfall): app/migrate.py übergibt eine bereits offene
  Verbindung über config.attributes["connection"], damit Advisory-Lock,
  Legacy-Brücke und Migration in einer Transaktion laufen.
- CLI (`alembic revision --autogenerate`, `alembic upgrade head`): baut
  eine eigene async Engine aus DATABASE_URL.
"""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import Base
from app.migrate import load_models

load_models()

config = context.config
target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )


def _run_sync(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline-Modus (--sql) wird nicht unterstützt.")

_connection = config.attributes.get("connection")
if _connection is not None:
    _run_sync(_connection)
else:
    asyncio.run(_run_async())
