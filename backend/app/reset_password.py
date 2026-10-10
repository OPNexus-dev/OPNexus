"""Recovery: give an account a new random password and print it once.

For a fresh install whose generated first-start password scrolled out of the
log, and for an admin who forgot theirs. Needs shell access to the server,
which is the trust boundary for this tool anyway (same as app.reset_2fa).
All sessions of the account are ended. Normally called through the host
CLI:

    ./opnexus password [username]

or directly:

    docker compose exec backend python -m app.reset_password [username]

Without a username the configured ADMIN_USERNAME is used; if that account does
not exist and there is exactly one admin, that one.
"""
import asyncio
import secrets
import sys

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.user import AdminUser
from app.services import auth_service


async def main(username: str | None) -> int:
    async with SessionLocal() as db:
        if username is None:
            username = settings.admin_username
            user = (await db.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()
            if user is None:
                admins = (await db.execute(select(AdminUser).where(AdminUser.role == "admin"))).scalars().all()
                if len(admins) != 1:
                    names = ", ".join(sorted(a.username for a in admins)) or "-"
                    print(f"No account named '{username}'. Pass the username (admins: {names}).", file=sys.stderr)
                    return 1
                user = admins[0]
        else:
            user = (await db.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()
            if user is None:
                print(f"No such user: {username}", file=sys.stderr)
                return 1

        password = secrets.token_urlsafe(18)
        user.password_hash = await auth_service.hash_password_async(password)
        await auth_service.revoke_sessions(db, user.id, commit=False)
        await db.commit()
        name = user.username
        has_totp = user.totp_enabled

    print(f"Username: {name}")
    print(f"Password: {password}")
    if has_totp:
        print("Two-factor sign-in is still on (lost the code? python -m app.reset_2fa).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print("usage: python -m app.reset_password [username]", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) == 2 else None)))
