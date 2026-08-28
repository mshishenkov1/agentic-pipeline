#!/usr/bin/env python3
"""Прогон quality gates G1–G14 (ТЗ §5). Команды и пороги — из pipeline.config.yaml.

Использование: run_gates.py [--run-id <id>] [--ci]

Пишет reports/gates-<run-id>.json и обновляет секцию gates в state.json.
Код выхода 0 — только если все гейты зелёные.
"""
from __future__ import annotations

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import yaml
except ImportError:
    print("нужен pyyaml: .venv/bin/pip install -e '.[dev]' (запускай через .venv/bin/python)",
          file=sys.stderr)
    sys.exit(2)


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)


def scope_services(cfg: dict) -> list[tuple[str | None, dict]]:
    """Сервисы, по которым гоняются гейты.

    Плоский режим (topology: flat, пустой scope или нет карты сервисов)
    возвращает [(None, {})] — один прогон с глобальными командами, как раньше.
    """
    services = cfg.get("services") or {}
    if cfg.get("topology", "flat") == "flat" or not services:
        return [(None, {})]
    try:
        state = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
        names = (state.get("scope") or {}).get("services") or []
    except Exception:
        names = []
    names = [n for n in names if n in services] or list(services)
    return [(n, services[n]) for n in names]


def cmd_for(cfg: dict, svc: dict, name: str) -> str:
    """Команда сервиса.

    Откат к глобальной секции commands допустим ТОЛЬКО в плоском режиме
    (svc пуст). Иначе сервис на Go унаследовал бы питоновские команды —
    и, например, прогнал бы mutmut по чужому коду.
    """
    if not svc:
        return cfg.get("commands", {}).get(name) or ""
    return (svc.get("commands") or {}).get(name) or ""


def per_service(cfg: dict, run) -> tuple[bool, str]:
    """Прогоняет проверку по каждому сервису области и сводит результат."""
    results, all_ok = [], True
    for name, svc in scope_services(cfg):
        ok, detail = run(svc)
        all_ok &= ok
        results.append(detail if name is None
                       else f"[{name}] {'ok' if ok else 'ПРОВАЛ'}: {detail}")
    return all_ok, " · ".join(results)


def gate_g1_tests(cfg: dict) -> tuple[bool, str]:
    return per_service(cfg, lambda svc: _g1_one(cfg, svc))


def _g1_one(cfg: dict, svc: dict) -> tuple[bool, str]:
    command = cmd_for(cfg, svc, "test")
    if not command:
        return True, "команда тестов не задана — пропущено"
    r = sh(command)
    skipped = 0
    m = re.search(r"(\d+) skipped", r.stdout + r.stderr)
    if m:
        skipped = int(m.group(1))
    justified = (ROOT / "reports" / "skip-justification.md").exists()
    if r.returncode != 0:
        return False, f"тесты красные (rc={r.returncode}): {tail(r.stdout)}"
    if skipped and not justified:
        return False, f"{skipped} skip без обоснования (нет reports/skip-justification.md)"
    return True, f"все тесты зелёные, skip: {skipped}" + (" (обоснованы)" if skipped else "")


def gate_g2_coverage_diff(cfg: dict) -> tuple[bool, str]:
    return per_service(cfg, lambda svc: _g2_one(cfg, svc))


def _g2_one(cfg: dict, svc: dict) -> tuple[bool, str]:
    cov_cmd = cmd_for(cfg, svc, "coverage")
    diff_cmd = cmd_for(cfg, svc, "coverage_diff")
    if not cov_cmd or not diff_cmd:
        return True, "coverage не настроен для сервиса — пропущено"
    r = sh(cov_cmd)
    if r.returncode != 0:
        return False, f"прогон с coverage упал: {tail(r.stdout)}"
    threshold = (svc.get("thresholds") or {}).get(
        "coverage_diff", cfg["thresholds"]["coverage_diff"])
    base = cfg.get("base_branch", "main")
    r = sh(diff_cmd.format(base=base, threshold=threshold))
    m = re.search(r"Coverage: ([\d.]+)%", r.stdout)
    pct = m.group(1) + "%" if m else "n/a"
    if "No lines with coverage information" in r.stdout:
        return True, "нет новых/изменённых строк логики относительно базовой ветки"
    if r.returncode != 0:
        return False, f"coverage-diff {pct} < порога {threshold}%: {tail(r.stdout)}"
    return True, f"coverage-diff {pct} >= {threshold}%"


# Разбор результата мутационного тестирования по инструментам.
# Ключ — значение services.<имя>.mutation_tool; можно задать свой regex
# через services.<имя>.mutation_score_regex (группа 1 = процент).
MUTATION_PARSERS = {
    "gremlins":      r"(?:Mutation score|Test efficacy)[:\s]+([\d.]+)\s*%?",   # Go
    "stryker":       r"Mutation score[:\s]+([\d.]+)",                          # TypeScript
    "pit":           r"Generated \d+ mutations Killed \d+ \(([\d.]+)%\)",     # Java
    "cargo-mutants": r"(\d+)\s+caught",                                        # Rust (доля считается ниже)
}


def gate_g3_mutation(cfg: dict) -> tuple[bool, str]:
    return per_service(cfg, lambda svc: _g3_one(cfg, svc))


def _g3_one(cfg: dict, svc: dict) -> tuple[bool, str]:
    threshold = (svc.get("thresholds") or {}).get(
        "mutation_score", cfg["thresholds"]["mutation_score"])
    mut_cmd = cmd_for(cfg, svc, "mutation")
    if not mut_cmd:
        lang = svc.get("language")
        hint = (f" (для {lang} доступен "
                f"{ {'go': 'gremlins', 'typescript': 'Stryker', 'ts': 'Stryker', 'java': 'PIT', 'rust': 'cargo-mutants'}.get(str(lang).lower(), 'подходящий инструмент') })"
                if lang else "")
        return True, ("мутационное тестирование НЕ НАСТРОЕНО — качество тестов "
                      f"этого сервиса гейтом не проверяется{hint}")
    r = sh(mut_cmd)
    out = r.stdout + r.stderr

    # 1) явный regex сервиса, 2) парсер инструмента, 3) эмодзи mutmut
    custom = svc.get("mutation_score_regex")
    tool = str(svc.get("mutation_tool") or "").lower()
    pattern = custom or MUTATION_PARSERS.get(tool)
    if pattern:
        m = re.search(pattern, out)
        if not m:
            return False, (f"не удалось разобрать результат ({tool or 'свой regex'}): "
                           f"{tail(out)}")
        score = float(m.group(1))
        detail = f"mutation score {score:.1f}% ({tool or 'свой парсер'}), порог {threshold}%"
        return score >= threshold, detail
    # формат прогресс-строки mutmut: 🎉 killed  ⏰ timeout  🤔 suspicious  🙁 survived  🔇 skipped
    counts = {}
    for emoji, name in (("🎉", "killed"), ("⏰", "timeout"), ("🤔", "suspicious"),
                        ("🙁", "survived"), ("🔇", "skipped")):
        matches = re.findall(re.escape(emoji) + r"\s*(\d+)", out)
        counts[name] = int(matches[-1]) if matches else 0
    detected = counts["killed"] + counts["timeout"]
    undetected = counts["survived"] + counts["suspicious"]
    total = detected + undetected
    if total == 0:
        return False, ("не удалось получить статистику mutmut — проверь вручную: "
                       f"{tail(out)}")
    score = 100.0 * detected / total
    detail = (f"mutation score {score:.1f}% (killed {counts['killed']}, timeout {counts['timeout']}, "
              f"survived {counts['survived']}, suspicious {counts['suspicious']}), порог {threshold}%")
    return score >= threshold, detail


def gate_g4_static(cfg: dict) -> tuple[bool, str]:
    return per_service(cfg, lambda svc: _g4_one(cfg, svc))


def _g4_one(cfg: dict, svc: dict) -> tuple[bool, str]:
    lint_cmd = cmd_for(cfg, svc, "lint")
    type_cmd = cmd_for(cfg, svc, "typecheck")
    lint = sh(lint_cmd) if lint_cmd else None
    typing = sh(type_cmd) if type_cmd else None
    problems = []
    if lint is not None and lint.returncode != 0:
        problems.append(f"lint: {tail(lint.stdout)}")
    if typing is not None and typing.returncode != 0:
        problems.append(f"types: {tail(typing.stdout)}")
    return (not problems), ("; ".join(problems) or "линтер и типизация чистые")


def gate_g5_review() -> tuple[bool, str]:
    reviews = sorted((ROOT / "reports").glob("review-*.json"))
    if not reviews:
        return False, "нет ни одного отчёта review-agent (reports/review-*.json)"
    last = reviews[-1]
    try:
        verdict = json.loads(last.read_text(encoding="utf-8")).get("verdict")
    except Exception as e:
        return False, f"не читается {last.name}: {e}"
    return verdict == "approve", f"{last.name}: verdict={verdict}"


def gate_g6_traceability() -> tuple[bool, str]:
    r = sh(f"{sys.executable} scripts/check_ac_traceability.py")
    return r.returncode == 0, tail(r.stdout, 400)


def gate_g7_requirements() -> tuple[bool, str]:
    """Условный гейт: без requirements.md (проект без фазы системного дизайна)
    проверять нечего — гейт не блокирует."""
    r = sh(f"{sys.executable} scripts/check_req_traceability.py")
    return r.returncode == 0, tail(r.stdout, 400)


def gate_g8_architecture() -> tuple[bool, str]:
    """Условный гейт: соответствие архитектуры требованиям и коду, полнота ADR."""
    r = sh(f"{sys.executable} scripts/check_design.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g9_diagrams() -> tuple[bool, str]:
    """Условный гейт: полнота и корректность комплекта диаграмм."""
    r = sh(f"{sys.executable} scripts/check_diagrams.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g10_capacity() -> tuple[bool, str]:
    """Условный гейт: сверка предсказаний нагрузки с измерениями."""
    r = sh(f"{sys.executable} scripts/check_capacity.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g11_migration() -> tuple[bool, str]:
    """Условный гейт: корректность плана миграции."""
    r = sh(f"{sys.executable} scripts/check_migration.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g12_infra() -> tuple[bool, str]:
    """Условный гейт: инфраструктурный код — validate, plan, политики, секреты."""
    r = sh(f"{sys.executable} scripts/check_infra.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g13_contracts() -> tuple[bool, str]:
    """Условный гейт: валидность и обратная совместимость контрактов."""
    r = sh(f"{sys.executable} scripts/check_contracts.py")
    return r.returncode == 0, tail(r.stdout, 500)


def gate_g14_test_layers() -> tuple[bool, str]:
    """Условный гейт: слои тестов существуют, наполнены и исполняются."""
    r = sh(f"{sys.executable} scripts/check_test_layers.py")
    return r.returncode == 0, tail(r.stdout, 500)


def tail(text: str, n: int = 300) -> str:
    text = text.strip()
    return text[-n:].replace("\n", " | ") if text else "(пусто)"


def main(argv: list[str]) -> int:
    run_id = argv[argv.index("--run-id") + 1] if "--run-id" in argv else "manual"
    cfg = yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8"))

    gates = [
        ("G1", "Тесты", lambda: gate_g1_tests(cfg)),
        ("G2", "Coverage-diff", lambda: gate_g2_coverage_diff(cfg)),
        ("G3", "Mutation", lambda: gate_g3_mutation(cfg)),
        ("G4", "Статанализ", lambda: gate_g4_static(cfg)),
        ("G5", "Ревью", gate_g5_review),
        ("G6", "AC-трассировка", gate_g6_traceability),
        ("G7", "Трассировка требований", gate_g7_requirements),
        ("G8", "Архитектура и ADR", gate_g8_architecture),
        ("G9", "Диаграммы", gate_g9_diagrams),
        ("G10", "Нагрузка и калибровка", gate_g10_capacity),
        ("G11", "План миграции", gate_g11_migration),
        ("G12", "Инфраструктура", gate_g12_infra),
        ("G13", "Контракты", gate_g13_contracts),
        ("G14", "Слои тестов", gate_g14_test_layers),
    ]

    results = {}
    all_ok = True
    for gid, name, fn in gates:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"гейт упал с исключением: {e}"
        results[gid] = {"name": name, "passed": ok, "detail": detail}
        all_ok &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {gid} {name}: {detail}\n")

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    report = {"run_id": run_id, "ts": ts, "all_passed": all_ok,
              "topology": cfg.get("topology", "flat"),
              "scope": [n for n, _ in scope_services(cfg) if n],
              "gates": results}
    out = ROOT / "reports" / f"gates-{run_id}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    state_path = ROOT / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["gates"] = {gid: r["passed"] for gid, r in results.items()}
        state.setdefault("history", []).append(
            {"ts": ts, "event": f"gates: {'ALL PASS' if all_ok else 'FAIL'} -> {out.name}"})
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except Exception:
        pass

    print(f"{'ВСЕ ГЕЙТЫ ЗЕЛЁНЫЕ' if all_ok else 'ЕСТЬ КРАСНЫЕ ГЕЙТЫ'} -> {out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
