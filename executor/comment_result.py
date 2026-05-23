"""
Типизированный результат операции комментирования.

ИСПРАВЛЕНО: Добавлены поля для диагностики видимости комментариев и теневого бана
(рекомендации ChatGPT).
"""
from typing import Optional, Literal
from dataclasses import dataclass


@dataclass
class CommentResult:
    """
    Типизированный результат операции комментирования.
    
    ИСПРАВЛЕНО: Расширен полями для диагностики видимости и теневого бана.
    
    Attributes:
        success: True если комментарий успешно отправлен
        comment_id: ID комментария (может быть pseudo-id)
        error_type: Тип ошибки, если success=False
        fatal_for_account: True если ошибка критична для аккаунта (бан, мертвые куки)
        fatal_for_proxy: True если ошибка критична для прокси (недоступен, таймаут)
        details: Дополнительная информация об ошибке
        screenshot_path: Путь к скриншоту (если был сделан)
        html_path: Путь к сохраненному HTML (если был сохранен)
        
        # НОВЫЕ ПОЛЯ (рекомендации ChatGPT):
        clicked_submit: True если кнопка отправки была нажата
        network_confirmed: True если получен подтверждающий ответ от сервера
        local_visible: True если комментарий виден автору (локальная проверка)
        public_visible: True если комментарий виден другому аккаунту (публичная проверка)
        moderation_suspected: True если подозревается модерация/фильтрация
        post_response_status: HTTP статус ответа сервера (если удалось поймать)
        ui_error_toast: Текст ошибки из UI (тост/alert, если был)
        visibility_checked: Проводилась ли локальная проверка видимости
        verification_attempts: Количество попыток проверки видимости
        shadowban_suspected: Подозрение на теневой бан (автор видит, другие нет)
    """
    success: bool
    comment_id: Optional[str] = None
    error_type: Optional[Literal[
        "ok",
        "navigation_error",
        "comment_button_not_found",
        "comment_input_not_found",
        "comment_disabled",
        "reply_button_not_found",
        "reply_input_not_found",
        "reply_parent_not_found",
        "login_required",
        "captcha",
        "account_banned",
        "layout_changed",
        "rate_limit",
        "video_unavailable",
        "network_error",
        "comment_backend_error",
        "visibility_timeout",
        "possible_shadowban",
        "unknown"
    ]] = None
    fatal_for_account: bool = False
    fatal_for_proxy: bool = False
    details: Optional[str] = None
    screenshot_path: Optional[str] = None
    html_path: Optional[str] = None
    
    # НОВЫЕ ПОЛЯ для диагностики видимости
    clicked_submit: bool = False
    network_confirmed: bool = False
    local_visible: Optional[bool] = None
    public_visible: Optional[bool] = None
    moderation_suspected: bool = False
    post_response_status: Optional[int] = None
    ui_error_toast: Optional[str] = None
    visibility_checked: bool = False
    verification_attempts: Optional[int] = None
    shadowban_suspected: bool = False
    parent_confirmation_level: Optional[Literal[
        "network_exact",
        "network_reply_to_reply",
        "network_root_match",
        "ui_author_only",
        "unconfirmed",
        "not_applicable",
    ]] = None
    visibility_status: Optional[Literal[
        "confirmed_local",
        "submitted_unconfirmed",
        "not_checked",
    ]] = None
    
    @classmethod
    def ok(cls, comment_id: str) -> "CommentResult":
        """Создает успешный результат (обратная совместимость)."""
        return cls(
            success=True,
            comment_id=comment_id,
            error_type="ok"
        )
    
    @classmethod
    def ok_with_visibility(
        cls,
        comment_id: str,
        visible_for_author: Optional[bool],
        verification_attempts: Optional[int],
        shadowban_suspected: bool,
        network_confirmed: bool = False,
        post_response_status: Optional[int] = None,
        parent_confirmation_level: Optional[Literal[
            "network_exact",
            "network_reply_to_reply",
            "network_root_match",
            "ui_author_only",
            "unconfirmed",
            "not_applicable",
        ]] = None,
        visibility_status: Optional[Literal[
            "confirmed_local",
            "submitted_unconfirmed",
            "not_checked",
        ]] = None,
    ) -> "CommentResult":
        """
        Создает успешный результат с информацией о видимости комментария.
        
        ИСПРАВЛЕНО: Добавлен согласно рекомендациям ChatGPT для диагностики теневого бана.
        """
        if not visibility_status:
            if visible_for_author is True:
                visibility_status = "confirmed_local"
            elif network_confirmed:
                visibility_status = "submitted_unconfirmed"
            else:
                visibility_status = "not_checked"

        return cls(
            success=True,
            comment_id=comment_id,
            error_type="ok",
            visibility_checked=visible_for_author is not None,
            local_visible=visible_for_author,
            verification_attempts=verification_attempts,
            shadowban_suspected=shadowban_suspected,
            network_confirmed=network_confirmed,
            post_response_status=post_response_status,
            clicked_submit=True,
            parent_confirmation_level=parent_confirmation_level,
            visibility_status=visibility_status,
        )
    
    @classmethod
    def error(
        cls,
        error_type: str,
        details: Optional[str] = None,
        fatal_for_account: bool = False,
        fatal_for_proxy: bool = False,
        screenshot_path: Optional[str] = None,
        html_path: Optional[str] = None,
        ui_error_toast: Optional[str] = None,
        post_response_status: Optional[int] = None
    ) -> "CommentResult":
        """Создает результат с ошибкой."""
        return cls(
            success=False,
            error_type=error_type,
            details=details,
            fatal_for_account=fatal_for_account,
            fatal_for_proxy=fatal_for_proxy,
            screenshot_path=screenshot_path,
            html_path=html_path,
            ui_error_toast=ui_error_toast,
            post_response_status=post_response_status
        )
    
    def get_outcome_classification(self) -> str:
        """
        Классифицирует результат комментирования по рекомендациям ChatGPT.
        
        Returns:
            str: Один из типов результата:
                - SUCCESS_PUBLIC: виден наблюдателю
                - SUCCESS_LOCAL_ONLY: виден только автору (фильтрация/модерация)
                - SUBMITTED_UNCONFIRMED: сетевой успех есть, но не найден (задержка)
                - FAILED_TO_POST: сетевой отказ/тост/ошибка
                - FAILED_UI_TOAST: ошибка в UI (тост)
                - RATE_LIMIT/CAPTCHA/LOGIN_REQUIRED: отдельные ветки
                - UNKNOWN: неизвестный исход
        """
        if not self.success:
            if self.ui_error_toast:
                return "FAILED_UI_TOAST"
            if self.error_type in ("rate_limit", "captcha", "login_required"):
                return self.error_type.upper()
            if self.error_type == "comment_backend_error":
                return "FAILED_TO_POST"
            return "FAILED_UNKNOWN"
        
        # success = True
        if self.public_visible:
            return "SUCCESS_PUBLIC"
        
        if self.local_visible and not self.public_visible:
            return "SUCCESS_LOCAL_ONLY"  # фильтрация/модерация
        
        if self.network_confirmed and not self.local_visible:
            return "SUBMITTED_UNCONFIRMED"  # задержка/ленивая загрузка
        
        if self.local_visible is None and self.network_confirmed:
            return "SUBMITTED_UNCONFIRMED"  # проверка не проводилась, но сеть подтвердила
        
        if self.shadowban_suspected:
            return "SUCCESS_LOCAL_ONLY"  # подозрение на теневой бан
        
        # Если есть comment_id, но нет проверки - считаем условно успешным
        if self.comment_id:
            return "SUBMITTED_UNCONFIRMED"
        
        return "UNKNOWN"
