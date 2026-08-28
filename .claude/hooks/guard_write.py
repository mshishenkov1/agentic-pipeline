#!/usr/bin/env python3
"""PreToolUse-hook: контроль зон записи для Write/Edit/NotebookEdit (ТЗ §6.3, §7).

Выход 2 = запрет; stderr возвращается модели как объяснение.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _zones import check_path  # noqa: E402


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = hook_input.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        sys.exit(0)

    ok, reason = check_path(path, hook_input)
    if not ok:
        print(f"[guard_write] ЗАПРЕЩЕНО: {reason}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
