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
            if (r.top < window.innerHeight * 0.25) continue;
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
        const target = clickTargetFor(dataE2e);
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
        '[data-cid]',
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
    const commentInputVisible = inputNodes.filter(commentInputLike).length;
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
    let selectedScrollNode = null;
    const scrollInfo = (() => {
        if (!chosenList || !chosenList.el) return null;
        const isScrollable = (node) => !!node && node.scrollHeight > node.clientHeight + 20;
        let scrollNode = chosenList.el;
        try {
            const descendants = Array.from(chosenList.el.querySelectorAll('*')).filter(visible);
            scrollNode = descendants.find(isScrollable) || chosenList.el;
        } catch (_) {}
        selectedScrollNode = scrollNode;
        return {
            scrollable: isScrollable(scrollNode),
            scroll_height: Number(scrollNode && scrollNode.scrollHeight) || 0,
            client_height: Number(scrollNode && scrollNode.clientHeight) || 0,
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
([parentCommentId, parentText]) => {
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
    const rootMatchesNeedle = (el) => {
        if (!normalizedNeedle) return true;
        return normalize(el && (el.textContent || el.innerText || '')).includes(normalizedNeedle);
    };

    const clickReplyInside = (root, strategy) => {
        if (!root) return null;
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
            const r = button.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            try {
                button.click();
                return { clicked: true, strategy };
            } catch (e) {
                return { clicked: false, reason: String(e) };
            }
        }
        return null;
    };

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
            '[data-cid]',
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
    ];

    const hasMarker = (txt) => {
        txt = String(txt || '');
        if (txt.includes('Introducing keyboard shortcuts')) return true;
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
                } else {
                    // Still make it click-through even if not bounded.
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
                    else {
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
    """Install and run a persistent killer for TikTok 'Introducing keyboard shortcuts' popup.

    This popup is a real blocker in headless/headful runs: it can cover the Post
    button center, so elementFromPoint returns the popup instead of comment-post.
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
        return bool(
            state.get("list_visible")
            and int(state.get("comment_items_visible") or 0) > 0
        )
    if require_input:
        return bool(state.get("input_visible"))
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
        try:
            locator = page.locator(selector)
            count = await locator.count()
            diag["selector_counts"][selector] = count
            if count <= 0:
                continue
            for index in range(min(count, 8)):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible(timeout=500):
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
                    await candidate.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                await candidate.click(timeout=1500, force=True)
                diag["playwright_clicks"] += 1
                await asyncio.sleep(1.0)
                state = await _comment_surface_state(page)
                _remember_state(state)
                if _comment_surface_ready(
                    state,
                    require_input=require_input,
                    require_list_with_items=require_list_with_items,
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
            await asyncio.sleep(1.0)
            state = await _comment_surface_state(page)
            _remember_state(state)
            if _comment_surface_ready(
                state,
                require_input=require_input,
                require_list_with_items=require_list_with_items,
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

    def _is_ready(state: dict[str, Any]) -> bool:
        return _comment_surface_ready(
            state,
            require_input=require_input,
            require_list_with_items=require_list_with_items,
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
                "list": bool(state.get("list_visible")),
                "items": int(state.get("comment_items_visible") or 0),
                "strong_items": int(list_source.get("strong_items") or 0),
                "scrollable": bool(list_source.get("scrollable")),
                "list_source": state.get("list_source"),
            }
        payload.update({k: v for k, v in fields.items() if v is not None})
        logger.log(level, "[COMMENT_SURFACE_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:1800])

    async def _wait_for_strict_items(
        state: dict[str, Any],
        *,
        attempt: int,
        result: Optional[dict[str, Any]],
        result_status: str,
    ) -> bool:
        if not require_list_with_items:
            return False
        if not state.get("list_visible") or int(state.get("comment_items_visible") or 0) > 0:
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
                        '[data-cid]',
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
                        '[data-cid]',
                        '[data-comment-id]',
                        'p',
                        'li',
                        '[role="listitem"]',
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

    max_open_attempts = 5 if require_list_with_items else 3
    for _attempt in range(max_open_attempts):
        if _time.monotonic() - started > max_wait:
            break

        try:
            result = await page.evaluate(_JS_OPEN_COMMENTS)
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
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="already_open")
                return True
            if result.get("list_visible") and not require_input:
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="list_visible")
                return True

        if result.get("input_pending") and require_input:
            try:
                remaining_ms = max_wait - (_time.monotonic() - started)
                await page.wait_for_selector(
                    (
                        '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                        'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                    ),
                    timeout=max(500, min(int(remaining_ms * 1000), 2500)),
                    state="visible",
                )
                _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="input_pending_visible")
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
                remaining_ms = max_wait - (_time.monotonic() - started)
                if require_list_with_items:
                    # In strict mode we wait for an actual comment item to be
                    # rendered, not just the drawer container.
                    selector = (
                        '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                        '[class*="DivCommentObject"], [data-cid], [data-comment-id]'
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
                    timeout=max(500, min(int(remaining_ms * 1000), 4000)),
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
                else:
                    _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="wait_selector")
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
            if require_input and _comment_surface_ready(state):
                await asyncio.sleep(0.6)
                continue
            # The plain fallback opener doesn't know about strict mode — we
            # still call it to nudge the UI, then re-verify state strictly.
            fallback_ready = await _click_comment_trigger_fallback(
                page,
                require_input=require_input,
                require_list_with_items=require_list_with_items,
            )
            if fallback_ready:
                try:
                    if require_list_with_items:
                        selector = (
                            '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                            '[class*="DivCommentObject"], [data-cid], [data-comment-id]'
                        )
                    else:
                        selector = (
                            '[data-e2e="comment-input"], [data-smart-id="comment-input"], '
                            'div[contenteditable="true"], [contenteditable="true"], textarea, [role="textbox"]'
                        )
                    await page.wait_for_selector(
                        selector,
                        timeout=2500,
                        state="visible",
                    )
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
                    else:
                        _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="fallback_selector")
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
                await asyncio.sleep(0.5)
                continue
            try:
                if require_list_with_items:
                    selector = (
                        '[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-e2e*="comment-username"], '
                        '[class*="DivCommentObject"], [data-cid], [data-comment-id]'
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
                await page.wait_for_selector(selector, timeout=2500, state="visible")
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
                else:
                    _log_surface_trace("ready", attempt=_attempt + 1, result=result, result_status="fallback_no_click_selector")
                    return True
            except Exception:
                pass

        await asyncio.sleep(0.5)

    try:
        state = await _comment_surface_state(page)
    except Exception:
        state = None
    _log_surface_trace("failed", state=state, level=logging.WARNING, elapsed=round(_time.monotonic() - started, 2))
    return False


async def open_reply_box(
    page: Page,
    parent_comment_text: Optional[str] = None,
    parent_comment_id: Optional[str] = None,
) -> DomActionResult:
    started = time.monotonic()
    has_anchor = bool(parent_comment_id or parent_comment_text)
    # When anchor is provided, retry with scroll to handle freshly posted comments.
    max_attempts = 4 if has_anchor else 1

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
            await asyncio.sleep(0.8)

        try:
            result = await page.evaluate(_JS_OPEN_REPLY, [parent_comment_id or "", parent_comment_text or ""])
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
                    timeout=3000,
                )
            except Exception:
                # Fallback: give TikTok at least 0.7s even if selector never appears
                await asyncio.sleep(0.7)
            try:
                activation = await page.evaluate(_JS_REPLY_INPUT_ACTIVE)
            except Exception as exc:
                return DomActionResult(
                    False,
                    reason=f"reply_input_activation_check_failed:{str(exc)[:80]}",
                    elapsed_seconds=time.monotonic() - started,
                )
            if not (isinstance(activation, dict) and activation.get("active")):
                return DomActionResult(
                    False,
                    reason="reply_input_not_activated",
                    elapsed_seconds=time.monotonic() - started,
                )
            return DomActionResult(
                True,
                strategy=result.get("strategy"),
                elapsed_seconds=time.monotonic() - started,
            )

        return DomActionResult(
            False,
            reason=(result or {}).get("reason") if isinstance(result, dict) else "reply_button_not_found",
            elapsed_seconds=time.monotonic() - started,
        )

    return DomActionResult(False, reason="reply_open_max_attempts", elapsed_seconds=time.monotonic() - started)


async def ensure_click_target_clear(page: Page, element: Any) -> None:
    """
    Surgical blocker cleanup for a specific clickable/editable element.

    Unlike dismiss_overlays(), this does not sweep the whole page. It checks the
    center point of the target element and only makes the exact blocking element
    click-through if it is clearly not the comment UI itself.
    """
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


async def find_comment_input_smart(page: Page, *, max_total_seconds: float = 15.0) -> tuple[Optional[Any], DomActionResult]:
    started = time.monotonic()
    attempt = 0
    await open_comment_section(page, max_wait=5.0, require_input=True)
    await dismiss_cookie_banners(page)

    try:
        diag = await page.evaluate(_JS_DIAGNOSE_PAGE)
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
            js_result = await page.evaluate(_JS_FIND_COMMENT_INPUT)
        except Exception as exc:
            elapsed = time.monotonic() - started
            err = str(exc).lower()
            if "target" in err or "closed" in err:
                return None, DomActionResult(False, "page_closed", elapsed)
            await asyncio.sleep(0.3)
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
                                    let cur = el;
                                    for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                        const label = labelOf(cur);
                                        if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return false;
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
                                'drag the slider', 'slide to verify', 'fit the puzzle'
                            ];
                            const challengeTextMarkers = [
                                'captcha', 'verification', 'security check', 'challenge',
                                'drag the slider', 'slide to verify', 'fit the puzzle'
                            ];
                            const insideBadSurface = (el) => {
                                let cur = el;
                                for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                    const label = labelOf(cur);
                                    if (badMarkers.some(marker => label.includes(marker))) return true;
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
                                '[data-e2e*="comment-level"], [data-e2e*="comment-username"], [data-cid], [data-comment-id]'
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

        if attempt > 1:
            await dismiss_cookie_banners(page)
        if attempt == 2:
            await open_comment_section(page, max_wait=5.0, require_input=True)
        try:
            await page.evaluate("window.scrollBy(0, 100)")
        except Exception:
            pass
        await asyncio.sleep(0.8)

    reason = "timeout"
    try:
        final_diag = await page.evaluate(_JS_DIAGNOSE_PAGE)
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
                            let cur = el;
                            for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                                const label = labelOf(cur);
                                if (label.includes('search') || label.includes('captcha') ||
                                    label.includes('verification') || label.includes('challenge')) {
                                    return true;
                                }
                                const text = (cur.innerText || cur.textContent || '').toLowerCase();
                                if (text.includes('drag the slider') || text.includes('slide to verify') ||
                                    text.includes('fit the puzzle') || text.includes('security verification')) {
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
) -> tuple[Optional[Any], DomActionResult]:
    """
    Wait for the Post button to become enabled.
    """
    started = time.monotonic()
    attempt = 0
    last_disabled_reason: Optional[str] = None

    while time.monotonic() - started < max_wait_seconds:
        try:
            result = await page.evaluate(r"""
                () => {
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
                            if (r.top < window.innerHeight * 0.25) continue;
                            if (getComputedStyle(el).pointerEvents === 'none') continue;
                            if (hardDisabled(el)) continue;
                            return el;
                        }
                        return null;
                    };

                    const rawCandidates = [
                        document.querySelector('[data-smart-id="comment-post-ready"]'),
                        document.querySelector('[data-smart-id="comment-post"]'),
                        document.querySelector('[data-e2e="comment-post"]'),
                        document.querySelector('[data-e2e*="comment-post"]'),
                        document.querySelector('[data-e2e="post-comment-button"]'),
                    ].filter(Boolean);

                    const input = document.querySelector('[data-e2e="comment-input"]') ||
                                  document.querySelector('[data-smart-id="comment-input"]');
                    const inputText = input ? (input.textContent || input.innerText || '').trim() : '';

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

                        if (!disabled) {
                            el.setAttribute('data-smart-id', 'comment-post-ready');
                            return {
                                ready: true,
                                tag: el.tagName,
                                inputText: inputText.substring(0, 30),
                                rawTag: raw.tagName,
                                rawPointerEvents: getComputedStyle(raw).pointerEvents,
                                targetPointerEvents: getComputedStyle(el).pointerEvents,
                            };
                        } else {
                            return {
                                ready: false,
                                reason: 'button_disabled',
                                inputText: inputText.substring(0, 30),
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
                    return { ready: false, reason: 'button_not_found', inputText: inputText.substring(0, 30) };
                }
            """)

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
                elif result.get("reason") == "button_disabled":
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


async def type_into_comment(page: Page, element: Any, text: str) -> bool:
    """
    Type text into TikTok's comment input.

    Final robust strategy:
    1) Resolve the real editable node (data-e2e can be just a wrapper).
    2) Try human-like focus + keyboard typing.
    3) If focus fails, use a direct DOM/React event fallback on the real editor.
    4) Verify globally, not only on the original ElementHandle, because TikTok may re-render input.
    """

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
                        if (txt === expected ||
                            txt.includes(expected) ||
                            expected.includes(txt)) {
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

                    if (real.tagName === 'TEXTAREA' || real.tagName === 'INPUT') {
                        setNativeValue(real, expected);
                        fireInputEvents(real, expected);
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

                        // Try execCommand first because many React editors listen to it better.
                        let execOk = false;
                        try { execOk = document.execCommand('insertText', false, expected); } catch (_) {}

                        const currentAfterExec = (real.textContent || real.innerText || '').trim();
                        if (!currentAfterExec || !currentAfterExec.includes(expected)) {
                            real.textContent = expected;
                        }

                        fireInputEvents(real, expected);
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
                    const ok = Boolean(
                        txt &&
                        !startsWithPlaceholder &&
                        (txt === expected || txt.includes(expected) || expected.includes(txt))
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
            elif attempt == 3:
                await page.keyboard.press("Tab")
            elif attempt == 4:
                await page.keyboard.press("Shift+Tab")
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
            if has_non_bmp:
                await page.keyboard.insert_text(text)
            else:
                await page.keyboard.type(text, delay=30)
            await asyncio.sleep(0.45)

            ok, found_text = await _page_text_contains_expected()
            if ok:
                logger.info("[DOM] Text typed and verified: '%s' (%d chars)", found_text[:30], len(found_text))
                return True

            # If keyboard went to wrong place, do not fail immediately.
            logger.warning(
                "[DOM] Keyboard typing did not verify. placeholder='%s'. Trying JS fallback.",
                placeholder_text[:30],
            )
        except Exception as exc:
            logger.warning("[DOM] keyboard typing path failed: %s", str(exc)[:120])

    else:
        logger.warning("[DOM] Could not focus comment input normally; trying JS input fallback")

    if await _js_insert_text("focus_failed_or_keyboard_unverified"):
        return True

    # One last check: maybe text is already there but handle was stale.
    ok, found_text = await _page_text_contains_expected()
    if ok:
        logger.info("[DOM] Text verified after final global check: '%s'", found_text[:30])
        return True

    logger.warning("[DOM] Could not type/insert comment text after all attempts")
    return False
