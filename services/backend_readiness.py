"""Read-only route diagnostics. No credentials, network probes or pool mutations."""
from collections import Counter

from sqlalchemy import func

from db.models import Account, Proxy
from services.proxy_normalizer import ProxyRoute, ProxyRouteError, is_sticky_residential_template


def route_policy_summary(db):
    metadata = {}
    for proxy in db.query(Proxy).order_by(Proxy.id):
        try:
            metadata.setdefault(ProxyRoute.parse(proxy.proxy).url, proxy)
        except ProxyRouteError:
            continue
    counts, fixed = Counter(), Counter()
    groups = db.query(Account.proxy, func.count(Account.id)).filter(Account.status == "active").group_by(Account.proxy)
    for raw, count in groups:
        counts["active_accounts"] += count
        if not raw or not raw.strip():
            counts["missing_routes"] += count
            continue
        try:
            route = ProxyRoute.parse(raw)
        except ProxyRouteError:
            counts["invalid_routes"] += count
            continue
        proxy = metadata.get(route.url)
        if proxy is not None and proxy.status not in {"active", "slow", "unknown"}:
            counts["inactive_routes"] += count
            continue
        if proxy is not None and str(proxy.is_mobile or "").strip().upper() == "Y":
            if not str(proxy.rotation_url or "").strip():
                counts["missing_rotation_accounts"] += count
            else:
                counts["mobile_accounts"] += count
        elif is_sticky_residential_template(raw):
            counts["sticky_accounts"] += count
        else:
            fixed[route.url] += count
    counts["fixed_residential_routes"] = len(fixed)
    counts["shared_fixed_routes"] = sum(value > 1 for value in fixed.values())
    counts["accounts_on_shared_fixed_routes"] = sum(value for value in fixed.values() if value > 1)
    counts["excess_fixed_assignments"] = sum(max(0, value - 1) for value in fixed.values())
    fields = ("active_accounts", "missing_routes", "invalid_routes", "inactive_routes", "mobile_accounts",
              "sticky_accounts", "missing_rotation_accounts", "fixed_residential_routes", "shared_fixed_routes",
              "accounts_on_shared_fixed_routes", "excess_fixed_assignments")
    return {field: int(counts[field]) for field in fields}
