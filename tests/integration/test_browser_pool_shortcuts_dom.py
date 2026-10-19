"""Exercise the actual context init script, including React-like DOM churn."""
import pytest
from patchright.async_api import async_playwright

from executor.browser_pool import _TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def startup_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        # Count layout-dependent reads in the SAME world as add_init_script.
        await context.add_init_script('''(() => {
            const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText');
            window.layoutTextReads = 0;
            Object.defineProperty(HTMLElement.prototype, 'innerText', {
                ...desc, get() { window.layoutTextReads++; return desc.get.call(this); }
            });
        })();''')
        await context.add_init_script(_TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT)
        page = await context.new_page()
        await page.route('**/*', lambda route: route.fulfill(
            content_type='text/html', body='<main id="rows">' + ''.join(
                f'<div data-e2e="comment-level-1">ordinary comment {i}</div>' for i in range(300)
            ) + '</main><div id="draft"></div>'
        ))
        try:
            await page.goto('https://fixture.test/video')
            yield page
        finally:
            await browser.close()


async def test_startup_cleanup_does_not_read_layout_text_for_entire_feed(startup_page):
    reads = await startup_page.evaluate('window.layoutTextReads', isolated_context=False)
    assert reads == 0


async def test_startup_observer_ignores_unrelated_attribute_churn(startup_page):
    calls = await startup_page.evaluate('''async () => {
        let scans = 0;
        const original = document.querySelectorAll.bind(document);
        document.querySelectorAll = (selector) => {
            if (selector === 'body *') scans++;
            return original(selector);
        };
        try {
            const draft = document.getElementById('draft');
            for (let i = 0; i < 40; i++) {
                draft.setAttribute('data-react-state', String(i));
                await Promise.resolve();
            }
            return scans;
        } finally { document.querySelectorAll = original; }
    }''', isolated_context=False)
    assert calls == 0


async def test_startup_removes_late_popup_and_keeps_matching_comment(startup_page):
    await startup_page.evaluate('''() => {
        document.getElementById('draft').innerHTML =
            '<div data-e2e="comment-level-1" id="comment" style="width:350px;height:120px">Introducing keyboard shortcuts</div>' +
            '<div id="popup" style="position:fixed;right:30px;top:240px;width:330px;height:150px">Introducing keyboard shortcuts</div>';
    }''')
    await startup_page.wait_for_function('!document.getElementById("popup")', timeout=3000)
    assert await startup_page.locator('#comment').is_visible()
    assert await startup_page.locator('#comment').evaluate('el => getComputedStyle(el).pointerEvents') == 'auto'
