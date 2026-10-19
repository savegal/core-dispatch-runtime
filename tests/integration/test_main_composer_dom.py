"""Offline main-composer reuse under delayed browser transport."""

import asyncio

import pytest
from patchright.async_api import async_playwright

from executor import publish_action, dom_actions
from executor.captcha_detector import detect_captcha


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def main_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            await page.set_content('''
                <div data-e2e="comment-input">
                  <div id="editor" contenteditable="true" style="width:300px;height:40px">previous variant</div>
                  <button data-e2e="comment-post">Post</button>
                </div>
                <input id="unrelated" value="keep this text">
            ''')
            yield page
        finally:
            await browser.close()


class _DelayedHandle:
    def __init__(self, inner, *, focus_delay=0, state_delay=0):
        self.inner = inner
        self.focus_delay = focus_delay
        self.state_delay = state_delay

    async def evaluate(self, script, *args):
        delay = self.focus_delay if 'editor.focus' in script else self.state_delay
        await asyncio.sleep(delay)
        return await self.inner.evaluate(script, *args)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.parametrize('blocked_wrapper', ['data-smart-parent-anchor="parent-1"', 'class="login-dialog"'])
async def test_main_input_fast_probe_skips_reply_or_login_editor_before_main(main_page, blocked_wrapper):
    await main_page.evaluate('''attr => {
        document.body.insertAdjacentHTML('afterbegin', `<section ${attr}>
          <div data-e2e="comment-input"><div id="wrongEditor" contenteditable="true"
          style="width:300px;height:40px">leave this draft</div></div>
        </section>`);
    }''', blocked_wrapper)
    handle, info = await dom_actions.find_comment_input_smart(
        main_page, max_total_seconds=3, aggressive=True,
        skip_initial_open=True, main_comment_aggressive=True,
    )
    assert handle is not None, info
    assert await handle.evaluate('el => el.id') == 'editor'
    assert await main_page.locator('#wrongEditor').text_content() == 'leave this draft'


async def test_ready_main_editor_is_resolved_once_and_typed_before_send_gate(main_page, monkeypatch):
    searches = []
    real_find = publish_action.find_comment_input_smart

    class ReadyToSend(Exception):
        pass

    async def find(*args, **kwargs):
        searches.append(True)
        return await real_find(*args, **kwargs)

    async def no_guard(*_args, **_kwargs):
        return None

    async def unexpected_scan(*_args, **_kwargs):
        pytest.fail('a ready main editor must not trigger broad rescanning or reopening')

    async def send_gate(*_args, **kwargs):
        assert await main_page.locator('#editor').text_content() == 'next main text'
        assert kwargs['reply_scoped'] is False
        raise ReadyToSend

    monkeypatch.setattr(publish_action, 'find_comment_input_smart', find)
    monkeypatch.setattr(publish_action, '_captcha_result_if_present', no_guard)
    monkeypatch.setattr(publish_action, '_logged_out_publish_result_if_visible', no_guard)
    monkeypatch.setattr(publish_action, 'get_comment_surface_state', unexpected_scan)
    monkeypatch.setattr(publish_action, 'open_comment_section', unexpected_scan)
    monkeypatch.setattr(publish_action, 'wait_for_post_button_enabled', send_gate)
    with pytest.raises(ReadyToSend):
        await publish_action.publish_comment_hybrid(
            main_page, 'next main text', comments_already_open=True,
            correlation_id='task_1110_parallel_main_v1', max_total_seconds=15,
        )
    assert searches == [True]
    assert await main_page.locator('#unrelated').input_value() == 'keep this text'


async def test_stale_open_hint_does_not_allow_typing_without_a_main_editor(main_page, monkeypatch):
    await main_page.locator('[data-e2e="comment-input"]').evaluate('el => el.remove()')

    async def no_guard(*_args, **_kwargs):
        return None

    async def unexpected_typing(*_args, **_kwargs):
        pytest.fail('an open-panel hint must not bypass main editor resolution')

    monkeypatch.setattr(publish_action, '_captcha_result_if_present', no_guard)
    monkeypatch.setattr(publish_action, '_logged_out_publish_result_if_visible', no_guard)
    monkeypatch.setattr(publish_action, 'type_into_comment', unexpected_typing)
    result = await publish_action.publish_comment_hybrid(
        main_page, 'next main text', comments_already_open=True,
        correlation_id='task_1112_parallel_main_v1', max_total_seconds=3,
    )
    assert result.status != 'success'
    assert result.network_request_attempted is False
    assert await main_page.locator('#unrelated').input_value() == 'keep this text'


@pytest.mark.parametrize('delays', [dict(focus_delay=0.55), dict(state_delay=0.4)])
async def test_pre_type_clear_keeps_ready_editor_when_browser_reply_is_slow(main_page, delays):
    handle = _DelayedHandle(await main_page.query_selector('#editor'), **delays)
    ok = await publish_action._ensure_main_comment_composer_clean_before_typing(
        main_page, reason='offline_slow_ready_editor', input_handle=handle,
    )
    assert ok is True
    await main_page.keyboard.insert_text('next variant')
    assert await main_page.locator('#editor').text_content() == 'next variant'
    assert await main_page.locator('#unrelated').input_value() == 'keep this text'


async def test_clear_still_rejects_editor_that_restores_previous_draft(main_page):
    await main_page.evaluate('''() => {
        const editor = document.querySelector('#editor');
        editor.addEventListener('input', () => {
            if (!editor.textContent.trim()) setTimeout(() => editor.textContent = 'stale draft', 75);
        });
    }''')
    ok = await publish_action._clear_main_comment_composer_best_effort(
        main_page, reason='offline_restored_draft',
        input_handle=await main_page.query_selector('#editor'),
    )
    assert ok is False
    assert await main_page.locator('#editor').text_content() == 'stale draft'


async def test_clear_still_rejects_focus_stolen_by_another_input(main_page):
    await main_page.evaluate('''() => {
        document.querySelector('#editor').addEventListener('focus', () => {
            document.querySelector('#unrelated').focus();
        });
    }''')
    ok = await publish_action._clear_main_comment_composer_best_effort(
        main_page, reason='offline_wrong_focus',
        input_handle=await main_page.query_selector('#editor'),
    )
    assert ok is False
    assert await main_page.locator('#editor').text_content() == 'previous variant'
    assert await main_page.locator('#unrelated').input_value() == 'keep this text'


async def test_captcha_probe_tolerates_loading_dom_and_detects_later_challenge(main_page):
    errors = []

    class ObservedPage:
        def is_closed(self):
            return main_page.is_closed()

        async def evaluate(self, script):
            try:
                return await main_page.evaluate(script)
            except Exception as exc:
                errors.append(str(exc))
                raise

    await main_page.evaluate('() => document.body.remove()')
    assert await detect_captcha(ObservedPage(), wait_ms=0) == (False, None)
    assert errors == []
    await main_page.set_content('<div id="captcha-verify-image" style="width:320px;height:200px">Drag the slider</div>')
    found, _kind = await detect_captcha(ObservedPage(), wait_ms=0)
    assert found is True
    assert errors == []
