from aiogram.fsm.state import State, StatesGroup


class OrderStates(StatesGroup):
    """
    Состояния FSM для создания заказа.
    
    Flow:
    1. INPUT_LINKS - ввод ссылок
    2. INPUT_MAIN_COMMENT - ввод основного комментария
    3. NEW_TEMPLATE_REPLY_COUNT - выбор количества ответов (0-20)
    4. INPUT_REPLIES_ALL - ввод ответов поочерёдно
    5. INPUT_TEMPLATE_NAME - сохранение как шаблон (опционально)
    6. CONFIRM_ORDER - превью и запуск
    """
    
    # Ввод ссылок на видео
    INPUT_LINKS = State()
    
    # Выбор существующего шаблона
    CHOOSE_TEMPLATE = State()
    
    # Ввод трёх вариантов основного комментария
    INPUT_MAIN_COMMENT = State()  # вариант 1
    INPUT_MAIN_COMMENT_VARIANT_2 = State()
    INPUT_MAIN_COMMENT_VARIANT_3 = State()
    
    # Выбор количества ответов (кнопки 0-20)
    NEW_TEMPLATE_REPLY_COUNT = State()
    
    # Ввод ответов поочерёдно
    INPUT_REPLIES_ALL = State()
    INPUT_REPLY1 = State()
    INPUT_REPLY2 = State()
    INPUT_REPLY3 = State()
    NEW_TEMPLATE_REPLY_4 = State()
    NEW_TEMPLATE_REPLY_5 = State()
    NEW_TEMPLATE_REPLY_6 = State()
    
    # Сохранение шаблона
    INPUT_TEMPLATE_NAME = State()
    
    # Подтверждение заказа
    CONFIRM_ORDER = State()
    
    # === Устаревшие (для обратной совместимости) ===
    CHOOSE_REPLY_MODE = State()  # Убран из flow, режим определяется автоматически
    CONFIRM_DELAYS = State()     # Убран, задержки включены по умолчанию
    
    # === Создание нового шаблона (quick_order) ===
    NEW_TEMPLATE_MAIN_COMMENT = State()
    NEW_TEMPLATE_REPLIES_ALL = State()  # УПРОЩЕНО: Ввод всех ответов сразу
    NEW_TEMPLATE_REPLY_1 = State()  # Устарело, оставлено для совместимости
    NEW_TEMPLATE_REPLY_2 = State()  # Устарело, оставлено для совместимости
    NEW_TEMPLATE_REPLY_3 = State()  # Устарело, оставлено для совместимости
    
    # === Редактирование шаблона ===
    EDIT_TEMPLATE_COMMENT = State()
    EDIT_TEMPLATE_REPLY1 = State()
    EDIT_TEMPLATE_REPLY2 = State()
    EDIT_TEMPLATE_REPLY3 = State()
    EDIT_TEMPLATE_REPLIES_ALL = State()
    QUICK_TEMPLATE_EDIT_COMMENT = State()
    QUICK_TEMPLATE_EDIT_REPLIES = State()
    EDIT_TEMPLATE_NAME = State()
    EDIT_TEMPLATE_MAIN_COMMENT = State()
    EDIT_TEMPLATE_LIKES = State()
    
    # === Админские состояния ===
    INPUT_ACCOUNTS_FILE = State()
    INPUT_PROXIES_FILE = State()
    ADMIN_SET_VIDEO_LIMIT = State()
    ADMIN_ADD_BALANCE = State()
    ADMIN_SUBTRACT_BALANCE = State()
    ADMIN_EDIT_PRICING = State()
    ADMIN_EDIT_BOOST_LIKES = State()
    SEARCH_ACCOUNTS = State()
    ADMIN_REAUTH_ACCOUNT = State()
    ADMIN_USER_SEARCH = State()

    # === Замена текстов для существующего заказа ===
    REPLACE_TEXTS = State()

    # === Адаптивный мониторинг (только для админа) ===
    ADMIN_ADAPTIVE_EDIT_INT   = State()   # ввод целочисленного параметра
    ADMIN_ADAPTIVE_EDIT_FLOAT = State()   # ввод float-параметра
    ADMIN_ADAPTIVE_EDIT_BOOL  = State()   # ввод булевого параметра (да/нет)
    ADMIN_BLOGGER_OVERRIDE    = State()   # ввод override_min/max для конкретного блогера
    ADMIN_BLOGGER_RENAME      = State()   # ввод нового username для мониторимого блогера
    ADMIN_BLOGGER_SEARCH      = State()   # ввод username для поиска блогера в адаптивном списке
    ADMIN_WORKER_BLOGGER_SEARCH = State() # поиск блогера в карточке конкретного работника
