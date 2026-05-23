# Тесты

Этот каталог содержит тесты для проекта.

## Структура

- `unit/` - Unit тесты (быстрые, без внешних зависимостей)
- `integration/` - Integration тесты (могут требовать БД, Redis и т.д.)
- `conftest.py` - Общие fixtures для pytest

## Запуск тестов

### Все тесты
```bash
pytest
```

### Только unit тесты
```bash
pytest -m unit
```

### Только integration тесты
```bash
pytest -m integration
```

### С покрытием кода
```bash
pytest --cov=. --cov-report=html
```

### Конкретный тест
```bash
pytest tests/unit/test_balance_service.py::TestBalanceService::test_calculate_task_cost_simple
```

## Настройка

Тесты используют тестовую БД (`test.db`). При необходимости можно настроить через переменные окружения:

```bash
export TEST_DB_URL="sqlite:///./test.db"
export TEST_DB_URL_ASYNC="sqlite+aiosqlite:///./test.db"
```

## Написание тестов

### Unit тесты

Unit тесты должны быть быстрыми и не требовать внешних зависимостей (БД, Redis, сеть).

```python
@pytest.mark.unit
def test_calculate_cost():
    # Тест логики без БД
    cost = calculate_cost(main_comments=1, replies=2)
    assert cost == 2.00
```

### Integration тесты

Integration тесты могут использовать fixtures для БД и других внешних зависимостей.

```python
@pytest.mark.integration
def test_create_account(db_session):
    account = Account(label="test")
    db_session.add(account)
    db_session.commit()
    assert account.id is not None
```

## Fixtures

Общие fixtures доступны в `conftest.py`:
- `db_session` - Sync БД сессия
- `async_db_session` - Async БД сессия
- `sample_user` - Тестовый пользователь
- `sample_account` - Тестовый аккаунт
- `sample_proxy` - Тестовый прокси
- `sample_task` - Тестовая задача
- `pricing_config` - Конфигурация цен

## Маркеры

- `@pytest.mark.unit` - Unit тест
- `@pytest.mark.integration` - Integration тест
- `@pytest.mark.slow` - Медленный тест (браузер, сеть)
- `@pytest.mark.admin` - Требует админских прав




