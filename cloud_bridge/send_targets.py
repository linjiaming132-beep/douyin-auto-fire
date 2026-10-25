"""Explicit friend-list checks and sends with one isolated context per recipient."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_bridge import send_daily, send_once as core
from cloud_bridge.common import MAIN_SECRET, MAX_PARTS, SessionTransportError, probe, session_script, unpack_parts
from cloud_bridge.ledger import Ledger, LedgerError
from cloud_bridge.targets import TARGETS_SECRET, parse_targets

INTER_TARGET_DELAY = 2
ITEM_TIMEOUT_SECONDS = 180
CLOSE_TIMEOUT_SECONDS = 15
MAX_BATCH_SECONDS = 70 * 60
PHASES = {"pending", "context", "restore", "navigation", "search", "target", "editor", "ready",
          "verify_chat", "reserve", "recheck", "send", "save_result", "done"}
ITEM_CODES = core.OUTCOMES | {"checked", "outside_window", "batch_timeout"}
SUCCESS_CODES = {"checked", "ui_confirmed", "already_attempted"}
OVERALL_CODES = {"batch_finished", "wrong_trigger", "disabled", "not_confirmed", "local_pause_not_confirmed",
                 "schedule_invalid", "outside_window", "configuration_error", "ledger_error", "environment_error"}


def now():
    return datetime.now(core.BJ)


def batch_deadline(instant, count):
    """An accepted batch gets its own bounded budget, capped at Beijing midnight."""
    if instant.tzinfo is None or instant.utcoffset() is None or type(count) is not int or not 1 <= count <= 20:
        raise ValueError("invalid_batch_budget")
    local = instant.astimezone(core.BJ)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = min(MAX_BATCH_SECONDS, 60 + count * (ITEM_TIMEOUT_SECONDS + CLOSE_TIMEOUT_SECONDS + INTER_TARGET_DELAY))
    return min(midnight, local + timedelta(seconds=seconds))


def phase(entry, stage):
    if stage not in PHASES:
        return
    entry["stage"] = stage
    index = entry.get("index")
    index = index if type(index) is int and 1 <= index <= 20 else 0
    print("CLOUD_STAGE_V1 " + json.dumps({"index": index, "stage": stage,
          "at_bj": datetime.now(core.BJ).isoformat(timespec="seconds")}, ensure_ascii=True))


def safe_timing(timing):
    result = {}
    for key in ("started", "accepted_until", "deadline"):
        value = timing.get(key) if isinstance(timing, dict) else None
        if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
            result[key + "_bj"] = value.astimezone(core.BJ).isoformat(timespec="seconds")
    return result


def item(index, outcome="environment_error", **values):
    return {"index": index, "outcome": outcome, "send_attempted": values.get("send_attempted") is True,
            "page_send_check_passed": values.get("page_send_check_passed") is True}


async def one_target(browser, bundle, target, ledger, run_id, kind, day, *, dry_run, progress, resources):
    phase(progress, "context")
    context = await browser.new_context(storage_state=bundle["storage_state"],
                                        viewport={"width": 1440, "height": 1000}, locale="zh-CN")
    resources["context"] = context
    phase(progress, "restore")
    await context.add_init_script(script=session_script(bundle))
    checked = await probe(context, target["friend"], on_stage=lambda stage: phase(progress, stage))
    if not checked.get("ok"):
        return {"outcome": "preflight_failed"}
    from app.douyin import DouyinChat
    from app.models import Message
    from app.sender import send_message
    page = context.pages[0]
    chat = DouyinChat(page, timeout_ms=20_000)
    if dry_run:
        blocked = await core.ready(chat, target["friend"])
        return {"outcome": blocked or "checked"}

    async def send_text_once(page, chat, text):
        await send_message(page, chat, Message(type="text", content=text), {})

    return await core.attempt(page, chat, target["friend"], target, ledger, run_id, kind, day,
                              send_text_once, progress=progress, on_stage=lambda stage: phase(progress, stage))


async def run_targets(bundle, config, ledger, run_id, kind, day, *, dry_run, deadline, worker, progress=None):
    """The worker handles one independent browser context; no retry is issued."""
    progress = [] if progress is None else progress
    for index, target in enumerate(config["targets"], 1):
        entry = item(index)
        entry["stage"] = "pending"
        progress.append(entry)
        instant = now()
        blocked = "date_changed" if instant.date().isoformat() != day else None
        remaining = (deadline - instant).total_seconds()
        if blocked or remaining <= 0:
            entry["outcome"] = blocked or "batch_timeout"
            for later in range(index + 1, len(config["targets"]) + 1):
                progress.append(item(later, entry["outcome"]))
            break
        try:
            result = await asyncio.wait_for(
                worker(bundle, target, ledger, run_id, kind, day, dry_run=dry_run, progress=entry),
                timeout=min(ITEM_TIMEOUT_SECONDS, remaining))
            outcome = result.get("outcome")
            entry["outcome"] = outcome if outcome in ITEM_CODES else "environment_error"
            for flag in ("send_attempted", "page_send_check_passed"):
                entry[flag] = entry[flag] or result.get(flag) is True
        except asyncio.TimeoutError:
            entry["outcome"] = "timeout"
            entry["timeout_source"] = "batch_limit" if remaining < ITEM_TIMEOUT_SECONDS else "item_limit"
        except Exception:
            entry["outcome"] = "environment_error"
        if entry["outcome"] == "date_changed":
            for later in range(index + 1, len(config["targets"]) + 1):
                progress.append(item(later, "date_changed"))
            break
        if index < len(config["targets"]):
            await asyncio.sleep(INTER_TARGET_DELAY)
    return progress


async def execute(bundle, config, ledger, run_id, kind, day, *, dry_run, deadline, progress=None,
                  playwright_factory=None):
    if playwright_factory is None:
        from playwright.async_api import async_playwright
        playwright_factory = async_playwright
    async with playwright_factory() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=False)
        try:
            async def worker(bundle, target, ledger, run_id, kind, day, *, dry_run, progress):
                resources = {}
                try:
                    return await one_target(browser, bundle, target, ledger, run_id, kind, day,
                                            dry_run=dry_run, progress=progress, resources=resources)
                finally:
                    context = resources.pop("context", None)
                    if context is not None:
                        try:
                            await asyncio.wait_for(context.close(), timeout=CLOSE_TIMEOUT_SECONDS)
                        except Exception:
                            pass

            return await run_targets(bundle, config, ledger, run_id, kind, day, dry_run=dry_run,
                                     deadline=deadline, worker=worker, progress=progress)
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass


def report(outcome, entries, *, dry_run=True, timing=None):
    safe_outcome = outcome if outcome in OVERALL_CODES else "environment_error"
    safe_entries = []
    for index, entry in enumerate(entries, 1):
        code = entry.get("outcome")
        safe_entry = item(index, code if code in ITEM_CODES else "environment_error", **{
            "send_attempted": entry.get("send_attempted"),
            "page_send_check_passed": entry.get("page_send_check_passed")})
        if "stage" in entry:
            safe_entry["stage"] = entry["stage"] if entry["stage"] in PHASES else "pending"
        if entry.get("timeout_source") in {"item_limit", "batch_limit"}:
            safe_entry["timeout_source"] = entry["timeout_source"]
        safe_entries.append(safe_entry)
    counts = {"total": len(safe_entries), "checked": sum(e["outcome"] == "checked" for e in safe_entries),
              "page_confirmed": sum(e["page_send_check_passed"] for e in safe_entries),
              "skipped": sum(e["outcome"] == "already_attempted" for e in safe_entries),
              "failed": sum(e["outcome"] not in SUCCESS_CODES for e in safe_entries)}
    result = {"outcome": safe_outcome, "mode": "check" if dry_run else "send", "counts": counts, "targets": safe_entries}
    timing_values = safe_timing(timing)
    if timing_values:
        result["timing"] = timing_values
    print("CLOUD_TARGETS_V1 " + json.dumps(result, ensure_ascii=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    labels = {"batch_finished": "名单处理已结束。", "wrong_trigger": "仅接受定时或手动触发。",
              "disabled": "每日发送未启用。", "not_confirmed": "未确认真实发送，已停止。",
              "local_pause_not_confirmed": "请先暂停本机定时，再确认已暂停。",
              "schedule_invalid": "定时配置不一致，已停止。", "outside_window": "当前已超出当天发送窗口，已跳过。",
              "configuration_error": "会话或名单配置无效，或名单与会话不匹配。请重新生成名单 Secret。",
              "ledger_error": "无法初始化远端发送记录，未进入发送流程。",
              "environment_error": "运行环境未完成全部任务，请查看每项结果。"}
    item_labels = {"checked": "检查通过，未发送", "ui_confirmed": "页面确认发送", "already_attempted": "今天已尝试，跳过",
                   "preflight_failed": "登录或好友检查未通过", "target_changed": "无法确认当前好友或输入框",
                   "draft_present": "存在草稿，已停止", "date_changed": "日期变化，已停止",
                   "outside_window": "超出允许开始时段，跳过", "batch_timeout": "批次执行时限已到，跳过",
                   "ledger_error": "发送预留未确认",
                   "ledger_update_failed": "记录回写未确认，保留预留", "send_retry_marker": "页面提示失败，不重试",
                   "send_failed_or_uncertain": "失败或结果不明，不重试", "timeout": "超时，不重试",
                   "environment_error": "运行未完成"}
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write("### 抖音多好友任务\n\n" + labels[safe_outcome] + "\n\n")
        stream.write("模式：**" + ("只检查，不发送" if dry_run else "真实发送") + "**。编号与设置窗口中的名单顺序一致。\n\n")
        for key, label in (("started_bj", "程序开始时间"), ("accepted_until_bj", "最晚接受新批次"), ("deadline_bj", "本批执行截止")):
            if key in timing_values:
                stream.write(label + "（北京时间）：" + timing_values[key] + "。\n\n")
        phase_labels = {"pending": "未开始", "context": "创建浏览器页面", "restore": "恢复登录状态",
                        "navigation": "打开私信页", "search": "等待好友搜索", "target": "定位好友",
                        "editor": "检查输入框", "ready": "页面就绪", "verify_chat": "确认当前聊天",
                        "reserve": "写入发送预留", "recheck": "发送前复查", "send": "发送消息",
                        "save_result": "保存发送结果", "done": "完成"}
        stream.write("| 编号 | 结果 | 最后阶段 | 进入发送流程 | 页面发送检查 |\n| --- | --- | --- | --- | --- |\n")
        for entry in safe_entries:
            label = item_labels.get(entry["outcome"], "任务停止")
            if entry.get("timeout_source") == "item_limit":
                label += "（单人执行时限）"
            elif entry.get("timeout_source") == "batch_limit":
                label += "（批次执行时限）"
            stream.write(f"| {entry['index']} | {label} | " + phase_labels.get(entry.get("stage"), "未开始") + " | "
                         + ("是" if entry["send_attempted"] else "否") + " | "
                         + ("通过" if entry["page_send_check_passed"] else "未通过或未发送") + " |\n")
        stream.write("\n每位好友与原单好友任务共用每日记录。失败或结果不明不自动重发。发送预留阶段超时也可能已有记录，不能仅凭尚未进入发送流程判断可重发。请在抖音确认实际收到；只检查不会预留发送次数。\n")


def main():
    event = os.environ.get("GITHUB_EVENT_NAME")
    dry_run = event != "schedule" and os.environ.get("REQUESTED_DRY_RUN") != "false"
    if event not in {"schedule", "workflow_dispatch"}:
        report("wrong_trigger", [], dry_run=dry_run)
        return 2
    if event == "schedule":
        if os.environ.get("CLOUD_DAILY_ENABLED") != "true":
            report("disabled", [], dry_run=False)
            return 0
        instant = now()
        try:
            day, remaining = send_daily.schedule_window(instant)
        except send_daily.ScheduleError as error:
            code = str(error)
            report(code, [], dry_run=False)
            return 0 if code == "outside_window" else 2
        deadline = instant + timedelta(seconds=remaining)
        kind = "scheduled"
    else:
        if not dry_run and os.environ.get("CONFIRM_SEND") != "true":
            report("not_confirmed", [], dry_run=False)
            return 2
        if not dry_run and os.environ.get("LOCAL_TIMER_PAUSED") != "true":
            report("local_pause_not_confirmed", [], dry_run=False)
            return 2
        instant = now()
        day = instant.date().isoformat()
        deadline = (instant + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        kind = "manual"
    values = {MAIN_SECRET: os.environ.pop(MAIN_SECRET, "").strip()}
    for index in range(1, MAX_PARTS + 1):
        key = f"{MAIN_SECRET}_{index:02d}"
        values[key] = os.environ.pop(key, "").strip()
    raw_targets = os.environ.pop(TARGETS_SECRET, "")
    os.environ.pop("DOUYIN_CLOUD_MESSAGE", None)
    try:
        bundle = unpack_parts(values)
        config = parse_targets(raw_targets, bundle=bundle)
    except (SessionTransportError, ValueError):
        report("configuration_error", [], dry_run=dry_run)
        return 2
    finally:
        del values, raw_targets
    if event == "schedule":
        accepted = now()
        try:
            accepted_day, remaining = send_daily.schedule_window(accepted)
            if accepted_day != day:
                raise send_daily.ScheduleError("outside_window")
        except send_daily.ScheduleError as error:
            code = str(error)
            report(code, [], dry_run=False)
            return 0 if code == "outside_window" else 2
        deadline = batch_deadline(accepted, len(config["targets"]))
        timing = {"started": instant, "accepted_until": accepted + timedelta(seconds=remaining), "deadline": deadline}
    else:
        timing = {"started": instant, "deadline": deadline}
    print("CLOUD_TIMING_V1 " + json.dumps(safe_timing(timing), ensure_ascii=True))
    ledger = None
    try:
        if not dry_run:
            ledger = Ledger.from_environment()
    except LedgerError:
        report("ledger_error", [], dry_run=dry_run)
        return 2
    finally:
        os.environ.pop("GITHUB_TOKEN", None)
    progress = []
    outcome = "batch_finished"
    try:
        asyncio.run(execute(bundle, config, ledger, os.environ.get("GITHUB_RUN_ID", ""), kind, day,
                            dry_run=dry_run, deadline=deadline, progress=progress))
    except Exception:
        outcome = "environment_error"
        for index in range(len(progress) + 1, len(config["targets"]) + 1):
            progress.append(item(index))
    report(outcome, progress, dry_run=dry_run, timing=timing)
    return 0 if outcome == "batch_finished" and all(entry["outcome"] in SUCCESS_CODES for entry in progress) else 1


if __name__ == "__main__":
    raise SystemExit(main())
