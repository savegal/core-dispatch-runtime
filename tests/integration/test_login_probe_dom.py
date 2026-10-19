"""Login probing must not traverse ancestor layout text for every feed row."""
import pytest
from patchright.async_api import async_playwright

from executor.session_bootstrap import get_browser_login_state

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def login_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_login_probe_filters_feed_text_before_layout_and_ancestor_checks(login_page):
    await login_page.set_content('<main>' + ''.join(
        f'<section><div><div><span>ordinary comment {i}</span><button>Reply</button></div></div></section>'
        for i in range(200)
    ) + '</main><button data-e2e="inbox-icon">Inbox</button>')

    class MeasuredPage:
        async def evaluate(self, expression):
            return await login_page.evaluate('''() => {
                let textReads = 0, geometryReads = 0;
                const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText');
                const rect = Element.prototype.getBoundingClientRect;
                Object.defineProperty(HTMLElement.prototype, 'innerText', {
                    ...desc, get() { textReads++; return desc.get.call(this); }
                });
                Element.prototype.getBoundingClientRect = function() { geometryReads++; return rect.call(this); };
                try { const probe = PROBE; return {...probe(), textReads, geometryReads}; }
                finally {
                    Object.defineProperty(HTMLElement.prototype, 'innerText', desc);
                    Element.prototype.getBoundingClientRect = rect;
                }
            }'''.replace('PROBE', expression))

    state = await get_browser_login_state(MeasuredPage())
    assert state['state'] == 'logged_in'
    assert state['textReads'] < 30
    assert state['geometryReads'] < 30


@pytest.mark.parametrize('markup, expected', [
    ('<button>Log in</button>', 'logged_out'),
    ('<button><span style="display:none">hidden prefix</span>Log in</button>', 'logged_out'),
    ('<div>Log in to comment</div>', 'logged_out'),
    ('<div role="dialog">Continue with Google</div>', 'logged_out'),
    ('<button style="display:none">Log in</button>', 'logged_in'),
    ('<div class="cookie-banner">Optional cookies <button>Log in</button></div>', 'logged_in'),
    ('<script id="SIGI_STATE" type="application/json">{"isLogin":false}</script>', 'logged_out'),
])
async def test_login_probe_preserves_real_ui_and_state_guards(login_page, markup, expected):
    await login_page.set_content('<button data-e2e="inbox-icon">Inbox</button>' + markup)
    state = await get_browser_login_state(login_page)
    assert state['state'] == expected, state
