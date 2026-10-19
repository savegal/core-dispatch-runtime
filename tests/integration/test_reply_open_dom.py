"""Offline Chromium regressions: real DOM/events, no TikTok or publication."""

import asyncio
from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright, TimeoutError as BrowserTimeoutError

from executor import dom_actions, publish_action


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize('click_delivered', [False, True])
async def test_native_reply_timeout_uses_different_click_path_before_retiring_page(reply_page, monkeypatch, click_delivered):
    await reply_page.evaluate('''() => {
        document.querySelector('#reply').onclick = () => {
            window.replyClicks++;
            const existing = document.querySelector('#replyEditor');
            if (existing) existing.parentElement.remove();
            else window.mountReply();
        };
    }''')
    native_calls = []
    real_wait = publish_action._await_hard_timeout

    class StalledLocator:
        def locator(self, *_args, **_kwargs):
            return self

        @property
        def first(self):
            return self

        async def click(self, **_kwargs):
            native_calls.append(True)
            if click_delivered:
                await reply_page.locator('#reply').evaluate('(el) => el.click()')
            await asyncio.Event().wait()

    class BusyPage:
        def locator(self, *_args, **_kwargs):
            return StalledLocator()

        def __getattr__(self, name):
            return getattr(reply_page, name)

    async def bounded_wait(awaitable, *, timeout):
        # Reproduce an outer cancellation before the driver finishes click.
        return await real_wait(awaitable, timeout=0.3 if timeout == 4.0 else timeout)

    async def no_guard(*_args, **_kwargs):
        return None

    async def offline_publish(_page, text, **kwargs):
        await kwargs['reply_target'].input_handle.fill(text)
        return publish_action.PublishResult(status='success', comment_id='offline-only')

    monkeypatch.setattr(publish_action, '_await_hard_timeout', bounded_wait)
    monkeypatch.setattr(publish_action, '_captcha_result_if_present', no_guard)
    monkeypatch.setattr(publish_action, '_logged_out_publish_result_if_visible', no_guard)
    monkeypatch.setattr(publish_action, '_publish_via_current_input', offline_publish)
    result = await publish_action.publish_reply_hybrid(
        BusyPage(), 'recovered reply', parent_comment_text='Где посмотреть?', parent_anchor_token='target',
        correlation_id='task_1109_engagement_reply_v1_a1_acc1_to_text',
        comments_already_open=True, max_total_seconds=20,
    )
    assert result.status == 'success', result
    assert len(native_calls) == 1
    assert await reply_page.locator('#replyEditor').text_content() == 'recovered reply'
    assert await reply_page.evaluate('window.replyClicks') == 1


@pytest.mark.parametrize('inside_video_dialog', [False, True])
async def test_dom_reply_retry_does_not_toggle_editor_while_post_is_mounting(reply_page, inside_video_dialog):
    if inside_video_dialog:
        await reply_page.evaluate('''() => {
            const parent = document.querySelector('#parent');
            const dialog = document.createElement('div');
            dialog.setAttribute('role', 'dialog');
            parent.before(dialog);
            dialog.append(parent);
        }''')
    await reply_page.evaluate('() => window.mountReply(700)')
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text='Где посмотреть?', parent_anchor_token='target',
        max_attempts=1, prefer_dom_click=True,
    )
    assert result.success, result
    assert await reply_page.evaluate('window.replyClicks') == 0
    assert await reply_page.locator('#replyEditor').count() == 1


async def test_visible_editor_does_not_wait_for_unrelated_overlay_sweep(reply_page, monkeypatch):
    calls = []

    async def overlay_sweep(_page):
        calls.append(True)

    monkeypatch.setattr(dom_actions, 'suppress_keyboard_shortcuts_overlay', overlay_sweep)
    assert await dom_actions.ensure_click_target_clear(reply_page, await reply_page.query_selector('#mainEditor'))
    assert calls == []


@pytest.mark.parametrize('challenge', [False, True])
async def test_target_clear_removes_only_plain_blocker_and_keeps_challenge(reply_page, challenge):
    await reply_page.evaluate('''challenge => {
        const blocker = document.createElement('div');
        blocker.id = 'blocker';
        blocker.className = challenge ? 'captcha' : 'notification-toast';
        blocker.textContent = challenge ? 'Drag the slider' : 'Notification';
        const rect = document.querySelector('#mainEditor').getBoundingClientRect();
        blocker.style.cssText = `position:fixed;left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;z-index:999999;background:white`;
        document.body.append(blocker);
    }''', challenge)
    assert await dom_actions.ensure_click_target_clear(
        reply_page, await reply_page.query_selector('#mainEditor'),
    ) is (not challenge)
    assert await reply_page.locator('#blocker').evaluate('(el) => getComputedStyle(el).pointerEvents') == ('auto' if challenge else 'none')


async def test_dom_retry_keeps_parent_identity_guard(reply_page):
    await reply_page.locator('#parent [data-e2e="comment-level-1"]').evaluate('(el) => el.textContent = "Changed parent"')
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text='Где посмотреть?', parent_anchor_token='target',
        max_attempts=1, prefer_dom_click=True,
    )
    assert not result.success
    assert result.reason == 'parent_anchor_text_mismatch'
    assert await reply_page.evaluate('window.replyClicks + window.wrongClicks') == 0


async def _probe_reply_send(page, probe):
    if probe == 'composer':
        result = await page.evaluate(dom_actions._JS_FIND_REPLY_INPUT, ['target', 'Где посмотреть?', ''])
        return await page.query_selector('[data-smart-reply-post="1"]') if result.get('found') else None
    if probe == 'post':
        result = await page.evaluate(dom_actions._JS_FIND_REPLY_POST_BUTTON)
        return await page.query_selector('[data-smart-id="comment-post"]') if result.get('found') else None
    if probe == 'enabled':
        handle, _ = await dom_actions.wait_for_post_button_enabled(
            page, reply_scoped=True, expected_text='I like this reply', max_wait_seconds=0.3,
        )
        return handle
    handle, _ = await dom_actions.find_reply_send_candidate_for_optimistic_click(
        page, expected_text='I like this reply', max_total_seconds=0.3,
    )
    return handle


async def _mount_send_candidates(page, *, like_metadata, real_send=False):
    await page.evaluate('''([metadata, realSend]) => {
        const composer = window.mountReply();
        composer.setAttribute('data-smart-reply-composer', '1');
        const editor = composer.querySelector('[contenteditable]');
        editor.textContent = 'I like this reply';
        editor.setAttribute('data-smart-reply-input', '1');
        composer.querySelector('button').remove();
        composer.style.width = '520px';
        const like = document.createElement('div');
        like.id = 'videoLike'; like.setAttribute('role', 'button');
        like.setAttribute('data-smart-reply-post', '1'); // stale binding must not override purpose
        for (const [key, value] of Object.entries(metadata)) like.setAttribute(key, value);
        like.innerHTML = '<svg width="28" height="21"><path d="M0 0L20 20"/></svg>';
        if (realSend) {
            const send = document.createElement('button');
            send.id = 'realSend';
            // The real arrow has neither a label nor a data-e2e attribute.
            send.innerHTML = '<svg width="24" height="24"><path d="M0 0L20 20"/></svg>';
            composer.append(send);
        }
        composer.append(like);
    }''', [like_metadata, real_send])


@pytest.mark.parametrize('probe', ['composer', 'post', 'enabled', 'optimistic'])
@pytest.mark.parametrize('like_metadata', [
    {'aria-label': 'Like video\n5 likes'},
    {'class': 'css-1qywyof-7937d88b--DivLikeContainer e16vxroe0'},
    {'data-e2e': 'comment-digg'},
])
async def test_reply_send_rejects_like_even_with_stale_post_binding(reply_page, probe, like_metadata):
    await _mount_send_candidates(reply_page, like_metadata=like_metadata)
    assert await _probe_reply_send(reply_page, probe) is None
    assert await reply_page.locator('#videoLike').get_attribute('data-smart-id') is None


@pytest.mark.parametrize('probe', ['composer', 'post', 'enabled', 'optimistic'])
async def test_reply_send_keeps_unlabelled_arrow_and_ignores_like_to_its_right(reply_page, probe):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=True)
    if probe == 'post':
        # This strict finder intentionally needs a send marker; geometry is
        # supported by composer discovery, the enabled gate and recovery.
        await reply_page.locator('#realSend').evaluate('(el) => el.setAttribute("data-e2e", "comment-post")')
    # Free-form parent/draft words must not be mistaken for a button's purpose.
    await reply_page.locator('#parent [data-e2e="comment-level-1"]').evaluate(
        '(el) => el.append(" Like, share, reply and follow")'
    )
    handle = await _probe_reply_send(reply_page, probe)
    assert handle is not None
    assert await handle.evaluate('(el) => el.closest("button").id') == 'realSend'


async def test_optimistic_send_prefers_bound_composer_over_farther_ancestor_icon(reply_page):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=True)
    await reply_page.evaluate('''() => {
        const parent = document.querySelector('#parent');
        parent.style.position = 'relative';
        const unrelated = document.createElement('button');
        unrelated.style.cssText = 'position:absolute; right:-70px; bottom:22px; width:28px; height:24px';
        unrelated.innerHTML = '<svg width="24" height="24"><path d="M0 0L20 20"/></svg>';
        parent.append(unrelated);
    }''')
    handle = await _probe_reply_send(reply_page, 'optimistic')
    assert handle is not None
    assert await handle.evaluate('(el) => el.closest("button").id') == 'realSend'


async def test_optimistic_send_can_still_find_arrow_outside_inner_editor_wrapper(reply_page):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=True)
    await reply_page.evaluate('''() => {
        const composer = document.querySelector('[data-smart-reply-composer]');
        composer.removeAttribute('data-smart-reply-composer');
        const wrapper = document.createElement('div');
        wrapper.setAttribute('data-smart-reply-composer', '1');
        composer.prepend(wrapper);
        wrapper.append(document.querySelector('#replyEditor'));
    }''')
    handle = await _probe_reply_send(reply_page, 'optimistic')
    assert handle is not None
    assert await handle.evaluate('(el) => el.closest("button").id') == 'realSend'


async def test_reply_post_handle_returns_new_send_not_earlier_stale_like(reply_page):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=True)
    await reply_page.evaluate('''() => {
        const send = document.querySelector('#realSend');
        send.setAttribute('data-e2e', 'comment-post');
        send.before(document.querySelector('#videoLike'));
    }''')
    handle = await reply_page.evaluate_handle(dom_actions._JS_FIND_REPLY_POST_BUTTON_HANDLE)
    assert await handle.evaluate('(el) => el && el.id') == 'realSend'


@pytest.mark.parametrize('real_send', [False, True])
async def test_direct_reply_recovery_never_clicks_stale_like(reply_page, real_send):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=real_send)
    await reply_page.evaluate('''() => {
        window.sendClicks = 0;
        document.querySelector('#videoLike').onclick = () => window.wrongClicks++;
        const send = document.querySelector('#realSend');
        if (send) {
            send.setAttribute('data-e2e', 'comment-post');
            send.onclick = () => window.sendClicks++;
        }
    }''')
    result = await reply_page.evaluate(publish_action._JS_REPLY_DIRECT_POST_CLICK, ['I like this reply'])
    assert result.get('clicked') is real_send, result
    assert await reply_page.evaluate('window.wrongClicks') == 0
    assert await reply_page.evaluate('window.sendClicks') == int(real_send)


async def test_reply_post_state_does_not_treat_stale_like_as_unsent_draft(reply_page):
    await _mount_send_candidates(reply_page, like_metadata={'aria-label': 'Like video'}, real_send=True)
    await reply_page.locator('#realSend').evaluate('''(el) => {
        el.setAttribute('data-e2e', 'comment-post'); el.disabled = true;
    }''')
    result = await reply_page.evaluate(publish_action._JS_POST_BUTTON_ENABLED, [True])
    assert result == {'found': True, 'enabled': False}


@pytest.fixture
async def reply_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""
                <style>
                  body { font: 16px Arial; }
                  article { width: 520px; margin: 35px; padding: 12px; border: 1px solid; }
                  [role=button] { cursor: pointer; display: inline-block; padding: 8px; }
                  .composer { width: 480px; height: 64px; display: flex; align-items: center; }
                  [contenteditable] { width: 380px; height: 32px; border: 1px solid; }
                  button { width: 60px; height: 32px; }
                  #main { position: fixed; bottom: 5px; left: 35px; }
                </style>
                <section id="comments">
                  <article data-e2e="comment-item" data-smart-parent-anchor="target" id="parent">
                    <p data-e2e="comment-level-1">Где посмотреть?</p>
                    <p role="button" data-e2e="comment-reply-1" id="reply">Ответить</p>
                  </article>
                  <article data-e2e="comment-item" id="neighbour">
                    <p data-e2e="comment-level-1">Другой комментарий</p>
                    <p role="button" data-e2e="comment-reply-1" onclick="window.wrongClicks++">Ответить</p>
                  </article>
                </section>
                <div id="main" class="composer" data-e2e="comment-input">
                  <div id="mainEditor" contenteditable="true" data-smart-main-input="1" aria-label="Add a comment"></div>
                  <button data-e2e="comment-post">Post</button>
                </div>
            """)
            # Patchright evaluates in an isolated world. Install the local
            # fixture's handlers in that same world as the probes/counters.
            await page.evaluate("""() => {
                  window.replyClicks = 0; window.wrongClicks = 0;
                  window.mountReply = (postDelay = 0) => {
                    const composer = document.createElement('div');
                    composer.className = 'composer';
                    composer.setAttribute('data-e2e', 'comment-input');
                    composer.innerHTML = '<div id="replyEditor" contenteditable="true" aria-label="Add a comment"></div>';
                    document.querySelector('#parent').append(composer);
                    const addPost = () => {
                      const post = document.createElement('button');
                      post.setAttribute('data-e2e', 'comment-post'); post.textContent = 'Post'; post.disabled = true;
                      composer.append(post);
                    };
                    if (postDelay) setTimeout(addPost, postDelay); else addPost();
                    return composer;
                  };
                  document.querySelector('#reply').onclick = () => { window.replyClicks++; };
                  document.querySelector('#neighbour [role="button"]').onclick = () => { window.wrongClicks++; };
            }""")
            yield page
        finally:
            await browser.close()


async def test_reply_mount_and_post_hydration_are_awaited_without_double_click(reply_page):
    await reply_page.evaluate("""() => {
        document.querySelector('#reply').onclick = () => {
            window.replyClicks++;
            setTimeout(() => window.mountReply(250), 250);
        };
    }""")
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="Где посмотреть?", parent_anchor_token="target", max_attempts=1,
    )
    assert result.success, result.reason
    assert await reply_page.evaluate("window.replyClicks") == 1
    assert await reply_page.locator('#replyEditor').get_attribute('data-smart-reply-anchor-token') == 'target'
    assert await reply_page.locator('#mainEditor').get_attribute('data-smart-reply-input') is None


@pytest.mark.parametrize("text", ["Где search?", "Где login password?", "Как пройти captcha verification?"])
async def test_user_comment_words_are_not_auth_or_search_surfaces(reply_page, text):
    await reply_page.locator('#parent [data-e2e="comment-level-1"]').evaluate('(el, text) => el.textContent = text', text)
    await reply_page.evaluate("window.mountReply()")
    result = await reply_page.evaluate(dom_actions._JS_FIND_REPLY_INPUT, ["target", text, ""])
    assert result["found"], result
    assert result["strategy"] == "reply-composer-inside_parent_subtree"


async def test_existing_reply_draft_is_reused_without_clicking_reply_again(reply_page):
    await reply_page.evaluate("window.mountReply()")
    await reply_page.locator('#replyEditor').fill('уже введённый ответ')
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="Где посмотреть?", parent_anchor_token="target", max_attempts=1,
    )
    assert result.success
    assert result.strategy.startswith('reply-already-open')
    assert await reply_page.evaluate("window.replyClicks") == 0
    assert await reply_page.locator('#replyEditor').inner_text() == 'уже введённый ответ'


async def test_native_reply_locator_waits_for_remounted_parent(reply_page):
    await reply_page.evaluate("""() => {
        const parent = document.querySelector('#parent');
        parent.remove();
        setTimeout(() => {
            document.querySelector('#comments').prepend(parent);
            parent.querySelector('#reply').onclick = () => { window.replyClicks++; window.mountReply(); };
        }, 180);
    }""")
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="Где посмотреть?", parent_anchor_token="target", max_attempts=1,
    )
    assert result.success, result.reason
    assert result.strategy.startswith("parent-anchor-native-canonical+")
    assert await reply_page.evaluate('window.replyClicks') == 1
    assert await reply_page.evaluate('window.wrongClicks') == 0


async def test_global_main_input_does_not_make_reply_open_successful(reply_page):
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="Где посмотреть?", parent_anchor_token="target", max_attempts=1,
    )
    assert not result.success
    assert result.reason == 'reply_composer_triple_not_found'
    assert await reply_page.evaluate('window.replyClicks') == 1
    assert await reply_page.locator('#mainEditor').get_attribute('data-smart-reply-input') is None


async def test_missing_reply_action_does_not_click_neighbour_parent(reply_page):
    await reply_page.locator('#reply').evaluate('(el) => el.remove()')
    result = await reply_page.evaluate(dom_actions._JS_OPEN_REPLY, ['', 'Где посмотреть?', 'target'])
    assert not result.get('clicked'), result
    assert await reply_page.evaluate('window.wrongClicks') == 0


async def test_remounted_parent_uses_new_anchor_returned_by_click(reply_page):
    await reply_page.locator('#parent').evaluate('(el) => el.setAttribute("data-smart-parent-anchor", "fresh")')
    await reply_page.evaluate("""() => {
        document.querySelector('#reply').onclick = () => { window.replyClicks++; window.mountReply(); };
    }""")
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="Где посмотреть?", parent_anchor_token="stale", max_attempts=1,
    )
    assert result.success, result.reason
    assert result.anchor_token == 'fresh'
    assert await reply_page.evaluate('window.replyClicks') == 1


async def test_real_login_editor_is_still_rejected_even_inside_parent(reply_page):
    await reply_page.evaluate("""() => { const composer = window.mountReply(); composer.classList.add('login-dialog'); }""")
    result = await reply_page.evaluate(dom_actions._JS_FIND_REPLY_INPUT, ['target', 'Где посмотреть?', ''])
    assert not result['found']
    assert any(row['reason'] == 'search_or_auth' for row in result['rejected'])


async def test_changed_parent_text_is_rejected_before_reply_click(reply_page):
    result = await dom_actions.open_reply_box(
        reply_page, parent_comment_text="совершенно другой текст", parent_anchor_token="target", max_attempts=1,
    )
    assert not result.success
    assert result.reason == 'parent_anchor_text_mismatch'
    assert await reply_page.evaluate('window.replyClicks') == 0


async def test_neighbour_reply_editor_is_not_bound_to_selected_parent(reply_page):
    await reply_page.evaluate("""() => {
        const composer = window.mountReply();
        const neighbour = document.querySelector('#neighbour');
        neighbour.setAttribute('data-smart-parent-anchor', 'other-parent');
        neighbour.append(composer);
        document.querySelector('#replyEditor').setAttribute('aria-label', 'Add a reply');
    }""")
    result = await reply_page.evaluate(dom_actions._JS_FIND_REPLY_INPUT, ['target', 'Где посмотреть?', ''])
    assert not result['found']
    assert any(row['reason'] == 'other_parent_composer' for row in result['rejected'])


@pytest.mark.parametrize('completion_error', ['navigation_wait', 'timeout_after_click'])
async def test_second_parent_is_typed_once_when_click_completion_is_late(reply_page, completion_error):
    await reply_page.evaluate('''() => {
        window.mountReply(); // The first parent's empty editor remains after a reply.
        const second = document.querySelector('#neighbour');
        second.setAttribute('data-smart-parent-anchor', 'second');
        window.secondReplyClicks = 0;
        second.querySelector('[data-e2e="comment-reply-1"]').onclick = () => {
            window.secondReplyClicks++;
            const existing = second.querySelector('.composer');
            if (existing) { existing.remove(); return; }
            const composer = document.querySelector('#parent .composer').cloneNode(true);
            composer.querySelector('[contenteditable]').id = 'secondEditor';
            second.append(composer);
        };
    }''')
    first = await dom_actions.open_reply_box(
        reply_page, parent_comment_text='Где посмотреть?', parent_anchor_token='target', max_attempts=1,
    )
    assert first.success

    class LateClickLocator:
        def __init__(self, locator):
            self.inner = locator

        def locator(self, *args, **kwargs):
            return LateClickLocator(self.inner.locator(*args, **kwargs))

        @property
        def first(self):
            return LateClickLocator(self.inner.first)

        async def click(self, **kwargs):
            # Deliver the local fixture's click deterministically; this test
            # injects a failure of the completion acknowledgement separately.
            await self.inner.evaluate('(el) => el.click()')
            # The event reached React, but the driver's completion can fail
            # afterwards. Retrying the click toggles this editor closed.
            if completion_error == 'timeout_after_click' or not kwargs.get('no_wait_after'):
                raise BrowserTimeoutError('click delivered; completion timed out')

    class LateClickPage:
        def __getattr__(self, name):
            return getattr(reply_page, name)

        def locator(self, *args, **kwargs):
            return LateClickLocator(reply_page.locator(*args, **kwargs))

    result = await dom_actions.open_reply_box(
        LateClickPage(), parent_comment_text='Другой комментарий', parent_anchor_token='second', max_attempts=1,
    )
    assert result.success, result.reason
    target, info = await dom_actions.find_reply_composer_for_parent(
        reply_page, parent_anchor_token='second', expected_parent_text='Другой комментарий',
    )
    assert target, info.reason
    await target.input_handle.fill('ответ второму родителю')
    assert await reply_page.locator('#secondEditor').inner_text() == 'ответ второму родителю'
    assert await reply_page.locator('#replyEditor').inner_text() == ''
    assert await reply_page.evaluate('window.secondReplyClicks') == 1


async def test_ready_reply_target_fits_recovery_budget_with_slow_browser_roundtrips(reply_page, monkeypatch):
    await reply_page.evaluate('window.mountReply()')
    elapsed = [0.0]

    async def roundtrip():
        started = elapsed[0]
        await asyncio.sleep(0.01)
        # Simulate 600ms transport latency without slowing the browser. Calls
        # issued concurrently share the same round trip.
        elapsed[0] = max(elapsed[0], started + 0.6)

    class SlowHandle:
        def __init__(self, inner):
            self.inner = inner

        def as_element(self):
            return self if self.inner.as_element() else None

        async def evaluate(self, *args):
            await roundtrip()
            return await self.inner.evaluate(*args)

    class SlowPage:
        async def evaluate(self, *args):
            await roundtrip()
            return await reply_page.evaluate(*args)

        async def evaluate_handle(self, *args):
            await roundtrip()
            return SlowHandle(await reply_page.evaluate_handle(*args))

    monkeypatch.setattr(dom_actions, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    target, info = await dom_actions.find_reply_composer_for_parent(
        SlowPage(), parent_anchor_token='target', expected_parent_text='Где посмотреть?', max_total_seconds=2.5,
    )
    assert target, info.reason
    assert info.elapsed_seconds <= 2.5, 'ready editor exceeded the caller recovery deadline'
    assert await target.input_handle.inner.evaluate('(el) => el.id') == 'replyEditor'
