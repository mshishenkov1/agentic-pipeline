#!/usr/bin/env python3
"""G7: трассировка требований проекта на критерии приёмки.

Продолжает цепочку доказательств вверх: check_ac_traceability.py проверяет
AC -> тест, этот скрипт — REQ/NFR -> AC. Вместе получается сквозная цепочка
требование -> критерий -> проходящий тест.

Гейт УСЛОВНЫЙ: если requirements.md нет (проект без фазы системного дизайна),
скрипт печатает «неприменим» и завершается с кодом 0.

Формат requirements.md (машиночитаемая часть):

    ### REQ-01: Короткое название требования
    ### NFR-03: Пропускная способность

    ## Не верифицируется в этом прогоне
    - NFR-05 — причина: нагрузочное тестирование вне объёма проекта

Формат связи в acceptance-criteria.yaml (поле req у критерия):

    - id: AC-01
      req: REQ-03
      ...
    - id: AC-02
      req: [REQ-03, NFR-01]
      ...

Ненулевой код выхода, если есть требование без критерия, ссылка на
несуществующее требование или освобождение без причины.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQ_FILE = ROOT / "requirements.md"
AC_FILE = ROOT / "acceptance-criteria.yaml"

# заголовок вида «### REQ-01: ...» или «### **NFR-02** ...»
REQ_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(?:\*\*)?((?:REQ|NFR)-\d+)\b", re.M)
# запасная форма — пункт списка «- **REQ-01** ...»
REQ_ITEM = re.compile(r"^\s*[-*]\s+(?:\*\*)?((?:REQ|NFR)-\d+)\b", re.M)
# заголовок секции освобождений
EXEMPT_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*.*не\s+верифицируется.*$", re.M | re.I)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)

AC_ENTRY = re.compile(r"^\s*-\s+id:\s*[\"']?(AC-\d+)[\"']?\s*$", re.M)
REQ_REF = re.compile(r"^\s*req:\s*(.+?)\s*$", re.M)
REQ_ID = re.compile(r"(?:REQ|NFR)-\d+")


def parse_requirements(text: str) -> tuple[list[str], dict[str, str]]:
    """Возвращает (список id требований по порядку, {id освобождённого: причина})."""
    exempt_from = _exempt_section(text)
    body = text[:exempt_from] if exempt_from is not None else text

    ids: list[str] = []
    for pattern in (REQ_HEADING, REQ_ITEM):
        for m in pattern.finditer(body):
            if m.group(1) not in ids:
                ids.append(m.group(1))

    exemptions: dict[str, str] = {}
    if exempt_from is not None:
        section = text[exempt_from:]
        end = ANY_HEADING.search(section, 1)
        section = section[: end.start()] if end else section
        for line in section.splitlines():
            m = REQ_ID.search(line)
            if not m:
                continue
            reason = line[m.end():].strip(" \t—-–:*")
            exemptions[m.group(0)] = reason
    return ids, exemptions


def _exempt_section(text: str) -> int | None:
    m = EXEMPT_HEADING.search(text)
    return m.start() if m else None


def parse_ac_links(text: str) -> dict[str, list[str]]:
    """Возвращает {AC-id: [требования, на которые он ссылается]}."""
    entries = list(AC_ENTRY.finditer(text))
    links: dict[str, list[str]] = {}
    for i, m in enumerate(entries):
        start = m.end()
        end = entries[i + 1].start() if i + 1 < len(entries) else len(text)
        block = text[start:end]
        ref = REQ_REF.search(block)
        links[m.group(1)] = REQ_ID.findall(ref.group(1)) if ref else []
    return links


def main() -> int:
    if not REQ_FILE.exists():
        print("G7 неприменим: requirements.md отсутствует — "
              "фаза системного дизайна для этого проекта не выполнялась")
        return 0

    req_ids, exemptions = parse_requirements(REQ_FILE.read_text(encoding="utf-8"))
    if not req_ids:
        print("G7 FAIL: в requirements.md не найдено ни одного требования "
              "вида REQ-NN / NFR-NN")
        return 1

    if not AC_FILE.exists():
        print("G7 FAIL: requirements.md есть, а acceptance-criteria.yaml нет — "
              "этап SPEC не выполнен")
        return 1

    links = parse_ac_links(AC_FILE.read_text(encoding="utf-8"))

    covered: dict[str, list[str]] = {r: [] for r in req_ids}
    unknown: list[tuple[str, str]] = []
    for ac, refs in links.items():
        for ref in refs:
            if ref in covered:
                covered[ref].append(ac)
            else:
                unknown.append((ref, ac))

    print(f"{'ТРЕБОВАНИЕ':<10} {'AC':<5} критерии")
    for r in req_ids:
        acs = covered[r]
        if acs:
            note = ", ".join(acs)
        elif r in exemptions:
            note = f"— освобождено: {exemptions[r] or 'ПРИЧИНА НЕ УКАЗАНА'}"
        else:
            note = "— НЕ ПОКРЫТО —"
        print(f"{r:<10} {len(acs):<5} {note}")

    ok = True
    missing = [r for r in req_ids if not covered[r] and r not in exemptions]
    if missing:
        print(f"\nG7 FAIL: без критериев приёмки: {', '.join(missing)}")
        ok = False

    blank = [r for r, why in exemptions.items() if not why]
    if blank:
        print(f"\nG7 FAIL: освобождение без причины: {', '.join(blank)}")
        ok = False

    stale = [r for r in exemptions if r not in covered]
    if stale:
        print(f"\nG7 FAIL: освобождены несуществующие требования: {', '.join(stale)}")
        ok = False

    if unknown:
        print("\nG7 FAIL: критерии ссылаются на несуществующие требования:")
        for ref, ac in unknown:
            print(f"  {ref} <- {ac}")
        ok = False

    unlinked = [ac for ac, refs in links.items() if not refs]
    if unlinked:
        print(f"\nG7 FAIL: критерии без ссылки на требование (поле req): "
              f"{', '.join(sorted(unlinked))}")
        ok = False

    if ok:
        exempt_note = f", освобождено {len(exemptions)}" if exemptions else ""
        print(f"\nG7 OK: все {len(req_ids)} требований покрыты критериями{exempt_note}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
