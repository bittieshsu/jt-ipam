"""CLI：bootstrap 第一個 admin 帳號。

用法：
    python -m app.cli.bootstrap create-admin --username admin --email admin@example.com
    # 互動式輸入密碼；或用 --password-stdin
    python -m app.cli.bootstrap reset-mfa --username admin
    # 唯一的管理員遺失驗證器又沒有復原碼時：清掉 TOTP 與復原碼、撤銷所有登入（留稽核）

OWASP A07：
- 密碼從 TTY / stdin 讀，不接受 --password 命令列參數（會留在 shell history）
- 密碼最少 12 字
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models.user import User


async def _create_admin(username: str, email: str, password: str, force: bool) -> int:
    async with SessionLocal() as session:
        # 分開查 username / email（各自唯一），避免 OR 同時命中兩筆不同帳號時
        # scalar_one_or_none 直接炸 MultipleResultsFound。
        by_name = (
            await session.execute(select(User).where(User.username == username).limit(1))
        ).scalars().first()
        by_email = (
            await session.execute(select(User).where(User.email == email).limit(1))
        ).scalars().first()
        existing = by_name or by_email
        if existing is not None:
            if not force:
                print(
                    f"[error] user already exists: id={existing.id} username={existing.username} "
                    f"email={existing.email}",
                    file=sys.stderr,
                )
                print("        use --force-update to reset password and grant admin", file=sys.stderr)
                return 1
            # 衝突：要設定的 username 與 email 分屬兩個不同帳號 → 無法安全合併
            if by_name is not None and by_email is not None and by_name.id != by_email.id:
                print(
                    f"[error] cannot update: username '{username}' belongs to id={by_name.id} but "
                    f"email '{email}' belongs to a different account id={by_email.id} "
                    f"(username={by_email.username}). Use a matching username/email pair.",
                    file=sys.stderr,
                )
                return 1
            target = by_name or by_email
            target.password_hash = hash_password(password)
            target.is_admin = True
            target.is_active = True
            target.failed_login_count = 0
            target.locked_until = None
            # force-update 視為「把此帳號設定成指定的 username/email」→ 同步更新識別資訊，
            # 否則使用者明明帶了新 email，卻看到舊 email 沒變。
            target.username = username
            target.email = email
            await session.commit()
            print(f"[ok] updated existing user → admin: {target.username} ({target.email})")
            return 0

        user = User(
            username=username,
            email=email,
            display_name=username,
            password_hash=hash_password(password),
            auth_provider="local",
            is_active=True,
            is_admin=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        print(f"[ok] created admin: id={user.id} username={user.username} email={user.email}")
        return 0


async def _reset_mfa(username: str) -> int:
    """網頁上的「重設雙因素驗證」需要另一位管理員；只有一位時只能從主機 shell 救回來。"""
    from app.core.audit import append_audit
    from app.services import sessions as sessions_service
    from app.services.mfa import clear_recovery_codes

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalars().first()
        if user is None:
            print(f"[error] no such user: {username}", file=sys.stderr)
            return 1
        had = user.totp_secret_enc is not None
        user.totp_secret_enc = None
        user.totp_nonce = None
        user.totp_last_step = None
        await clear_recovery_codes(session, user.id)
        n = await sessions_service.revoke_all(session, user, reason="mfa_reset", cutoff=True)
        await append_audit(
            session, actor_user_id=None, actor_ip=None, actor_user_agent="jt-ipam CLI",
            object_type="user", object_id=str(user.id), action="mfa_reset",
            diff={"username": user.username, "had_totp": had, "sessions_revoked": n, "via": "cli"},
            request_id=None,
        )
        await session.commit()
        print(f"[ok] two-factor authentication reset for {user.username} "
              f"(had TOTP: {had}; signed out of {n} session(s))")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jt-ipam-bootstrap")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_admin = sub.add_parser("create-admin", help="Create the first administrator user")
    p_admin.add_argument("--username", required=True)
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from stdin (one line). Otherwise prompt interactively.",
    )
    p_admin.add_argument(
        "--force-update",
        action="store_true",
        help="If user exists, reset password and grant admin",
    )

    p_mfa = sub.add_parser("reset-mfa", help="Clear a user's two-factor authentication and sign them out")
    p_mfa.add_argument("--username", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "reset-mfa":
        return asyncio.run(_reset_mfa(args.username))

    if args.cmd == "create-admin":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            pw1 = getpass.getpass("Password: ")
            pw2 = getpass.getpass("Confirm:  ")
            if pw1 != pw2:
                print("[error] passwords do not match", file=sys.stderr)
                return 1
            password = pw1
        if len(password) < 12:
            print("[error] password must be ≥ 12 characters", file=sys.stderr)
            return 1
        return asyncio.run(
            _create_admin(args.username, args.email, password, args.force_update)
        )

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
