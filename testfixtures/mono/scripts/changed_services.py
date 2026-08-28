#!/usr/bin/env python3
"""Определяет сервисы, затронутые изменениями относительно базовой ветки.

Нужен для селективного запуска гейтов: на большом монорепозитории гонять всё
на каждый коммит непрактично.

Использование:
  changed_services.py                 — список имён, по одному в строке
  changed_services.py --json          — JSON-массив (для матрицы CI)
  changed_services.py --base <ref>    — база сравнения (умолчание из конфига)
  changed_services.py --all           — все сервисы, без учёта изменений

Плоский режим (topology: flat) печатает пустой список: гейты гоняются глобально.
Изменение общих файлов (контракты, конфиг, скрипты, схемы) считается
затрагивающим ВСЕ сервисы — иначе ломающее изменение проскочит незамеченным.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Изменение этих путей влияет на все сервисы разом
GLOBAL_PATHS = ("contracts/", "pipeline.config.yaml", "scripts/", ".github/",
                "system-design.md", "requirements.md", "acceptance-criteria.yaml")


def cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def changed_files(base: str) -> list[str]:
    for args in (["diff", "--name-only", f"{base}...HEAD"],
                 ["diff", "--name-only", base]):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return []


def main(argv: list[str]) -> int:
    conf = cfg()
    services = conf.get("services") or {}
    as_json = "--json" in argv

    if conf.get("topology", "flat") == "flat" or not services:
        print(json.dumps([]) if as_json else "", end="" if as_json else "\n")
        return 0

    if "--all" in argv:
        names = sorted(services)
    else:
        base = (argv[argv.index("--base") + 1] if "--base" in argv
                else conf.get("base_branch", "main"))
        files = changed_files(base)
        if any(f.startswith(GLOBAL_PATHS) for f in files):
            names = sorted(services)          # общее изменение — проверяем всё
        else:
            names = sorted(
                name for name, svc in services.items()
                if any(f.startswith(str(svc.get("path", name)).rstrip("/") + "/")
                       for f in files))

    if as_json:
        print(json.dumps(names))
    else:
        for n in names:
            print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
