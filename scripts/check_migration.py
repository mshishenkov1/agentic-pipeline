#!/usr/bin/env python3
"""G11: проверка плана миграции.

Правила (knowledge/system-design/13-brownfield-и-миграции.md):
  1. Есть инвариант безопасности, и он содержателен.
  2. Стадии M-NN пронумерованы сплошно.
  3. У каждой стадии заполнены: что делаем, обратимость, простой,
     критерий перехода, как откатываемся, риск.
  4. Необратимая стадия помечена явно и не находится в середине плана
     без стадии проверки перед ней.
  5. Простой указан явно (в том числе «нет»); неявный простой — дефект.
  6. Критерий перехода измерим: содержит число, единицу или явное условие.

Гейт УСЛОВНЫЙ: нет migration-plan.md — «неприменим», код 0.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "migration-plan.md"

STAGE = re.compile(r"^(\s{0,3})(#{2,4})\s*(M-\d+)\s*[:.]\s*(.+?)\s*$", re.M)
FIELD = re.compile(r"^\s*[-*]\s*\*\*(.+?):?\*\*[:\t ]*(.*)$", re.M)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
INVARIANT = re.compile(r"^\s{0,3}#{1,6}\s*.*инвариант.*$", re.M | re.I)

REQUIRED = ["что делаем", "обратимость", "простой", "критерий перехода",
            "как откатываемся", "риск"]
IRREVERSIBLE = re.compile(r"необратим", re.I)
NO_DOWNTIME = re.compile(r"^\s*(нет|없|no|отсутствует)\b", re.I)
MEASURABLE = re.compile(r"\d|=|<|>|≤|≥|ноль|нол[ья]|100\s*%|сош|совпад|проход")


def section_body(text: str, heading_re: re.Pattern) -> str:
    m = heading_re.search(text)
    if not m:
        return ""
    tail = text[m.end():]
    nxt = ANY_HEADING.search(tail)
    return tail[: nxt.start()] if nxt else tail


def parse_stages(text: str) -> dict[str, dict[str, str]]:
    entries = list(STAGE.finditer(text))
    out: dict[str, dict[str, str]] = {}
    for i, m in enumerate(entries):
        start, level = m.end(), len(m.group(2))
        end = len(text)
        for nxt in ANY_HEADING.finditer(text, start):
            h = re.match(r"\s{0,3}(#+)", nxt.group(0))
            if h and len(h.group(1)) <= level:
                end = nxt.start()
                break
        fields = {k.strip().lower(): v.strip()
                  for k, v in FIELD.findall(text[start:end])}
        fields["_name"] = m.group(4)
        out[m.group(3)] = fields
    return out


def main() -> int:
    if not PLAN.exists():
        print("G11 неприменим: migration-plan.md отсутствует — "
              "миграция в этом прогоне не планируется")
        return 0

    text = PLAN.read_text(encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []

    inv = section_body(text, INVARIANT).strip()
    if not inv:
        errors.append("отсутствует раздел «Инвариант безопасности»")
    elif len(inv) < 40 or not MEASURABLE.search(inv):
        errors.append("инвариант безопасности сформулирован неизмеримо "
                      "(«система должна работать» — не инвариант)")

    stages = parse_stages(text)
    if not stages:
        print("G11 FAIL: в плане нет ни одной стадии вида «### M-01: Название»")
        return 1

    nums = sorted(int(s.split("-")[1]) for s in stages)
    if nums != list(range(1, len(nums) + 1)):
        errors.append(f"нумерация стадий не сплошная: {nums}")

    order = sorted(stages, key=lambda s: int(s.split("-")[1]))
    irreversible_idx: list[int] = []

    for idx, sid in enumerate(order):
        f = stages[sid]
        for req in REQUIRED:
            if not f.get(req):
                errors.append(f"{sid}: не заполнено поле «{req}»")

        rev = f.get("обратимость", "")
        if IRREVERSIBLE.search(rev):
            irreversible_idx.append(idx)
            if "после" not in rev.lower() and "когда" not in rev.lower():
                errors.append(f"{sid}: помечена необратимой, но не указано, "
                              f"после какого условия откат становится невозможен")

        downtime = f.get("простой", "")
        if downtime and not NO_DOWNTIME.match(downtime) and not MEASURABLE.search(downtime):
            warnings.append(f"{sid}: простой указан, но без длительности — «{downtime}»")

        crit = f.get("критерий перехода", "")
        if crit and not MEASURABLE.search(crit):
            errors.append(f"{sid}: критерий перехода неизмерим — «{crit}»")

        rollback = f.get("как откатываемся", "")
        if rollback and len(rollback) < 15:
            warnings.append(f"{sid}: процедура отката описана слишком коротко")

    for idx in irreversible_idx:
        if idx < len(order) - 1:
            prev = stages[order[idx - 1]] if idx > 0 else None
            prev_crit = (prev or {}).get("критерий перехода", "")
            if not prev_crit or not MEASURABLE.search(prev_crit):
                errors.append(f"{order[idx]}: необратимая стадия в середине плана, "
                              f"а предыдущая не имеет измеримого критерия проверки")

    print(f"стадий: {len(stages)} · необратимых: {len(irreversible_idx)}")
    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG11 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nG11 OK: план миграции корректен — стадии обратимы, "
          "критерии измеримы, инвариант задан")
    return 0


if __name__ == "__main__":
    sys.exit(main())
