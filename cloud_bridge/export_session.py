"""Export and locally replay one portable session. This tool never sends messages."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cloud_bridge import common

EXPORT_NAME = "DOUYIN_CLOUD_SESSION.txt"
PART_NAMES = tuple(f"DOUYIN_CLOUD_SESSION_{index:02d}" for index in range(1, 17))
SESSION_SNAPSHOT = """() => {
  if (location.protocol !== 'http:' && location.protocol !== 'https:') return null;
  if (sessionStorage.length > 4096) throw new Error('storage_limit');
  const values = Object.create(null);
  for (let index = 0; index < sessionStorage.length; index++) {
    const key = sessionStorage.key(index);
    if (key !== null) values[key] = sessionStorage.getItem(key);
  }
  return {origin: location.origin, values};
}"""


class ExportFailure(Exception):
    """Carries only a fixed, non-sensitive stage identifier."""


def read_friend(root):
    data = Path(root) / "data"
    path = data / "panel-config.json"
    if not path.is_file():
        path = data / "local-settings.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        friend = value.get("friend") if isinstance(value, dict) else None
        if not isinstance(friend, str) or not 1 <= len(friend.strip()) <= 100:
            raise ValueError
    except Exception:
        raise ExportFailure("settings") from None
    return friend.strip()


def valid_origin(origin):
    if not isinstance(origin, str):
        return False
    try:
        parts = urlsplit(origin)
        return (parts.scheme in ("http", "https") and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and not parts.path and not parts.query and not parts.fragment
                and origin == parts.scheme + "://" + parts.netloc
                and parts.port != 0)
    except ValueError:
        return False


async def capture_session_storage(context):
    captured = {}
    for page in context.pages:
        for frame in page.frames:
            try:
                snapshot = await frame.evaluate(SESSION_SNAPSHOT)
            except Exception:
                raise ExportFailure("session_storage") from None
            if snapshot is None:
                continue
            if not isinstance(snapshot, dict) or not valid_origin(snapshot.get("origin")):
                raise ExportFailure("session_storage")
            origin, values = snapshot["origin"], snapshot.get("values")
            if (not isinstance(values, dict) or len(values) > 4096
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           for key, value in values.items())):
                raise ExportFailure("session_storage")
            if origin in captured and captured[origin] != values:
                raise ExportFailure("session_conflict")
            captured[origin] = values
            if len(captured) > 32 or sum(map(len, captured.values())) > 4096:
                raise ExportFailure("session_storage")
    return captured


async def _close_all(resources):
    failed = False
    for key, method in (("replay_context", "close"), ("original", "close"),
                        ("browser", "close"), ("playwright", "stop")):
        resource = resources.pop(key, None)
        if resource is not None:
            try:
                await getattr(resource, method)()
            except Exception:
                failed = True
    return not failed


async def _collect_and_replay(resources, friend, profile, factory):
    playwright = resources["playwright"] = await factory().start()
    context = resources["original"] = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile), channel="msedge", headless=False,
        viewport={"width": 1440, "height": 1000}, locale="zh-CN",
    )
    result = await common.probe(context, friend)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ExportFailure("original_probe")
    state = await context.storage_state(indexed_db=True)
    sessions = await capture_session_storage(context)
    bundle = {"version": common.BUNDLE_VERSION, "friend": friend,
              "storage_state": state, "session_storage": sessions}
    # Close the real profile before testing a genuinely fresh browser context.
    await asyncio.wait_for(context.close(), timeout=15)
    resources.pop("original", None)
    browser = resources["browser"] = await playwright.chromium.launch(
        channel="msedge", headless=False,
    )
    replay = resources["replay_context"] = await browser.new_context(
        storage_state=state, viewport={"width": 1440, "height": 1000}, locale="zh-CN",
    )
    await replay.add_init_script(common.session_script(bundle))
    result = await common.probe(replay, friend)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ExportFailure("replay_probe")
    return bundle


def _write_secret(output, packed):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", newline="\n",
                                         dir=output.parent, prefix=".cloud-export-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(packed + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _clear_exports(folder):
    # Exact owned files only; preserve all unrelated notes and browser data.
    for name in ("DOUYIN_CLOUD_SESSION",) + PART_NAMES:
        (folder / (name + ".txt")).unlink(missing_ok=True)


def _write_parts(folder, secrets):
    allowed = {"DOUYIN_CLOUD_SESSION", *PART_NAMES}
    if not isinstance(secrets, dict) or "DOUYIN_CLOUD_SESSION" not in secrets or not set(secrets) <= allowed:
        raise ExportFailure("export_format")
    for name, value in secrets.items():
        if not isinstance(value, str) or not value:
            raise ExportFailure("export_format")
    # The manifest is the final commit marker; never leave an old manifest
    # pointing to a partially updated set of files.
    for name in PART_NAMES:
        if name in secrets:
            _write_secret(folder / (name + ".txt"), secrets[name])
    _write_secret(folder / EXPORT_NAME, secrets["DOUYIN_CLOUD_SESSION"])


async def export_session(root=PROJECT_ROOT, *, playwright_factory=None, local_module=None):
    """Return the secret's path only after the local replay passes and closes."""
    root = Path(root)
    output = root / "data" / "cloud-export" / EXPORT_NAME
    resources = {}
    try:
        if local_module is None:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            local_module = importlib.import_module("local_check")
        if playwright_factory is None:
            from playwright.async_api import async_playwright
            playwright_factory = async_playwright
        lock = local_module.single_instance()
        try:
            lock.__enter__()
        except RuntimeError:
            raise ExportFailure("profile_busy") from None
        try:
            try:
                _clear_exports(output.parent)
            except OSError:
                raise ExportFailure("output_cleanup") from None
            friend = read_friend(root)
            profile = root / "data" / "edge-profile"
            if not profile.is_dir():
                raise ExportFailure("profile_missing")
            try:
                bundle = await asyncio.wait_for(
                    _collect_and_replay(resources, friend, profile, playwright_factory),
                    timeout=180,
                )
            finally:
                try:
                    closed = await asyncio.wait_for(_close_all(resources), timeout=15)
                except (Exception, asyncio.CancelledError):
                    closed = False
            if not closed:
                raise ExportFailure("close")
            try:
                secrets = common.pack_parts(bundle)
            except ValueError as exc:
                size_codes = {"bundle_too_large", "encoded_too_large", "single_secret_too_large"}
                raise ExportFailure("export_size" if getattr(exc, "code", "") in size_codes else "export_format") from None
            _write_parts(output.parent, secrets)
        except BaseException:
            _clear_exports(output.parent)
            raise
        finally:
            lock.__exit__(None, None, None)
        return output
    except BaseException:
        # A second export that fails to acquire the shared lock must not
        # remove the files being produced by the active export.
        raise


MESSAGES = {
    "settings": "好友设置不完整，请先在原面板保存准确好友。",
    "profile_missing": "未找到原专用浏览器数据，请将本工具放进原成功目录。",
    "profile_busy": "原验证工具或发送任务正在使用浏览器，请正常关闭后再试；本次未更新导出。",
    "original_probe": "原专用浏览器检查未通过，请先在电脑完成登录、验证或好友定位。",
    "session_storage": "网页会话状态未能安全读取，本次未生成导出文件。",
    "session_conflict": "同一网站的多个页面会话不一致，请正常关闭多余页面后重试。",
    "replay_probe": "全新浏览器恢复检查未通过，此次登录状态暂不能用于云端验证。",
    "export_size": "登录状态超过本版支持的分片总容量，本次未生成导出文件，请反馈此提示。",
    "export_format": "网页登录状态格式校验未通过，本次未生成导出文件，请反馈此提示。",
    "close": "浏览器未能正常关闭，本次未生成导出文件。",
    "output_cleanup": "无法移除上一次导出文件，请关闭该文件后重试。",
}


def main():
    print("本工具只检查登录和好友，导出后再用全新浏览器验证；不会发送消息。")
    try:
        output = asyncio.run(export_session())
        manifest = json.loads(output.read_text(encoding="ascii"))
        if manifest.get("format") != "douyin-session-parts-v1" or type(manifest.get("count")) is not int or not 1 <= manifest["count"] <= 16:
            raise ExportFailure("export_format")
    except ExportFailure as exc:
        print(MESSAGES.get(str(exc), "导出未完成，本次没有可用导出文件。"))
        return 1
    except (TimeoutError, asyncio.TimeoutError):
        print("检查超时，已停止；本次未生成导出文件。")
        return 1
    except KeyboardInterrupt:
        print("已取消，本次未生成导出文件。")
        return 130
    except Exception:
        print("导出未完成，请检查 Edge 和原工具环境；本次未生成导出文件。")
        return 1
    print("原浏览器与全新浏览器检查均通过；这还不代表云端能够发送消息。")
    print(f"已自动拆成 {manifest['count']} 份数据，另有 1 份索引，共 {manifest['count'] + 1} 个 Secret。")
    print("导出目录：" + str(output.parent))
    print("下一步：在原工具目录双击 复制云端Secret.cmd，按界面依次复制名称和内容。")
    print("请保留这些文件在本机，不要提交到仓库或发进聊天。本工具未上传、未复制内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
