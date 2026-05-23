import inspect
from pathlib import Path

import pytest

from bot.handlers import order_wizard, orders, quick_order, templates


@pytest.mark.unit
def test_order_wizard_template_selection_preserves_text_variants():
    source = inspect.getsource(order_wizard.wizard_select_template)

    assert "template_text_variants(template)" in source
    assert "draft.text_variants = text_variants" in source
    assert "text_variants=text_variants" in source
    assert "[draft.comment] * 3" not in source


@pytest.mark.unit
def test_order_wizard_uses_per_message_comment_and_reply_flow():
    main_source = inspect.getsource(order_wizard.wizard_process_main_comment)
    reply_source = inspect.getsource(order_wizard.wizard_select_reply_count)
    reply_input_source = inspect.getsource(order_wizard.process_reply_input)

    assert "_process_main_comment_variant(message, state, 1)" in main_source
    assert "INPUT_MAIN_COMMENT_VARIANT_2" in inspect.getsource(order_wizard.wizard_process_main_comment_variant2)
    assert "INPUT_MAIN_COMMENT_VARIANT_3" in inspect.getsource(order_wizard.wizard_process_main_comment_variant3)
    assert "OrderStates.NEW_TEMPLATE_REPLIES_ALL" not in main_source
    assert "wizard:reply_count:" in reply_source
    assert "_reply_state_for_index(1)" in reply_source
    assert "normalize_user_text(raw_text)" in reply_input_source
    assert "preserve_nonempty_lines(raw_text)" in reply_input_source
    assert "settings.MAX_REPLY_LENGTH" in reply_input_source


@pytest.mark.unit
def test_quick_order_confirm_uses_template_reply_count_not_hardcoded_three():
    source = inspect.getsource(quick_order.select_template)

    assert "default_reply_count" in source
    assert "quicktpl:confirm:{template_id}:3" not in source
    assert "quicktpl:confirm:{template_id}:{default_reply_count}" in source


@pytest.mark.unit
def test_quick_order_new_template_replies_are_collected_per_message():
    source = inspect.getsource(quick_order.process_all_replies_new_template)

    assert "new_template:finish_replies" in source
    assert "Отправьте только один ответ" in source
    assert "replies[next_index] = reply_text" in source
    assert "for i, line in enumerate(lines" not in source


@pytest.mark.unit
def test_order_wizard_no_legacy_bulk_reply_handler_conflict():
    assert not hasattr(order_wizard, "wizard_process_all_replies")


@pytest.mark.unit
def test_quick_order_edit_replies_are_collected_per_message():
    source = inspect.getsource(quick_order.save_replies)

    assert "quicktpl:finish_edit_replies" in source
    assert "Отправьте только один ответ" in source
    assert "edited_replies[next_index] = reply_text" in source
    assert "for idx, line in enumerate(lines" not in source


@pytest.mark.unit
def test_template_edit_replies_are_collected_per_message():
    source = inspect.getsource(templates.edit_template_replies_handler)

    assert "template:finish_replies:" in source
    assert "Отправьте только один ответ" in source
    assert "collected.append(reply_text)" in source
    assert "preserve_nonempty_lines(raw_text)" in source
    assert "for idx, line in enumerate" not in source


@pytest.mark.unit
def test_order_replace_texts_are_collected_per_message():
    source = inspect.getsource(orders.handle_replace_texts)

    assert "order:texts_finish" in inspect.getsource(orders.finish_replace_texts_callback)
    assert "Отправьте только один вариант" in source
    assert "variants.append(variant)" in source
    assert ".splitlines()" not in source


@pytest.mark.unit
def test_user_order_ui_does_not_expose_internal_likes_provider_or_fake_quantity():
    repo = Path(__file__).resolve().parents[2]
    user_ui_files = [
        "bot/handlers/order_wizard.py",
        "bot/handlers/quick_order.py",
        "bot/handlers/menu.py",
        "bot/handlers/composer.py",
        "bot/handlers/engagement_wizard.py",
        "bot/keyboards/common.py",
        "bot/keyboards/templates.py",
        "bot/keyboards/composer.py",
        "bot/services/order_creation_ui.py",
    ]
    combined = "\n".join((repo / path).read_text(encoding="utf-8") for path in user_ui_files)

    assert "Boost" not in combined
    assert "по настройке админа" not in combined
    assert "Лайки: 100" not in combined
    assert "Только ответы + лайки" not in combined
