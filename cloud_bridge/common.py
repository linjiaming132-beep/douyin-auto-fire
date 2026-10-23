"""Read-only browser checks and bounded, private session transport."""
from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_VERSION = 1
MAX_SECRET_BYTES = 47_000
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
CHAT_URL = "https://www.douyin.com/chat"


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or type(bundle.get("version")) is not int or bundle["version"] != BUNDLE_VERSION:
        raise ValueError("Unsupported session format")
    friend = bundle.get("friend")
    if not isinstance(friend, str) or not 1 <= len(friend.strip()) <= 100:
        raise ValueError("Missing recipient")
    state = bundle.get("storage_state")
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
        raise ValueError("Invalid browser state")
    sessions = bundle.get("session_storage", {})
    if not isinstance(sessions, dict) or len(sessions) > 32:
        raise ValueError("Invalid session storage")
    for origin, values in sessions.items():
        if not isinstance(origin, str) or len(origin) > 1024:
            raise ValueError("Invalid origin")
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Invalid origin")
        if not isinstance(values, dict) or len(values) > 4096 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError("Invalid storage values")
    return bundle


def pack_secret(bundle):
    validate_bundle(bundle)
    raw = json.dumps(bundle, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("Session is too large")
    encoded = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(encoded) > MAX_SECRET_BYTES:
        raise ValueError("Session exceeds one GitHub secret; nothing was exported")
    return encoded


def unpack_secret(value):
    if not isinstance(value, str) or not value or len(value) > MAX_SECRET_BYTES:
        raise ValueError("Missing or oversized session secret")
    try:
        compressed = base64.b64decode(value, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
        if len(raw) > MAX_BUNDLE_BYTES:
            raise ValueError()
        bundle = json.loads(raw)
        return validate_bundle(bundle)
    except Exception:
        raise ValueError("Cannot decode session secret") from None


def session_script(bundle):
    validate_bundle(bundle)
    data = json.dumps(bundle.get("session_storage", {}), ensure_ascii=True, separators=(",", ":"))
    return """(() => {
      const byOrigin = %s;
      if (!Object.prototype.hasOwnProperty.call(byOrigin, location.origin)) return;
      const values = byOrigin[location.origin];
      try {
        for (const [key, value] of Object.entries(values)) sessionStorage.setItem(key, value);
      } catch (_) {}
    })();""" % data


async def _visible_any(page, selectors):
    for selector in selectors:
        try:
            nodes = page.locator(selector)
            for index in range(min(await nodes.count(), 15)):
                if await nodes.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


async def probe(context, friend):
    """Open exact recipient and inspect its composer; never type chat content."""
    runtime = str(Path(__file__).resolve().parent / "runtime")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from app.douyin import DouyinChat
    from app.selectors import LOGIN_REQUIRED_MARKERS, RISK_MARKERS, SEARCH_INPUTS

    # Legacy diagnostics can inspect responses. Keep them out of cloud logs.
    logging.getLogger("douyin_sender").disabled = True
    result = {"ok": False, "stage": "navigation", "search_visible": False,
              "target_open": False, "editor_visible": False,
              "login_text_visible": False, "verification_text_visible": False}
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60_000)
        result["stage"] = "search"
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            route = urlsplit(page.url)
            on_chat = route.hostname == "www.douyin.com" and route.path.rstrip("/") == "/chat"
            result["search_visible"] = on_chat and await _visible_any(page, SEARCH_INPUTS)
            if result["search_visible"] or asyncio.get_running_loop().time() >= deadline:
                break
            await page.wait_for_timeout(300)
        result["login_text_visible"] = await _visible_any(page, LOGIN_REQUIRED_MARKERS)
        result["verification_text_visible"] = await _visible_any(page, RISK_MARKERS)
        if not result["search_visible"]:
            return result
        result["stage"] = "target"
        chat = DouyinChat(page, timeout_ms=20_000, confirm_timeout_ms=20_000)
        await chat.open_target(friend.strip(), retries=0)
        if await chat._chat_open_error(friend.strip()) is not None:
            return result
        result["target_open"] = True
        result["stage"] = "editor"
        editor = await chat.message_input()
        result["editor_visible"] = await editor.is_visible() and await editor.is_editable()
        if result["editor_visible"]:
            result.update(ok=True, stage="ready")
        return result
    except Exception:
        # Never serialize Playwright's errors: they can contain selectors,
        # recipient names, request URLs, or browser state.
        return result
