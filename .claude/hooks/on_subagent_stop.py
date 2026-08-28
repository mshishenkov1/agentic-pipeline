#!/usr/bin/env python3
"""SubagentStop-hook: возвращает управление оркестратору и пишет переход в state.json."""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _zones import project_root  # noqa: E402


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    state_path = project_root(hook_input) / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        finished = state.get("current_agent", "?")
        state["current_agent"] = "orchestrator"
        state.setdefault("history", []).append({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "event": f"subagent_stop: {finished} -> orchestrator",
        })
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
