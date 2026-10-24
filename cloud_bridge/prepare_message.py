"""Prepare one message Secret from paused local settings; never send or copy it."""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_bridge import common

BJ = timezone(timedelta(hours=8), "Asia/Shanghai")
MESSAGE_NAME = "DOUYIN_CLOUD_MESSAGE"
MAX_JSON_BYTES = 16_000


class MessagePreparationError(ValueError):
    """Expose a fixed stage only, without message or friend details."""


def _json_loads(value):
    def invalid_constant(_value):
        raise ValueError
    return json.loads(value, parse_constant=invalid_constant)


def _iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def parse_message_config(value, friend=None):
    """Validate a Secret without importing browser or local-check modules."""
    try:
        if isinstance(value, str):
            if len(value.encode("utf-8")) > MAX_JSON_BYTES:
                raise ValueError
            value = _json_loads(value)
        if not isinstance(value, dict) or set(value) != {"version", "text", "friend_sha256", "imported_days"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError
        text, digest, days = value["text"], value["friend_sha256"], value["imported_days"]
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or "\0" in text:
            raise ValueError
        text.encode("utf-8")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError
        digest = digest.lower()
        if not isinstance(days, list) or len(days) > 7:
            raise ValueError
        parsed_days = [_iso_day(item).isoformat() for item in days]
        if len(set(parsed_days)) != len(parsed_days):
            raise ValueError
        if friend is not None:
            if not isinstance(friend, str) or not 1 <= len(friend.strip()) <= 100:
                raise ValueError
            expected = hashlib.sha256(friend.strip().encode("utf-8")).hexdigest()
            if not hmac.compare_digest(digest, expected):
                raise ValueError
        return {"version": 1, "text": text, "friend_sha256": digest, "imported_days": sorted(parsed_days)}
    except Exception:
        raise MessagePreparationError("message_config") from None


def _read_json(path, limit):
    raw = Path(path).read_bytes()
    if len(raw) > limit:
        raise ValueError
    return _json_loads(raw.decode("utf-8-sig"))


def _read_panel(root):
    try:
        panel = _read_json(root / "data" / "panel-config.json", MAX_JSON_BYTES)
        if not isinstance(panel, dict) or panel.get("enabled") is not False:
            raise MessagePreparationError("pause_required")
        friend = panel.get("friend")
        if not isinstance(friend, str) or not 1 <= len(friend.strip()) <= 100:
            raise ValueError
        friend = friend.strip()
        message = parse_message_config({"version": 1, "text": panel.get("message"),
            "friend_sha256": hashlib.sha256(friend.encode("utf-8")).hexdigest(), "imported_days": []}, friend)
        return friend, message
    except MessagePreparationError as exc:
        if str(exc) == "pause_required":
            raise
        raise MessagePreparationError("settings") from None
    except Exception:
        raise MessagePreparationError("settings") from None


def _read_exported_friend(root):
    folder = root / "data" / "cloud-export"
    secrets = {}
    try:
        names = [common.MAIN_SECRET] + [f"{common.MAIN_SECRET}_{index:02d}" for index in range(1, 17)]
        for name in names:
            path = folder / (name + ".txt")
            if path.exists():
                raw = path.read_bytes()
                if len(raw) > common.MAX_SECRET_BYTES + 2:
                    raise ValueError
                secrets[name] = raw.decode("ascii").strip()
        return common.unpack_parts(secrets)["friend"].strip()
    except Exception:
        raise MessagePreparationError("session") from None


def _import_history(root, friend, current_day):
    path = root / "data" / "panel-send-history.json"
    try:
        history = _read_json(path, 2 * 1024 * 1024) if path.exists() else {}
        if not isinstance(history, dict):
            raise ValueError
        imported = set()
        earliest = current_day - timedelta(days=6)
        for key, record in history.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key) or not isinstance(record, dict):
                raise ValueError
            day = _iso_day(record.get("day"))
            if day > current_day:
                raise ValueError
            expected = hashlib.sha256((day.isoformat() + "\0" + friend.strip()).encode("utf-8")).hexdigest()
            if day >= earliest and hmac.compare_digest(key.lower(), expected):
                imported.add(day.isoformat())
        return sorted(imported)
    except Exception:
        raise MessagePreparationError("history") from None


def _atomic_write(output, message):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=output.parent,
                                         prefix=".cloud-message-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(message, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_message(root=ROOT, *, local_module=None, current=None):
    """Return the owned Secret path only after all settings and history pass."""
    root = Path(root)
    output = root / "data" / "cloud-export" / (MESSAGE_NAME + ".txt")
    if local_module is None:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        local_module = importlib.import_module("local_check")
    lock = local_module.single_instance()
    try:
        lock.__enter__()
    except RuntimeError:
        raise MessagePreparationError("profile_busy") from None
    try:
        try:
            friend, message = _read_panel(root)
            if friend != _read_exported_friend(root):
                raise MessagePreparationError("friend_mismatch")
            instant = current if current is not None else datetime.now(BJ)
            if not isinstance(instant, datetime) or instant.tzinfo is None:
                raise MessagePreparationError("clock")
            message["imported_days"] = _import_history(root, friend, instant.astimezone(BJ).date())
            message = parse_message_config(message, friend)
            _atomic_write(output, message)
        except BaseException:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                raise MessagePreparationError("output_cleanup") from None
            raise
    finally:
        lock.__exit__(None, None, None)
    return output


MESSAGES = {
    "pause_required": "请先在本机面板暂停定时并保存，再运行本工具。",
    "settings": "本机面板的好友或发送文字无效，请先在面板保存设置。",
    "session": "没有找到完整且校验通过的会话导出，请先运行 cloud-export.cmd。",
    "friend_mismatch": "面板好友与已导出的云端会话不一致，请先重新导出并完成云端会话检查。",
    "history": "本机发送记录异常，已停止准备消息，避免重复发送。",
    "profile_busy": "原验证工具或发送任务正在使用浏览器，请正常关闭后再试；本次未更新消息 Secret。",
    "clock": "系统日期无法校验，未生成消息 Secret。",
    "output_cleanup": "无法移除上一次消息文件，请关闭该文件后重试。",
}


def main():
    print("本工具只读取已保存的文字和发送记录；不会打开浏览器、发送消息或自动复制内容。")
    try:
        output = prepare_message()
        prepared = parse_message_config(output.read_text(encoding="utf-8"))
    except MessagePreparationError as exc:
        print(MESSAGES.get(str(exc), "消息 Secret 未准备完成，请检查本机设置。"))
        return 1
    except KeyboardInterrupt:
        print("已取消，本次未完成消息 Secret 准备。")
        return 130
    except Exception:
        print("消息 Secret 未准备完成，请检查本机工具环境。")
        return 1
    print("消息Secret已准备好")
    if datetime.now(BJ).date().isoformat() in prepared["imported_days"]:
        print("本机今天已有发送或尝试记录，GitHub 当天不会重复发送。")
    print("文件只保存在本机，请用后续复制窗口将内容保存为 GitHub Secret；不要提交进仓库或发到聊天。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
