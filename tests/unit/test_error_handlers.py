from bot.services.error_handlers import get_error_message, get_task_creation_error_message


def test_task_creation_error_message_includes_required_and_available_balance():
    text, _ = get_task_creation_error_message(
        Exception("Insufficient balance. Required: 21.00 грн, Available: 7.00 грн")
    )

    assert "21.00 грн" in text
    assert "7.00 грн" in text
    assert "числу ссылок" in text


def test_generic_insufficient_accounts_is_not_treated_as_balance_problem():
    text, _ = get_error_message(Exception("Insufficient accounts for task"), "создании заказа")

    assert "Недостаточно ресурсов" in text
    assert "Недостаточно средств" not in text
