"""GitHub-hosted read-only check; no send option and no sender dependency."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_bridge.common import (MAIN_SECRET, MAX_PARTS, TRANSPORT_CODES,
                                 SessionTransportError, parts_info, probe,
                                 session_script, unpack_parts)


async def check(bundle):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=False)
        try:
            context = await browser.new_context(
                storage_state=bundle["storage_state"], viewport={"width": 1440, "height": 1000}, locale="zh-CN",
            )
            await context.add_init_script(script=session_script(bundle))
            return await probe(context, bundle["friend"])
        finally:
            try:
                await asyncio.wait_for(browser.close(), timeout=15)
            except Exception:
                pass


def report(result):
    stages = {"ready", "configuration", "environment", "timeout", "navigation", "search", "target", "editor"}
    stage = result.get("stage")
    safe = {"ok": result.get("ok") is True,
            "stage": stage if isinstance(stage, str) and stage in stages else "environment"}
    for key in ("search_visible", "target_open", "editor_visible", "login_text_visible", "verification_text_visible"):
        if type(result.get(key)) is bool:
            safe[key] = result[key]
    reason = result.get("configuration_reason")
    if isinstance(reason, str) and (reason in TRANSPORT_CODES or reason == "ok"):
        safe["configuration_reason"] = reason
    for key in ("parts_expected", "parts_present"):
        value = result.get(key)
        if type(value) is int and 0 <= value <= MAX_PARTS:
            safe[key] = value
    safe["real_messages_sent"] = 0
    print("CLOUD_PREFLIGHT_V1 " + json.dumps(safe, ensure_ascii=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        titles = {"ready": "云端登录和好友定位检查通过；尚未验证发送", "configuration": "会话 Secret 配置未通过检查；未启动浏览器",
                  "environment": "浏览器或运行环境未就绪", "timeout": "检查超时", "navigation": "私信页面未正常打开",
                  "search": "未检测到私信搜索框", "target": "无法确认准确好友聊天已打开", "editor": "未检测到可编辑消息框"}
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("### 抖音云端会话检查\n\n" + titles.get(safe.get("stage"), "检查未完成") + "\n\n")
            reasons = {
                "missing_session": "尚未配置主 Secret DOUYIN_CLOUD_SESSION。",
                "invalid_manifest": "主 Secret 清单格式不正确，请使用本次导出的完整清单。",
                "missing_part": "缺少清单要求的分片，请补齐本次导出的所有分片。",
                "unexpected_part": "旧版单文件 Secret 与分片配置混用，请统一使用本次导出文件。",
                "invalid_part": "分片格式不正确，请完整复制对应文件。",
                "length_mismatch": "分片不完整，请重新复制本次导出的分片。",
                "checksum_mismatch": "分片顺序错误或来自不同次导出，请统一使用同一次导出文件。",
                "invalid_base64": "会话编码无效，请完整复制对应文件。",
                "invalid_gzip": "会话压缩内容无效，请重新导出。",
                "invalid_json": "会话数据格式无效，请重新导出。",
                "invalid_bundle": "会话结构不符合要求，请重新导出；这不是容量错误。",
                "bundle_too_large": "会话原始数据超过 32 MiB 上限，未继续恢复。",
                "encoded_too_large": "会话超过 16 个分片的容量上限，未继续恢复。",
                "single_secret_too_large": "旧版单文件超过容量，请改用新版分片导出。",
            }
            if safe.get("configuration_reason") in reasons:
                stream.write(reasons[safe["configuration_reason"]] + "\n\n")
            if "parts_expected" in safe and "parts_present" in safe:
                stream.write("所需分片：**" + str(safe["parts_expected"]) + "**；已配置：**" + str(safe["parts_present"]) + "**。\n\n")
            for key in ("search_visible", "target_open", "editor_visible", "login_text_visible", "verification_text_visible"):
                if key in safe:
                    stream.write("- `" + key + "`: `" + str(safe[key]).lower() + "`\n")
            stream.write("\n本次真实发送：**0 条**。验证字样不等于已发生拦截；预检通过也不代表发送已成功。\n")


def main():
    secrets = {MAIN_SECRET: os.environ.pop(MAIN_SECRET, "").strip()}
    for index in range(1, MAX_PARTS + 1):
        name = f"{MAIN_SECRET}_{index:02d}"
        secrets[name] = os.environ.pop(name, "").strip()
    try:
        bundle = unpack_parts(secrets)
    except SessionTransportError as exc:
        report({"ok": False, "stage": "configuration", "configuration_reason": exc.code,
                "parts_expected": exc.parts_expected, "parts_present": exc.parts_present})
        return 2
    expected, present = parts_info(secrets)
    del secrets
    try:
        result = asyncio.run(asyncio.wait_for(check(bundle), timeout=180))
    except asyncio.TimeoutError:
        result = {"ok": False, "stage": "timeout"}
    except Exception:
        result = {"ok": False, "stage": "environment"}
    result.update(configuration_reason="ok", parts_expected=expected, parts_present=present)
    report(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
