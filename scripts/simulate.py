#!/usr/bin/env python3
"""Уровень 7: симуляция нагрузки по модели массового обслуживания.

Читает раздел «## Модель нагрузки» из system-design.md и отвечает на вопрос
«где насытится и при каком RPS» ДО постройки системы.

Модель: каждый узел — система M/M/c. Считается загрузка, ожидание в очереди
по формуле Эрланга C, задержка сценария по цепочке, точка насыщения.

Использование:
  simulate.py                 — прогон по модели из system-design.md
  simulate.py --sweep         — развёртка по RPS, поиск точки насыщения
  simulate.py --json          — машиночитаемый вывод

Модель — фильтр здравого смысла, а не предсказание: она ловит «дизайн упрётся
в 300 rps при требовании 5000» и не знает про сборку мусора, троттлинг и кэши.
Расхождение с измерением вдвое — норма, на порядок — ошибка в модели или дизайне.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "system-design.md"

MODEL_SECTION = re.compile(r"^\s{0,3}#{1,6}\s*.*модель\s+нагрузки.*$", re.M | re.I)
YAML_BLOCK = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


def erlang_c(c: int, a: float) -> float:
    """Вероятность ожидания в очереди для M/M/c. a = λ/μ (предложенная нагрузка)."""
    if c <= 0:
        return 1.0
    rho = a / c
    if rho >= 1:
        return 1.0
    # сумма ряда
    s = sum(a ** k / math.factorial(k) for k in range(c))
    last = a ** c / (math.factorial(c) * (1 - rho))
    return last / (s + last)


def node_latency(service_ms: float, concurrency: int, arrival_rps: float) -> tuple[float, float]:
    """Возвращает (полная задержка узла в мс, загрузка ρ)."""
    if service_ms <= 0:
        return 0.0, 0.0
    mu = 1000.0 / service_ms          # запросов в секунду на один обработчик
    a = arrival_rps / mu              # предложенная нагрузка в эрлангах
    rho = a / concurrency if concurrency else float("inf")
    if rho >= 1:
        return float("inf"), rho
    pw = erlang_c(concurrency, a)
    wq_ms = (pw / (concurrency * mu - arrival_rps)) * 1000.0 if concurrency * mu > arrival_rps else float("inf")
    return service_ms + wq_ms, rho


def simulate_scenario(sc: dict, rps: float | None = None) -> dict:
    lam = rps if rps is not None else float(sc.get("rps", 0))
    total_ms = 0.0
    nodes = []
    saturated = None
    for step in sc.get("chain", []):
        share = float(step.get("share", 1.0))
        arrival = lam * share
        lat, rho = node_latency(float(step.get("service_ms", 0)),
                                int(step.get("concurrency", 1)), arrival)
        nodes.append({
            "component": step.get("component", "?"),
            "arrival_rps": round(arrival, 1),
            "utilization": round(rho, 3) if rho != float("inf") else None,
            "latency_ms": round(lat, 1) if lat != float("inf") else None,
            "async": bool(step.get("async", False)),
        })
        if rho >= 1 and saturated is None:
            saturated = step.get("component", "?")
        if not step.get("async", False) and lat != float("inf"):
            total_ms += lat

    busiest = max((n for n in nodes if n["utilization"] is not None),
                  key=lambda n: n["utilization"], default=None)
    return {
        "scenario": sc.get("id", "?"),
        "rps": lam,
        "latency_ms": round(total_ms, 1) if saturated is None else None,
        "saturated_at": saturated,
        "bottleneck": busiest["component"] if busiest else None,
        "bottleneck_utilization": busiest["utilization"] if busiest else None,
        "nodes": nodes,
    }


def find_saturation(sc: dict) -> float:
    """Минимальный RPS, при котором любой узел достигает ρ >= 1 (бинарный поиск)."""
    lo, hi = 1.0, max(float(sc.get("rps", 100)) * 100, 1000.0)
    if simulate_scenario(sc, lo)["saturated_at"]:
        return lo
    if not simulate_scenario(sc, hi)["saturated_at"]:
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if simulate_scenario(sc, mid)["saturated_at"]:
            hi = mid
        else:
            lo = mid
    return round(lo, 1)


def load_model() -> dict | None:
    if not DESIGN.exists():
        return None
    text = DESIGN.read_text(encoding="utf-8")
    m = MODEL_SECTION.search(text)
    if not m:
        return None
    tail = text[m.end():]
    nxt = re.search(r"^\s{0,3}#{1,6}\s", tail, re.M)
    body = tail[: nxt.start()] if nxt else tail
    blocks = YAML_BLOCK.findall(body)
    if not blocks:
        return None
    try:
        import yaml
        return yaml.safe_load(blocks[0])
    except ImportError:
        print("нужен pyyaml — запускай через .venv/bin/python", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"модель нагрузки не разобрана: {e}", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    sweep = "--sweep" in argv

    data = load_model()
    if data is None:
        msg = ("симуляция неприменима: нет раздела «## Модель нагрузки» "
               "с блоком YAML в system-design.md")
        print(json.dumps({"applicable": False, "reason": msg}) if as_json else msg)
        return 0

    results = []
    for sc in data.get("model", data).get("scenarios", []):
        r = simulate_scenario(sc)
        r["saturation_rps"] = find_saturation(sc)
        headroom = (r["saturation_rps"] / sc.get("rps", 1)) if sc.get("rps") else None
        r["headroom_x"] = round(headroom, 2) if headroom else None
        results.append(r)

    if as_json:
        print(json.dumps({"applicable": True, "scenarios": results},
                         ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print(f"\n▸ {r['scenario']} при {r['rps']:.0f} rps")
        if r["saturated_at"]:
            print(f"  ⚠ НАСЫЩЕНИЕ уже на целевой нагрузке: узел {r['saturated_at']}")
        else:
            print(f"  задержка по модели: {r['latency_ms']} мс")
        print(f"  узкое место: {r['bottleneck']} (загрузка {r['bottleneck_utilization']})")
        print(f"  точка насыщения: {r['saturation_rps']} rps"
              + (f" — запас ×{r['headroom_x']}" if r["headroom_x"] else ""))
        if r["headroom_x"] and r["headroom_x"] < 2:
            print(f"  ⚠ запас меньше ×2 — при отклонении профиля нагрузки система ляжет")
        if sweep:
            for k in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0):
                probe = simulate_scenario(
                    next(s for s in data.get("model", data)["scenarios"]
                         if s.get("id") == r["scenario"]), r["rps"] * k)
                lat = probe["latency_ms"]
                print(f"    ×{k:<4} = {r['rps'] * k:>7.0f} rps → "
                      + (f"{lat:>7.1f} мс" if lat is not None
                         else f"насыщение на {probe['saturated_at']}"))

    print("\nМодель — фильтр здравого смысла, а не предсказание: "
          "расхождение с измерением вдвое нормально, на порядок — ошибка.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
