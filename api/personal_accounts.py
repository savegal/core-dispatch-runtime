"""API для системы "личных аккаунтов" работника.

Концепция:
- У каждого работника может быть свой пул аккаунтов (Account.owner_user_id == user.id).
- Эти аккаунты НИКОГДА не пересекаются с общим пулом (owner_user_id IS NULL) —
  гарантия в селекторах services/account_service.py через жёсткий SQL-фильтр.
- При создании заказа работник может выбрать `account_pool_mode = "personal"`,
  и executor подставит pool_owner_user_id = task.user_id.

Auth model: используем verify_user_header (тот же механизм, что в API users) —
работник видит и управляет ТОЛЬКО своими аккаунтами. Перекрёстный доступ
невозможен.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import ensure_user_scope, verify_user_header
from config.settings import get_settings
from db.database import get_db
from db.models import Account, User

router = APIRouter(prefix="/api/personal-accounts", tags=["personal-accounts"])


# --------- Pydantic models ---------

class PersonalAccountIn(BaseModel):
    """Запись для импорта в личный пул работника.
    Тот же набор, что и AccountRecord в /api/accounts/bulk_import."""
    label: str
    cookies: Optional[str] = None
    cookie_file: Optional[str] = None
    user_agent: Optional[str] = None
    proxy: Optional[str] = None


class PersonalAccountImportRequest(BaseModel):
    user_id: int
    accounts: List[PersonalAccountIn]
    daily_main_limit: Optional[int] = None
    daily_reply_limit: Optional[int] = None


class PersonalAccountImportResponse(BaseModel):
    added: int
    updated: int
    skipped: int
    skipped_reasons: List[str] = Field(default_factory=list)
    total_owned_after: int


class PersonalAccountOut(BaseModel):
    id: int
    label: str
    status: str
    has_proxy: bool
    country_code: Optional[str] = None
    today_main_comments: int = 0
    today_replies: int = 0
    daily_main_limit: Optional[int] = None
    daily_reply_limit: Optional[int] = None
    last_used_at: Optional[datetime] = None
    last_main_comment_at: Optional[datetime] = None
    last_error: Optional[str] = None
    error_count_recent: int = 0
    created_at: Optional[datetime] = None


class PersonalAccountListResponse(BaseModel):
    accounts: List[PersonalAccountOut]
    total: int
    summary: Dict[str, int]


class PersonalAccountLimitsRequest(BaseModel):
    """Запрос на установку лимитов для конкретного аккаунта или для всех сразу."""
    user_id: int
    daily_main_limit: Optional[int] = None
    daily_reply_limit: Optional[int] = None


class PersonalAccountLimitsBulkRequest(BaseModel):
    """Запрос на массовое обновление лимитов всех аккаунтов работника."""
    user_id: int
    daily_main_limit: Optional[int] = None
    daily_reply_limit: Optional[int] = None
    apply_to_existing: bool = True


class PersonalPoolSummary(BaseModel):
    user_id: int
    total: int
    active: int
    error: int
    dead_or_banned: int
    available_main_now: int
    available_reply_now: int
    today_main_comments_total: int
    today_replies_total: int
    default_daily_main_limit: int
    default_daily_reply_limit: int
    max_accounts_per_user: int


# --------- Helpers ---------

def _clamp_main_limit(value: Optional[int]) -> Optional[int]:
    """Ограничиваем лимиты разумными границами."""
    if value is None:
        return None
    s = get_settings()
    return max(0, min(int(value), int(s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT)))


def _clamp_reply_limit(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    s = get_settings()
    return max(0, min(int(value), int(s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT)))


def _account_to_out(account: Account) -> PersonalAccountOut:
    return PersonalAccountOut(
        id=account.id,
        label=account.label,
        status=account.status or "unknown",
        has_proxy=bool(account.proxy),
        country_code=account.country_code,
        today_main_comments=int(account.today_main_comments or 0),
        today_replies=int(account.today_replies or 0),
        daily_main_limit=account.daily_main_limit,
        daily_reply_limit=account.daily_reply_limit,
        last_used_at=account.last_used_at,
        last_main_comment_at=account.last_main_comment_at,
        last_error=(account.last_error[:200] if account.last_error else None),
        error_count_recent=int(account.error_count_recent or 0),
        created_at=account.created_at,
    )


def _has_auth_material(account: Account) -> bool:
    return bool((account.cookies or "").strip() or (account.cookie_file or "").strip())


def _is_personal_account_available_now(
    account: Account,
    *,
    scope: str,
    now: datetime,
) -> bool:
    s = get_settings()
    status = account.status or ""
    if status == "error" and int(account.error_count_recent or 0) >= int(s.ERROR_THRESHOLD):
        return False
    if status not in {"active", "error"}:
        return False
    if status in {"dead", "reauth_needed", "banned"}:
        return False
    if not _has_auth_material(account):
        return False
    if s.USE_PROXY and not (account.proxy or "").strip():
        return False

    if scope == "main":
        main_limit = int(account.daily_main_limit or s.MAX_ACTIONS_PER_ACCOUNT)
        if int(account.today_main_comments or 0) >= main_limit:
            return False
        cooldown_threshold = now - timedelta(seconds=int(s.ACCOUNT_COOLDOWN_SECONDS))
        last_main = account.last_main_comment_at
        if last_main is not None and last_main.tzinfo is None:
            last_main = last_main.replace(tzinfo=timezone.utc)
        if last_main is not None and last_main >= cooldown_threshold:
            return False
        return True

    if scope == "reply":
        reply_limit = int(account.daily_reply_limit or s.MAX_ACTIONS_PER_ACCOUNT)
        return int(account.today_replies or 0) < reply_limit

    return False


async def _ensure_user_exists(db: AsyncSession, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.id == int(user_id)))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _count_owned(db: AsyncSession, user_id: int) -> int:
    res = await db.execute(
        select(sa_func.count(Account.id)).where(Account.owner_user_id == int(user_id))
    )
    return int(res.scalar_one() or 0)


# --------- Endpoints ---------

@router.get("/list", response_model=PersonalAccountListResponse)
async def list_personal_accounts(
    user_id: int = Query(...),
    page: int = Query(0, ge=0),
    page_size: int = Query(20, ge=1, le=200),
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Список личных аккаунтов работника (только свои)."""
    ensure_user_scope(user_id, authenticated_user_id)

    base_filter = Account.owner_user_id == int(user_id)

    total_res = await db.execute(select(sa_func.count(Account.id)).where(base_filter))
    total = int(total_res.scalar_one() or 0)

    summary_rows = await db.execute(
        select(Account.status, sa_func.count(Account.id))
        .where(base_filter)
        .group_by(Account.status)
    )
    summary: Dict[str, int] = {row[0]: int(row[1]) for row in summary_rows.all()}

    res = await db.execute(
        select(Account)
        .where(base_filter)
        .order_by(Account.id.asc())
        .offset(page * page_size)
        .limit(page_size)
    )
    accounts = [_account_to_out(a) for a in res.scalars().all()]

    return PersonalAccountListResponse(accounts=accounts, total=total, summary=summary)


@router.get("/summary", response_model=PersonalPoolSummary)
async def get_personal_pool_summary(
    user_id: int = Query(...),
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Сводка по личному пулу: счётчики, дефолтные лимиты, потолок."""
    ensure_user_scope(user_id, authenticated_user_id)
    s = get_settings()

    base_filter = Account.owner_user_id == int(user_id)
    res = await db.execute(select(Account).where(base_filter))
    accounts = list(res.scalars().all())

    total = len(accounts)
    active = sum(1 for a in accounts if a.status == "active")
    err = sum(1 for a in accounts if a.status == "error")
    dead = sum(1 for a in accounts if a.status in {"dead", "reauth_needed", "banned"})
    main_today = sum(int(a.today_main_comments or 0) for a in accounts)
    reply_today = sum(int(a.today_replies or 0) for a in accounts)
    now = datetime.now(timezone.utc)
    available_main_now = sum(
        1 for a in accounts
        if _is_personal_account_available_now(a, scope="main", now=now)
    )
    available_reply_now = sum(
        1 for a in accounts
        if _is_personal_account_available_now(a, scope="reply", now=now)
    )

    return PersonalPoolSummary(
        user_id=int(user_id),
        total=total,
        active=active,
        error=err,
        dead_or_banned=dead,
        available_main_now=available_main_now,
        available_reply_now=available_reply_now,
        today_main_comments_total=main_today,
        today_replies_total=reply_today,
        default_daily_main_limit=int(s.PERSONAL_POOL_DEFAULT_DAILY_MAIN_LIMIT),
        default_daily_reply_limit=int(s.PERSONAL_POOL_DEFAULT_DAILY_REPLY_LIMIT),
        max_accounts_per_user=int(s.PERSONAL_POOL_MAX_ACCOUNTS_PER_USER),
    )


@router.post("/import", response_model=PersonalAccountImportResponse)
async def import_personal_accounts(
    request: PersonalAccountImportRequest,
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Импорт аккаунтов в личный пул работника.

    Поведение:
    - label уникален глобально (как в общем пуле). Если такой label уже
      существует и принадлежит другому пользователю/общему пулу — пропускаем
      с reason="label_owned_by_another".
    - Если label принадлежит этому же работнику — обновляем cookies/proxy.
    - Поверх лимитов запроса/дефолта проставляются ограничители из settings.
    - Жёсткий потолок PERSONAL_POOL_MAX_ACCOUNTS_PER_USER защищает от случайного
      «слива» 1000 строк сразу.
    """
    s = get_settings()
    user_id = int(request.user_id)
    ensure_user_scope(user_id, authenticated_user_id)
    await _ensure_user_exists(db, user_id)

    main_limit = _clamp_main_limit(
        request.daily_main_limit
        if request.daily_main_limit is not None
        else s.PERSONAL_POOL_DEFAULT_DAILY_MAIN_LIMIT
    )
    reply_limit = _clamp_reply_limit(
        request.daily_reply_limit
        if request.daily_reply_limit is not None
        else s.PERSONAL_POOL_DEFAULT_DAILY_REPLY_LIMIT
    )

    owned_count = await _count_owned(db, user_id)
    capacity = max(0, int(s.PERSONAL_POOL_MAX_ACCOUNTS_PER_USER) - owned_count)

    added = 0
    updated = 0
    skipped = 0
    skipped_reasons: List[str] = []

    for acc in request.accounts:
        try:
            label = (acc.label or "").strip()
            if not label:
                skipped += 1
                skipped_reasons.append("empty_label")
                continue
            if not (acc.cookies or acc.cookie_file):
                skipped += 1
                skipped_reasons.append(f"{label}: no_cookies")
                continue

            existing = (
                await db.execute(select(Account).where(Account.label == label))
            ).scalar_one_or_none()

            if existing is not None:
                # Чужой/общий аккаунт — не трогаем.
                if existing.owner_user_id != user_id:
                    skipped += 1
                    skipped_reasons.append(f"{label}: label_owned_by_another")
                    continue
                # Обновляем cookies/прокси у нашего же аккаунта.
                if acc.cookies:
                    existing.cookies = acc.cookies
                if acc.cookie_file:
                    existing.cookie_file = acc.cookie_file
                if acc.user_agent:
                    existing.user_agent = acc.user_agent
                if acc.proxy:
                    existing.proxy = acc.proxy
                # Подмешиваем актуальные лимиты только если работник прислал явные.
                if request.daily_main_limit is not None:
                    existing.daily_main_limit = main_limit
                if request.daily_reply_limit is not None:
                    existing.daily_reply_limit = reply_limit
                # Снимаем «error» — операция импорта = «принял к сведению».
                if existing.status not in {"dead", "banned", "reauth_needed"}:
                    existing.status = "active"
                    existing.error_count_recent = 0
                    existing.last_error = None
                updated += 1
                continue

            if capacity <= 0:
                skipped += 1
                skipped_reasons.append(f"{label}: pool_capacity_exceeded")
                continue

            user_agent = acc.user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            new_account = Account(
                label=label,
                cookies=acc.cookies,
                cookie_file=acc.cookie_file,
                user_agent=user_agent,
                proxy=acc.proxy,
                status="active",
                error_count_recent=0,
                owner_user_id=user_id,
                daily_main_limit=main_limit,
                daily_reply_limit=reply_limit,
            )
            # Гео-детекция: повторно используем тот же сервис, что в админском импорте.
            try:
                from services.geo_detection_service import detect_account_country
                detected = detect_account_country(new_account)
                if detected:
                    new_account.country_code = detected
            except Exception:
                # Гео — best effort. Падать импорт не должен.
                pass

            db.add(new_account)
            added += 1
            capacity -= 1
        except Exception as exc:
            skipped += 1
            skipped_reasons.append(f"{acc.label}: {str(exc)[:100]}")

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to commit personal pool import: {exc}")

    total_after = await _count_owned(db, user_id)

    return PersonalAccountImportResponse(
        added=added,
        updated=updated,
        skipped=skipped,
        skipped_reasons=skipped_reasons[:30],
        total_owned_after=total_after,
    )


@router.delete("/{account_id}")
async def delete_personal_account(
    account_id: int = Path(..., ge=1),
    user_id: int = Query(...),
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Удаление личного аккаунта (только свой)."""
    ensure_user_scope(user_id, authenticated_user_id)

    account = (
        await db.execute(select(Account).where(Account.id == int(account_id)))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.owner_user_id != int(user_id):
        # Не раскрываем факт существования чужого аккаунта.
        raise HTTPException(status_code=404, detail="Account not found")

    await db.delete(account)
    await db.commit()
    return {"ok": True, "deleted_id": int(account_id)}


@router.delete("/all/clear")
async def clear_personal_pool(
    user_id: int = Query(...),
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Удалить ВСЕ личные аккаунты работника. Защита: только свои."""
    ensure_user_scope(user_id, authenticated_user_id)

    res = await db.execute(select(Account).where(Account.owner_user_id == int(user_id)))
    items = list(res.scalars().all())
    for a in items:
        await db.delete(a)
    await db.commit()
    return {"ok": True, "deleted": len(items)}


@router.patch("/{account_id}/limits", response_model=PersonalAccountOut)
async def patch_personal_account_limits(
    account_id: int,
    request: PersonalAccountLimitsRequest,
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Обновить per-account лимиты конкретного аккаунта.
    None в поле = снять override и использовать глобальный лимит."""
    ensure_user_scope(int(request.user_id), authenticated_user_id)

    account = (
        await db.execute(select(Account).where(Account.id == int(account_id)))
    ).scalar_one_or_none()
    if not account or account.owner_user_id != int(request.user_id):
        raise HTTPException(status_code=404, detail="Account not found")

    if request.daily_main_limit is not None:
        account.daily_main_limit = _clamp_main_limit(request.daily_main_limit)
    if request.daily_reply_limit is not None:
        account.daily_reply_limit = _clamp_reply_limit(request.daily_reply_limit)

    await db.commit()
    await db.refresh(account)
    return _account_to_out(account)


@router.patch("/limits/bulk")
async def patch_personal_pool_limits_bulk(
    request: PersonalAccountLimitsBulkRequest,
    authenticated_user_id: Annotated[int, Depends(verify_user_header)] = 0,
    db: AsyncSession = Depends(get_db),
):
    """Массово обновить лимиты на все аккаунты работника.
    Используется для UX «изменить лимит на весь пул сразу»."""
    ensure_user_scope(int(request.user_id), authenticated_user_id)

    main_limit = _clamp_main_limit(request.daily_main_limit)
    reply_limit = _clamp_reply_limit(request.daily_reply_limit)

    if main_limit is None and reply_limit is None:
        return {"ok": True, "updated": 0}

    if not request.apply_to_existing:
        # Только дефолты на будущее — у нас нет per-user storage,
        # поэтому просто игнорируем (для UX совместимости).
        return {"ok": True, "updated": 0}

    res = await db.execute(
        select(Account).where(Account.owner_user_id == int(request.user_id))
    )
    items = list(res.scalars().all())
    updated = 0
    for a in items:
        changed = False
        if main_limit is not None and a.daily_main_limit != main_limit:
            a.daily_main_limit = main_limit
            changed = True
        if reply_limit is not None and a.daily_reply_limit != reply_limit:
            a.daily_reply_limit = reply_limit
            changed = True
        if changed:
            updated += 1
    await db.commit()
    return {"ok": True, "updated": updated, "total": len(items)}
