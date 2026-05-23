from aiogram.fsm.state import State, StatesGroup


class TemplateStates(StatesGroup):
    """Состояния для работы с шаблонами."""
    CHOOSING_TEMPLATE = State()  # Выбор шаблона
    CREATING_TEMPLATE_MAIN = State()  # Ввод основного комментария для нового шаблона
    CREATING_TEMPLATE_REPLIES = State()  # Ввод ответов для нового шаблона
    EDITING_TEMPLATE_MAIN = State()  # Редактирование основного комментария
    EDITING_TEMPLATE_REPLIES = State()  # Редактирование ответов
    CONFIRMING_TEMPLATE = State()  # Подтверждение и запуск задачи

