"""Daily GitHub runner reusing the verified sender and durable daily ledger."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_bridge import send_once as core
from cloud_bridge.common import MAIN_SECRET, MAX_PARTS, SessionTransportError, unpack_parts
from cloud_bridge.ledger import Ledger, LedgerError
from cloud_bridge.prepare_message import parse_message_config

MAX_DELAY_SECONDS = 2 * 60 * 60
OUTCOMES = core.OUTCOMES | {"disabled", "schedule_invalid", "outside_window"}


def now():
    return datetime.now(core.BJ)


class ScheduleError(ValueError):
    pass


def schedule_window(instant):
    """Accept the configured UTC daily cron within its Beijing-day window."""
    text = os.environ.get("DAILY_TIME", "")
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", text):
        raise ScheduleError("schedule_invalid")
    hour, minute = map(int, text.split(":"))
    cron = f"{minute} {(hour - 8) % 24} * * *"
    if os.environ.get("DAILY_CRON") != cron or os.environ.get("TRIGGER_CRON") != cron:
        raise ScheduleError("schedule_invalid")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ScheduleError("schedule_invalid")
    local = instant.astimezone(core.BJ)
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    elapsed = (local - due).total_seconds()
    if elapsed < 0 or elapsed >= MAX_DELAY_SECONDS:
        raise ScheduleError("outside_window")
    midnight = (due + timedelta(days=1)).replace(hour=0, minute=0)
    remaining = min(MAX_DELAY_SECONDS - elapsed, (midnight - local).total_seconds())
    return local.date().isoformat(), remaining


def report(result):
    outcome = result.get("outcome")
    safe = {"outcome": outcome if outcome in OUTCOMES else "environment_error",
            "send_attempted": result.get("send_attempted") is True,
            "page_send_check_passed": result.get("page_send_check_passed") is True}
    if result.get("configuration_reason") in {"session_invalid", "message_invalid"}:
        safe["configuration_reason"] = result["configuration_reason"]
    reason = result.get("ledger_reason")
    if reason in {"invalid_configuration", "invalid_input", "api_unavailable", "api_uncertain",
                  "api_rejected", "invalid_response", "invalid_state", "state_too_large", "state_conflict"}:
        safe["ledger_reason"] = reason
    print("CLOUD_DAILY_V1 " + json.dumps(safe, ensure_ascii=True))
    labels = {
        "ui_confirmed": "页面发送检查通过。",
        "already_attempted": "今天已尝试过向该好友发送，本次已跳过。",
        "disabled": "每日发送未启用，未进入发送流程。",
        "wrong_trigger": "每日入口仅接受 GitHub 定时触发，未进入发送流程。",
        "schedule_invalid": "每日时间或定时触发配置不一致，未进入发送流程。",
        "outside_window": "当前不在当天的发送时间窗口内，已跳过；不会跨日补发。",
        "configuration_error": "会话或发送文字配置无效，请检查原有 Secrets。",
        "preflight_failed": "本次登录或好友定位检查未通过，未进入发送流程。",
        "target_changed": "当前聊天或编辑框无法再次确认，已停止。",
        "draft_present": "输入框存在草稿或附件，已停止。",
        "date_changed": "北京时间日期已变化，已停止旧任务。",
        "ledger_error": "远端发送预留未确认成功，未进入发送流程。",
        "ledger_update_failed": "发送记录回写未确认成功，原预留保持有效，不会自动重发。",
        "send_retry_marker": "抖音页面提示发送失败或重试，本次不自动重发。",
        "send_failed_or_uncertain": "发送失败或结果无法确认，本次不自动重发。",
        "timeout": "已到执行时限。如已进入发送流程，预留会保留，不会自动重发。",
        "environment_error": "运行环境未能完成任务，请查看脱敏日志。",
    }
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("### 抖音云端每日发送\n\n" + labels.get(safe["outcome"], labels["environment_error"]) + "\n\n")
            stream.write("进入发送流程：**" + ("是" if safe["send_attempted"] else "否")
                         + "**；页面发送检查通过：**" + ("是" if safe["page_send_check_passed"] else "否") + "**。\n\n")
            stream.write("每日与普通手动测试共用 douyin-send-state 分支记录。同一好友每天最多一次尝试；失败或结果不明不会自动重发。\n")


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        report({"outcome": "wrong_trigger"})
        return 2
    if os.environ.get("CLOUD_DAILY_ENABLED") != "true":
        report({"outcome": "disabled"})
        return 0
    try:
        day, remaining = schedule_window(now())
    except ScheduleError as exc:
        code = str(exc)
        report({"outcome": code})
        return 0 if code == "outside_window" else 2
    secrets = {MAIN_SECRET: os.environ.pop(MAIN_SECRET, "").strip()}
    for index in range(1, MAX_PARTS + 1):
        key = f"{MAIN_SECRET}_{index:02d}"
        secrets[key] = os.environ.pop(key, "").strip()
    try:
        bundle = unpack_parts(secrets)
    except SessionTransportError:
        report({"outcome": "configuration_error", "configuration_reason": "session_invalid"})
        return 2
    finally:
        del secrets
    try:
        config = parse_message_config(os.environ.pop("DOUYIN_CLOUD_MESSAGE", ""), friend=bundle["friend"])
    except ValueError:
        report({"outcome": "configuration_error", "configuration_reason": "message_invalid"})
        return 2
    try:
        ledger = Ledger.from_environment()
    except LedgerError as exc:
        report({"outcome": "ledger_error", "ledger_reason": exc.code})
        return 2
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
    try:
        current_day, remaining = schedule_window(now())
        if current_day != day:
            raise ScheduleError("outside_window")
    except ScheduleError as exc:
        code = str(exc)
        report({"outcome": code})
        return 0 if code == "outside_window" else 2
    progress = {"send_attempted": False, "page_send_check_passed": False}
    try:
        result = asyncio.run(asyncio.wait_for(
            core.execute(bundle, config, ledger, os.environ.get("GITHUB_RUN_ID", ""),
                         "scheduled", day, progress=progress), timeout=min(180, remaining)))
    except asyncio.TimeoutError:
        result = dict(progress, outcome="timeout")
    except Exception:
        result = dict(progress, outcome="environment_error")
    report(result)
    return 0 if result.get("outcome") in {"ui_confirmed", "already_attempted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
