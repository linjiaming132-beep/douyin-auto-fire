"""Strict, bounded recipient settings bound to one exported browser session."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import date

from cloud_bridge import common

TARGETS_SECRET = "DOUYIN_CLOUD_TARGETS"
MAX_TARGETS = 20
MAX_SECRET_BYTES = 47_000


class TargetsConfigError(ValueError):
    """Only fixed, non-sensitive codes may cross the configuration boundary."""

    def __init__(self, code="targets_config"):
        self.code = code if code in {"targets_config", "session_mismatch"} else "targets_config"
        super().__init__(self.code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError


def _loads(value):
    return json.loads(value, object_pairs_hook=_object, parse_constant=_invalid_constant)


def _valid_friend(value):
    if not isinstance(value, str) or any(character in value for character in ("\r", "\n", "\0")):
        raise ValueError
    friend = value.strip()
    if not 1 <= len(friend) <= 100:
        raise ValueError
    friend.encode("utf-8")
    return friend


def _valid_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or "\0" in value:
        raise ValueError
    value.encode("utf-8")
    return value


def _valid_days(value):
    if not isinstance(value, list) or len(value) > 7:
        raise ValueError
    days = []
    for day in value:
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise ValueError
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
        days.append(day)
    if len(set(days)) != len(days):
        raise ValueError
    return sorted(days)


def session_digest(bundle):
    """Hash the complete bundle using a stable representation; reveal no fields."""
    try:
        common.validate_bundle(bundle)
        canonical = json.dumps(bundle, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()
    except Exception:
        raise TargetsConfigError() from None


def parse_targets(value, bundle=None):
    """Return validated settings; accept only the exact version-one schema."""
    try:
        if isinstance(value, str):
            if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
                raise ValueError
            value = _loads(value)
        if not isinstance(value, dict) or set(value) != {"version", "session_sha256", "targets"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError
        digest, entries = value["session_sha256"], value["targets"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TARGETS:
            raise ValueError
        normalized = []
        seen = set()
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"friend", "text", "imported_days"}:
                raise ValueError
            friend = _valid_friend(item["friend"])
            if friend in seen:
                raise ValueError
            seen.add(friend)
            normalized.append({"friend": friend, "text": _valid_text(item["text"]),
                               "imported_days": _valid_days(item["imported_days"])})
        result = {"version": 1, "session_sha256": digest.lower(), "targets": normalized}
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_SECRET_BYTES:
            raise ValueError
        if bundle is not None and not hmac.compare_digest(result["session_sha256"], session_digest(bundle)):
            raise TargetsConfigError("session_mismatch")
        return result
    except TargetsConfigError:
        raise
    except Exception:
        raise TargetsConfigError() from None
