#!/usr/bin/env python3
"""CLI для работы оркестратора со state.json.

Использование:
  state.py get [ключ]                — показать состояние или значение ключа
  state.py set <ключ> <значение>     — установить ключ (значение парсится как JSON, иначе строка)
  state.py agent <роль>              — установить current_agent
                                       (orchestrator|intake|architect|perf|infra|spec|dev|test|review)
  state.py stage <этап>              — установить stage
  state.py design <статус>           — статус фазы системного дизайна
                                       (not_started|intake|design|design_review|approved)
  state.py scope <сервис...>         — область работы прогона (мультисервисный режим)
  state.py scope --infra [<сервис>..]  — включить в область инфраструктурный код
  state.py scope --clear             — очистить область (вернуться к плоскому режиму)
  state.py log "<событие>"           — добавить запись в history
  state.py sync-bugs                 — синхронизировать сводку bugs/ в state.json
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state.json"
ROLES = {"orchestrator", "intake", "architect", "perf", "infra",
         "spec", "dev", "test", "review"}
DESIGN_STATUSES = ["not_started", "intake", "design", "design_review", "approved"]


def load() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


# Сколько последних событий держать в state.json. Остальное уезжает в
# reports/history/, иначе состояние перестаёт помещаться в контекст оркестратора.
HISTORY_LIMIT = 60


def rotate_history(state: dict) -> int:
    """Переносит хвост истории в reports/history/. Возвращает число вынесенных."""
    history = state.get("history") or []
    if len(history) <= HISTORY_LIMIT:
        return 0
    overflow = history[:-HISTORY_LIMIT]
    run_id = state.get("run_id") or "unknown"
    out = ROOT / "reports" / "history"
    try:
        out.mkdir(parents=True, exist_ok=True)
        with open(out / f"{run_id}.jsonl", "a", encoding="utf-8") as f:
            for entry in overflow:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"не удалось вынести историю: {e}", file=sys.stderr)
        return 0
    state["history"] = history[-HISTORY_LIMIT:]
    return len(overflow)


def save(state: dict) -> None:
    moved = rotate_history(state)
    if moved:
        print(f"история: вынесено {moved} событий в reports/history/", file=sys.stderr)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log_event(state: dict, event: str) -> None:
    state.setdefault("history", []).append(
        {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "event": event}
    )


def _known_services() -> set:
    """Имена сервисов из конфига; пустое множество, если конфиг не читается."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8"))
        return set((cfg or {}).get("services") or {})
    except Exception:
        return set()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    state = load()

    if cmd == "get":
        print(json.dumps(state.get(args[0]) if args else state, ensure_ascii=False, indent=2))
        return 0

    if cmd == "set" and len(args) == 2:
        key, raw = args
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        state[key] = value
        log_event(state, f"set {key}={raw}")
    elif cmd == "agent" and len(args) == 1:
        if args[0] not in ROLES:
            print(f"неизвестная роль: {args[0]} (допустимо: {sorted(ROLES)})", file=sys.stderr)
            return 1
        state["current_agent"] = args[0]
        log_event(state, f"current_agent -> {args[0]}")
    elif cmd == "stage" and len(args) == 1:
        state["stage"] = args[0]
        log_event(state, f"stage -> {args[0]}")
    elif cmd == "design" and len(args) == 1:
        if args[0] not in DESIGN_STATUSES:
            print(f"неизвестный статус: {args[0]} (допустимо: {DESIGN_STATUSES})",
                  file=sys.stderr)
            return 1
        design = state.setdefault("system_design", {"status": "not_started"})
        design["status"] = args[0]
        if args[0] == "approved":
            design["approved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        log_event(state, f"system_design.status -> {args[0]}")
    elif cmd == "scope":
        if "--clear" in args:
            state.pop("scope", None)
            log_event(state, "scope -> очищен (плоский режим)")
        else:
            infra = "--infra" in args
            names = [a for a in args if not a.startswith("--")]
            known = _known_services()
            unknown = [n for n in names if known and n not in known]
            if unknown:
                print(f"неизвестные сервисы: {', '.join(unknown)} "
                      f"(в pipeline.config.yaml описаны: {', '.join(sorted(known))})",
                      file=sys.stderr)
                return 1
            state["scope"] = {"services": names, "infra": infra}
            log_event(state, f"scope -> сервисы: {names or '—'}, инфраструктура: {infra}")
    elif cmd == "log" and len(args) == 1:
        log_event(state, args[0])
    elif cmd == "sync-bugs":
        bugs = []
        for f in sorted((ROOT / "bugs").glob("BUG-*.json")):
            try:
                b = json.loads(f.read_text(encoding="utf-8"))
                bugs.append({k: b.get(k) for k in
                             ("id", "severity", "status", "iteration", "related_ac", "title")})
            except Exception as e:
                print(f"повреждённый баг-файл {f.name}: {e}", file=sys.stderr)
        state["bugs"] = bugs
        state["escalations"] = [b["id"] for b in bugs if b.get("status") == "escalated"]
        log_event(state, f"sync-bugs: {len(bugs)} багов, "
                         f"{sum(1 for b in bugs if b.get('status') == 'open')} открытых")
    else:
        print(__doc__)
        return 1

    save(state)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
