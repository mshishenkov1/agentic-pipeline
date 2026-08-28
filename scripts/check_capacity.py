#!/usr/bin/env python3
"""G10: сверка предсказаний нагрузки с измерениями и накопление калибровки.

Читает раздел «## Предсказания нагрузки» из system-design.md и последний отчёт
из reports/perf/*.json, сравнивает и дописывает reports/calibration.jsonl.

Провал гейта:
  - расхождение задержки или точки насыщения больше допустимого по confidence;
  - предсказанное узкое место не совпало с наблюдаемым (модель системы неверна);
  - доля ошибок выше error_budget_pct;
  - предсказание не покрыто ни одним измерением;
  - предсказание с confidence: high промахнулось.

Гейт УСЛОВНЫЙ: нет предсказаний — «неприменим», код 0.
Есть предсказания, но нет измерений — предупреждение, код 0 (нагрузочный прогон
может выполняться отдельно от прогона гейтов). Есть и то и другое — сверка.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "system-design.md"
PERF_DIR = ROOT / "reports" / "perf"
CALIBRATION = ROOT / "reports" / "calibration.jsonl"

PRED_SECTION = re.compile(r"^\s{0,3}#{1,6}\s*.*предсказани\w*\s+нагрузки.*$", re.M | re.I)
YAML_BLOCK = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)
ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.M)

TOLERANCE = {"high": 1.5, "medium": 2.0, "low": 3.0}


def load_predictions() -> list[dict] | None:
    if not DESIGN.exists():
        return None
    text = DESIGN.read_text(encoding="utf-8")
    m = PRED_SECTION.search(text)
    if not m:
        return None
    tail = text[m.end():]
    nxt = ANY_HEADING.search(tail)
    body = tail[: nxt.start()] if nxt else tail
    blocks = YAML_BLOCK.findall(body)
    if not blocks:
        return None
    try:
        import yaml
    except ImportError:
        print("нужен pyyaml — запускай через .venv/bin/python", file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(blocks[0]) or {}
    return data.get("predictions", data if isinstance(data, list) else [])


def latest_perf() -> dict | None:
    if not PERF_DIR.exists():
        return None
    files = sorted(PERF_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception as e:
        print(f"не читается {files[-1].name}: {e}", file=sys.stderr)
        return None


def ratio(predicted: float | None, measured: float | None) -> float | None:
    """Во сколько раз разошлись, всегда >= 1."""
    if not predicted or not measured or predicted <= 0 or measured <= 0:
        return None
    return max(predicted / measured, measured / predicted)


def main() -> int:
    preds = load_predictions()
    if not preds:
        print("G10 неприменим: в system-design.md нет раздела «Предсказания нагрузки»")
        return 0

    perf = latest_perf()
    if perf is None:
        print(f"G10: предсказаний {len(preds)}, измерений нет "
              f"(reports/perf/*.json пуст) — сверка отложена")
        print("  предупреждение: нефункциональные требования остаются непроверенными")
        return 0

    measured = {m.get("prediction_id"): m for m in perf.get("measurements", [])}
    errors: list[str] = []
    warnings: list[str] = []
    records: list[dict] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"окружение прогона: {perf.get('environment', 'не указано')}")
    print(f"{'ID':<6} {'сценарий':<18} {'p95 пред/факт':<18} "
          f"{'насыщение пред/факт':<22} вердикт")

    for p in preds:
        pid = p.get("id", "?")
        conf = str(p.get("confidence", "medium")).lower()
        tol = TOLERANCE.get(conf, 2.0)
        m = measured.get(pid)

        if not m:
            fail = f"{pid}: предсказание не покрыто ни одним измерением"
            errors.append(fail)
            print(f"{pid:<6} {str(p.get('scenario','?')):<18} {'—':<18} {'—':<22} НЕТ ИЗМЕРЕНИЯ")
            continue

        p95_r = ratio(p.get("p95_ms"), m.get("p95_ms"))
        sat_r = ratio(p.get("saturation_rps"), m.get("saturation_rps"))
        verdict = []

        if p95_r and p95_r > tol:
            verdict.append(f"p95 разошёлся ×{p95_r:.1f} при допуске ×{tol}")
        if sat_r and sat_r > tol:
            verdict.append(f"точка насыщения разошлась ×{sat_r:.1f} при допуске ×{tol}")

        pb = (p.get("bottleneck") or "").strip().lower()
        ob = (m.get("observed_bottleneck") or "").strip().lower()
        bottleneck_ok = True
        if pb and ob:
            key = re.match(r"(c-\d+)", pb)
            bottleneck_ok = (key.group(1) in ob) if key else (pb[:12] in ob or ob[:12] in pb)
            if not bottleneck_ok:
                verdict.append(f"узкое место не совпало: ждали «{p.get('bottleneck')}», "
                               f"получили «{m.get('observed_bottleneck')}» — модель системы неверна")

        budget = float(p.get("error_budget_pct", 1.0))
        if float(m.get("errors_pct", 0)) > budget:
            verdict.append(f"ошибок {m.get('errors_pct')}% при бюджете {budget}%")

        if verdict and conf == "high":
            verdict.append("заявлена высокая уверенность")

        status = "OK" if not verdict else "ПРОВАЛ"
        p95_cell = f"{p.get('p95_ms')}/{m.get('p95_ms')}"
        sat_cell = f"{p.get('saturation_rps')}/{m.get('saturation_rps')}"
        print(f"{pid:<6} {str(p.get('scenario', '?')):<18} "
              f"{p95_cell:<18} {sat_cell:<22} {status}")
        for v in verdict:
            print(f"       └ {v}")
            errors.append(f"{pid}: {v}")

        records.append({
            "ts": ts,
            "run_id": perf.get("run_id"),
            "commit": perf.get("commit"),
            "environment": perf.get("environment"),
            "prediction_id": pid,
            "scenario": p.get("scenario"),
            "confidence": conf,
            "predicted": {"p95_ms": p.get("p95_ms"), "p99_ms": p.get("p99_ms"),
                          "saturation_rps": p.get("saturation_rps"),
                          "bottleneck": p.get("bottleneck")},
            "measured": {"p95_ms": m.get("p95_ms"), "p99_ms": m.get("p99_ms"),
                         "saturation_rps": m.get("saturation_rps"),
                         "bottleneck": m.get("observed_bottleneck"),
                         "errors_pct": m.get("errors_pct")},
            "ratio": {"p95": round(p95_r, 2) if p95_r else None,
                      "saturation": round(sat_r, 2) if sat_r else None},
            "bottleneck_match": bottleneck_ok,
            "passed": not verdict,
        })

    if records:
        CALIBRATION.parent.mkdir(parents=True, exist_ok=True)
        with open(CALIBRATION, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nв журнал калибровки дописано записей: {len(records)} "
              f"({CALIBRATION.relative_to(ROOT)})")
        summarize_calibration()

    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG10 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nG10 OK: предсказания подтверждены измерениями")
    return 0


def summarize_calibration() -> None:
    """Систематическая ошибка по журналу — её архитектор обязан учитывать."""
    if not CALIBRATION.exists():
        return
    rows = []
    for line in CALIBRATION.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if len(rows) < 3:
        return
    biases = []
    for r in rows:
        p, m = r.get("predicted", {}), r.get("measured", {})
        if p.get("p95_ms") and m.get("p95_ms"):
            biases.append(m["p95_ms"] / p["p95_ms"])
    if biases:
        avg = sum(biases) / len(biases)
        direction = "занижает" if avg > 1 else "завышает"
        print(f"калибровка по {len(biases)} наблюдениям: архитектор {direction} "
              f"p95 в среднем в {max(avg, 1 / avg):.1f} раза "
              f"— учитывать при следующем предсказании")
    matches = [r for r in rows if r.get("bottleneck_match") is not None]
    if matches:
        hit = sum(1 for r in matches if r["bottleneck_match"])
        print(f"узкое место угадано в {hit} из {len(matches)} случаев")


if __name__ == "__main__":
    sys.exit(main())
