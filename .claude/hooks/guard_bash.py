#!/usr/bin/env python3
"""PreToolUse-hook для Bash: запрет опасных операций (git и инфраструктурных)
и best-effort контроль записи в чужие зоны через редиректы/tee/sed -i/cp/mv.

Три группы правил:
  1. git — force-push, push в main, удаление веток;
  2. инфраструктура — разрушающие операции и apply в неразрешённую среду;
  3. запись — редиректы в чужую зону.

Основная страховка от обхода — diff-проверка scripts/check_zone_diff.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _zones import check_path  # noqa: E402

GIT_RULES = [
    (r"git\s+push\b.*(\s--force\b|\s-f\b|--force-with-lease)", "force-push запрещён (только человек)"),
    (r"git\s+push\b.*\b(main|master)\b", "push в main запрещён (только человек)"),
    (r"git\s+push\b.*--delete", "удаление удалённых веток запрещено (только человек)"),
    (r"git\s+branch\s+(-D|-d)\b", "удаление веток запрещено (только человек)"),
    (r"rm\s+(-\w+\s+)*(/|~|\$HOME)(\s|$|/\*)", "удаление вне репозитория запрещено"),
]

PROTECTED_TOKEN = r"(pipeline\.config\.yaml|\.claude/|\.github/|knowledge/)"

# Разрушающие инфраструктурные операции — запрещены всем агентам без исключений.
# Их выполняет только человек: цена ошибки — уничтоженная среда или данные.
DESTRUCTIVE_RULES = [
    (r"\bterraform\b.*\bdestroy\b", "terraform destroy уничтожает среду"),
    (r"\bterraform\b.*\bstate\s+rm\b", "terraform state rm рассинхронизирует состояние"),
    (r"\bterraform\b.*-auto-approve\b", "apply без плана и подтверждения запрещён"),
    (r"\bkubectl\b.*\bdelete\b.*\b(namespace|ns|pv|pvc|statefulset|crd)\b",
     "удаление namespace, томов или statefulset уничтожает данные"),
    (r"\bkubectl\b.*\b(drain|cordon)\b", "вывод узла из обслуживания — операция человека"),
    (r"\bhelm\b.*\b(uninstall|delete)\b", "helm uninstall сносит релиз"),
    (r"\bdocker\b.*\bsystem\s+prune\b", "docker system prune удаляет данные"),
    (r"\b(aws|az|gcloud|yc)\b.*\bdelete\b", "удаление облачного ресурса — операция человека"),
    (r"\bdrop\s+(database|table|schema)\b", "DROP уничтожает данные"),
    (r"\btruncate\s+table\b", "TRUNCATE уничтожает данные"),
]

# Операции, применяющие изменения к среде: разрешены только в apply_allowed.
APPLY_RULES = [
    r"\bterraform\b.*\bapply\b",
    r"\bkubectl\b.*\bapply\b",
    r"\bhelm\b.*\b(install|upgrade)\b",
    r"\bkustomize\b.*\bbuild\b.*\|\s*kubectl\s+apply",
]

# Смена контекста/профиля на боевой — запрещена внутри сессии агента.
CONTEXT_RULES = [
    r"\bkubectl\b.*\bconfig\s+use-context\b.*\b(prod|production)\b",
    r"\bterraform\b.*\bworkspace\s+select\b.*\b(prod|production)\b",
]
WRITE_VERB = r"(\brm\b|\bmv\b|\bcp\b|\btee\b|\bsed\s+-i|\btruncate\b|\bchmod\b|>>?|\bln\b)"

# цели редиректов и tee — прогоняем через зонную проверку
REDIRECT_TARGET = re.compile(r"(?:>>?\s*|\btee\s+(?:-a\s+)?)([\w./~-]+)")


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cmd = (hook_input.get("tool_input") or {}).get("command") or ""

    for pattern, reason in GIT_RULES:
        if re.search(pattern, cmd):
            deny(reason)

    for pattern, reason in DESTRUCTIVE_RULES:
        if re.search(pattern, cmd, re.I):
            deny(f"разрушающая операция — только человек: {reason}")

    for pattern in CONTEXT_RULES:
        if re.search(pattern, cmd, re.I):
            deny("переключение на боевой контур запрещено агенту")

    check_apply_environment(cmd, hook_input)

    if re.search(WRITE_VERB, cmd) and re.search(PROTECTED_TOKEN, cmd):
        deny("команда изменяет защищённый путь (pipeline.config.yaml / .claude / .github) — только человек")

    for target in REDIRECT_TARGET.findall(cmd):
        if target.startswith(("/dev/", "-")):
            continue
        ok, reason = check_path(target, hook_input)
        if not ok:
            deny(f"редирект в чужую зону: {reason}")

    sys.exit(0)


def infra_config(hook_input: dict) -> dict:
    """Секция infra из конфига; пустой словарь, если не настроена."""
    try:
        from _zones import load_config, project_root
        return (load_config(project_root(hook_input)) or {}).get("infra") or {}
    except Exception:
        return {}


def check_apply_environment(cmd: str, hook_input: dict) -> None:
    """apply разрешён только в среды из infra.apply_allowed.

    Команда без явно названной среды тоже запрещается: неоднозначный apply —
    самый частый способ выкатить на прод по ошибке.
    """
    if not any(re.search(p, cmd, re.I) for p in APPLY_RULES):
        return
    cfg = infra_config(hook_input)
    allowed = [str(e).lower() for e in (cfg.get("apply_allowed") or [])]
    known = [str(e).lower() for e in (cfg.get("environments") or [])] or allowed
    if not allowed:
        deny("apply запрещён: в pipeline.config.yaml не задан infra.apply_allowed")

    mentioned = [e for e in known if re.search(rf"\b{re.escape(e)}\b", cmd, re.I)]
    if not mentioned:
        deny(f"apply без явно указанной среды запрещён "
             f"(разрешены: {', '.join(allowed)}) — укажи среду в команде")
    forbidden = [e for e in mentioned if e not in allowed]
    if forbidden:
        deny(f"apply в среду '{', '.join(forbidden)}' запрещён агенту "
             f"(разрешены только {', '.join(allowed)}); раскатку туда делает человек")


def deny(reason: str) -> None:
    print(f"[guard_bash] ЗАПРЕЩЕНО: {reason}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
