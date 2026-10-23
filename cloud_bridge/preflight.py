"""GitHub-hosted read-only check; no send option and no sender dependency."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_bridge.common import probe, session_script, unpack_secret


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
    allowed = {"ok", "stage", "search_visible", "target_open", "editor_visible", "login_text_visible", "verification_text_visible"}
    safe = {key: value for key, value in result.items() if key in allowed}
    safe["real_messages_sent"] = 0
    print("CLOUD_PREFLIGHT_V1 " + json.dumps(safe, ensure_ascii=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        titles = {"ready": "云端登录和好友定位检查通过；尚未验证发送", "configuration": "缺少或无法读取 DOUYIN_CLOUD_SESSION",
                  "environment": "浏览器或运行环境未就绪", "timeout": "检查超时", "navigation": "私信页面未正常打开",
                  "search": "未检测到私信搜索框", "target": "无法确认准确好友聊天已打开", "editor": "未检测到可编辑消息框"}
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("### 抖音云端会话检查\n\n" + titles.get(safe.get("stage"), "检查未完成") + "\n\n")
            for key in ("search_visible", "target_open", "editor_visible", "login_text_visible", "verification_text_visible"):
                if key in safe:
                    stream.write("- `" + key + "`: `" + str(safe[key]).lower() + "`\n")
            stream.write("\n本次真实发送：**0 条**。验证字样不等于已发生拦截；预检通过也不代表发送已成功。\n")


def main():
    try:
        bundle = unpack_secret(os.environ.pop("DOUYIN_CLOUD_SESSION", "").strip())
    except ValueError:
        report({"ok": False, "stage": "configuration"})
        return 2
    try:
        result = asyncio.run(asyncio.wait_for(check(bundle), timeout=180))
    except asyncio.TimeoutError:
        result = {"ok": False, "stage": "timeout"}
    except Exception:
        result = {"ok": False, "stage": "environment"}
    report(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
