from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from patchright.async_api import Page

logger = logging.getLogger("executor.dom_actions")

_JS_FIND_COMMENT_INPUT = r"""
() => {
    const url = window.location.pathname || '';
    if (url.includes('/messages') || url.includes('/login') || url.includes('/verify')) {
        return { found: false, reason: 'redirected', url };
    }

    const pickEditable = (root) => {
        if (!root) return null;

        if (root.matches && root.matches('textarea, input, div[contenteditable="true"], [contenteditable="true"]')) {
            return root;
        }

        // TikTok often puts data-e2e="comment-input" on a wrapper DIV.
        // The actual editor is nested inside it.
        const direct = root.querySelector && root.querySelector(
            'textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]'
        );
        if (direct) return direct;

        return root;
    };

    const visibleEnough = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 20 && r.height > 10 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    // visibleEnoughAny: accepts element OR its parent having size > threshold.
    // CRITICAL FIX: TikTok's inner contenteditable has height=0 when unfocused
    // (wrapper data-e2e="comment-input" has h=42, but nested div[contenteditable] has h=0).
    // All strategies must check the WRAPPER visibility, not just the inner element.
    const visibleEnoughOrParent = (el, raw) => {
        if (visibleEnough(el)) return true;
        const parent = raw && raw !== el ? raw : (el ? el.parentElement : null);
        return parent ? visibleEnough(parent) : false;
    };

    const challengeMarkers = [
        'captcha',
        'verification',
        'security check',
        'security verification',
        'complete verification',
        'drag the slider',
        'fit the puzzle',
        'slide to verify',
        'challenge',
    ];
    const authMarkers = [
        'login',
        'log in',
        'sign in',
        'sign-in',
        'signup',
        'sign up',
        'phone / email',
        'email / username',
        'continue with google',
        'continue with facebook',
        'continue with apple',
        'use phone',
        'password',
    ];

    const insideChallenge = (el) => {
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            const structuralLabel = [
                cur.id || '',
                String(cur.className || ''),
                cur.getAttribute && (cur.getAttribute('role') || ''),
                cur.getAttribute && (cur.getAttribute('aria-label') || ''),
                cur.tagName || '',
            ].join(' ').toLowerCase();
            if (challengeMarkers.some(marker => structuralLabel.includes(marker))) {
                return true;
            }
            const r = cur.getBoundingClientRect ? cur.getBoundingClientRect() : null;
            const st = cur.ownerDocument ? getComputedStyle(cur) : null;
            const z = st ? parseInt(st.zIndex || '0', 10) : 0;
            const isDialogLike =
                structuralLabel.includes('dialog')
                || structuralLabel.includes('modal')
                || structuralLabel.includes('challenge')
                || (r && r.width > 120 && r.height > 80 && r.width < window.innerWidth * 0.9 && r.height < window.innerHeight * 0.9 && (st.position === 'fixed' || st.position === 'absolute' || z > 10));
            if (isDialogLike) {
                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                if (challengeMarkers.some(marker => text.includes(marker))) {
                    return true;
                }
            }
        }
        return false;
    };

    const insideAuthSurface = (el) => {
        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            const structuralLabel = [
                cur.id || '',
                String(cur.className || ''),
                cur.getAttribute && (cur.getAttribute('role') || ''),
                cur.getAttribute && (cur.getAttribute('aria-label') || ''),
                cur.getAttribute && (cur.getAttribute('data-e2e') || ''),
                cur.getAttribute && (cur.getAttribute('href') || ''),
                cur.tagName || '',
            ].join(' ').toLowerCase();
            if (authMarkers.some(marker => structuralLabel.includes(marker))) {
                return true;
            }

            const isDialogLike =
                structuralLabel.includes('dialog')
                || structuralLabel.includes('modal')
                || authTokenLike(structuralLabel)
                || structuralLabel.includes('login');
            if (isDialogLike) {
                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                if (authTokenLike(text) || authMarkers.some(marker => text.includes(marker))) {
                    return true;
                }
            }
        }
        return false;
    };

    const searchMarkers = [
        'search',
        'search-input',
        'searchbar',
        'search bar',
        'find creators',
        '\u043f\u043e\u0438\u0441\u043a',
        '\u0448\u0443\u043a\u0430\u0442\u0438',
        '\u641c\u7d22',
        '\u691c\u7d22',
        '\uac80\uc0c9',
    ];

    const looksLikeSearchInput = (el, raw) => {
        const nodes = [el, raw, el && el.parentElement, raw && raw.parentElement].filter(Boolean);
        for (const node of nodes) {
            const label = [
                node.getAttribute && (node.getAttribute('placeholder') || ''),
                node.getAttribute && (node.getAttribute('aria-label') || ''),
                node.getAttribute && (node.getAttribute('data-placeholder') || ''),
                node.getAttribute && (node.getAttribute('data-e2e') || ''),
                node.getAttribute && (node.getAttribute('name') || ''),
                node.getAttribute && (node.getAttribute('type') || ''),
                node.id || '',
                String(node.className || ''),
                node.tagName || '',
            ].join(' ').toLowerCase();
            if (searchMarkers.some(marker => label.includes(marker))) return true;
            const name = (node.getAttribute && (node.getAttribute('name') || '')).toLowerCase();
            if (name === 'q' || name === 'query') return true;
        }
        return false;
    };

    const safeCommentCandidate = (el, raw) => {
        return el
            && !insideChallenge(el)
            && (!raw || !insideChallenge(raw))
            && !insideAuthSurface(el)
            && (!raw || !insideAuthSurface(raw))
            && !looksLikeSearchInput(el, raw);
    };

    // Strategy 0: Check for comment input in side panel (desktop layout).
    const sidePanel = document.querySelector(
        '[class*="DivCommentListContainer"], [class*="CommentListContainer"], ' +
        '[data-e2e="comment-list-container"], [class*="comment-detail"]'
    );
    if (sidePanel) {
        try { sidePanel.scrollTop = 0; } catch (e) {}
        const panelInputRaw = sidePanel.querySelector(
            '[data-e2e="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
        );
        const panelInput = pickEditable(panelInputRaw);
        if (safeCommentCandidate(panelInput, panelInputRaw) && visibleEnoughOrParent(panelInput, panelInputRaw)) {
            panelInput.setAttribute('data-smart-id', 'comment-input');
            return { found: true, strategy: 'side-panel' };
        }
    }

    const dataE2eRaw = document.querySelector(
        '[data-e2e="comment-input"], [data-e2e*="CommentInput"], [data-e2e*="comment-input"]'
    );
    const dataE2e = pickEditable(dataE2eRaw);
    // Check wrapper visibility (dataE2eRaw) because inner contenteditable may have h=0 when unfocused
    if (safeCommentCandidate(dataE2e, dataE2eRaw) && visibleEnoughOrParent(dataE2e, dataE2eRaw)) {
        dataE2e.setAttribute('data-smart-id', 'comment-input');
        return { found: true, strategy: dataE2e === dataE2eRaw ? 'data-e2e' : 'data-e2e-inner-editable' };
    }

    const editables = document.querySelectorAll('div[contenteditable="true"]');
    for (const el of editables) {
        const combined = [
            el.getAttribute('placeholder') || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('data-placeholder') || '',
        ].join(' ').toLowerCase();
        if (combined.match(/comment|комментарий|коммент|say something|add a comment|написать/)) {
            if (safeCommentCandidate(el, el.parentElement) && visibleEnoughOrParent(el, el.parentElement)) {
                el.setAttribute('data-smart-id', 'comment-input');
                return { found: true, strategy: 'placeholder-text' };
            }
        }
    }

    const sections = document.querySelectorAll(
        '[data-e2e*="comment"], [class*="CommentList"], [class*="CommentContainer"]'
    );
    for (const section of sections) {
        const rawInner = section.querySelector('[data-e2e="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]');
        const inner = pickEditable(rawInner);
        if (safeCommentCandidate(inner, rawInner) && visibleEnoughOrParent(inner, rawInner)) {
            inner.setAttribute('data-smart-id', 'comment-input');
            return { found: true, strategy: 'inside-comment-section' };
        }
    }

    const candidates = [
        ...document.querySelectorAll('div[contenteditable="true"]'),
        ...document.querySelectorAll('[contenteditable="true"]'),
        ...document.querySelectorAll('[role="textbox"]'),
        ...document.querySelectorAll('textarea'),
    ];
    for (const raw of candidates) {
        const el = pickEditable(raw);
        if (!el) continue;
        if (!safeCommentCandidate(el, raw)) continue;
        // Check el OR its parent (wrapper may be bigger than inner)
        const rEl = el.getBoundingClientRect();
        const rParent = el.parentElement ? el.parentElement.getBoundingClientRect() : rEl;
        const r = rParent.height > rEl.height ? rParent : rEl;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (r.width > 100 && r.height > 15 && r.top > window.innerHeight * 0.35) {
            el.setAttribute('data-smart-id', 'comment-input');
            return { found: true, strategy: 'position-heuristic' };
        }
    }


    // Strategy 5: ANY visible contenteditable at bottom/lower part of viewport.
    // TikTok A/B variants sometimes render comment input without data-e2e.
    const allEditable = document.querySelectorAll('div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea');
    for (const raw of allEditable) {
        const el = pickEditable(raw);
        if (!el) continue;
        if (!safeCommentCandidate(el, raw)) continue;
        const rEl = el.getBoundingClientRect();
        const rParent = el.parentElement ? el.parentElement.getBoundingClientRect() : rEl;
        const r = rParent.height > rEl.height ? rParent : rEl;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (r.width > 50 && r.height > 5 && r.top > window.innerHeight * 0.3) {
            el.setAttribute('data-smart-id', 'comment-input');
            return { found: true, strategy: 'any-visible-editable' };
        }
    }

    // Strategy 6: ANY visible editable anywhere on page.
    // Last-resort fallback for accounts where comment input exists but has no e2e marker.
    for (const raw of allEditable) {
        const el = pickEditable(raw);
        if (!safeCommentCandidate(el, raw)) continue;
        if (!el || !visibleEnough(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 50 && r.height > 5) {
            el.setAttribute('data-smart-id', 'comment-input');
            return { found: true, strategy: 'any-editable-last-resort' };
        }
    }

    return { found: false, reason: 'not_visible' };
}
"""

_JS_FIND_COMMENT_INPUT_HANDLE = (
    "() => {\n"
    "    const probe = " + _JS_FIND_COMMENT_INPUT.strip() + ";\n"
    "    let result = null;\n"
    "    try {\n"
    "        result = probe();\n"
    "    } catch (exc) {\n"
    "        result = { found: false, reason: 'probe_exception', error: String(exc && (exc.message || exc)) };\n"
    "    }\n"
    "    try { window.__smartCommentInputProbeState = result || null; } catch (_) {}\n"
    "    if (!result || !result.found) return null;\n"
    "    const marked = document.querySelector('[data-smart-id=\"comment-input\"]');\n"
    "    if (!marked) {\n"
    "        try { window.__smartCommentInputProbeState = { ...result, found: false, reason: 'marked_input_missing_after_probe' }; } catch (_) {}\n"
    "        return null;\n"
    "    }\n"
    "    try { marked.setAttribute('data-smart-strategy', result.strategy || 'old-js-handle'); } catch (_) {}\n"
    "    return marked;\n"
    "}\n"
)

_JS_RESOLVE_COMMENT_INPUT_HANDLE = r"""
() => {
    const finish = (state) => {
        try { window.__smartCommentInputResolverState = state || null; } catch (_) {}
        return state;
    };

    const path = window.location.pathname || '';
    if (path.includes('/messages') || path.includes('/login') || path.includes('/verify')) {
        finish({ found: false, reason: 'redirected', url: path });
        return null;
    }

    try {
        document
            .querySelectorAll('[data-smart-input-resolver="comment"]')
            .forEach(el => el.removeAttribute('data-smart-input-resolver'));
    } catch (_) {}

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const labelOf = (el) => {
        if (!el) return '';
        return [
            el.getAttribute && (el.getAttribute('data-smart-id') || ''),
            el.getAttribute && (el.getAttribute('data-e2e') || ''),
            el.getAttribute && (el.getAttribute('aria-label') || ''),
            el.getAttribute && (el.getAttribute('placeholder') || ''),
            el.getAttribute && (el.getAttribute('data-placeholder') || ''),
            el.getAttribute && (el.getAttribute('name') || ''),
            el.getAttribute && (el.getAttribute('type') || ''),
            el.id || '',
            String(el.className || ''),
            el.tagName || '',
        ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    };

    const badMarkers = [
        'search', 'search-input', 'searchbar', 'search bar', 'find creators',
        'captcha', 'verification', 'security check', 'security verification',
        'challenge', 'drag the slider', 'slide to verify', 'fit the puzzle',
        'login', 'log in', 'sign in', 'sign-in', 'signup', 'sign up',
        'phone / email', 'email / username', 'continue with google',
        'continue with facebook', 'continue with apple', 'password',
    ];
    const commentMarkers = [
        'comment', 'comment-input', 'commentinput', 'comments', 'reply',
        'say something', 'add a comment',
    ];

    const insideBadSurface = (el) => {
        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            const label = labelOf(cur);
            if (authTokenLike(label) || badMarkers.some(marker => label.includes(marker))) return true;
            if (depth <= 3) {
                const role = cur.getAttribute && (cur.getAttribute('role') || '').toLowerCase();
                const modalLike = role.includes('dialog') || label.includes('modal') || label.includes('challenge') || authTokenLike(label);
                if (modalLike) {
                    const text = (cur.innerText || cur.textContent || '').toLowerCase();
                    if (authTokenLike(text) || badMarkers.some(marker => text.includes(marker))) return true;
                }
            }
        }
        return false;
    };

    const hasCommentScope = (el) => {
        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            const label = labelOf(cur);
            if (authTokenLike(label) || badMarkers.some(marker => label.includes(marker))) return false;
            if (commentMarkers.some(marker => label.includes(marker))) return true;
        }
        return false;
    };

    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );

    const pickEditor = (raw) => {
        if (!raw) return null;
        if (isRealEditor(raw)) return raw;
        return raw.querySelector && raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input'
        );
    };

    const rectFor = (editor, raw) => {
        const rects = [];
        for (const node of [raw, editor && editor.parentElement, editor]) {
            if (node && node.getBoundingClientRect) {
                const rect = node.getBoundingClientRect();
                if (rect) rects.push(rect);
            }
        }
        rects.sort((a, b) => (b.width * b.height) - (a.width * a.height));
        return rects[0] || null;
    };

    const looksLikeSearchOrAuthInput = (el, raw) => {
        const labels = [labelOf(el), labelOf(raw), labelOf(el && el.parentElement), labelOf(raw && raw.parentElement)].join(' ');
        if (badMarkers.some(marker => labels.includes(marker))) return true;
        const name = (el && el.getAttribute && (el.getAttribute('name') || '')).toLowerCase();
        const type = (el && el.getAttribute && (el.getAttribute('type') || '')).toLowerCase();
        return name === 'q' || name === 'query' || ['search', 'password', 'email', 'tel'].includes(type);
    };

    const commentSurfaceVisible = (() => {
        const selectors = [
            '[data-e2e="comment-list"]',
            '[data-e2e="comment-list-container"]',
            '[class*="CommentList"]',
            '[class*="DivCommentListContainer"]',
            '[class*="CommentContainer"]',
            '[data-e2e*="comment-level"]',
            '[data-e2e*="comment-username"]',
            '[data-cid]:not([data-cid^="one-column-item"])',
            '[data-comment-id]',
        ];
        for (const selector of selectors) {
            try {
                const node = document.querySelector(selector);
                if (visible(node)) return true;
            } catch (_) {}
        }
        return false;
    })();

    const rawCandidates = [];
    const addCandidates = (selector) => {
        try { rawCandidates.push(...document.querySelectorAll(selector)); } catch (_) {}
    };
    addCandidates('[data-smart-id="comment-input"]');
    addCandidates('[data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"]');
    addCandidates('[data-e2e*="comment" i] div[contenteditable="true"], [data-e2e*="comment" i] [contenteditable="true"], [data-e2e*="comment" i] textarea, [data-e2e*="comment" i] [role="textbox"]');
    addCandidates('[class*="Comment" i] div[contenteditable="true"], [class*="Comment" i] [contenteditable="true"], [class*="Comment" i] textarea, [class*="Comment" i] [role="textbox"]');
    addCandidates('div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea');

    const seen = new Set();
    const candidates = [];
    for (const raw of rawCandidates) {
        if (!raw || seen.has(raw) || insideBadSurface(raw)) continue;
        seen.add(raw);

        const editor = pickEditor(raw);
        if (!editor || insideBadSurface(editor) || looksLikeSearchOrAuthInput(editor, raw)) continue;

        const rect = rectFor(editor, raw);
        if (!rect || rect.width < 40 || rect.height < 4) continue;
        if (!visible(editor) && !visible(raw) && !(editor.parentElement && visible(editor.parentElement))) continue;

        const rawLabel = labelOf(raw);
        const editorLabel = labelOf(editor);
        const parentLabel = labelOf(editor.parentElement);
        const labels = `${rawLabel} ${editorLabel} ${parentLabel}`;
        const scoped = hasCommentScope(editor) || hasCommentScope(raw);
        const placeholderLike =
            labels.includes('add a comment') ||
            labels.includes('say something') ||
            labels.includes('comment');
        const lowerScreen = rect.top >= window.innerHeight * 0.25;

        let score = 0;
        if (labels.includes('data-smart-id comment-input') || rawLabel.includes('comment-input')) score += 9000;
        if (rawLabel.includes('data-e2e comment-input') || rawLabel.includes('comment-input')) score += 7000;
        if (editorLabel.includes('comment') || parentLabel.includes('comment')) score += 3500;
        if (placeholderLike) score += 2500;
        if (scoped) score += 1800;
        if (commentSurfaceVisible) score += 1200;
        if (lowerScreen) score += 350;
        if (visible(editor)) score += 200;

        if (!scoped && !placeholderLike && !commentSurfaceVisible) continue;
        if (score < 1200) continue;

        candidates.push({
            editor,
            raw,
            score,
            strategy: score >= 9000 ? 'unified-stable-comment-input' : 'unified-comment-input',
            top: rect.top,
        });
    }

    candidates.sort((a, b) => b.score - a.score || b.top - a.top);
    const selected = candidates[0];
    if (!selected) {
        finish({ found: false, reason: commentSurfaceVisible ? 'comment_input_not_found' : 'comment_surface_not_ready' });
        return null;
    }

    try { selected.editor.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
    try { selected.editor.setAttribute('data-smart-input-resolver', 'comment'); } catch (_) {}
    try { selected.editor.setAttribute('data-smart-input-resolver-strategy', selected.strategy); } catch (_) {}
    try { selected.editor.setAttribute('data-smart-input-resolver-score', String(selected.score || 0)); } catch (_) {}
    finish({
        found: true,
        strategy: selected.strategy,
        score: selected.score,
        commentSurfaceVisible,
    });
    return selected.editor;
}
"""

_JS_FIND_POST_BUTTON = r"""
() => {
    document
        .querySelectorAll('[data-smart-id="comment-post"], [data-smart-id="comment-post-ready"]')
        .forEach(el => el.removeAttribute('data-smart-id'));

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const disabled = (el) => {
        if (!el) return true;
        const st = getComputedStyle(el);
        return el.hasAttribute('disabled') ||
               el.getAttribute('aria-disabled') === 'true' ||
               el.classList.contains('disabled') ||
               parseFloat(st.opacity || '1') < 0.5;
    };

    const clickTargetFor = (raw, opts = {}) => {
        if (!raw) return null;
        const allowUpperViewport = Boolean(opts.allowUpperViewport);
        const candidates = [
            raw,
            raw.closest('button'),
            raw.closest('[role="button"]'),
            raw.closest('div[tabindex="0"]'),
            raw.parentElement,
            raw.parentElement && raw.parentElement.parentElement,
        ].filter(Boolean);
        const seen = new Set();
        for (const el of candidates) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 360 || r.height > 140) continue;
            if (!allowUpperViewport && r.top < window.innerHeight * 0.25) continue;
            const st = getComputedStyle(el);
            if (st.pointerEvents === 'none') continue;
            if (disabled(el)) continue;
            return el;
        }
        return null;
    };

    const dataE2e = document.querySelector(
        '[data-e2e="comment-post"], [data-e2e*="comment-post"], [data-e2e="post-comment-button"]'
    );
    if (dataE2e) {
        const target = clickTargetFor(dataE2e, {allowUpperViewport: true});
        if (target) {
            target.setAttribute('data-smart-id', 'comment-post');
            return {
                found: true,
                strategy: target === dataE2e ? 'data-e2e' : 'data-e2e-clickable-ancestor',
                rawPointerEvents: getComputedStyle(dataE2e).pointerEvents,
                targetTag: target.tagName,
                targetPointerEvents: getComputedStyle(target).pointerEvents,
            };
        }
        if (visible(dataE2e)) {
            dataE2e.setAttribute('data-smart-id', 'comment-post');
            return {
                found: true,
                strategy: 'data-e2e-pointer-events-none',
                rawPointerEvents: getComputedStyle(dataE2e).pointerEvents,
                targetTag: dataE2e.tagName,
                targetPointerEvents: getComputedStyle(dataE2e).pointerEvents,
            };
        }
    }

    const postKeywords = ['post', 'send', 'submit', 'опублик', 'отправ', 'отпр'];
    const buttons = [
        ...document.querySelectorAll('button'),
        ...document.querySelectorAll('[role="button"]'),
        ...document.querySelectorAll('div[tabindex="0"]'),
    ];
    for (const el of buttons) {
        const text = (((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).trim()).toLowerCase();
        if (postKeywords.some(k => text.includes(k))) {
            const target = clickTargetFor(el);
            if (target) {
                target.setAttribute('data-smart-id', 'comment-post');
                return { found: true, strategy: 'text-heuristic' };
            }
        }
    }
    const allPostLike = document.querySelectorAll('[data-e2e*="comment-post"], [data-e2e*="post"]');
    const diag = [];
    for (const el of allPostLike) {
        const r = el.getBoundingClientRect();
        diag.push({
            e2e: el.getAttribute('data-e2e'),
            tag: el.tagName,
            w: Math.round(r.width),
            h: Math.round(r.height),
            disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
            opacity: getComputedStyle(el).opacity,
            pointerEvents: getComputedStyle(el).pointerEvents,
            text: (el.textContent || '').trim().substring(0, 20),
        });
    }
    return { found: false, diagnostic: diag };
}
"""

_JS_FIND_REPLY_INPUT = r"""
([parentAnchorToken]) => {
    document
        .querySelectorAll('[data-smart-reply-input], [data-smart-reply-composer]')
        .forEach(el => {
            el.removeAttribute('data-smart-reply-input');
            el.removeAttribute('data-smart-reply-composer');
        });

    const targetAnchorToken = String(parentAnchorToken || '').trim();

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const pickEditable = (root) => {
        if (!root) return null;
        if (root.matches && root.matches('textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]')) {
            return root;
        }
        return root.querySelector && root.querySelector(
            'textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]'
        );
    };

    const labelOf = (el) => [
        (el && (el.innerText || el.textContent)) || '',
        el && el.getAttribute && (el.getAttribute('placeholder') || ''),
        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
        el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
        el && String(el.className || ''),
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();

    const wrapperRect = (editor, raw) => {
        const editorRect = editor && editor.getBoundingClientRect ? editor.getBoundingClientRect() : null;
        const rawRect = raw && raw.getBoundingClientRect ? raw.getBoundingClientRect() : null;
        const parentRect = editor && editor.parentElement && editor.parentElement.getBoundingClientRect
            ? editor.parentElement.getBoundingClientRect()
            : null;
        const rects = [rawRect, parentRect, editorRect].filter(Boolean);
        return rects.sort((a, b) => (b.width * b.height) - (a.width * a.height))[0] || editorRect || rawRect;
    };

    const anchor = targetAnchorToken
        ? [...document.querySelectorAll('[data-smart-parent-anchor]')]
            .find(el => (el.getAttribute('data-smart-parent-anchor') || '') === targetAnchorToken)
        : null;
    const anchorMissing = Boolean(targetAnchorToken && !anchor);
    const anchorRect = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;

    const rawCandidates = [
        ...document.querySelectorAll('[data-e2e*="reply" i] [contenteditable="true"], [data-e2e*="reply" i] textarea, [data-e2e*="reply" i] [role="textbox"]'),
        ...document.querySelectorAll('[aria-label*="reply" i] [contenteditable="true"], [class*="reply" i] [contenteditable="true"]'),
        ...document.querySelectorAll('[data-e2e="comment-input"], [data-e2e*="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'),
    ];

    const seen = new Set();
    const candidates = [];
    for (const raw of rawCandidates) {
        const editor = pickEditable(raw);
        if (!editor || seen.has(editor)) continue;
        seen.add(editor);
        if (!visible(editor) && !(raw && visible(raw)) && !(editor.parentElement && visible(editor.parentElement))) continue;
        const rect = wrapperRect(editor, raw);
        if (!rect || rect.width < 40 || rect.height < 4) continue;
        const label = `${labelOf(editor)} ${labelOf(raw)} ${editor.parentElement ? labelOf(editor.parentElement) : ''}`;
        let score = 0;
        if (label.includes('reply') || label.includes('replying') || label.includes('ответ') || label.includes('відпов')) score += 5000;
        const text = ((editor.innerText || editor.textContent || '')).trim();
        if (text.startsWith('@') || (text.length <= 100 && text.includes('@'))) score += 3000;
        if (raw && raw.getAttribute && String(raw.getAttribute('data-e2e') || '').toLowerCase().includes('comment-input')) score += 1000;
        if (anchorRect) {
            const verticalNear =
                rect.top >= anchorRect.top - 40 &&
                rect.top <= Math.max(anchorRect.bottom + 520, anchorRect.top + 120);
            const horizontalOverlap =
                rect.right >= anchorRect.left - 60 &&
                rect.left <= anchorRect.right + 500;
            if (verticalNear && horizontalOverlap) score += 8000;
            if (anchor && (anchor.contains(editor) || anchor.contains(raw))) score += 12000;
        }
        if (rect.top > window.innerHeight * 0.35) score += 250;
        candidates.push({ editor, raw, score, rectTop: rect.top });
    }

    candidates.sort((a, b) => b.score - a.score || b.rectTop - a.rectTop);
    const selected = candidates[0];
    if (!selected || selected.score <= 0) {
        return { found: false, reason: 'reply_input_not_found' };
    }
    // Anti "reply-as-main" guard: in a reply flow we always pass a parent anchor
    // token. Only accept an input that is actually associated with the reply —
    // i.e. near/inside the parent row (+8000/+12000), labelled as a reply
    // (+5000) or pre-filled with the "@mention" reply indicator (+3000). A bare
    // global comment composer scores ~1250; accepting it would type the reply
    // into the main comment box and publish it as a top-level comment. Rejecting
    // here is safe: the caller retries instead of posting to the wrong place.
    if (targetAnchorToken && selected.score < 3000) {
        return {
            found: false,
            reason: anchorMissing ? 'parent_anchor_not_found' : 'reply_input_not_scoped_to_parent',
        };
    }

    const editor = selected.editor;
    const composer = (() => {
        const selectors = [
            '[data-e2e*="reply" i]',
            '[class*="reply" i]',
            '[data-e2e*="comment" i]',
            '[class*="Comment" i]',
        ];
        for (const sel of selectors) {
            try {
                const node = editor.closest(sel);
                if (node && visible(node)) {
                    const r = node.getBoundingClientRect();
                    const panelLike = r.width > Math.max(620, window.innerWidth * 0.34) ||
                        r.height > Math.max(360, window.innerHeight * 0.45);
                    if (!panelLike) return node;
                }
            } catch (_) {}
        }
        return editor.parentElement || editor;
    })();
    try { editor.setAttribute('data-smart-reply-input', '1'); } catch (_) {}
    try { editor.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
    try { composer.setAttribute('data-smart-reply-composer', '1'); } catch (_) {}
    return {
        found: true,
        strategy: selected.score >= 8000 ? 'reply-scoped-anchor-input' : 'reply-scoped-input',
        score: selected.score,
    };
}
"""

_JS_FIND_REPLY_INPUT_HANDLE = (
    "([parentAnchorToken]) => {\n"
    "    const probe = " + _JS_FIND_REPLY_INPUT.strip() + ";\n"
    "    let result = null;\n"
    "    try {\n"
    "        result = probe([parentAnchorToken || '']);\n"
    "    } catch (exc) {\n"
    "        result = { found: false, reason: 'reply_probe_exception', error: String(exc && (exc.message || exc)) };\n"
    "    }\n"
    "    try { window.__smartReplyInputProbeState = result || null; } catch (_) {}\n"
    "    if (!result || !result.found) return null;\n"
    "    const marked = document.querySelector('[data-smart-reply-input=\"1\"]');\n"
    "    if (!marked) {\n"
    "        try { window.__smartReplyInputProbeState = { ...result, found: false, reason: 'reply_marked_input_missing_after_probe' }; } catch (_) {}\n"
    "        return null;\n"
    "    }\n"
    "    try { marked.setAttribute('data-smart-reply-strategy', result.strategy || 'reply-scoped-handle'); } catch (_) {}\n"
    "    try { marked.setAttribute('data-smart-reply-score', String(result.score || 0)); } catch (_) {}\n"
    "    return marked;\n"
    "}\n"
)

# Atomic "set comment text" primitive. ONE page.evaluate round-trip:
#   pick editor (global composer OR reply-scoped editor) -> focus -> clear ->
#   insert via execCommand('insertText') with beforeinput/input fallback ->
#   place caret at end -> verify the text actually landed (reject placeholder
#   leak) -> tag editor with data-smart-id="comment-input" -> return
#   {ok, text, reason, placeholder_leak, scoped}.
#
# This is the proven body of the old _js_insert_text fallback, promoted to the
# primary path so the happy path is ~1 round-trip and survives latency.
#
# REPLY SCOPING: when replyScoped is true the editor is selected ONLY via the
# existing _JS_FIND_REPLY_INPUT anchor-token mechanism (embedded below). It will
# NEVER fall back to the global composer, preserving the "reply must not post as
# a main comment" protection. When replyScoped is false the global composer
# picker is used.
_JS_ATOMIC_SET_COMMENT_TEXT = r"""
([insertText, expected, anchorToken, replyScoped, focusOnly]) => {
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 20 && r.height > 10 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const labelOf = (el) => {
        const parts = [
            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
            el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
            el && (el.id || ''),
            el && String(el.className || ''),
            el && (el.tagName || ''),
        ];
        return parts.join(' ').toLowerCase();
    };
    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );
    const isCommentScoped = (el) => {
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
            const label = labelOf(cur);
            if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return false;
            if (label.includes('comment') || label.includes('reply')) return true;
        }
        return false;
    };
    const fireInputEvents = (el, data) => {
        const evs = [
            new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data}),
            new InputEvent('input', {bubbles:true, cancelable:true, inputType:'insertText', data}),
            new Event('change', {bubbles:true, cancelable:true}),
            new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:data.slice(-1) || 'a'}),
            new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:data.slice(-1) || 'a'})
        ];
        for (const ev of evs) {
            try { el.dispatchEvent(ev); } catch (_) {}
        }
    };
    const setNativeValue = (el, val) => {
        const proto =
            el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype :
            el.tagName === 'INPUT' ? window.HTMLInputElement.prototype :
            null;
        if (!proto) return false;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, val);
        else el.value = val;
        return true;
    };
    const globalPick = () => {
        const commentSelectors = [
            '[data-smart-id="comment-input"]',
            '[data-e2e="comment-input"]',
            '[data-e2e*="comment-input"]',
            '[data-e2e*="CommentInput"]'
        ];
        for (const sel of commentSelectors) {
            const raw = document.querySelector(sel);
            if (!raw || !visible(raw)) continue;
            const inner = raw.querySelector && raw.querySelector(
                'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
            );
            const el = inner || raw;
            if (!isRealEditor(el)) continue;
            return el;
        }
        const broadSelectors = [
            'div[contenteditable="true"]',
            '[contenteditable="true"]',
            'textarea',
            'input',
            '[role="textbox"]'
        ];
        for (const sel of broadSelectors) {
            for (const el of document.querySelectorAll(sel)) {
                if (!visible(el) || !isRealEditor(el) || !isCommentScoped(el)) continue;
                return el;
            }
        }
        return null;
    };

    let el = null;
    const token = String(anchorToken || '').trim();
    if (replyScoped) {
        const replyProbe = __REPLY_PROBE__;
        let res = null;
        try {
            res = replyProbe([token]);
        } catch (exc) {
            res = { found: false, reason: 'reply_probe_exception' };
        }
        if (!res || !res.found) {
            return { ok: false, reason: (res && res.reason) || 'no_editor', scoped: 'reply' };
        }
        // The reply probe tags the scoped editor; never fall back to global.
        el = document.querySelector('[data-smart-reply-input="1"]');
        if (!el) {
            return { ok: false, reason: 'reply_marked_input_missing', scoped: 'reply' };
        }
    } else {
        el = globalPick();
        if (!el) {
            return { ok: false, reason: 'no_editor', scoped: 'global' };
        }
    }

    try { el.scrollIntoView({block:'center', inline:'center'}); } catch (_) {}
    try { el.click(); } catch (_) {}
    try { el.focus(); } catch (_) {}

    // Resolve the REAL inner editor before writing. `el` might be a wrapper.
    const real = (el.isContentEditable || (el.getAttribute && el.getAttribute('contenteditable') === 'true') || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')
        ? el
        : (el.querySelector && el.querySelector('div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input')) || el;

    try { real.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}

    if (focusOnly) {
        try {
            const range = document.createRange();
            range.selectNodeContents(real);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        } catch (_) {}
        return {
            ok: true,
            reason: 'focused',
            text: '',
            placeholder_leak: false,
            scoped: replyScoped ? 'reply' : 'global'
        };
    }

    if (real.tagName === 'TEXTAREA' || real.tagName === 'INPUT') {
        setNativeValue(real, insertText);
        fireInputEvents(real, insertText);
    } else {
        try { real.focus(); } catch (_) {}
        try {
            real.textContent = '';
            const range = document.createRange();
            range.selectNodeContents(real);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        } catch (_) {}

        let execOk = false;
        try { execOk = document.execCommand('insertText', false, insertText); } catch (_) {}

        fireInputEvents(real, insertText);

        // Leave caret at the end so a trusted trailing keystroke (if any) appends.
        try {
            const range = document.createRange();
            range.selectNodeContents(real);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        } catch (_) {}
    }

    const placeholderMarkers = [
        'add comment',
        'add a comment',
        'say something',
        'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c',
        'добавьте комментар',
        'Напишите комментар',
        'комментарий...'
    ];
    const rawText = (real.value !== undefined && real.value !== null && real.value !== '')
        ? real.value
        : (real.textContent || real.innerText || '');
    const txt = rawText.trim();
    const txtLower = txt.toLowerCase();
    const startsWithPlaceholder = placeholderMarkers.some(m => txtLower.startsWith(m));
    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const ok = Boolean(
        txt &&
        !startsWithPlaceholder &&
        normalizeText(txt) === normalizeText(expected)
    );
    return {
        ok: ok,
        reason: ok ? 'inserted' : 'verify_failed',
        text: txt.slice(0, 120),
        placeholder_leak: startsWithPlaceholder,
        scoped: replyScoped ? 'reply' : 'global'
    };
}
""".replace("__REPLY_PROBE__", _JS_FIND_REPLY_INPUT.strip())


async def atomic_set_comment_text(
    page: Page,
    text: str,
    *,
    reply_scoped: bool = False,
    anchor_token: Optional[str] = None,
    insert_text: Optional[str] = None,
    focus_only: bool = False,
) -> dict[str, Any]:
    """Atomically focus, clear and insert text into the comment/reply editor.

    Single page.evaluate round-trip. Returns the JS result dict, e.g.
    {ok, reason, text, placeholder_leak, scoped}. On any exception returns
    {ok: False, reason: 'evaluate_exception'}.

    When ``focus_only`` is True the editor is located and focused only (no
    insert) so a trusted ``keyboard.insert_text`` path can update React state.

    When ``insert_text`` is provided it is what actually gets inserted in-page,
    while ``text`` remains the full expected string used for verification.
    """
    payload_insert = "" if focus_only else (text if insert_text is None else insert_text)
    try:
        result = await page.evaluate(
            _JS_ATOMIC_SET_COMMENT_TEXT,
            [payload_insert, text, anchor_token or "", bool(reply_scoped), bool(focus_only)],
        )
        if isinstance(result, dict):
            return result
        return {"ok": False, "reason": "non_dict_result"}
    except Exception as exc:
        logger.debug("[DOM] atomic_set_comment_text evaluate failed: %s", str(exc)[:160])
        return {"ok": False, "reason": "evaluate_exception"}


_JS_FIND_REPLY_POST_BUTTON = r"""
() => {
    document
        .querySelectorAll('[data-smart-reply-post], [data-smart-id="comment-post"], [data-smart-id="comment-post-ready"]')
        .forEach(el => {
            el.removeAttribute('data-smart-reply-post');
            el.removeAttribute('data-smart-id');
        });

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const disabled = (el) => {
        if (!el) return true;
        const st = getComputedStyle(el);
        return el.hasAttribute('disabled') ||
            el.getAttribute('aria-disabled') === 'true' ||
            el.classList.contains('disabled') ||
            parseFloat(st.opacity || '1') < 0.5;
    };
    const clickTargetFor = (raw) => {
        if (!raw) return null;
        const candidates = [
            raw,
            raw.closest('button'),
            raw.closest('[role="button"]'),
            raw.closest('div[tabindex="0"]'),
            raw.parentElement,
            raw.parentElement && raw.parentElement.parentElement,
        ].filter(Boolean);
        const seen = new Set();
        for (const el of candidates) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 360 || r.height > 140) continue;
            if (getComputedStyle(el).pointerEvents === 'none') continue;
            if (disabled(el)) continue;
            return el;
        }
        return null;
    };
    const labelOf = (el) => [
        (el && (el.innerText || el.textContent)) || '',
        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
        el && el.getAttribute && (el.getAttribute('title') || ''),
        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    const usableComposerRoot = (el) => {
        if (!visible(el)) return false;
        const r = el.getBoundingClientRect();
        return r.width <= Math.max(720, window.innerWidth * 0.38) &&
            r.height <= Math.max(420, window.innerHeight * 0.50);
    };

    const input = document.querySelector('[data-smart-reply-input="1"]') ||
        document.querySelector('[data-smart-id="comment-input"]');
    const roots = [];
    const composer = document.querySelector('[data-smart-reply-composer="1"]');
    if (composer && usableComposerRoot(composer)) roots.push({ root: composer, strategy: 'reply-composer' });
    if (input) {
        for (let cur = input; cur && cur !== document.body && cur !== document.documentElement && roots.length < 8; cur = cur.parentElement) {
            if (!usableComposerRoot(cur)) continue;
            roots.push({ root: cur, strategy: 'reply-input-ancestor' });
        }
    }

    const seenRoots = new Set();
    for (const item of roots) {
        const root = item.root;
        if (!root || seenRoots.has(root) || !visible(root)) continue;
        seenRoots.add(root);
        const rawButtons = [
            ...root.querySelectorAll('[data-e2e="comment-post"], [data-e2e*="comment-post"], [data-e2e="post-comment-button"]'),
            ...root.querySelectorAll('button, [role="button"], div[tabindex="0"]'),
        ];
        for (const raw of rawButtons) {
            const label = labelOf(raw);
            const looksPost =
                label.includes('comment-post') ||
                label.includes('post') ||
                label.includes('send') ||
                label.includes('submit') ||
                label.includes('опублик') ||
                label.includes('отправ');
            if (!looksPost) continue;
            if (label.includes('reply') && !label.includes('post')) continue;
            const target = clickTargetFor(raw);
            if (!target) continue;
            target.setAttribute('data-smart-reply-post', '1');
            target.setAttribute('data-smart-id', 'comment-post');
            return {
                found: true,
                strategy: item.strategy,
                targetTag: target.tagName,
                rawPointerEvents: getComputedStyle(raw).pointerEvents,
                targetPointerEvents: getComputedStyle(target).pointerEvents,
            };
        }
    }

    return { found: false, reason: input ? 'reply_post_not_found_near_input' : 'reply_input_missing_for_post' };
}
"""

_JS_FIND_REPLY_POST_BUTTON_HANDLE = (
    "() => {\n"
    "    const probe = " + _JS_FIND_REPLY_POST_BUTTON.strip() + ";\n"
    "    let result = null;\n"
    "    try {\n"
    "        result = probe();\n"
    "    } catch (exc) {\n"
    "        result = { found: false, reason: 'reply_post_probe_exception', error: String(exc && (exc.message || exc)) };\n"
    "    }\n"
    "    try { window.__smartReplyPostProbeState = result || null; } catch (_) {}\n"
    "    if (!result || !result.found) return null;\n"
    "    const marked = document.querySelector('[data-smart-reply-post=\"1\"], [data-smart-id=\"comment-post\"]');\n"
    "    if (!marked) {\n"
    "        try { window.__smartReplyPostProbeState = { ...result, found: false, reason: 'reply_marked_post_missing_after_probe' }; } catch (_) {}\n"
    "        return null;\n"
    "    }\n"
    "    try { marked.setAttribute('data-smart-reply-post-strategy', result.strategy || 'reply-post-handle'); } catch (_) {}\n"
    "    try { marked.setAttribute('data-smart-reply-post-target', result.targetTag || marked.tagName || ''); } catch (_) {}\n"
    "    return marked;\n"
    "}\n"
)

_JS_OPEN_COMMENTS = r"""
() => {
    const existing = document.querySelector(
        '[data-e2e="comment-input"], [data-smart-id="comment-input"], ' +
        'div[contenteditable="true"][data-e2e*="comment"]'
    );
    if (existing) {
        const r = existing.getBoundingClientRect();
        if (r.width > 20 && r.height > 10) {
            return { already_open: true };
        }
    }

    let listVisible = false;
    const commentList = document.querySelector(
        '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
        '[class*="CommentList"], [data-e2e*="comment-container"]'
    );
    if (commentList) {
        const r = commentList.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            listVisible = true;
        }
    }

    const commentTriggerMarkers = [
        'comment',
        'comments',
        'reply',
        'replies',
        'komment',
        'coment',
        'comentario',
        'commentaire',
        'yorum',
        '\u043a\u043e\u043c\u043c\u0435\u043d\u0442',
        '\u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c \u0438\u043b\u0438 \u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c',
        '\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
        'коммент',
        'комментар',
        'комментарии',
        'прочитать или оставить',
        'оставить комментар'
    ];
    const blockedTriggerMarkers = [
        'input',
        'textbox',
        'text box',
        'list',
        'container',
        'post',
        'send',
        'search',
        'share',
        'like',
        'follow',
        'report',
        'login',
        'log in'
    ];
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const labelOf = (el) => {
        const parts = [
            el.getAttribute('data-e2e') || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('title') || '',
            el.getAttribute('role') || '',
            String(el.className || ''),
            String(el.id || ''),
            (el.innerText || el.textContent || '').slice(0, 120),
        ];
        return parts.join(' ').replace(/\s+/g, ' ').toLowerCase();
    };
    const commentTabPatterns = [
        /^comments?(\s+\d+)?$/,
        /^commentaires?(\s+\d+)?$/,
        /^comentarios?(\s+\d+)?$/,
        /^kommentare?(\s+\d+)?$/,
        /^\u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440(\u0438\u0438|\u0438\u0439|\u0438\u0435\u0432)?(\s+\d+)?$/,
        /^\u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440(\u0456|\u0456\u0432)?(\s+\d+)?$/,
        /^comentários?(\s+\d+)?$/,
        /^комментар(ии|ий|иев)(\s+\d+)?$/,
    ];
    const normalizeTabLabel = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const commentTabLabelCandidates = (el) => {
        const out = [];
        const push = (value) => {
            const normalized = normalizeTabLabel(value);
            if (normalized && !out.includes(normalized)) out.push(normalized);
        };
        push(el.getAttribute('aria-label') || '');
        push(el.getAttribute('title') || '');
        const text = el.innerText || el.textContent || '';
        for (const part of String(text).split(/[\n\r\t]+/)) push(part);
        push(text);
        return out;
    };
    const isCommentTabLabel = (label) => {
        if (commentTabPatterns.some((pattern) => pattern.test(label))) return true;
        return /^comments?\b/.test(label) && !/\brepl(y|ies)\b/.test(label) && label.length <= 40;
    };
    const exactCommentTabChild = (el) => {
        for (const child of Array.from(el.querySelectorAll('[role="tab"], button, [role="button"], a, div[tabindex="0"], span'))) {
            if (!visible(child)) continue;
            if (commentTabLabelCandidates(child).some((label) => commentTabPatterns.some((pattern) => pattern.test(label)))) {
                return child;
            }
        }
        return null;
    };
    for (const tab of Array.from(document.querySelectorAll('[role="tab"], button, [role="button"], a, div[tabindex="0"], span'))) {
        if (!visible(tab)) continue;
        const labelCandidates = commentTabLabelCandidates(tab);
        const rawLabel = labelCandidates[0] || '';
        if (!labelCandidates.length) continue;
        if (!labelCandidates.some(isCommentTabLabel)) continue;
        const exactChild = exactCommentTabChild(tab);
        let clickTarget = exactChild
            ? (exactChild.closest('[role="tab"], button, [role="button"], a, div[tabindex="0"]') || exactChild)
            : (tab.closest('[role="tab"], button, [role="button"], a, div[tabindex="0"]') || tab);
        if (!visible(clickTarget)) continue;
        clickTarget.setAttribute('data-smart-id', 'comment-open-trigger');
        const cr = clickTarget.getBoundingClientRect();
        return {
            click: true,
            comment_tab: true,
            target_tag: clickTarget.tagName,
            target_aria: clickTarget.getAttribute('aria-label'),
            target_text: (clickTarget.innerText || clickTarget.textContent || '').trim().slice(0, 80),
            x: cr.x,
            y: cr.y,
            w: cr.width,
            h: cr.height,
        };
    }
    const candidateSet = new Set([
        ...document.querySelectorAll('[data-e2e*="comment"], [data-e2e*="Comment"]'),
        ...document.querySelectorAll('button, [role="button"], a, div[tabindex="0"]')
    ]);
    const candidates = Array.from(candidateSet);
    let bestTrigger = null;
    let bestScore = -1;

    for (const el of candidates) {
        const e2e = (el.getAttribute('data-e2e') || '').toLowerCase();
        const tag = el.tagName;
        const r = el.getBoundingClientRect();
        const isVisible = visible(el);
        const label = labelOf(el);
        const hasCommentMarker = commentTriggerMarkers.some(marker => label.includes(marker));
        const isButtonLike = tag === 'BUTTON' || el.getAttribute('role') === 'button' || el.closest('button, [role="button"]');

        if (!hasCommentMarker) continue;
        if (blockedTriggerMarkers.some(marker => label.includes(marker))) continue;
        if (r.width > window.innerWidth * 0.8 && r.height > window.innerHeight * 0.4) continue;

        let score = 0;
        if (isButtonLike) score += 3;
        if (isVisible) score += 5;
        if (e2e === 'comments') score += 2;
        if (e2e === 'comment-icon') score += 4;
        if ((el.getAttribute('aria-label') || '').toLowerCase().includes('comment')) score += 3;
        if (tag === 'BUTTON') score += 2;
        if (tag === 'SVG' || tag === 'SPAN' || tag === 'STRONG' || tag === 'I') score += 1;
        if (!isVisible) score -= 10;

        if (score > bestScore) {
            bestScore = score;
            bestTrigger = el;
        }
    }

    if (bestTrigger && bestScore > 0) {
        try {
            let clickTarget = bestTrigger;
            const tag = bestTrigger.tagName;
            if (tag === 'SPAN' || tag === 'STRONG' || tag === 'I' || tag === 'SVG') {
                const parent = bestTrigger.closest('button, [role="button"], div[tabindex], a');
                if (parent) {
                    const pr = parent.getBoundingClientRect();
                    if (pr.width > 0 && pr.height > 0) {
                        clickTarget = parent;
                    }
                }
            }
            if ((clickTarget.getAttribute('aria-expanded') || '').toLowerCase() === 'true' || listVisible) {
                return {
                    input_pending: true,
                    list_visible: true,
                    expanded: (clickTarget.getAttribute('aria-expanded') || null),
                    target_e2e: bestTrigger.getAttribute('data-e2e'),
                    target_tag: clickTarget.tagName,
                    target_aria: clickTarget.getAttribute('aria-label'),
                };
            }
            clickTarget.setAttribute('data-smart-id', 'comment-open-trigger');
            const cr = clickTarget.getBoundingClientRect();
            return {
                click: true,
                target_e2e: bestTrigger.getAttribute('data-e2e'),
                target_tag: clickTarget.tagName,
                target_aria: clickTarget.getAttribute('aria-label'),
                target_text: (clickTarget.innerText || clickTarget.textContent || '').trim().slice(0, 80),
                x: cr.x,
                y: cr.y,
                w: cr.width,
                h: cr.height,
            };
        } catch (e) {
            return { clicked: false, error: String(e) };
        }
    }

    try {
        window.scrollBy(0, 400);
    } catch (e) {}

    return {
        found_trigger: false,
        list_visible: listVisible,
        input_pending: listVisible,
        candidates_count: candidates.length
    };
}
"""

_JS_OPEN_COMMENTS_MAIN_FAST = r"""
() => {
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const inputSelectors = [
        '[data-smart-id="comment-input"]',
        '[data-e2e="comment-input"]',
        '[data-e2e*="comment-input"]',
        '[data-e2e*="CommentInput"]',
        'div[contenteditable="true"][data-e2e*="comment"]',
    ].join(', ');
    const existing = document.querySelector(inputSelectors);
    if (visible(existing)) {
        return { already_open: true, input_pending: true, action: 'main_fast_existing_input' };
    }

    const list = document.querySelector(
        '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
        '[class*="CommentList"], [class*="DivCommentListContainer"]'
    );
    const listVisible = visible(list);

    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const labelsOf = (el) => {
        const out = [];
        const push = (value) => {
            const label = normalize(value);
            if (label && !out.includes(label)) out.push(label);
        };
        push(el.getAttribute && el.getAttribute('aria-label'));
        push(el.getAttribute && el.getAttribute('title'));
        const text = String((el.innerText || el.textContent || '')).slice(0, 80);
        for (const part of text.split(/[\n\r\t]+/)) push(part);
        push(text);
        return out;
    };
    const isCommentLabel = (label) =>
        /^comments?(\s+\d+)?$/.test(label)
        || /^commentaires?(\s+\d+)?$/.test(label)
        || /^coment[a\u00e1]rios?(\s+\d+)?$/.test(label)
        || /^kommentare?(\s+\d+)?$/.test(label)
        || /^komentarze?(\s+\d+)?$/.test(label)
        || /^yorumlar?(\s+\d+)?$/.test(label)
        || /^\u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440(\u0438\u0438|\u0438\u0439|\u0438\u0435\u0432)?(\s+\d+)?$/.test(label)
        || /^\u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440(\u0456|\u0456\u0432)?(\s+\d+)?$/.test(label)
        || /^comments?\b/.test(label) && !/\brepl(y|ies)\b/.test(label) && label.length <= 40;

    const exactCommentChild = (el) => {
        const children = Array.from(el.querySelectorAll(
            'span, [role="tab"], button, [role="button"], a, div[tabindex="0"]'
        )).slice(0, 40);
        for (const child of children) {
            if (!visible(child)) continue;
            if (labelsOf(child).some(isCommentLabel)) return child;
        }
        return null;
    };

    const tabCandidates = Array.from(document.querySelectorAll(
        '[role="tab"], button, [role="button"], a, div[tabindex="0"]'
    )).filter(visible);
    for (const el of tabCandidates) {
        const exact = labelsOf(el).some(isCommentLabel) ? el : exactCommentChild(el);
        if (!exact) continue;
        const clickTarget = exact.closest('button, [role="button"], [role="tab"], a, div[tabindex="0"]') || exact;
        if (!visible(clickTarget)) continue;
        clickTarget.setAttribute('data-smart-id', 'comment-open-trigger');
        const r = clickTarget.getBoundingClientRect();
        return {
            click: true,
            comment_tab: true,
            main_fast: true,
            target_tag: clickTarget.tagName,
            target_aria: clickTarget.getAttribute('aria-label'),
            target_text: (clickTarget.innerText || clickTarget.textContent || '').trim().slice(0, 80),
            x: r.x,
            y: r.y,
            w: r.width,
            h: r.height,
        };
    }

    const iconCandidates = Array.from(document.querySelectorAll(
        '[data-e2e="comment-icon"], [data-e2e="comment-count"], [aria-label*="comment" i]'
    )).filter(visible);
    for (const el of iconCandidates) {
        const clickTarget = el.closest('button, [role="button"], a, div[tabindex="0"]') || el;
        if (!visible(clickTarget)) continue;
        clickTarget.setAttribute('data-smart-id', 'comment-open-trigger');
        const r = clickTarget.getBoundingClientRect();
        return {
            click: true,
            comment_icon: true,
            main_fast: true,
            target_tag: clickTarget.tagName,
            target_aria: clickTarget.getAttribute('aria-label'),
            target_text: (clickTarget.innerText || clickTarget.textContent || '').trim().slice(0, 80),
            x: r.x,
            y: r.y,
            w: r.width,
            h: r.height,
        };
    }

    return {
        found_trigger: false,
        list_visible: listVisible,
        input_pending: listVisible,
        main_fast: true,
    };
}
"""

_JS_COMMENT_SURFACE_STATE = r"""
() => {
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const visibleNodes = (root, selector) => {
        try {
            return Array.from(root.querySelectorAll(selector)).filter(visible);
        } catch (_) {
            return [];
        }
    };

    const inputSelectors = [
        '[data-e2e="comment-input"]',
        '[data-smart-id="comment-input"]',
        '[data-e2e*="CommentInput"]',
        '[data-e2e*="comment-input"]',
        'div[contenteditable="true"]',
        '[contenteditable="true"]',
        'textarea',
        '[role="textbox"]',
    ].join(', ');
    const commentInputLike = (el) => {
        if (!el) return false;
        const badMarkers = [
            'search',
            'captcha',
            'challenge',
            'verification',
            'login',
            'log in',
            'sign in',
            'sign-in',
            'signup',
            'sign up',
            'phone / email',
            'email / username',
            'continue with google',
            'continue with facebook',
            'continue with apple',
            'use phone',
            'password',
        ];
        const markers = [
            'comment-input',
            'commentinput',
            'comment input',
            'comment',
            'add a comment',
            'say something',
            'write a comment',
            'leave a comment',
            '\u043a\u043e\u043c\u043c\u0435\u043d\u0442',
        ];
        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
        let cur = el;
        for (let depth = 0; depth < 7 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            const label = [
                cur.getAttribute && (cur.getAttribute('data-smart-id') || ''),
                cur.getAttribute && (cur.getAttribute('data-e2e') || ''),
                cur.getAttribute && (cur.getAttribute('aria-label') || ''),
                cur.getAttribute && (cur.getAttribute('placeholder') || ''),
                cur.getAttribute && (cur.getAttribute('data-placeholder') || ''),
                cur.id || '',
                String(cur.className || ''),
            ].join(' ').toLowerCase();
            if (authTokenLike(label) || badMarkers.some(marker => label.includes(marker))) return false;
            const isDialogLike =
                label.includes('dialog')
                || label.includes('modal')
                || authTokenLike(label)
                || label.includes('login');
            if (isDialogLike) {
                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                if (authTokenLike(text) || badMarkers.some(marker => text.includes(marker))) return false;
            }
            if (markers.some(marker => label.includes(marker))) return true;
        }
        return false;
    };
    const listSelectors = [
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[class*="CommentList"]',
        '[class*="DivCommentListContainer"]',
        '[class*="CommentContainer"]',
    ].join(', ');
    const strongItemSelectors = [
        '[data-e2e="comment-item"]',
        '[data-e2e*="comment-level"]',
        '[data-e2e*="comment-text"]',
        '[data-e2e*="comment-username"]',
        '[class*="DivCommentObject"]',
        '[data-cid]:not([data-cid^="one-column-item"])',
        '[data-comment-id]',
    ].join(', ');
    const itemSelectors = [
        strongItemSelectors,
        'p',
        'li',
        '[role="listitem"]',
    ].join(', ');

    const inputNodes = visibleNodes(document, inputSelectors);
    const inputVisible = inputNodes.length;
    const commentInputNodes = inputNodes.filter(commentInputLike);
    const commentInputVisible = commentInputNodes.length;
    for (const raw of commentInputNodes) {
        try {
            const inner = raw.matches && raw.matches('textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]')
                ? raw
                : raw.querySelector && raw.querySelector('textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]');
            (inner || raw).setAttribute('data-smart-id', 'comment-input');
        } catch (_) {}
    }
    const listCandidates = visibleNodes(document, listSelectors).map((el) => {
        const r = el.getBoundingClientRect();
        const strongItems = visibleNodes(el, strongItemSelectors).length;
        const items = visibleNodes(el, itemSelectors).length;
        const strongList = (() => {
            try {
                return el.matches(
                    '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                    '[class*="CommentList"], [class*="DivCommentListContainer"]'
                );
            } catch (_) {
                return false;
            }
        })();
        return {
            el,
            items,
            strongItems,
            strongList,
            area: Math.max(0, r.width) * Math.max(0, r.height),
            top: r.top,
            left: r.left,
        };
    }).filter((entry) => {
        // TikTok keeps feed/recommendation cards in the DOM. They often expose
        // [data-cid] / text nodes that look like comments globally, so only a
        // real visible comment-list container is allowed to contribute items.
        return entry.strongList || entry.strongItems > 0;
    }).sort((a, b) => {
        const score = (entry) =>
            entry.strongItems * 1000 +
            entry.items * 10 +
            (entry.strongList ? 100 : 0) -
            Math.max(0, entry.area - (window.innerWidth * window.innerHeight * 0.8)) / 10000;
        return score(b) - score(a);
    });
    const chosenList = listCandidates[0] || null;
    const itemVisible = chosenList ? chosenList.items : 0;
    const publishInputCandidateVisible = (() => {
        if (commentInputVisible > 0) return commentInputVisible;
        const hasCommentSurface = !!chosenList || itemVisible > 0 ||
            visibleNodes(document, '[data-e2e="comment-icon"], [data-e2e="comment-count"], [data-e2e*="comment-level"], [data-e2e*="comment-text"]').length > 0;
        if (!hasCommentSurface) return 0;
        const badMarkers = [
            'search', 'captcha', 'challenge', 'verification', 'login', 'log in',
            'sign in', 'sign-in', 'signup', 'sign up', 'password',
            'phone / email', 'email / username'
        ];
        const labelOf = (el) => [
            el && el.getAttribute && (el.getAttribute('data-smart-id') || ''),
            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
            el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
            el && (el.id || ''),
            el && String(el.className || ''),
            el && (el.tagName || ''),
        ].join(' ').toLowerCase();
        const insideBadSurface = (el) => {
            const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
            let cur = el;
            for (let depth = 0; depth < 7 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
                const label = labelOf(cur);
                if (authTokenLike(label) || badMarkers.some(marker => label.includes(marker))) return true;
                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                if (authTokenLike(text) || badMarkers.some(marker => text.includes(marker))) return true;
            }
            return false;
        };
        let count = 0;
        for (const el of inputNodes) {
            if (insideBadSurface(el)) continue;
            const r = el.getBoundingClientRect();
            const parent = el.parentElement;
            const pr = parent && parent.getBoundingClientRect ? parent.getBoundingClientRect() : r;
            const visibleBox = Math.max(r.width || 0, pr.width || 0) > 40 &&
                Math.max(r.height || 0, pr.height || 0) > 4;
            const lowerOrSidePanel = Math.max(r.top || 0, pr.top || 0) > window.innerHeight * 0.22 ||
                Math.max(r.left || 0, pr.left || 0) > window.innerWidth * 0.45;
            if (visibleBox && lowerOrSidePanel) {
                try {
                    const inner = el.matches && el.matches('textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]')
                        ? el
                        : el.querySelector && el.querySelector('textarea, input, div[contenteditable="true"], [contenteditable="true"], [role="textbox"]');
                    (inner || el).setAttribute('data-smart-id', 'comment-input');
                } catch (_) {}
                count += 1;
            }
        }
        return count;
    })();
    let selectedScrollNode = null;
    const scrollInfo = (() => {
        if (!chosenList || !chosenList.el) return null;
        const badScrollTags = new Set([
            'IMG', 'SVG', 'PATH', 'USE', 'PICTURE', 'SOURCE', 'CANVAS',
            'SPAN', 'STRONG', 'P', 'A', 'BUTTON', 'I'
        ]);
        const isRealScrollContainer = (node) => {
            if (!node || !visible(node) || badScrollTags.has(node.tagName)) return false;
            const rect = node.getBoundingClientRect();
            if (rect.width < 180 || rect.height < 120) return false;
            return node.scrollHeight > node.clientHeight + 20;
        };
        const scoreScrollNode = (node) => {
            if (!isRealScrollContainer(node)) return -1;
            const rect = node.getBoundingClientRect();
            const strongItems = visibleNodes(node, strongItemSelectors).length;
            return strongItems * 1000 + Math.min(node.clientHeight, 1200) + Math.min(rect.width, 800);
        };
        let scrollNode = chosenList.el;
        try {
            const descendants = Array.from(chosenList.el.querySelectorAll('*')).filter(visible);
            const candidates = [chosenList.el, ...descendants]
                .map((node) => ({ node, score: scoreScrollNode(node) }))
                .filter((item) => item.score >= 0)
                .sort((a, b) => b.score - a.score);
            scrollNode = candidates.length ? candidates[0].node : chosenList.el;
        } catch (_) {}
        selectedScrollNode = scrollNode;
        return {
            scrollable: isRealScrollContainer(scrollNode),
            scroll_height: Number(scrollNode && scrollNode.scrollHeight) || 0,
            client_height: Number(scrollNode && scrollNode.clientHeight) || 0,
            tag: scrollNode && scrollNode.tagName ? scrollNode.tagName : null,
        };
    })();
    try {
        document
            .querySelectorAll('[data-smart-comment-surface-opened], [data-smart-comment-scroll-opened]')
            .forEach((el) => {
                el.removeAttribute('data-smart-comment-surface-opened');
                el.removeAttribute('data-smart-comment-scroll-opened');
            });
        if (chosenList && chosenList.el) {
            chosenList.el.setAttribute('data-smart-comment-surface-opened', '1');
        }
        if (selectedScrollNode) {
            selectedScrollNode.setAttribute('data-smart-comment-scroll-opened', '1');
        }
    } catch (_) {}
    return {
        input_visible: inputVisible > 0,
        comment_input_visible: commentInputVisible > 0,
        publish_input_candidate_visible: publishInputCandidateVisible > 0,
        list_visible: !!chosenList,
        comment_items_visible: itemVisible,
        surface_visible: inputVisible > 0 || !!chosenList || itemVisible > 0,
        list_source: chosenList ? {
            items: chosenList.items,
            strong_items: chosenList.strongItems,
            strong_list: chosenList.strongList,
            scrollable: !!(scrollInfo && scrollInfo.scrollable),
            scroll_height: scrollInfo ? scrollInfo.scroll_height : 0,
            client_height: scrollInfo ? scrollInfo.client_height : 0,
            scroll_tag: scrollInfo ? scrollInfo.tag : null,
            top: Math.round(chosenList.top),
            left: Math.round(chosenList.left),
        } : null,
    };
}
"""

_JS_DISMISS_COOKIE_BANNERS = r"""
() => {
    const allElementsDeep = () => {
        const out = [];
        const visit = (root) => {
            if (!root || !root.querySelectorAll) return;
            for (const el of root.querySelectorAll('*')) {
                out.push(el);
                if (el.shadowRoot) visit(el.shadowRoot);
            }
        };
        visit(document);
        return out;
    };

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').trim();
    const lowerTextOf = (el) => textOf(el).toLowerCase();

    const cookieTextMarkers = [
        'cookie',
        'cookies',
        'allow cookies',
        'optional cookies',
        'cookies policy',
        'tiktok uses cookies',
        'manage your choices',
        'essential cookies',
        'consent',
        'gdpr',
        '\u30af\u30c3\u30ad\u30fc',
        '\ucfe0\ud0a4',
        '\u997c\u5e72',
        '\u96b1\u79c1',
        '\u9690\u79c1'
    ];

    const acceptTextMarkers = [
        'allow all',
        'accept all',
        'allow cookies',
        'accept cookies',
        'i agree',
        'agree',
        'got it'
    ];

    const clickButton = (button) => {
        try {
            button.click();
            return true;
        } catch (_) {
            try {
                button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                return true;
            } catch (__) {
                return false;
            }
        }
    };

    const tagClassId = (el) => [
        (el && el.tagName) || '',
        String((el && el.className) || ''),
        String((el && el.id) || ''),
        (el && el.getAttribute && (el.getAttribute('data-e2e') || '')) || '',
        (el && el.getAttribute && (el.getAttribute('aria-label') || '')) || '',
    ].join(' ').toLowerCase();

    const looksLikeCookieElement = (el) => {
        const label = tagClassId(el);
        const text = lowerTextOf(el);
        return cookieTextMarkers.some(marker => text.includes(marker) || label.includes(marker))
            || label.includes('cookie')
            || label.includes('consent')
            || label.includes('gdpr');
    };

    const hasCookieContext = (el) => {
        let cur = el;
        for (let depth = 0; depth < 20 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
            if (looksLikeCookieElement(cur)) return true;
        }
        try {
            const host = el.getRootNode && el.getRootNode().host;
            if (host && looksLikeCookieElement(host)) return true;
        } catch (_) {}
        return false;
    };

    const hasAcceptLabel = (el) => {
        const label = [
            lowerTextOf(el),
            (el.getAttribute && (el.getAttribute('aria-label') || '').toLowerCase()) || '',
            (el.getAttribute && (el.getAttribute('data-e2e') || '').toLowerCase()) || ''
        ].join(' ');
        return label.includes('accept-button') || acceptTextMarkers.some(marker => label.includes(marker));
    };

    const hideCookieElement = (el, text) => {
        try {
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
            el.style.setProperty('opacity', '0', 'important');
            el.style.setProperty('z-index', '-1', 'important');
            el.setAttribute('data-teletan-cookie-hidden', '1');
            try {
                const host = el.getRootNode && el.getRootNode().host;
                if (host && host !== el) {
                    host.style.setProperty('pointer-events', 'none', 'important');
                    host.style.setProperty('display', 'none', 'important');
                    host.style.setProperty('visibility', 'hidden', 'important');
                    host.style.setProperty('opacity', '0', 'important');
                    host.style.setProperty('z-index', '-1', 'important');
                    host.setAttribute('data-teletan-cookie-hidden-host', '1');
                }
            } catch (_) {}
            return { count: 1, action: 'cookie-banner-hidden', label: String(text || '').slice(0, 120) };
        } catch (_) {
            return null;
        }
    };

    const runCookieCleanup = () => {
        const nodes = allElementsDeep();
        const buttons = nodes.filter((el) => {
            const tag = (el.tagName || '').toLowerCase();
            const role = (el.getAttribute && el.getAttribute('role')) || '';
            return (tag === 'button' || role === 'button') && visible(el);
        });
        for (const button of buttons) {
            if (hasAcceptLabel(button) && hasCookieContext(button)) {
                if (clickButton(button)) {
                    return { count: 1, action: 'cookie-accept-clicked', label: lowerTextOf(button).slice(0, 120) };
                }
            }
        }

        const candidates = [
            ...nodes.filter((el) => {
                const tag = (el.tagName || '').toLowerCase();
                const cls = String(el.className || '').toLowerCase();
                const id = String(el.id || '').toLowerCase();
                return tag === 'div'
                    || tag === 'section'
                    || tag === 'aside'
                    || tag === 'tiktok-cookie-banner'
                    || tag.includes('cookie')
                    || cls.includes('cookie')
                    || cls.includes('consent')
                    || id.includes('cookie')
                    || id.includes('consent');
            })
        ];
        for (const el of candidates) {
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            const text = lowerTextOf(el);
            const isBottomBand = r.width > window.innerWidth * 0.55 && r.height > 70 && r.top > window.innerHeight * 0.55;
            const nestedButtons = el.querySelectorAll ? [...el.querySelectorAll('button, [role="button"]')].filter(visible) : [];
            const hasAcceptButton = nestedButtons.some(hasAcceptLabel);
            const looksCookie = looksLikeCookieElement(el);
            if (!looksCookie && !(isBottomBand && hasAcceptButton)) continue;
            if (el.querySelector && el.querySelector('[data-e2e*="comment" i], [data-smart-id*="comment" i], [contenteditable="true"], textarea')) {
                continue;
            }

            for (const button of nestedButtons) {
                if (hasAcceptLabel(button)) {
                    if (clickButton(button)) {
                        return { count: 1, action: 'cookie-accept-clicked-nested', label: lowerTextOf(button).slice(0, 120) };
                    }
                }
            }

            const hidden = hideCookieElement(el, text);
            if (hidden) return hidden;
        }

        return { count: 0, action: 'none' };
    };

    try {
        window.__teletanCookieBannerCleanup = runCookieCleanup;
        if (!window.__teletanCookieBannerKillerInstalled) {
            window.__teletanCookieBannerKillerInstalled = true;
            try {
                window.__teletanCookieBannerObserver = new MutationObserver(() => {
                    try { window.__teletanCookieBannerCleanup(); } catch (_) {}
                });
                window.__teletanCookieBannerObserver.observe(document.documentElement || document.body, {
                    subtree: true,
                    childList: true,
                    attributes: false,
                });
            } catch (_) {}
            try {
                window.__teletanCookieBannerInterval = setInterval(() => {
                    try { window.__teletanCookieBannerCleanup(); } catch (_) {}
                }, 1500);
            } catch (_) {}
        }
    } catch (_) {}

    return runCookieCleanup();
}
"""

_JS_DIAGNOSE_PAGE = r"""
() => {
    return {
        url: window.location.href.substring(0, 100),
        pathname: window.location.pathname,
        title: document.title.substring(0, 60),
        readyState: document.readyState,
        editableCount: document.querySelectorAll('div[contenteditable="true"]').length,
        textareaCount: document.querySelectorAll('textarea').length,
        dataE2eCommentElements: (() => {
            const els = document.querySelectorAll('[data-e2e*="comment"]');
            const result = [];
            for (const el of els) {
                const r = el.getBoundingClientRect();
                result.push({
                    e2e: el.getAttribute('data-e2e'),
                    tag: el.tagName,
                    visible: r.width > 0 && r.height > 0,
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                });
            }
            return result;
        })(),
        commentTriggerCandidates: (() => {
            const markers = ['comment', 'comments', 'reply', 'replies', 'komment', 'coment', 'comentario', 'commentaire', 'yorum'];
            const blocked = ['input', 'textbox', 'list', 'container', 'post', 'send', 'search', 'share', 'like', 'follow', 'report'];
            const result = [];
            const visible = (el) => {
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
            };
            const labelOf = (el) => [
                el.getAttribute('data-e2e') || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || '',
                el.getAttribute('role') || '',
                String(el.className || ''),
                String(el.id || ''),
                (el.innerText || el.textContent || '').slice(0, 80),
            ].join(' ').toLowerCase();
            const els = new Set([
                ...document.querySelectorAll('[data-e2e*="comment"], [data-e2e*="Comment"]'),
                ...document.querySelectorAll('button, [role="button"], a, div[tabindex="0"]')
            ]);
            for (const el of els) {
                const label = labelOf(el);
                if (!markers.some(marker => label.includes(marker))) continue;
                if (blocked.some(marker => label.includes(marker))) continue;
                const r = el.getBoundingClientRect();
                result.push({
                    e2e: el.getAttribute('data-e2e'),
                    aria: el.getAttribute('aria-label'),
                    tag: el.tagName,
                    visible: visible(el),
                    text: (el.innerText || el.textContent || '').trim().slice(0, 40),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                });
                if (result.length >= 8) break;
            }
            return result;
        })(),
        bodyHeight: document.body ? document.body.scrollHeight : 0,
        viewportHeight: window.innerHeight,
    };
}
"""

_JS_DISMISS_OVERLAYS = r"""
() => {
    const dismissed = [];

    const COMMENT_UI_SELECTOR = [
        '[data-e2e*="comment" i]',
        '[data-smart-id*="comment" i]',
        '[data-e2e="comment-input"]',
        '[data-e2e="comment-post"]',
        '[data-smart-id="comment-input"]',
        '[data-smart-id="comment-post"]',
        '[data-smart-id="comment-post-ready"]',
        'div[contenteditable="true"]',
        '[contenteditable="true"]',
        'textarea',
        '[role="textbox"]'
    ].join(', ');

    const containsCommentUi = (el) => {
        try {
            if (!el) return false;
            if (el.matches && el.matches(COMMENT_UI_SELECTOR)) return true;
            if (el.closest && el.closest(COMMENT_UI_SELECTOR)) return true;
            if (el.querySelector && el.querySelector(COMMENT_UI_SELECTOR)) return true;
        } catch (_) {}
        return false;
    };

    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    // 1. Notice containers.
    // IMPORTANT: never display:none these containers, and never touch comment UI.
    const noticeContainers = document.querySelectorAll(
        '[class*="DivNoticeMainContainer"], [class*="NoticeContainer"], ' +
        'section[aria-label*="Notification"], [class*="notice-main"]'
    );
    for (const el of noticeContainers) {
        if (containsCommentUi(el)) continue;
        try {
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('z-index', '-1', 'important');
            dismissed.push('notice-container');
        } catch (e) {}
    }

    // 2. Notification sections.
    // Only make them click-through; do NOT hide them. Some TikTok layouts put
    // comment panels near notification/live regions.
    const notifSections = document.querySelectorAll(
        'section[aria-label*="Notification"], section[aria-live="polite"]'
    );
    for (const el of notifSections) {
        if (containsCommentUi(el)) continue;
        try {
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('z-index', '-1', 'important');
            dismissed.push('notification-section');
        } catch (e) {}
    }

    // 3. Cookie banners. These are safe to accept/hide if they do not contain comment UI.
    const cookieBanners = document.querySelectorAll(
        '[class*="cookie" i], [class*="consent" i], [class*="gdpr" i], ' +
        '[id*="cookie" i], [id*="consent" i]'
    );
    for (const el of cookieBanners) {
        if (containsCommentUi(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width > 100 && r.height > 30) {
            try {
                const acceptBtn = el.querySelector(
                    'button[data-e2e="accept-button"], ' +
                    'button:not([class*="reject"]):not([class*="decline"])'
                );
                if (acceptBtn) {
                    acceptBtn.click();
                    dismissed.push('cookie-accept-clicked');
                } else {
                    el.style.setProperty('pointer-events', 'none', 'important');
                    el.style.setProperty('z-index', '-1', 'important');
                    dismissed.push('cookie-banner-clickthrough');
                }
            } catch (e) {}
        }
    }

    // 4. Modal overlays.
    // DO NOT use broad [class*="Overlay"] here. On TikTok it can match wrappers
    // around the desktop video/comment layout and kill the whole comment section.
    const modals = document.querySelectorAll(
        '[class*="Modal-overlay"], [class*="modal-overlay"]'
    );
    for (const el of modals) {
        if (containsCommentUi(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width > window.innerWidth * 0.5 && r.height > window.innerHeight * 0.5) {
            const closeBtn = el.querySelector(
                'button[aria-label*="close" i], button[aria-label*="Close"], ' +
                '[class*="close" i][role="button"]'
            );
            if (closeBtn) {
                try {
                    closeBtn.click();
                    dismissed.push('modal-close-clicked');
                } catch (e) {}
            } else {
                // Never display:none a modal here; too risky for comment UI.
                try {
                    el.style.setProperty('pointer-events', 'none', 'important');
                    el.style.setProperty('z-index', '-1', 'important');
                    dismissed.push('modal-clickthrough');
                } catch (e) {}
            }
        }
    }

    // 5. Popup buttons that explicitly invite continuing/accepting.
    const allButtons = document.querySelectorAll('button');
    const popupKeywords = ['continue watching', 'i am 18', 'accept all', 'принять', 'продолжить'];
    for (const btn of allButtons) {
        if (containsCommentUi(btn)) continue;
        const text = (btn.textContent || '').trim().toLowerCase();
        if (popupKeywords.some(k => text.includes(k))) {
            const r = btn.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                try {
                    btn.click();
                    dismissed.push('popup-button:' + text.substring(0, 20));
                } catch (e) {}
            }
        }
    }

    // 6. Fixed/sticky notification-like elements only.
    // Avoid querySelectorAll('*') because it is slow and too dangerous.
    const fixedCandidates = document.querySelectorAll(
        '[class*="Notice"], [class*="Toast"], [class*="Snackbar"], [class*="Alert"], ' +
        '[class*="Banner"]:not([class*="video"]):not([class*="comment" i])'
    );
    for (const el of fixedCandidates) {
        if (containsCommentUi(el)) continue;

        const style = getComputedStyle(el);
        if (style.position !== 'fixed' && style.position !== 'sticky') continue;

        const r = el.getBoundingClientRect();
        if (r.height < 200 && r.width > 50) {
            try {
                el.style.setProperty('pointer-events', 'none', 'important');
                el.style.setProperty('z-index', '-1', 'important');
                dismissed.push('fixed-element');
            } catch (e) {}
        }
    }

    return { dismissed, count: dismissed.length };
}
"""

_JS_OPEN_REPLY = r"""
([parentCommentId, parentText, parentAnchorToken]) => {
    const url = window.location.pathname || '';
    if (url.includes('/messages') || url.includes('/login') || url.includes('/verify')) {
        return { clicked: false, reason: 'redirected', url };
    }

    const keywords = ['reply', 'ответ', 'ответить'];
    const normalize = (value) => String(value || '')
        .replace(/[\u200b-\u200d\ufeff]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
    const normalizedNeedle = normalize(parentText);
    const targetId = (parentCommentId || '').trim();
    const targetAnchorToken = (parentAnchorToken || '').trim();
    const rootMatchesNeedle = (el) => {
        if (!normalizedNeedle) return true;
        return normalize(el && (el.textContent || el.innerText || '')).includes(normalizedNeedle);
    };
    const ensureAnchorToken = (root) => {
        if (!root || !root.setAttribute) return null;
        const existing = root.getAttribute('data-smart-parent-anchor');
        if (existing) return existing;
        const token = targetAnchorToken || `pa-open-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        try {
            root.setAttribute('data-smart-parent-anchor', token);
            return token;
        } catch (_) {
            return null;
        }
    };

    // Distinguishes the "View/Show/Hide N replies" expander from the real Reply
    // action. Both contain the word reply/ответ, so a bare keyword match would
    // click the expander and the inline reply editor would never open — the
    // reply would then be typed into the global comment box and posted as a main
    // comment. Exclude expander-style controls explicitly.
    const isReplyExpander = (el, text) => {
        const e2e = ((el.getAttribute && el.getAttribute('data-e2e')) || '').toLowerCase();
        if (e2e.includes('expand')) return true;
        if (/(view|show|hide|показа|скры|посмотр|смотрет|перегл|схова|розгор|згорн|развернуть|свернуть)/.test(text)) return true;
        // Count-prefixed expanders like "3 replies" / "Ответы 5" (the Reply verb
        // never carries a number).
        if (/\d/.test(text) && /(repl|ответ|відповід)/.test(text)) return true;
        return false;
    };
    const clickReplyInside = (root, strategy) => {
        if (!root) return null;
        // Prefer the canonical Reply action control (the verb button). This is
        // far safer than a keyword scan because it cannot match the expander.
        const canonical = root.querySelector('p[data-e2e="comment-reply-1"], [data-e2e="comment-reply-1"]');
        if (canonical) {
            const cr = canonical.getBoundingClientRect();
            if (cr.width > 0 && cr.height > 0) {
                try {
                    const anchorToken = ensureAnchorToken(root);
                    canonical.click();
                    return { clicked: true, strategy: strategy + '-canonical', anchorToken };
                } catch (e) {
                    return { clicked: false, reason: String(e) };
                }
            }
        }
        const buttons = [
            ...root.querySelectorAll('button'),
            ...root.querySelectorAll('[role="button"]'),
            ...root.querySelectorAll('div[tabindex="0"]'),
            ...root.querySelectorAll('[data-e2e*="reply"]'),
        ];
        for (const button of buttons) {
            const text = (((button.textContent || '') + ' ' + (button.getAttribute('aria-label') || '')).trim()).toLowerCase();
            if (!text) continue;
            if (!keywords.some(k => text.includes(k))) continue;
            if (isReplyExpander(button, text)) continue;
            const r = button.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            try {
                const anchorToken = ensureAnchorToken(root);
                button.click();
                return { clicked: true, strategy, anchorToken };
            } catch (e) {
                return { clicked: false, reason: String(e) };
            }
        }
        return null;
    };

    // Strategy 0: exact DOM row anchor created by parent search. This is safer
    // than re-searching by text when several parent comments have the same text.
    if (targetAnchorToken) {
        const anchors = document.querySelectorAll('[data-smart-parent-anchor]');
        for (const el of anchors) {
            if ((el.getAttribute('data-smart-parent-anchor') || '') !== targetAnchorToken) continue;
            try { el.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (e) {}
            const roots = [el];
            for (let cur = el.parentElement, depth = 0; cur && depth < 8; cur = cur.parentElement, depth++) {
                roots.push(cur);
            }
            for (const root of roots) {
                const strategy = root === el ? 'parent-anchor' : 'parent-anchor-parent';
                const parentResult = clickReplyInside(root, strategy);
                if (parentResult) return parentResult;
            }
            break;
        }
    }

    // Strategy 1: exact comment ID match via DOM attributes (most reliable).
    // TikTok sets data-comment-id, data-cid, or id="comment-{id}" on comment containers.
    if (targetId) {
        const idSelectors = [
            `[data-comment-id="${targetId}"]`,
            `[data-cid="${targetId}"]`,
            `[id="comment-${targetId}"]`,
            `[id="${targetId}"]`,
        ];
        for (const sel of idSelectors) {
            let el = null;
            try {
                el = document.querySelector(sel);
            } catch (e) {
                continue;
            }
            if (!el) continue;
            if (!rootMatchesNeedle(el)) continue;
            const result = clickReplyInside(el, 'comment-id-attr');
            if (result) return result;
            // Found the container but Reply button is not inside — scroll it into view and retry.
            el.scrollIntoView({ block: 'center', behavior: 'instant' });
            const result2 = clickReplyInside(el, 'comment-id-attr-scrolled');
            if (result2) return result2;
        }
        // Also check parent containers a few levels up.
        let anyIdEl = null;
        try {
            anyIdEl = document.querySelector(idSelectors.join(','));
        } catch (e) {
            anyIdEl = null;
        }
        if (anyIdEl && rootMatchesNeedle(anyIdEl)) {
            for (let cur = anyIdEl.parentElement, depth = 0; cur && depth < 4; cur = cur.parentElement, depth++) {
                const result = clickReplyInside(cur, 'comment-id-parent');
                if (result) return result;
            }
        }
    }

    // Strategy 2: text match (works when DOM attributes are absent).
    if (normalizedNeedle) {
        const visible = (el) => {
            if (!el || !el.getBoundingClientRect) return false;
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
        };
        const commentContainerSelector = [
            '[data-cid]:not([data-cid^="one-column-item"])',
            '[data-comment-id]',
            '[data-e2e="comment-item"]',
            '[class*="DivCommentObject"]',
            '[class*="CommentItem"]',
            '[class*="DivComment"]',
            'article',
            'li'
        ].join(', ');
        const textNodes = [
            ...document.querySelectorAll(
                '[data-e2e*="comment-level"], [data-e2e*="comment-text"], p, span'
            )
        ].filter(visible);
        const triedRoots = new Set();
        for (const node of textNodes) {
            const text = normalize(node.textContent || node.innerText || '');
            if (!text || !text.includes(normalizedNeedle)) continue;
            if (text.length > Math.max(normalizedNeedle.length * 4, normalizedNeedle.length + 220)) {
                continue;
            }

            const roots = [];
            const closest = node.closest(commentContainerSelector);
            if (closest) roots.push(closest);
            for (let cur = node.parentElement, depth = 0; cur && depth < 8; cur = cur.parentElement, depth++) {
                if (cur.matches && cur.matches(commentContainerSelector)) roots.push(cur);
                if ((cur.textContent || '').length > 2500) break;
            }

            for (const root of roots) {
                if (!root || triedRoots.has(root)) continue;
                triedRoots.add(root);
                try { root.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (e) {}
                const result = clickReplyInside(root, 'parent-text-match');
                if (result) return result;
                for (let cur = root.parentElement, depth = 0; cur && depth < 4; cur = cur.parentElement, depth++) {
                    const parentResult = clickReplyInside(cur, 'parent-text-match-parent');
                    if (parentResult) return parentResult;
                }
            }
        }
        // Parent comment not found in DOM — caller will scroll and retry.
        return { clicked: false, reason: 'parent_comment_not_found_in_dom' };
    }

    // Strategy 3 (no anchor provided): click the first visible reply button.
    // Only reached when caller passes neither ID nor text — treated as generic reply.
    const sideButtons = document.querySelectorAll('[data-e2e*="reply"], button, [role="button"]');
    for (const button of sideButtons) {
        const text = (((button.textContent || '') + ' ' + (button.getAttribute('aria-label') || '')).trim()).toLowerCase();
        if (!keywords.some(k => text.includes(k))) continue;
        const r = button.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        try {
            button.click();
            return { clicked: true, strategy: 'fallback-first-reply' };
        } catch (e) {
            return { clicked: false, reason: String(e) };
        }
    }

    return { clicked: false, reason: 'reply_button_not_found' };
}
"""

_JS_LIKE_PARENT_COMMENT = r"""
([parentCommentId, parentText, parentAnchorToken]) => {
    const url = window.location.pathname || '';
    if (url.includes('/messages') || url.includes('/login') || url.includes('/verify')) {
        return { success: false, reason: 'redirected', url };
    }

    const normalize = (value) => String(value || '')
        .replace(/[\u200b-\u200d\ufeff]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
    const normalizedNeedle = normalize(parentText);
    const targetId = (parentCommentId || '').trim();
    const targetAnchorToken = (parentAnchorToken || '').trim();

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const rootMatchesNeedle = (el) => {
        if (!normalizedNeedle) return true;
        const txt = normalize(el && (el.textContent || el.innerText || ''));
        return txt.includes(normalizedNeedle);
    };
    const commentContainerSelector = [
        '[data-cid]:not([data-cid^="one-column-item"])',
        '[data-comment-id]',
        '[data-e2e="comment-item"]',
        '[class*="DivCommentObject"]',
        '[class*="CommentItem"]',
        '[class*="DivComment"]',
        'article',
        'li'
    ].join(', ');

    const labelOf = (el) => [
        (el && (el.innerText || el.textContent)) || '',
        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
        el && el.getAttribute && (el.getAttribute('title') || ''),
        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
        el && el.getAttribute && (el.getAttribute('role') || ''),
        el && String(el.className || ''),
    ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
    const cssEscape = (value) => {
        try {
            if (window.CSS && typeof window.CSS.escape === 'function') {
                return window.CSS.escape(String(value || ''));
            }
        } catch (_) {}
        return String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    };

    const likeState = (button) => {
        const label = labelOf(button);
        const ariaPressed = (button.getAttribute('aria-pressed') || '').toLowerCase();
        const pressed = ariaPressed === 'true' ||
            label.includes('unlike') ||
            label.includes('liked') ||
            label.includes('active');
        return { pressed, label: label.slice(0, 120), ariaPressed };
    };

    const clickLikeInside = (root, strategy) => {
        if (!root || !visible(root)) return { success: false, reason: 'parent_row_not_visible', strategy };
        if (!rootMatchesNeedle(root)) return { success: false, reason: 'parent_text_mismatch', strategy };
        const candidates = [
            ...root.querySelectorAll('[data-e2e="comment-like"], [data-e2e*="comment-like"]'),
            ...root.querySelectorAll('button, [role="button"], div[tabindex="0"]'),
        ];
        const seen = new Set();
        for (const candidate of candidates) {
            if (!candidate) continue;
            const clickable = candidate.closest && candidate.closest('button, [role="button"], div[tabindex="0"]');
            const button = clickable && root.contains(clickable) ? clickable : candidate;
            if (!button || seen.has(button)) continue;
            seen.add(button);
            if (!visible(button)) continue;
            const label = `${labelOf(candidate)} ${labelOf(button)}`.trim();
            const isLike =
                label.includes('comment-like') ||
                label.includes('like') ||
                label.includes('\u043b\u0430\u0439\u043a') ||
                label.includes('\u043f\u043e\u0434\u043e\u0431\u0430') ||
                label.includes('лайк') ||
                label.includes('подоба') ||
                label.includes('heart');
            const isWrongAction =
                label.includes('reply') ||
                label.includes('share') ||
                label.includes('follow') ||
                label.includes('report') ||
                label.includes('post') ||
                label.includes('send');
            if (!isLike || isWrongAction) continue;
            const r = button.getBoundingClientRect();
            if (r.width > 260 || r.height > 120) continue;
            const state = likeState(button);
            if (state.pressed) {
                return {
                    success: true,
                    already_liked: true,
                    clicked: false,
                    strategy,
                    label: state.label,
                };
            }
            try { button.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (_) {}
            try {
                button.click();
            } catch (clickErr) {
                try {
                    for (const type of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        const eventInit = { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' };
                        const Ev = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                        button.dispatchEvent(new Ev(type, eventInit));
                    }
                } catch (dispatchErr) {
                    return {
                        success: false,
                        reason: String(clickErr || dispatchErr).slice(0, 120),
                        strategy,
                        label: state.label,
                    };
                }
            }
            const after = likeState(button);
            return {
                success: true,
                clicked: true,
                liked_now: after.pressed,
                strategy,
                label: after.label,
            };
        }
        return { success: false, reason: 'like_button_not_found_in_parent', strategy };
    };

    const candidateRows = [];
    if (targetAnchorToken) {
        for (const el of document.querySelectorAll('[data-smart-parent-anchor]')) {
            if ((el.getAttribute('data-smart-parent-anchor') || '') === targetAnchorToken) {
                candidateRows.push({ el, strategy: 'parent-anchor' });
            }
        }
    }
    if (targetId) {
        const escapedId = cssEscape(targetId);
        const idSelectors = [
            `[data-comment-id="${escapedId}"]`,
            `[data-cid="${escapedId}"]`,
            `[id="comment-${escapedId}"]`,
            `[id="${escapedId}"]`,
        ];
        for (const sel of idSelectors) {
            try {
                const el = document.querySelector(sel);
                if (el) candidateRows.push({ el, strategy: 'comment-id' });
            } catch (_) {}
        }
    }
    if (normalizedNeedle && !candidateRows.length) {
        const textNodes = [
            ...document.querySelectorAll('[data-e2e*="comment-level"], [data-e2e*="comment-text"], p, span')
        ].filter(visible);
        const tried = new Set();
        for (const node of textNodes) {
            const text = normalize(node.textContent || node.innerText || '');
            if (!text || !text.includes(normalizedNeedle)) continue;
            const root = node.closest(commentContainerSelector);
            if (!root || tried.has(root)) continue;
            tried.add(root);
            candidateRows.push({ el: root, strategy: 'parent-text' });
            if (candidateRows.length >= 3) break;
        }
    }

    for (const item of candidateRows) {
        let row = item.el;
        for (let depth = 0; row && depth < 4; row = row.parentElement, depth++) {
            const result = clickLikeInside(row, item.strategy + (depth ? '-parent' : ''));
            if (result && result.success) return result;
            if (result && result.reason !== 'like_button_not_found_in_parent') break;
        }
    }
    return { success: false, reason: 'parent_row_not_found' };
}
"""

_JS_REPLY_INPUT_ACTIVE = r"""
() => {
    const replyMarkers = [
        'reply',
        'replying',
        'reply to',
        '\u043e\u0442\u0432\u0435\u0442',
        '\u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0441\u0442\u0438',
        '\u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c',
        'antwort',
        '\u0440\u00e9\u0070\u006f\u006e\u0064\u0072\u0065',
        '\u0072\u0065\u0073\u0070\u006f\u006e\u0064\u0065\u0072',
        '\u8fd4\u4fe1',
        '\u56de\u590d',
        '\u8fd4\u56de',
        '\ub2f5\uae00',
    ];

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const textOf = (el) => String((el && (el.innerText || el.textContent)) || '')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();

    const labelOf = (el) => [
        textOf(el),
        el.getAttribute && (el.getAttribute('placeholder') || ''),
        el.getAttribute && (el.getAttribute('aria-label') || ''),
        el.getAttribute && (el.getAttribute('data-placeholder') || ''),
        el.getAttribute && (el.getAttribute('data-e2e') || ''),
        el.id || '',
        String(el.className || ''),
    ].join(' ').toLowerCase();

    const editors = [
        ...document.querySelectorAll('[data-e2e*="reply" i] [contenteditable="true"]'),
        ...document.querySelectorAll('[data-e2e*="reply" i] textarea'),
        ...document.querySelectorAll('[data-e2e*="reply" i] [role="textbox"]'),
        ...document.querySelectorAll('[data-e2e="comment-input"], [data-e2e*="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'),
    ].filter(visible);

    for (const editor of editors) {
        const label = labelOf(editor);
        const parentLabel = editor.parentElement ? labelOf(editor.parentElement) : '';
        if (replyMarkers.some(marker => label.includes(marker) || parentLabel.includes(marker))) {
            return { active: true, reason: 'reply_label' };
        }
        const text = textOf(editor);
        if (text.startsWith('@') || (text.length <= 80 && text.includes('@'))) {
            return { active: true, reason: 'mention_prefill' };
        }
    }

    const replyContainers = [
        ...document.querySelectorAll('[data-e2e*="reply" i], [aria-label*="reply" i], [class*="reply" i]')
    ].filter(visible);
    for (const node of replyContainers) {
        const label = labelOf(node);
        if (replyMarkers.some(marker => label.includes(marker))) {
            const editable = node.querySelector && node.querySelector(
                'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
            );
            if (visible(editable)) return { active: true, reason: 'reply_container' };
        }
    }

    return { active: false, reason: 'reply_input_not_activated' };
}
"""


_JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY = r"""
() => {
    const markers = [
        'Introducing keyboard shortcuts',
        'Go to previous video',
        'Go to next video',
        'Mute / unmute video',
        'Play / Pause',
        'Scroll, use the',
        'browse your feed',
        'click the arrows on the right',
    ];

    const hasMarker = (txt) => {
        txt = String(txt || '');
        if (txt.includes('Introducing keyboard shortcuts')) return true;
        // TikTok also shows a feed-navigation onboarding modal:
        // "Scroll, use the ↑ ↓ keys, or click the arrows on the right to browse your feed."
        // It can dim the page and block comment/Post interactions just like the shortcuts popup.
        const lower = txt.toLowerCase();
        if (
            lower.includes('browse your feed') &&
            (lower.includes('scroll') || lower.includes('arrows on the right') || lower.includes('got it'))
        ) return true;
        // The shortcuts popup often gets rendered as text-only DIV without labels.
        return txt.includes('Go to previous video')
            && txt.includes('Go to next video')
            && txt.includes('Mute / unmute video')
            && txt.includes('Play / Pause');
    };

    const isVisible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };

    const kill = (el, mode='hide') => {
        if (!el || el === document.body || el === document.documentElement) return 0;
        try {
            el.setAttribute('data-teletan-killed-shortcuts', '1');
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
            el.style.setProperty('opacity', '0', 'important');
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('z-index', '-1', 'important');
            if (mode === 'remove') {
                try { el.remove(); } catch (_) {}
            }
            return 1;
        } catch (_) {
            return 0;
        }
    };

    const boundedOverlay = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (!(r.width > 120 && r.height > 80)) return false;
        if (r.width > window.innerWidth * 0.95 || r.height > window.innerHeight * 0.95) return false;
        // In logs this popup is right/bottom-ish around x=1569,y=640,w=327,h=420 on 1920x1080.
        if (r.x > window.innerWidth * 0.35 && r.y > window.innerHeight * 0.15) return true;
        const st = getComputedStyle(el);
        const z = parseInt(st.zIndex || '0', 10);
        return st.position === 'fixed' || st.position === 'absolute' || z > 10;
    };
    const safeMarkerNode = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (r.width > window.innerWidth * 0.75 || r.height > window.innerHeight * 0.75) return false;
        return r.width >= 20 && r.height >= 12;
    };

    const setSeenFlags = () => {
        const keys = [
            'tiktok_keyboard_shortcuts_seen',
            'keyboard_shortcuts_seen',
            'keyboardShortcutsSeen',
            'keyboard_shortcuts_modal_seen',
            'video_keyboard_shortcuts_seen',
            'shortcut_guide_seen',
                'shortcutGuideSeen',
                'tiktok_shortcuts_guide_seen',
                'webapp_keyboard_shortcuts_seen',
                'tiktok_feed_navigation_guide_seen',
                'feed_navigation_guide_seen',
                'webapp_feed_navigation_guide_seen',
                'tiktok_video_navigation_guide_seen',
            ];
        try {
            for (const k of keys) {
                localStorage.setItem(k, '1');
                sessionStorage.setItem(k, '1');
            }
        } catch (_) {}
    };

    const killOnce = () => {
        setSeenFlags();
        let count = 0;

        // 1) Kill marker-containing bounded popup nodes and their bounded parents.
        const all = [...document.querySelectorAll('body *')];
        for (const node of all) {
            if (!isVisible(node)) continue;
            const txt = node.innerText || node.textContent || '';
            if (!hasMarker(txt)) continue;

            let cur = node;
            for (let depth = 0; depth < 10 && cur && cur !== document.body && cur !== document.documentElement; depth++, cur = cur.parentElement) {
                const curTxt = cur.innerText || cur.textContent || '';
                if (!hasMarker(curTxt)) continue;

                if (boundedOverlay(cur)) {
                    count += kill(cur, 'remove');
                    // Keep walking; TikTok sometimes recreates an ancestor wrapper.
                } else if (depth <= 1 && safeMarkerNode(cur)) {
                    // Still make the marker node itself click-through, but avoid
                    // touching large ancestors/app roots that merely contain the text.
                    try {
                        cur.style.setProperty('pointer-events', 'none', 'important');
                        cur.setAttribute('data-teletan-clickthrough-shortcuts', '1');
                        count++;
                    } catch (_) {}
                }
            }
        }

        // 2) Probe bottom-right points where the Post button lives. If a shortcuts popup is under the pointer,
        // force all ancestors click-through/hidden.
        const points = [
            [window.innerWidth - 44, window.innerHeight - 49],
            [window.innerWidth - 36, window.innerHeight - 36],
            [window.innerWidth * 0.88, window.innerHeight * 0.91],
            [window.innerWidth * 0.98, window.innerHeight * 0.95],
        ];

        for (const [x, y] of points) {
            let el = document.elementFromPoint(x, y);
            for (let depth = 0; depth < 10 && el && el !== document.body && el !== document.documentElement; depth++, el = el.parentElement) {
                const txt = el.innerText || el.textContent || '';
                if (hasMarker(txt)) {
                    if (boundedOverlay(el)) count += kill(el, 'remove');
                    else if (depth <= 1 && safeMarkerNode(el)) {
                        try {
                            el.style.setProperty('pointer-events', 'none', 'important');
                            el.style.setProperty('visibility', 'hidden', 'important');
                            el.style.setProperty('opacity', '0', 'important');
                            el.setAttribute('data-teletan-point-killed-shortcuts', '1');
                            count++;
                        } catch (_) {}
                    }
                }
            }
        }

        return count;
    };

    try {
        if (!window.__teletanKeyboardShortcutsKillerInstalled) {
            window.__teletanKeyboardShortcutsKillerInstalled = true;
            window.__teletanKillKeyboardShortcuts = killOnce;

            try {
                const observer = new MutationObserver(() => {
                    try { killOnce(); } catch (_) {}
                });
                observer.observe(document.documentElement || document.body, {
                    subtree: true,
                    childList: true,
                    attributes: false,
                    characterData: false,
                });
                window.__teletanKeyboardShortcutsObserver = observer;
            } catch (_) {}

            try {
                window.__teletanKeyboardShortcutsInterval = setInterval(() => {
                    try { killOnce(); } catch (_) {}
                }, 250);
            } catch (_) {}

            try {
                document.addEventListener('keydown', (e) => {
                    if (e && (e.key === '?' || e.key === 'F1')) {
                        e.preventDefault();
                        e.stopPropagation();
                        e.stopImmediatePropagation();
                        setTimeout(killOnce, 0);
                    }
                }, true);
            } catch (_) {}
        }
    } catch (_) {}

    return killOnce();
}
"""


async def suppress_keyboard_shortcuts_overlay(page: Page) -> int:
    """Install and run a persistent killer for TikTok blocking onboarding popups.

    These popups are real blockers in headful runs: they can cover/dim the page
    or the Post button, so elementFromPoint returns the guide instead of
    comment-post.
    """
    try:
        result = await page.evaluate(_JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY)
        count = int(result or 0)
        if count > 0:
            logger.info("[DOM] Suppressed keyboard-shortcuts overlay node(s): %s", count)
            await asyncio.sleep(0.25)
        return count
    except Exception as exc:
        logger.debug("[DOM] keyboard-shortcuts suppression error: %s", exc)
        return 0


@dataclass
class DomActionResult:
    success: bool
    reason: Optional[str] = None
    elapsed_seconds: float = 0.0
    strategy: Optional[str] = None
    redirected_to: Optional[str] = None
    anchor_token: Optional[str] = None


async def _comment_surface_state(page: Page) -> dict[str, Any]:
    try:
        state = await page.evaluate(_JS_COMMENT_SURFACE_STATE)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


async def get_comment_surface_state(page: Page) -> dict[str, Any]:
    """Return the visible comment surface state for higher-level workflow gates."""
    return await _comment_surface_state(page)


def _comment_surface_ready(
    state: dict[str, Any],
    *,
    require_input: bool = False,
    require_list_with_items: bool = False,
    aggressive: bool = False,
) -> bool:
    """Decide if the comment surface is ready for the requested operation.

    Modes (in priority order):
    - require_list_with_items=True: ONLY ready if list container is visible AND
      at least one comment item is rendered. Use this for observer/reply flows
      where we will scope a search to the list container. Without items the
      drawer can be open but empty, and a fallback would match sidebar nodes.
    - require_input=True: ready only when the input is visible (publish flow).
    - default (loose): any of input/list/items/surface visible. Kept for
      legacy callers (publish path uses this when input availability is
      enough).
    """
    if require_list_with_items:
        list_source = state.get("list_source") or {}
        if not isinstance(list_source, dict):
            list_source = {}
        strong_items_raw = list_source.get("strong_items")
        try:
            strong_items = int(strong_items_raw or 0) if strong_items_raw is not None else None
        except (TypeError, ValueError):
            strong_items = 0
        rendered_items = (
            strong_items
            if strong_items is not None
            else int(state.get("comment_items_visible") or 0)
        )
        return bool(
            state.get("list_visible")
            and rendered_items > 0
        )
    if require_input:
        # Publishing needs the real comment composer, not any visible textbox
        # (for example TikTok search can also be a visible input).
        return bool(
            state.get("comment_input_visible")
            or (aggressive and state.get("publish_input_candidate_visible"))
        )
    return bool(
        state.get("surface_visible")
        or state.get("input_visible")
        or state.get("list_visible")
        or int(state.get("comment_items_visible") or 0) > 0
    )


async def _click_comment_trigger_fallback(
    page: Page,
    *,
    require_input: bool = False,
    require_list_with_items: bool = False,
    aggressive: bool = False,
    deadline: Optional[float] = None,
) -> bool:
    """Extra robust opener for TikTok layouts where JS scoring picks a stale node."""
    selectors = [
        'button:has([data-e2e="comment-icon"])',
        'button:has([data-e2e="comment-count"])',
        '[role="button"]:has([data-e2e="comment-icon"])',
        '[role="button"]:has([data-e2e="comment-count"])',
        'button[aria-label*="Read or add" i]',
        'button[aria-label*="comments" i]',
        'button[aria-label*="comment" i]',
        '[role="button"][aria-label*="comments" i]',
        '[role="button"][aria-label*="comment" i]',
        'div[tabindex][aria-label*="comments" i]',
        'div[tabindex][aria-label*="comment" i]',
        'button[aria-label*="Прочитать" i]',
        'button[aria-label*="оставить" i]',
        'button[aria-label*="комментар" i]',
        '[aria-label*="תגובות" i]',
        '[aria-label*="תגובה" i]',
        '[data-e2e="comments"]',
        '[data-e2e="comment-icon"]',
        '[data-e2e="comment-count"]',
    ]
    tried_boxes: set[tuple[int, int, int, int]] = set()
    diag: dict[str, Any] = {
        "url": getattr(page, "url", None),
        "mode": (
            "list_with_items"
            if require_list_with_items
            else "input"
            if require_input
            else "surface"
        ),
        "selector_counts": {},
        "visible_candidates": 0,
        "deduped_boxes": 0,
        "off_target_candidates": 0,
        "playwright_clicks": 0,
        "playwright_not_ready": 0,
        "js_icons": 0,
        "js_visible_icons": 0,
        "js_clicked_icons": 0,
        "js_not_ready": 0,
        "errors": 0,
    }

    def _remaining_seconds() -> Optional[float]:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    def _timeout_ms(cap_ms: int, *, floor_ms: int = 50) -> Optional[int]:
        remaining = _remaining_seconds()
        if remaining is None:
            return cap_ms
        if remaining <= 0:
            return None
        return max(floor_ms, min(cap_ms, int(remaining * 1000)))

    def _remember_state(state: dict[str, Any]) -> None:
        diag["last_state"] = {
            "input": bool(state.get("input_visible")),
            "list": bool(state.get("list_visible")),
            "items": int(state.get("comment_items_visible") or 0),
            "list_source": state.get("list_source"),
        }

    try:
        viewport = await page.evaluate("() => ({ w: window.innerWidth || 0, h: window.innerHeight || 0 })")
    except Exception:
        viewport = {}
    diag["viewport"] = viewport

    def _box_looks_like_target(box: Optional[Dict[str, float]]) -> bool:
        if not box:
            return True
        try:
            vw = float((viewport or {}).get("w") or 0)
            vh = float((viewport or {}).get("h") or 0)
            if vw <= 0 or vh <= 0:
                return True
            cx = float(box.get("x", 0)) + float(box.get("width", 0)) / 2
            cy = float(box.get("y", 0)) + float(box.get("height", 0)) / 2
            right_limit = 0.985 if require_list_with_items else 0.86
            if cx < vw * 0.18 or cx > vw * right_limit:
                return False
            if cy < vh * 0.04 or cy > vh * 0.98:
                return False
            return True
        except Exception:
            return True

    for selector in selectors:
        if deadline is not None and time.monotonic() >= deadline:
            diag["budget_expired"] = True
            break
        try:
            locator = page.locator(selector)
            count = await locator.count()
            diag["selector_counts"][selector] = count
            if count <= 0:
                continue
            for index in range(min(count, 8)):
                if deadline is not None and time.monotonic() >= deadline:
                    diag["budget_expired"] = True
                    break
                candidate = locator.nth(index)
                try:
                    visible_timeout = _timeout_ms(500)
                    if visible_timeout is None:
                        diag["budget_expired"] = True
                        break
                    if not await candidate.is_visible(timeout=visible_timeout):
                        continue
                except Exception:
                    diag["errors"] += 1
                    continue
                diag["visible_candidates"] += 1
                try:
                    box = await candidate.bounding_box()
                except Exception:
                    box = None
                if box:
                    box_key = (
                        int(round(box.get("x", 0))),
                        int(round(box.get("y", 0))),
                        int(round(box.get("width", 0))),
                        int(round(box.get("height", 0))),
                    )
                    if box_key in tried_boxes:
                        diag["deduped_boxes"] += 1
                        continue
                    tried_boxes.add(box_key)
                    if not _box_looks_like_target(box):
                        diag["off_target_candidates"] += 1
                        logger.debug(
                            "[DOM] Skipping off-target comment trigger selector=%s index=%d box=%s viewport=%s",
                            selector,
                            index,
                            box_key,
                            viewport,
                        )
                        continue
                try:
                    scroll_timeout = _timeout_ms(1000)
                    if scroll_timeout is not None:
                        await candidate.scroll_into_view_if_needed(timeout=scroll_timeout)
                except Exception:
                    pass
                click_timeout = _timeout_ms(1500)
                if click_timeout is None:
                    diag["budget_expired"] = True
                    break
                await candidate.click(timeout=click_timeout, force=True)
                diag["playwright_clicks"] += 1
                sleep_for = 1.0
                remaining = _remaining_seconds()
                if remaining is not None:
                    sleep_for = min(sleep_for, max(0.0, remaining))
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                state = await _comment_surface_state(page)
                _remember_state(state)
                if _comment_surface_ready(
                    state,
                    require_input=require_input,
                    require_list_with_items=require_list_with_items,
                    aggressive=aggressive,
                ):
                    logger.info(
                        "[DOM] Opened comments via Playwright fallback selector=%s index=%d state=%s",
                        selector,
                        index,
                        {
                            "input": bool(state.get("input_visible")),
                            "list": bool(state.get("list_visible")),
                            "items": int(state.get("comment_items_visible") or 0),
                        },
                    )
                    return True
                diag["playwright_not_ready"] += 1
        except Exception:
            diag["errors"] += 1
            continue

    try:
        icons = await page.query_selector_all('[data-e2e="comment-icon"]')
        diag["js_icons"] = len(icons)
        for index, icon in enumerate(icons[:8]):
            if deadline is not None and time.monotonic() >= deadline:
                diag["budget_expired"] = True
                break
            try:
                visible = await icon.is_visible()
            except Exception:
                visible = False
            if not visible:
                continue
            diag["js_visible_icons"] += 1
            clicked = await icon.evaluate(
                r"""
                (icon, allowRightRail) => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const button = icon.closest('button, [role="button"], div[tabindex], a');
                    if (!button || !visible(button)) return false;
                    const r = button.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const vw = window.innerWidth || 0;
                    const vh = window.innerHeight || 0;
                    const rightLimit = allowRightRail ? 0.985 : 0.86;
                    if (vw > 0 && (cx < vw * 0.18 || cx > vw * rightLimit)) return false;
                    if (vh > 0 && (cy < vh * 0.04 || cy > vh * 0.98)) return false;
                    try { button.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (_) {}
                    for (const type of ['pointerover', 'mouseover', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        try {
                            const eventInit = { bubbles: true, cancelable: true, view: window, pointerType: 'mouse' };
                            const Ev = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                            button.dispatchEvent(new Ev(type, eventInit));
                        } catch (_) {
                            try {
                                button.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                            } catch (__) {}
                        }
                    }
                    return true;
                }
                """,
                bool(require_list_with_items),
            )
            if not clicked:
                continue
            diag["js_clicked_icons"] += 1
            sleep_for = 1.0
            remaining = _remaining_seconds()
            if remaining is not None:
                sleep_for = min(sleep_for, max(0.0, remaining))
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            state = await _comment_surface_state(page)
            _remember_state(state)
            if _comment_surface_ready(
                state,
                require_input=require_input,
                require_list_with_items=require_list_with_items,
                aggressive=aggressive,
            ):
                logger.info(
                    "[DOM] Opened comments via JS event fallback on comment-icon parent index=%d state=%s",
                    index,
                    {
                        "input": bool(state.get("input_visible")),
                        "list": bool(state.get("list_visible")),
                        "items": int(state.get("comment_items_visible") or 0),
                    },
                )
                return True
            diag["js_not_ready"] += 1
    except Exception:
        diag["errors"] += 1
        pass

    logger.info("[COMMENT_TRIGGER_TRACE] %s", json.dumps(diag, ensure_ascii=False)[:1800])
    return False


async def open_comment_section(
    page: Page,
    max_wait: float = 3.0,
    *,
    require_input: bool = False,
    require_list_with_items: bool = False,
    aggressive: bool = False,
    main_comment_aggressive: bool = False,
) -> bool:
    """
    Open the comment section with increasing waits.

    Modes:
    - require_input=True: returns True when comment input becomes visible
      (publish flow needs the textbox).
    - require_list_with_items=True: returns True ONLY when the comment-list
      container is visible AND has at least one comment item. Use this for
      observer/reply lookup where any "loose" success leads to false positives
      (matching sidebar feed cards via <li>/<article> page-wide selectors).
    - default (no flags): legacy loose mode — any surface signal counts.
    """
    import time as _time

    # Do NOT run global dismiss_overlays() here.
    # TikTok desktop comment UI can live inside/near overlay-like wrappers.
    # A global sweep can hide or make the comment panel non-interactive.
    # Only suppress the known keyboard-shortcuts popup; actual clicks use force/targeted logic.
    await suppress_keyboard_shortcuts_overlay(page)
    await dismiss_cookie_banners(page)
    started = _time.monotonic()
    deadline = started + max(0.1, max_wait)

    def _remaining() -> float:
        return max(0.0, deadline - _time.monotonic())

    def _bounded_timeout_ms(cap_ms: int, *, floor_ms: int = 50) -> Optional[int]:
        remaining = _remaining()
        if remaining <= 0:
            return None
        return max(floor_ms, min(cap_ms, int(remaining * 1000)))

    def _is_ready(state: dict[str, Any]) -> bool:
        return _comment_surface_ready(
            state,
            require_input=require_input,
            require_list_with_items=require_list_with_items,
            aggressive=aggressive,
        )

    def _log_surface_trace(
        stage: str,
        *,
        attempt: Optional[int] = None,
        result: Optional[dict[str, Any]] = None,
        state: Optional[dict[str, Any]] = None,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "stage": stage,
            "attempt": attempt,
            "mode": (
                "list_with_items"
                if require_list_with_items
                else "input"
                if require_input
                else "loose"
            ),
            "url": getattr(page, "url", None),
        }
        if result:
            payload["opener"] = {
                "action": result.get("action"),
                "reason": result.get("reason"),
                "already_open": result.get("already_open"),
                "click": result.get("click"),
                "input_pending": result.get("input_pending"),
                "list_visible": result.get("list_visible"),
            }
        if state:
            list_source = state.get("list_source") or {}
            if not isinstance(list_source, dict):
                list_source = {}
            payload["surface"] = {
                "input": bool(state.get("input_visible")),
                "publish_input_candidate": bool(state.get("publish_input_candidate_visible")),
                "list": bool(state.get("list_visible")),
                "items": int(state.get("comment_items_visible") or 0),
                "strong_items": int(list_source.get("strong_items") or 0),
                "scrollable": bool(list_source.get("scrollable")),
                "list_source": state.get("list_source"),
            }
        payload.update({k: v for k, v in fields.items() if v is not None})
        logger.log(level, "[COMMENT_SURFACE_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:1800])

    async def _visible_captcha_kind() -> Optional[str]:
        try:
            from executor.captcha_detector import detect_captcha

            has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
            if has_captcha:
                return captcha_type or "unknown"
        except Exception:
            return None
        return None

    captcha_kind = await _visible_captcha_kind()
    if captcha_kind:
        _log_surface_trace(
            "captcha_detected",
            level=logging.WARNING,
            error=f"captcha_challenge:{captcha_kind}",
        )
        return False

    async def _wait_for_strict_items(
        state: dict[str, Any],
        *,
        attempt: int,
        result: Optional[dict[str, Any]],
        result_status: str,
    ) -> bool:
        if not require_list_with_items:
            return False
        if not state.get("list_visible") or _is_ready(state):
            return False
        remaining = max_wait - (_time.monotonic() - started)
        if remaining <= 0.5:
            return False
        timeout_ms = max(500, min(int(remaining * 1000), 7000))
        _log_surface_trace(
            "hydrate_wait_start",
            attempt=attempt,
            result=result,
            state=state,
            result_status=result_status,
            timeout_ms=timeout_ms,
        )
        try:
            nudge = await page.evaluate(
                r"""
                () => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const listSelectors = [
                        '[data-e2e="comment-list"]',
                        '[data-e2e="comment-list-container"]',
                        '[class*="CommentList"]',
                        '[class*="DivCommentListContainer"]',
                        '[class*="CommentContainer"]',
                    ].join(', ');
                    const strongItemSelectors = [
                        '[data-e2e="comment-item"]',
                        '[data-e2e*="comment-level"]',
                        '[data-e2e*="comment-text"]',
                        '[data-e2e*="comment-username"]',
                        '[class*="DivCommentObject"]',
                        '[data-cid]:not([data-cid^="one-column-item"])',
                        '[data-comment-id]',
                    ].join(', ');
                    const candidates = [...document.querySelectorAll(listSelectors)]
                        .filter(visible)
                        .map((el) => ({
                            el,
                            strongItems: [...el.querySelectorAll(strongItemSelectors)].filter(visible).length,
                        }))
                        .sort((a, b) => b.strongItems - a.strongItems);
                    const list = candidates[0] && candidates[0].el;
                    if (!list) return { nudged: false, reason: 'no_list' };
                    try {
                        list.scrollTop += Math.max(40, Math.floor((list.clientHeight || 0) * 0.5));
                        return {
                            nudged: true,
                            scrollTop: list.scrollTop,
                            clientHeight: list.clientHeight,
                            scrollHeight: list.scrollHeight,
                        };
                    } catch (e) {
                        return { nudged: false, reason: String(e).slice(0, 80) };
                    }
                }
                """
            )
            _log_surface_trace(
                "hydrate_wait_nudge",
                attempt=attempt,
                result=result,
                state=state,
                result_status=result_status,
                nudge=nudge,
            )
        except Exception as exc:
            _log_surface_trace(
                "hydrate_wait_nudge_failed",
                attempt=attempt,
                result=result,
                state=state,
                result_status=result_status,
                error=str(exc)[:120],
                level=logging.DEBUG,
            )
        try:
            await page.wait_for_function(
                r"""
                () => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const listSelectors = [
                        '[data-e2e="comment-list"]',
                        '[data-e2e="comment-list-container"]',
                        '[class*="CommentList"]',
                        '[class*="DivCommentListContainer"]',
                        '[class*="CommentContainer"]',
                    ].join(', ');
                    const itemSelectors = [
                        '[data-e2e*="comment-level"]',
                        '[data-e2e*="comment-text"]',
                        '[data-e2e*="comment-username"]',
                        '[class*="DivCommentObject"]',
                        '[data-cid]:not([data-cid^="one-column-item"])',
                        '[data-comment-id]',
                    ].join(', ');
                    for (const list of [...document.querySelectorAll(listSelectors)].filter(visible)) {
                        if ([...list.querySelectorAll(itemSelectors)].filter(visible).length > 0) return true;
                    }
                    return false;
                }
                """,
                timeout=timeout_ms,
                polling=250,
            )
        except Exception:
            final_state = await _comment_surface_state(page)
            _log_surface_trace(
                "hydrate_wait_timeout",
                attempt=attempt,
                result=result,
                state=final_state,
                result_status=result_status,
                timeout_ms=timeout_ms,
                level=logging.INFO,
            )
            return False
        final_state = await _comment_surface_state(page)
        if _is_ready(final_state):
            _log_surface_trace(
                "ready",
                attempt=attempt,
                result=result,
                state=final_state,
                result_status=f"{result_status}_hydrated",
            )
            return True
        _log_surface_trace(
            "hydrate_wait_not_ready",
            attempt=attempt,
            result=result,
            state=final_state,
            result_status=result_status,
            level=logging.INFO,
        )
        return False

    async def _confirm_selector_ready(
        *,
        attempt: int,
        result: Optional[dict[str, Any]],
        result_status: str,
    ) -> bool:
        if not require_input:
            _log_surface_trace("ready", attempt=attempt, result=result, result_status=result_status)
            return True
        if main_comment_aggressive:
            _log_surface_trace(
                "ready",
                attempt=attempt,
                result=result,
                result_status=f"{result_status}_main_aggressive",
            )
            return True
        state = await _comment_surface_state(page)
        if _is_ready(state):
            _log_surface_trace(
                "ready",
                attempt=attempt,
                result=result,
                state=state,
                result_status=f"{result_status}_state",
            )
            return True
        _log_surface_trace(
            "input_selector_not_comment_ready",
            attempt=attempt,
            result=result,
            state=state,
            result_status=result_status,
            level=logging.INFO,
        )
        return False

    max_open_attempts = 5 if require_list_with_items else (3 if require_input else 3)
    for _attempt in range(max_open_attempts):
        if _remaining() <= 0:
            break

        captcha_kind = await _visible_captcha_kind()
        if captcha_kind:
            _log_surface_trace(
                "captcha_detected",
                attempt=_attempt + 1,
                level=logging.WARNING,
                error=f"captcha_challenge:{captcha_kind}",
            )
            return False

        try:
            result = await page.evaluate(
                _JS_OPEN_COMMENTS_MAIN_FAST
                if main_comment_aggressive and require_input
                else _JS_OPEN_COMMENTS
            )
        except Exception:
            await asyncio.sleep(0.3)
            continue

        if not isinstance(result, dict):
            await asyncio.sleep(0.3)
            continue

        # In strict mode "already_open" / "list_visible" from the opener are not
        # sufficient — they only confirm the drawer surface is present, not that
        # items are rendered. Re-validate via the surface state probe before
        # accepting.
        if require_list_with_items:
            state = await _comment_surface_state(page)
            _log_surface_trace("state_after_opener", attempt=_attempt + 1, result=result, state=state)
            if _is_ready(state):
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="ok")
                return True
            if await _wait_for_strict_items(
                state,
                attempt=_attempt + 1,
                result=result,
                result_status="opener_state",
            ):
                return True
        else:
            if result.get("already_open"):
                if await _confirm_selector_ready(
                    attempt=_attempt + 1,
                    result=result,
                    result_status="already_open",
                ):
                    return True
            if result.get("list_visible") and not require_input:
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="list_visible")
                return True

        if result.get("input_pending") and require_input:
            try:
                timeout_ms = _bounded_timeout_ms(2500, floor_ms=100)
                if timeout_ms is None:
                    break
                if main_comment_aggressive:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], '
                        'div[contenteditable="true"][data-e2e*="comment"]'
                    )
                else:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                    )
                await page.wait_for_selector(
                    selector,
                    timeout=timeout_ms,
                    state="visible",
                )
                if await _confirm_selector_ready(
                    attempt=_attempt + 1,
                    result=result,
                    result_status="input_pending_visible",
                ):
                    return True
            except Exception:
                await asyncio.sleep(0.4)
                continue

        if result.get("click"):
            trigger = None
            try:
                try:
                    trigger = await page.query_selector('[data-smart-id="comment-open-trigger"]')
                except Exception:
                    trigger = None
                x = float(result.get("x") or 0)
                y = float(result.get("y") or 0)
                w = float(result.get("w") or 0)
                h = float(result.get("h") or 0)
                clicked = False
                if trigger:
                    try:
                        await trigger.click(timeout=1500, force=True)
                        clicked = True
                    except Exception:
                        clicked = False
                if not clicked and w > 0 and h > 0:
                    await page.mouse.click(x + w / 2, y + h / 2)
                    clicked = True
                if not clicked and trigger:
                    await trigger.evaluate(
                        "el => el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }))"
                    )
            except Exception:
                try:
                    if not trigger:
                        trigger = await page.query_selector('[data-smart-id="comment-open-trigger"]')
                    if trigger:
                        await trigger.click(timeout=1500, force=True)
                except Exception:
                    pass

            try:
                selector_timeout = _bounded_timeout_ms(
                    1600 if aggressive and require_input else 4000,
                    floor_ms=100,
                )
                if selector_timeout is None:
                    break
                if require_list_with_items:
                    # In strict mode we wait for an actual comment item to be
                    # rendered, not just the drawer container.
                    selector = (
                        '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                        '[class*="DivCommentObject"], [data-cid]:not([data-cid^="one-column-item"]), [data-comment-id]'
                    )
                elif require_input and main_comment_aggressive:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], '
                        'div[contenteditable="true"][data-e2e*="comment"]'
                    )
                elif require_input:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                    )
                else:
                    selector = (
                        '[data-e2e="comment-input"], [data-e2e="comment-list"], '
                        '[class*="CommentList"], div[contenteditable="true"]'
                    )
                await page.wait_for_selector(
                    selector,
                    timeout=selector_timeout,
                    state="visible",
                )
                if require_list_with_items:
                    # Double-check via surface state: wait_for_selector only checks
                    # one node; we want items count > 0.
                    state = await _comment_surface_state(page)
                    if _is_ready(state):
                        _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="wait_selector_state")
                        return True
                    if await _wait_for_strict_items(
                        state,
                        attempt=_attempt + 1,
                        result=result,
                        result_status="wait_selector_state",
                    ):
                        return True
                elif await _confirm_selector_ready(
                    attempt=_attempt + 1,
                    result=result,
                    result_status="wait_selector",
                ):
                    return True
            except Exception:
                pass

            state = await _comment_surface_state(page)
            if _is_ready(state):
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="post_click_state")
                return True
            if await _wait_for_strict_items(
                state,
                attempt=_attempt + 1,
                result=result,
                result_status="post_click_state",
            ):
                return True
            if require_input and state.get("surface_visible"):
                sleep_for = min(0.2 if aggressive else 0.6, _remaining())
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                continue
            # The plain fallback opener doesn't know about strict mode — we
            # still call it to nudge the UI, then re-verify state strictly.
            fallback_ready = await _click_comment_trigger_fallback(
                page,
                require_input=require_input,
                require_list_with_items=require_list_with_items,
                aggressive=aggressive,
                deadline=deadline,
            )
            if fallback_ready:
                try:
                    if require_list_with_items:
                        selector = (
                            '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                            '[class*="DivCommentObject"], [data-cid]:not([data-cid^="one-column-item"]), [data-comment-id]'
                        )
                    elif require_input and main_comment_aggressive:
                        selector = (
                            '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                            '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], '
                            'div[contenteditable="true"][data-e2e*="comment"]'
                        )
                    else:
                        selector = (
                            '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                            'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                        )
                    selector_timeout = _bounded_timeout_ms(1000 if aggressive and require_input else 2500)
                    if selector_timeout is None:
                        break
                    await page.wait_for_selector(selector, timeout=selector_timeout, state="visible")
                    if require_list_with_items:
                        state = await _comment_surface_state(page)
                        if _is_ready(state):
                            _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="fallback_state")
                            return True
                        if await _wait_for_strict_items(
                            state,
                            attempt=_attempt + 1,
                            result=result,
                            result_status="fallback_state",
                        ):
                            return True
                    elif await _confirm_selector_ready(
                        attempt=_attempt + 1,
                        result=result,
                        result_status="fallback_selector",
                    ):
                        return True
                except Exception:
                    pass
            elif require_list_with_items:
                state = await _comment_surface_state(page)
                if _is_ready(state):
                    _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="fallback_after_click_state")
                    return True
                if await _wait_for_strict_items(
                    state,
                    attempt=_attempt + 1,
                    result=result,
                    result_status="fallback_after_click_no_ready",
                ):
                    return True

        else:
            fallback_ready = await _click_comment_trigger_fallback(
                page,
                require_input=require_input,
                require_list_with_items=require_list_with_items,
                aggressive=aggressive,
                deadline=deadline,
            )
            if not fallback_ready and require_list_with_items:
                state = await _comment_surface_state(page)
                if _is_ready(state):
                    _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="fallback_no_click_post_state")
                    return True
                if await _wait_for_strict_items(
                    state,
                    attempt=_attempt + 1,
                    result=result,
                    result_status="fallback_no_click_no_ready",
                ):
                    return True
            if not fallback_ready:
                await asyncio.sleep(0.2 if aggressive else 0.5)
                continue
            try:
                if require_list_with_items:
                    selector = (
                        '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                        '[class*="DivCommentObject"], [data-cid]:not([data-cid^="one-column-item"]), [data-comment-id]'
                    )
                elif require_input and main_comment_aggressive:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], '
                        'div[contenteditable="true"][data-e2e*="comment"]'
                    )
                elif require_input:
                    selector = (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                    )
                else:
                    selector = (
                        '[data-e2e="comment-input"], [data-e2e="comment-list"], '
                        '[class*="CommentList"], div[contenteditable="true"]'
                    )
                selector_timeout = _bounded_timeout_ms(1000 if aggressive and require_input else 2500)
                if selector_timeout is None:
                    break
                await page.wait_for_selector(selector, timeout=selector_timeout, state="visible")
                if require_list_with_items:
                    state = await _comment_surface_state(page)
                    if _is_ready(state):
                        _log_surface_trace("ready", attempt=_attempt + 1, result=result, state=state, result_status="fallback_no_click_state")
                        return True
                    if await _wait_for_strict_items(
                        state,
                        attempt=_attempt + 1,
                        result=result,
                        result_status="fallback_no_click_state",
                    ):
                        return True
                elif await _confirm_selector_ready(
                    attempt=_attempt + 1,
                    result=result,
                    result_status="fallback_no_click_selector",
                ):
                    return True
            except Exception:
                pass

        sleep_for = min(0.2 if aggressive else 0.5, _remaining())
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    try:
        state = await _comment_surface_state(page)
    except Exception:
        state = None
    if state and _is_ready(state):
        _log_surface_trace(
            "ready",
            state=state,
            result_status="final_state",
            elapsed=round(_time.monotonic() - started, 2),
        )
        return True
    _log_surface_trace("failed", state=state, level=logging.WARNING, elapsed=round(_time.monotonic() - started, 2))
    return False


async def open_reply_box(
    page: Page,
    parent_comment_text: Optional[str] = None,
    parent_comment_id: Optional[str] = None,
    parent_anchor_token: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> DomActionResult:
    started = time.monotonic()
    has_anchor = bool(parent_comment_id or parent_comment_text)
    # When anchor is provided, retry with scroll to handle freshly posted comments.
    default_attempts = 4 if has_anchor else 1
    try:
        max_attempts = int(max_attempts or default_attempts)
    except Exception:
        max_attempts = default_attempts
    max_attempts = max(1, min(max_attempts, default_attempts))
    fast_mode = max_attempts <= 2

    for attempt in range(max_attempts):
        if attempt > 0:
            try:
                await page.evaluate(r"""
                    ([attempt, maxAttempts]) => {
                        const candidates = [
                            ...document.querySelectorAll(
                                '[data-e2e="comment-list"], [class*="CommentList"], [class*="CommentContainer"]'
                            )
                        ];
                        const list = candidates.find((el) => {
                            try { return el.scrollHeight > el.clientHeight + 20; } catch (_) { return false; }
                        }) || candidates[0];
                        if (list) {
                            if (attempt >= maxAttempts - 1) {
                                list.scrollTop = 0;
                            } else {
                                list.scrollTop += Math.max(120, list.clientHeight * 0.25);
                            }
                        } else {
                            window.scrollBy(0, -300);
                        }
                    }
                """, [attempt, max_attempts])
            except Exception:
                pass
            await asyncio.sleep(0.35 if fast_mode else 0.8)

        try:
            result = await page.evaluate(
                _JS_OPEN_REPLY,
                [parent_comment_id or "", parent_comment_text or "", parent_anchor_token or ""],
            )
        except Exception as exc:
            return DomActionResult(False, reason=str(exc)[:120], elapsed_seconds=time.monotonic() - started)

        if isinstance(result, dict) and result.get("reason") == "redirected":
            return DomActionResult(
                False,
                reason="redirected",
                redirected_to=result.get("url"),
                elapsed_seconds=time.monotonic() - started,
            )

        if isinstance(result, dict) and result.get("reason") == "parent_comment_not_found_in_dom":
            if attempt < max_attempts - 1:
                logger.debug(
                    "[DOM] Parent comment not found in DOM (attempt %d/%d), scrolling and retrying",
                    attempt + 1,
                    max_attempts,
                )
                continue
            logger.warning(
                "[DOM] Parent comment not found in DOM after %d attempts: '%s'",
                max_attempts,
                (parent_comment_text or "")[:40],
            )
            return DomActionResult(
                False,
                reason="parent_comment_not_found",
                elapsed_seconds=time.monotonic() - started,
            )

        if isinstance(result, dict) and result.get("clicked"):
            # Wait for the reply input to actually appear rather than sleeping blindly.
            # TikTok animates the reply box open; the contenteditable may take ~500-900ms.
            try:
                await page.wait_for_selector(
                    '[data-e2e="comment-input"], [data-e2e*="reply"] [contenteditable="true"], div[contenteditable="true"]',
                    state="attached",
                    timeout=1200 if fast_mode else 2500,
                )
            except Exception:
                # Fallback: give TikTok at least 0.7s even if selector never appears
                await asyncio.sleep(0.25 if fast_mode else 0.7)
            try:
                activation = await page.evaluate(_JS_REPLY_INPUT_ACTIVE)
            except Exception as exc:
                activation = {"active": False, "reason": f"reply_input_activation_check_failed:{str(exc)[:80]}"}
            if not (isinstance(activation, dict) and activation.get("active")):
                # The old working flow did not require an extra "reply active" marker after
                # clicking Reply. TikTok sometimes opens a perfectly usable scoped composer
                # without exposing a reliable reply label/mention yet, so accept the click if
                # the scoped reply-input finder can already see an editor near the parent
                # anchor. Keep this anchor-gated so we do not publish a normal top-level
                # comment as a reply.
                try:
                    scoped_input = await page.evaluate(
                        _JS_FIND_REPLY_INPUT,
                        [parent_anchor_token or result.get("anchorToken") or ""],
                    )
                except Exception:
                    scoped_input = None
                if (
                    parent_anchor_token or result.get("anchorToken")
                ) and isinstance(scoped_input, dict) and scoped_input.get("found") and (
                    scoped_input.get("strategy") == "reply-scoped-anchor-input"
                    or int(scoped_input.get("score") or 0) >= 8000
                ):
                    return DomActionResult(
                        True,
                        strategy=f"{result.get('strategy') or 'reply-click'}+scoped-input-visible",
                        anchor_token=result.get("anchorToken") or parent_anchor_token,
                        elapsed_seconds=time.monotonic() - started,
                    )
                return DomActionResult(
                    False,
                    reason=(activation or {}).get("reason") or "reply_input_not_activated",
                    elapsed_seconds=time.monotonic() - started,
                )
            return DomActionResult(
                True,
                strategy=result.get("strategy"),
                anchor_token=result.get("anchorToken") or parent_anchor_token,
                elapsed_seconds=time.monotonic() - started,
            )

        return DomActionResult(
            False,
            reason=(result or {}).get("reason") if isinstance(result, dict) else "reply_button_not_found",
            elapsed_seconds=time.monotonic() - started,
        )

    return DomActionResult(False, reason="reply_open_max_attempts", elapsed_seconds=time.monotonic() - started)


async def like_parent_comment_if_safe(
    page: Page,
    *,
    parent_comment_text: Optional[str] = None,
    parent_comment_id: Optional[str] = None,
    parent_anchor_token: Optional[str] = None,
    max_total_seconds: float = 2.0,
) -> DomActionResult:
    """Best-effort like for the exact parent row already found by parent search.

    Failure is diagnostic only. Higher-level reply flow must continue to reply
    unless its own captcha/login/url guards stop the publish attempt.
    """
    started = time.monotonic()
    if not (parent_anchor_token or parent_comment_id or parent_comment_text):
        return DomActionResult(False, reason="missing_parent_locator", elapsed_seconds=0.0)
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                _JS_LIKE_PARENT_COMMENT,
                [parent_comment_id or "", parent_comment_text or "", parent_anchor_token or ""],
            ),
            timeout=max(0.3, max_total_seconds),
        )
    except asyncio.TimeoutError:
        return DomActionResult(False, reason="parent_like_timeout", elapsed_seconds=time.monotonic() - started)
    except Exception as exc:
        return DomActionResult(False, reason=str(exc)[:120], elapsed_seconds=time.monotonic() - started)

    elapsed = time.monotonic() - started
    if isinstance(result, dict):
        if result.get("success"):
            strategy = str(result.get("strategy") or "parent-like")
            if result.get("already_liked"):
                strategy = f"{strategy}:already_liked"
            elif result.get("clicked"):
                strategy = f"{strategy}:clicked"
            return DomActionResult(True, strategy=strategy, elapsed_seconds=elapsed)
        if result.get("reason") == "redirected":
            return DomActionResult(
                False,
                reason="redirected",
                redirected_to=result.get("url"),
                elapsed_seconds=elapsed,
            )
        return DomActionResult(
            False,
            reason=str(result.get("reason") or "parent_like_failed")[:120],
            strategy=str(result.get("strategy") or "") or None,
            elapsed_seconds=elapsed,
        )
    return DomActionResult(False, reason="parent_like_unknown_result", elapsed_seconds=elapsed)


async def ensure_click_target_clear(page: Page, element: Any) -> None:
    """
    Surgical blocker cleanup for a specific clickable/editable element.

    Unlike dismiss_overlays(), this does not sweep the whole page. It checks the
    center point of the target element and only makes the exact blocking element
    click-through if it is clearly not the comment UI itself.
    """
    if element is None:
        return
    try:
        await suppress_keyboard_shortcuts_overlay(page)

        result = await page.evaluate(
            r"""
            (el) => {
                if (!el || !el.getBoundingClientRect) return {blocked:false, reason:'no_element'};

                const commentSelector = [
                    '[data-e2e*="comment" i]',
                    '[data-smart-id*="comment" i]',
                    '[data-e2e="comment-input"]',
                    '[data-e2e="comment-post"]',
                    '[data-smart-id="comment-input"]',
                    '[data-smart-id="comment-post-ready"]',
                    'div[contenteditable="true"]',
                    '[contenteditable="true"]',
                    'textarea',
                    '[role="textbox"]'
                ].join(', ');

                const box = el.getBoundingClientRect();
                if (box.width <= 0 || box.height <= 0) {
                    return {blocked:false, reason:'target_not_visible'};
                }

                const x = box.x + box.width / 2;
                const y = box.y + box.height / 2;
                const topEl = document.elementFromPoint(x, y);

                if (!topEl) return {blocked:false, reason:'no_top_element'};
                if (topEl === el || el.contains(topEl) || topEl.contains(el)) {
                    return {blocked:false, reason:'target_on_top'};
                }

                const topIsCommentUi = Boolean(
                    topEl.matches && topEl.matches(commentSelector) ||
                    topEl.closest && topEl.closest(commentSelector) ||
                    topEl.querySelector && topEl.querySelector(commentSelector)
                );

                if (topIsCommentUi) {
                    return {blocked:true, fixed:false, reason:'comment_ui_on_top'};
                }

                const classes = (topEl.className || '').toString();
                const aria = (topEl.getAttribute && topEl.getAttribute('aria-label') || '').toString();
                const text = (topEl.innerText || topEl.textContent || '').toString();
                const blockerLabel = (classes + ' ' + aria + ' ' + text).toLowerCase();
                const challengeMarkers = [
                    'drag the slider',
                    'fit the puzzle',
                    'slide to verify',
                    'security verification',
                    'complete verification',
                    'captcha'
                ];
                if (challengeMarkers.some(marker => blockerLabel.includes(marker))) {
                    return {
                        blocked:true,
                        fixed:false,
                        reason:'challenge_blocker',
                        tag: topEl.tagName,
                        classes: classes.slice(0, 120),
                        aria: aria.slice(0, 120),
                        text: text.slice(0, 120),
                    };
                }

                try {
                    topEl.style.setProperty('pointer-events', 'none', 'important');
                    // Do not set display:none. z-index only for clear notifications/overlays.
                    if (
                        /notice|notification|toast|snackbar|alert|banner|modal|overlay/i.test(classes + ' ' + aria)
                    ) {
                        topEl.style.setProperty('z-index', '-1', 'important');
                    }
                    return {
                        blocked:true,
                        fixed:true,
                        tag: topEl.tagName,
                        classes: classes.slice(0, 120),
                        aria: aria.slice(0, 120),
                        text: text.slice(0, 120),
                    };
                } catch (e) {
                    return {blocked:true, fixed:false, reason:String(e).slice(0,120)};
                }
            }
            """,
            element,
        )

        if isinstance(result, dict) and result.get("blocked"):
            if result.get("fixed"):
                logger.info("[DOM] Cleared blocker over target: %s", result)
            else:
                logger.debug("[DOM] Target blocked but not modified: %s", result)
    except Exception as exc:
        logger.debug("[DOM] ensure_click_target_clear failed: %s", exc)


async def dismiss_cookie_banners(page: Page) -> int:
    try:
        result = await page.evaluate(_JS_DISMISS_COOKIE_BANNERS)
        count = int((result or {}).get("count", 0)) if isinstance(result, dict) else 0
        if count:
            logger.info("[DOM] Dismissed cookie banner: %s", result)
            await asyncio.sleep(0.35)
        return count
    except Exception as exc:
        logger.debug("[DOM] dismiss_cookie_banners failed: %s", exc)
        return 0



async def dismiss_overlays(page: Page) -> int:
    """
    Dismiss known TikTok overlays without hiding the comment UI.

    Safety rules:
    - keep suppress_keyboard_shortcuts_overlay(), because publish_action imports it;
    - never broad-hide [class*="Overlay"] wrappers;
    - after cleanup, verify comment input is still visible and restore hidden parents if needed.
    """
    total = 0
    total += await suppress_keyboard_shortcuts_overlay(page)
    total += await dismiss_cookie_banners(page)

    try:
        result = await page.evaluate(_JS_DISMISS_OVERLAYS)
        if isinstance(result, dict) and result.get("count", 0) > 0:
            dismissed = result.get("dismissed", [])
            logger.info("[DOM] Dismissed %d overlay(s): %s", result["count"], dismissed)
            total += int(result["count"])
            await asyncio.sleep(0.25)

            # Safety check: if comment input existed but became hidden, undo hidden parents.
            try:
                still_visible = await page.evaluate(r"""
                    () => {
                        const input = document.querySelector(
                            '[data-smart-id="comment-input"], [data-e2e="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                        );
                        if (!input) return { exists: false };
                        const r = input.getBoundingClientRect();
                        const st = getComputedStyle(input);
                        return {
                            exists: true,
                            visible: r.width > 20 && r.height > 10 && st.display !== 'none' && st.visibility !== 'hidden',
                            w: Math.round(r.width),
                            h: Math.round(r.height),
                        };
                    }
                """)
                if isinstance(still_visible, dict) and still_visible.get("exists") and not still_visible.get("visible"):
                    logger.warning("[DOM] Comment input became invisible after overlay dismiss; restoring parents...")
                    await page.evaluate(r"""
                        () => {
                            const input = document.querySelector(
                                '[data-smart-id="comment-input"], [data-e2e="comment-input"], div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                            );
                            if (!input) return;
                            let el = input;
                            for (let i = 0; i < 12 && el && el !== document.body; i++, el = el.parentElement) {
                                try {
                                    el.style.removeProperty('display');
                                    el.style.removeProperty('visibility');
                                    el.style.removeProperty('opacity');
                                    el.style.removeProperty('pointer-events');
                                    el.style.removeProperty('z-index');
                                } catch (_) {}
                            }
                        }
                    """)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("[DOM] Overlay dismissal error: %s", exc)

    total += await suppress_keyboard_shortcuts_overlay(page)
    return total


async def find_comment_input_smart(
    page: Page,
    *,
    max_total_seconds: float = 15.0,
    aggressive: bool = False,
    skip_initial_open: bool = False,
    main_comment_aggressive: bool = False,
) -> tuple[Optional[Any], DomActionResult]:
    started = time.monotonic()
    attempt = 0

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    def _budget_exhausted(grace_seconds: float = 0.03) -> bool:
        return _remaining() <= grace_seconds

    def _as_element_handle(handle: Any) -> Optional[Any]:
        if not handle:
            return None
        as_element = getattr(handle, "as_element", None)
        if callable(as_element):
            try:
                return as_element()
            except Exception:
                return None
        return handle

    async def _fast_existing_input_probe() -> tuple[Optional[Any], Optional[str]]:
        """Cheap first grab for a visible composer in an already-open panel.

        This intentionally avoids broad text/ancestor scans. Under several
        visible Chromium windows those scans can stall long enough that a good
        browser looks idle and gets replaced. Prefer stable TikTok comment
        composer selectors first, then let the heavier legacy probe run only
        when this fast path cannot prove a safe input.
        """
        selectors = (
            '[data-smart-id="comment-input"]',
            '[data-e2e="comment-input"] div[contenteditable="true"]',
            '[data-e2e="comment-input"] [contenteditable="true"]',
            '[data-e2e*="comment-input"] div[contenteditable="true"]',
            '[data-e2e*="comment-input"] [contenteditable="true"]',
            '[data-e2e*="CommentInput"] div[contenteditable="true"]',
            '[data-e2e*="CommentInput"] [contenteditable="true"]',
            '[data-e2e="comment-input"] textarea',
            '[data-e2e*="comment-input"] textarea',
        )
        for selector in selectors:
            try:
                handle = await page.query_selector(selector)
            except Exception:
                handle = None
            if not handle:
                continue
            try:
                state = await page.evaluate(
                    r"""
                    el => {
                        const labelOf = (node) => {
                            if (!node) return '';
                            return [
                                node.getAttribute && (node.getAttribute('data-e2e') || ''),
                                node.getAttribute && (node.getAttribute('aria-label') || ''),
                                node.getAttribute && (node.getAttribute('placeholder') || ''),
                                node.getAttribute && (node.getAttribute('data-placeholder') || ''),
                                node.id || '',
                                String(node.className || ''),
                                node.tagName || '',
                            ].join(' ').toLowerCase();
                        };
                        const visibleEnough = (node) => {
                            if (!node || !node.getBoundingClientRect) return false;
                            const r = node.getBoundingClientRect();
                            const st = getComputedStyle(node);
                            if (r.width > 20 && r.height > 4 && st.display !== 'none' && st.visibility !== 'hidden') {
                                return true;
                            }
                            const parent = node.closest && node.closest('[data-e2e*="comment" i], [class*="comment" i], [aria-label*="comment" i]');
                            if (!parent || !parent.getBoundingClientRect) return false;
                            const pr = parent.getBoundingClientRect();
                            const ps = getComputedStyle(parent);
                            return pr.width > 50 && pr.height > 10 && ps.display !== 'none' && ps.visibility !== 'hidden';
                        };
                        let scoped = false;
                        let bad = false;
                        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                        for (let cur = el, depth = 0; cur && cur !== document.body && depth < 8; cur = cur.parentElement, depth++) {
                            const label = labelOf(cur);
                            if (
                                label.includes('search') ||
                                label.includes('captcha') ||
                                label.includes('verification') ||
                                label.includes('challenge') ||
                                label.includes('login') ||
                                label.includes('sign in') ||
                                label.includes('signup') ||
                                authTokenLike(label)
                            ) {
                                bad = true;
                                break;
                            }
                            if (label.includes('comment') || label.includes('reply')) scoped = true;
                        }
                        return { ok: !bad && scoped && visibleEnough(el) };
                    }
                    """,
                    handle,
                )
                if isinstance(state, dict) and state.get("ok"):
                    try:
                        await handle.evaluate("el => el.setAttribute('data-smart-id', 'comment-input')")
                    except Exception:
                        pass
                    return handle, "fast-existing-input"
            except Exception as exc:
                logger.debug("[DOM] Fast existing input candidate failed for %s: %s", selector, exc)
        return None, None

    async def _trusted_existing_input_probe() -> tuple[Optional[Any], Optional[str]]:
        """Very cheap grab when open_comment_section/surface state already proved composer visibility."""
        selectors = (
            '[data-smart-id="comment-input"]',
            '[data-e2e="comment-input"]',
            '[data-e2e*="comment-input"]',
            '[data-e2e*="CommentInput"]',
        )

        async def _inner_editor_or_self(handle: Any, selector: str) -> tuple[Any, str]:
            if selector == '[data-smart-id="comment-input"]':
                return handle, "trusted-smart-id-input"
            try:
                inner = await handle.query_selector(
                    'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                )
            except Exception:
                inner = None
            if inner:
                return inner, "trusted-comment-input-inner"
            return handle, "trusted-comment-input-wrapper"

        async def _has_usable_box(candidate: Any, wrapper: Any) -> bool:
            for node in (candidate, wrapper):
                if not node:
                    continue
                try:
                    box = await node.bounding_box()
                except Exception:
                    box = None
                if box and box.get("width", 0) > 30 and box.get("height", 0) > 4:
                    return True
            return False

        for selector in selectors:
            try:
                handle = await page.query_selector(selector)
            except Exception:
                handle = None
            if not handle:
                continue
            candidate, strategy = await _inner_editor_or_self(handle, selector)
            if (
                selector == '[data-smart-id="comment-input"]'
                or strategy == "trusted-comment-input-inner"
                or await _has_usable_box(candidate, handle)
            ):
                try:
                    await candidate.evaluate("el => el.setAttribute('data-smart-id', 'comment-input')")
                except Exception:
                    pass
                return candidate, strategy
        return None, None

    async def _direct_existing_input_probe() -> tuple[Optional[Any], Optional[str]]:
        """Fast path for already-open TikTok comment panels.

        The full JS probe is intentionally broad and can be slow under four
        visible Chromium windows. When open_comment_section already exposed the
        composer, try to grab the real editable immediately before diagnostics.
        """
        try:
            handle = await page.evaluate_handle(
                r"""
                () => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                            st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const labelOf = (el) => {
                        if (!el) return '';
                        return [
                            el.getAttribute && (el.getAttribute('data-e2e') || ''),
                            el.getAttribute && (el.getAttribute('aria-label') || ''),
                            el.getAttribute && (el.getAttribute('placeholder') || ''),
                            el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                            el.id || '',
                            String(el.className || ''),
                            el.tagName || '',
                        ].join(' ').toLowerCase();
                    };
                    const badMarkers = [
                        'search', 'captcha', 'verification', 'security check',
                        'challenge', 'login', 'log in', 'sign in', 'signup',
                        'sign up', 'phone / email', 'email / username',
                    ];
                    const insideBadSurface = (el) => {
                        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                        let cur = el;
                        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                            const label = labelOf(cur);
                            const text = (cur.innerText || cur.textContent || '').toLowerCase();
                            if (authTokenLike(label) || authTokenLike(text) || badMarkers.some(marker => label.includes(marker) || text.includes(marker))) return true;
                        }
                        return false;
                    };
                    const isCommentScoped = (el) => {
                        let cur = el;
                        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                            const label = labelOf(cur);
                            if (badMarkers.some(marker => label.includes(marker))) return false;
                            if (label.includes('comment') || label.includes('reply')) return true;
                        }
                        return false;
                    };
                    const isRealEditor = (el) => el && (
                        el.isContentEditable ||
                        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
                        el.tagName === 'TEXTAREA' ||
                        el.tagName === 'INPUT' ||
                        (el.getAttribute && el.getAttribute('role') === 'textbox')
                    );
                    const visibleEnoughOrParent = (el, parent) => {
                        if (visible(el)) return true;
                        if (!parent || !parent.getBoundingClientRect) return false;
                        const r = parent.getBoundingClientRect();
                        const st = getComputedStyle(parent);
                        return r.width > 50 && r.height > 10 &&
                            st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const candidates = [
                        ...document.querySelectorAll(
                            '[data-smart-id="comment-input"], [data-e2e="comment-input"], ' +
                            '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], ' +
                            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                        )
                    ];
                    for (const raw of candidates) {
                        if (!raw || insideBadSurface(raw)) continue;
                        const inner = raw.querySelector && raw.querySelector(
                            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                        );
                        const chosen = isRealEditor(inner) ? inner : (isRealEditor(raw) ? raw : (inner || raw));
                        if (!chosen || insideBadSurface(chosen)) continue;
                        if (!isCommentScoped(raw) && !isCommentScoped(chosen)) continue;
                        if (!visibleEnoughOrParent(chosen, raw)) continue;
                        try { chosen.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
                        return chosen;
                    }
                    return null;
                }
                """
            )
            is_null = await page.evaluate("el => el === null", handle)
            if is_null:
                return None, None
            return handle, "direct-existing-input"
        except Exception as exc:
            logger.debug("[DOM] Direct comment input probe failed: %s", exc)
            return None, None

    async def _resolve_legacy_js_input(strategy: Optional[str]) -> tuple[Optional[Any], Optional[str]]:
        """Resolve the editor marked by the legacy TikTok JS probe.

        The old working flow trusted _JS_FIND_COMMENT_INPUT early. Keep that
        speed, but still resolve wrapper nodes to the real inner editor so
        typing updates React instead of only wrapper textContent.
        """
        element = None
        element_source = None
        try:
            element = await page.query_selector('[data-smart-id="comment-input"]')
            if element:
                element_source = "smart-id"
        except Exception:
            element = None

        if not element:
            try:
                wrapper = await page.query_selector(
                    '[data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"]'
                )
                if wrapper:
                    try:
                        inner = await wrapper.query_selector(
                            'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                        )
                        if inner:
                            box = await inner.bounding_box()
                            if not box or box.get("height", 0) <= 5:
                                try:
                                    await wrapper.click(timeout=800, force=True)
                                    await asyncio.sleep(0.12)
                                    inner_refresh = await wrapper.query_selector(
                                        'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                                    )
                                    if inner_refresh:
                                        inner = inner_refresh
                                except Exception:
                                    pass
                            element = inner
                            element_source = "data-e2e-inner-editable"
                        else:
                            element = wrapper
                            element_source = "data-e2e-wrapper-no-inner"
                    except Exception:
                        element = wrapper
                        element_source = "data-e2e-wrapper-no-inner"
            except Exception:
                element = None

        if not element:
            try:
                element = await page.evaluate_handle(r"""
                    () => {
                        const labelOf = (el) => {
                            if (!el) return '';
                            return [
                                el.getAttribute && (el.getAttribute('data-e2e') || ''),
                                el.getAttribute && (el.getAttribute('aria-label') || ''),
                                el.getAttribute && (el.getAttribute('placeholder') || ''),
                                el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                                el.id || '',
                                String(el.className || ''),
                                el.tagName || '',
                            ].join(' ').toLowerCase();
                        };
                        const badMarkers = [
                            'search', 'captcha', 'verification', 'security check',
                            'challenge', 'login', 'log in', 'sign in', 'signup',
                            'sign up', 'phone / email', 'email / username',
                            'continue with google', 'continue with facebook',
                            'continue with apple', 'password'
                        ];
                        const insideBadSurface = (el) => {
                            const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                            let cur = el;
                            for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                const label = labelOf(cur);
                                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                                if (authTokenLike(label) || authTokenLike(text) || badMarkers.some(marker => label.includes(marker) || text.includes(marker))) return true;
                            }
                            return false;
                        };
                        const isCommentScoped = (el) => {
                            let cur = el;
                            for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                const label = labelOf(cur);
                                if (badMarkers.some(marker => label.includes(marker))) return false;
                                if (label.includes('comment') || label.includes('reply')) return true;
                            }
                            return false;
                        };
                        const visible = (el) => {
                            if (!el || !el.getBoundingClientRect) return false;
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        const selectors = 'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea';
                        for (const el of document.querySelectorAll(selectors)) {
                            if (!isCommentScoped(el) || insideBadSurface(el)) continue;
                            const parent = el.closest && el.closest('[data-e2e*="comment" i], [class*="comment" i], [aria-label*="comment" i]');
                            if (visible(el) || visible(parent)) {
                                try { el.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
                                return el;
                            }
                        }
                        return null;
                    }
                """)
                is_null = await page.evaluate("el => el === null", element)
                if is_null:
                    element = None
                else:
                    element_source = "visible-editable"
            except Exception:
                element = None

        if not element:
            return None, None

        final_strategy = strategy or "legacy-js"
        if element_source and element_source not in ("smart-id", None) and element_source != final_strategy:
            final_strategy = f"fallback:{final_strategy}:{element_source}"
        return element, final_strategy

    async def _legacy_js_input_probe() -> tuple[Optional[Any], Optional[str], Optional[DomActionResult]]:
        js_result = await page.evaluate(_JS_FIND_COMMENT_INPUT)
        if isinstance(js_result, dict) and js_result.get("found"):
            element, strategy = await _resolve_legacy_js_input(js_result.get("strategy"))
            if element:
                return element, strategy or js_result.get("strategy") or "legacy-js", None
        if isinstance(js_result, dict) and js_result.get("reason") == "redirected":
            return None, None, DomActionResult(
                False,
                reason="redirected",
                redirected_to=js_result.get("url"),
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None, None

    async def _old_style_handle_probe() -> tuple[Optional[Any], Optional[str], Optional[DomActionResult]]:
        handle = await page.evaluate_handle(_JS_FIND_COMMENT_INPUT_HANDLE)
        element = _as_element_handle(handle)
        if element:
            strategy = "old-js-handle"
            try:
                strategy = await element.evaluate(
                    "el => el.getAttribute('data-smart-strategy') || 'old-js-handle'"
                )
            except Exception:
                pass
            return element, strategy or "old-js-handle", None

        try:
            probe_state = await page.evaluate("() => window.__smartCommentInputProbeState || null")
        except Exception:
            probe_state = None
        if isinstance(probe_state, dict) and probe_state.get("reason") == "redirected":
            return None, None, DomActionResult(
                False,
                reason="redirected",
                redirected_to=probe_state.get("url"),
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None, None

    async def _unified_input_resolver_probe() -> tuple[Optional[Any], Optional[str], Optional[DomActionResult]]:
        handle = await page.evaluate_handle(_JS_RESOLVE_COMMENT_INPUT_HANDLE)
        element = _as_element_handle(handle)
        if element:
            return element, "unified-comment-input", None

        try:
            probe_state = await page.evaluate("() => window.__smartCommentInputResolverState || null")
        except Exception:
            probe_state = None
        if isinstance(probe_state, dict) and probe_state.get("reason") == "redirected":
            return None, None, DomActionResult(
                False,
                reason="redirected",
                redirected_to=probe_state.get("url"),
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None, None

    async def _bounded_call(label: str, timeout_seconds: float, factory) -> Any:
        remaining = _remaining()
        if remaining <= 0:
            raise asyncio.TimeoutError(label)
        timeout = min(timeout_seconds, remaining)
        if timeout <= 0:
            raise asyncio.TimeoutError(label)
        try:
            return await asyncio.wait_for(factory(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[DOM] %s timed out in find_comment_input_smart after %.1fs (budget %.1fs)",
                label,
                timeout,
                max_total_seconds,
            )
            raise

    async def _try_old_style_handle(label: str, timeout_seconds: float) -> tuple[Optional[Any], Optional[DomActionResult]]:
        try:
            input_handle, strategy, terminal_result = await _bounded_call(
                label,
                min(timeout_seconds, max(0.1, _remaining())),
                _old_style_handle_probe,
            )
        except asyncio.TimeoutError:
            return None, None
        except Exception as exc:
            err = str(exc).lower()
            if "target" in err or "closed" in err:
                return None, DomActionResult(
                    False,
                    reason="page_closed",
                    elapsed_seconds=time.monotonic() - started,
                )
            logger.debug("[DOM] %s failed in find_comment_input_smart: %s", label, exc)
            return None, None
        if terminal_result:
            return None, terminal_result
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None

    async def _try_unified_resolver(label: str, timeout_seconds: float) -> tuple[Optional[Any], Optional[DomActionResult]]:
        try:
            input_handle, strategy, terminal_result = await _bounded_call(
                label,
                min(timeout_seconds, max(0.1, _remaining())),
                _unified_input_resolver_probe,
            )
        except asyncio.TimeoutError:
            return None, None
        except Exception as exc:
            err = str(exc).lower()
            if "target" in err or "closed" in err:
                return None, DomActionResult(
                    False,
                    reason="page_closed",
                    elapsed_seconds=time.monotonic() - started,
                )
            logger.debug("[DOM] %s failed in find_comment_input_smart: %s", label, exc)
            return None, None
        if terminal_result:
            return None, terminal_result
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None

    async def _try_legacy_js_fast(label: str, timeout_seconds: float) -> tuple[Optional[Any], Optional[DomActionResult]]:
        try:
            input_handle, strategy, terminal_result = await _bounded_call(
                label,
                min(timeout_seconds, max(0.1, _remaining())),
                _legacy_js_input_probe,
            )
        except asyncio.TimeoutError:
            return None, None
        except Exception as exc:
            err = str(exc).lower()
            if "target" in err or "closed" in err:
                return None, DomActionResult(
                    False,
                    reason="page_closed",
                    elapsed_seconds=time.monotonic() - started,
                )
            logger.debug("[DOM] %s failed in find_comment_input_smart: %s", label, exc)
            return None, None
        if terminal_result:
            return None, terminal_result
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        return None, None

    if main_comment_aggressive and skip_initial_open and _remaining() > 0.1:
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_main_aggressive_trusted_existing",
                min(0.45 if aggressive else 0.65, max(0.1, _remaining())),
                _trusted_existing_input_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )

    if main_comment_aggressive and _remaining() > 0.1:
        input_handle, result = await _try_old_style_handle(
            "comment_input_main_aggressive_old_js_handle",
            2.8 if aggressive else 3.5,
        )
        if input_handle or result:
            return input_handle, result

    async def _oneshot_surface_probe() -> tuple[Optional[Any], Optional[str]]:
        """Cheap, reliable one-shot: the surface-state evaluate already detects
        the composer and tags the real editor with data-smart-id="comment-input".

        ONE page.evaluate + ONE query_selector. When the composer is visible
        this returns a usable handle immediately, avoiding the long sequential
        probe chain whose summed budgets exceed the caller's total budget.
        """
        try:
            state = await page.evaluate(_JS_COMMENT_SURFACE_STATE)
        except Exception as exc:
            logger.debug("[DOM] One-shot surface probe evaluate failed: %s", str(exc)[:120])
            return None, None
        if not isinstance(state, dict):
            return None, None
        if not (
            state.get("comment_input_visible")
            or state.get("publish_input_candidate_visible")
        ):
            return None, None
        try:
            handle = await page.query_selector('[data-smart-id="comment-input"]')
        except Exception:
            handle = None
        if handle:
            return handle, "oneshot-surface-input"
        return None, None

    # PRIMARY one-shot: try the cheap reliable surface detector FIRST. The
    # heavier sequential probe chain below remains as fallback, each step
    # bounded by _remaining() so the chain can never exceed the total budget.
    if not main_comment_aggressive and _remaining() > 0.05:
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_oneshot_surface_probe",
                min(1.2 if aggressive else 1.5, max(0.1, _remaining())),
                _oneshot_surface_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )

    if skip_initial_open:
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_trusted_existing_probe",
                min(0.35 if aggressive else 0.5, max(0.1, _remaining())),
                _trusted_existing_input_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_surface_ready_direct_probe",
                min(0.75 if aggressive else 1.0, max(0.2, _remaining())),
                _direct_existing_input_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_fast_existing_probe",
                min(0.8 if aggressive else 1.0, max(0.1, _remaining())),
                _fast_existing_input_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        input_handle, result = await _try_unified_resolver(
            "comment_input_unified_surface_probe",
            1.4 if aggressive else 1.8,
        )
        if input_handle or result:
            return input_handle, result
        input_handle, result = await _try_old_style_handle(
            "comment_input_surface_ready_old_js_handle",
            3.2 if aggressive else 4.0,
        )
        if input_handle or result:
            return input_handle, result
        input_handle, result = await _try_legacy_js_fast(
            "comment_input_surface_ready_legacy_probe",
            2.2 if aggressive else 3.0,
        )
        if input_handle or result:
            return input_handle, result
    if not skip_initial_open:
        if _remaining() > 0.35:
            input_handle, result = await _try_unified_resolver(
                "comment_input_unified_pre_open_probe",
                0.6 if aggressive else 0.8,
            )
            if input_handle or result:
                return input_handle, result
        try:
            initial_open_budget = min(1.6 if aggressive else 3.0, max(0.4, max_total_seconds * (0.22 if aggressive else 0.35)))
            await _bounded_call(
                "comment_input_initial_open",
                initial_open_budget,
                lambda: open_comment_section(
                    page,
                    max_wait=min(1.6 if aggressive else 3.0, initial_open_budget),
                    require_input=True,
                    aggressive=aggressive,
                ),
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            if _budget_exhausted():
                return None, DomActionResult(False, "comment_input_open_deadline", elapsed)
            logger.info(
                "[DOM] Initial comment input opener timed out with %.1fs remaining; continuing input probes",
                _remaining(),
            )
        except Exception as exc:
            logger.debug("[DOM] Initial comment input opener failed: %s", exc)
        input_handle, result = await _try_unified_resolver(
            "comment_input_unified_after_open_probe",
            1.2 if aggressive else 1.6,
        )
        if input_handle or result:
            return input_handle, result
        input_handle, result = await _try_old_style_handle(
            "comment_input_old_js_handle_after_open",
            3.0 if aggressive else 4.0,
        )
        if input_handle or result:
            return input_handle, result
        input_handle, result = await _try_legacy_js_fast(
            "comment_input_legacy_fast_js_probe_after_open",
            1.2 if aggressive else 1.8,
        )
        if input_handle or result:
            return input_handle, result
        try:
            input_handle, strategy = await _bounded_call(
                "comment_input_fast_existing_probe_after_open",
                min(0.6 if aggressive else 0.9, max(0.1, _remaining())),
                _fast_existing_input_probe,
            )
        except asyncio.TimeoutError:
            input_handle, strategy = None, None
        if input_handle:
            logger.info(
                "[DOM] Comment input found via %s in %.1fs",
                strategy,
                time.monotonic() - started,
            )
            return input_handle, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )

    cleanup_timeout = min(
        0.45 if aggressive else 0.6,
        max(0.0, _remaining() - 0.25),
    )
    if cleanup_timeout >= 0.12:
        try:
            await _bounded_call(
                "comment_input_cookie_cleanup",
                cleanup_timeout,
                lambda: dismiss_cookie_banners(page),
            )
        except asyncio.TimeoutError:
            logger.info(
                "[DOM] Initial comment input cookie cleanup timed out; continuing input probes with %.1fs remaining",
                _remaining(),
            )
        except Exception as exc:
            logger.debug("[DOM] Initial comment input cookie cleanup failed: %s", exc)

    try:
        diag = await _bounded_call(
            "comment_input_initial_diag",
            min(2.0 if aggressive else 1.0, max(0.1, _remaining())),
            lambda: page.evaluate(_JS_DIAGNOSE_PAGE),
        )
        logger.info(
            "[DOM_DIAG] url=%s editables=%d textareas=%d e2e_comment=%s triggers=%s bodyH=%d",
            diag.get("pathname", "?"),
            diag.get("editableCount", 0),
            diag.get("textareaCount", 0),
            json.dumps(diag.get("dataE2eCommentElements", []), ensure_ascii=False)[:300],
            json.dumps(diag.get("commentTriggerCandidates", []), ensure_ascii=False)[:300],
            diag.get("bodyHeight", 0),
        )
    except Exception:
        pass

    while time.monotonic() - started < max_total_seconds:
        attempt += 1
        try:
            js_result = await _bounded_call(
                "comment_input_js_probe",
                min(2.5 if aggressive else 1.0, max(0.1, _remaining())),
                lambda: page.evaluate(_JS_FIND_COMMENT_INPUT),
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            err = str(exc).lower()
            if isinstance(exc, asyncio.TimeoutError):
                if _budget_exhausted():
                    return None, DomActionResult(False, "comment_input_probe_deadline", elapsed)
            if "target" in err or "closed" in err:
                return None, DomActionResult(False, "page_closed", elapsed)
            if _remaining() <= 0:
                break
            await asyncio.sleep(min(0.3, max(0.05, _remaining())))
            continue

        if attempt <= 3 or attempt % 5 == 0:
            logger.debug(
                "[DOM] find_comment_input attempt=%d js=%s elapsed=%.1fs",
                attempt,
                js_result if isinstance(js_result, dict) else str(js_result)[:80],
                time.monotonic() - started,
            )

        if isinstance(js_result, dict):
            if js_result.get("found"):
                strategy = js_result.get("strategy")
                element = None
                element_source = None

                # Priority 1: smart-id set by JS. Can disappear after React re-render.
                try:
                    element = await page.query_selector('[data-smart-id="comment-input"]')
                    if element:
                        element_source = "smart-id"
                except Exception:
                    element = None

                # Priority 2: stable data-e2e wrapper → resolve INNER editable.
                # Wrapper has placeholder-span as a sibling of the real editor.
                # Returning wrapper for typing caused JS-fallback to write into
                # the wrapper's textContent — placeholder remained visible, React
                # never registered the input, Post stayed disabled. We now
                # ALWAYS resolve to the inner element. If inner has h=0
                # (unfocused TikTok state), we click the wrapper to force focus
                # then re-check — but the returned handle is always the inner.
                if not element:
                    try:
                        wrapper = await page.query_selector('[data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"]')
                        if wrapper:
                            try:
                                inner = await wrapper.query_selector('div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]')
                                if inner:
                                    box = await inner.bounding_box()
                                    if not box or box.get("height", 0) <= 5:
                                        # Inner has h=0 — focus wrapper to render the editor,
                                        # then return inner (re-fetched to be safe).
                                        try:
                                            await wrapper.click(timeout=1500, force=True)
                                            await asyncio.sleep(0.25)
                                            # Re-query inner — after focus, TikTok may have
                                            # replaced the contenteditable node.
                                            inner_refresh = await wrapper.query_selector('div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]')
                                            if inner_refresh:
                                                inner = inner_refresh
                                        except Exception:
                                            pass
                                    element = inner
                                    element_source = "data-e2e-inner-editable"
                                else:
                                    # No inner found inside wrapper — last resort.
                                    # Mark the strategy distinctly so type_into_comment
                                    # knows it must NOT trust this handle for direct
                                    # textContent writes.
                                    element = wrapper
                                    element_source = "data-e2e-wrapper-no-inner"
                            except Exception:
                                element = wrapper
                                element_source = "data-e2e-wrapper-no-inner"
                    except Exception:
                        element = None

                # Priority 3: first visible editable. Covers TikTok A/B where data-e2e is absent.
                if not element:
                    try:
                        element = await page.evaluate_handle(r"""
                            () => {
                                const labelOf = (el) => {
                                    const parts = [
                                        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                                        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                                        el && el.getAttribute && (el.getAttribute('placeholder') || ''),
                                        el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                                        el && (el.id || ''),
                                        el && String(el.className || ''),
                                        el && (el.tagName || ''),
                                    ];
                                    return parts.join(' ').toLowerCase();
                                };
                                const isCommentScoped = (el) => {
                                    const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                                    let cur = el;
                                    for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                        const label = labelOf(cur);
                                        if (
                                            label.includes('search') ||
                                            label.includes('captcha') ||
                                            label.includes('challenge') ||
                                            label.includes('login') ||
                                            label.includes('sign in') ||
                                            label.includes('signup') ||
                                            authTokenLike(label)
                                        ) return false;
                                        if (label.includes('comment') || label.includes('reply')) return true;
                                    }
                                    return false;
                                };
                                const selectors = 'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea';
                                const editables = document.querySelectorAll(selectors);
                                for (const el of editables) {
                                    if (!isCommentScoped(el)) continue;
                                    const r = el.getBoundingClientRect();
                                    const st = getComputedStyle(el);
                                    if (r.width > 50 && r.height > 5 && st.display !== 'none' && st.visibility !== 'hidden') {
                                        return el;
                                    }
                                }
                                return null;
                            }
                        """)
                        if element:
                            try:
                                is_null = await page.evaluate("el => el === null", element)
                                if is_null:
                                    element = None
                                else:
                                    element_source = "visible-editable"
                            except Exception:
                                element_source = "visible-editable"
                    except Exception:
                        element = None

                if element:
                    final_strategy = strategy
                    if element_source and element_source not in ("smart-id", None):
                        final_strategy = f"fallback:{strategy}:{element_source}"
                        logger.info(
                            "[DOM] Found input via fallback. strategy=%s source=%s elapsed=%.1fs",
                            strategy,
                            element_source,
                            time.monotonic() - started,
                        )
                    return element, DomActionResult(
                        True,
                        strategy=final_strategy,
                        elapsed_seconds=time.monotonic() - started,
                    )
            elif js_result.get("reason") == "redirected":
                return None, DomActionResult(
                    False,
                    reason="redirected",
                    redirected_to=js_result.get("url"),
                    elapsed_seconds=time.monotonic() - started,
                )

            if js_result.get("reason") in (None, "not_visible"):
                try:
                    fallback = await page.evaluate_handle(r"""
                        () => {
                            const visible = (el) => {
                                if (!el || !el.getBoundingClientRect) return false;
                                const r = el.getBoundingClientRect();
                                const st = getComputedStyle(el);
                                return r.width > 0 && r.height > 0 &&
                                       st.display !== 'none' && st.visibility !== 'hidden';
                            };
                            const labelOf = (el) => {
                                const parts = [
                                    el.getAttribute && (el.getAttribute('data-e2e') || ''),
                                    el.getAttribute && (el.getAttribute('aria-label') || ''),
                                    el.getAttribute && (el.getAttribute('placeholder') || ''),
                                    el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                                    el.id || '',
                                    String(el.className || ''),
                                    el.tagName || '',
                                ];
                                return parts.join(' ').toLowerCase();
                            };
                            const badMarkers = [
                                'search', 'search-input', 'searchbar', 'find creators',
                                'captcha', 'verification', 'security check', 'challenge',
                                'drag the slider', 'slide to verify', 'fit the puzzle',
                                'login', 'log in', 'sign in', 'signup', 'sign up',
                                'phone / email', 'email / username', 'continue with google',
                                'continue with facebook', 'continue with apple', 'use phone', 'password'
                            ];
                            const challengeTextMarkers = [
                                'captcha', 'verification', 'security check', 'challenge',
                                'drag the slider', 'slide to verify', 'fit the puzzle'
                            ];
                            const insideBadSurface = (el) => {
                                const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                                let cur = el;
                                for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                    const label = labelOf(cur);
                                    if (authTokenLike(label) || badMarkers.some(marker => label.includes(marker))) return true;
                                    const text = (cur.innerText || cur.textContent || '').toLowerCase();
                                    if (text && challengeTextMarkers.some(marker => text.includes(marker))) return true;
                                }
                                return false;
                            };
                            const isCommentScoped = (el) => {
                                let cur = el;
                                for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                    const label = labelOf(cur);
                                    if (badMarkers.some(marker => label.includes(marker))) return false;
                                    if (label.includes('comment') || label.includes('reply')) return true;
                                }
                                return false;
                            };
                            const commentSurface = document.querySelector(
                                '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                                '[class*="CommentList"], [class*="DivCommentListContainer"], [class*="CommentContainer"], ' +
                                '[data-e2e*="comment-level"], [data-e2e*="comment-username"], [data-cid]:not([data-cid^="one-column-item"]), [data-comment-id]'
                            );
                            const surfaceVisible = visible(commentSurface) ||
                                Array.from(document.querySelectorAll('[data-e2e*="comment"], [class*="Comment"]')).some(visible);
                            if (!surfaceVisible) return null;

                            const editables = Array.from(document.querySelectorAll(
                                '[data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"], ' +
                                'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                            ));
                            // Prefer inner real editor over wrapper. Wrapper has
                            // placeholder-span sibling; writing into wrapper leaves
                            // placeholder visible and React state empty.
                            const isRealEditor = (el) => el && (
                                el.isContentEditable ||
                                (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
                                el.tagName === 'TEXTAREA' ||
                                el.tagName === 'INPUT' ||
                                (el.getAttribute && el.getAttribute('role') === 'textbox')
                            );
                            for (const raw of editables) {
                                if (!raw || insideBadSurface(raw)) continue;
                                const inner = raw.querySelector && raw.querySelector(
                                    'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                                );
                                const el = inner || raw;
                                if (insideBadSurface(el)) continue;
                                if (!isCommentScoped(raw) && !isCommentScoped(el)) continue;
                                const er = el.getBoundingClientRect();
                                const pr = raw.getBoundingClientRect();
                                const visibleRaw = visible(raw);
                                const visibleEl = visible(el);
                                const lowerHalf = Math.max(er.top || 0, pr.top || 0) > window.innerHeight * 0.25;
                                if ((visibleEl || visibleRaw) && lowerHalf) {
                                    // Choose real inner editor when available, even if
                                    // its bounding box is currently h=0 — the click in
                                    // type_into_comment will activate it.
                                    let chosen;
                                    if (isRealEditor(inner)) {
                                        chosen = inner;
                                    } else if (isRealEditor(raw)) {
                                        chosen = raw;
                                    } else {
                                        chosen = visibleRaw ? raw : el;
                                    }
                                    chosen.setAttribute('data-smart-id', 'comment-input');
                                    return chosen;
                                }
                            }
                            return null;
                        }
                    """)
                    is_null = await page.evaluate("el => el === null", fallback)
                    if not is_null:
                        logger.info(
                            "[DOM] Comment input recovered via visible editable fallback in %.1fs",
                            time.monotonic() - started,
                        )
                        return fallback, DomActionResult(
                            True,
                            strategy="visible-editable-fallback",
                            elapsed_seconds=time.monotonic() - started,
                        )
                except Exception:
                    pass

        if attempt > 1 and _remaining() > 0.35:
            retry_cleanup_timeout = min(
                0.35 if aggressive else 0.5,
                max(0.0, _remaining() - 0.2),
            )
            try:
                if retry_cleanup_timeout >= 0.1:
                    await _bounded_call(
                        "comment_input_retry_cookie_cleanup",
                        retry_cleanup_timeout,
                        lambda: dismiss_cookie_banners(page),
                    )
            except asyncio.TimeoutError:
                logger.debug(
                    "[DOM] Retry comment input cookie cleanup timed out; continuing input probes"
                )
            except Exception:
                pass
        if not skip_initial_open and attempt == (1 if aggressive else 2):
            retry_open_budget = min(1.2 if aggressive else 2.0, max(0.2, _remaining()))
            try:
                await _bounded_call(
                    "comment_input_retry_open",
                    retry_open_budget,
                    lambda: open_comment_section(
                        page,
                        max_wait=min(1.2 if aggressive else 2.0, retry_open_budget),
                        require_input=True,
                        aggressive=aggressive,
                    ),
                )
            except asyncio.TimeoutError:
                if _remaining() <= 0:
                    break
            except Exception:
                pass
        try:
            await page.evaluate("window.scrollBy(0, 100)")
        except Exception:
            pass
        if _remaining() <= 0:
            break
        await asyncio.sleep(min(0.3 if aggressive else 0.8, max(0.05, _remaining())))

    reason = "timeout"
    try:
        final_diag = await _bounded_call(
            "comment_input_final_diag",
            min(2.0 if aggressive else 1.0, max(0.1, _remaining())),
            lambda: page.evaluate(_JS_DIAGNOSE_PAGE),
        )
        editable_count = int(final_diag.get("editableCount") or 0)
        textarea_count = int(final_diag.get("textareaCount") or 0)
        e2e_comments = final_diag.get("dataE2eCommentElements") or []
        trigger_candidates = final_diag.get("commentTriggerCandidates") or []
        visible_e2e_comments = [
            item for item in e2e_comments
            if isinstance(item, dict) and item.get("visible") and int(item.get("w") or 0) > 0 and int(item.get("h") or 0) > 0
        ]
        visible_triggers = [
            item for item in trigger_candidates
            if isinstance(item, dict) and item.get("visible") and int(item.get("w") or 0) > 0 and int(item.get("h") or 0) > 0
        ]
        if editable_count == 0 and textarea_count == 0 and not visible_e2e_comments and not visible_triggers:
            reason = "comment_ui_not_found"
        elif editable_count == 0 and textarea_count == 0:
            reason = "comment_input_not_visible"
        logger.warning(
            "[DOM] Comment input final diagnostic: reason=%s url=%s editables=%d textareas=%d e2e_comment=%s triggers=%s bodyH=%s",
            reason,
            final_diag.get("pathname", "?"),
            editable_count,
            textarea_count,
            json.dumps(e2e_comments, ensure_ascii=False)[:300],
            json.dumps(trigger_candidates, ensure_ascii=False)[:300],
            final_diag.get("bodyHeight", 0),
        )
        if editable_count > 0:
            try:
                fallback = await page.evaluate_handle(r"""
                    () => {
                        const labelOf = (el) => {
                            if (!el) return '';
                            return [
                                el.getAttribute && (el.getAttribute('data-e2e') || ''),
                                el.getAttribute && (el.getAttribute('aria-label') || ''),
                                el.getAttribute && (el.getAttribute('placeholder') || ''),
                                el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                                el.id || '',
                                String(el.className || ''),
                                el.tagName || '',
                            ].join(' ').toLowerCase();
                        };
                        const visible = (el) => {
                            if (!el || !el.getBoundingClientRect) return false;
                            const r = el.getBoundingClientRect();
                            const st = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                   st.display !== 'none' && st.visibility !== 'hidden';
                        };
                        const insideBadSurface = (el) => {
                            const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
                            let cur = el;
                            for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                const label = labelOf(cur);
                                if (label.includes('search') || label.includes('captcha') ||
                                    label.includes('verification') || label.includes('challenge') ||
                                    label.includes('login') || label.includes('sign in') ||
                                    label.includes('signup') || authTokenLike(label)) {
                                    return true;
                                }
                                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                                if (text.includes('drag the slider') || text.includes('slide to verify') ||
                                    text.includes('fit the puzzle') || text.includes('security verification') ||
                                    text.includes('phone / email') || text.includes('email / username') ||
                                    text.includes('continue with google') || text.includes('continue with facebook') ||
                                    text.includes('continue with apple')) {
                                    return true;
                                }
                            }
                            return false;
                        };
                        const editables = Array.from(document.querySelectorAll(
                            '[data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"], ' +
                            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                        )).filter(el => el && !insideBadSurface(el));
                        if (!editables.length) return null;
                        const visibleEditable = editables.find(visible);
                        const raw = visibleEditable || editables[0];
                        const inner = raw.querySelector && raw.querySelector(
                            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
                        );
                        // PRIORITY: real inner editor over wrapper.
                        // The wrapper [data-e2e="comment-input"] often contains a
                        // placeholder-span sibling. Writing textContent on the wrapper
                        // does not clear the placeholder and does not notify React.
                        // We prefer inner whenever it exists (even if h=0); a later
                        // click/focus step will activate it.
                        const isRealEditor = (el) => el && (
                            el.isContentEditable ||
                            el.getAttribute && el.getAttribute('contenteditable') === 'true' ||
                            el.tagName === 'TEXTAREA' ||
                            el.tagName === 'INPUT' ||
                            (el.getAttribute && el.getAttribute('role') === 'textbox')
                        );
                        let chosen;
                        if (isRealEditor(inner)) {
                            chosen = inner;
                        } else if (isRealEditor(raw)) {
                            chosen = raw;
                        } else {
                            chosen = inner || raw;
                        }
                        try { chosen.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
                        return chosen;
                    }
                """)
                is_null = await page.evaluate("el => el === null", fallback)
                if not is_null:
                    logger.info(
                        "[DOM] Comment input recovered via final editable fallback in %.1fs",
                        time.monotonic() - started,
                    )
                    return fallback, DomActionResult(
                        True,
                        strategy="final-editable-fallback",
                        elapsed_seconds=time.monotonic() - started,
                    )
            except Exception as fallback_exc:
                logger.debug("[DOM] final editable fallback failed: %s", fallback_exc)
    except Exception:
        pass

    return None, DomActionResult(False, reason=reason, elapsed_seconds=time.monotonic() - started)


async def find_reply_input_smart(
    page: Page,
    *,
    parent_anchor_token: Optional[str] = None,
    max_total_seconds: float = 4.0,
) -> tuple[Optional[Any], DomActionResult]:
    """Find the editor opened by clicking Reply, scoped near the parent anchor."""
    started = time.monotonic()
    last_reason: Optional[str] = None

    def _as_element_handle(handle: Any) -> Optional[Any]:
        if not handle:
            return None
        as_element = getattr(handle, "as_element", None)
        if callable(as_element):
            try:
                return as_element()
            except Exception:
                return None
        return handle

    while time.monotonic() - started < max_total_seconds:
        try:
            handle = await page.evaluate_handle(
                _JS_FIND_REPLY_INPUT_HANDLE,
                [parent_anchor_token or ""],
            )
        except Exception as exc:
            await asyncio.sleep(0.2)
            last_reason = str(exc)[:120]
            continue
        element = _as_element_handle(handle)
        if element:
            strategy = "reply-scoped-handle"
            score = None
            try:
                meta = await element.evaluate(
                    """
                    el => ({
                        strategy: el.getAttribute('data-smart-reply-strategy') || 'reply-scoped-handle',
                        score: el.getAttribute('data-smart-reply-score') || null
                    })
                    """
                )
                if isinstance(meta, dict):
                    strategy = meta.get("strategy") or strategy
                    score = meta.get("score")
            except Exception:
                pass
            logger.info(
                "[DOM] Reply input found via %s in %.1fs score=%s",
                strategy,
                time.monotonic() - started,
                score,
            )
            return element, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            js_result = await page.evaluate("() => window.__smartReplyInputProbeState || null")
        except Exception:
            js_result = None
        last_reason = (js_result or {}).get("reason") if isinstance(js_result, dict) else "not_found"
        await asyncio.sleep(0.2)
    return None, DomActionResult(
        False,
        reason=last_reason or "reply_input_timeout",
        elapsed_seconds=time.monotonic() - started,
    )


async def find_reply_post_button_smart(
    page: Page,
    *,
    max_total_seconds: float = 3.0,
) -> tuple[Optional[Any], DomActionResult]:
    """Find the Post button belonging to the currently opened reply composer."""
    started = time.monotonic()
    last_reason: Optional[str] = None

    def _as_element_handle(handle: Any) -> Optional[Any]:
        if not handle:
            return None
        as_element = getattr(handle, "as_element", None)
        if callable(as_element):
            try:
                return as_element()
            except Exception:
                return None
        return handle

    while time.monotonic() - started < max_total_seconds:
        try:
            handle = await page.evaluate_handle(_JS_FIND_REPLY_POST_BUTTON_HANDLE)
        except Exception as exc:
            last_reason = str(exc)[:120]
            await asyncio.sleep(0.2)
            continue
        element = _as_element_handle(handle)
        if element:
            strategy = "reply-post-handle"
            target_tag = None
            try:
                meta = await element.evaluate(
                    """
                    el => ({
                        strategy: el.getAttribute('data-smart-reply-post-strategy') || 'reply-post-handle',
                        targetTag: el.getAttribute('data-smart-reply-post-target') || el.tagName || null
                    })
                    """
                )
                if isinstance(meta, dict):
                    strategy = meta.get("strategy") or strategy
                    target_tag = meta.get("targetTag")
            except Exception:
                pass
            logger.info(
                "[DOM] Reply Post button found via %s in %.1fs target=%s",
                strategy,
                time.monotonic() - started,
                target_tag,
            )
            return element, DomActionResult(
                True,
                strategy=strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            js_result = await page.evaluate("() => window.__smartReplyPostProbeState || null")
        except Exception:
            js_result = None
        last_reason = (js_result or {}).get("reason") if isinstance(js_result, dict) else "not_found"
        await asyncio.sleep(0.2)
    return None, DomActionResult(
        False,
        reason=last_reason or "reply_post_timeout",
        elapsed_seconds=time.monotonic() - started,
    )


async def find_post_button_smart(page: Page, *, max_total_seconds: float = 4.0) -> tuple[Optional[Any], DomActionResult]:
    started = time.monotonic()
    while time.monotonic() - started < max_total_seconds:
        try:
            js_result = await page.evaluate(_JS_FIND_POST_BUTTON)
        except Exception:
            await asyncio.sleep(0.3)
            continue
        if isinstance(js_result, dict) and js_result.get("found"):
            element = await page.query_selector('[data-smart-id="comment-post"]')
            if element:
                logger.info(
                    "[DOM] Post button found via %s in %.1fs target=%s rawPE=%s targetPE=%s",
                    js_result.get("strategy"),
                    time.monotonic() - started,
                    js_result.get("targetTag"),
                    js_result.get("rawPointerEvents"),
                    js_result.get("targetPointerEvents"),
                )
                return element, DomActionResult(True, strategy=js_result.get("strategy"), elapsed_seconds=time.monotonic() - started)
        await asyncio.sleep(0.3)
    try:
        final_check = await page.evaluate(_JS_FIND_POST_BUTTON)
        if isinstance(final_check, dict) and final_check.get("diagnostic"):
            logger.info("[DOM] Post button diagnostic: %s", json.dumps(final_check["diagnostic"], ensure_ascii=False)[:400])
    except Exception:
        pass
    return None, DomActionResult(False, reason="timeout", elapsed_seconds=time.monotonic() - started)


async def wait_for_post_button_enabled(
    page: Page,
    *,
    max_wait_seconds: float = 5.0,
    reply_scoped: bool = False,
    expected_text: Optional[str] = None,
) -> tuple[Optional[Any], DomActionResult]:
    """
    Wait for the Post button to become enabled.
    """
    started = time.monotonic()
    attempt = 0
    last_disabled_reason: Optional[str] = None
    input_not_ready_streak = 0

    while time.monotonic() - started < max_wait_seconds:
        try:
            result = await page.evaluate(r"""
                ([replyScoped, expectedText]) => {
                    document
                        .querySelectorAll('[data-smart-id="comment-post-ready"]')
                        .forEach(el => el.removeAttribute('data-smart-id'));

                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    };

                    const hardDisabled = (el) => {
                        if (!el) return true;
                        return el.hasAttribute('disabled') ||
                               el.getAttribute('aria-disabled') === 'true' ||
                               el.classList.contains('disabled');
                    };

                    const clickTargetFor = (raw) => {
                        if (!raw) return null;
                        const candidates = [
                            raw,
                            raw.closest('button'),
                            raw.closest('[role="button"]'),
                            raw.closest('div[tabindex="0"]'),
                            raw.parentElement,
                            raw.parentElement && raw.parentElement.parentElement,
                        ].filter(Boolean);
                        const seen = new Set();
                        for (const el of candidates) {
                            if (seen.has(el)) continue;
                            seen.add(el);
                            if (!visible(el)) continue;
                            const r = el.getBoundingClientRect();
                            if (r.width > 360 || r.height > 140) continue;
                            if (getComputedStyle(el).pointerEvents === 'none') continue;
                            if (hardDisabled(el)) continue;
                            return el;
                        }
                        return null;
                    };

                    const rawCandidates = (
                        replyScoped
                            ? [
                                document.querySelector('[data-smart-reply-post="1"]'),
                                document.querySelector('[data-smart-id="comment-post"]'),
                            ]
                            : [
                                document.querySelector('[data-smart-id="comment-post-ready"]'),
                                document.querySelector('[data-smart-id="comment-post"]'),
                                document.querySelector('[data-e2e="comment-post"]'),
                                document.querySelector('[data-e2e*="comment-post"]'),
                                document.querySelector('[data-e2e="post-comment-button"]'),
                            ]
                    ).filter(Boolean);

                    const placeholderMarkers = [
                        'add comment',
                        'add a comment',
                        'say something',
                        'leave a comment',
                        'добавьте комментар',
                        'напишите комментар',
                        'комментарий...'
                    ];
                    placeholderMarkers.push(
                        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
                        '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
                        '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
                        '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c'
                    );

                    const resolveInnerEditor = (wrapper) => {
                        if (!wrapper) return null;
                        if (
                            wrapper.isContentEditable ||
                            (wrapper.getAttribute && wrapper.getAttribute('contenteditable') === 'true') ||
                            wrapper.tagName === 'TEXTAREA' ||
                            wrapper.tagName === 'INPUT' ||
                            (wrapper.getAttribute && wrapper.getAttribute('role') === 'textbox')
                        ) {
                            return wrapper;
                        }
                        if (!wrapper.querySelector) return wrapper;
                        return wrapper.querySelector(
                            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                        ) || wrapper;
                    };
                    const readEditorText = (editor) => {
                        if (!editor) return '';
                        const raw = (editor.value !== undefined && editor.value !== null && editor.value !== '')
                            ? editor.value
                            : (editor.textContent || editor.innerText || '');
                        return raw.trim();
                    };
                    const normalizeSubmitText = (value) => {
                        let text = String(value || '');
                        try { text = text.normalize('NFC'); } catch (_) {}
                        return text
                            .replace(/[\u200b\u200c\ufeff]/g, '')
                            .replace(/\u00a0/g, ' ')
                            .replace(/\s+/g, ' ')
                            .trim();
                    };
                    const hasPlaceholderLeak = (txt) => {
                        if (!txt) return false;
                        const lower = txt.toLowerCase();
                        return placeholderMarkers.some(marker => lower.startsWith(marker) || lower.includes('add comment...'));
                    };

                    const inputWrapper = replyScoped
                        ? (
                            document.querySelector('[data-smart-reply-input="1"]') ||
                            document.querySelector('[data-smart-id="comment-input"]')
                        )
                        : (
                            document.querySelector('[data-smart-id="comment-input"]') ||
                            document.querySelector('[data-e2e="comment-input"]')
                        );
                    const inputEditor = resolveInnerEditor(inputWrapper);
                    const inputText = readEditorText(inputEditor);
                    const placeholderLeak = hasPlaceholderLeak(inputText);
                    const actualNormalized = normalizeSubmitText(inputText);
                    const expectedProvided = expectedText !== null && expectedText !== undefined;
                    const expectedNormalized = expectedProvided ? normalizeSubmitText(expectedText) : '';
                    const expectedMatches = !expectedProvided || actualNormalized === expectedNormalized;

                    for (const raw of rawCandidates) {
                        const el = clickTargetFor(raw);
                        if (!el) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) continue;

                        const disabled = el.hasAttribute('disabled') ||
                                         el.getAttribute('aria-disabled') === 'true' ||
                                         el.classList.contains('disabled') ||
                                         getComputedStyle(el).pointerEvents === 'none' ||
                                         parseFloat(getComputedStyle(el).opacity) < 0.5;

                        if (!disabled && !placeholderLeak && inputText.length > 0 && expectedMatches) {
                            el.setAttribute('data-smart-id', 'comment-post-ready');
                            return {
                                ready: true,
                                tag: el.tagName,
                                inputText: inputText.substring(0, 80),
                                rawTag: raw.tagName,
                                rawPointerEvents: getComputedStyle(raw).pointerEvents,
                                targetPointerEvents: getComputedStyle(el).pointerEvents,
                            };
                        } else if (!placeholderLeak && inputText.length > 0 && !expectedMatches) {
                            return {
                                ready: false,
                                reason: 'text_mismatch',
                                inputText: inputText.substring(0, 80),
                                expectedText: String(expectedText || '').substring(0, 80),
                                inputNormalized: actualNormalized.substring(0, 80),
                                expectedNormalized: expectedNormalized.substring(0, 80),
                            };
                        } else if (!disabled && (placeholderLeak || inputText.length === 0)) {
                            return {
                                ready: false,
                                reason: placeholderLeak ? 'placeholder_leak' : 'input_empty',
                                inputText: inputText.substring(0, 80),
                                placeholderLeak: placeholderLeak,
                            };
                        } else {
                            return {
                                ready: false,
                                reason: 'button_disabled',
                                inputText: inputText.substring(0, 80),
                                hasDisabledAttr: el.hasAttribute('disabled'),
                                ariaDisabled: el.getAttribute('aria-disabled'),
                                opacity: getComputedStyle(el).opacity,
                                pointerEvents: getComputedStyle(el).pointerEvents,
                                rawPointerEvents: getComputedStyle(raw).pointerEvents,
                                rawTag: raw.tagName,
                                targetTag: el.tagName,
                            };
                        }
                    }
                    return { ready: false, reason: 'button_not_found', inputText: inputText.substring(0, 80) };
                }
            """, [reply_scoped, expected_text])

            if isinstance(result, dict):
                if result.get("ready"):
                    el = await page.query_selector('[data-smart-id="comment-post-ready"]')
                    if el:
                        logger.info(
                            "[DOM] Post button enabled: inputText='%s' in %.1fs target=%s rawPE=%s targetPE=%s",
                            result.get("inputText", ""),
                            time.monotonic() - started,
                            result.get("tag"),
                            result.get("rawPointerEvents"),
                            result.get("targetPointerEvents"),
                        )
                        return el, DomActionResult(True, strategy="enabled-check", elapsed_seconds=time.monotonic() - started)
                elif result.get("reason") in {"input_empty", "placeholder_leak", "text_mismatch"}:
                    input_not_ready_streak += 1
                    last_disabled_reason = f"post_button_input_not_ready:{result.get('reason')}"
                    if attempt == 0:
                        logger.info(
                            "[DOM] Post button not ready because composer text is not the expected variant: reason=%s inputText='%s' expected='%s'",
                            result.get("reason"),
                            result.get("inputText", ""),
                            result.get("expectedText", ""),
                        )
                    if input_not_ready_streak >= 2:
                        return None, DomActionResult(
                            False,
                            reason=last_disabled_reason,
                            elapsed_seconds=time.monotonic() - started,
                        )
                    await asyncio.sleep(0.3)
                    attempt += 1
                    continue
                elif result.get("reason") == "button_disabled":
                    input_not_ready_streak = 0
                    pointer_events = result.get("pointerEvents")
                    opacity = result.get("opacity")
                    if pointer_events == "none":
                        last_disabled_reason = "post_button_stayed_disabled:pointer_events_none"
                    elif opacity is not None:
                        try:
                            if float(opacity) < 0.5:
                                last_disabled_reason = "post_button_stayed_disabled:low_opacity"
                            else:
                                last_disabled_reason = "post_button_stayed_disabled"
                        except Exception:
                            last_disabled_reason = "post_button_stayed_disabled"
                    else:
                        last_disabled_reason = "post_button_stayed_disabled"
                    if attempt == 0:
                        logger.info(
                            "[DOM] Post button disabled state: inputText='%s' disabled=%s ariaDisabled=%s opacity=%s pointerEvents=%s rawPointerEvents=%s target=%s",
                            result.get("inputText", ""),
                            result.get("hasDisabledAttr"),
                            result.get("ariaDisabled"),
                            result.get("opacity"),
                            result.get("pointerEvents"),
                            result.get("rawPointerEvents"),
                            result.get("targetTag"),
                        )
                    await asyncio.sleep(0.3)
                    attempt += 1
                    continue
        except Exception:
            pass

        await asyncio.sleep(0.3)
        attempt += 1

    return None, DomActionResult(False, reason=last_disabled_reason or "post_button_stayed_disabled", elapsed_seconds=time.monotonic() - started)


_JS_COMMENT_INPUT_TEXT_MATCH = r"""
(expected) => {
    const placeholderMarkers = [
        'add comment',
        'add a comment',
        'say something',
        'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439...'
    ];

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const isRealEditor = (el) => {
        if (!el) return false;
        if (el.isContentEditable) return true;
        if (el.getAttribute && el.getAttribute('contenteditable') === 'true') return true;
        if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return true;
        if (el.getAttribute && el.getAttribute('role') === 'textbox') return true;
        return false;
    };
    const labelOf = (el) => {
        const parts = [
            el && el.getAttribute && (el.getAttribute('data-smart-id') || ''),
            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
            el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
            el && (el.id || ''),
            el && String(el.className || ''),
            el && (el.tagName || ''),
        ];
        return parts.join(' ').toLowerCase();
    };
    const isCommentOrReplyScoped = (el) => {
        let cur = el;
        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
            const label = labelOf(cur);
            if (
                label.includes('search') ||
                label.includes('captcha') ||
                label.includes('challenge') ||
                label.includes('verification') ||
                label.includes('login') ||
                label.includes('password')
            ) return false;
            if (label.includes('comment') || label.includes('reply')) return true;
        }
        return false;
    };
    const addEditor = (set, raw) => {
        if (!raw) return;
        if (isRealEditor(raw)) {
            set.add(raw);
            return;
        }
        if (!raw.querySelector) return;
        const inner = raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
        );
        if (isRealEditor(inner)) set.add(inner);
    };

    const editors = new Set();
    const wrappers = [
        ...document.querySelectorAll('[data-smart-id="comment-input"]'),
        ...document.querySelectorAll('[data-smart-reply-input]'),
        ...document.querySelectorAll('[data-e2e="comment-input"]'),
        ...document.querySelectorAll('[data-e2e*="comment-input"]'),
        ...document.querySelectorAll('[data-e2e*="CommentInput"]'),
    ];
    for (const wrapper of wrappers) addEditor(editors, wrapper);
    for (const el of document.querySelectorAll(
        'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input'
    )) {
        if (isRealEditor(el) && isCommentOrReplyScoped(el)) editors.add(el);
    }

    for (const el of editors) {
        if (!visible(el) && !(el.parentElement && visible(el.parentElement))) continue;
        const raw = (el.value !== undefined && el.value !== null && el.value !== '')
            ? el.value
            : (el.textContent || el.innerText || '');
        const txt = raw.trim();
        if (!txt) continue;
        const txtLower = txt.toLowerCase();
        if (placeholderMarkers.some(marker => txtLower.startsWith(marker))) continue;
        const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        if (normalizeText(txt) === normalizeText(expected)) {
            return { ok: true, text: txt.slice(0, 160) };
        }
    }
    return { ok: false, text: '' };
}
"""


async def comment_input_contains_expected_text(page: Page, text: str) -> tuple[bool, str]:
    """Return whether the real comment/reply editor already contains text."""
    try:
        result = await page.evaluate(_JS_COMMENT_INPUT_TEXT_MATCH, text)
        if isinstance(result, dict):
            return bool(result.get("ok")), str(result.get("text") or "")
    except Exception:
        pass
    return False, ""


async def type_into_comment(
    page: Page,
    element: Any,
    text: str,
    *,
    deadline: Optional[float] = None,
    fast: bool = False,
    reply_scoped: bool = False,
    anchor_token: Optional[str] = None,
) -> bool:
    """
    Type text into TikTok's comment input.

    Final robust strategy:
    0) PRIMARY: one atomic page.evaluate that focuses + clears + inserts the
       text and verifies it landed (atomic_set_comment_text). ~1 round-trip,
       survives latency. For replies it is scoped to the reply editor only.
    1) Resolve the real editable node (data-e2e can be just a wrapper).
    2) Try human-like focus + keyboard typing.
    3) If focus fails, use a direct DOM/React event fallback on the real editor.
    4) Verify globally, not only on the original ElementHandle, because TikTok may re-render input.

    reply_scoped/anchor_token: when reply_scoped is True the atomic primitive
    selects the editor INSIDE the reply container only (never the global
    composer), preserving the "reply must not post as a main comment" guard.
    """

    def _deadline_exceeded() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _remaining_budget() -> Optional[float]:
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    if _deadline_exceeded():
        return False

    await suppress_keyboard_shortcuts_overlay(page)

    async def _resolve_editor_handle():
        """Return the best element to click/type into for the comment input.

        Priority: comment-specific selectors first, generic last.
        When inner contenteditable has h=0 (unfocused), returns the WRAPPER
        so that click lands on the visible area and the browser focuses the inner.
        """
        # Comment-specific selectors FIRST to avoid picking an unrelated input (e.g. search box)
        selectors = [
            '[data-smart-id="comment-input"]',
            '[data-e2e="comment-input"]',
            '[data-e2e*="comment-input"]',
            '[data-e2e*="CommentInput"]',
            '[role="textbox"]',
            'div[contenteditable="true"]',
            '[contenteditable="true"]',
            'textarea',
            'input',
        ]

        for sel in selectors:
            try:
                candidate = await page.query_selector(sel)
                if not candidate:
                    continue

                inner = None
                try:
                    inner = await candidate.query_selector(
                        'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                    )
                except Exception:
                    pass

                real = inner or candidate

                # `real` = inner if inner exists, else candidate. We check that
                # it's actually editable (real editor element, not wrapper).
                is_real_editor = await real.evaluate(
                    r"""
                    el => {
                        if (!el) return false;
                        return Boolean(
                            el.isContentEditable ||
                            (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
                            el.tagName === 'TEXTAREA' ||
                            el.tagName === 'INPUT' ||
                            (el.getAttribute && el.getAttribute('role') === 'textbox')
                        );
                    }
                    """
                )

                visible_now = await real.evaluate(
                    r"""
                    el => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 20 && r.height > 10 && st.display !== 'none' && st.visibility !== 'hidden';
                    }
                    """
                )

                if is_real_editor and visible_now:
                    try:
                        await real.evaluate("el => el.setAttribute('data-smart-id', 'comment-input')")
                    except Exception:
                        pass
                    return real

                # Inner is a real editor but currently has h=0 (TikTok unfocused
                # state). Click the wrapper to force focus, then return the inner
                # editor itself for typing — NEVER return the wrapper, because its
                # textContent reflects placeholder + editor state combined, and
                # writing into wrapper leaves placeholder visible.
                if is_real_editor and inner is real and inner is not candidate:
                    try:
                        await candidate.click(timeout=1500, force=True)
                        await asyncio.sleep(0.25)
                    except Exception:
                        pass
                    # Re-check inner visibility after focus.
                    try:
                        visible_after = await real.evaluate(
                            r"""
                            el => {
                                if (!el || !el.getBoundingClientRect) return false;
                                const r = el.getBoundingClientRect();
                                const st = getComputedStyle(el);
                                return r.width > 20 && r.height > 5 && st.display !== 'none' && st.visibility !== 'hidden';
                            }
                            """
                        )
                    except Exception:
                        visible_after = True
                    if visible_after:
                        try:
                            await real.evaluate("el => el.setAttribute('data-smart-id', 'comment-input')")
                        except Exception:
                            pass
                        logger.debug("[DOM] Inner editable activated via wrapper focus: %s", sel)
                        return real
                    # Inner still h=0 after focus — keep iterating to next selector.
                    # We deliberately do NOT return the wrapper here.
            except Exception:
                continue

        # Ultimate fallback: original handle passed to type_into_comment.
        return element

    async def _page_text_contains_expected() -> tuple[bool, str]:
        """Strict verification: confirm text reached the actual React editor.

        We only accept nodes that are real editable elements (contenteditable
        / textarea / input / role=textbox). Wrapper divs like
        [data-e2e="comment-input"] are excluded — their textContent contains
        placeholder-span concatenated with the inner editor's text, which gave
        false positives in previous versions (`Add comment...How much...`).

        Additionally, if the candidate text starts with a known placeholder
        marker, it's invalid — React didn't clear the placeholder, meaning the
        editor hasn't received an actual input event.
        """
        try:
            found = await page.evaluate(
                r"""
                (expected) => {
                    const placeholderMarkers = [
                        'add comment',
                        'add a comment',
                        'say something',
                        'leave a comment',
                        'добавьте комментар',
                        'Напишите комментар',
                        'комментарий...'
                    ];

                    const isRealEditor = (el) => {
                        if (!el) return false;
                        if (el.isContentEditable) return true;
                        if (el.getAttribute && el.getAttribute('contenteditable') === 'true') return true;
                        if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return true;
                        if (el.getAttribute && el.getAttribute('role') === 'textbox') return true;
                        return false;
                    };
                    const labelOf = (el) => {
                        const parts = [
                            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
                            el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                            el && (el.id || ''),
                            el && String(el.className || ''),
                            el && (el.tagName || ''),
                        ];
                        return parts.join(' ').toLowerCase();
                    };
                    const isCommentScoped = (el) => {
                        let cur = el;
                        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                            const label = labelOf(cur);
                            if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return false;
                            if (label.includes('comment') || label.includes('reply')) return true;
                        }
                        return false;
                    };

                    // Gather candidate nodes. Wrapper-style [data-e2e="comment-input"]
                    // is included only to descend into its inner editor, never used
                    // as the verification target directly.
                    const wrappers = [
                        ...document.querySelectorAll('[data-smart-id="comment-input"]'),
                        ...document.querySelectorAll('[data-e2e="comment-input"]'),
                        ...document.querySelectorAll('[data-e2e*="comment-input"]'),
                        ...document.querySelectorAll('[data-e2e*="CommentInput"]'),
                    ];
                    const editors = new Set();
                    for (const w of wrappers) {
                        if (isRealEditor(w)) {
                            editors.add(w);
                        } else if (w && w.querySelector) {
                            const inner = w.querySelector(
                                'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                            );
                            if (isRealEditor(inner)) editors.add(inner);
                        }
                    }
                    // Add free-standing editors too.
                    for (const el of document.querySelectorAll(
                        'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input'
                    )) {
                        if (isRealEditor(el) && isCommentScoped(el)) editors.add(el);
                    }

                    for (const el of editors) {
                        const raw = (el.value !== undefined && el.value !== null && el.value !== '')
                            ? el.value
                            : (el.textContent || el.innerText || '');
                        const txt = raw.trim();
                        if (!txt) continue;
                        const txtLower = txt.toLowerCase();
                        const startsWithPlaceholder = placeholderMarkers.some(
                            m => txtLower.startsWith(m)
                        );
                        if (startsWithPlaceholder) {
                            // React still rendering placeholder — input not committed.
                            // Skip this node.
                            continue;
                        }
                        const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                        if (normalizeText(txt) === normalizeText(expected)) {
                            return { ok: true, text: txt.slice(0, 120) };
                        }
                    }
                    return { ok: false, text: '' };
                }
                """,
                text,
            )
            if isinstance(found, dict):
                return bool(found.get("ok")), str(found.get("text") or "")
        except Exception:
            pass
        return False, ""

    async def _focus_ok() -> bool:
        try:
            result = await page.evaluate(
                r"""
                () => {
                    const active = document.activeElement;
                    if (!active) return false;
                    const scopedEditor = () => {
                        const wrapper = document.querySelector(
                            '[data-smart-id="comment-input"], [data-e2e="comment-input"], [data-e2e*="comment-input"], [data-e2e*="CommentInput"]'
                        );
                        if (wrapper) {
                            return wrapper.querySelector(
                                'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                            ) || wrapper;
                        }
                        const labelOf = (el) => [
                            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
                            el && String(el.className || ''),
                        ].join(' ').toLowerCase();
                        const isCommentScoped = (el) => {
                            let cur = el;
                            for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                const label = labelOf(cur);
                                if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return false;
                                if (label.includes('comment') || label.includes('reply')) return true;
                            }
                            return false;
                        };
                        return [...document.querySelectorAll('div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]')]
                            .find(isCommentScoped) || null;
                    };
                    const editor = scopedEditor();

                    return Boolean(
                        editor && (editor === active || editor.contains(active))
                    );
                }
                """
            )
            return bool(result)
        except Exception:
            return False

    async def _js_insert_text(reason: str) -> bool:
        """Insert text using DOM + React-like events. This is still on-page UI, not API spoofing."""
        try:
            result = await page.evaluate(
                r"""
                (expected) => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 20 && r.height > 10 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const labelOf = (el) => {
                        const parts = [
                            el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                            el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                            el && el.getAttribute && (el.getAttribute('placeholder') || ''),
                            el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                            el && (el.id || ''),
                            el && String(el.className || ''),
                            el && (el.tagName || ''),
                        ];
                        return parts.join(' ').toLowerCase();
                    };
                    const isRealEditor = (el) => el && (
                        el.isContentEditable ||
                        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
                        el.tagName === 'TEXTAREA' ||
                        el.tagName === 'INPUT' ||
                        (el.getAttribute && el.getAttribute('role') === 'textbox')
                    );
                    const isCommentScoped = (el) => {
                        let cur = el;
                        for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                            const label = labelOf(cur);
                            if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return false;
                            if (label.includes('comment') || label.includes('reply')) return true;
                        }
                        return false;
                    };

                    const pickEditor = () => {
                        const commentSelectors = [
                            '[data-smart-id="comment-input"]',
                            '[data-e2e="comment-input"]',
                            '[data-e2e*="comment-input"]',
                            '[data-e2e*="CommentInput"]'
                        ];

                        for (const sel of commentSelectors) {
                            const raw = document.querySelector(sel);
                            if (!raw || !visible(raw)) continue;

                            const inner = raw.querySelector && raw.querySelector(
                                'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                            );
                            const el = inner || raw;
                            if (!isRealEditor(el)) continue;
                            return el;
                        }
                        const broadSelectors = [
                            'div[contenteditable="true"]',
                            '[contenteditable="true"]',
                            'textarea',
                            'input',
                            '[role="textbox"]'
                        ];
                        for (const sel of broadSelectors) {
                            for (const el of document.querySelectorAll(sel)) {
                                if (!visible(el) || !isRealEditor(el) || !isCommentScoped(el)) continue;
                                return el;
                            }
                        }
                        return null;
                    };

                    const fireInputEvents = (el, data) => {
                        const evs = [
                            new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data}),
                            new InputEvent('input', {bubbles:true, cancelable:true, inputType:'insertText', data}),
                            new Event('change', {bubbles:true, cancelable:true}),
                            new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:data.slice(-1) || 'a'}),
                            new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:data.slice(-1) || 'a'})
                        ];
                        for (const ev of evs) {
                            try { el.dispatchEvent(ev); } catch (_) {}
                        }
                    };

                    const setNativeValue = (el, val) => {
                        const proto =
                            el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype :
                            el.tagName === 'INPUT' ? window.HTMLInputElement.prototype :
                            null;
                        if (!proto) return false;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        if (desc && desc.set) desc.set.call(el, val);
                        else el.value = val;
                        return true;
                    };

                    const el = pickEditor();
                    if (!el) {
                        return { ok:false, reason:'no_editor', diag:null };
                    }

                    try { el.scrollIntoView({block:'center', inline:'center'}); } catch (_) {}
                    try { el.click(); } catch (_) {}
                    try { el.focus(); } catch (_) {}

                    // Resolve the REAL inner editor before writing. `el` returned
                    // by pickEditor might be the wrapper [data-e2e="comment-input"]
                    // whose textContent includes the placeholder-span. Writing into
                    // wrapper leaves the placeholder visible and React never sees the
                    // change. Verification at the end runs against `real`, not `el`.
                    const real = (el.isContentEditable || (el.getAttribute && el.getAttribute('contenteditable') === 'true') || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')
                        ? el
                        : (el.querySelector && el.querySelector('div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input')) || el;

                    const isNativeEditor = real.tagName === 'TEXTAREA' || real.tagName === 'INPUT';
                    if (isNativeEditor) {
                        setNativeValue(real, expected);
                        fireInputEvents(real, expected);
                    } else {
                        return {
                            ok: false,
                            reason: 'contenteditable_js_fallback_disabled',
                            text: (real.textContent || real.innerText || '').trim().slice(0, 120),
                            placeholder_leak: false,
                            diag: {
                                tag: real.tagName,
                                e2e: real.getAttribute && real.getAttribute('data-e2e'),
                                smart: real.getAttribute && real.getAttribute('data-smart-id'),
                                role: real.getAttribute && real.getAttribute('role'),
                                contenteditable: real.getAttribute && real.getAttribute('contenteditable'),
                                isContentEditable: Boolean(real.isContentEditable),
                                html: (real.outerHTML || '').slice(0, 400)
                            }
                        };
                    }

                    // STRICT verification against the actual editor, NOT the wrapper.
                    // Previous version used `el` (potentially wrapper) and an
                    // `allTxt.includes(expected)` body fallback — both produced false
                    // positives when text was anywhere on the page.
                    const placeholderMarkers = [
                        'add comment',
                        'add a comment',
                        'say something',
                        'leave a comment',
                        'добавьте комментар',
                        'Напишите комментар',
                        'комментарий...'
                    ];
                    const rawText = (real.value !== undefined && real.value !== null && real.value !== '')
                        ? real.value
                        : (real.textContent || real.innerText || '');
                    const txt = rawText.trim();
                    const txtLower = txt.toLowerCase();
                    const startsWithPlaceholder = placeholderMarkers.some(m => txtLower.startsWith(m));
                    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                    const ok = Boolean(
                        txt &&
                        !startsWithPlaceholder &&
                        normalizeText(txt) === normalizeText(expected)
                    );

                    return {
                        ok: ok,
                        reason: 'inserted',
                        text: txt.slice(0, 120),
                        placeholder_leak: startsWithPlaceholder,
                        diag: {
                            tag: real.tagName,
                            e2e: real.getAttribute && real.getAttribute('data-e2e'),
                            smart: real.getAttribute && real.getAttribute('data-smart-id'),
                            role: real.getAttribute && real.getAttribute('role'),
                            contenteditable: real.getAttribute && real.getAttribute('contenteditable'),
                            isContentEditable: Boolean(real.isContentEditable),
                            html: (real.outerHTML || '').slice(0, 400)
                        }
                    };
                }
                """,
                text,
            )

            if isinstance(result, dict):
                if result.get("ok"):
                    logger.info(
                        "[DOM] Text inserted by JS fallback (%s): '%s'",
                        reason,
                        (result.get("text") or "")[:30],
                    )
                    return True
                if result.get("placeholder_leak"):
                    # We wrote into the wrapper instead of the inner editor;
                    # placeholder remained in the read-back text. React state
                    # was not updated → Post will stay disabled.
                    logger.warning(
                        "[DOM] JS fallback wrote into wrapper (placeholder leak detected): %s",
                        result.get("text", "")[:60],
                    )
                else:
                    logger.warning("[DOM] JS fallback failed (%s): %s", reason, result)
            else:
                logger.warning("[DOM] JS fallback returned non-dict (%s): %s", reason, result)
        except Exception as exc:
            logger.warning("[DOM] JS fallback exception (%s): %s", reason, str(exc)[:160])

        return False

    def _normalize_local(value: str) -> str:
        return " ".join(str(value or "").split()).strip()

    async def _atomic_prefix_then_trusted_tail() -> bool:
        """Fast path: DOM-set the prefix, then use one trusted keyboard insert.

        A full keyboard.insert_text can stall badly when several Chromium
        windows are busy. Setting all but the last character in-page keeps the
        operation cheap, while the final trusted insert wakes TikTok/React state.
        """
        if not text:
            return False

        prefix = text[:-1]
        tail = text[-1:]
        result = await atomic_set_comment_text(
            page,
            text,
            reply_scoped=reply_scoped,
            anchor_token=anchor_token,
            insert_text=prefix,
        )
        if not isinstance(result, dict):
            return False

        reason = str(result.get("reason") or "")
        if reason in {"no_editor", "reply_marked_input_missing", "reply_input_not_scoped_to_parent"}:
            logger.debug("[DOM] Atomic prefix primary failed (%s); falling back", reason)
            return False

        landed = str(result.get("text") or "")
        if prefix and len(prefix) <= 120 and _normalize_local(landed) != _normalize_local(prefix):
            logger.debug(
                "[DOM] Atomic prefix primary did not land prefix cleanly (%s); falling back",
                reason,
            )
            return False

        try:
            await suppress_keyboard_shortcuts_overlay(page)
            if not await _focus_ok():
                focus_result = await atomic_set_comment_text(
                    page,
                    text,
                    reply_scoped=reply_scoped,
                    anchor_token=anchor_token,
                    focus_only=True,
                )
                if not isinstance(focus_result, dict) or not focus_result.get("ok"):
                    logger.debug(
                        "[DOM] Atomic prefix primary could not refocus editor (%s); falling back",
                        focus_result.get("reason") if isinstance(focus_result, dict) else "no_result",
                    )
                    return False
                if not await _focus_ok():
                    logger.debug("[DOM] Atomic prefix primary focus check failed; falling back")
                    return False
            await page.keyboard.insert_text(tail)
            await asyncio.sleep(0.10 if fast else 0.18)
        except Exception as exc:
            logger.debug("[DOM] Atomic prefix primary trusted tail failed (%s); falling back", str(exc)[:120])
            return False

        ok, found_text = await _page_text_contains_expected()
        if ok:
            logger.info(
                "[DOM] Text set via atomic-prefix+trusted-tail primary: '%s'",
                found_text[:30],
            )
            return True

        logger.debug("[DOM] Atomic prefix primary failed global re-verify; falling back")
        return False

    async def _atomic_primary() -> bool:
        """Fallback path: atomic focus + trusted keyboard.insert_text."""
        if not text:
            return False

        focus_result = await atomic_set_comment_text(
            page,
            text,
            reply_scoped=reply_scoped,
            anchor_token=anchor_token,
            focus_only=True,
        )
        if not isinstance(focus_result, dict) or not focus_result.get("ok"):
            reason = focus_result.get("reason") if isinstance(focus_result, dict) else "no_result"
            logger.debug("[DOM] Atomic focus primary failed (%s); falling back", reason)
            return False

        try:
            await suppress_keyboard_shortcuts_overlay(page)
            if not await _focus_ok():
                logger.debug("[DOM] Atomic focus primary did not leave focus in comment input; falling back")
                return False
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(text)
            await asyncio.sleep(0.12 if fast else 0.22)
        except Exception as exc:
            logger.debug("[DOM] Trusted insert_text primary failed (%s); falling back", str(exc)[:120])
            return False

        ok, found_text = await _page_text_contains_expected()
        if ok:
            logger.info(
                "[DOM] Text set via focus+insert_text primary (%s): '%s'",
                focus_result.get("scoped"),
                found_text[:30],
            )
            return True

        logger.debug("[DOM] Focus+insert_text primary failed global re-verify; falling back")
        return False

    if reply_scoped and not _deadline_exceeded():
        if await _atomic_prefix_then_trusted_tail():
            return True

    if not _deadline_exceeded():
        if await _atomic_primary():
            return True

    editor = await _resolve_editor_handle()

    # Placeholder before typing. Used only for sanity check, not as hard gate.
    placeholder_text = ""
    try:
        placeholder_text = await editor.evaluate("el => (el.textContent || el.innerText || el.value || '').trim()")
    except Exception:
        pass

    # Try normal focus and keyboard path.
    focused = False
    for attempt in range(6):
        if _deadline_exceeded():
            return False
        await suppress_keyboard_shortcuts_overlay(page)
        editor = await _resolve_editor_handle()

        try:
            if attempt == 0:
                await editor.click(timeout=1800, force=True)
            elif attempt == 1:
                box = await editor.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    await editor.click(timeout=1800, force=True)
            elif attempt == 2:
                await editor.evaluate(
                    r"""
                    el => {
                        const inner = (el.isContentEditable || el.getAttribute('contenteditable') === 'true' || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')
                            ? el
                            : (el.querySelector && el.querySelector('div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]')) || el;

                        inner.scrollIntoView({block:'center', inline:'center'});
                        inner.click();
                        inner.focus();

                        if (inner.isContentEditable || inner.getAttribute('contenteditable') === 'true') {
                            const range = document.createRange();
                            range.selectNodeContents(inner);
                            range.collapse(false);
                            const sel = window.getSelection();
                            sel.removeAllRanges();
                            sel.addRange(range);
                        }
                    }
                    """
                )
            else:
                await editor.evaluate("el => { try { el.click(); el.focus(); } catch (_) {} }")

            await asyncio.sleep(0.22)
            if await _focus_ok():
                focused = True
                logger.debug("[DOM] Focus confirmed in comment input (attempt %d)", attempt + 1)
                break
        except Exception as exc:
            logger.debug("[DOM] Focus attempt %d failed: %s", attempt + 1, str(exc)[:100])

    if focused:
        try:
            await suppress_keyboard_shortcuts_overlay(page)
            if not await _focus_ok():
                logger.warning("[DOM] Focus was lost before keyboard typing; skipping blind keyboard input")
                focused = False
            else:
                await page.keyboard.press("Control+a")
                await asyncio.sleep(0.05)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.08)
                # Эмодзи (U+10000+, surrogate pairs) ломаются через keyboard.type
                # потому что Playwright шлёт по одному KeyEvent на codepoint, а
                # contenteditable получает половину пары. Используем insert_text
                # (CDP Input.insertText) — он отдаёт строку целиком, включая
                # multi-byte эмодзи. Для plain-ASCII оставляем human-like type
                # чтобы антифрод не видел мгновенную вставку.
                has_non_bmp = any(ord(ch) > 0xFFFF for ch in text)
                remaining_budget = _remaining_budget()
                estimated_human_type_seconds = max(0.6, len(text or "") * 0.03 + 0.2)
                use_fast_insert = fast or len(text or "") >= 80
                if use_fast_insert or has_non_bmp or (
                    remaining_budget is not None
                    and remaining_budget < estimated_human_type_seconds
                ):
                    await page.keyboard.insert_text(text)
                else:
                    await page.keyboard.type(text, delay=30)
                await asyncio.sleep(0.45)

                ok, found_text = await _page_text_contains_expected()
                if ok:
                    logger.info("[DOM] Text typed and verified: '%s' (%d chars)", found_text[:30], len(found_text))
                    return True
        except Exception as exc:
            logger.warning("[DOM] keyboard typing path failed: %s", str(exc)[:120])

    if focused:
        logger.warning(
            "[DOM] Keyboard typing did not verify. placeholder='%s'. Trying JS fallback.",
            placeholder_text[:30],
        )
    else:
        logger.warning("[DOM] Could not focus comment input normally; trying JS input fallback")

    if _deadline_exceeded():
        return False

    if await _js_insert_text("focus_failed_or_keyboard_unverified"):
        return True

    # One last check: maybe text is already there but handle was stale.
    ok, found_text = await _page_text_contains_expected()
    if ok:
        logger.info("[DOM] Text verified after final global check: '%s'", found_text[:30])
        return True

    logger.warning("[DOM] Could not type/insert comment text after all attempts")
    return False
