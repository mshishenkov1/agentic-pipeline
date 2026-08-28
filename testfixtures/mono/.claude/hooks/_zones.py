"""Общая логика зон записи для hooks. Источник истины — pipeline.config.yaml.

Если PyYAML недоступен системному python3, используются встроенные значения,
зеркалящие конфиг (при изменении зон в конфиге обнови и DEFAULTS).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "zones": {
        "orchestrator": {"allow": ["state.json", "reports/", "bugs/", "examples/"],
                         "deny": ["src/", "tests/"]},
        "intake": {"allow": ["requirements.md", "reports/"],
                   "deny": ["src/", "tests/", "spec.md", "acceptance-criteria.yaml",
                            "system-design.md"]},
        "architect": {"allow": ["system-design.md", "migration-plan.md", "decisions/",
                                "contracts/", "docs/", "reports/"],
                      "deny": ["src/", "tests/", "requirements.md",
                               "acceptance-criteria.yaml"]},
        "perf": {"allow": ["loadtests/", "reports/", "bugs/"],
                 "deny": ["src/", "tests/", "system-design.md", "requirements.md"]},
        "spec": {"allow": ["spec.md", "acceptance-criteria.yaml", "reports/"], "deny": []},
        "dev": {"allow": ["src/", "disputes/", "pyproject.toml"], "deny": ["tests/"]},
        "test": {"allow": ["tests/", "bugs/", "reports/"], "deny": ["src/"]},
        "review": {"allow": ["reports/", "disputes/"], "deny": ["src/", "tests/"]},
        "infra": {"allow": ["deploy/", "infra/", "reports/"], "deny": ["src/", "tests/"]},
    },
    "protected": ["pipeline.config.yaml", ".claude/", ".github/", "knowledge/"],
    "topology": "flat",
    "services": {},
    "shared_artifacts": [],
    # Зеркало zone_templates из конфига. Используются только при непустом scope.
    "zone_templates": {
        "orchestrator": {"allow": ["state.json", "reports/", "bugs/", "examples/"],
                         "deny": ["{services}", "{infra}"]},
        "dev": {"allow": ["{service}/src/", "{service}/internal/", "{service}/cmd/",
                          "{service}/pyproject.toml", "{service}/go.mod",
                          "{service}/package.json", "disputes/"],
                "deny": ["{service}/tests/", "{foreign}", "{infra}"]},
        "test": {"allow": ["{service}/tests/", "bugs/", "reports/"],
                 "deny": ["{service}/src/", "{service}/internal/", "{foreign}", "{infra}"]},
        "infra": {"allow": ["{infra}", "reports/"], "deny": ["{services}"]},
        "perf": {"allow": ["loadtests/", "reports/", "bugs/"],
                 "deny": ["{services}", "{infra}"]},
        "review": {"allow": ["reports/", "disputes/"], "deny": ["{services}", "{infra}"]},
        "architect": {"allow": ["system-design.md", "migration-plan.md", "decisions/",
                                "contracts/", "docs/", "reports/"],
                      "deny": ["{services}", "{infra}", "requirements.md",
                               "acceptance-criteria.yaml"]},
        "intake": {"allow": ["requirements.md", "reports/"],
                   "deny": ["{services}", "{infra}", "spec.md",
                            "acceptance-criteria.yaml", "system-design.md"]},
        "spec": {"allow": ["spec.md", "acceptance-criteria.yaml", "reports/"],
                 "deny": ["{services}", "{infra}"]},
    },
}

# Каталоги временных файлов, куда запись разрешена любому агенту
TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")


def project_root(hook_input: dict) -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or hook_input.get("cwd") or "."
    return Path(root).resolve()


def load_config(root: Path) -> dict:
    cfg_path = root / "pipeline.config.yaml"
    try:
        import yaml  # type: ignore
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "zones" in cfg and "protected" in cfg:
            return cfg
    except Exception:
        pass
    return DEFAULTS


def current_agent(root: Path) -> str:
    try:
        with open(root / "state.json", encoding="utf-8") as f:
            return json.load(f).get("current_agent") or "orchestrator"
    except Exception:
        return "orchestrator"


def current_scope(root: Path) -> dict:
    """Область работы прогона: {"services": [...], "infra": bool}.

    Пустой список сервисов означает плоский режим — зоны берутся из секции
    zones, как было до введения мультисервисности.
    """
    try:
        with open(root / "state.json", encoding="utf-8") as f:
            scope = json.load(f).get("scope") or {}
    except Exception:
        return {"services": [], "infra": False}
    return {"services": list(scope.get("services") or []),
            "infra": bool(scope.get("infra"))}


def resolve_zones(cfg: dict, scope: dict) -> dict:
    """Разворачивает zone_templates по области работы.

    Плоский режим (topology: flat или пустой scope) возвращает секцию zones
    без изменений — это гарантирует обратную совместимость.
    """
    services = cfg.get("services") or {}
    in_scope = [s for s in scope.get("services", []) if s in services]
    if cfg.get("topology", "flat") == "flat" or not in_scope:
        return cfg.get("zones", {})

    templates = cfg.get("zone_templates") or DEFAULTS["zone_templates"]
    scope_paths = [str(services[s].get("path", s)).rstrip("/") for s in in_scope]
    all_paths = [str(v.get("path", k)).rstrip("/") for k, v in services.items()]
    foreign_paths = [p for p in all_paths if p not in scope_paths]
    infra_path = str((cfg.get("infra") or {}).get("path", "")).rstrip("/")

    def expand(pattern: str) -> list[str]:
        if "{service}" in pattern:
            return [pattern.replace("{service}", p) for p in scope_paths]
        if pattern == "{foreign}":
            return [p + "/" for p in foreign_paths]
        if pattern == "{services}":
            return [p + "/" for p in all_paths]
        if pattern == "{infra}":
            return [infra_path + "/"] if infra_path else []
        return [pattern]

    resolved = {}
    for role, zone in templates.items():
        resolved[role] = {
            key: [item for pat in zone.get(key, []) for item in expand(pat)]
            for key in ("allow", "deny")
        }
    return resolved


def _match(rel: str, prefix: str) -> bool:
    prefix = prefix.strip()
    if prefix.endswith("/"):
        return rel.startswith(prefix) or rel == prefix.rstrip("/")
    return rel == prefix


def check_path(raw_path: str, hook_input: dict) -> tuple[bool, str]:
    """Возвращает (разрешено, причина отказа)."""
    root = project_root(hook_input)
    p = Path(raw_path)
    if not p.is_absolute():
        p = root / p
    p = Path(os.path.normpath(p))

    # временные файлы — можно всем
    if str(p).startswith(TMP_PREFIXES):
        return True, ""

    try:
        rel = str(p.relative_to(root))
    except ValueError:
        return False, f"запись вне репозитория запрещена: {p}"

    cfg = load_config(root)

    # общие артефакты монорепозитория — не нарушение зоны ни для кого
    for shared in cfg.get("shared_artifacts", []) or []:
        if _match(rel, shared):
            return True, ""

    for prot in cfg.get("protected", []):
        if _match(rel, prot):
            return False, (f"'{rel}' — защищённый путь (pipeline.config.yaml → protected). "
                           "Изменять его может только человек.")

    # служебный файл оркестрации доступен всегда (обновляется скриптами/хуками)
    if rel == "state.json":
        return True, ""

    agent = current_agent(root)
    zone = resolve_zones(cfg, current_scope(root)).get(agent)
    if zone is None:
        return True, ""  # неизвестная роль — не блокируем, ловится diff-проверкой

    for d in zone.get("deny", []):
        if _match(rel, d):
            return False, (f"агенту '{agent}' запрещена запись в '{rel}' (deny-зона). "
                           "Если считаешь чужой артефакт неверным — верни возражение оркестратору.")
    for a in zone.get("allow", []):
        if _match(rel, a):
            return True, ""
    scope = current_scope(root)
    hint = (f" · область работы: {', '.join(scope['services'])}"
            if scope["services"] else "")
    return False, (f"'{rel}' вне разрешённой зоны агента '{agent}'{hint} "
                   f"(allow: {zone.get('allow')}).")
