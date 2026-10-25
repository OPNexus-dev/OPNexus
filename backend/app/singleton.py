"""Settings tables with one fixed row (id 1): read it, or create it on first use.

On a streaming replica (HA standby) the database is read-only: the INSERT would fail, and a login or a settings read
must not depend on it. Migration 0033 creates every row up front, so this is only the fallback for a row that is
missing anyway (a table cleaned by hand): the caller gets a default object that is not stored."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect

SINGLETON_ID = 1


def with_defaults(model, **values):
    """The row the database would hold after an INSERT of ``values``: column defaults (plain values and callables)
    applied in Python, because SQLAlchemy only applies them while flushing."""
    for column in inspect(model).columns:
        default = column.default
        if column.key in values or default is None or not (default.is_scalar or default.is_callable):
            continue
        values[column.key] = default.arg(None) if default.is_callable else default.arg
    return model(**values)


async def get_or_create(db: AsyncSession, model, **initial):
    row = await db.get(model, SINGLETON_ID)
    if row is not None:
        return row
    if await db.scalar(text("SELECT pg_is_in_recovery()")):
        return with_defaults(model, id=SINGLETON_ID, **initial)
    row = model(id=SINGLETON_ID, **initial)
    db.add(row)
    await db.flush()
    return row
