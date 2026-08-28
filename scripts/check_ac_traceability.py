#!/usr/bin/env python3
"""G6: трассировка критериев приёмки на тесты.

Каждый AC из acceptance-criteria.yaml должен иметь минимум один тест,
помеченный маркером критерия. Печатает таблицу AC -> тесты.

Маркер зависит от языка сервиса и задаётся в pipeline.config.yaml:
  python  @pytest.mark.ac("AC-01")     (умолчание, плоский режим)
  go      // ac:AC-01
  ts/js   // ac:AC-01
  java    // ac:AC-01

В мультисервисном режиме тесты ищутся внутри каждого сервиса по его
test_file_glob; сервис может переопределить ac_marker и test_def.

Ненулевой код выхода, если есть непокрытые AC или маркеры на несуществующие AC.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

AC_ID = re.compile(r"^\s*-\s+id:\s*[\"']?(AC-\d+)[\"']?", re.M)
MARKER = re.compile(r"pytest\.mark\.ac\(\s*[\"'](AC-\d+)[\"']\s*\)")
TEST_DEF = re.compile(r"^(?:async\s+)?def\s+(test_\w+)", re.M)

# Умолчания по языкам: маркер критерия, маска файлов тестов, объявление теста.
LANG_DEFAULTS = {
    "python": {
        "ac_marker": MARKER.pattern,
        "test_file_glob": "test_*.py",
        "test_def": TEST_DEF.pattern,
    },
    "go": {
        "ac_marker": r"//\s*ac:\s*(AC-\d+)",
        "test_file_glob": "*_test.go",
        "test_def": r"^func\s+(Test\w+)",
    },
    "typescript": {
        "ac_marker": r"//\s*ac:\s*(AC-\d+)",
        "test_file_glob": "*.test.ts",
        "test_def": r"(?:it|test)\(\s*[\"'`]([^\"'`]+)",
    },
    "java": {
        "ac_marker": r"//\s*ac:\s*(AC-\d+)",
        "test_file_glob": "*Test.java",
        "test_def": r"(?:public\s+)?void\s+(\w+)\s*\(",
    },
}
for _alias in ("ts", "js", "javascript"):
    LANG_DEFAULTS[_alias] = LANG_DEFAULTS["typescript"]


def load_cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load(
            (ROOT / "pipeline.config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def test_sources(cfg: dict) -> list:
    """[(имя области, каталог тестов, правила разбора)] по сервисам или плоско."""
    services = cfg.get("services") or {}
    if cfg.get("topology", "flat") == "flat" or not services:
        return [("", ROOT / "tests", LANG_DEFAULTS["python"])]
    out = []
    for name, svc in services.items():
        lang = str(svc.get("language", "python")).lower()
        rules = dict(LANG_DEFAULTS.get(lang, LANG_DEFAULTS["python"]))
        for key in ("ac_marker", "test_file_glob", "test_def"):
            if svc.get(key):
                rules[key] = svc[key]
        out.append((name, ROOT / str(svc.get("path", name)) / "tests", rules))
    return out



def main() -> int:
    ac_file = ROOT / "acceptance-criteria.yaml"
    if not ac_file.exists():
        print("acceptance-criteria.yaml не найден — этап SPEC не выполнен")
        return 1
    ac_ids = AC_ID.findall(ac_file.read_text(encoding="utf-8"))
    if not ac_ids:
        print("в acceptance-criteria.yaml нет ни одного критерия с id AC-NN")
        return 1

    mapping: dict[str, list[str]] = {ac: [] for ac in ac_ids}
    unknown: list[tuple[str, str]] = []

    cfg = load_cfg()
    scanned = 0
    for area, tests_dir, rules in test_sources(cfg):
        if not tests_dir.exists():
            continue
        marker = re.compile(rules["ac_marker"])
        test_def = re.compile(rules["test_def"], re.M)
        for test_file in sorted(tests_dir.rglob(rules["test_file_glob"])):
            scanned += 1
            text = test_file.read_text(encoding="utf-8")
            # сопоставляем маркеры с ближайшим следующим объявлением теста
            for m in marker.finditer(text):
                tail = text[m.end():]
                d = test_def.search(tail)
                rel = str(test_file.relative_to(ROOT))
                test_name = f"{rel}::{d.group(1)}" if d else rel
                if m.group(1) in mapping:
                    mapping[m.group(1)].append(test_name)
                else:
                    unknown.append((m.group(1), test_name))
    if scanned == 0:
        print("G6 FAIL: не найдено ни одного файла тестов "
              "(проверь пути сервисов и test_file_glob)")
        return 1

    print(f"{'AC':<8} {'тестов':<7} тесты")
    for ac in ac_ids:
        tests = mapping[ac]
        print(f"{ac:<8} {len(tests):<7} {', '.join(tests) if tests else '— НЕ ПОКРЫТ —'}")

    ok = True
    missing = [ac for ac in ac_ids if not mapping[ac]]
    if missing:
        print(f"\nG6 FAIL: без тестов: {', '.join(missing)}")
        ok = False
    if unknown:
        print("\nG6 FAIL: маркеры на несуществующие AC:")
        for ac, t in unknown:
            print(f"  {ac} <- {t}")
        ok = False
    if ok:
        print(f"\nG6 OK: все {len(ac_ids)} критериев покрыты тестами")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
