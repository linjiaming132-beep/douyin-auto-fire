from __future__ import annotations

# Required Notice: Copyright (c) 2026 unmev
# Derived from douyin-auto-fire; PolyForm Noncommercial License 1.0.0.
# https://polyformproject.org/licenses/noncommercial/1.0.0

import asyncio
import json
import logging
import re
import random
import secrets
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

from app.douyin import DouyinChat, PageOperationError, first_visible
from app.models import Message, Sticker
from app.selectors import IMAGE_INPUTS, MESSAGE_INPUTS, STICKER_BUTTONS, STICKER_PANELS


def _monotonic() -> float:
    """Monotonic clock for the send-state deadline.

    Indirected (rather than calling ``asyncio.get_running_loop().time()``
    inline) so tests can fast-forward the clock without real wall-clock waits.
    """
    return asyncio.get_running_loop().time()


SEND_BUTTONS = (
    '[class*="messageMsgInputpublishBtn"]',
    '.e2e-send-msg-bt',
    'button[aria-label*="发送"]',
    '[role="button"][aria-label*="发送"]',
)


async def _trigger_send(page: Page) -> None:
    button = None
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                button = candidate
                break
        except Exception:
            continue
    if button is not None:
        _SEND_LOG.info('SEND_DIAG_V1 trigger=button_click')
        await button.click()
    else:
        _SEND_LOG.info("SEND_DIAG_V1 trigger=enter_key")
        await page.keyboard.press("Enter")


async def _publish_ready(page: Page) -> bool:
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                return True
        except Exception:
            continue
    return False


LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    '.messageMessageBoxmessageBox:has(.messageMessageBoxcontentBox.messageMessageBoxisFromMe)'
)
MESSAGE_CONFIRM_ANCHOR = "data-douyin-sender-anchor"

# Send-status markers, scoped to the single outgoing message being confirmed.
#
# Douyin renders an outgoing bubble *before* its send status is resolved: the
# bubble appears, a spinner sits beside it, and only later does it clear
# (success) or flip to a retry marker. We must wait for a terminal state
# rather than assume a visible bubble means success (Issue #11).
#
# Failure: the red `!` retry control. `ContentSideSendStatusretry` is the real
# Douyin class from Issue #11; `SendStatusretry` is a stable fallback. The bare
# `SendStatusicon` class is intentionally excluded -- it is shared by other
# send states and would cause false failures.
SEND_FAILURE_MARKERS = (
    "text=发送失败",
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="sendFailed"]',
    '[class*="SendFailed"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)
# Pending: the in-flight spinner beside the bubble. These are checked only on
# the scoped outgoing message, never page-wide, so unrelated loading spinners
# cannot produce a false pending state.
SEND_PENDING_MARKERS = (
    ".semi-spin",
    '[class*="im-saas-message-spin"]',
    '[data-icon="spin"]',
)
SEND_RETRY_MARKERS = (
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)

# Overall budget for confirming a single message reaches a terminal state. A
# stuck spinner past this is treated as failure/uncertain, never success.
SEND_CONFIRM_TIMEOUT_MS = 15_000
# Poll interval for re-checking pending/failure state.
SEND_POLL_INTERVAL_MS = 300
# Short grace after pending clears: the retry marker can mount a tick after the
# spinner disappears, so require a stable non-pending frame before success.
SEND_STABLE_INTERVAL_MS = 500
# Initial-clean grace window. A freshly matched bubble with no spinner and no
# failure marker is NOT yet success -- Douyin mounts the spinner/retry *after*
# the bubble. We must keep observing until either a state appears or the whole
# grace window stays clean (Issue #11 E2E regression: late-mounting retry was
# missed because the old single 500ms check ended before the retry mounted).
# This is a continuous observation window, NOT a single sleep: the loop polls
# every SEND_POLL_INTERVAL_MS throughout it.
SEND_INITIAL_CLEAN_GRACE_MS = 2_000


async def _send_message_without_diagnostics(page: Page, chat: DouyinChat, message: Message, stickers: dict[str, Sticker]) -> None:
    if message.type == "random":
        await send_message(page, chat, random.choice(message.choices), stickers)
        return
    if message.type == "text":
        await send_text(chat, message.content or "")
        return
    if message.type == "image":
        if message.path is None:
            raise PageOperationError("图片消息缺少文件路径")
        await send_image(page, message.path.as_posix())
        return
    if message.type == "douyin_sticker":
        sticker = stickers.get(message.sticker or "")
        if sticker is None:
            raise PageOperationError(f"没有原生表情映射: {message.sticker}")
        await send_douyin_sticker(page, sticker)
        return
    raise PageOperationError(f"不支持的消息类型: {message.type}")


async def send_text(chat: DouyinChat, content: str) -> None:
    editor = await chat.message_input()
    page = editor.page
    await editor.click()
    await page.keyboard.insert_text(content)
    try:
        await page.wait_for_function(
            """([txt]) => {
                const es = [...document.querySelectorAll('[class*=messageEditor] [contenteditable=true], .messageEditorinputArea')];
                return es.some(e => (e.innerText || '').includes(txt));
            }""",
            arg=[content],
            timeout=5_000,
        )
    except Exception as exc:
        raise PageOperationError("文字未能写入聊天输入框") from exc

    _SEND_LOG.info('SEND_DIAG_V1 stage=editor_verified')
    before = await _mark_latest_outgoing_message(page)
    await page.wait_for_timeout(300)
    await _trigger_send(page)
    await _confirm_outgoing_message(page, before, label="文字", expected_text=content)


async def send_image(page: Page, image_path: str) -> None:
    before = await _mark_latest_outgoing_message(page)
    file_input = None
    for selector in IMAGE_INPUTS:
        candidate = page.locator(selector).first
        if await candidate.count():
            file_input = candidate
            break
    if file_input is None:
        raise PageOperationError("找不到图片上传控件")
    await file_input.set_input_files(image_path)
    await page.wait_for_timeout(1_500)

    await _trigger_send(page)
    try:
        # The image bubble appearing is only the *start* of confirmation: the
        # send may still be in flight or already failed. Anchor on the latest
        # outgoing message (the freshly inserted image bubble) and wait for it
        # to reach a real terminal state, exactly as text/sticker does. A mere
        # bubble count increase is not success (Issue #11).
        await page.wait_for_function(
            """([selector, anchor]) => {
                const message = document.querySelector(selector);
                if (!message) return false;
                return message.getAttribute('data-douyin-sender-anchor') !== anchor;
            }""",
            arg=[LATEST_OUTGOING_MESSAGE, before[0]],
            timeout=15_000,
        )
        latest = page.locator(LATEST_OUTGOING_MESSAGE).first
        await _await_send_terminal_state(page, latest, "图片")
    except PageOperationError:
        raise
    except Exception as exc:
        raise PageOperationError("图片消息已触发发送，但无法确认是否发送成功；为避免重复不会自动重试") from exc

async def _restore_composer(page: Page, timeout_ms: int = 10_000) -> None:
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        editor = await first_visible(page, MESSAGE_INPUTS, timeout_ms)
    except Exception:
        return
    try:
        await editor.click(timeout=timeout_ms)
        await editor.focus()
    except Exception:
        pass


async def send_douyin_sticker(page: Page, sticker: Sticker) -> None:
    before = await _mark_latest_outgoing_message(page)
    try:
        button = await first_visible(page, STICKER_BUTTONS)
        await button.click(force=True)
        panel = await first_visible(page, STICKER_PANELS)

        if sticker.category:
            category = panel.get_by_text(sticker.category, exact=True)
            if await category.count() and await category.first.is_visible():
                await category.first.click()

        name = sticker.accessible_name or sticker.name
        item = panel.locator('.emojiEmojiItememojiItem').filter(has_text=name)
        for index in range(await item.count()):
            candidate = item.nth(index)
            description = candidate.locator('.emojiEmojiItememojiItemDesc')
            if await description.count() and (await description.first.inner_text()).strip() == name:
                await _click_and_confirm_sticker(page, candidate, before, name)
                return

        candidates = (
            panel.get_by_role("img", name=name, exact=True),
            panel.get_by_role("button", name=name, exact=True),
            panel.locator(f'[aria-label="{_css_escape(name)}"]'),
            panel.locator(f'[title="{_css_escape(name)}"]'),
            panel.locator(f'[alt="{_css_escape(name)}"]'),
        )
        for candidate in candidates:
            if await candidate.count() and await candidate.first.is_visible():
                await _click_and_confirm_sticker(page, candidate.first, before, name)
                return

        if sticker.fallback_index is not None:
            items = panel.locator('[role="button"], img, [aria-label], [title]')
            if await items.count() > sticker.fallback_index:
                await _click_and_confirm_sticker(page, items.nth(sticker.fallback_index), before, name)
                return
        raise PageOperationError(f"在抖音表情面板中找不到原生表情: {sticker.name}")
    finally:
        await _restore_composer(page)


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _mark_latest_outgoing_message(page: Page) -> tuple[str, str]:
    anchor = secrets.token_hex(8)
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    if not await latest.count():
        return anchor, ""

    content = latest.locator('[data-e2e="msg-item-content"]').first
    before_content = await content.inner_html() if await content.count() else await latest.inner_html()
    await latest.evaluate(
        "(element, value) => element.setAttribute('data-douyin-sender-anchor', value)",
        anchor,
    )
    return anchor, before_content


async def _click_and_confirm_sticker(page: Page, item, before: tuple[str, str], name: str) -> None:
    resource_key = await _sticker_resource_key(item)
    await item.click(force=True)
    try:
        await _confirm_sticker_sent(page, before, name, resource_key)
    except PageOperationError as exc:
        if "页面提示可以重试" in str(exc) and await _click_retry_on_latest_failed_message(page):
            await _confirm_sticker_sent(page, before, name, resource_key)
            return
        if await _publish_ready(page):
            await _trigger_send(page)
            await _confirm_sticker_sent(page, before, name, resource_key)
        else:
            raise


async def _sticker_resource_key(item) -> str:
    src = await item.get_attribute("src")
    if not src:
        image = item.locator("img").first
        if await image.count():
            src = await image.get_attribute("src")
    if not src:
        return ""
    return urlsplit(src).path.rsplit("/", 1)[-1]


async def _confirm_sticker_sent(
    page: Page,
    before: tuple[str, str],
    name: str,
    resource_key: str = "",
) -> None:
    await _confirm_outgoing_message(page, before, f"原生表情“{name}”", resource_key=resource_key)


async def _click_retry_on_latest_failed_message(page: Page) -> bool:
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    for selector in SEND_RETRY_MARKERS:
        marker = latest.locator(selector).first
        try:
            if await marker.count() and await marker.is_visible():
                await marker.click(force=True)
                return True
        except Exception:
            continue
    return False


async def _marker_visible(scope: Locator, selectors: tuple[str, ...]) -> bool:
    """True if any selector in ``selectors`` resolves to a visible element.

    Scoped to ``scope`` (the single outgoing message) so unrelated page-wide
    spinners cannot influence the verdict.
    """
    for selector in selectors:
        marker = scope.locator(selector).first
        try:
            if await marker.count() and await marker.is_visible():
                return True
        except Exception:
            continue
    return False


async def _await_send_terminal_state(
    page: Page,
    scope: Locator,
    label: str,
    timeout_ms: int = SEND_CONFIRM_TIMEOUT_MS,
) -> None:
    """Wait for ``scope`` (one outgoing message) to reach a terminal state.

    State machine (Issue #11):

        MATCHED
           |
           v
        OBSERVING_INITIAL  (bubble matched, status not yet resolved)
           |  failure visible            -> FAILED
           |  pending visible            -> WAITING_PENDING
           |  clean for the whole grace  -> SUCCESS
           v
        WAITING_PENDING  (spinner visible)
           |  failure visible            -> FAILED
           |  pending gone              -> STABILIZING
           v
        STABILIZING  (spinner just cleared)
           |  failure visible            -> FAILED
           |  pending reappeared         -> WAITING_PENDING
           |  stable clean window held   -> SUCCESS

    Critical correctness rule: a *newly* matched bubble that is clean is treated
    as UNKNOWN/OBSERVING, never as success, because Douyin mounts the spinner or
    retry marker *after* the bubble (Issue #11 E2E regression: the old code
    declared success after a single 500ms check, before a late retry mounted).
    The initial-clean grace window therefore polls continuously; success only
    after the bubble stays clean for the full window (or pending clears + holds).

    ``timeout_ms`` is the overall budget; a stuck spinner past it raises, never
    success. ``page.wait_for_timeout`` advances the injectable monotonic clock,
    so tests run in milliseconds while simulating multi-second windows.
    """
    deadline = _monotonic() + timeout_ms / 1000

    # Phase 1: observe the freshly matched bubble. It is UNKNOWN until either a
    # state marker appears or the initial-clean grace window elapses clean.
    grace_deadline = _monotonic() + SEND_INITIAL_CLEAN_GRACE_MS / 1000
    while _monotonic() < grace_deadline:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if await _marker_visible(scope, SEND_PENDING_MARKERS):
            break  # -> resolve pending in Phase 2
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)
    else:
        # Grace window elapsed fully clean -> fast success (normal fast send).
        return

    # Phase 2: a spinner appeared. Wait for it to clear (or flip to failure),
    # then require a stable clean window before success.
    while True:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if not await _marker_visible(scope, SEND_PENDING_MARKERS):
            # Spinner gone. Require it to STAY clear across the stable window --
            # the retry marker can mount a tick after the spinner disappears.
            await page.wait_for_timeout(SEND_STABLE_INTERVAL_MS)
            if await _marker_visible(scope, SEND_FAILURE_MARKERS):
                raise PageOperationError(f"{label}发送失败，页面提示可以重试")
            if not await _marker_visible(scope, SEND_PENDING_MARKERS):
                return  # terminal: success
            # spinner reappeared -> keep waiting
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)


async def _confirm_outgoing_message(
    page: Page,
    before: tuple[str, str],
    label: str,
    resource_key: str = "",
    expected_text: str = "",
) -> None:
    anchor, before_content = before
    try:
        await page.wait_for_function(
            """([selector, anchor, previousContent, expectedResource, expectedText]) => {
                const message = document.querySelector(selector);
                if (!message) return false;
                const content = message.querySelector('[data-e2e="msg-item-content"]') || message;
                const isNewMessage =
                    message.getAttribute('data-douyin-sender-anchor') !== anchor ||
                    content.innerHTML !== previousContent;
                if (!isNewMessage) return false;
                if (expectedText) {
                    const normalize = value => (value || '').replace(/[\\s\\u200B\\u200C\\u200D\\uFEFF]+/g, ' ').trim();
                    return normalize(content.innerText).includes(normalize(expectedText));
                }
                if (!expectedResource) return true;
                const images = [...content.querySelectorAll('img')];
                return images.some(image => (image.src || '').includes(expectedResource)) || images.length > 0;
            }""",
            arg=[LATEST_OUTGOING_MESSAGE, anchor, before_content, resource_key, expected_text],
            timeout=15_000,
        )
        # The bubble now matches our payload, but the send may still be in
        # flight or have already failed. Wait for a real terminal state rather
        # than treating a visible bubble as success (Issue #11).
        _SEND_LOG.info('SEND_DIAG_V1 stage=bubble_matched')
        latest = page.locator(LATEST_OUTGOING_MESSAGE).first
        await _await_send_terminal_state(page, latest, label)
    except PageOperationError:
        raise
    except Exception as exc:
        raise PageOperationError(f"{label}已发送，但没有检测到新的已发送消息") from exc
    finally:
        anchors = page.locator(f"[{MESSAGE_CONFIRM_ANCHOR}]")
        try:
            await anchors.evaluate_all(
                "elements => elements.forEach(element => element.removeAttribute('data-douyin-sender-anchor'))"
            )
        except Exception:
            pass


def _result_hint(value) -> str:
    # Only fixed categories leave the process; never echo arbitrary server text.
    if not isinstance(value, str):
        return 'absent'
    upper = value.strip().upper()
    if upper == 'OK':
        return 'OK'
    for label in (
        'INVALID_REQUEST', 'INVALID_PARAM', 'UNAUTHORIZED', 'AUTH_FAILED',
        'TOKEN_EXPIRED', 'TOKEN_INVALID', 'SESSION_EXPIRED', 'FORBIDDEN',
        'KICK', 'NOT_LOGIN', 'NOT_AUTHORIZED', 'TOO_MANY_REQUESTS',
    ):
        if re.search(r'(?<![A-Z0-9_])' + label + r'(?![A-Z0-9_])', upper):
            return label
    for label, terms in (
        ('SESSION_LENGTH', ('SESSION LENGTH',)),
        ('VERIFICATION', ('CAPTCHA', '安全验证', '验证身份')),
        ('AUTH', ('UNAUTHENTICATED', '登录失效', '未登录', 'TOKEN EXPIRED')),
        ('RATE_LIMIT', ('RATE LIMIT', '操作频繁', '请求频繁')),
    ):
        if any(term in upper for term in terms):
            return label
    return 'other_text'


def _safe_json_statuses(data) -> dict:
    output = {}
    if not isinstance(data, dict):
        return output
    containers = [('root', data)]
    for key in ('data', 'base_resp', 'BaseResp', 'extra'):
        if isinstance(data.get(key), dict):
            containers.append((key, data[key]))
    for prefix, container in containers:
        for key in ('status_code', 'error_code', 'code', 'err_no', 'retcode', 'StatusCode'):
            value = container.get(key)
            if isinstance(value, str) and re.fullmatch(r'-?\d{1,9}', value):
                value = int(value)
            if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 999_999_999:
                output[prefix + '.' + key] = value
        for key in ('message', 'msg', 'error_desc', 'status_msg', 'description', 'StatusMessage'):
            if key in container:
                output[prefix + '.' + key] = _result_hint(container[key])
    return output


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('truncated varint')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError('oversized varint')


def _im_result_summary(data: bytes) -> dict:
    if len(data) > 2_000_000:
        return {'format': 'too_large'}
    stripped = data.lstrip()
    if not stripped:
        return {'format': 'empty'}
    if stripped.startswith((b'{', b'[')):
        try:
            return {'format': 'json', **_safe_json_statuses(json.loads(data))}
        except Exception:
            return {'format': 'json_unreadable'}
    if len(data) <= 1024 and re.fullmatch(rb'[\x20-\x7e\r\n\t]+', data):
        hint = _result_hint(data.decode('ascii'))
        return {'format': 'text', 'message_hint': hint}

    # Read only the outer response envelope: field 1 cmd, field 3 error_desc,
    # field 4 message. Skip field 6 (conversations/messages/tickets) entirely.
    # Schema reference: cv-cat/DouYin_Spider static/Response.proto.
    output = {'format': 'protobuf_envelope_candidate', 'body_present': False}
    offset = 0
    fields = 0
    try:
        while offset < len(data):
            fields += 1
            if fields > 128:
                raise ValueError('too many envelope fields')
            key, offset = _read_varint(data, offset)
            number, wire = key >> 3, key & 7
            if not number:
                raise ValueError('invalid field number')
            if wire == 0:
                value, offset = _read_varint(data, offset)
                if number == 1 and value <= 999_999:
                    output['cmd'] = value
            elif wire == 1:
                offset += 8
            elif wire == 2:
                size, offset = _read_varint(data, offset)
                end = offset + size
                if end > len(data):
                    raise ValueError('truncated field')
                if number in (3, 4) and size <= 1024:
                    output['error_hint' if number == 3 else 'message_hint'] = _result_hint(
                        data[offset:end].decode('utf-8', errors='replace')
                    )
                if number == 6:
                    output['body_present'] = bool(size)
                offset = end
            elif wire == 5:
                offset += 4
            else:
                raise ValueError('unsupported wire type')
            if offset > len(data):
                raise ValueError('truncated fixed field')
    except Exception:
        return {'format': 'not_decoded'}
    return output


# Passive send-stage diagnostics. No request replay or automatic retry.
_SEND_LOG = logging.getLogger("douyin_sender")


def _send_endpoint(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
    except Exception:
        return "ignored", "ignored"
    if host in ("imapi.douyin.com", "imapi.snssdk.com"):
        return "douyin_im" if host.endswith("douyin.com") else "bytedance_im", "im"
    if host == "www.douyin.com":
        if re.search(r"/(?:im|message|messages|conversation)(?:/|$)", path):
            return "douyin_web", "im"
        if any(word in path for word in ("passport", "login", "captcha", "verify")):
            return "douyin_web", "auth"
    if host.endswith(".snssdk.com") and host.startswith("frontier"):
        return "bytedance_gateway", "gateway"
    return "ignored", "ignored"


def _send_failure_hint(value):
    text = str(value or "").upper()
    for label, terms in (
        ("timeout", ("TIMED_OUT", "TIMEOUT")),
        ("connection", ("CONNECTION", "DISCONNECTED", "NETWORK_CHANGED")),
        ("dns", ("NAME_NOT_RESOLVED", "DNS")),
        ("tls", ("CERT_", "SSL_")),
        ("blocked", ("BLOCKED", "ACCESS_DENIED")),
        ("cancelled", ("ABORTED", "CANCELED", "CANCELLED")),
    ):
        if any(term in text for term in terms):
            return label
    return "other"


class _SendDiagnostics:
    """Observe one send, including writing, triggering and UI confirmation.

    HTTP response bodies are parsed in memory only for allowlisted IM endpoints.
    Logs contain fixed classifications/numeric status codes, never raw bodies,
    headers, full URLs, exception messages, friend names or message content.
    CDP WebSocket observation reports metadata only, including unknown existing
    connections. WebSocket payloads are never parsed or printed.
    """

    def __init__(self, page):
        self.page = page
        self.source = page.context
        self.listeners = []
        self.tasks = []
        self.requests = {}
        self.http_requests = self.http_responses = self.http_failures = 0
        self.im_bodies = 0
        self.truncated = False
        self.cdp = None
        self.ws_routes = {}
        self.ws_sent = self.ws_received = self.ws_errors = 0
        self.ws_logged = 0
        self.closed = False

    async def start(self):
        _SEND_LOG.info("SEND_DIAG_V1 begin scope=send_stage")
        for event, callback in (
            ("request", self._request),
            ("response", self._response),
            ("requestfailed", self._failed),
        ):
            try:
                self.source.on(event, callback)
                self.listeners.append((event, callback))
            except Exception:
                _SEND_LOG.info("SEND_DIAG_V1 observer=http_unavailable")
        try:
            self.cdp = await self.page.context.new_cdp_session(self.page)
            self.cdp.on("Network.webSocketCreated", self._ws_created)
            self.cdp.on("Network.webSocketFrameSent", lambda event: self._ws_frame(event, True))
            self.cdp.on("Network.webSocketFrameReceived", lambda event: self._ws_frame(event, False))
            self.cdp.on("Network.webSocketFrameError", self._ws_error)
            self.cdp.on("Network.webSocketHandshakeResponseReceived", self._ws_handshake)
            await self.cdp.send("Network.enable")
            _SEND_LOG.info("SEND_DIAG_V1 observer=websocket_metadata_enabled")
        except Exception:
            _SEND_LOG.info("SEND_DIAG_V1 observer=websocket_unavailable")

    def _identify(self, request):
        route = _send_endpoint(request.url)
        if route[0] == "ignored":
            return None
        key = id(request)
        if key not in self.requests:
            if len(self.requests) >= 80:
                self.truncated = True
                return None
            # Retain object to prevent Python id reuse during the observation.
            self.requests[key] = (len(self.requests) + 1, route, request)
        return self.requests[key][:2]

    def _request(self, request):
        if self.closed:
            return
        try:
            info = self._identify(request)
            if info is None:
                return
            number, (host_group, category) = info
            method = request.method if request.method in ("GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH") else "other"
            self.http_requests += 1
            _SEND_LOG.info("SEND_DIAG_V1 request=%d group=%s category=%s method=%s", number, host_group, category, method)
        except Exception:
            pass

    def _response(self, response):
        if self.closed:
            return
        try:
            info = self._identify(response.request)
            if info is None:
                return
            number, (host_group, category) = info
            self.http_responses += 1
            status = response.status
            status = status if isinstance(status, int) and 100 <= status <= 599 else 0
            _SEND_LOG.info("SEND_DIAG_V1 response=%d group=%s category=%s http=%d", number, host_group, category, status)
            if category == "im" and len(self.tasks) < 6:
                self.tasks.append(asyncio.create_task(self._inspect_body(response, number, host_group)))
        except Exception:
            pass

    async def _inspect_body(self, response, number, host_group):
        self.im_bodies += 1
        try:
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > 2_000_000:
                result = {"format": "too_large"}
            else:
                payload = await response.body()
                if host_group == "douyin_web" and not payload.lstrip().startswith((b"{", b"[")):
                    result = {"format": "not_decoded"}
                else:
                    result = _im_result_summary(payload)
            for key, value in result.items():
                _SEND_LOG.info("SEND_DIAG_V1 reply=%d %s=%s", number, key, value)
        except Exception:
            _SEND_LOG.info("SEND_DIAG_V1 reply=%d format=unavailable", number)

    def _failed(self, request):
        if self.closed:
            return
        try:
            info = self._identify(request)
            if info is None:
                return
            number, (host_group, category) = info
            self.http_failures += 1
            _SEND_LOG.info("SEND_DIAG_V1 failed=%d group=%s category=%s reason=%s", number, host_group, category, _send_failure_hint(request.failure))
        except Exception:
            pass

    def _ws_created(self, event):
        if self.closed:
            return
        if len(self.ws_routes) < 40:
            self.ws_routes[event.get("requestId")] = _send_endpoint(event.get("url", ""))[0]

    def _ws_frame(self, event, outgoing):
        if self.closed:
            return
        if outgoing:
            self.ws_sent += 1
        else:
            self.ws_received += 1
        if self.ws_logged >= 20:
            return
        self.ws_logged += 1
        group = self.ws_routes.get(event.get("requestId"), "unknown_existing")
        response = event.get("response") or {}
        opcode = response.get("opcode")
        opcode = opcode if opcode in (0, 1, 2, 8, 9, 10) else -1
        # Only log wire size, not frame data. Binary CDP payloads use base64.
        payload = response.get("payloadData", "")
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else 0
        _SEND_LOG.info("SEND_DIAG_V1 websocket direction=%s group=%s opcode=%d wire_chars=%d", "sent" if outgoing else "received", group, opcode, size)

    def _ws_error(self, event):
        if self.closed:
            return
        self.ws_errors += 1
        _SEND_LOG.info("SEND_DIAG_V1 websocket_error reason=%s", _send_failure_hint(event.get("errorMessage")))

    def _ws_handshake(self, event):
        if self.closed:
            return
        status = (event.get("response") or {}).get("status")
        status = status if isinstance(status, (int, float)) and 100 <= status <= 599 else 0
        group = self.ws_routes.get(event.get("requestId"), "unknown_existing")
        _SEND_LOG.info("SEND_DIAG_V1 websocket_handshake group=%s http=%d", group, status)

    async def finish(self, outcome):
        try:
            # Give terminal responses a short chance to finish after the UI result.
            await self.page.wait_for_timeout(400)
        except Exception:
            pass
        self.closed = True
        for event, callback in self.listeners:
            try:
                self.source.remove_listener(event, callback)
            except Exception:
                pass
        self.listeners.clear()
        try:
            pending = [task for task in self.tasks if not task.done()]
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=2)
                for task in unfinished:
                    task.cancel()
                if unfinished:
                    await asyncio.gather(*unfinished, return_exceptions=True)
        finally:
            if self.cdp is not None:
                try:
                    await self.cdp.detach()
                except Exception:
                    pass
            _SEND_LOG.info("SEND_DIAG_V1 summary outcome=%s requests=%d responses=%d network_failures=%d im_bodies=%d ws_sent=%d ws_received=%d ws_errors=%d http_truncated=%s", outcome, self.http_requests, self.http_responses, self.http_failures, self.im_bodies, self.ws_sent, self.ws_received, self.ws_errors, self.truncated)


async def send_message(page: Page, chat: DouyinChat, message: Message, stickers: dict[str, Sticker]) -> None:
    # Resolve random choices before the wrapper to avoid duplicate observers.
    while message.type == "random":
        message = random.choice(message.choices)
    diagnostic = _SendDiagnostics(page)
    outcome = "operation_failed"
    try:
        await diagnostic.start()
        await _send_message_without_diagnostics(page, chat, message, stickers)
        outcome = "ui_confirmed"
    except PageOperationError as exc:
        if "页面提示可以重试" in str(exc):
            outcome = "ui_retry_marker"
        elif "文字未能写入" in str(exc):
            outcome = "editor_not_written"
        elif "状态未能确认" in str(exc):
            outcome = "ui_state_uncertain"
        raise
    finally:
        try:
            await diagnostic.finish(outcome)
        except Exception:
            _SEND_LOG.info("SEND_DIAG_V1 cleanup=unavailable")
