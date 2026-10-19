"""Cookie cleanup on real local DOM: content is never a consent banner."""
import pytest
from patchright.async_api import async_playwright
from executor import dom_actions

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def cookie_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            yield page
        finally:
            await browser.close()


@pytest.mark.parametrize('caption', ['Wonderland Cookie Dough', 'I do not consent', 'GDPR explained'])
@pytest.mark.parametrize('has_button', [False, True])
async def test_cookie_words_in_video_caption_do_not_hide_content(cookie_page, caption, has_button):
    await cookie_page.set_content('<section id="video"><div id="caption"></div></section>')
    if has_button:
        await cookie_page.locator('#video').evaluate('''el => el.insertAdjacentHTML('beforeend', '<button onclick="document.body.dataset.clicked=1">I agree</button>')''')
    await cookie_page.locator('#caption').evaluate('(el, text) => el.textContent = text', caption)
    assert await dom_actions.dismiss_cookie_banners(cookie_page) == 0
    assert await cookie_page.locator('#video').is_visible()
    assert await cookie_page.locator('#caption').is_visible()
    assert await cookie_page.locator('body').get_attribute('data-clicked') is None


async def test_cookie_consent_phrase_inside_comment_is_not_a_banner(cookie_page):
    await cookie_page.set_content('<div data-e2e="comment-level-1" id="comment">TikTok uses cookies</div>')
    await dom_actions.dismiss_cookie_banners(cookie_page)
    assert await cookie_page.locator('#comment').is_visible()


@pytest.mark.parametrize('shadow', [False, True])
async def test_real_cookie_banner_accept_control_still_works(cookie_page, shadow):
    await cookie_page.set_content('<div id="host"></div><div id="video">Video stays visible</div>')
    await cookie_page.evaluate('''shadow => {
        const host = document.querySelector('#host');
        const root = shadow ? host.attachShadow({mode:'open'}) : host;
        root.innerHTML = '<div class="cookie-banner">Allow cookies from TikTok on this browser? <button>Accept all</button></div>';
        root.querySelector('button').onclick = () => { window.accepted = true; root.querySelector('.cookie-banner').remove(); };
    }''', shadow)
    await dom_actions.dismiss_cookie_banners(cookie_page)
    assert await cookie_page.evaluate('Boolean(window.accepted)')
    assert await cookie_page.locator('#video').is_visible()


async def test_cookie_observer_coalesces_typing_mutations(cookie_page):
    await cookie_page.set_content('<div id="draft"></div>')
    await dom_actions.dismiss_cookie_banners(cookie_page)
    calls = await cookie_page.evaluate('''async () => {
        window.cleanupCalls = 0;
        window.__teletanCookieBannerCleanup = () => {
            window.cleanupCalls++;
            document.documentElement.dataset.cleanupCalls = String(window.cleanupCalls);
        };
        clearInterval(window.__teletanCookieBannerInterval);
        const draft = document.querySelector('#draft');
        for (let i = 0; i < 20; i++) {
            draft.textContent = String(i);
            await Promise.resolve();
        }
        return window.cleanupCalls;
    }''')
    assert calls == 0
    # Patchright evaluates wait_for_function in a different world; inspect DOM,
    # not a window variable created in the helper's isolated evaluation world.
    await cookie_page.wait_for_function('document.documentElement.dataset.cleanupCalls', timeout=3000)
    assert await cookie_page.locator('html').get_attribute('data-cleanup-calls') == '1'


async def test_shortcuts_cleanup_does_not_measure_every_comment_row(cookie_page):
    await cookie_page.set_content('<main id="rows"></main>')
    await cookie_page.locator('#rows').evaluate('el => el.innerHTML = Array.from({length:300}, (_,i) => `<div>ordinary comment ${i}</div>`).join("")')
    script = '''() => {
        let reads = 0;
        const original = Element.prototype.getBoundingClientRect;
        Element.prototype.getBoundingClientRect = function() { reads++; return original.call(this); };
        try { const cleanup = CLEANUP; cleanup(); return reads; }
        finally { Element.prototype.getBoundingClientRect = original; }
    }'''.replace('CLEANUP', dom_actions._JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY)
    assert await cookie_page.evaluate(script) < 40


@pytest.mark.parametrize('marker', ['Introducing keyboard shortcuts', 'Scroll, use the arrows on the right to browse your feed'])
async def test_shortcuts_popup_is_removed_but_identical_comment_is_preserved(cookie_page, marker):
    await cookie_page.set_content('<div data-e2e="comment-level-1" id="comment" style="width:350px;height:120px"></div><div id="popup" style="position:fixed;right:30px;top:240px;width:330px;height:150px"></div>')
    await cookie_page.locator('#comment').evaluate('(el,text) => el.textContent=text', marker)
    await cookie_page.locator('#popup').evaluate('(el,text) => el.textContent=text', marker)
    await dom_actions.suppress_keyboard_shortcuts_overlay(cookie_page)
    assert await cookie_page.locator('#comment').is_visible()
    assert await cookie_page.locator('#comment').evaluate('el => getComputedStyle(el).pointerEvents') == 'auto'
    assert await cookie_page.locator('#popup').count() == 0 or not await cookie_page.locator('#popup').is_visible()
