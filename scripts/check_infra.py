#!/usr/bin/env python3
"""G12: проверка инфраструктурного кода.

Проверяет:
  1. validate — синтаксис и схема корректны;
  2. plan — не содержит удаления ресурсов (если не разрешено явно);
  3. policy — политики (conftest/OPA) пройдены;
  4. секретов в коде нет;
  5. среда, для которой построен план, входит в infra.apply_allowed.

Использование:
  check_infra.py [--env <среда>] [--allow-destroy]

Гейт УСЛОВНЫЙ: пустой infra.path — «неприменим», код 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Признаки удаления ресурса в выводе terraform plan
DESTROY_MARKERS = [
    re.compile(r"will be destroyed", re.I),
    re.compile(r"must be replaced", re.I),
    re.compile(r"-/\+\s+destroy and then create", re.I),
    re.compile(r"Plan:\s*\d+\s+to add,\s*\d+\s+to change,\s*([1-9]\d*)\s+to destroy", re.I),
]

# Секреты в инфраструктурном коде
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "ключ доступа AWS"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "приватный ключ"),
    (re.compile(r"""(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*["'][^"'\s]{8,}["']"""),
     "пароль или токен в открытом виде"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}"), "bearer-токен"),
]
# Ложные срабатывания: ссылки на хранилище секретов, переменные, плейсхолдеры
SECRET_ALLOW = re.compile(
    r"(?i)(vault|secretsmanager|ssm|var\.|data\.|env\.|\$\{|<[^>]+>|example|changeme|xxx+)")

IAC_SUFFIXES = (".tf", ".tfvars", ".yaml", ".yml", ".json", ".tpl", ".sh")
SKIP_DIRS = {".terraform", "node_modules", ".git"}


def cfg() -> dict:
    try:
        import yaml
        return yaml.safe_load((ROOT / "pipeline.config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)


def tail(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text[-n:].replace("\n", " | ") if text else "(пусто)"


def scan_secrets(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in IAC_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if SECRET_ALLOW.search(line):
                continue
            for pattern, what in SECRET_PATTERNS:
                if pattern.search(line):
                    rel = path.relative_to(ROOT)
                    findings.append(f"{rel}:{line_no} — {what}")
                    break
    return findings


def main(argv: list[str]) -> int:
    conf = cfg()
    infra = conf.get("infra") or {}
    path = str(infra.get("path") or "").strip()
    if not path:
        print("G12 неприменим: infra.path не задан — инфраструктурный код "
              "конвейером не ведётся")
        return 0

    infra_root = ROOT / path
    if not infra_root.exists():
        print(f"G12 FAIL: infra.path указывает на несуществующий каталог '{path}'")
        return 1

    env = argv[argv.index("--env") + 1] if "--env" in argv else None
    allow_destroy = "--allow-destroy" in argv
    commands = infra.get("commands") or {}
    allowed = [str(e).lower() for e in (infra.get("apply_allowed") or [])]
    errors: list[str] = []
    warnings: list[str] = []

    if env and env.lower() not in allowed:
        errors.append(f"среда '{env}' не входит в apply_allowed ({', '.join(allowed) or '—'}) "
                      f"— проверку плана для неё делает человек")

    def fmt(c: str) -> str:
        return c.format(env=env) if env and "{env}" in c else c

    # 1. validate
    if commands.get("validate"):
        r = sh(fmt(commands["validate"]))
        if r.returncode != 0:
            errors.append(f"validate провален: {tail(r.stdout + r.stderr)}")
        else:
            print("validate: ok")
    else:
        warnings.append("команда validate не задана")

    # 2. plan
    if commands.get("plan"):
        r = sh(fmt(commands["plan"]))
        out = r.stdout + r.stderr
        if r.returncode not in (0, 2):   # terraform plan -detailed-exitcode: 2 = есть изменения
            errors.append(f"plan провален: {tail(out)}")
        else:
            hits = [p.pattern for p in DESTROY_MARKERS if p.search(out)]
            if hits and not allow_destroy:
                errors.append("план содержит УДАЛЕНИЕ ресурсов. Если это осознанно — "
                              "запусти с --allow-destroy и приложи обоснование человеку")
            elif hits:
                warnings.append("план содержит удаление ресурсов (разрешено флагом)")
            print(f"plan: ok{' · есть удаления' if hits else ''}")
    else:
        warnings.append("команда plan не задана — изменения применяются вслепую")

    # 3. policy
    if commands.get("policy"):
        r = sh(fmt(commands["policy"]))
        if r.returncode != 0:
            errors.append(f"политики не пройдены: {tail(r.stdout + r.stderr)}")
        else:
            print("policy: ok")
    else:
        warnings.append("политики (conftest/OPA) не настроены")

    # 4. секреты
    secrets = scan_secrets(infra_root)
    if secrets:
        errors.append(f"секреты в инфраструктурном коде: {'; '.join(secrets[:5])}"
                      + (f" и ещё {len(secrets) - 5}" if len(secrets) > 5 else ""))
    else:
        print("секретов не найдено")

    for w in warnings:
        print(f"  предупреждение: {w}")
    if errors:
        print("\nG12 FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"\nG12 OK: инфраструктура корректна"
          + (f" (среда {env})" if env else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
