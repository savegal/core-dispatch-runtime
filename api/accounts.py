from fastapi import APIRouter, Depends, HTTPException, Header, Request, Request as FastAPIRequest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from db.database import get_db, get_sync_db_context
from db.models import Account, Proxy
from services.account_service import get_account_runtime_health
from api.auth import verify_admin_header
from services.proxy_assignment_service import assign_proxies_to_accounts
from services.app_settings_service import (
    get_setting,
    set_setting,
    get_proxy_type_filter,
    set_proxy_type_filter,
    get_proxy_prefer_mobile,
    set_proxy_prefer_mobile,
    get_account_proxy_test_mode,
    set_account_proxy_test_mode,
)
from config.limiter import limiter
from config.settings import get_settings
from typing import Annotated, List, Dict, Any
import asyncio
import logging

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(verify_admin_header)])
logger = logging.getLogger(__name__)

# ИСПРАВЛЕНО: Импортируем limiter из отдельного модуля


def _load_account_runtime_health() -> dict[str, Any]:
    """Run the CPU-heavy account scan outside FastAPI's event loop."""
    with get_sync_db_context() as db:
        return get_account_runtime_health(db)


class AccountRecord(BaseModel):
    label: str
    cookie_file: str | None = None
    cookies: str | None = None  # Inline cookies
    user_agent: str | None = None
    proxy: str | None = None


class BulkImportRequest(BaseModel):
    accounts: List[AccountRecord]


class BulkImportResponse(BaseModel):
    added: int
    updated: int
    skipped: int
    duplicates_by_cookies: int = 0  # Дубликаты по cookies (одинаковые cookies, разные label)
    assigned_proxies: int = 0
    without_proxies: int = 0


class AssignProxiesRequest(BaseModel):
    only_accounts_without_proxy: bool = True
    reuse_existing: bool = False
    allow_reuse_within_batch: bool = False
    test_shared_residential: bool = False
    force_proxy_type_filter: str | None = None
    use_geo_matching: bool | None = None


class AssignProxiesResponse(BaseModel):
    total_accounts_without_proxy: int
    assigned: int
    not_enough_proxies: bool
    free_proxies_before: int
    free_proxies_after: int
    geo_matched: int = 0
    geo_fallback: int = 0
    test_shared_residential: bool = False
    allow_reuse_within_batch: bool = False
    force_proxy_type_filter: str | None = None


class AccountProxyTestModeRequest(BaseModel):
    enabled: bool


class AccountPoolDiagnosticsResponse(BaseModel):
    test_mode: bool
    proxy_type_filter: str | None = None
    proxy_prefer_mobile: bool
    use_proxy: bool
    proxy_geo_matching: bool
    total_accounts: int
    active_accounts: int
    error_accounts_low: int
    with_proxy: int
    without_proxy: int
    with_auth_material: int
    proxy_total: int
    proxy_mobile: int
    proxy_residential: int
    main_sql_candidates: int
    main_sql_candidates_after_proxy: int
    notes: list[str] = []


class ReplaceProxyRequest(BaseModel):
    account_id: int | None = None  # Если None - заменяет у всех аккаунтов с медленными/проблемными прокси
    new_proxy_id: int | None = None  # Если None - автоматически выбирает лучший прокси
    replace_slow: bool = True  # Заменять ли у аккаунтов с медленными прокси
    replace_bad: bool = True  # Заменять ли у аккаунтов с нерабочими прокси


class ReplaceProxyResponse(BaseModel):
    replaced: int
    account_ids: List[int]




@router.post("/bulk_import", response_model=BulkImportResponse)
async def bulk_import(
    request: BulkImportRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Импортирует или обновляет аккаунты с автоназначением прокси.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    from db.models import Proxy
    
    added = 0
    updated = 0
    skipped = 0
    duplicates_by_cookies = 0  # Счетчик дубликатов по cookies
    assigned_proxies = 0  # Счетчик автоназначенных прокси
    without_proxies = 0  # Счетчик аккаунтов без прокси
    
    # ИСПРАВЛЕНО: Получаем список всех прокси для round-robin назначения (status удален)
    result = await db.execute(
        select(Proxy).order_by(Proxy.id)
    )
    active_proxies = result.scalars().all()
    proxy_country_by_proxy = {
        proxy_obj.proxy: proxy_obj.country_code
        for proxy_obj in active_proxies
        if proxy_obj.proxy and proxy_obj.country_code
    }
    proxy_round_robin_index = 0
    
    # Кеш для быстрой проверки дубликатов по cookies (оптимизация)
    # Ключ: normalized_cookies, Значение: account.label
    from services.cookie_validator import validate_cookies
    cookies_cache: dict[str, str] = {}
    
    # Предзагружаем normalized_cookies всех аккаунтов с cookies (для быстрой проверки)
    try:
        all_accounts_result = await db.execute(
            select(Account).filter(Account.cookies.isnot(None))
        )
        all_accounts = all_accounts_result.scalars().all()
        for account in all_accounts:
            if account.cookies:
                try:
                    account_validation = validate_cookies(account.cookies)
                    if account_validation.is_valid and account_validation.normalized_cookies:
                        cookies_cache[account_validation.normalized_cookies] = account.label
                except Exception:
                    # Игнорируем ошибки валидации при предзагрузке
                    pass
    except Exception as cache_err:
        logger.warning(f"Error preloading cookies cache: {cache_err}, will check duplicates on-the-fly")

    for acc_data in request.accounts:
        try:
            # ИСПРАВЛЕНО: Проверяем сначала по cookies (приоритет), потом по label
            # Если аккаунт найден по cookies - это тот же аккаунт (обновляем)
            # Если найден только по label - проверяем куки: если разные - создаём новый
            existing = None
            
            # Проверяем по cookies (приоритет)
            if acc_data.cookies:
                try:
                    validation_result = validate_cookies(acc_data.cookies)
                    if validation_result.is_valid and validation_result.normalized_cookies:
                        # Используем кеш для быстрой проверки дубликатов по cookies
                        if validation_result.normalized_cookies in cookies_cache:
                            existing_label = cookies_cache[validation_result.normalized_cookies]
                            # Нашли дубликат по cookies - получаем полный аккаунт из БД
                            result = await db.execute(
                                select(Account).filter(Account.label == existing_label)
                            )
                            existing = result.scalar_one_or_none()
                            if existing:
                                # Если найден по cookies - это обновление (даже если label другой)
                                logger.info(
                                    f"Found existing account by cookies: '{existing.label[:30]}' "
                                    f"(import label: '{acc_data.label[:30]}')"
                                )
                except Exception as cookie_check_err:
                    # Если ошибка при проверке cookies - игнорируем, продолжаем импорт
                    logger.debug(f"Error checking cookies duplicate for {acc_data.label[:30]}: {cookie_check_err}")
            
            # Если не нашли по cookies, проверяем по label
            if not existing:
                result = await db.execute(
                    select(Account).filter(Account.label == acc_data.label)
                )
                existing_by_label = result.scalar_one_or_none()
                if existing_by_label:
                    # Найден по label - проверяем куки
                    # Если куки разные - создаём новый аккаунт с уникальным label
                    if acc_data.cookies and existing_by_label.cookies:
                        try:
                            existing_validation = validate_cookies(existing_by_label.cookies)
                            new_validation = validate_cookies(acc_data.cookies)
                            if (existing_validation.is_valid and new_validation.is_valid and
                                existing_validation.normalized_cookies and new_validation.normalized_cookies and
                                existing_validation.normalized_cookies != new_validation.normalized_cookies):
                                # Куки разные - создаём новый аккаунт с уникальным label
                                # Генерируем уникальный label добавлением timestamp
                                from datetime import datetime, timezone
                                timestamp = int(datetime.now(timezone.utc).timestamp())
                                acc_data.label = f"{acc_data.label}_{timestamp}"
                                logger.info(
                                    f"Different cookies for label '{acc_data.label[:30]}', "
                                    f"creating new account with label '{acc_data.label[:30]}'"
                                )
                                existing = None  # Создадим новый аккаунт
                            else:
                                # Куки одинаковые или не удалось сравнить - обновляем
                                existing = existing_by_label
                        except Exception as compare_err:
                            # Если ошибка при сравнении - обновляем существующий (старое поведение)
                            logger.debug(f"Error comparing cookies: {compare_err}")
                            existing = existing_by_label
                    else:
                        # Нет куков у одного из аккаунтов - обновляем по label
                        existing = existing_by_label

            # Определяем proxy (из данных или автоназначение)
            proxy = acc_data.proxy
            was_assigned = False
            if not proxy and active_proxies:
                # Автоназначение по round-robin
                proxy_obj = active_proxies[proxy_round_robin_index % len(active_proxies)]
                proxy = proxy_obj.proxy
                proxy_round_robin_index += 1
                was_assigned = True
                assigned_proxies += 1
            
            if not proxy:
                without_proxies += 1

            if existing:
                # Обновляем существующий
                if acc_data.cookie_file:
                    existing.cookie_file = acc_data.cookie_file
                if acc_data.cookies:
                    existing.cookies = acc_data.cookies
                    # Обновляем country_code если cookies изменились
                    from services.geo_detection_service import detect_account_country
                    detected_country = detect_account_country(existing)
                    if detected_country:
                        existing.country_code = detected_country
                    elif proxy and not existing.country_code:
                        # Импорт должен быть быстрым: не делаем сетевую geo-проверку прокси
                        # для каждого аккаунта. Берём уже сохранённую страну прокси, если она есть.
                        proxy_country = proxy_country_by_proxy.get(proxy)
                        if proxy_country:
                            existing.country_code = proxy_country
                # Обновляем user_agent если передан, иначе оставляем существующий или ставим дефолтный
                if acc_data.user_agent:
                    existing.user_agent = acc_data.user_agent
                elif not existing.user_agent:
                    # Если у существующего аккаунта нет user_agent, устанавливаем дефолтный
                    existing.user_agent = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                if proxy:
                    existing.proxy = proxy
                if existing.status != "active":
                    existing.status = "active"
                existing.error_count_recent = 0
                existing.last_error = None
                updated += 1
            else:
                # Создаём новый
                # Устанавливаем дефолтный user_agent если не передан
                user_agent = acc_data.user_agent
                if not user_agent:
                    user_agent = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                
                account = Account(
                    label=acc_data.label,
                    cookie_file=acc_data.cookie_file,
                    cookies=acc_data.cookies,
                    user_agent=user_agent,
                    proxy=proxy,
                    status="active",
                    error_count_recent=0
                )
                
                # Автоматически определяем country_code из cookies
                from services.geo_detection_service import detect_account_country
                detected_country = detect_account_country(account)
                if detected_country:
                    account.country_code = detected_country
                elif proxy:
                    # Импорт не должен ждать внешние geo-сервисы. Если страна прокси уже
                    # известна в БД, используем её; иначе гео можно определить отдельной кнопкой.
                    proxy_country = proxy_country_by_proxy.get(proxy)
                    if proxy_country:
                        account.country_code = proxy_country
                
                db.add(account)
                added += 1

        except Exception as e:
            logger.error(f"Error importing account label={acc_data.label[:30]}: {str(e)[:200]}")
            skipped += 1

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Error committing bulk import: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")

    logger.info(
        f"Bulk import: added={added}, updated={updated}, skipped={skipped}, "
        f"duplicates_by_cookies={duplicates_by_cookies}, "
        f"assigned_proxies={assigned_proxies}, without_proxies={without_proxies}"
    )
    return BulkImportResponse(
        added=added,
        updated=updated,
        skipped=skipped,
        duplicates_by_cookies=duplicates_by_cookies,
        assigned_proxies=assigned_proxies,
        without_proxies=without_proxies
    )


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    """
    Возвращает статистику аккаунтов.
    
    ИСПРАВЛЕНО: Переведено на async, используется прямой async запрос вместо sync функции.
    """
    try:
        from sqlalchemy import func, and_
        from db.models import Account, Proxy
        
        # Выполняем async запросы напрямую
        total_result = await db.execute(select(func.count(Account.id)))
        total = total_result.scalar()
        
        active_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "active")
        )
        active = active_result.scalar()
        
        dead_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "dead")
        )
        dead = dead_result.scalar()
        
        error_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "error")
        )
        error = error_result.scalar()
        
        limited_result = await db.execute(
            select(func.count(Account.id)).filter(Account.status == "limited")
        )
        limited = limited_result.scalar()
        
        with_errors_result = await db.execute(
            select(func.count(Account.id)).filter(
                and_(Account.status == "active", Account.error_count_recent > 0)
            )
        )
        with_recent_errors = with_errors_result.scalar()
        
        return {
            "total": total or 0,
            "active": active or 0,
            "dead": dead or 0,
            "error": error or 0,
            "limited": limited or 0,
            "with_recent_errors": with_recent_errors or 0
        }
    except Exception as e:
        logger.error(f"Error getting account stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting stats: {str(e)}")


@router.get("/runtime_health")
async def runtime_health():
    """Returns runtime health of account pools used by monitor/main/reply flows."""
    try:
        return await asyncio.to_thread(_load_account_runtime_health)
    except Exception as e:
        logger.error(f"Error getting account runtime health: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting runtime health: {str(e)}")


@router.get("")
async def list_accounts(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
):
    """
    Получает список аккаунтов.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    query = select(Account)

    if status:
        query = query.filter(Account.status == status)

    query = query.order_by(Account.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    accounts = result.scalars().all()

    # НЕ возвращаем cookies в ответе (безопасность)
    return [
        {
            "id": acc.id,
            "label": acc.label,
            "status": acc.status,
            "country_code": acc.country_code,  # Добавляем гео-информацию
            "proxy": acc.proxy[:30] + "..." if acc.proxy and len(acc.proxy) > 30 else acc.proxy,  # Показываем прокси (маскированный)
            "today_main_comments": acc.today_main_comments,
            "today_replies": acc.today_replies,
            "last_used_at": acc.last_used_at.isoformat() if acc.last_used_at else None,
            "created_at": acc.created_at.isoformat() if acc.created_at else None
        }
        for acc in accounts
    ]


@router.post("/assign_proxies", response_model=AssignProxiesResponse)
async def assign_proxies_endpoint(
    request: AssignProxiesRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Распределяет прокси из пула активных прокси по аккаунтам.
    
    ИСПРАВЛЕНО: Переведено на async. Использует sync функцию через sync_db для обратной совместимости.
    """
    # ИСПРАВЛЕНО: Используем context manager для безопасного закрытия соединения
    from db.database import get_sync_db_context
    try:
        with get_sync_db_context() as sync_db:
            result = assign_proxies_to_accounts(
                sync_db,
                only_accounts_without_proxy=request.only_accounts_without_proxy,
                reuse_existing=request.reuse_existing,
                use_geo_matching=request.use_geo_matching,
                allow_reuse_within_batch=request.allow_reuse_within_batch,
                force_proxy_type_filter=request.force_proxy_type_filter,
                test_shared_residential=request.test_shared_residential,
            )
            return AssignProxiesResponse(**result)
    except Exception as e:
        logger.error(f"Error assigning proxies: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error assigning proxies: {str(e)}")


@router.get("/pool_diagnostics", response_model=AccountPoolDiagnosticsResponse)
async def account_pool_diagnostics_endpoint(
    _: None = Depends(verify_admin_header)
):
    """Показывает, сколько аккаунтов реально проходит proxy/main-comment фильтры."""
    from db.database import get_sync_db_context
    app_settings = get_settings()
    try:
        with get_sync_db_context() as sync_db:
            test_mode = get_account_proxy_test_mode(sync_db)
            proxy_type_filter = get_proxy_type_filter(sync_db)
            proxy_prefer_mobile = bool(get_proxy_prefer_mobile(sync_db))

            total_accounts = sync_db.query(Account).count()
            active_accounts = sync_db.query(Account).filter(Account.status == "active").count()
            error_accounts_low = sync_db.query(Account).filter(
                Account.status == "error",
                Account.error_count_recent < app_settings.ERROR_THRESHOLD,
            ).count()
            with_proxy = sync_db.query(Account).filter(Account.proxy.isnot(None), Account.proxy != "").count()
            without_proxy = max(0, total_accounts - with_proxy)
            with_auth_material = sync_db.query(Account).filter(
                or_(
                    and_(Account.cookies.isnot(None), Account.cookies != ""),
                    and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
                )
            ).count()

            proxy_total = sync_db.query(Proxy).filter(Proxy.status.in_(["active", "slow", "unknown"])).count()
            proxy_mobile = sync_db.query(Proxy).filter(
                Proxy.status.in_(["active", "slow", "unknown"]),
                Proxy.is_mobile == "Y",
            ).count()
            proxy_residential = max(0, proxy_total - proxy_mobile)

            base_q = sync_db.query(Account).filter(
                or_(
                    Account.status == "active",
                    and_(Account.status == "error", Account.error_count_recent < app_settings.ERROR_THRESHOLD),
                ),
                Account.today_main_comments < app_settings.MAX_ACTIONS_PER_ACCOUNT,
            )
            main_sql_candidates = base_q.count()

            proxy_q = base_q
            if app_settings.USE_PROXY:
                proxy_q = proxy_q.filter(Account.proxy.isnot(None), Account.proxy != "")
            if proxy_type_filter:
                proxy_q = proxy_q.outerjoin(Proxy, Account.proxy == Proxy.proxy)
                if str(proxy_type_filter).lower() == "mobile":
                    proxy_q = proxy_q.filter(Proxy.is_mobile == "Y")
                elif str(proxy_type_filter).lower() == "residential":
                    proxy_q = proxy_q.filter(or_(Proxy.id.is_(None), Proxy.is_mobile != "Y"))
            main_sql_candidates_after_proxy = proxy_q.count()

            notes: list[str] = []
            if test_mode:
                notes.append("Тестовый режим включён: proxy_type_filter должен быть none, residential можно шарить при назначении.")
            if app_settings.USE_PROXY and without_proxy > 0:
                notes.append(f"Есть аккаунты без proxy: {without_proxy}. При USE_PROXY=True они не попадают в работу.")
            if proxy_type_filter:
                notes.append(f"Включён жёсткий proxy_type_filter={proxy_type_filter}.")
            if main_sql_candidates_after_proxy == 0 and main_sql_candidates > 0:
                notes.append("Аккаунты отрезаются proxy-фильтрами, а не статусом аккаунта.")

            return AccountPoolDiagnosticsResponse(
                test_mode=test_mode,
                proxy_type_filter=proxy_type_filter,
                proxy_prefer_mobile=proxy_prefer_mobile,
                use_proxy=app_settings.USE_PROXY,
                proxy_geo_matching=app_settings.PROXY_GEO_MATCHING,
                total_accounts=total_accounts,
                active_accounts=active_accounts,
                error_accounts_low=error_accounts_low,
                with_proxy=with_proxy,
                without_proxy=without_proxy,
                with_auth_material=with_auth_material,
                proxy_total=proxy_total,
                proxy_mobile=proxy_mobile,
                proxy_residential=proxy_residential,
                main_sql_candidates=main_sql_candidates,
                main_sql_candidates_after_proxy=main_sql_candidates_after_proxy,
                notes=notes,
            )
    except Exception as e:
        logger.error(f"Error building account pool diagnostics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/test_mode")
async def get_account_proxy_test_mode_endpoint(
    _: None = Depends(verify_admin_header)
):
    from db.database import get_sync_db_context
    try:
        with get_sync_db_context() as sync_db:
            return {
                "enabled": get_account_proxy_test_mode(sync_db),
                "proxy_type_filter": get_proxy_type_filter(sync_db),
                "proxy_prefer_mobile": get_proxy_prefer_mobile(sync_db),
            }
    except Exception as e:
        logger.error(f"Error reading test mode: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test_mode")
async def set_account_proxy_test_mode_endpoint(
    request: AccountProxyTestModeRequest,
    _: None = Depends(verify_admin_header)
):
    """Включает/выключает тестовый режим аккаунтов/прокси."""
    from db.database import get_sync_db_context
    try:
        with get_sync_db_context() as sync_db:
            before = {
                "enabled": get_account_proxy_test_mode(sync_db),
                "proxy_type_filter": get_proxy_type_filter(sync_db),
                "proxy_prefer_mobile": get_proxy_prefer_mobile(sync_db),
            }
            assignment_result = None

            if request.enabled:
                if not get_account_proxy_test_mode(sync_db):
                    set_setting(sync_db, "account_proxy_test_mode_prev_proxy_type_filter", before["proxy_type_filter"])
                    set_setting(sync_db, "account_proxy_test_mode_prev_proxy_prefer_mobile", bool(before["proxy_prefer_mobile"]))

                set_account_proxy_test_mode(sync_db, True)
                set_proxy_type_filter(sync_db, None)
                set_proxy_prefer_mobile(sync_db, False)

                assignment_result = assign_proxies_to_accounts(
                    sync_db,
                    only_accounts_without_proxy=True,
                    reuse_existing=True,
                    use_geo_matching=False,
                    allow_reuse_within_batch=True,
                    force_proxy_type_filter="residential",
                    test_shared_residential=True,
                )
            else:
                prev_filter = get_setting(sync_db, "account_proxy_test_mode_prev_proxy_type_filter", None)
                prev_prefer = bool(get_setting(sync_db, "account_proxy_test_mode_prev_proxy_prefer_mobile", False))
                set_account_proxy_test_mode(sync_db, False)
                set_proxy_type_filter(sync_db, prev_filter if prev_filter in [None, "mobile", "residential"] else None)
                set_proxy_prefer_mobile(sync_db, prev_prefer)

            after = {
                "enabled": get_account_proxy_test_mode(sync_db),
                "proxy_type_filter": get_proxy_type_filter(sync_db),
                "proxy_prefer_mobile": get_proxy_prefer_mobile(sync_db),
            }
            return {"ok": True, "before": before, "after": after, "assignment": assignment_result}
    except Exception as e:
        logger.error(f"Error toggling account proxy test mode: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/replace_proxy", response_model=ReplaceProxyResponse)
async def replace_proxy_endpoint(
    request: ReplaceProxyRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Заменяет прокси у аккаунта или группы аккаунтов с проблемными прокси.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    from db.models import Proxy
    from sqlalchemy import and_, or_
    
    replaced = 0
    account_ids = []
    
    # Определяем новый прокси
    new_proxy_obj = None
    if request.new_proxy_id:
        result = await db.execute(
            select(Proxy).filter(Proxy.id == request.new_proxy_id)
        )
        new_proxy_obj = result.scalar_one_or_none()
        if not new_proxy_obj:
            raise HTTPException(status_code=400, detail="Proxy not found")
        if new_proxy_obj.dedicated_user_id is not None:
            raise HTTPException(status_code=400, detail="Dedicated user proxy cannot be assigned to an account")
    else:
        # Автоматически выбираем лучший прокси (быстрый и свободный)
        # Получаем использованные прокси
        accounts_result = await db.execute(
            select(Account.proxy).filter(
                and_(Account.proxy.isnot(None), Account.proxy != "")
            )
        )
        used_proxy_strings = {acc for acc in accounts_result.scalars().all() if acc}
        
        # ИСПРАВЛЕНО: Убраны фильтры по status и response_time_ms (поля удалены)
        query = select(Proxy).filter(Proxy.dedicated_user_id.is_(None))
        if used_proxy_strings:
            query = query.filter(~Proxy.proxy.in_(used_proxy_strings))
        query = query.order_by(Proxy.id)
        
        result = await db.execute(query)
        new_proxy_obj = result.scalar_one_or_none()
        
        if not new_proxy_obj:
            raise HTTPException(status_code=400, detail="No free proxies available")
    
    # Определяем аккаунты для замены
    if request.account_id:
        # Заменяем у конкретного аккаунта
        result = await db.execute(
            select(Account).filter(Account.id == request.account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        old_proxy = account.proxy
        account.proxy = new_proxy_obj.proxy
        replaced = 1
        account_ids = [account.id]
        logger.info(f"Replaced proxy for account {account.id} ({account.label}): {old_proxy[:30] if old_proxy else None} -> {new_proxy_obj.proxy[:30]}")
    else:
        # Заменяем у всех аккаунтов с проблемными прокси
        query = select(Account).filter(Account.status != "dead")
        
        # ИСПРАВЛЕНО: Убрана фильтрация по статусу прокси (поле status удалено)
        # Теперь все прокси считаются активными, фильтрация не нужна
        # Если нужно заменить прокси у всех аккаунтов - просто выбираем всех не dead
        
        result = await db.execute(query)
        accounts_to_update = result.scalars().all()
        
        for account in accounts_to_update:
            old_proxy = account.proxy
            account.proxy = new_proxy_obj.proxy
            replaced += 1
            account_ids.append(account.id)
            logger.info(f"Replaced proxy for account {account.id} ({account.label}): {old_proxy[:30] if old_proxy else None} -> {new_proxy_obj.proxy[:30]}")
    
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Error committing proxy replacement: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
    
    return ReplaceProxyResponse(
        replaced=replaced,
        account_ids=account_ids
    )


@router.delete("/dead")
async def delete_dead_accounts(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Удаляет аккаунты со статусом 'dead'.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    """
    result = await db.execute(
        select(Account).filter(Account.status == "dead")
    )
    dead_accounts = result.scalars().all()
    deleted_count = len(dead_accounts)
    
    for account in dead_accounts:
        await db.delete(account)
    
    await db.commit()
    logger.info(f"Deleted {deleted_count} dead accounts")
    return {"deleted_count": deleted_count}


class ValidateAccountsResponse(BaseModel):
    total: int
    checked: int
    valid: int
    invalid: int
    errors: int
    results: List[Dict]


# ИСПРАВЛЕНО: Используем Redis для хранения прогресса валидации (thread-safe)
# Ключ для хранения прогресса в Redis
VALIDATION_PROGRESS_KEY = "validation:accounts:progress"

import redis
from config.settings import get_settings

settings = get_settings()
redis_client = None

def get_redis_client():
    """Получает Redis клиент (singleton)."""
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return redis_client


@router.get("/validate/progress")
async def get_validation_progress():
    """
    Возвращает текущий прогресс валидации аккаунтов.
    Используется для обновления статуса в реальном времени.
    ИСПРАВЛЕНО: Использует Redis вместо глобальной переменной.
    """
    try:
        r = get_redis_client()
        progress_str = r.get(VALIDATION_PROGRESS_KEY)
        if progress_str:
            import json
            return json.loads(progress_str)
        return {
            "running": False,
            "total": 0,
            "checked": 0,
            "valid": 0,
            "invalid": 0,
            "errors": 0,
            "current_account": None
        }
    except Exception as e:
        logger.error(f"Error getting validation progress from Redis: {e}")
        return {
            "running": False,
            "total": 0,
            "checked": 0,
            "valid": 0,
            "invalid": 0,
            "errors": 0,
            "current_account": None
        }


@router.post("/validate")
@limiter.limit("5/minute")  # ИСПРАВЛЕНО: Rate limiting для валидации аккаунтов
async def validate_accounts(
    request: FastAPIRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Проверяет валидность куков всех активных аккаунтов.
    ИСПРАВЛЕНО: Использует браузерную валидацию (Playwright) для точной проверки.
    HTTP валидация неточная - TikTok различает API и браузерные сессии.
    
    Returns:
        ValidateAccountsResponse: результаты проверки
    """
    try:
        import asyncio
        import sys
        import json
        from datetime import datetime, timezone
        from executor.tiktok_client import quick_validate_account
    except Exception as e:
        logger.error(f"Error importing modules in validate_accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import error: {str(e)[:200]}")
    
    # ИСПРАВЛЕНО: Для Windows устанавливаем ProactorEventLoopPolicy
    # Это необходимо для работы Playwright в API endpoints
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # ИСПРАВЛЕНО: Проверяем через Redis
    try:
        r = get_redis_client()
    except Exception as e:
        logger.error(f"Error getting Redis client: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)[:200]}")
    try:
        progress_str = r.get(VALIDATION_PROGRESS_KEY)
        if progress_str:
            current_progress = json.loads(progress_str)
            if current_progress.get("running", False):
                return {
                    "error": "Validation already in progress",
                    "progress": current_progress
                }
    except Exception as e:
        logger.warning(f"Error checking validation progress: {e}")
    
    # Получаем все аккаунты со статусом active или error
    try:
        result = await db.execute(
            select(Account).filter(Account.status.in_(["active", "error"]))
        )
        accounts = result.scalars().all()
        total = len(accounts)
        logger.info(f"Found {total} accounts to validate")
    except Exception as e:
        logger.error(f"Error fetching accounts for validation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database query error: {str(e)[:200]}")
    
    # ИСПРАВЛЕНО: Если нет аккаунтов для проверки - возвращаем понятное сообщение
    if total == 0:
        # Проверяем, есть ли вообще аккаунты в базе
        all_accounts_result = await db.execute(select(Account))
        all_accounts = all_accounts_result.scalars().all()
        all_total = len(all_accounts)
        
        if all_total == 0:
            return {
                "total": 0,
                "checked": 0,
                "valid": 0,
                "invalid": 0,
                "errors": 0,
                "results": [],
                "message": "В базе нет аккаунтов. Добавьте аккаунты через импорт."
            }
        else:
            # Есть аккаунты, но все в статусе dead или другом
            dead_count = len([a for a in all_accounts if a.status == "dead"])
            other_count = all_total - dead_count
            
            return {
                "total": 0,
                "checked": 0,
                "valid": 0,
                "invalid": 0,
                "errors": 0,
                "results": [],
                "message": f"Нет аккаунтов для проверки. Всего аккаунтов: {all_total}, из них dead: {dead_count}, других статусов: {other_count}. Проверяются только аккаунты со статусом 'active' или 'error'."
            }
    
    # ИСПРАВЛЕНО: Инициализируем прогресс в Redis
    try:
        r = get_redis_client()
    except Exception as e:
        logger.error(f"Error getting Redis client for progress: {e}", exc_info=True)
        # Продолжаем без Redis - прогресс не будет сохраняться
        r = None
    
    def update_progress(**kwargs):
        """Обновляет прогресс в Redis."""
        if r is None:
            return  # Redis недоступен, пропускаем обновление прогресса
        try:
            progress_str = r.get(VALIDATION_PROGRESS_KEY)
            if progress_str:
                progress = json.loads(progress_str)
            else:
                progress = {
                    "running": True,
                    "total": total,
                    "checked": 0,
                    "valid": 0,
                    "invalid": 0,
                    "errors": 0,
                    "current_account": None
                }
            progress.update(kwargs)
            r.set(VALIDATION_PROGRESS_KEY, json.dumps(progress), ex=3600)
        except Exception as e:
            logger.warning(f"Error updating validation progress: {e}")
    
    initial_progress = {
        "running": True,
        "total": total,
        "checked": 0,
        "valid": 0,
        "invalid": 0,
        "errors": 0,
        "current_account": None
    }
    if r is not None:
        r.set(VALIDATION_PROGRESS_KEY, json.dumps(initial_progress), ex=3600)  # TTL 1 час
    
    results = []
    valid_count = 0
    invalid_count = 0
    error_count = 0
    
    # ОПТИМИЗАЦИЯ: Увеличено количество параллельных валидаций для скорости
    # Используем настройку из settings или дефолтное значение
    from config.settings import get_settings
    app_settings = get_settings()
    max_concurrent = min(app_settings.VALIDATION_MAX_CONCURRENT, total)  # Из settings (по умолчанию 12)
    logger.info(f"Starting browser validation of {total} accounts (max {max_concurrent} concurrent)")
    
    semaphore = asyncio.Semaphore(max_concurrent)
    
    def validate_single_sync(account, timeout_seconds=None):
        """Синхронная обертка для валидации на Windows."""
        # ИСПРАВЛЕНО: На Windows создаем новый event loop с правильной политикой
        if sys.platform == 'win32':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(quick_validate_account(account, timeout=timeout_seconds))
            finally:
                loop.close()
        else:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(quick_validate_account(account, timeout=timeout_seconds))
    
    async def validate_single(account):
        """Валидирует один аккаунт с семафором."""
        nonlocal valid_count, invalid_count, error_count
        
        async with semaphore:
            update_progress(current_account=account.label)
            
            try:
                # ИСПРАВЛЕНО: Браузерная проверка (Playwright) - точная
                # На Windows используем синхронную функцию в отдельном потоке
                if sys.platform == 'win32':
                    import concurrent.futures
                    # ИСПРАВЛЕНО: Увеличиваем таймаут с учетом типа прокси
                    # Проверяем, является ли прокси мобильным через async сессию
                    timeout_seconds = app_settings.VALIDATION_TIMEOUT_SECONDS
                    if account.proxy:
                        try:
                            from db.models import Proxy
                            from sqlalchemy import select, func, and_, or_
                            proxy_result = await db.execute(
                                select(Proxy).filter(Proxy.proxy == account.proxy)
                            )
                            proxy_obj = proxy_result.scalar_one_or_none()
                            if proxy_obj and proxy_obj.is_mobile == "Y":
                                # Используем настройку для мобильных прокси, если она доступна
                                if hasattr(app_settings, 'VALIDATION_TIMEOUT_MOBILE_SECONDS'):
                                    timeout_seconds = app_settings.VALIDATION_TIMEOUT_MOBILE_SECONDS
                                else:
                                    timeout_seconds = app_settings.VALIDATION_TIMEOUT_SECONDS * 2
                        except Exception as e:
                            # Если не удалось проверить - используем базовый таймаут
                            logger.debug(f"Could not check proxy type for account {account.id}: {e}")
                            pass
                    
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        # Передаем таймаут в validate_single_sync
                        future = executor.submit(validate_single_sync, account, timeout_seconds)
                        # Добавляем запас времени (20%) для надежности ThreadPoolExecutor
                        executor_timeout = int(timeout_seconds * 1.2)
                        is_valid = future.result(timeout=executor_timeout)
                else:
                    is_valid = await quick_validate_account(account)
                validation_result = {"valid": is_valid, "username": None, "error": None if is_valid else "Browser validation failed - login required"}
                
                result_entry = {
                    "account_id": account.id,
                    "label": account.label,
                    "valid": validation_result["valid"],
                    "username": validation_result.get("username"),
                    "error": validation_result.get("error"),
                    "old_status": account.status
                }
                
                # ИСПРАВЛЕНО: Браузерная валидация точная - обновляем статус аккуратно
                if validation_result["valid"]:
                    valid_count += 1
                    update_progress(valid=valid_count)
                    
                    # Если аккаунт был в error - возвращаем в active
                    if account.status == "error":
                        account.status = "active"
                        account.error_count_recent = 0
                        account.last_error = None
                    account.last_validation_ok = datetime.now(timezone.utc)
                    result_entry["new_status"] = account.status
                    result_entry["note"] = "Browser validation passed"
                else:
                    invalid_count += 1
                    update_progress(invalid=invalid_count)
                    
                    # ИСПРАВЛЕНО: Браузерная валидация точная - помечаем как error
                    account.status = "error"
                    account.error_count_recent = (account.error_count_recent or 0) + 1
                    account.last_error = validation_result.get("error", "Browser validation failed - login required")
                    result_entry["new_status"] = "error"
                    result_entry["note"] = "Browser validation failed - cookies invalid"
                
                return result_entry
                
            except Exception as e:
                error_count += 1
                update_progress(errors=error_count)
                logger.error(f"Error validating account {account.id}: {e}")
                return {
                    "account_id": account.id,
                    "label": account.label,
                    "valid": False,
                    "error": str(e)[:200],
                    "old_status": account.status,
                    "new_status": account.status
                }
            finally:
                # Обновление checked будет после gather
                pass
    
    try:
        # Запускаем все проверки параллельно
        tasks = [validate_single(acc) for acc in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Обрабатываем исключения в результатах
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Error validating account {accounts[i].id}: {result}", exc_info=True)
                error_count += 1
                processed_results.append({
                    "account_id": accounts[i].id,
                    "label": accounts[i].label,
                    "valid": False,
                    "error": str(result)[:200],
                    "old_status": accounts[i].status,
                    "new_status": accounts[i].status
                })
            else:
                processed_results.append(result)
        results = processed_results
        
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"Error committing validation results: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
    except Exception as e:
        logger.error(f"Critical error in validate_accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Validation error: {str(e)[:200]}")
        
    finally:
        # ИСПРАВЛЕНО: Обновляем финальный статус в Redis
        try:
            if r is not None:
                update_progress(running=False, current_account=None)
        except Exception as e:
            logger.warning(f"Error updating final validation progress: {e}")
    
    logger.info(f"Validation complete: {valid_count} valid, {invalid_count} invalid, {error_count} errors")
    
    return {
        "total": total,
        "checked": total,
        "valid": valid_count,
        "invalid": invalid_count,
        "errors": error_count,
        "results": results
    }


@router.post("/validate/{account_id}")
async def validate_single_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Проверяет валидность куков одного аккаунта.
    
    ИСПРАВЛЕНО: Переведено на async для соответствия async БД.
    
    Args:
        account_id: ID аккаунта для проверки
        
    Returns:
        dict: результат проверки
    """
    import sys
    import concurrent.futures
    from executor.tiktok_client import quick_validate_account
    from datetime import datetime, timezone
    
    result_query = await db.execute(
        select(Account).filter(Account.id == account_id)
    )
    account = result_query.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    def validate_sync():
        """Синхронная обертка для валидации на Windows."""
        if sys.platform == 'win32':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(quick_validate_account(account))
            finally:
                loop.close()
        else:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(quick_validate_account(account))
    
    try:
        # ИСПРАВЛЕНО: Используем браузерную валидацию вместо HTTP
        # На Windows используем отдельный поток с новым event loop
        if sys.platform == 'win32':
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(validate_sync)
                is_valid = future.result(timeout=60)  # 60 секунд таймаут
        else:
            is_valid = await quick_validate_account(account)
        now = datetime.now(timezone.utc)
        
        result = {
            "account_id": account.id,
            "label": account.label,
            "valid": is_valid,
            "username": None,  # Браузерная валидация не возвращает username
            "error": None if is_valid else "Browser validation failed - login required",
            "old_status": account.status
        }
        
        # Обновляем статус и кэш
        account.last_checked_at = now
        
        if is_valid:
            if account.status in ["error", "dead"]:
                account.status = "active"
                account.error_count_recent = 0
                account.last_error = None
            account.validation_fail_count = 0
            account.last_validation_ok = now
            result["new_status"] = "active"
            result["note"] = "Browser validation passed"
        else:
            # ИСПРАВЛЕНО: Браузерная валидация точная - помечаем как error
            account.status = "error"
            account.error_count_recent = (account.error_count_recent or 0) + 1
            account.last_error = "Browser validation failed - login required"
            result["new_status"] = "error"
            result["note"] = "Browser validation failed - cookies invalid"
        
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.error(f"Error committing validation result for account {account_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error validating account {account_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Validation error: {str(e)[:200]}")


# Глобальное состояние для прогресса перепроверки мёртвых аккаунтов
_recheck_dead_progress = {
    "running": False,
    "total": 0,
    "checked": 0,
    "revived": 0,
    "still_dead": 0,
    "current_account": None
}


@router.get("/recheck_dead/progress")
async def get_recheck_dead_progress():
    """Возвращает прогресс перепроверки мёртвых аккаунтов."""
    return _recheck_dead_progress.copy()


@router.post("/recheck_dead")
async def recheck_dead_accounts(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Перепроверяет все мёртвые аккаунты.
    Если куки ожили - возвращает аккаунт в active.
    
    Returns:
        dict: результаты перепроверки
    """
    import asyncio
    from datetime import datetime, timezone
    # ИСПРАВЛЕНО: Импорт браузерной валидации вместо HTTP
    # HTTP валидация неточная - TikTok различает API и браузерные сессии
    
    try:
        global _recheck_dead_progress
        
        if _recheck_dead_progress["running"]:
            return {
                "error": "Recheck already in progress",
                "progress": _recheck_dead_progress.copy()
            }
        
        # Получаем все мёртвые аккаунты
        result = await db.execute(
            select(Account).filter(Account.status == "dead")
        )
        dead_accounts = result.scalars().all()
        total = len(dead_accounts)
        
        if total == 0:
            return {
                "total": 0,
                "checked": 0,
                "revived": 0,
                "still_dead": 0,
                "results": [],
                "message": "No dead accounts to recheck"
            }
        
        _recheck_dead_progress = {
            "running": True,
            "total": total,
            "checked": 0,
            "revived": 0,
            "still_dead": 0,
            "current_account": None
        }
        
        results = []
        revived_count = 0
        still_dead_count = 0
        
        logger.info(f"Starting recheck of {total} dead accounts")
        
        # Параллельная проверка (до 5 одновременно)
        semaphore = asyncio.Semaphore(5)
        
        async def check_single(account):
            nonlocal revived_count, still_dead_count
            
            async with semaphore:
                _recheck_dead_progress["current_account"] = account.label
                
                try:
                    # ИСПРАВЛЕНО: Используем браузерную валидацию (как в executor)
                    # HTTP валидация неточная - TikTok различает API и браузерные сессии
                    # Если аккаунт был помечен как dead через браузерную валидацию,
                    # то и recheck должен использовать браузерную валидацию
                    from executor.tiktok_client import quick_validate_account
                    import sys
                    import concurrent.futures
                    
                    # ИСПРАВЛЕНО: На Windows используем отдельный поток с новым event loop
                    def validate_sync_recheck():
                        if sys.platform == 'win32':
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            try:
                                return loop.run_until_complete(quick_validate_account(account))
                            finally:
                                loop.close()
                        else:
                            loop = asyncio.get_event_loop()
                            return loop.run_until_complete(quick_validate_account(account))
                    
                    if sys.platform == 'win32':
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(validate_sync_recheck)
                            is_valid = future.result(timeout=60)  # 60 секунд таймаут
                    else:
                        is_valid = await quick_validate_account(account)
                    now = datetime.now(timezone.utc)
                    
                    validation_result = {
                        "valid": is_valid,
                        "username": None,  # Браузерная валидация не возвращает username
                        "error": None if is_valid else "Браузерная валидация не прошла"
                    }
                    
                    result_entry = {
                        "account_id": account.id,
                        "label": account.label,
                        "valid": validation_result["valid"],
                        "username": validation_result.get("username"),
                        "error": validation_result.get("error")
                    }
                    
                    account.last_checked_at = now
                    
                    if validation_result["valid"]:
                        # ИСПРАВЛЕНО: Аккаунт ожил через браузерную валидацию (надежно)
                        account.status = "active"
                        account.validation_fail_count = 0
                        account.last_validation_ok = now
                        account.error_count_recent = 0
                        account.last_error = None
                        revived_count += 1
                        _recheck_dead_progress["revived"] = revived_count
                        result_entry["new_status"] = "active"
                        logger.info(f"Account {account.id} ({account.label}) revived via browser validation!")
                    else:
                        # Аккаунт все еще мертв - браузерная валидация подтверждает
                        still_dead_count += 1
                        _recheck_dead_progress["still_dead"] = still_dead_count
                        result_entry["new_status"] = "dead"
                        logger.debug(f"Account {account.id} ({account.label}) still dead (browser validation failed)")
                    
                    return result_entry
                    
                except Exception as e:
                    still_dead_count += 1
                    _recheck_dead_progress["still_dead"] = still_dead_count
                    logger.error(f"Error rechecking account {account.id}: {e}")
                    return {
                        "account_id": account.id,
                        "label": account.label,
                        "valid": False,
                        "error": str(e)[:200],
                        "new_status": "dead"
                    }
                finally:
                    _recheck_dead_progress["checked"] += 1
        
        try:
            tasks = [check_single(acc) for acc in dead_accounts]
            results = await asyncio.gather(*tasks)
            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"Error committing recheck dead accounts results: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
        finally:
            _recheck_dead_progress["running"] = False
            _recheck_dead_progress["current_account"] = None
        
        logger.info(f"Recheck complete: {revived_count} revived, {still_dead_count} still dead")
        
        return {
            "total": total,
            "checked": total,
            "revived": revived_count,
            "still_dead": still_dead_count,
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error rechecking dead accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error rechecking dead accounts: {str(e)[:200]}")


# ========== НОВЫЕ ДЕЙСТВИЯ НАД АККАУНТАМИ ==========

@router.get("/export")
@limiter.limit("10/minute")
async def export_accounts(
    request: Request,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Экспортирует аккаунты в формате для импорта.
    Можно фильтровать по статусу.
    """
    try:
        query = select(Account)
        if status:
            query = query.filter(Account.status == status)
        
        result = await db.execute(query)
        accounts = result.scalars().all()
        
        lines = []
        for acc in accounts:
            parts = [acc.label]
            if acc.cookie_file:
                parts.append(f"cookie_file:{acc.cookie_file}")
            elif acc.cookies:
                parts.append(f"cookies:{acc.cookies[:100]}")  # Ограничиваем длину
            if acc.user_agent:
                parts.append(f"user_agent:{acc.user_agent}")
            if acc.proxy:
                parts.append(f"proxy:{acc.proxy}")
            lines.append(" | ".join(parts))
        
        return {
            "total": len(accounts),
            "format": "text",
            "data": "\n".join(lines)
        }
    except Exception as e:
        logger.error(f"Error exporting accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error exporting accounts: {str(e)[:200]}")


@router.delete("/by_status/{status}")
@limiter.limit("5/minute")
async def delete_accounts_by_status(
    request: Request,
    status: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Удаляет аккаунты по статусу.
    """
    if status not in ("active", "error", "dead", "limited"):
        raise HTTPException(status_code=400, detail="Invalid status")
    
    try:
        result = await db.execute(
            select(Account).filter(Account.status == status)
        )
        accounts = result.scalars().all()
        deleted_count = len(accounts)
        
        for account in accounts:
            await db.delete(account)
        
        await db.commit()
        logger.info(f"Deleted {deleted_count} accounts with status '{status}'")
        return {"deleted_count": deleted_count, "status": status}
    except Exception as e:
        await db.rollback()
        logger.error(f"Error deleting accounts by status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error deleting accounts: {str(e)[:200]}")


@router.delete("/all")
@limiter.limit("2/minute")
async def delete_all_accounts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Deletes every account from the account pool.
    """
    try:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()

        deleted_by_status: dict[str, int] = {}
        for account in accounts:
            status = account.status or "unknown"
            deleted_by_status[status] = deleted_by_status.get(status, 0) + 1
            await db.delete(account)

        deleted_count = len(accounts)
        await db.commit()

        logger.warning(
            "Deleted all accounts: total=%s by_status=%s",
            deleted_count,
            deleted_by_status,
        )
        return {
            "deleted_count": deleted_count,
            "deleted_by_status": deleted_by_status,
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Error deleting all accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error deleting all accounts: {str(e)[:200]}")


@router.get("/{account_id}")
@limiter.limit("60/minute")
async def get_account_details(
    request: Request,
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Получает детальную информацию об аккаунте.
    """
    try:
        result = await db.execute(
            select(Account).filter(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        return {
            "id": account.id,
            "label": account.label,
            "status": account.status,
            "country_code": account.country_code,
            "proxy": account.proxy,
            "cookie_file": account.cookie_file,
            "has_cookies": bool(account.cookies),
            "user_agent": account.user_agent,
            "error_count_recent": account.error_count_recent or 0,
            "last_error": account.last_error,
            "today_main_comments": account.today_main_comments or 0,
            "today_replies": account.today_replies or 0,
            "last_main_comment_at": account.last_main_comment_at.isoformat() if account.last_main_comment_at else None,
            "last_reply_at": account.last_reply_at.isoformat() if account.last_reply_at else None,
            "last_checked_at": account.last_checked_at.isoformat() if account.last_checked_at else None,
            "created_at": account.created_at.isoformat() if account.created_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting account details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting account details: {str(e)[:200]}")


class UpdateAccountRequest(BaseModel):
    label: str | None = None
    cookies: str | None = None
    cookie_file: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    status: str | None = None


@router.put("/{account_id}")
@limiter.limit("30/minute")
async def update_account(
    request: Request,
    account_id: int,
    update_data: UpdateAccountRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Обновляет данные аккаунта.
    """
    try:
        result = await db.execute(
            select(Account).filter(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        if update_data.label:
            account.label = update_data.label
        if update_data.cookies is not None:
            account.cookies = update_data.cookies
        if update_data.cookie_file is not None:
            account.cookie_file = update_data.cookie_file
        if update_data.user_agent is not None:
            account.user_agent = update_data.user_agent
        if update_data.proxy is not None:
            account.proxy = update_data.proxy
        if update_data.status and update_data.status in ("active", "error", "dead", "limited", "reauth_needed", "banned"):
            account.status = update_data.status
        
        await db.commit()
        logger.info(f"Updated account {account_id}")
        return {"success": True, "account_id": account_id}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error updating account: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating account: {str(e)[:200]}")


@router.post("/{account_id}/reset_errors")
@limiter.limit("30/minute")
async def reset_account_errors(
    request: Request,
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Сбрасывает счетчики ошибок аккаунта и возвращает статус в 'active' если был 'error'.
    """
    try:
        result = await db.execute(
            select(Account).filter(Account.id == account_id)
        )
        account = result.scalar_one_or_none()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        account.error_count_recent = 0
        account.last_error = None
        if account.status == "error":
            account.status = "active"
        
        await db.commit()
        logger.info(f"Reset errors for account {account_id}")
        return {"success": True, "account_id": account_id}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error resetting account errors: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error resetting account errors: {str(e)[:200]}")


class BulkUpdateStatusRequest(BaseModel):
    account_ids: List[int]
    new_status: str


@router.post("/bulk_update_status")
@limiter.limit("10/minute")
async def bulk_update_status(
    request: Request,
    update_data: BulkUpdateStatusRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Массово изменяет статус аккаунтов.
    """
    if update_data.new_status not in ("active", "error", "dead", "limited", "reauth_needed", "banned"):
        raise HTTPException(status_code=400, detail="Invalid status")
    
    try:
        result = await db.execute(
            select(Account).filter(Account.id.in_(update_data.account_ids))
        )
        accounts = result.scalars().all()
        updated_count = len(accounts)
        
        for account in accounts:
            account.status = update_data.new_status
            if update_data.new_status == "active":
                account.error_count_recent = 0
                account.last_error = None
        
        await db.commit()
        logger.info(f"Bulk updated {updated_count} accounts to status '{update_data.new_status}'")
        return {"success": True, "updated_count": updated_count}
    except Exception as e:
        await db.rollback()
        logger.error(f"Error bulk updating account status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error bulk updating status: {str(e)[:200]}")


@router.post("/make_all_active")
@limiter.limit("5/minute")
async def make_all_accounts_active(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Устанавливает статус 'active' для всех аккаунтов.
    Сбрасывает error_count_recent, last_error, validation_fail_count.
    """
    try:
        result = await db.execute(select(Account))
        accounts = result.scalars().all()
        total_count = len(accounts)

        updated_count = 0
        for account in accounts:
            if account.status != "active":
                account.status = "active"
                updated_count += 1
            account.error_count_recent = 0
            account.last_error = None
            account.validation_fail_count = 0

        await db.commit()
        logger.info(f"Made all {total_count} accounts active, reset all counters (updated {updated_count} statuses)")
        return {
            "success": True,
            "total_accounts": total_count,
            "updated_count": updated_count,
            "message": f"Все {total_count} аккаунтов установлены как активные",
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Error making all accounts active: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error making all accounts active: {str(e)[:200]}")


@router.post("/reset_cooldowns")
@limiter.limit("10/minute")
async def reset_account_cooldowns(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Мягкий сброс runtime cooldown'ов для всех аккаунтов.

    Что делает:
    - Очищает last_error (убирает JSON с cooldown_until) у не-dead аккаунтов
    - Сбрасывает error_count_recent у не-dead аккаунтов
    - Переводит error → active
    - Очищает Redis bad-proxy кеш

    НЕ трогает dead аккаунты (они мёртвые по серьёзной причине).
    Для полного сброса включая dead — используйте make_all_active.
    """
    try:
        from services.proxy_health_service import clear_all_bad_proxies

        # 1. Сбрасываем cooldown и ошибки для не-dead аккаунтов
        result = await db.execute(
            select(Account).filter(Account.status.notin_(["dead", "banned"]))
        )
        accounts = result.scalars().all()
        total = len(accounts)

        cooldowns_cleared = 0
        errors_reset = 0
        statuses_restored = 0

        for account in accounts:
            had_cooldown = bool(account.last_error)
            had_errors = bool(account.error_count_recent and account.error_count_recent > 0)

            account.last_error = None
            account.error_count_recent = 0

            if had_cooldown:
                cooldowns_cleared += 1
            if had_errors:
                errors_reset += 1

            if account.status == "error":
                account.status = "active"
                statuses_restored += 1

        await db.commit()

        # 2. Очищаем Redis bad-proxy кеш
        proxy_keys_cleared = clear_all_bad_proxies()

        logger.info(
            f"Reset cooldowns: {cooldowns_cleared} cooldowns cleared, "
            f"{errors_reset} error counts reset, {statuses_restored} statuses restored, "
            f"{proxy_keys_cleared} bad-proxy keys cleared"
        )

        return {
            "success": True,
            "total_accounts": total,
            "cooldowns_cleared": cooldowns_cleared,
            "errors_reset": errors_reset,
            "statuses_restored": statuses_restored,
            "proxy_keys_cleared": proxy_keys_cleared,
        }
    except Exception as e:
        await db.rollback()
        logger.error(f"Error resetting cooldowns: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error resetting cooldowns: {str(e)[:200]}")


@router.get("/search")
@limiter.limit("60/minute")
async def search_accounts(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header)
):
    """
    Поиск аккаунтов по label или другим параметрам.
    """
    try:
        query = select(Account)
        
        if q:
            query = query.filter(Account.label.ilike(f"%{q}%"))
        if status:
            query = query.filter(Account.status == status)
        
        query = query.limit(limit).offset(offset)
        result = await db.execute(query)
        accounts = result.scalars().all()
        
        return [
            {
                "id": acc.id,
                "label": acc.label,
                "status": acc.status,
                "country_code": acc.country_code,
                "proxy": acc.proxy,
            }
            for acc in accounts
        ]
    except Exception as e:
        logger.error(f"Error searching accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error searching accounts: {str(e)[:200]}")
