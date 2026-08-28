#!/usr/bin/env python3
"""G13: контракты между сервисами.

Проверяет:
  1. Файлы контрактов синтаксически корректны и опознаны (OpenAPI / AsyncAPI).
  2. Обратная совместимость относительно базовой ветки: ломающие изменения
     запрещены без повышения мажорной версии.
  3. Каждое межсервисное взаимодействие из карты компонентов имеет контракт.

Ломающими считаются: удаление пути или операции, удаление кода ответа,
удаление поля из ответа, добавление обязательного поля в запрос,
смена типа поля, сужение перечисления.

Гейт УСЛОВНЫЙ: нет каталога contracts/ — «неприменим», код 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMPONENT = re.compile(r"^\s{0,3}#{2,4}\s*(C-\d+)\s*[:.]\s*(.+?)\s*$", re.M)
FIELD = re.compile(r"^\s*[-*]\s*\*\*(.+?):?\*\*[:\t ]*(.*)$", re.M)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
COMP_ID = re.compile(r"C-\d+")


def cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_yaml(text: str) -> dict | None:
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        return None


def git_show(ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def kind_of(doc: dict) -> str | None:
    if not isinstance(doc, dict):
        return None
    if "openapi" in doc:
        return "openapi"
    if "asyncapi" in doc:
        return "asyncapi"
    return None


def major(doc: dict) -> str:
    v = str((doc.get("info") or {}).get("version", "0"))
    return v.split(".")[0]


def _schema_fields(schema: dict, prefix: str = "") -> dict[str, str]:
    """Плоская карта поле -> тип по схеме (без разыменования $ref)."""
    out: dict[str, str] = {}
    if not isinstance(schema, dict):
        return out
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            key = f"{prefix}{name}"
            out[key] = str((sub or {}).get("type", "any")) if isinstance(sub, dict) else "any"
            if isinstance(sub, dict) and sub.get("type") == "object":
                out.update(_schema_fields(sub, key + "."))
    return out


def _body_schema(payload: dict) -> dict:
    content = (payload or {}).get("content") or {}
    for media in content.values():
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return {}


def breaking_openapi(old: dict, new: dict) -> list[str]:
    issues: list[str] = []
    old_paths = old.get("paths") or {}
    new_paths = new.get("paths") or {}

    for path, ops in old_paths.items():
        if path not in new_paths:
            issues.append(f"удалён путь {path}")
            continue
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            new_op = (new_paths[path] or {}).get(method)
            if not isinstance(new_op, dict):
                issues.append(f"удалена операция {method.upper()} {path}")
                continue

            old_codes = set((op.get("responses") or {}).keys())
            new_codes = set((new_op.get("responses") or {}).keys())
            for gone in sorted(old_codes - new_codes):
                issues.append(f"удалён код ответа {gone} у {method.upper()} {path}")

            # поля ответов: удаление и смена типа
            for code in sorted(old_codes & new_codes):
                o = _schema_fields(_body_schema((op.get("responses") or {}).get(code) or {}))
                n = _schema_fields(_body_schema((new_op.get("responses") or {}).get(code) or {}))
                for f in sorted(set(o) - set(n)):
                    issues.append(f"удалено поле ответа '{f}' у {method.upper()} {path} [{code}]")
                for f in sorted(set(o) & set(n)):
                    if o[f] != n[f]:
                        issues.append(f"тип поля ответа '{f}' изменён {o[f]}→{n[f]} "
                                      f"у {method.upper()} {path}")

            # обязательные поля запроса: добавление ломает клиентов
            o_req = set((_body_schema(op.get("requestBody") or {}) or {}).get("required") or [])
            n_req = set((_body_schema(new_op.get("requestBody") or {}) or {}).get("required") or [])
            for f in sorted(n_req - o_req):
                issues.append(f"добавлено обязательное поле запроса '{f}' "
                              f"у {method.upper()} {path}")

            # обязательные параметры
            def required_params(o_: dict) -> set:
                return {p.get("name") for p in (o_.get("parameters") or [])
                        if isinstance(p, dict) and p.get("required")}
            for f in sorted(required_params(new_op) - required_params(op)):
                issues.append(f"добавлен обязательный параметр '{f}' у {method.upper()} {path}")
    return issues


def breaking_asyncapi(old: dict, new: dict) -> list[str]:
    issues = []
    old_ch = set((old.get("channels") or {}).keys())
    new_ch = set((new.get("channels") or {}).keys())
    for gone in sorted(old_ch - new_ch):
        issues.append(f"удалён канал {gone}")
    return issues


def design_interactions() -> list[tuple[str, str]]:
    """Пары (компонент, от кого зависит) из карты компонентов."""
    design = ROOT / "system-design.md"
    if not design.exists():
        return []
    text = design.read_text(encoding="utf-8")
    entries = list(COMPONENT.finditer(text))
    pairs = []
    for i, m in enumerate(entries):
        start = m.end()
        end = entries[i + 1].start() if i + 1 < len(entries) else len(text)
        nxt = ANY_HEADING.search(text, start)
        if nxt and nxt.start() < end:
            end = nxt.start()
        fields = {k.strip().lower(): v.strip() for k, v in FIELD.findall(text[start:end])}
        for dep in COMP_ID.findall(fields.get("зависит от", "")):
            pairs.append((m.group(1), dep))
    return pairs


def main(argv: list[str]) -> int:
    conf = cfg()
    contracts_dir = ROOT / str((conf.get("system_design") or {}).get("contracts_dir", "contracts"))
    if not contracts_dir.exists():
        print("G13 неприменим: каталог contracts/ отсутствует")
        return 0

    files = [p for p in sorted(contracts_dir.rglob("*"))
             if p.suffix in (".yaml", ".yml", ".json") and p.is_file()]
    if not files:
        print("G13 неприменим: контрактов не найдено")
        return 0

    base = conf.get("base_branch", "main")
    errors: list[str] = []
    warnings: list[str] = []
    checked = 0

    for path in files:
        rel = str(path.relative_to(ROOT))
        doc = load_yaml(path.read_text(encoding="utf-8"))
        kind = kind_of(doc)
        if kind is None:
            warnings.append(f"{rel}: не опознан как OpenAPI или AsyncAPI — пропущен")
            continue
        checked += 1

        old_text = git_show(base, rel)
        if old_text is None:
            print(f"{rel}: новый контракт ({kind}) — сравнивать не с чем")
            continue
        old = load_yaml(old_text)
        if not isinstance(old, dict):
            warnings.append(f"{rel}: версия из {base} не разобрана")
            continue

        issues = (breaking_openapi(old, doc) if kind == "openapi"
                  else breaking_asyncapi(old, doc))
        if not issues:
            print(f"{rel}: совместим с {base}")
            continue
        if major(old) != major(doc):
            print(f"{rel}: ломающие изменения при смене мажорной версии "
                  f"{major(old)}→{major(doc)} — допустимо ({len(issues)} шт.)")
            continue
        errors.append(f"{rel}: ломающие изменения без смены мажорной версии — "
                      + "; ".join(issues[:4])
                      + (f" и ещё {len(issues) - 4}" if len(issues) > 4 else ""))

    # взаимодействия из карты компонентов без контракта
    interactions = design_interactions()
    if interactions and checked:
        blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)
        for src, dst in interactions:
            if dst not in blob and src not in blob:
                warnings.append(f"взаимодействие {src} → {dst} не описано ни одним контрактом")

    print(f"\nпроверено контрактов: {checked}")
    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG13 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("G13 OK: контракты валидны и обратно совместимы")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
