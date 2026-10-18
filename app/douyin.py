from __future__ import annotations

import asyncio
import logging
import json
import re
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

from app.selectors import CHAT_PANEL_MARKERS, MESSAGE_INPUTS, SEARCH_INPUTS


class PageOperationError(RuntimeError):
    pass


RETRY_DELAY_MS = 3_000
LOGGER = logging.getLogger("douyin_sender")


class DouyinChat:
    def __init__(
        self,
        page: Page,
        timeout_ms: int = 15_000,
        confirm_timeout_ms: int = 15_000,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.confirm_timeout_ms = confirm_timeout_ms

    async def open_target(self, name: str, retries: int = 1) -> None:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                await self._open_target_once(name)
                return
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    await self.page.wait_for_timeout(RETRY_DELAY_MS)
        if last_error is not None:
            raise last_error
        raise PageOperationError("打开聊天失败")

    async def _open_target_once(self, name: str) -> None:
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.click(timeout=self.timeout_ms)
        await search.fill("")
        await search.fill(name)

        # Search results mount asynchronously. While search is active, only the
        # matching result's explicit send-message action may open a recipient.
        # Cached conversation rows can remain visible without being actionable.
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while True:
            result = await self._search_result(name)
            if result is not None:
                LOGGER.info("已匹配目标搜索结果，点击对应的发消息按钮")
                # Normal click waits for actionability; force=True can bypass an
                # overlay or click a stale control without opening the chat.
                diagnostic = _OpenDiagnostics(self.page)
                diagnostic.start()
                try:
                    await diagnostic.arm(result)
                    await diagnostic.snapshot("before_click")
                    await result.click(timeout=self.timeout_ms)
                    await diagnostic.snapshot("after_click")
                    await self._confirm_opened(name)
                    return
                except Exception:
                    await diagnostic.snapshot("open_failed")
                    raise
                finally:
                    diagnostic.report_network()
                    await diagnostic.stop()
            if asyncio.get_running_loop().time() >= deadline:
                rows = self.page.locator(_SEARCH_RESULT_ROWS)
                visible_rows = 0
                for index in range(await rows.count()):
                    if await rows.nth(index).is_visible():
                        visible_rows += 1
                LOGGER.warning("可见搜索结果行数: %d", visible_rows)
                raise PageOperationError(
                    "未识别到目标好友对应的可见发消息按钮；请查看本次失败截图，核对搜索结果和名称"
                )
            await self.page.wait_for_timeout(300)

    async def _search_result(self, name: str) -> Locator | None:
        rows = self.page.locator(_SEARCH_RESULT_ROWS)
        visible_rows = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            if await row.is_visible():
                visible_rows.append(row)

        # Resolve exact names across every row before considering a group count
        # suffix. Never use a substring or the text of the whole result list.
        for matcher in (_visible_exact_text_locator, _visible_group_text_locator):
            matches = []
            matched_names = 0
            for row in visible_rows:
                title = await matcher(row, _SEARCH_RESULT_NAMES, name)
                if title is None:
                    continue
                matched_names += 1
                button = await _send_action_in(row)
                if button is not None:
                    matches.append(button)
            if matched_names > 1:
                raise PageOperationError("搜索结果中存在多个同名目标，请设置唯一备注后再配置")
            if matches:
                return matches[0]
            if matched_names:
                # An exact contact is present but its action has not mounted.
                # Wait for it instead of selecting a similarly named group.
                return None
        return None

    async def message_input(self) -> Locator:
        return await first_visible(self.page, MESSAGE_INPUTS, self.timeout_ms, editable=True)

    async def _confirm_opened(self, name: str, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms if timeout_ms is not None else self.confirm_timeout_ms
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            last_error = await self._chat_open_error(name)
            if last_error is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise last_error
            await self.page.wait_for_timeout(500)

    async def _chat_open_error(self, name: str) -> PageOperationError | None:
        # Confirm the right-side current chat by the authoritative chat title, which
        # must itself be visible. A visible header retaining a hidden stale name node
        # (common during SPA transitions) must not confirm the wrong recipient, and
        # a secondary username/title field must never substitute for the chat title.
        title_selectors = (
            '[class*="RightPanelHeadertitle"]',
            '[class*="RightPanelHeaderTitle"]',
            '[class*="RightPanelHeader_title"]',
            '[class*="RightPanelHeader-title"]',
            '[class*="chatHeadertitle"]',
            '[class*="ChatHeaderTitle"]',
            '[class*="chatHeader_title"]',
            '[class*="ChatHeader-title"]',
            '[class*="name"]',
            '[class*="Name"]',
            '[class*="nickname"]',
            '[class*="Nickname"]',
        )
        for selector in CHAT_PANEL_MARKERS[:3]:
            headers = self.page.locator(selector)
            for index in range(await headers.count()):
                header = headers.nth(index)
                try:
                    if not await header.is_visible():
                        continue
                except Exception:
                    continue
                if await _visible_exact_or_group_text_in(header, title_selectors, name):
                    if await self._composer_visible():
                        return None

        composer_visible = await self._composer_visible()
        return PageOperationError(
            f"点击搜索结果后无法确认聊天已打开（输入框: {'有' if composer_visible else '无'}）"
        )

    async def _composer_visible(self) -> bool:
        for selector in MESSAGE_INPUTS:
            nodes = self.page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                try:
                    if await node.is_visible() and await node.is_editable():
                        return True
                except Exception:
                    continue
        return False


# Match individual result rows, never the plural SearchPanelitems wrapper.
_SEARCH_RESULT_ROWS = (
    '[class*="SearchPanelitembox" i], '
    '[class*="SearchPanelitem-box" i], '
    '[class*="SearchPanelitem_box" i]'
)
_SEARCH_RESULT_NAMES = (
    '[class*="SearchPanelitemtitle" i]',
    '[class*="SearchPanelitem_title" i]',
    '[class*="SearchPanelitem-title" i]',
    '[class*="SearchPanelitemname" i]',
    '[class*="SearchPanelitem_name" i]',
    '[class*="SearchPanelitem-name" i]',
)


async def _send_action_in(row: Locator) -> Locator | None:
    # User-facing text survives CSS class changes. The search row containing
    # the exact name has already been resolved before any action is considered.
    candidates = (
        row.get_by_role("button", name="发消息", exact=True),
        row.get_by_text("发消息", exact=True),
        row.locator(
            '[class*="SearchPanelitemchat_btn" i], '
            '[class*="SearchPanelitemchat-btn" i], '
            '[class*="SearchPanelitemchatBtn" i]'
        ),
    )
    for candidate in candidates:
        visible = []
        for index in range(await candidate.count()):
            node = candidate.nth(index)
            if await node.is_visible() and await node.is_enabled():
                visible.append(node)
        if len(visible) > 1:
            raise PageOperationError("同一搜索结果中存在多个发消息按钮，无法唯一确认目标")
        if visible:
            return visible[0]
    return None


async def _visible_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    return await _visible_exact_text_locator(container, selectors, expected) is not None


async def _visible_exact_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _text_equals(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_group_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    # Second-phase match for group chats: accepts a trailing "(N)"/"（N）"
    # member count. Only reached after the exact pass found nothing, so a bare
    # "test" never reaches here when an exact "test" row exists.
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _group_name_matches(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_exact_or_group_text_in(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> bool:
    # Chat-header confirmation: an exact title wins; otherwise a group member
    # count suffix also confirms. The node must be visible in both cases, so a
    # hidden stale title node can never confirm the wrong recipient.
    if await _visible_exact_text_locator(container, selectors, expected) is not None:
        return True
    return await _visible_group_text_locator(container, selectors, expected) is not None


async def _has_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    for selector in selectors:
        if await _has_exact_text(container.locator(selector), expected):
            return True
    return False


async def _has_exact_text(locators: Locator, expected: str) -> bool:
    for index in range(await locators.count()):
        if await _text_equals(locators.nth(index), expected):
            return True
    return False


async def _text_equals(locator: Locator, expected: str) -> bool:
    try:
        return (await locator.inner_text(timeout=500)).strip() == expected
    except Exception:
        return False


# Matches a group chat display name: the configured target name optionally
# followed by exactly one pair of brackets containing a pure member count,
# e.g. "4161" / "4161(7)" / "4161（123）". This is an INDEPENDENT helper kept
# separate from _text_equals so the friend exact-match semantics stay strict:
# it must never let "test" match "test1" (no trailing brackets to legitimize a
# longer name). re.fullmatch anchors both ends; re.escape makes the name literal.
_GROUP_COUNT_SUFFIX_RE_TEMPLATE = r"{name}\s*[\(（]\s*\d+\s*[\)）]"


def _group_count_suffix_matches(actual: str, expected: str) -> bool:
    actual = actual.strip()
    expected = expected.strip()
    if actual == expected:
        return True
    pattern = _GROUP_COUNT_SUFFIX_RE_TEMPLATE.format(name=re.escape(expected))
    return re.fullmatch(pattern, actual) is not None


async def _group_name_matches(locator: Locator, expected: str) -> bool:
    try:
        return _group_count_suffix_matches(
            await locator.inner_text(timeout=500), expected
        )
    except Exception:
        return False


async def first_visible(
    page: Page, selectors: tuple[str, ...], timeout_ms: int = 15_000,
    *, editable: bool = False,
) -> Locator:
    # A hidden cached editor may be the first match. Check every match and use
    # one shared deadline instead of waiting on the hidden first element.
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        for selector in selectors:
            nodes = page.locator(selector)
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                try:
                    if await node.is_visible():
                        if not editable or await node.is_editable():
                            return node
                except Exception:
                    continue
        if asyncio.get_running_loop().time() >= deadline:
            raise PageOperationError(f"找不到可用页面元素，已尝试: {', '.join(selectors)}")
        await page.wait_for_timeout(200)



# Diagnostic version only: observe the normal click and coarse network status.
# Never read request headers, cookies, response bodies, DOM text, or input values.
_DIAG_ARM = """element => {
    const key = '__douyinOpenDiagV2';
    const previous = window[key];
    if (previous && previous.handler) {
        for (const type of previous.types) document.removeEventListener(type, previous.handler, true);
    }
    const state = {types: ['pointerdown', 'mousedown', 'mouseup', 'click'], events: []};
    state.handler = event => {
        if (state.events.length < 12) state.events.push({
            type: event.type,
            trusted: event.isTrusted,
            on_selected_action: event.composedPath().includes(element)
        });
    };
    for (const type of state.types) document.addEventListener(type, state.handler, true);
    window[key] = state;
    const chain = [];
    for (let node = element; node && chain.length < 5; node = node.parentElement) {
        const propsKey = Object.keys(node).find(k => k.startsWith('__reactProps$'));
        const props = propsKey ? node[propsKey] : null;
        chain.push({
            tag: node.tagName.toLowerCase(),
            react_click_handler: !!(props && typeof props.onClick === 'function'),
            react_pointer_handler: !!(props && typeof props.onPointerDown === 'function')
        });
    }
    return {target_chain: chain};
}"""

_DIAG_SNAPSHOT = """() => {
    const visible = element => {
        const style = getComputedStyle(element);
        return style.visibility !== 'hidden' && style.display !== 'none' &&
            element.getClientRects().length > 0;
    };
    const editors = [...document.querySelectorAll('[contenteditable], textarea, [role="textbox"]')];
    const headers = [...document.querySelectorAll('[class*="RightPanelHeader"], [class*="chatHeader"], [class*="ChatHeader"]')];
    const state = window.__douyinOpenDiagV2;
    return {
        ready_state: document.readyState,
        visible_headers: headers.filter(visible).length,
        editor_count: editors.length,
        visible_editors: editors.filter(visible).length,
        editors: editors.slice(0, 8).map(element => ({
            tag: element.tagName.toLowerCase(), visible: visible(element),
            contenteditable: element.isContentEditable,
            readonly: !!element.readOnly, disabled: !!element.disabled
        })),
        visible_dialogs: [...document.querySelectorAll('[role="dialog"]')].filter(visible).length,
        iframe_count: document.querySelectorAll('iframe').length,
        click_events: state ? state.events : []
    };
}"""

_DIAG_STOP = """() => {
    const state = window.__douyinOpenDiagV2;
    if (state && state.handler) {
        for (const type of state.types) document.removeEventListener(type, state.handler, true);
    }
    delete window.__douyinOpenDiagV2;
}"""


def _network_endpoint(url: str) -> tuple[str, str]:
    # URLs often contain tokens, user IDs or message IDs. Report no full URLs.
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if not re.fullmatch(r'[a-z0-9.-]{1,128}', host):
            host = 'other'
        path = parsed.path.lower()
    except Exception:
        return 'other', 'other'
    for category, words in (
        ('auth', ('passport', 'login', 'auth')),
        ('verification', ('captcha', 'verify', 'risk')),
        ('chat', ('conversation', 'message', '/im/', '/chat')),
    ):
        if any(word in path for word in words):
            return host, category
    return host, 'other'


class _OpenDiagnostics:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.network = {}
        self.listeners = []
        self.suppressed = 0
        self.page_errors = 0

    def _record(self, kind, request, status=0) -> None:
        try:
            if request.resource_type not in ('xhr', 'fetch', 'document', 'script'):
                return
            host, category = _network_endpoint(request.url)
            status = status if isinstance(status, int) and not isinstance(status, bool) else 0
            key = (kind, host, category, status)
            if key not in self.network and len(self.network) >= 25:
                self.suppressed += 1
                return
            self.network[key] = self.network.get(key, 0) + 1
        except Exception:
            # Diagnostics must not affect the task or print exception strings
            # that might contain a full URL or page content.
            pass

    def start(self) -> None:
        callbacks = (
            ('response', lambda response: self._record('response', response.request, response.status)),
            ('requestfailed', lambda request: self._record('requestfailed', request)),
            ('pageerror', lambda error: self._record_pageerror()),
        )
        for event, callback in callbacks:
            try:
                self.page.on(event, callback)
                self.listeners.append((event, callback))
            except Exception:
                pass
    def _record_pageerror(self) -> None:
        self.page_errors += 1

    async def arm(self, action: Locator) -> None:
        try:
            data = await action.evaluate(_DIAG_ARM)
            LOGGER.info('OPEN_DIAG_V2 action %s', json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            LOGGER.info('OPEN_DIAG_V2 action_unavailable %s', type(exc).__name__)

    async def snapshot(self, stage: str) -> None:
        try:
            data = await self.page.evaluate(_DIAG_SNAPSHOT)
            LOGGER.info('OPEN_DIAG_V2 %s %s', stage, json.dumps(data, ensure_ascii=False))
            # Editors inside frames cannot be found by page-level locators.
            # Report only coarse counts for each frame, without frame names/URLs.
            frames = self.page.frames
            for index, frame in enumerate(frames[1:6], 1):
                try:
                    frame_data = await frame.evaluate(_DIAG_SNAPSHOT)
                    LOGGER.info('OPEN_DIAG_V2 %s frame_%d %s', stage, index,
                                json.dumps(frame_data, ensure_ascii=False))
                except Exception as exc:
                    LOGGER.info('OPEN_DIAG_V2 frame_unavailable %s', type(exc).__name__)
        except Exception as exc:
            LOGGER.info('OPEN_DIAG_V2 snapshot_unavailable %s', type(exc).__name__)

    def report_network(self) -> None:
        rows = [dict(kind=key[0], host=key[1], category=key[2], status=key[3], count=count)
                for key, count in self.network.items()]
        LOGGER.info('OPEN_DIAG_V2 network %s', json.dumps({
            'requests': rows, 'extra_categories': self.suppressed,
            'page_error_count': self.page_errors,
            'scope': 'click_to_chat_only; HTTP 200 does not prove business success'
        }, ensure_ascii=False))

    async def stop(self) -> None:
        for event, callback in self.listeners:
            try:
                self.page.remove_listener(event, callback)
            except Exception:
                pass
        self.listeners.clear()
        try:
            await self.page.evaluate(_DIAG_STOP)
        except Exception:
            pass
