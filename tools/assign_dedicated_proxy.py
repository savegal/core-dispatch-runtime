"""Assign and verify one strict proxy route for a user's shared-pool orders."""
from __future__ import annotations

import argparse
import asyncio

from db.database import SessionLocal
from db.models import Account, Proxy, User
from services.log_sanitizer import sanitize_url
from services.proxy_service import check_proxy_health_async, normalize_proxy_url


def _normalize_cli_proxy(value: str) -> str:
    raw = str(value or "").strip()
    # Common provider export: host:port:login:password.
    if "://" not in raw and "@" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
            raw = f"{username}:{password}@{host}:{port}"
    return normalize_proxy_url(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--proxy", required=True, help="scheme://login:password@host:port")
    parser.add_argument("--country-code", default="US")
    parser.add_argument("--mobile", action="store_true")
    parser.add_argument("--rotation-url")
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()

    proxy_value = _normalize_cli_proxy(args.proxy)
    if not args.skip_check:
        healthy, status, error, response_ms = asyncio.run(
            check_proxy_health_async(proxy_value, timeout=20.0)
        )
        if not healthy:
            raise SystemExit(f"Proxy check failed: status={status}, error={error}")
        print(f"Proxy check: {status}, response={response_ms}ms")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == args.telegram_id).first()
        if user is None:
            raise SystemExit(f"Telegram user {args.telegram_id} not found")

        target = db.query(Proxy).filter(Proxy.proxy == proxy_value).first()
        if target is not None and target.dedicated_user_id not in (None, user.id):
            raise SystemExit("Proxy is already dedicated to another user")

        current = db.query(Proxy).filter(Proxy.dedicated_user_id == user.id).first()
        if target is None:
            if current is not None:
                target = current
                target.proxy = proxy_value
            else:
                target = Proxy(proxy=proxy_value)
                db.add(target)
        elif current is not None and current.id != target.id:
            current.dedicated_user_id = None
            current.status = "dead"

        target.country_code = str(args.country_code or "US").strip().upper()[:2]
        target.is_mobile = "Y" if args.mobile else "N"
        target.rotation_url = str(args.rotation_url or "").strip() or None
        target.status = "active"
        target.fail_count = 0
        target.dedicated_user_id = user.id
        db.flush()

        # A dedicated proxy must never remain persistently attached to a shared
        # account, otherwise monitor/legacy paths could use it for somebody else.
        cleared = (
            db.query(Account)
            .filter(Account.proxy == proxy_value)
            .update({Account.proxy: None}, synchronize_session=False)
        )
        db.commit()
        print(
            "Dedicated route saved: "
            f"telegram_id={args.telegram_id}, user_id={user.id}, proxy_id={target.id}, "
            f"proxy={sanitize_url(proxy_value, max_length=100)}, cleared_accounts={cleared}"
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
