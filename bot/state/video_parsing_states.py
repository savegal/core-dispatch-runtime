"""
Состояния FSM для раздела Парсинг видео.
"""
from aiogram.fsm.state import State, StatesGroup


class VideoParsingStates(StatesGroup):
    """Состояния для процесса добавления блогера."""
    SUGGESTIONS = State()
    INPUT_USERNAME = State()
    SEARCH_CREATORS = State()
    INPUT_INTERVAL = State()
    INPUT_MODE = State()
    DELETE_CONFIRM = State()
