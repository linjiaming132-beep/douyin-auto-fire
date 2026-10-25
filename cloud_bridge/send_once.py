"""Explicit cloud text test with a durable pre-send reservation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_bridge.common import MAIN_SECRET, MAX_PARTS, SessionTransportError, probe, session_script, unpack_parts
from cloud_bridge.ledger import Ledger, LedgerError
from cloud_bridge.prepare_message import parse_message_config

BJ = timezone(timedelta(hours=8))
APPROVED_EXTRA_TEST_DAY = "2026-10-23"
OUTCOMES = {
    "ui_confirmed", "not_confirmed", "local_pause_not_confirmed", "wrong_trigger",
    "extra_permission_expired", "configuration_error", "preflight_failed", "target_changed",
    "draft_present", "date_changed", "already_attempted", "ledger_error", "ledger_update_failed",
    "send_retry_marker", "send_failed_or_uncertain", "timeout", "environment_error",
}


def now():
    return datetime.now(BJ)


async def editor_has_draft(editor):
    value = await editor.evaluate("""el => {
      const composer = el.closest('[class*="messageMsgInput"], [class*="MessageInput"]') || el.parentElement;
      const visible = node => {
        const r = node.getBoundingClientRect(), s = getComputedStyle(node);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      };
      return {
        text: ('value' in el ? el.value : el.innerText) || '',
        embedded_media: !!el.querySelector('img,video,audio,canvas,[contenteditable="false"]'),
        selected_file: [...document.querySelectorAll('input[type="file"]')].some(n => n.files && n.files.length),
        attachment_preview: !!composer && [...composer.querySelectorAll('[class*="preview" i] img,[class*="attachment" i] img,[class*="upload" i] img')].some(visible)
      };
    }""")
    return bool(re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", value.get("text", ""))) or any(
        value.get(key) for key in ("embedded_media", "selected_file", "attachment_preview")
    )


class MetadataOnly(logging.Filter):
    def filter(self, record):
        return isinstance(record.msg, str) and record.msg.startswith("SEND_DIAG_V1 ") and not record.exc_info and not record.exc_text and not record.stack_info


def configure_metadata():
    logger = logging.getLogger("douyin_sender")
    logger.disabled, logger.propagate = False, False
    logger.setLevel(logging.INFO)
    for previous in list(logger.handlers):
        logger.removeHandler(previous)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(MetadataOnly())
    logger.addHandler(handler)


async def ready(chat, friend):
    try:
        if await chat._chat_open_error(friend) is not None:
            return "target_changed"
        editor = await chat.message_input()
        if not await editor.is_visible() or not await editor.is_editable():
            return "target_changed"
        if await editor_has_draft(editor):
            return "draft_present"
        return None
    except Exception:
        # A failed inspection cannot establish a safe target or empty draft.
        return "target_changed"


async def attempt(page, chat, friend, config, ledger, run_id, kind, day, sender, *, progress=None, on_stage=None):
    result = {"outcome": "environment_error", "send_attempted": False, "page_send_check_passed": False}
    if now().date().isoformat() != day:
        return dict(result, outcome="date_changed")
    if on_stage is not None:
        on_stage("verify_chat")
    blocked = await ready(chat, friend)
    if blocked:
        return dict(result, outcome=blocked)
    try:
        if on_stage is not None:
            on_stage("reserve")
        reserved = await asyncio.to_thread(ledger.reserve, day, friend, run_id, kind, imported_days=config["imported_days"])
    except LedgerError as exc:
        return dict(result, outcome="ledger_error", ledger_reason=exc.code)
    if not reserved:
        return dict(result, outcome="already_attempted")
    if on_stage is not None:
        on_stage("recheck")
    # CAS/network traffic may take time. Recheck the live target, draft, and
    # Beijing day after the durable reservation and before typing anything.
    if now().date().isoformat() != day:
        return dict(result, outcome="date_changed")
    blocked = await ready(chat, friend)
    if blocked:
        return dict(result, outcome=blocked)
    configure_metadata()
    if on_stage is not None:
        on_stage("send")
    result["send_attempted"] = True
    if progress is not None:
        progress["send_attempted"] = True
    try:
        await sender(page, chat, config["text"])
        result.update(outcome="ui_confirmed", page_send_check_passed=True)
        if progress is not None:
            progress["page_send_check_passed"] = True
        terminal = "ui_confirmed"
    except Exception as exc:
        terminal = "send_retry_marker" if "页面提示可以重试" in str(exc) else "send_failed_or_uncertain"
        result["outcome"] = terminal
    try:
        if on_stage is not None:
            on_stage("save_result")
        recorded = await asyncio.to_thread(ledger.finish, day, friend, run_id, terminal, kind=kind)
    except LedgerError as exc:
        return dict(result, outcome="ledger_update_failed", ledger_reason=exc.code)
    if not recorded:
        return dict(result, outcome="ledger_update_failed")
    if on_stage is not None:
        on_stage("done")
    return result


async def execute(bundle, config, ledger, run_id, kind, day, *, progress=None):
    from playwright.async_api import async_playwright
    # common.probe puts the vendored runtime first on sys.path before these
    # imports. No local persistent-profile or interactive menu is used here.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=False)
        try:
            context = await browser.new_context(storage_state=bundle["storage_state"],
                                                viewport={"width": 1440, "height": 1000}, locale="zh-CN")
            await context.add_init_script(script=session_script(bundle))
            checked = await probe(context, bundle["friend"])
            if not checked.get("ok"):
                return {"outcome": "preflight_failed", "send_attempted": False, "page_send_check_passed": False}
            from app.douyin import DouyinChat
            from app.models import Message
            from app.sender import send_message
            page = context.pages[0]
            chat = DouyinChat(page, timeout_ms=20_000)

            async def send_text_once(page, chat, content):
                await send_message(page, chat, Message(type="text", content=content), {})

            return await attempt(page, chat, bundle["friend"], config, ledger, run_id, kind, day, send_text_once, progress=progress)
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=15)
            except Exception:
                pass


def report(result):
    outcome = result.get("outcome")
    safe = {"outcome": outcome if outcome in OUTCOMES else "environment_error",
            "send_attempted": result.get("send_attempted") is True,
            "page_send_check_passed": result.get("page_send_check_passed") is True}
    if result.get("configuration_reason") in {"session_invalid", "message_invalid"}:
        safe["configuration_reason"] = result["configuration_reason"]
    reason = result.get("ledger_reason")
    if reason in {"invalid_configuration", "invalid_input", "api_unavailable", "api_uncertain", "api_rejected", "invalid_response", "invalid_state", "state_too_large", "state_conflict"}:
        safe["ledger_reason"] = reason
    print("CLOUD_SEND_V1 " + json.dumps(safe, ensure_ascii=True))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        labels = {
            "ui_confirmed": "页面发送检查通过，请在抖音确认好友实际收到。",
            "not_confirmed": "尚未勾选真实发送确认，未打开浏览器。",
            "local_pause_not_confirmed": "请先在本机面板暂停并保存，再勾选已暂停本机定时。",
            "wrong_trigger": "本入口仅支持手动测试。",
            "extra_permission_expired": "额外测试授权仅适用于2026年10月23日。其他日期请取消额外测试选项，按每日一次检查。",
            "configuration_error": "会话或发送文字配置不完整。请准备 DOUYIN_CLOUD_MESSAGE，并保留原来的会话分片。",
            "preflight_failed": "本次云端登录或好友定位检查未通过，未进入发送流程。",
            "target_changed": "当前聊天或编辑框无法再次确认，已停止。",
            "draft_present": "当前输入框中存在草稿或附件，已停止。",
            "date_changed": "北京时间日期已变化，已停止旧任务。",
            "already_attempted": "该日已经尝试过，本次没有重复发送。",
            "ledger_error": "远端发送预留未确认成功，未进入发送流程。请检查工作流 contents:write 权限及仓库规则。",
            "ledger_update_failed": "发送后的记录回写未确认成功，原预留保持有效，不会自动重发。",
            "send_retry_marker": "抖音页面提示发送失败或重试，本次不自动重发。",
            "send_failed_or_uncertain": "发送失败或结果无法确认，本次不自动重发。",
            "timeout": "执行超时。如已进入发送流程，预留会保留，不会自动重发。",
            "environment_error": "运行环境未能完成任务，请查看脱敏日志。",
        }
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write("### 抖音云端单次发送测试\n\n" + labels[safe["outcome"]] + "\n\n")
            stream.write("进入发送流程：**" + ("是" if safe["send_attempted"] else "否") + "**；页面发送检查通过：**" + ("是" if safe["page_send_check_passed"] else "否") + "**。\n\n")
            stream.write("本入口未配置定时；发送记录保存在 douyin-send-state 分支。重复运行不会清除预留或重新发送。\n")


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        report({"outcome": "wrong_trigger"})
        return 2
    if os.environ.get("CONFIRM_SEND") != "true":
        report({"outcome": "not_confirmed"})
        return 2
    if os.environ.get("LOCAL_TIMER_PAUSED") != "true":
        report({"outcome": "local_pause_not_confirmed"})
        return 2
    day = now().date().isoformat()
    extra = os.environ.get("ALLOW_EXTRA_TEST") == "true"
    if extra and day != APPROVED_EXTRA_TEST_DAY:
        report({"outcome": "extra_permission_expired"})
        return 2
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
    kind = "manual_extra_test" if extra else "manual"
    progress = {"send_attempted": False, "page_send_check_passed": False}
    try:
        result = asyncio.run(asyncio.wait_for(execute(bundle, config, ledger, os.environ.get("GITHUB_RUN_ID", ""), kind, day, progress=progress), timeout=180))
    except asyncio.TimeoutError:
        result = dict(progress, outcome="timeout")
    except Exception:
        result = dict(progress, outcome="environment_error")
    report(result)
    return 0 if result.get("outcome") in {"ui_confirmed", "already_attempted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
