from aiogram.fsm.state import State, StatesGroup


class EngagementStates(StatesGroup):
    """
    Состояния FSM для создания engagement_only заказа.
    
    Flow:
    1. INPUT_VIDEO_URL - ввод URL видео
    2. INPUT_COMMENT_TEXT - ввод точного текста комментария
    3. CHOOSE_REPLY_SOURCE - ручной ввод ответов или шаблон
    4. CHOOSE_TEMPLATE - выбор шаблона для ответов
    5. INPUT_REPLIES - ввод replies (1-20 текстов)
    6. CONFIRM_ORDER - превью и запуск
    """
    
    # Ввод URL видео
    INPUT_VIDEO_URL = State()
    
    # Ввод части текста комментария
    INPUT_COMMENT_TEXT = State()

    # Выбор источника ответов
    CHOOSE_REPLY_SOURCE = State()

    # Выбор шаблона с ответами
    CHOOSE_TEMPLATE = State()
    
    # Ввод replies
    INPUT_REPLIES = State()
    INPUT_REPLY1 = State()
    INPUT_REPLY2 = State()
    INPUT_REPLY3 = State()
    
    # Подтверждение заказа
    CONFIRM_ORDER = State()
