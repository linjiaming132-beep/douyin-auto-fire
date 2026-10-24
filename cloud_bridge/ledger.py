"""Durable, private daily reservations through GitHub Contents CAS writes."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BRANCH = "douyin-send-state"
STATE_PATH = "state/douyin-send-history.json"
MAX_JSON_BYTES = 256 * 1024
MAX_BASE64_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
RESULTS = frozenset({"reserved_unknown", "ui_confirmed", "send_failed_or_uncertain",
                     "send_retry_marker", "timeout", "config_changed", "date_changed",
                     "imported_local"})
KINDS = frozenset({"manual", "scheduled", "manual_extra_test", "imported_local"})
CODES = frozenset({"invalid_configuration", "invalid_input", "api_unavailable",
                   "api_uncertain", "api_rejected", "invalid_response", "invalid_state",
                   "state_too_large", "state_conflict"})
HEX40 = re.compile(r"[0-9a-fA-F]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class LedgerError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in CODES else "invalid_response"
        super().__init__(self.code)


def _valid_day(value):
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _key(day, friend, kind="manual"):
    suffix = "\0manual-extra-test" if kind == "manual_extra_test" else ""
    return hashlib.sha256((day + "\0" + friend.strip() + suffix).encode("utf-8")).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(_value):
    raise ValueError()


def _json(raw, code="invalid_response"):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except Exception:
        raise LedgerError(code) from None


def _validate_state(value):
    if (not isinstance(value, dict) or set(value) != {"version", "records"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("records"), dict)):
        raise LedgerError("invalid_state")
    for key, record in value["records"].items():
        if (not isinstance(key, str) or not HEX64.fullmatch(key)
                or not isinstance(record, dict)
                or set(record) != {"day", "at", "kind", "run_id", "result"}
                or not _valid_day(record.get("day"))
                or not isinstance(record.get("kind"), str) or record["kind"] not in KINDS
                or not isinstance(record.get("run_id"), str) or not RUN_ID.fullmatch(record["run_id"])
                or not isinstance(record.get("result"), str) or record["result"] not in RESULTS
                or not isinstance(record.get("at"), str) or len(record["at"]) > 64):
            raise LedgerError("invalid_state")
        try:
            timestamp = datetime.fromisoformat(record["at"])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError()
        except Exception:
            raise LedgerError("invalid_state") from None
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(method, url, headers, body):
    # A redirect must never forward the Authorization header elsewhere.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)


class Ledger:
    def __init__(self, token, repository, sha, *, api_url="https://api.github.com", transport=None):
        if (not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n")
                or not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository)
                or repository.split("/")[-1] in (".", "..")
                or not isinstance(sha, str) or not HEX40.fullmatch(sha)
                or api_url not in ("https://api.github.com", "https://api.github.com/")):
            raise LedgerError("invalid_configuration")
        self._token = token
        self._sha = sha
        self._base = "https://api.github.com/repos/" + repository
        self._transport = transport or _transport

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", ""),
                   os.environ.get("GITHUB_SHA", ""), api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"))

    def _request(self, method, path, value=None):
        body = None if value is None else json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DouyinCloudLedger/1",
                   "Content-Type": "application/json"}
        try:
            status, raw = self._transport(method, self._base + path, headers, body)
        except Exception:
            raise LedgerError("api_unavailable" if method == "GET" else "api_uncertain") from None
        if type(status) is not int or not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise LedgerError("invalid_response" if method == "GET" else "api_uncertain")
        return status, raw

    def _ref_valid(self, raw):
        value = _json(raw)
        return (isinstance(value, dict) and value.get("ref") == "refs/heads/" + BRANCH
                and isinstance(value.get("object"), dict)
                and isinstance(value["object"].get("sha"), str)
                and HEX40.fullmatch(value["object"]["sha"]) is not None)

    def _ensure_branch(self):
        path = "/git/ref/heads/" + BRANCH
        status, raw = self._request("GET", path)
        if status == 200:
            if not self._ref_valid(raw):
                raise LedgerError("invalid_response")
            return
        if status != 404:
            raise LedgerError("api_rejected")
        status, raw = self._request("POST", "/git/refs", {"ref": "refs/heads/" + BRANCH, "sha": self._sha})
        if status == 201:
            if not self._ref_valid(raw):
                raise LedgerError("api_uncertain")
            return
        if status == 422:
            status, raw = self._request("GET", path)
            if status == 200 and self._ref_valid(raw):
                return
            raise LedgerError("api_rejected")
        raise LedgerError("api_uncertain" if status >= 500 else "api_rejected")

    def _read(self):
        status, raw = self._request("GET", "/contents/" + STATE_PATH + "?ref=" + BRANCH)
        if status == 404:
            return {"version": 1, "records": {}}, None
        if status != 200:
            raise LedgerError("api_rejected")
        value = _json(raw)
        if (not isinstance(value, dict) or value.get("type") != "file" or value.get("encoding") != "base64"
                or value.get("path") != STATE_PATH or not isinstance(value.get("sha"), str)
                or not HEX40.fullmatch(value["sha"]) or not isinstance(value.get("content"), str)
                or type(value.get("size")) is not int):
            raise LedgerError("invalid_response")
        encoded = value["content"]
        if len(encoded) > MAX_BASE64_BYTES + MAX_BASE64_BYTES // 60 + 2:
            raise LedgerError("state_too_large")
        encoded = encoded.replace("\n", "").replace("\r", "")
        if len(encoded) > MAX_BASE64_BYTES or not 0 <= value["size"] <= MAX_JSON_BYTES:
            raise LedgerError("state_too_large")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise LedgerError("invalid_state") from None
        if len(decoded) > MAX_JSON_BYTES:
            raise LedgerError("state_too_large")
        if len(decoded) != value["size"]:
            raise LedgerError("invalid_state")
        return _validate_state(_json(decoded, "invalid_state")), value["sha"]

    def _write(self, state, sha):
        _validate_state(state)
        decoded = json.dumps(state, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.b64encode(decoded).decode("ascii")
        if len(decoded) > MAX_JSON_BYTES or len(encoded) > MAX_BASE64_BYTES:
            raise LedgerError("state_too_large")
        value = {"message": "Update durable Douyin daily reservation", "branch": BRANCH, "content": encoded}
        if sha is not None:
            value["sha"] = sha
        status, raw = self._request("PUT", "/contents/" + STATE_PATH, value)
        if status in (409, 422):
            return False
        if status not in (200, 201):
            raise LedgerError("api_uncertain" if status >= 500 else "api_rejected")
        try:
            response = _json(raw)
            content = response.get("content") if isinstance(response, dict) else None
            if (not isinstance(content, dict) or not isinstance(content.get("sha"), str)
                    or not HEX40.fullmatch(content["sha"])):
                raise ValueError()
        except Exception:
            raise LedgerError("api_uncertain") from None
        return True

    @staticmethod
    def _inputs(day, friend, run_id):
        if (not _valid_day(day) or not isinstance(friend, str) or not 1 <= len(friend.strip()) <= 100
                or not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
            raise LedgerError("invalid_input")

    @staticmethod
    def _record(day, kind, run_id, result):
        return {"day": day, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind, "run_id": run_id, "result": result}

    def reserve(self, day, friend, run_id, kind, imported_days=None):
        self._inputs(day, friend, run_id)
        if kind not in ("manual", "scheduled", "manual_extra_test"):
            raise LedgerError("invalid_input")
        imported_days = [] if imported_days is None else imported_days
        if (not isinstance(imported_days, list) or len(imported_days) > 4096
                or any(not _valid_day(item) for item in imported_days)):
            raise LedgerError("invalid_input")
        self._ensure_branch()
        key = _key(day, friend, kind)
        primary_key = _key(day, friend)
        for _attempt in range(3):
            state, sha = self._read()
            records = state["records"]
            changed = False
            for imported_day in sorted(set(imported_days)):
                imported_key = _key(imported_day, friend)
                if imported_key not in records:
                    records[imported_key] = self._record(imported_day, "imported_local", "local-import", "imported_local")
                    changed = True
            if kind == "manual_extra_test" and primary_key not in records:
                raise LedgerError("invalid_input")
            duplicate = key in records
            if not duplicate:
                records[key] = self._record(day, kind, run_id, "reserved_unknown")
                changed = True
            if not changed:
                return False
            if self._write(state, sha):
                return not duplicate
        raise LedgerError("state_conflict")

    def finish(self, day, friend, run_id, result, *, kind="manual"):
        self._inputs(day, friend, run_id)
        if (not isinstance(result, str) or result not in RESULTS
                or kind not in ("manual", "scheduled", "manual_extra_test")):
            raise LedgerError("invalid_input")
        self._ensure_branch()
        key = _key(day, friend, kind)
        for _attempt in range(3):
            state, sha = self._read()
            record = state["records"].get(key)
            if record is None or record["run_id"] != run_id:
                return False
            if record["result"] == result:
                return True
            if record["result"] != "reserved_unknown":
                return False
            record["result"] = result
            record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if self._write(state, sha):
                return True
        raise LedgerError("state_conflict")
