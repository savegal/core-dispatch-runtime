from aiogram.fsm.state import State, StatesGroup


class ComposerStates(StatesGroup):
    """
    ИСПРАВЛЕНО: Отдельные FSM состояния для композера заказов.
    
    Это предотвращает конфликт с OrderStates, используемыми в order_wizard.
    Теперь композер и wizard не мешают друг другу.
    
    Flow:
    1. INPUT_LINKS - ввод ссылок
    2. INPUT_MAIN_COMMENT - ввод основного комментария
    3. INPUT_TEMPLATE_NAME - сохранение как шаблон (опционально)
    """
    
    # Ввод ссылок на видео
    INPUT_LINKS = State()
    
    # Ввод основного комментария
    INPUT_MAIN_COMMENT = State()
    
    # Сохранение шаблона
    INPUT_TEMPLATE_NAME = State()


