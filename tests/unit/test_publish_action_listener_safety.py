"""Проверяем, что _publish_via_current_input не оставляет response-listener
на page при любом сценарии: успех, captcha, redirect, post_click_failed.

Раньше до фикса listener регистрировался на page.on("response", ...) и
снимался только на нормальном пути. Если исключение (captcha, redirect)
прерывало flow между регистрацией и финальным remove_listener — слушатель
оставался жить на page. Поскольку Playwright Page переиспользуется через
browser_pool, на следующих заказах один и тот же on_response вызывался
многократно, и captured содержал результаты предыдущих публикаций.

Тест статичный (inspect.getsource), потому что полный flow требует Playwright.
"""
import inspect

import pytest


@pytest.mark.unit
def test_publish_action_uses_detach_listener_helper():
    from executor import publish_action
    src = inspect.getsource(publish_action._publish_via_current_input)
    assert "def _detach_listener" in src, (
        "_detach_listener должен быть определён внутри _publish_via_current_input"
    )
    # idempotency: повторный detach не должен дёргать page.remove_listener дважды
    assert "_listener_removed" in src


@pytest.mark.unit
def test_publish_action_detaches_listener_on_captcha_return():
    """На путь captcha challenge listener детачится до return."""
    from executor import publish_action
    src = inspect.getsource(publish_action._publish_via_current_input)
    # Перед return PublishResult(captcha_challenge:slider) должен быть _detach_listener()
    cap_pos = src.find("captcha_challenge:slider")
    assert cap_pos > 0
    window = src[max(0, cap_pos - 400) : cap_pos]
    assert "_detach_listener()" in window, (
        "captcha_challenge:slider return path забыли _detach_listener()"
    )


@pytest.mark.unit
def test_publish_action_detaches_listener_on_post_click_failed():
    """На цепочке post_click_failed (mouse/locator/pw/js все упали) listener
    тоже детачится."""
    from executor import publish_action
    src = inspect.getsource(publish_action._publish_via_current_input)
    err_pos = src.find("post_click_failed:")
    assert err_pos > 0
    # Both post_click_failed returns share a single _detach_listener() that sits a
    # few lines above (just inside the `except ... as js_exc:` block). Use a 400-char
    # window — matching the captcha test — so the legitimate driver_closed_error
    # branch between the detach and the return string doesn't trip a false failure.
    window = src[max(0, err_pos - 400) : err_pos]
    assert "_detach_listener()" in window, (
        "post_click_failed return забыли _detach_listener()"
    )


@pytest.mark.unit
def test_publish_action_no_raw_remove_listener_left():
    """Все вызовы page.remove_listener должны быть унифицированы через
    _detach_listener(). Это гарантирует idempotency и одну точку выхода."""
    from executor import publish_action
    src = inspect.getsource(publish_action._publish_via_current_input)

    # Допустимо только внутри определения самого _detach_listener.
    helper_idx = src.find("def _detach_listener")
    # Найдём конец функции-хелпера: по двум \n после неё.
    helper_end = src.find("_listener_removed = True", helper_idx) + len("_listener_removed = True")
    forbidden_region = src[:helper_idx] + src[helper_end:]
    assert "page.remove_listener" not in forbidden_region, (
        "page.remove_listener должен дёргаться только через _detach_listener()"
    )


@pytest.mark.unit
def test_publish_action_listener_idempotent():
    """_detach_listener должен быть idempotent (повторный вызов не падает
    и не дёргает page.remove_listener дважды)."""
    from executor import publish_action
    src = inspect.getsource(publish_action._publish_via_current_input)
    # ищем _detach_listener body
    body_start = src.find("def _detach_listener")
    body_end = src.find("\n\n", body_start)
    body = src[body_start:body_end]
    # Должна быть проверка флага в начале функции
    assert "if _listener_removed" in body
