#!/usr/bin/env python3
"""G8: соответствие архитектуры коду и полнота дизайн-артефактов.

Проверяет:
  1. Карта компонентов: сплошная нумерация C-NN, непустые поля.
  2. Каждый компонент обоснован хотя бы одним требованием, и требование существует.
  3. Зависимости ссылаются на существующие компоненты; циклов нет.
  4. Модуль компонента существует в src/ (если код уже есть).
  5. Обратно: каждый модуль верхнего уровня в src/ числится в карте.
  6. Каждое REQ/NFR покрыто компонентом либо вынесено в «Отложено».
  7. Раздел слабых мест спецификации присутствует.
  8. ADR: сплошная нумерация, обязательные разделы, ссылки на требования,
     корректное вытеснение.

Гейт УСЛОВНЫЙ: без system-design.md печатает «неприменим» и выходит с кодом 0.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "system-design.md"
REQ_FILE = ROOT / "requirements.md"
DECISIONS = ROOT / "decisions"
SRC = ROOT / "src"

COMPONENT = re.compile(r"^(\s{0,3})(#{2,4})\s*(C-\d+)\s*[:.]\s*(.+?)\s*$", re.M)
# [:\t ]* вместо [:\s]* — иначе пустое поле проглатывает следующую строку
FIELD = re.compile(r"^\s*[-*]\s*\*\*(.+?):?\*\*[:\t ]*(.*)$", re.M)
REQ_ID = re.compile(r"(?:REQ|NFR)-\d+")
COMP_ID = re.compile(r"C-\d+")
REQ_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(?:\*\*)?((?:REQ|NFR)-\d+)\b", re.M)
DEFERRED = re.compile(r"^\s{0,3}#{1,6}\s*.*отложено.*$", re.M | re.I)
WEAK = re.compile(r"^\s{0,3}#{1,6}\s*.*слаб\w*\s+мест.*$", re.M | re.I)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)

ADR_FILE = re.compile(r"^ADR-(\d+)")
ADR_SECTIONS = ["контекст", "рассмотренные варианты", "решение", "обоснование",
                "при каких условиях", "последствия"]
ADR_VARIANT = re.compile(r"^\s{0,3}#{3,4}\s*Вариант\s", re.M | re.I)
SUPERSEDES = re.compile(r"вытесня\w*\s+(ADR-\d+)", re.I)
SUPERSEDED = re.compile(r"вытеснен\w*\s+(ADR-\d+)", re.I)

# каталоги в src/, которые не являются компонентами
SRC_IGNORE = {"__pycache__", ".gitkeep"}
SRC_IGNORE_SUFFIX = (".egg-info",)


def fail(errors: list[str], msg: str) -> None:
    errors.append(msg)


def parse_components(text: str) -> dict[str, dict[str, str]]:
    """{C-01: {поле: значение}} в порядке появления.

    Блок компонента заканчивается на следующем заголовке того же или более
    высокого уровня — иначе поля соседних разделов утекают в последний компонент.
    """
    entries = list(COMPONENT.finditer(text))
    out: dict[str, dict[str, str]] = {}
    for i, m in enumerate(entries):
        start = m.end()
        level = len(m.group(2))
        end = len(text)
        for nxt in ANY_HEADING.finditer(text, start):
            hashes = re.match(r"\s{0,3}(#+)", nxt.group(0))
            if hashes and len(hashes.group(1)) <= level:
                end = nxt.start()
                break
        block = text[start:end]
        fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(block)}
        fields["_name"] = m.group(4)
        out[m.group(3)] = fields
    return out


def section_body(text: str, heading_re: re.Pattern) -> str:
    m = heading_re.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = ANY_HEADING.search(rest)
    return rest[: nxt.start()] if nxt else rest


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def walk(node: str, path: list[str]) -> None:
        color[node] = GREY
        for nxt in graph.get(node, []):
            if nxt not in color:
                continue
            if color[nxt] == GREY:
                cycles.append(path[path.index(nxt):] + [nxt])
            elif color[nxt] == WHITE:
                walk(nxt, path + [nxt])
        color[node] = BLACK

    for n in graph:
        if color[n] == WHITE:
            walk(n, [n])
    return cycles


def check_components(text: str, req_ids: set[str], errors: list[str],
                     warnings: list[str]) -> dict[str, dict[str, str]]:
    comps = parse_components(text)
    if not comps:
        fail(errors, "в system-design.md не найдено ни одного компонента вида «### C-01: Название»")
        return comps

    # сплошная нумерация
    nums = sorted(int(c.split("-")[1]) for c in comps)
    expected = list(range(1, len(nums) + 1))
    if nums != expected:
        fail(errors, f"нумерация компонентов не сплошная: {nums}, ожидалось {expected}")

    for cid, f in comps.items():
        if not f.get("зона ответственности"):
            fail(errors, f"{cid}: пустое поле «зона ответственности»")
        elif " и " in f["зона ответственности"].lower().split(";")[0][:80]:
            warnings.append(f"{cid}: зона ответственности содержит «и» — возможно, "
                            f"компонент делает две вещи")

        reqs = REQ_ID.findall(f.get("требования", ""))
        if not reqs:
            fail(errors, f"{cid}: не обоснован ни одним требованием "
                         f"(бюджет сложности: компонент без требования — дефект)")
        for r in reqs:
            if r not in req_ids:
                fail(errors, f"{cid}: ссылается на несуществующее требование {r}")

        if not f.get("модуль"):
            fail(errors, f"{cid}: пустое поле «модуль»")

        external = str(f.get("модуль", "")).lower().startswith("внешн")
        if not external:
            if not f.get("язык"):
                fail(errors, f"{cid}: не указан язык "
                             f"(выбор языка — обязанность архитектора)")
            elif not f.get("обоснование языка"):
                fail(errors, f"{cid}: язык указан без обоснования "
                             f"(«быстрее» и «современнее» обоснованием не считаются)")
        if not f.get("интерфейс"):
            warnings.append(f"{cid}: не описан интерфейс")

    # зависимости и циклы
    graph = {cid: [d for d in COMP_ID.findall(f.get("зависит от", "")) if d != cid]
             for cid, f in comps.items()}
    for cid, deps in graph.items():
        for d in deps:
            if d not in comps:
                fail(errors, f"{cid}: зависит от несуществующего компонента {d}")
    for cyc in find_cycles(graph):
        fail(errors, f"циклическая зависимость компонентов: {' -> '.join(cyc)}")

    return comps


def check_code_conformance(comps: dict[str, dict[str, str]], errors: list[str],
                           warnings: list[str]) -> None:
    if not SRC.exists():
        return
    src_modules = {p.name for p in SRC.iterdir()
                   if p.is_dir() and p.name not in SRC_IGNORE
                   and not p.name.endswith(SRC_IGNORE_SUFFIX)}
    if not src_modules:
        return  # кода ещё нет — проверять нечего

    declared: set[str] = set()
    for cid, f in comps.items():
        mod = f.get("модуль", "").strip().strip("`")
        if not mod or mod.lower().startswith("внешн"):
            continue
        path = ROOT / mod
        if not path.exists():
            fail(errors, f"{cid}: модуль «{mod}» не существует в репозитории")
        parts = Path(mod).parts
        if len(parts) >= 2 and parts[0] == "src":
            declared.add(parts[1])

    for orphan in sorted(src_modules - declared):
        fail(errors, f"модуль src/{orphan} не числится ни в одном компоненте карты "
                     f"(архитектура разошлась с кодом)")


def check_requirements_coverage(text: str, comps: dict[str, dict[str, str]],
                                req_ids: list[str], errors: list[str]) -> None:
    covered: set[str] = set()
    for f in comps.values():
        covered |= set(REQ_ID.findall(f.get("требования", "")))
    deferred = set(REQ_ID.findall(section_body(text, DEFERRED)))
    missing = [r for r in req_ids if r not in covered and r not in deferred]
    if missing:
        fail(errors, f"требования не покрыты компонентами и не вынесены в «Отложено»: "
                     f"{', '.join(missing)}")


def check_adrs(req_ids: set[str], errors: list[str], warnings: list[str]) -> int:
    if not DECISIONS.exists():
        fail(errors, "каталог decisions/ отсутствует")
        return 0
    files = sorted(p for p in DECISIONS.glob("ADR-*.md"))
    if not files:
        fail(errors, "нет ни одного ADR — существенные решения не зафиксированы")
        return 0

    nums, texts = [], {}
    for p in files:
        m = ADR_FILE.match(p.name)
        if not m:
            warnings.append(f"{p.name}: имя не соответствует ADR-NNN-*.md")
            continue
        num = int(m.group(1))
        nums.append(num)
        texts[f"ADR-{m.group(1)}"] = p.read_text(encoding="utf-8")

    if sorted(nums) != list(range(1, len(nums) + 1)):
        warnings.append(f"нумерация ADR не сплошная: {sorted(nums)}")

    for adr, body in texts.items():
        low = body.lower()
        for sec in ADR_SECTIONS:
            if sec not in low:
                fail(errors, f"{adr}: отсутствует обязательный раздел «{sec}»")
        if len(ADR_VARIANT.findall(body)) < 2:
            fail(errors, f"{adr}: рассмотрено меньше двух вариантов "
                         f"(нужны заголовки «### Вариант A/B»)")
        refs = REQ_ID.findall(body)
        if not refs:
            fail(errors, f"{adr}: не ссылается ни на одно требование")
        for r in set(refs):
            if r not in req_ids:
                warnings.append(f"{adr}: ссылается на неизвестное требование {r}")
        for target in SUPERSEDES.findall(body):
            if target not in texts:
                fail(errors, f"{adr}: вытесняет несуществующий {target}")
            elif not SUPERSEDED.search(texts[target]):
                fail(errors, f"{target}: вытеснен {adr}, но статус не обновлён")

    return len(texts)


def main() -> int:
    if not DESIGN.exists():
        print("G8 неприменим: system-design.md отсутствует — "
              "фаза системного дизайна для этого проекта не выполнялась")
        return 0

    text = DESIGN.read_text(encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []

    req_ids_list: list[str] = []
    if REQ_FILE.exists():
        req_ids_list = REQ_HEADING.findall(REQ_FILE.read_text(encoding="utf-8"))
    else:
        warnings.append("requirements.md отсутствует — проверка покрытия требований пропущена")
    req_ids = set(req_ids_list)

    comps = check_components(text, req_ids, errors, warnings)
    if comps:
        check_code_conformance(comps, errors, warnings)
        if req_ids_list:
            check_requirements_coverage(text, comps, req_ids_list, errors)

    if not WEAK.search(text):
        fail(errors, "отсутствует раздел слабых мест спецификации "
                     "(обязателен даже пустой)")

    adr_count = check_adrs(req_ids, errors, warnings)

    print(f"компонентов: {len(comps)} · требований: {len(req_ids)} · ADR: {adr_count}")
    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG8 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nG8 OK: архитектура согласована с требованиями и кодом")
    return 0


if __name__ == "__main__":
    sys.exit(main())
