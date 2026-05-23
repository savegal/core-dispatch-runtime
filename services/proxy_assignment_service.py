"""
Сервис для автоматического распределения прокси по аккаунтам с учётом географии.
"""
import logging
import time
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from db.models import Account, Proxy
from typing import Any, Dict, Optional
from config.settings import get_settings
from services.proxy_filter_utils import apply_proxy_type_filter
from services.proxy_health_service import get_bad_proxy_set
from services.account_service import (
    ACCOUNT_LANE_RETIRE,
    ERROR_THRESHOLD,
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

# КЭШ для активных прокси (TTL 60 секунд)
# ИСПРАВЛЕНО: Добавлена функция инвалидации кэша
_active_proxies_cache: Dict[str, tuple[list, float]] = {}
PROXY_CACHE_TTL = 60.0  # секунды


def invalidate_proxy_cache():
    """
    ИСПРАВЛЕНО: Инвалидирует кэш активных прокси.
    Вызывается при изменении статуса прокси или при добавлении/удалении прокси.
    """
    global _active_proxies_cache
    _active_proxies_cache.clear()
    logger.debug("Proxy cache invalidated")


def _is_mobile_proxy(proxy_obj: Proxy) -> bool:
    return str(proxy_obj.is_mobile or "").upper() == "Y"


def _normalize_proxy_type_filter(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().lower() or None
    if normalized in {"none", "all", "any"}:
        return None
    if normalized not in {None, "mobile", "residential"}:
        logger.warning("Invalid proxy_type_filter=%s; ignoring", value)
        return None
    return normalized


def _safe_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _get_active_proxies(
    db: Session,
    *,
    force_proxy_type_filter: Optional[str] = None,
) -> tuple[list[Proxy], Optional[str]]:
    normalized_force_filter = _normalize_proxy_type_filter(force_proxy_type_filter)
    query = db.query(Proxy).filter(Proxy.status.in_(["active", "slow", "unknown"]))
    if normalized_force_filter == "mobile":
        query = query.filter(Proxy.is_mobile == "Y")
    elif normalized_force_filter == "residential":
        query = query.filter(Proxy.is_mobile != "Y")
    else:
        query = apply_proxy_type_filter(query, db)
    return query.order_by(Proxy.created_at.asc(), Proxy.id.asc()).all(), normalized_force_filter


def _proxy_weight(proxy_obj: Proxy, mobile_weight: int) -> int:
    return mobile_weight if _is_mobile_proxy(proxy_obj) else 1


def _calculate_proxy_targets(
    proxies: list[Proxy],
    total_accounts: int,
    *,
    mobile_weight: int,
) -> dict[str, int]:
    if total_accounts <= 0 or not proxies:
        return {}

    weights = {proxy.proxy: _proxy_weight(proxy, mobile_weight) for proxy in proxies}
    total_weight = max(1, sum(weights.values()))
    exact_targets: list[tuple[Proxy, float, int, float]] = []
    targets: dict[str, int] = {}

    for proxy in proxies:
        exact = total_accounts * weights[proxy.proxy] / total_weight
        base = int(exact)
        targets[proxy.proxy] = base
        exact_targets.append((proxy, exact, base, exact - base))

    remaining = total_accounts - sum(targets.values())
    if remaining > 0:
        # Largest-remainder allocation keeps the total exact and deterministic.
        for proxy, _exact, _base, _fraction in sorted(
            exact_targets,
            key=lambda item: (-item[3], -_proxy_weight(item[0], mobile_weight), item[0].id),
        )[:remaining]:
            targets[proxy.proxy] += 1

    return targets


def _target_summary_by_proxy_id(proxies: list[Proxy], counts_by_proxy: dict[str, int]) -> dict[int, int]:
    return {int(proxy.id): int(counts_by_proxy.get(proxy.proxy, 0)) for proxy in proxies}


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


def rebalance_working_account_proxies(
    db: Session,
    *,
    mobile_weight: int | None = None,
    force_proxy_type_filter: Optional[str] = None,
    use_geo_matching: bool | None = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Weighted rebalance for accounts that are currently usable by the monitor.

    Mobile proxies intentionally get a larger target share. With the default
    weight of 5, one mobile proxy plus five residential proxies produces a
    roughly 50/10/10/10/10/10 split across the working account pool.
    """
    from datetime import datetime, timezone

    if use_geo_matching is None:
        use_geo_matching = settings.PROXY_GEO_MATCHING

    effective_mobile_weight = _safe_positive_int(
        mobile_weight if mobile_weight is not None else getattr(settings, "PROXY_REBALANCE_MOBILE_WEIGHT", 5),
        default=5,
    )

    active_proxies, normalized_force_filter = _get_active_proxies(
        db,
        force_proxy_type_filter=force_proxy_type_filter,
    )
    if not active_proxies:
        logger.warning("[PROXY_REBALANCE] No active proxies available")
        return {
            "active_proxies": 0,
            "eligible_accounts": 0,
            "changed": 0,
            "dry_run": bool(dry_run),
            "mobile_weight": effective_mobile_weight,
            "force_proxy_type_filter": normalized_force_filter,
        }

    temporarily_bad = get_bad_proxy_set(proxy.proxy for proxy in active_proxies)
    rebalance_excluded_bad = {
        proxy.proxy
        for proxy in active_proxies
        if proxy.proxy in temporarily_bad and proxy.is_mobile != "Y"
    }
    mobile_bad_kept = len(temporarily_bad) - len(rebalance_excluded_bad)
    usable_proxies = [proxy for proxy in active_proxies if proxy.proxy not in rebalance_excluded_bad]
    fail_open_bad_proxies = False
    if temporarily_bad and not usable_proxies:
        usable_proxies = active_proxies
        fail_open_bad_proxies = True
        logger.warning(
            "[PROXY_REBALANCE] All active proxies are temporarily marked bad; fail-open keeps them in rotation"
        )
    elif mobile_bad_kept:
        logger.info(
            "[PROXY_REBALANCE] Keeping %s temporarily-bad mobile proxies in target calculation to avoid mass reassignment",
            mobile_bad_kept,
        )

    now = datetime.now(timezone.utc)
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    candidate_accounts = (
        db.query(Account)
        .filter(
            Account.status.in_(["active", "error"]),
            Account.error_count_recent < ERROR_THRESHOLD,
            auth_material,
        )
        .order_by(Account.id.asc())
        .all()
    )

    skipped: dict[str, int] = defaultdict(int)
    eligible_items: list[dict[str, Any]] = []
    for account in candidate_accounts:
        eligible, reason, score = _is_rebalance_eligible_account(account, now=now)
        if not eligible:
            skipped[reason] += 1
            continue
        eligible_items.append({"account": account, "score": int(score)})

    targets = _calculate_proxy_targets(
        usable_proxies,
        len(eligible_items),
        mobile_weight=effective_mobile_weight,
    )
    usable_proxy_map = {proxy.proxy: proxy for proxy in usable_proxies}
    assigned_by_proxy: dict[str, list[dict[str, Any]]] = {proxy.proxy: [] for proxy in usable_proxies}
    current_by_proxy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    movable_items: list[dict[str, Any]] = []

    for item in eligible_items:
        account = item["account"]
        current_proxy = (account.proxy or "").strip()
        if current_proxy in usable_proxy_map:
            current_by_proxy[current_proxy].append(item)
        else:
            movable_items.append(item)

    for proxy in usable_proxies:
        current_items = current_by_proxy.get(proxy.proxy, [])
        current_items.sort(key=lambda item: (-int(item["score"]), int(item["account"].id)))
        target_count = targets.get(proxy.proxy, 0)
        assigned_by_proxy[proxy.proxy].extend(current_items[:target_count])
        movable_items.extend(current_items[target_count:])

    needed_by_proxy = {
        proxy.proxy: max(0, targets.get(proxy.proxy, 0) - len(assigned_by_proxy[proxy.proxy]))
        for proxy in usable_proxies
    }
    slot_proxies: list[Proxy] = []
    while any(needed_by_proxy.values()):
        for proxy in usable_proxies:
            if needed_by_proxy.get(proxy.proxy, 0) > 0:
                slot_proxies.append(proxy)
                needed_by_proxy[proxy.proxy] -= 1

    movable_items.sort(key=lambda item: (-int(item["score"]), int(item["account"].id)))
    for item in movable_items:
        if not slot_proxies:
            break
        account = item["account"]
        best_index = max(
            range(len(slot_proxies)),
            key=lambda idx: _rebalance_account_fit_score(
                account,
                slot_proxies[idx],
                use_geo_matching=bool(use_geo_matching),
            ),
        )
        selected_proxy = slot_proxies.pop(best_index)
        assigned_by_proxy[selected_proxy.proxy].append(item)

    changed = 0
    geo_filled = 0
    actual_counts: dict[str, int] = {}
    for proxy in usable_proxies:
        items = assigned_by_proxy.get(proxy.proxy, [])
        actual_counts[proxy.proxy] = len(items)
        for item in items:
            account = item["account"]
            if (account.proxy or "").strip() != proxy.proxy:
                changed += 1
                if not dry_run:
                    account.proxy = proxy.proxy
                    if not account.country_code and proxy.country_code:
                        account.country_code = proxy.country_code
                        geo_filled += 1

    if not dry_run:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[PROXY_REBALANCE] Commit failed: %s", e, exc_info=True)
            raise

    mobile_count = sum(1 for proxy in usable_proxies if _is_mobile_proxy(proxy))
    residential_count = len(usable_proxies) - mobile_count
    result = {
        "active_proxies": len(usable_proxies),
        "ignored_temporarily_bad_proxies": 0 if fail_open_bad_proxies else len(rebalance_excluded_bad),
        "kept_temporarily_bad_mobile_proxies": 0 if fail_open_bad_proxies else mobile_bad_kept,
        "fail_open_bad_proxies": fail_open_bad_proxies,
        "mobile_proxies": mobile_count,
        "residential_proxies": residential_count,
        "eligible_accounts": len(eligible_items),
        "candidate_accounts": len(candidate_accounts),
        "changed": changed,
        "unchanged": max(0, len(eligible_items) - changed),
        "geo_filled": geo_filled,
        "dry_run": bool(dry_run),
        "mobile_weight": effective_mobile_weight,
        "force_proxy_type_filter": normalized_force_filter,
        "targets_by_proxy_id": _target_summary_by_proxy_id(usable_proxies, targets),
        "actual_by_proxy_id": _target_summary_by_proxy_id(usable_proxies, actual_counts),
        "skipped": dict(sorted(skipped.items())),
    }

    logger.info(
        "[PROXY_REBALANCE] working=%s changed=%s proxies=%s mobile=%s residential=%s targets=%s actual=%s",
        result["eligible_accounts"],
        result["changed"],
        result["active_proxies"],
        result["mobile_proxies"],
        result["residential_proxies"],
        result["targets_by_proxy_id"],
        result["actual_by_proxy_id"],
    )
    return result



def assign_proxies_to_accounts(
    db: Session,
    *,
    only_accounts_without_proxy: bool = True,
    reuse_existing: bool = False,
    use_geo_matching: bool | None = None,
    allow_reuse_within_batch: bool = False,
    force_proxy_type_filter: Optional[str] = None,
    test_shared_residential: bool = False,
) -> Dict[str, int | bool | str | None]:
    """Распределяет прокси из пула активных прокси по аккаунтам.

    Обычный режим:
    - residential proxy стараемся держать 1 proxy = 1 account;
    - mobile proxy с rotation_url можно переиспользовать.

    Тестовый режим (`test_shared_residential=True`):
    - residential proxy можно временно назначать нескольким аккаунтам;
    - режим нужен только для диагностики пула, не для продакшена.
    """
    if use_geo_matching is None:
        use_geo_matching = settings.PROXY_GEO_MATCHING

    normalized_force_filter = (force_proxy_type_filter or "").strip().lower() or None
    if normalized_force_filter in {"none", "all", "any"}:
        normalized_force_filter = None
    if normalized_force_filter not in {None, "mobile", "residential"}:
        logger.warning("Invalid force_proxy_type_filter=%s; ignoring", force_proxy_type_filter)
        normalized_force_filter = None

    if test_shared_residential:
        reuse_existing = True
        allow_reuse_within_batch = True
        normalized_force_filter = normalized_force_filter or "residential"
        logger.warning(
            "[PROXY_ASSIGN_TEST] test_shared_residential enabled: residential proxies can be reused by multiple accounts."
        )

    cache_key = f"active_proxies:force={normalized_force_filter or 'settings'}:test={int(test_shared_residential)}"
    current_time = time.time()

    active_proxies = None
    cached = _active_proxies_cache.get(cache_key)
    if cached:
        cached_proxies, cache_time = cached
        if current_time - cache_time < PROXY_CACHE_TTL:
            active_proxies = cached_proxies
        else:
            _active_proxies_cache.pop(cache_key, None)

    if active_proxies is None:
        query = db.query(Proxy).filter(Proxy.status.in_(["active", "slow", "unknown"]))
        if normalized_force_filter == "mobile":
            query = query.filter(Proxy.is_mobile == "Y")
        elif normalized_force_filter == "residential":
            query = query.filter(Proxy.is_mobile != "Y")
        else:
            query = apply_proxy_type_filter(query, db)
        active_proxies = query.order_by(Proxy.created_at.asc()).all()
        _active_proxies_cache[cache_key] = (active_proxies, current_time)

    if not active_proxies:
        logger.warning("No active proxies available for assignment")
        return {
            "total_accounts_without_proxy": 0,
            "assigned": 0,
            "not_enough_proxies": True,
            "free_proxies_before": 0,
            "free_proxies_after": 0,
            "geo_matched": 0,
            "geo_fallback": 0,
            "test_shared_residential": bool(test_shared_residential),
            "allow_reuse_within_batch": bool(allow_reuse_within_batch),
            "force_proxy_type_filter": normalized_force_filter,
        }

    used_proxy_strings = set()
    if not reuse_existing:
        proxy_results = db.query(Account.proxy).filter(
            and_(Account.proxy.isnot(None), Account.proxy != "")
        ).distinct().all()
        used_proxy_strings = {p[0] for p in proxy_results if p[0]}

    def _is_mobile_proxy(proxy_obj: Proxy) -> bool:
        return proxy_obj.is_mobile == "Y" and bool(proxy_obj.rotation_url)

    def _proxy_can_be_reused(proxy_obj: Proxy) -> bool:
        if allow_reuse_within_batch:
            return True
        if test_shared_residential and proxy_obj.is_mobile != "Y":
            return True
        return _is_mobile_proxy(proxy_obj)

    def _proxy_available(proxy_obj: Proxy) -> bool:
        if _proxy_can_be_reused(proxy_obj):
            return True
        if reuse_existing:
            return True
        return proxy_obj.proxy not in used_proxy_strings

    free_proxies = [p for p in active_proxies if _proxy_available(p)]
    free_proxies_before = len(free_proxies)

    query = db.query(Account).filter(Account.status != "dead")
    if only_accounts_without_proxy:
        query = query.filter(or_(Account.proxy.is_(None), Account.proxy == ""))
    accounts_needing_proxy = query.order_by(Account.id.asc()).all()
    total_accounts_without_proxy = len(accounts_needing_proxy)

    proxies_by_country: Dict[Optional[str], list] = {}
    proxies_without_country = []
    if use_geo_matching:
        for proxy in free_proxies:
            country = proxy.country_code
            if country:
                proxies_by_country.setdefault(country, []).append(proxy)
            else:
                proxies_without_country.append(proxy)
    else:
        proxies_without_country = list(free_proxies)

    assigned = 0
    geo_matched = 0
    geo_fallback = 0

    for account in accounts_needing_proxy:
        proxy = None
        matched_by_geo = False

        if use_geo_matching and account.country_code:
            for p in proxies_by_country.get(account.country_code, []):
                if _proxy_available(p):
                    proxy = p
                    matched_by_geo = True
                    break

        if not proxy:
            for p in proxies_without_country:
                if _proxy_available(p):
                    proxy = p
                    break

        if not proxy:
            for p in free_proxies:
                if _proxy_available(p):
                    proxy = p
                    break

        if not proxy:
            break

        account.proxy = proxy.proxy
        if not account.country_code and proxy.country_code:
            account.country_code = proxy.country_code
            logger.info(
                "[GEO] Auto-set country_code %s for account %s from assigned proxy",
                proxy.country_code,
                account.id,
            )

        if not _proxy_can_be_reused(proxy):
            used_proxy_strings.add(proxy.proxy)
        assigned += 1

        if matched_by_geo:
            geo_matched += 1
        elif use_geo_matching and account.country_code:
            geo_fallback += 1

        logger.info(
            "Assigned proxy %s to account %s (test_shared=%s, reusable=%s)",
            proxy.proxy[:30],
            account.id,
            test_shared_residential,
            _proxy_can_be_reused(proxy),
        )

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Error committing proxy assignment: %s", e, exc_info=True)
        raise

    if allow_reuse_within_batch or test_shared_residential:
        free_proxies_after = free_proxies_before
    else:
        free_proxies_after = max(0, free_proxies_before - assigned)
    not_enough_proxies = assigned < total_accounts_without_proxy

    logger.info(
        "Proxy assignment completed: %s/%s accounts assigned, geo-matched=%s, geo-fallback=%s, free=%s->%s, test_shared=%s",
        assigned,
        total_accounts_without_proxy,
        geo_matched,
        geo_fallback,
        free_proxies_before,
        free_proxies_after,
        test_shared_residential,
    )

    return {
        "total_accounts_without_proxy": total_accounts_without_proxy,
        "assigned": assigned,
        "not_enough_proxies": not_enough_proxies,
        "free_proxies_before": free_proxies_before,
        "free_proxies_after": free_proxies_after,
        "geo_matched": geo_matched,
        "geo_fallback": geo_fallback,
        "test_shared_residential": bool(test_shared_residential),
        "allow_reuse_within_batch": bool(allow_reuse_within_batch),
        "force_proxy_type_filter": normalized_force_filter,
    }
