"""
Сервис для автоматического распределения прокси по аккаунтам с учётом географии.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from db.models import Account, Proxy
from typing import Any, Dict, Optional
from config.settings import get_settings
from services.proxy_filter_utils import apply_proxy_type_filter
from services.proxy_health_service import get_bad_proxy_set
from services.account_service import (
    ACCOUNT_LANE_RETIRE,
    MONITOR_SCOPE,
    SESSION_BUCKET_DEAD,
    _calculate_scope_quality_score,
    _has_auth_material,
    _read_account_lifecycle,
    _read_account_session_quality,
    _read_account_spend_lane,
    _read_monitor_state,
)

settings = get_settings()
logger = logging.getLogger("services.proxy_assignment")

def invalidate_proxy_cache():
    """Compatibility hook. Assignments read current database rows under a lock."""
    return None


def _is_mobile_proxy(proxy_obj: Proxy) -> bool:
    return str(proxy_obj.is_mobile or "").strip().upper() == "Y"


def _normalize_proxy_type_filter(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().lower() or None
    if normalized in {"none", "all", "any"}:
        return None
    if normalized not in {None, "mobile", "residential"}:
        logger.warning("Invalid proxy_type_filter=%s; ignoring", value)
        return None
    return normalized


def _get_active_proxies(
    db: Session,
    *,
    force_proxy_type_filter: Optional[str] = None,
) -> tuple[list[Proxy], Optional[str]]:
    normalized_force_filter = _normalize_proxy_type_filter(force_proxy_type_filter)
    query = db.query(Proxy).filter(
        Proxy.status.in_(["active", "slow", "unknown"]),
        Proxy.dedicated_user_id.is_(None),
    )
    if normalized_force_filter == "mobile":
        query = query.filter(func.upper(func.trim(Proxy.is_mobile)) == "Y")
    elif normalized_force_filter == "residential":
        query = query.filter(or_(Proxy.is_mobile.is_(None), func.upper(func.trim(Proxy.is_mobile)) != "Y"))
    else:
        query = apply_proxy_type_filter(query, db)
    return query.order_by(Proxy.created_at.asc(), Proxy.id.asc()).all(), normalized_force_filter


def _rebalance_account_fit_score(account: Account, proxy_obj: Proxy, *, use_geo_matching: bool) -> tuple[int, int]:
    geo_score = 0
    if use_geo_matching:
        account_country = (account.country_code or "").strip().upper()
        proxy_country = (proxy_obj.country_code or "").strip().upper()
        if account_country and proxy_country:
            geo_score = 10 if account_country == proxy_country else -2
        elif not account_country and proxy_country:
            geo_score = 1
    return geo_score, -int(proxy_obj.id or 0)


def _is_rebalance_eligible_account(
    account: Account,
    *,
    now,
) -> tuple[bool, str, int]:
    if not _has_auth_material(account):
        return False, "no_auth_material", 0

    # Do not pass the temporary bad-proxy set here: accounts sitting on a bad
    # proxy should still be eligible so the rebalance can move them away.
    state = _read_monitor_state(account, now, bad_proxy_set=set())
    lifecycle = _read_account_lifecycle(account, now)
    session_quality = _read_account_session_quality(account, MONITOR_SCOPE, now)
    spend_lane = _read_account_spend_lane(account, MONITOR_SCOPE, now)

    if lifecycle["hard_expired"]:
        return False, "hard_expired", 0
    if state["active_cooldown"] or state["bucket"] in {"cooldown", "dead_session"}:
        return False, f"state_{state['bucket']}", 0
    if session_quality["bucket"] == SESSION_BUCKET_DEAD:
        return False, "session_dead", 0
    if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
        return False, "lane_retire", 0

    score = _calculate_scope_quality_score(account, state, MONITOR_SCOPE, now)
    return True, state["bucket"], score


def _lock_assignment_pool(db: Session) -> list[Proxy]:
    """Serialize assignments before inspecting owners, including SQLite writers."""
    from sqlalchemy import update

    with db.no_autoflush:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            db.execute(update(Proxy).values(updated_at=Proxy.updated_at),
                       execution_options={"synchronize_session": False})
        elif dialect == "postgresql":
            # The pool may be empty when the first proxies are imported.
            db.query(func.pg_advisory_xact_lock(0x54454C45)).scalar()
        db.query(Proxy).order_by(Proxy.id).with_for_update().all()
    db.flush()
    return db.query(Proxy).order_by(Proxy.id).with_for_update().populate_existing().all()


def _resolved_proxy(raw, account, *, country=None):
    from services.proxy_normalizer import ProxyRoute, resolve_account_sticky_proxy
    return ProxyRoute.parse(resolve_account_sticky_proxy(raw, account, default_country=country)).url


def _reusable_proxy(proxy: Proxy) -> bool:
    from services.proxy_normalizer import is_sticky_residential_template
    if _is_mobile_proxy(proxy):
        return bool(str(proxy.rotation_url or "").strip())
    return is_sticky_residential_template(proxy.proxy)


def _has_active_assignment(account: Account) -> bool:
    expires = account.assignment_lease_until
    if expires is None:
        return False
    return expires.replace(tzinfo=expires.tzinfo or timezone.utc) > datetime.now(timezone.utc)


def bind_account_proxy(db: Session, account: Account, raw: str | None, *, proxy: Proxy | None = None) -> str | None:
    """Explicit assignment. A fixed residential route can have only one owner."""
    from services.proxy_normalizer import ProxyRouteError, find_account_proxy_record

    _lock_assignment_pool(db)
    db.refresh(account, attribute_names=["proxy", "assignment_lease_until"], with_for_update=True)
    if _has_active_assignment(account):
        raise ProxyRouteError("account_route_in_use")
    if not raw or not raw.strip():
        account.proxy = None
        return None
    if proxy is None:
        proxy = db.query(Proxy).filter(Proxy.proxy == raw).first()
    if proxy is None:
        from services.proxy_normalizer import normalize_proxy_string, is_sticky_residential_template
        resolved_candidate = _resolved_proxy(raw, account)
        proxy = next((candidate for candidate in db.query(Proxy)
                      if not is_sticky_residential_template(candidate.proxy)
                      and normalize_proxy_string(candidate.proxy) == resolved_candidate), None)
        if proxy is None:
            proxy = Proxy(proxy=raw if is_sticky_residential_template(raw) else resolved_candidate,
                          is_mobile="N", status="active")
            db.add(proxy)
            db.flush()
    if proxy is not None and proxy.dedicated_user_id is not None:
        raise ProxyRouteError("dedicated_proxy_cannot_be_assigned")
    if proxy is not None and _is_mobile_proxy(proxy) and not proxy.rotation_url:
        raise ProxyRouteError("mobile_rotation_url_missing")
    resolved = _resolved_proxy(raw, account, country=proxy.country_code if proxy else None)
    mobile = proxy is not None and _is_mobile_proxy(proxy)
    if not mobile:
        owners = db.query(Account).filter(Account.id != account.id, Account.proxy.isnot(None), Account.proxy != "")
        for other in owners.populate_existing():
            try:
                other_source = find_account_proxy_record(db, other)
                if other_source is not None and _is_mobile_proxy(other_source):
                    continue
                if _resolved_proxy(other.proxy, other) == resolved:
                    raise ProxyRouteError("residential_route_already_assigned")
            except ProxyRouteError as exc:
                if exc.reason == "residential_route_already_assigned":
                    raise
    account.proxy = resolved
    return resolved


def _free_proxy_source_count(proxies, accounts, owners) -> int:
    from services.proxy_normalizer import ProxyRouteError

    free = 0
    for proxy in proxies:
        if _is_mobile_proxy(proxy) and not proxy.rotation_url:
            continue
        try:
            if _reusable_proxy(proxy):
                _resolved_proxy(proxy.proxy, accounts[0] if accounts else None, country=proxy.country_code)
                free += 1
                continue
            if "{" in proxy.proxy:
                occupied = any(account.id in owners.get(
                    _resolved_proxy(proxy.proxy, account, country=proxy.country_code), ()
                ) for account in accounts)
            else:
                occupied = bool(owners.get(_resolved_proxy(proxy.proxy, None)))
        except ProxyRouteError:
            continue
        free += not occupied
    return free


def _assign_stable_routes(db, accounts, proxies, *, use_geo_matching, replace_existing=False, repair_existing=False):
    from services.proxy_normalizer import ProxyRouteError

    all_accounts = db.query(Account).order_by(Account.id).with_for_update().populate_existing().all()
    owners = defaultdict(set)
    for account in all_accounts:
        if account.proxy:
            try:
                owners[_resolved_proxy(account.proxy, account)].add(account.id)
            except ProxyRouteError:
                pass
    free_before = _free_proxy_source_count(proxies, all_accounts, owners)
    changed = geo_matched = geo_fallback = skipped_busy = 0
    for account in accounts:
        if _has_active_assignment(account):
            skipped_busy += 1
            continue
        previous = account.proxy
        if previous and not replace_existing:
            if not repair_existing:
                continue
            from services.proxy_normalizer import find_account_proxy_record
            source = find_account_proxy_record(db, account)
            try:
                current_route = _resolved_proxy(previous, account)
            except ProxyRouteError:
                current_route = None
            if source is not None and _is_mobile_proxy(source):
                continue
            if current_route and min(owners[current_route] or {account.id}) == account.id:
                continue
        selected = False
        ranked = sorted(proxies, key=lambda proxy: (
            _is_mobile_proxy(proxy),
            -_rebalance_account_fit_score(account, proxy, use_geo_matching=use_geo_matching)[0],
            proxy.id,
        ))
        for proxy in ranked:
            if _is_mobile_proxy(proxy) and not proxy.rotation_url:
                continue
            if use_geo_matching and account.country_code and not settings.PROXY_GEO_FALLBACK:
                from services.proxy_normalizer import _COUNTRY_PLACEHOLDER_RE
                dynamic_country = bool(_COUNTRY_PLACEHOLDER_RE.search(proxy.proxy))
                if not dynamic_country and account.country_code.upper() != (proxy.country_code or "").upper():
                    continue
            try:
                resolved = _resolved_proxy(proxy.proxy, account, country=proxy.country_code)
            except ProxyRouteError:
                continue
            if not _is_mobile_proxy(proxy) and owners[resolved] - {account.id}:
                continue
            if previous:
                try:
                    owners[_resolved_proxy(previous, account)].discard(account.id)
                except ProxyRouteError:
                    pass
            owners[resolved].add(account.id)
            account.proxy = resolved
            changed += previous != resolved
            matched = bool(account.country_code and account.country_code == proxy.country_code)
            geo_matched += bool(use_geo_matching and matched)
            geo_fallback += bool(use_geo_matching and account.country_code and not matched)
            selected = True
            break
        if repair_existing and previous and not selected:
            try:
                owners[_resolved_proxy(previous, account)].discard(account.id)
            except ProxyRouteError:
                pass
            account.proxy = None
            changed += 1
    return changed, geo_matched, geo_fallback, skipped_busy, free_before, _free_proxy_source_count(proxies, all_accounts, owners)


def rebalance_working_account_proxies(
    db: Session, *, mobile_weight: int | None = None, force_proxy_type_filter: Optional[str] = None,
    use_geo_matching: bool | None = None, dry_run: bool = False,
) -> Dict[str, Any]:
    """Fill missing routes while preserving every existing account binding."""
    from datetime import datetime, timezone
    from services.proxy_normalizer import find_account_proxy_record

    _lock_assignment_pool(db)
    proxies, normalized = _get_active_proxies(db, force_proxy_type_filter=force_proxy_type_filter)
    bad = get_bad_proxy_set(proxy.proxy for proxy in proxies)
    usable = [proxy for proxy in proxies if proxy.proxy not in bad or _is_mobile_proxy(proxy)]
    now = datetime.now(timezone.utc)
    accounts, skipped = [], defaultdict(int)
    for account in db.query(Account).filter(Account.status.in_(("active", "error"))).order_by(Account.id):
        eligible, reason, _ = _is_rebalance_eligible_account(account, now=now)
        if eligible:
            accounts.append(account)
        else:
            skipped[reason] += 1

    pinned = sum(bool(account.proxy) for account in accounts)
    missing = [account for account in accounts if not account.proxy]
    # A savepoint lets dry-run use the same allocation algorithm without changing bindings.
    nested = db.begin_nested() if dry_run else None
    changed, _, _, busy, _, _ = _assign_stable_routes(
        db, accounts, usable,
        use_geo_matching=settings.PROXY_GEO_MATCHING if use_geo_matching is None else use_geo_matching,
        repair_existing=True,
    )
    counts = {proxy.id: 0 for proxy in proxies}
    for account in accounts:
        source = find_account_proxy_record(db, account)
        if source and source.id in counts:
            counts[source.id] += 1
    distinct_routes = set()
    for account in accounts:
        if account.proxy:
            try:
                distinct_routes.add(_resolved_proxy(account.proxy, account))
            except Exception:
                pass
    unassigned = sum(not account.proxy for account in accounts)
    if nested:
        nested.rollback()
    else:
        db.commit()
    mobile_count = sum(_is_mobile_proxy(proxy) and bool(proxy.rotation_url) for proxy in usable)
    residential_count = len(usable) - sum(_is_mobile_proxy(proxy) for proxy in usable)
    return {
        "eligible_accounts": len(accounts), "changed": changed, "active_proxies": len(usable),
        "mobile_proxies": mobile_count, "residential_proxies": residential_count,
        "targets_by_proxy_id": counts, "actual_by_proxy_id": counts,
        "pinned_sticky_accounts": pinned, "pinned_accounts": pinned,
        "distinct_account_routes": len(distinct_routes), "unassigned_accounts": unassigned,
        "skipped_busy_accounts": busy, "skipped": dict(skipped), "geo_filled": 0,
        "ignored_temporarily_bad_proxies": sum(proxy.proxy in bad and not _is_mobile_proxy(proxy) for proxy in proxies),
        "kept_temporarily_bad_mobile_proxies": sum(proxy.proxy in bad and _is_mobile_proxy(proxy) for proxy in proxies),
        "mobile_weight": 1, "force_proxy_type_filter": normalized, "dry_run": dry_run,
    }


def assign_proxies_to_accounts(
    db: Session, *, only_accounts_without_proxy: bool = True, reuse_existing: bool = False,
    use_geo_matching: bool | None = None, allow_reuse_within_batch: bool = False,
    force_proxy_type_filter: Optional[str] = None, test_shared_residential: bool = False,
    account_ids: list[int] | None = None, commit: bool = True,
) -> Dict[str, Any]:
    """Assign unique residential routes first, then mobile routes with rotation."""
    _lock_assignment_pool(db)
    proxies, normalized = _get_active_proxies(db, force_proxy_type_filter=force_proxy_type_filter)
    bad = get_bad_proxy_set(proxy.proxy for proxy in proxies)
    proxies = [proxy for proxy in proxies if proxy.proxy not in bad]
    query = db.query(Account).filter(Account.status != "dead")
    if account_ids is not None:
        query = query.filter(Account.id.in_(account_ids))
    if only_accounts_without_proxy:
        query = query.filter(or_(Account.proxy.is_(None), Account.proxy == ""))
    accounts = query.order_by(Account.id).all()
    changed, matched, fallback, busy, free_before, free_after = _assign_stable_routes(
        db, accounts, proxies, use_geo_matching=settings.PROXY_GEO_MATCHING if use_geo_matching is None else use_geo_matching,
        replace_existing=not only_accounts_without_proxy,
        repair_existing=not only_accounts_without_proxy,
    )
    assigned = sum(bool(account.proxy) for account in accounts)
    if commit:
        db.commit()
    else:
        db.flush()
    invalidate_proxy_cache()
    return {
        "total_accounts_without_proxy": len(accounts), "assigned": changed,
        "not_enough_proxies": assigned < len(accounts), "free_proxies_before": free_before,
        "free_proxies_after": free_after, "geo_matched": matched,
        "geo_fallback": fallback, "test_shared_residential": False, "allow_reuse_within_batch": False,
        "force_proxy_type_filter": normalized, "skipped_busy_accounts": busy,
    }


def clear_proxy_bindings(db: Session, proxy_ids: set[int] | None, *, skip_busy: bool = False) -> int:
    from services.proxy_normalizer import ProxyRouteError, find_account_proxy_record
    _lock_assignment_pool(db)
    cleared = 0
    accounts = db.query(Account).filter(Account.proxy.isnot(None), Account.proxy != "").with_for_update()
    for account in accounts.populate_existing():
        source = find_account_proxy_record(db, account) if proxy_ids is not None else None
        if proxy_ids is None or source is not None and source.id in proxy_ids:
            if _has_active_assignment(account):
                if skip_busy:
                    continue
                raise ProxyRouteError("account_route_in_use")
            account.proxy = None
            cleared += 1
    db.flush()
    return cleared
