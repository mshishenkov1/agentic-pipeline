#!/usr/bin/env python3
"""PostToolUse-hook: полная трасса действий агентов в reports/trace/ (ТЗ §7)."""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _zones import current_agent, project_root  # noqa: E402


def brief(tool: str, tool_input: dict) -> str:
    if tool in ("Write", "Edit", "NotebookEdit"):
        return tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if tool == "Bash":
        return (tool_input.get("command") or "")[:300]
    if tool == "Task":
        return f"subagent={tool_input.get('subagent_type')} :: {(tool_input.get('description') or '')[:120]}"
    return json.dumps(tool_input, ensure_ascii=False)[:200]


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    root = project_root(hook_input)
    trace_dir = root / "reports" / "trace"
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "agent": current_agent(root),
            "session": hook_input.get("session_id", "")[:8],
            "tool": hook_input.get("tool_name"),
            "detail": brief(hook_input.get("tool_name") or "", hook_input.get("tool_input") or {}),
        }
        day = datetime.date.today().isoformat()
        with open(trace_dir / f"trace-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
