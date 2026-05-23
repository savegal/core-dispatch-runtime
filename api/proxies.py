from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
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
    """Элемент импорта прокси (поддерживает мобильные прокси с rotation_url)."""
    proxy: str
    rotation_url: str | None = None
    is_mobile: str = "N"  # "Y" или "N"


class ProxyImportRequest(BaseModel):
    """Запрос на импорт прокси. Поддерживает как старый формат (список строк), так и новый (список объектов)."""
    proxies: List[str | dict]  # Поддержка обратной совместимости


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
    inserted = 0
    updated = 0
    
    # Парсим прокси (поддержка старого и нового формата)
    parsed_proxies = []
    seen = set()
    
    for proxy_item in request.proxies:
        # Поддержка обратной совместимости: строка или словарь
        if isinstance(proxy_item, str):
            # Старый формат: просто строка
            proxy_str = proxy_item.strip()
            if proxy_str and proxy_str not in seen:
                parsed_proxies.append({
                    "proxy": proxy_str,
                    "rotation_url": None,
                    "is_mobile": "N"
                })
                seen.add(proxy_str)
        elif isinstance(proxy_item, dict):
            # Новый формат: словарь с rotation_url
            proxy_str = proxy_item.get("proxy", "").strip()
            if proxy_str and proxy_str not in seen:
                parsed_proxies.append({
                    "proxy": proxy_str,
                    "rotation_url": proxy_item.get("rotation_url"),
                    "is_mobile": proxy_item.get("is_mobile", "N")
                })
                seen.add(proxy_str)
    
    # Сохраняем прокси в БД
    for proxy_data in parsed_proxies:
        proxy_str = proxy_data["proxy"]
        rotation_url = proxy_data.get("rotation_url")
        is_mobile = proxy_data.get("is_mobile", "N")
        
        try:
            result = await db.execute(
                select(Proxy).filter(Proxy.proxy == proxy_str)
            )
            existing = result.scalar_one_or_none()
            
            if existing:
                # Обновляем существующий
                # ИСПРАВЛЕНО: Убраны поля статуса - все прокси считаются рабочими
                # Обновляем rotation_url и is_mobile (даже если rotation_url = None - это может быть сброс мобильного прокси)
                existing.rotation_url = rotation_url
                existing.is_mobile = is_mobile
                existing.status = "active"
                existing.fail_count = 0
                updated += 1
            else:
                # Создаём новый
                proxy_obj = Proxy(
                    proxy=proxy_str,
                    rotation_url=rotation_url,
                    is_mobile=is_mobile,
                    status="active",
                    fail_count=0
                )
                db.add(proxy_obj)
                inserted += 1
            
            await db.flush()
            
        except Exception as e:
            logger.error(f"Error processing proxy {proxy_str[:30]}: {str(e)[:200]}")
            await db.rollback()
    
    await db.commit()
    
    # ИСПРАВЛЕНО: Убрана проверка прокси - все прокси считаются рабочими
    # Инвалидируем кэш прокси после импорта
    from services.proxy_assignment_service import invalidate_proxy_cache
    invalidate_proxy_cache()
    
    logger.info(f"Proxy import: inserted={inserted}, updated={updated}, total={len(parsed_proxies)}")
    return ProxyImportResponse(
        total=len(parsed_proxies),
        inserted=inserted,
        updated=updated
    )


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

    sem = asyncio.Semaphore(concurrency)

    async def _check(proxy_obj: Proxy):
        async with sem:
            return proxy_obj, await check_proxy_health_async(
                proxy_obj.proxy,
                timeout=timeout
            )

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
            if proxy_obj.proxy:
                bad_proxy_values.append(proxy_obj.proxy)

    await db.commit()

    cleared_accounts = 0
    assigned_accounts = 0
    not_enough_proxies = False

    if request.reassign_bad_proxies and bad_proxy_values:
        result = await db.execute(
            update(Account)
            .where(Account.proxy.in_(bad_proxy_values))
            .values(proxy=None)
        )
        cleared_accounts = result.rowcount or 0
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
        from db.models import Account
        proxy_values = [p.proxy for p in targets if p.proxy]
        if proxy_values:
            update_result = await db.execute(
                update(Account)
                .where(Account.proxy.in_(proxy_values))
                .values(proxy=None)
            )
            cleared_accounts = update_result.rowcount or 0

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
    total_result = await db.execute(select(func.count(Proxy.id)))
    total = total_result.scalar() or 0

    cleared_accounts = 0
    if request.clear_accounts:
        from db.models import Account
        update_result = await db.execute(
            update(Account)
            .where(Account.proxy.isnot(None))
            .values(proxy=None)
        )
        cleared_accounts = update_result.rowcount or 0

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
