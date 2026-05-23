from datetime import datetime, timezone, timedelta
from sqlalchemy import or_, and_
from db.database import get_sync_db
from db.models import Account, Proxy
from config.settings import get_settings
from services.app_settings_service import (
    get_proxy_type_filter,
    get_proxy_prefer_mobile,
    get_account_proxy_test_mode,
)

settings = get_settings()


def main():
    db = next(get_sync_db())
    now = datetime.now(timezone.utc)
    cooldown_threshold = now - timedelta(seconds=settings.ACCOUNT_COOLDOWN_SECONDS)

    proxy_type_filter = get_proxy_type_filter(db)
    proxy_prefer_mobile = get_proxy_prefer_mobile(db)
    test_mode = get_account_proxy_test_mode(db)

    print("=== SETTINGS ===")
    for name in [
        "USE_PROXY",
        "PROXY_GEO_MATCHING",
        "MAX_ACTIONS_PER_ACCOUNT",
        "ACCOUNT_COOLDOWN_SECONDS",
        "PLAYWRIGHT_HEADLESS",
        "VALIDATE_BEFORE_TASK",
    ]:
        print(name, "=", getattr(settings, name, None))
    print("proxy_type_filter =", proxy_type_filter)
    print("proxy_prefer_mobile =", proxy_prefer_mobile)
    print("account_proxy_test_mode =", test_mode)

    print("\n=== ACCOUNTS ===")
    print("Total:", db.query(Account).count())
    for status in ["active", "error", "dead", "reauth_needed", "banned"]:
        print(status + ":", db.query(Account).filter(Account.status == status).count())

    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    print("\nWith auth material:", db.query(Account).filter(auth_material).count())
    print("With proxy:", db.query(Account).filter(Account.proxy.isnot(None), Account.proxy != "").count())
    print("Without proxy:", db.query(Account).filter(or_(Account.proxy.is_(None), Account.proxy == "")).count())
    print("Not in main cooldown:", db.query(Account).filter(or_(Account.last_main_comment_at.is_(None), Account.last_main_comment_at < cooldown_threshold)).count())
    print("Below main limit:", db.query(Account).filter(Account.today_main_comments < settings.MAX_ACTIONS_PER_ACCOUNT).count())

    print("\n=== PROXIES ===")
    active_proxy_q = db.query(Proxy).filter(Proxy.status.in_(["active", "slow", "unknown"]))
    print("Proxy table:", db.query(Proxy).count())
    print("Active/slow/unknown:", active_proxy_q.count())
    print("Residential:", active_proxy_q.filter(Proxy.is_mobile != "Y").count())
    print("Mobile:", active_proxy_q.filter(Proxy.is_mobile == "Y").count())
    print("Mobile with rotation_url:", active_proxy_q.filter(Proxy.is_mobile == "Y", Proxy.rotation_url.isnot(None)).count())

    print("\n=== MAIN COMMENT SQL CANDIDATES ===")
    q = db.query(Account).filter(
        or_(
            Account.status == "active",
            and_(Account.status == "error", Account.error_count_recent < settings.ERROR_THRESHOLD),
        ),
        auth_material,
        Account.today_main_comments < settings.MAX_ACTIONS_PER_ACCOUNT,
        or_(Account.last_main_comment_at.is_(None), Account.last_main_comment_at < cooldown_threshold),
    )
    print("Before proxy filter:", q.count())

    if settings.USE_PROXY:
        q = q.filter(Account.proxy.isnot(None), Account.proxy != "")
    print("After USE_PROXY:", q.count())

    if proxy_type_filter:
        q = q.outerjoin(Proxy, Account.proxy == Proxy.proxy)
        if str(proxy_type_filter).lower() == "mobile":
            q = q.filter(Proxy.is_mobile == "Y")
        elif str(proxy_type_filter).lower() == "residential":
            q = q.filter(or_(Proxy.id.is_(None), Proxy.is_mobile != "Y"))
    print("After proxy_type_filter:", q.with_entities(Account.id).distinct().count())

    print("\n=== SAMPLE ===")
    for a in db.query(Account).order_by(Account.id.asc()).limit(50).all():
        print(
            f"id={a.id} status={a.status} errors={a.error_count_recent} "
            f"main_today={a.today_main_comments} proxy={bool(a.proxy)} "
            f"country={getattr(a, 'country_code', None)} "
            f"last_main={a.last_main_comment_at} last_error={str(getattr(a, 'last_error', '') or '')[:120]}"
        )

    db.close()


if __name__ == "__main__":
    main()
