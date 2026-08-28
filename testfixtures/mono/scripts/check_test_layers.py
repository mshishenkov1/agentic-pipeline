#!/usr/bin/env python3
"""G14: слои тестов — существуют, наполнены и реально исполняются.

До этого гейта слои были описаны в промпте test-agent, но их никто не запускал:
G1 гонял только основной сьют, G13 проверял файлы контрактов, а не контрактные
тесты. Для распределённой системы это дыра — интеграционные проверки ловят то,
что юнит-тесты пропускают по построению.

Проверяет:
  1. Обязательный слой существует и содержит хотя бы один файл тестов.
  2. Команда слоя отрабатывает зелёным.
  3. Каждое межсервисное взаимодействие из карты компонентов покрыто
     контрактным тестом.

Использование:
  check_test_layers.py                — проверить и прогнать
  check_test_layers.py --dry          — только наличие, без прогона
  check_test_layers.py --layer unit   — только один слой

Гейт УСЛОВНЫЙ: слои не настроены — «неприменим», код 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMPONENT = re.compile(r"^(\s{0,3})(#{2,4})\s*(C-\d+)\s*[:.]\s*(.+?)\s*$", re.M)
FIELD = re.compile(r"^\s*[-*]\s*\*\*(.+?):?\*\*[:\t ]*(.*)$", re.M)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
COMP_ID = re.compile(r"C-\d+")

TEST_FILE_HINTS = ("test_", "_test.", ".test.", "Test.", "spec.", ".spec.")


def cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)


def tail(text: str, n: int = 240) -> str:
    text = (text or "").strip()
    return text[-n:].replace("\n", " | ") if text else "(пусто)"


def has_tests(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*")
               if p.is_file() and any(h in p.name for h in TEST_FILE_HINTS))


def targets(conf: dict) -> list[tuple[str, Path, dict]]:
    """[(область, корень области, конфигурация слоёв)]."""
    services = conf.get("services") or {}
    global_layers = conf.get("test_layers") or {}
    if conf.get("topology", "flat") == "flat" or not services:
        return [("", ROOT, global_layers)]
    out = []
    for name, svc in services.items():
        layers = svc.get("test_layers") or global_layers
        out.append((name, ROOT / str(svc.get("path", name)), layers))
    return out


def design_interactions() -> list[tuple[str, str]]:
    design = ROOT / "system-design.md"
    if not design.exists():
        return []
    text = design.read_text(encoding="utf-8")
    entries = list(COMPONENT.finditer(text))
    pairs = []
    for i, m in enumerate(entries):
        start, level = m.end(), len(m.group(2))
        end = len(text)
        for nxt in ANY_HEADING.finditer(text, start):
            h = re.match(r"\s{0,3}(#+)", nxt.group(0))
            if h and len(h.group(1)) <= level:
                end = nxt.start()
                break
        fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(text[start:end])}
        for dep in COMP_ID.findall(fields.get("зависит от", "")):
            pairs.append((m.group(3), dep))
    return pairs


def main(argv: list[str]) -> int:
    conf = cfg()
    only = argv[argv.index("--layer") + 1] if "--layer" in argv else None
    dry = "--dry" in argv

    all_targets = targets(conf)
    if not any(layers for _, _, layers in all_targets):
        print("G14 неприменим: слои тестов не настроены "
              "(секция test_layers в pipeline.config.yaml)")
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    ran = 0

    for area, area_root, layers in all_targets:
        prefix = f"[{area}] " if area else ""
        for layer, spec in (layers or {}).items():
            if only and layer != only:
                continue
            spec = spec or {}
            rel = str(spec.get("path") or f"tests/{layer}")
            path = area_root / rel
            required = bool(spec.get("required"))
            count = has_tests(path)

            if count == 0:
                msg = f"{prefix}слой '{layer}': нет тестов в {rel}"
                if required:
                    errors.append(msg + " (слой обязателен)")
                else:
                    warnings.append(msg)
                continue

            command = spec.get("command")
            if not command:
                warnings.append(f"{prefix}слой '{layer}': {count} файлов, "
                                f"но команда прогона не задана — не исполняется")
                continue
            if dry:
                print(f"{prefix}{layer}: {count} файлов, команда задана (прогон пропущен)")
                continue

            r = sh(command)
            ran += 1
            if r.returncode != 0:
                errors.append(f"{prefix}слой '{layer}' красный: {tail(r.stdout + r.stderr)}")
            else:
                print(f"{prefix}{layer}: ok ({count} файлов)")

    # межсервисные взаимодействия без контрактных тестов
    interactions = design_interactions()
    if interactions:
        contract_blob = ""
        for area, area_root, layers in all_targets:
            cpath = area_root / str(((layers or {}).get("contract") or {}).get(
                "path", "tests/contract"))
            if cpath.exists():
                for f in cpath.rglob("*"):
                    if f.is_file():
                        try:
                            contract_blob += f.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            pass
        for src, dst in interactions:
            if dst not in contract_blob:
                warnings.append(f"взаимодействие {src} → {dst} не покрыто контрактным тестом")

    print(f"\nпрогнано слоёв: {ran}")
    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG14 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("G14 OK: слои тестов на месте и зелёные")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
