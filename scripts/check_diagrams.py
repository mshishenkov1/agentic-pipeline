#!/usr/bin/env python3
"""G9: полнота и корректность диаграмм в system-design.md.

Проверяет:
  1. Присутствие обязательного комплекта: контекст, контейнеры, топология,
     sequence, модель данных, матрица деградации.
  2. Подписанность стрелок на графовых диаграммах («зачем» и «как»).
  3. Идентификаторы C-NN на диаграмме контейнеров совпадают с картой компонентов.
  4. Ветки ошибок в sequence-диаграммах.
  5. Синтаксическую валидность — рендером через mmdc, если он доступен.

Гейт УСЛОВНЫЙ: без system-design.md печатает «неприменим» и выходит с кодом 0.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "system-design.md"

FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)
COMPONENT = re.compile(r"^\s{0,3}#{2,4}\s*(C-\d+)\s*[:.]", re.M)
COMP_ID = re.compile(r"C-\d+")
DEGRADATION = re.compile(r"^\s{0,3}#{1,6}\s*.*(деград|отказ\w*\s+и\s+границ).*$", re.M | re.I)

# стрелка со подписью: -->|текст| или -.->|текст|
LABELLED = re.compile(r"-{1,2}\.?-{1,2}>\s*\|[^|]+\|")
# любая стрелка в графовой нотации
ANY_ARROW = re.compile(r"^[^%\n]*?(-{2,3}>|-\.->|={2,3}>)", re.M)


def classify(block: str) -> str:
    head = block.strip().lower()
    if head.startswith("sequencediagram"):
        return "sequence"
    if head.startswith("erdiagram"):
        return "er"
    if head.startswith("statediagram"):
        return "state"
    if head.startswith(("graph", "flowchart")):
        low = block.lower()
        if any(k in low for k in ("зона", "регион", "availability", "az", "ячейк", "реплик")):
            return "deployment"
        if COMP_ID.search(block):
            return "containers"
        return "context"
    if head.startswith("c4context"):
        return "context"
    if head.startswith("c4container"):
        return "containers"
    return "other"


def check_arrows(block: str, kind: str, name: str, errors: list[str]) -> None:
    if kind in ("sequence", "er", "state"):
        return
    arrows = ANY_ARROW.findall(block)
    labelled = LABELLED.findall(block)
    if arrows and len(labelled) < len(arrows):
        errors.append(f"{name}: {len(arrows) - len(labelled)} стрелок без подписи "
                      f"(каждая стрелка обязана отвечать «зачем» и «как»)")


def render_check(blocks: list[str]) -> tuple[bool, str]:
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return True, "mmdc не установлен — проверка рендером пропущена " \
                     "(поставить: npm i -g @mermaid-js/mermaid-cli)"
    bad = []
    with tempfile.TemporaryDirectory() as td:
        for i, b in enumerate(blocks, 1):
            src = Path(td) / f"d{i}.mmd"
            src.write_text(b, encoding="utf-8")
            r = subprocess.run([mmdc, "-i", str(src), "-o", str(Path(td) / f"d{i}.svg")],
                               capture_output=True, text=True)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()
                bad.append(f"блок {i}: {tail[-1] if tail else 'ошибка рендера'}")
    if bad:
        return False, "не отрендерились: " + "; ".join(bad)
    return True, f"все {len(blocks)} блоков отрендерились"


def main() -> int:
    if not DESIGN.exists():
        print("G9 неприменим: system-design.md отсутствует")
        return 0

    text = DESIGN.read_text(encoding="utf-8")
    blocks = FENCE.findall(text)
    errors: list[str] = []
    warnings: list[str] = []

    if not blocks:
        print("G9 FAIL: в system-design.md нет ни одной диаграммы Mermaid")
        return 1

    kinds: dict[str, int] = {}
    for i, b in enumerate(blocks, 1):
        k = classify(b)
        kinds[k] = kinds.get(k, 0) + 1
        check_arrows(b, k, f"диаграмма {i} ({k})", errors)

    required = {
        "context": "контекст C4 L1",
        "containers": "контейнеры C4 L2",
        "deployment": "топология развёртывания",
        "sequence": "sequence на ключевой сценарий",
        "er": "модель данных",
    }
    for key, human in required.items():
        if not kinds.get(key):
            errors.append(f"отсутствует обязательная диаграмма: {human}")

    if not DEGRADATION.search(text):
        errors.append("отсутствует матрица деградации "
                      "(раздел с описанием поведения при отказах)")

    # sequence обязан содержать ветку ошибки
    for i, b in enumerate(blocks, 1):
        if classify(b) == "sequence" and "alt" not in b.lower():
            warnings.append(f"диаграмма {i} (sequence): нет ветки alt — "
                            f"сценарий ошибки не показан")

    # C-NN на диаграмме контейнеров против карты компонентов
    map_ids = set(COMPONENT.findall(text))
    diagram_ids: set[str] = set()
    for b in blocks:
        if classify(b) == "containers":
            diagram_ids |= set(COMP_ID.findall(b))
    if map_ids and diagram_ids:
        only_diagram = diagram_ids - map_ids
        only_map = map_ids - diagram_ids
        if only_diagram:
            errors.append(f"на диаграмме контейнеров есть компоненты, которых нет "
                          f"в карте: {', '.join(sorted(only_diagram))}")
        if only_map:
            warnings.append(f"в карте есть компоненты, не показанные на диаграмме "
                            f"контейнеров: {', '.join(sorted(only_map))}")

    ok_render, render_msg = render_check(blocks)
    if not ok_render:
        errors.append(render_msg)

    print(f"диаграмм: {len(blocks)} — " +
          ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())))
    print(f"рендер: {render_msg}")
    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG9 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nG9 OK: комплект диаграмм полон и корректен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
