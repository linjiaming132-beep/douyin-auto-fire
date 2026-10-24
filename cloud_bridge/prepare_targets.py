"""Prepare a private multi-recipient Secret without opening a browser or sending."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_bridge import common, prepare_message
from cloud_bridge.targets import MAX_SECRET_BYTES, TARGETS_SECRET, _loads, parse_targets, session_digest


class TargetsPreparationError(ValueError):
    """Expose a fixed preparation stage, never paths, recipients, or text."""

    def __init__(self, code):
        allowed = {"pause_required", "settings", "session", "history", "clock", "targets_config",
                   "profile_busy", "output_cleanup", "input", "invocation"}
        self.code = code if code in allowed else "targets_config"
        super().__init__(self.code)


def _bounded_read(path, limit):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
    return raw


def _read_panel(root):
    try:
        panel = _loads(_bounded_read(root / "data" / "panel-config.json", MAX_SECRET_BYTES).decode("utf-8-sig"))
        if not isinstance(panel, dict):
            raise ValueError
        if panel.get("enabled") is not False:
            raise TargetsPreparationError("pause_required")
    except TargetsPreparationError:
        raise
    except Exception:
        raise TargetsPreparationError("settings") from None


def _read_bundle(root):
    folder = root / "data" / "cloud-export"
    try:
        values = {}
        names = [common.MAIN_SECRET] + [f"{common.MAIN_SECRET}_{index:02d}" for index in range(1, common.MAX_PARTS + 1)]
        for name in names:
            path = folder / (name + ".txt")
            if path.exists():
                values[name] = _bounded_read(path, common.MAX_SECRET_BYTES + 2).decode("ascii").strip()
        return common.unpack_parts(values)
    except Exception:
        raise TargetsPreparationError("session") from None


def _atomic_write(output, value):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=output.parent,
                                         prefix=".cloud-targets-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_target_rows(root, rows, current=None, local_module=None):
    """Bind explicit recipients to the existing export and import each local history."""
    root = Path(root)
    output = root / "data" / "cloud-export" / (TARGETS_SECRET + ".txt")
    if local_module is None:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        local_module = importlib.import_module("local_check")
    lock = local_module.single_instance()
    try:
        lock.__enter__()
    except RuntimeError:
        raise TargetsPreparationError("profile_busy") from None
    try:
        try:
            _read_panel(root)
            bundle = _read_bundle(root)
            instant = current if current is not None else datetime.now(prepare_message.BJ)
            if not isinstance(instant, datetime) or instant.tzinfo is None:
                raise TargetsPreparationError("clock")
            current_day = instant.astimezone(prepare_message.BJ).date()
            if not isinstance(rows, list):
                raise TargetsPreparationError("targets_config")
            if any(not isinstance(row, dict) or set(row) != {"friend", "text"} for row in rows):
                raise TargetsPreparationError("targets_config")
            value = parse_targets({"version": 1, "session_sha256": session_digest(bundle),
                                   "targets": [{"friend": row["friend"], "text": row["text"], "imported_days": []} for row in rows]}, bundle)
            for item in value["targets"]:
                try:
                    item["imported_days"] = prepare_message._import_history(root, item["friend"], current_day)
                except Exception:
                    raise TargetsPreparationError("history") from None
            value = parse_targets(value, bundle)
            _atomic_write(output, value)
        except BaseException:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                raise TargetsPreparationError("output_cleanup") from None
            raise
    finally:
        lock.__exit__(None, None, None)
    return output


def prepare_targets(root, friends, text, current=None, local_module=None):
    """Compatibility entry for an explicit list that shares the same text."""
    rows = [{"friend": friend, "text": text} for friend in friends] if isinstance(friends, list) else None
    return prepare_target_rows(root, rows, current=current, local_module=local_module)


def _arguments(argv):
    if (len(argv) != 4 or argv[0] not in {"--root", "--input"}
            or argv[2] not in {"--root", "--input"} or argv[0] == argv[2]
            or not argv[1] or not argv[3]):
        raise TargetsPreparationError("invocation")
    values = {argv[0]: argv[1], argv[2]: argv[3]}
    return Path(values["--root"]), Path(values["--input"])


def main(argv=None):
    try:
        root, input_path = _arguments(list(sys.argv[1:] if argv is None else argv))
        try:
            value = _loads(_bounded_read(input_path, MAX_SECRET_BYTES).decode("utf-8-sig"))
            if not isinstance(value, dict) or set(value) not in ({"targets"}, {"friends", "text"}):
                raise ValueError
        except Exception:
            value = {"targets": None}
        if set(value) == {"friends", "text"}:
            output = prepare_targets(root, value["friends"], value["text"])
        else:
            output = prepare_target_rows(root, value["targets"])
        count = len(parse_targets(output.read_text(encoding="utf-8"))["targets"])
    except KeyboardInterrupt:
        print("CLOUD_TARGETS_PREP_V1 ok=false stage=cancelled count=0")
        return 130
    except Exception as exc:
        code = exc.code if isinstance(exc, TargetsPreparationError) else "targets_config"
        print(f"CLOUD_TARGETS_PREP_V1 ok=false stage={code} count=0")
        return 1
    print(f"CLOUD_TARGETS_PREP_V1 ok=true stage=prepared count={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

