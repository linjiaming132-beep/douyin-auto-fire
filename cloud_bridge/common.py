"""Read-only browser checks and bounded, private session transport."""
from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import hashlib
import hmac
import io
import json
import logging
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_VERSION = 1
MAX_SECRET_BYTES = 47_000
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
PART_BYTES = 24_000
MAX_PARTS = 16
MAX_ENCODED_BYTES = PART_BYTES * MAX_PARTS
PARTS_FORMAT = "douyin-session-parts-v1"
MAIN_SECRET = "DOUYIN_CLOUD_SESSION"
TRANSPORT_CODES = frozenset({
    "missing_session", "invalid_manifest", "missing_part", "unexpected_part",
    "invalid_part", "length_mismatch", "checksum_mismatch", "invalid_base64",
    "invalid_gzip", "invalid_json", "invalid_bundle", "bundle_too_large",
    "encoded_too_large", "single_secret_too_large",
})
CHAT_URL = "https://www.douyin.com/chat"


class SessionTransportError(ValueError):
    """Only a fixed code and bounded non-sensitive counters may escape."""

    def __init__(self, code, *, parts_expected=0, parts_present=0):
        self.code = code if isinstance(code, str) and code in TRANSPORT_CODES else "invalid_manifest"
        self.parts_expected = parts_expected if type(parts_expected) is int and 0 <= parts_expected <= MAX_PARTS else 0
        self.parts_present = parts_present if type(parts_present) is int and 0 <= parts_present <= MAX_PARTS else 0
        super().__init__(self.code)


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


def _encode_bundle(bundle):
    try:
        validate_bundle(bundle)
        raw = json.dumps(bundle, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        raise SessionTransportError("invalid_bundle") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise SessionTransportError("bundle_too_large")
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def pack_secret(bundle):
    encoded = _encode_bundle(bundle)
    if len(encoded) > MAX_SECRET_BYTES:
        raise SessionTransportError("single_secret_too_large")
    return encoded


def _invalid_constant(_value):
    raise ValueError()


def _decode_bundle(value):
    try:
        compressed = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise SessionTransportError("invalid_base64") from None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
    except Exception:
        raise SessionTransportError("invalid_gzip") from None
    if len(raw) > MAX_BUNDLE_BYTES:
        raise SessionTransportError("bundle_too_large")
    try:
        bundle = json.loads(raw, parse_constant=_invalid_constant)
    except Exception:
        raise SessionTransportError("invalid_json") from None
    try:
        return validate_bundle(bundle)
    except Exception:
        raise SessionTransportError("invalid_bundle") from None


def unpack_secret(value):
    if not isinstance(value, str) or not value:
        raise SessionTransportError("missing_session")
    if len(value) > MAX_SECRET_BYTES:
        raise SessionTransportError("single_secret_too_large")
    return _decode_bundle(value)


def pack_parts(bundle):
    encoded = _encode_bundle(bundle)
    if len(encoded) > MAX_ENCODED_BYTES:
        raise SessionTransportError("encoded_too_large")
    parts = [encoded[index:index + PART_BYTES] for index in range(0, len(encoded), PART_BYTES)]
    manifest = {"format": PARTS_FORMAT, "count": len(parts), "length": len(encoded),
                "sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest()}
    values = {MAIN_SECRET: json.dumps(manifest, separators=(",", ":"))}
    values.update({f"{MAIN_SECRET}_{index:02d}": part for index, part in enumerate(parts, 1)})
    return values


def parts_info(secrets):
    """Return only safe counts, never any part content or encoded length."""
    if not isinstance(secrets, Mapping):
        return 0, 0
    expected = 0
    main = secrets.get(MAIN_SECRET)
    if isinstance(main, str) and len(main) <= MAX_SECRET_BYTES:
        try:
            manifest = json.loads(main)
            count = manifest.get("count") if isinstance(manifest, dict) else None
            if (isinstance(manifest, dict) and manifest.get("format") == PARTS_FORMAT
                    and type(count) is int and 1 <= count <= MAX_PARTS):
                expected = count
        except Exception:
            pass
    present = sum(isinstance(secrets.get(f"{MAIN_SECRET}_{index:02d}"), str)
                  and bool(secrets[f"{MAIN_SECRET}_{index:02d}"])
                  for index in range(1, (expected or MAX_PARTS) + 1))
    return expected, present


def unpack_parts(secrets):
    if not isinstance(secrets, Mapping):
        raise SessionTransportError("invalid_manifest")
    expected, present = parts_info(secrets)

    def fail(code):
        raise SessionTransportError(code, parts_expected=expected, parts_present=present)

    main = secrets.get(MAIN_SECRET)
    if not isinstance(main, str) or not main:
        fail("missing_session")
    # A manifest is tiny; bound it before parsing potentially untrusted JSON.
    if len(main) > MAX_SECRET_BYTES:
        fail("invalid_manifest" if main.lstrip().startswith(("{", "[")) else "single_secret_too_large")
    try:
        manifest = json.loads(main)
    except Exception:
        if main.lstrip().startswith(("{", "[")):
            fail("invalid_manifest")
        if present:
            fail("unexpected_part")
        try:
            return unpack_secret(main)
        except SessionTransportError as exc:
            fail(exc.code)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"format", "count", "length", "sha256"}
            or manifest.get("format") != PARTS_FORMAT):
        fail("invalid_manifest")
    count, length, checksum = manifest["count"], manifest["length"], manifest["sha256"]
    if (type(count) is not int or not 1 <= count <= MAX_PARTS
            or type(length) is not int or not 1 <= length <= MAX_ENCODED_BYTES
            or not (count - 1) * PART_BYTES < length <= count * PART_BYTES
            or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum)):
        fail("invalid_manifest")
    selected = []
    for index in range(1, count + 1):
        value = secrets.get(f"{MAIN_SECRET}_{index:02d}")
        if value is None or value == "":
            fail("missing_part")
        if not isinstance(value, str) or not value.isascii() or len(value) > PART_BYTES:
            fail("invalid_part")
        selected.append(value)
    # Extra old parts outside this manifest are deliberately ignored: a newer,
    # smaller snapshot must not require deleting all surplus GitHub Secrets.
    encoded = "".join(selected)
    if len(encoded) != length:
        fail("length_mismatch")
    actual = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    if not hmac.compare_digest(actual, checksum.lower()):
        fail("checksum_mismatch")
    try:
        return _decode_bundle(encoded)
    except SessionTransportError as exc:
        fail(exc.code)


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
