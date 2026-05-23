"""
Централизованные CSS-селекторы для TikTok.

Все селекторы вынесены в один файл для удобства поддержки и обновления.
"""
from dataclasses import dataclass
from typing import List


@dataclass
class TikTokSelectors:
    """Класс с селекторами TikTok элементов."""
    
    # Комментарии
    COMMENT_INPUT: str = '[data-e2e="comment-input"], textarea[placeholder*="comment" i], textarea[placeholder*="комментарий" i]'
    COMMENT_INPUT_ALTERNATIVE: str = 'textarea[data-e2e="comment-input"]'
    # УЛУЧШЕНО: Добавлены дополнительные селекторы для поля ввода комментария
    COMMENT_INPUT_CONTENTEDITABLE: str = 'div[contenteditable="true"][data-e2e*="comment"], div[contenteditable="true"][placeholder*="comment" i], div[contenteditable="true"][placeholder*="комментарий" i]'
    COMMENT_INPUT_TEXTAREA: str = 'textarea[placeholder*="Add comment" i], textarea[placeholder*="Добавить комментарий" i], textarea[placeholder*="Write a comment" i], textarea[placeholder*="Написать комментарий" i]'
    COMMENT_INPUT_DIV: str = 'div[data-e2e*="comment-input"], div[class*="CommentInput"], div[class*="comment-input"], div[class*="CommentTextarea"]'
    COMMENT_SECTION: str = '[data-e2e="comment-list"], [data-e2e="comment-container"], [class*="CommentList"], [class*="CommentContainer"], [class*="comment-section"]'
    # ИСПРАВЛЕНО: Добавлены дополнительные селекторы для TikTok 2026
    COMMENT_SECTION_EXTENDED: str = '[data-e2e*="comment"], [class*="Comment"], [id*="comment" i], [aria-label*="comment" i]'
    VIEW_COMMENTS_BUTTON: str = 'button[data-e2e*="view-comments"], button:has-text("View comments"), button:has-text("Показать комментарии"), [data-e2e*="comment-button"]'
    # ИСПРАВЛЕНО (Jan 2026): TikTok теперь использует div[data-e2e="comment-post"] вместо button
    POST_COMMENT_BUTTON: str = 'div[data-e2e="comment-post"][role="button"], button[data-e2e="post-comment-button"], button[data-e2e="post-button"], button[data-e2e="comment-post-button"]'
    POST_BUTTON: str = 'div[data-e2e="comment-post"], button:has-text("Post"), button:has-text("Опубликовать"), button:has-text("Отправить"), button[type="submit"]:has-text("Post")'
    # ОБНОВЛЕНО: Реальная структура DOM (Jan 2026)
    # Корневой контейнер комментария
    COMMENT_ROOT: str = 'div[class*="DivCommentObject"]'
    # Текст комментария (внутри корневого контейнера)
    COMMENT_TEXT_SPAN: str = 'span[data-e2e="comment-level-1"]'
    # Старые селекторы для обратной совместимости
    COMMENT_ITEM: str = 'div[class*="DivCommentObject"], [data-e2e="comment-item"], [data-e2e="comment-level-1"], [class*="CommentItem"]'
    COMMENT_LIST: str = '[data-e2e="comment-list"], [class*="CommentList"]'
    
    # Авторизация
    LOGIN_BUTTON: str = 'button[data-e2e="login-button"]'
    LOGIN_LINK: str = 'a[href*="/login"], a[href*="/signup"]'
    SIGNUP_BUTTON: str = 'button[data-e2e="signup-button"]'
    PASSWORD_INPUT: str = 'input[type="password"]'
    LOGIN_CONTAINER: str = 'div[class*="LoginContainer"]'
    
    # Профиль пользователя
    PROFILE_ICON: str = '[data-e2e="profile-icon"]'
    USER_AVATAR: str = '[data-e2e="user-avatar"]'
    USER_AVATAR_CLASS: str = '[class*="Avatar"][class*="User"], [class*="UserAvatar"]'
    
    # Видео
    VIDEO_PLAYER: str = 'video, [data-e2e*="video"], [class*="video"], [data-e2e*="Video"], [class*="VideoPlayer"]'
    
    # Оверлеи и попапы
    ACCEPT_BUTTON: str = 'button[data-e2e="accept-button"], button[aria-label*="accept" i]'
    ACCEPT_BUTTON_TEXT: str = 'button:has-text("Accept"), button:has-text("Принять"), button:has-text("I agree"), button:has-text("Согласен")'
    CONTINUE_WATCHING: str = 'button:has-text("Continue watching"), button:has-text("Продолжить просмотр"), button:has-text("Watch more"), button:has-text("Смотреть дальше")'
    CONTINUE_WATCHING_DATA: str = '[data-e2e*="continue"], [data-e2e*="watch-more"]'
    AGE_RESTRICTION: str = 'button:has-text("I am 18"), button:has-text("Мне 18"), button:has-text("Confirm"), button:has-text("Подтвердить")'
    AGE_RESTRICTION_DATA: str = '[data-e2e*="age"], [data-e2e*="confirm"]'
    MODAL_OVERLAY: str = '.TUXModal-overlay, [class*="Modal-overlay"], [class*="modal-overlay"], [class*="Overlay"], [class*="overlay"]'
    MODAL_CLOSE: str = '[data-e2e*="modal"], [data-e2e*="close"]'
    MODAL_CLOSE_BUTTON: str = 'button[aria-label*="close" i], button[aria-label*="закрыть" i], [class*="Close"], [class*="close"]'
    NOTIFICATION: str = '[class*="Notification"], [class*="notification"], [class*="Banner"], [class*="banner"]'
    NOTIFICATION_DATA: str = '[data-e2e*="notification"], [data-e2e*="banner"]'
    
    # Ответы на комментарии
    # ИСПРАВЛЕНО: Реальные селекторы из TikTok
    REPLY_BUTTON: str = 'p[data-e2e="comment-reply-1"]'  # Основной селектор
    REPLY_BUTTON_FALLBACK: str = 'button:has-text("Ответить"), button:has-text("Reply")'  # Fallback
    REPLY_INPUT: str = 'div[data-e2e="comment-input"]'  # Поле ввода ответа (может быть то же, что и для основного)
    REPLY_POST_BUTTON: str = 'div[data-e2e="comment-post"][role="button"]'  # Кнопка отправки ответа
    REPLY_POST_BUTTON_FALLBACK: str = 'div[data-e2e="comment-post"]'  # Fallback без role
    
    # Лайки
    # ОБНОВЛЕНО: Реальная структура DOM (Jan 2026)
    # Кнопка лайка на комментарии (внутри корневого контейнера комментария)
    LIKE_BUTTON: str = 'div[role="button"][class*="DivLikeContainer"]'
    LIKE_BUTTON_FALLBACK: str = 'div[role="button"][aria-label*="лайк" i]'
    # Старые селекторы удалены - они не работают в текущей версии TikTok
    
    # Поиск комментариев по автору
    USERNAME_SELECTOR: str = '[class*="username"], [class*="author"], [data-e2e*="username"]'
    
    @classmethod
    def get_comment_input_selectors(cls) -> List[str]:
        """УЛУЧШЕНО: Возвращает расширенный список селекторов для поля ввода комментария.
        
        ИСПРАВЛЕНО (Jan 2026): contenteditable приоритетный - TikTok перешёл на div[contenteditable="true"]
        """
        return [
            # ПРИОРИТЕТ 1: contenteditable (самый актуальный для TikTok 2026)
            'div[contenteditable="true"]',
            cls.COMMENT_INPUT_CONTENTEDITABLE,
            'div[contenteditable="true"][data-e2e*="comment"]',
            'div[contenteditable="true"][data-e2e*="input"]',
            'div[contenteditable="true"][placeholder*="comment" i]',
            'div[contenteditable="true"][placeholder*="комментарий" i]',
            # ПРИОРИТЕТ 2: data-e2e атрибуты (стандартные)
            cls.COMMENT_INPUT,
            cls.COMMENT_INPUT_ALTERNATIVE,
            # ПРИОРИТЕТ 3: textarea (старый формат)
            cls.COMMENT_INPUT_TEXTAREA,
            'textarea',
            # ПРИОРИТЕТ 4: классы и другие селекторы
            cls.COMMENT_INPUT_DIV,
            'div[data-e2e*="input"]',
            'div[role="textbox"]',
            '[class*="Input"][class*="Comment"]',
            '[class*="Textarea"][class*="Comment"]',
        ]
    
    @classmethod
    def get_login_indicators(cls) -> List[str]:
        """Возвращает список селекторов, указывающих на страницу логина."""
        return [
            cls.LOGIN_BUTTON,
            cls.LOGIN_LINK,
            cls.SIGNUP_BUTTON,
            cls.PASSWORD_INPUT,
            cls.LOGIN_CONTAINER,
        ]
    
    @classmethod
    def get_user_indicators(cls) -> List[str]:
        """Возвращает список селекторов, указывающих на авторизованного пользователя."""
        return [
            cls.PROFILE_ICON,
            cls.USER_AVATAR,
            cls.USER_AVATAR_CLASS,
        ]
    
    @classmethod
    def get_overlay_selectors(cls) -> List[str]:
        """Возвращает список селекторов для оверлеев и попапов."""
        return [
            cls.ACCEPT_BUTTON,
            cls.ACCEPT_BUTTON_TEXT,
            cls.CONTINUE_WATCHING,
            cls.CONTINUE_WATCHING_DATA,
            cls.AGE_RESTRICTION,
            cls.AGE_RESTRICTION_DATA,
            cls.MODAL_OVERLAY,
            cls.MODAL_CLOSE,
            cls.NOTIFICATION,
            cls.NOTIFICATION_DATA,
        ]


# Экспортируем экземпляр по умолчанию
selectors = TikTokSelectors()

