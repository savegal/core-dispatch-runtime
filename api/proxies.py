from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, or_, func
from db.database import get_db
from db.models import Proxy, Account
from api.auth import verify_admin_header
from services.proxy_service import check_proxy_health_async
from services.geo_service import detect_proxy_country
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
import asyncio
from typing import Annotated, List
from config.settings import get_settings
from datetime import datetime, timezone
import logging

router = APIRouter(prefix="/api/proxies", tags=["proxies"], dependencies=[Depends(verify_admin_header)])
logger = logging.getLogger(__name__)
settings = get_settings()

# ИСПРАВЛЕНО: Rate limiter для защиты API
limiter = Limiter(key_func=get_remote_address)


class ProxyImportItem(BaseModel):
    proxy: str
    rotation_url: str | None = None
    is_mobile: str = "N"
    country_code: str | None = None

    @field_validator("is_mobile")
    @classmethod
    def validate_type(cls, value):
        normalized = str(value).strip().upper()
        if normalized not in {"Y", "N"}:
            raise ValueError("is_mobile must be Y or N")
        return normalized

    @model_validator(mode="after")
    def validate_route(self):
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        from services.proxy_normalizer import (
            ProxyRoute, resolve_account_sticky_proxy, is_sticky_residential_template, _AUTO_SESSION_MARKER_RE,
        )
        self.proxy = self.proxy.strip()
        if _AUTO_SESSION_MARKER_RE.search(self.proxy):
            raise ValueError("Use an explicit {session} placeholder for residential sessions")
        resolved = resolve_account_sticky_proxy(self.proxy, SimpleNamespace(id=1, country_code=self.country_code))
        try:
            ProxyRoute.parse(resolved)
        except RuntimeError:
            raise ValueError("Invalid proxy address") from None
        if self.rotation_url:
            self.rotation_url = self.rotation_url.strip()
            parts = urlsplit(self.rotation_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("rotation_url must be an HTTP(S) URL")
        if self.is_mobile == "Y" and is_sticky_residential_template(self.proxy):
            raise ValueError("Mobile proxies use a fixed endpoint with rotation_url")
        if self.is_mobile == "Y" and not self.rotation_url:
            raise ValueError("Mobile proxies require rotation_url")
        if self.is_mobile == "N" and self.rotation_url:
            raise ValueError("Residential proxies use per-account sessions and cannot have rotation_url")
        if self.country_code:
            self.country_code = self.country_code.strip().upper()
            if len(self.country_code) != 2 or not self.country_code.isascii() or not self.country_code.isalpha():
                raise ValueError("country_code must contain two ASCII letters")
        return self


class ProxyImportRequest(BaseModel):
    proxies: List[str | ProxyImportItem]


class ProxyImportResponse(BaseModel):
    total: int
    inserted: int
    updated: int


class ProxyDeleteRequest(BaseModel):
    """Запрос на удаление прокси по ID и/или по строкам прокси."""
    ids: List[int] | None = None
    proxies: List[str] | None = None
    clear_accounts: bool = True  # Сбрасывать прокси у аккаунтов, которые используют удаляемые прокси


class ProxyDeleteAllRequest(BaseModel):
    """Запрос на полное удаление всех прокси."""
    clear_accounts: bool = True


class ProxyValidateRequest(BaseModel):
    """Запрос на быструю проверку прокси."""
    ids: List[int] | None = None
    proxies: List[str] | None = None
    timeout: float = 8.0
    concurrency: int = 30
    limit: int | None = None
    reassign_bad_proxies: bool = False  # очищать proxy у аккаунтов с bad/unknown
    assign_after: bool = False  # назначить прокси аккаунтам без proxy после очистки


class ProxyValidateResponse(BaseModel):
    checked: int
    active: int
    slow: int
    bad: int
    unknown: int
    errors: int
    cleared_accounts: int = 0
    assigned_accounts: int = 0
    not_enough_proxies: bool = False




@router.post("/import", response_model=ProxyImportResponse)
async def import_proxies(
    request: ProxyImportRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Импортирует прокси. Все прокси считаются рабочими.
    
    ИСПРАВЛЕНО: Убрана проверка прокси - все прокси считаются рабочими.
    """
    from services.proxy_assignment_service import invalidate_proxy_cache
    from services.proxy_normalizer import ProxyRouteError, normalize_proxy_string, is_sticky_residential_template

    parsed = {}
    try:
        for item in request.proxies:
            if isinstance(item, str):
                # Plain re-import preserves the existing mobile type and rotation URL.
                checked = ProxyImportItem(proxy=item)
                metadata = {}
            else:
                checked = item
                metadata = checked.model_dump(exclude={"proxy"}, exclude_unset=True)
                if "is_mobile" in checked.model_fields_set:
                    metadata["rotation_url"] = checked.rotation_url
            raw = checked.proxy
            if not is_sticky_residential_template(raw) and "{" not in raw:
                raw = normalize_proxy_string(raw)
            parsed[raw] = {**parsed.get(raw, {}), **metadata}
    except (ValueError, ProxyRouteError) as exc:
        raise HTTPException(status_code=422, detail="Invalid proxy configuration") from exc

    inserted = updated = 0
    try:
        from services.proxy_assignment_service import _lock_assignment_pool
        sources = await db.run_sync(_lock_assignment_pool)
        for raw, metadata in parsed.items():
            existing = next((source for source in sources if source.proxy == raw
                             or not is_sticky_residential_template(source.proxy)
                             and normalize_proxy_string(source.proxy) == raw), None)
            if existing:
                for key, value in metadata.items():
                    setattr(existing, key, value)
                existing.status = "active"
                existing.fail_count = 0
                updated += 1
            else:
                db.add(Proxy(proxy=raw, **metadata, status="active", fail_count=0))
                inserted += 1
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    invalidate_proxy_cache()
    return ProxyImportResponse(total=len(parsed), inserted=inserted, updated=updated)


@router.post("/{proxy_id}/rotate")
async def rotate_mobile_proxy_endpoint(proxy_id: int, db: AsyncSession = Depends(get_db)):
    """Request and verify a mobile IP change after active route users finish."""
    from types import SimpleNamespace
    from services.mobile_proxy_service import ensure_proxy_ip_rotated, save_proxy_last_ip, ProxyRotationError
    from services.proxy_normalizer import ProxyRouteError

    row = (await db.execute(select(Proxy).where(Proxy.id == proxy_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if row.is_mobile != "Y" or not row.rotation_url:
        raise HTTPException(status_code=409, detail="IP rotation requires a mobile proxy with rotation_url")
    proxy = SimpleNamespace(id=row.id, proxy=row.proxy, is_mobile=row.is_mobile, rotation_url=row.rotation_url)
    # The network operation owns a Redis rotation lease, not a database transaction.
    await db.rollback()
    try:
        ip = await ensure_proxy_ip_rotated(proxy, None, force_rotation=True)
        await save_proxy_last_ip(proxy.id, ip)
        return {"proxy_id": proxy.id, "ip": ip, "rotated": True}
    except (ProxyRotationError, ProxyRouteError):
        raise HTTPException(status_code=503, detail="Mobile IP rotation could not be confirmed") from None


@router.post("/validate", response_model=ProxyValidateResponse)
async def validate_proxies(
    request: ProxyValidateRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Быстро проверяет прокси и помечает статус (active/slow/bad/unknown).
    Опционально очищает proxy у аккаунтов с bad/unknown и назначает новые.
    """
    concurrency = max(1, min(request.concurrency, 50))
    timeout = max(3.0, min(request.timeout, 30.0))

    ids = request.ids or []
    proxies = [p.strip() for p in (request.proxies or []) if p and p.strip()]

    query = select(Proxy)
    filters = []
    if ids:
        filters.append(Proxy.id.in_(ids))
    if proxies:
        filters.append(Proxy.proxy.in_(proxies))
    if filters:
        query = query.filter(or_(*filters))
    if request.limit:
        query = query.limit(request.limit)

    result = await db.execute(query)
    proxy_rows = result.scalars().all()

    if not proxy_rows:
        return ProxyValidateResponse(
            checked=0, active=0, slow=0, bad=0, unknown=0, errors=0
        )

    from services.proxy_normalizer import find_account_proxy_record, is_sticky_residential_template

    def resolved_check_routes(sync_db):
        routes = {}
        for account in sync_db.query(Account).filter(Account.proxy.isnot(None), Account.proxy != ""):
            source = find_account_proxy_record(sync_db, account)
            if source is not None:
                routes.setdefault(source.id, account.proxy)
        return routes

    account_routes = await db.run_sync(resolved_check_routes)
    sem = asyncio.Semaphore(concurrency)

    async def _check(proxy_obj: Proxy):
        async with sem:
            raw = account_routes.get(proxy_obj.id, proxy_obj.proxy)
            if is_sticky_residential_template(raw):
                return proxy_obj, (False, "unknown", "Assign an account session before checking this gateway", None)
            outcome = await check_proxy_health_async(raw, timeout=timeout)
            if is_sticky_residential_template(proxy_obj.proxy) and not outcome[0]:
                # A provider session can fail while other sessions on the gateway work.
                outcome = (False, "unknown", outcome[2], outcome[3])
            return proxy_obj, outcome

    tasks = [asyncio.create_task(_check(p)) for p in proxy_rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    active = slow = bad = unknown = errors = 0
    bad_proxy_values: list[str] = []

    for item in results:
        if isinstance(item, Exception):
            errors += 1
            continue

        proxy_obj, check_result = item
        is_active, status, _, _ = check_result

        if is_active:
            if status == "slow":
                slow += 1
                proxy_obj.status = "slow"
            else:
                active += 1
                proxy_obj.status = "active"
            proxy_obj.fail_count = 0
        else:
            if status == "unknown":
                unknown += 1
                proxy_obj.status = "unknown"
            else:
                bad += 1
                proxy_obj.status = "bad"
            proxy_obj.fail_count = (proxy_obj.fail_count or 0) + 1
            if proxy_obj.proxy and not is_sticky_residential_template(proxy_obj.proxy):
                bad_proxy_values.append(proxy_obj.proxy)

    await db.commit()

    cleared_accounts = 0
    assigned_accounts = 0
    not_enough_proxies = False

    if request.reassign_bad_proxies and bad_proxy_values:
        from services.proxy_assignment_service import clear_proxy_bindings
        bad_ids = {proxy.id for proxy in proxy_rows if proxy.proxy in bad_proxy_values}
        cleared_accounts = await db.run_sync(lambda sync_db: clear_proxy_bindings(sync_db, bad_ids, skip_busy=True))
        await db.commit()

    if request.assign_after:
        from db.database import get_sync_db_context
        from services.proxy_assignment_service import assign_proxies_to_accounts
        try:
            with get_sync_db_context() as sync_db:
                assign_result = assign_proxies_to_accounts(
                    sync_db,
                    only_accounts_without_proxy=True,
                    reuse_existing=False
                )
                assigned_accounts = int(assign_result.get("assigned", 0))
                not_enough_proxies = bool(assign_result.get("not_enough_proxies", False))
        except Exception as e:
            logger.error(f"Error assigning proxies after validation: {e}", exc_info=True)

    from services.proxy_assignment_service import invalidate_proxy_cache
    invalidate_proxy_cache()

    return ProxyValidateResponse(
        checked=len(proxy_rows),
        active=active,
        slow=slow,
        bad=bad,
        unknown=unknown,
        errors=errors,
        cleared_accounts=cleared_accounts,
        assigned_accounts=assigned_accounts,
        not_enough_proxies=not_enough_proxies
    )


@router.post("/delete")
async def delete_proxies(
    request: ProxyDeleteRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Удаляет прокси по ID и/или по строкам proxy.
    По умолчанию сбрасывает proxy у аккаунтов, которые использовали эти прокси.
    """
    ids = request.ids or []
    proxies = [p.strip() for p in (request.proxies or []) if p and p.strip()]

    if not ids and not proxies:
        raise HTTPException(status_code=400, detail="Provide ids or proxies to delete")

    from services.proxy_assignment_service import _lock_assignment_pool, clear_proxy_bindings
    await db.run_sync(_lock_assignment_pool)
    query = select(Proxy)
    filters = []
    if ids:
        filters.append(Proxy.id.in_(ids))
    if proxies:
        filters.append(Proxy.proxy.in_(proxies))

    if len(filters) == 1:
        query = query.filter(filters[0])
    else:
        query = query.filter(or_(*filters))

    result = await db.execute(query)
    targets = result.scalars().all()

    if not targets:
        return {"deleted": 0, "cleared_accounts": 0}

    cleared_accounts = 0
    if request.clear_accounts:
        from services.proxy_normalizer import ProxyRouteError
        try:
            cleared_accounts = await db.run_sync(lambda sync_db: clear_proxy_bindings(sync_db, {p.id for p in targets}))
        except ProxyRouteError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None

    for p in targets:
        await db.delete(p)

    await db.commit()

    from services.proxy_assignment_service import invalidate_proxy_cache
    invalidate_proxy_cache()

    return {"deleted": len(targets), "cleared_accounts": cleared_accounts}


@router.post("/delete_all")
async def delete_all_proxies(
    request: ProxyDeleteAllRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Полностью удаляет все прокси.
    По умолчанию сбрасывает proxy у всех аккаунтов, у которых он был задан.
    """
    from services.proxy_assignment_service import _lock_assignment_pool, clear_proxy_bindings
    from services.proxy_normalizer import ProxyRouteError
    await db.run_sync(_lock_assignment_pool)
    total_result = await db.execute(select(func.count(Proxy.id)))
    total = total_result.scalar() or 0

    cleared_accounts = 0
    if request.clear_accounts:
        try:
            cleared_accounts = await db.run_sync(lambda sync_db: clear_proxy_bindings(sync_db, None))
        except ProxyRouteError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None

    # Удаляем все прокси
    await db.execute(Proxy.__table__.delete())
    await db.commit()

    from services.proxy_assignment_service import invalidate_proxy_cache
    invalidate_proxy_cache()

    return {"deleted": total, "cleared_accounts": cleared_accounts}


@router.get("/stats")
async def get_proxy_stats(db: AsyncSession = Depends(get_db)):
    """
    Возвращает статистику прокси.
    
    Показывает общее количество и распределение по статусам.
    """
    from sqlalchemy import func
    
    total_result = await db.execute(select(func.count(Proxy.id)))
    total = total_result.scalar()

    active_result = await db.execute(select(func.count(Proxy.id)).where(Proxy.status == "active"))
    slow_result = await db.execute(select(func.count(Proxy.id)).where(Proxy.status == "slow"))
    bad_result = await db.execute(select(func.count(Proxy.id)).where(Proxy.status == "bad"))
    unknown_result = await db.execute(select(func.count(Proxy.id)).where(Proxy.status == "unknown"))

    return {
        "total": total,
        "active": active_result.scalar() or 0,
        "slow": slow_result.scalar() or 0,
        "bad": bad_result.scalar() or 0,
        "unknown": unknown_result.scalar() or 0
    }


@router.get("")
async def list_proxies(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """
    Получает список прокси.
    
    ИСПРАВЛЕНО: Убраны статусы и фильтрация. Показываются полные прокси (без маскирования).
    """
    query = select(Proxy)
    query = query.order_by(Proxy.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    proxies = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "proxy": p.proxy,  # ИСПРАВЛЕНО: Показываем полный прокси без маскирования
            "country_code": p.country_code,
            "is_mobile": p.is_mobile,
            "rotation_url": p.rotation_url,
            "status": p.status,
            "fail_count": p.fail_count,
            "created_at": p.created_at.isoformat() if p.created_at else None
        }
        for p in proxies
    ]


# ИСПРАВЛЕНО: Удалены endpoints проверки прокси (/validate, /validate/progress, /recheck_bad, /bad)
# Все прокси считаются рабочими, проверка не требуется
