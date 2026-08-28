#!/usr/bin/env python3
"""Защита от подгонки (ТЗ §4): проверяет, что после шага субагента изменены
только файлы из его зоны.

Использование: check_zone_diff.py <роль> [--since <rev>]

Учитываются: коммиты после <rev>, незакоммиченные изменения и новые файлы.
Ненулевой код выхода = нарушение зоны → немедленная эскалация.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))
from _zones import DEFAULTS, current_scope, load_config, resolve_zones  # noqa: E402

# файлы, обновляемые самим конвейером/хуками — не считаются нарушением
ALWAYS_OK = {"state.json"}
ALWAYS_OK_PREFIXES = ("reports/trace/",)


def git_files(*args: str) -> set[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def matches(rel: str, prefix: str) -> bool:
    if prefix.endswith("/"):
        return rel.startswith(prefix)
    return rel == prefix


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    role = argv[0]
    since = argv[argv.index("--since") + 1] if "--since" in argv else None

    changed: set[str] = set()
    if since:
        changed |= git_files("diff", "--name-only", f"{since}..HEAD")
    changed |= git_files("diff", "--name-only", "HEAD")
    changed |= git_files("ls-files", "--others", "--exclude-standard")

    cfg = load_config(ROOT)
    scope = current_scope(ROOT)
    zones = resolve_zones(cfg, scope)
    zone = zones.get(role) or DEFAULTS["zones"].get(role)
    if zone is None:
        print(f"неизвестная роль: {role}")
        return 2
    protected = cfg.get("protected", DEFAULTS["protected"])
    shared = cfg.get("shared_artifacts") or []

    violations = []
    for rel in sorted(changed):
        if rel in ALWAYS_OK or rel.startswith(ALWAYS_OK_PREFIXES):
            continue
        if any(matches(rel, s) for s in shared):
            continue  # общий артефакт монорепозитория
        if any(matches(rel, p) for p in protected):
            violations.append((rel, "защищённый путь"))
        elif any(matches(rel, d) for d in zone.get("deny", [])):
            violations.append((rel, "deny-зона роли"))
        elif not any(matches(rel, a) for a in zone.get("allow", [])):
            violations.append((rel, "вне allow-зоны роли"))

    if violations:
        print(f"НАРУШЕНИЕ ЗОНЫ записи ролью '{role}':")
        for rel, why in violations:
            print(f"  - {rel} ({why})")
        print("Требуется немедленная эскалация человеку (ТЗ §4).")
        return 1

    scope_note = (f", область: {', '.join(scope['services'])}"
                  if scope.get("services") else "")
    print(f"ok: изменения роли '{role}' в пределах зоны "
          f"({len(changed)} файлов проверено{scope_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
