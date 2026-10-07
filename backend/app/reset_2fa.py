"""Emergency recovery: switch off two-factor sign-in for one account.

For the case where the only admin lost both their authenticator and their
recovery codes (no other admin can use the UI reset). Needs shell access to the
server, which is the trust boundary for this tool anyway:

    docker compose exec backend python -m app.reset_2fa <username>
"""
import asyncio
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import AdminUser
from app.services import totp_service


async def main(username: str) -> int:
    async with SessionLocal() as db:
        user = (await db.execute(select(AdminUser).where(AdminUser.username == username))).scalar_one_or_none()
        if user is None:
            print(f"No such user: {username}")
            return 1
        totp_service.delete_secrets(user.id)
        user.totp_enabled = False
        user.totp_recovery_hashes = None
        user.totp_last_step = 0
        await db.commit()
    print(f"Two-factor sign-in disabled for {username}.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m app.reset_2fa <username>")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
