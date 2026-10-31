"""Гейт вёрстки: детектор impeccable не должен находить ничего в шаблонах.

Детерминированные правила против халтурной ИИ-вёрстки — контраст ниже WCAG AA,
текст мельче порога, узнаваемые генеративные шаблоны (плитка-иконка над
заголовком, eyebrow-чип над h1, цветная планка сбоку карточки).

Осознанные исключения и незакрытый долг — в `.impeccable/config.json`,
там же на каждое исключение написано, почему оно там.

Тул ставится локально: `npm install` в корне репозитория. `npx impeccable`
на части машин виснет наглухо, поэтому дёргаем бинарник напрямую.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["app/templates", "static"]


def _binary() -> Path | None:
    base = ROOT / "node_modules" / ".bin"
    for name in ("impeccable.cmd", "impeccable"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def test_templates_pass_impeccable() -> None:
    binary = _binary()
    if binary is None:
        pytest.skip(
            "детектор вёрстки не установлен — выполните `npm install` в корне "
            "репозитория, иначе гейт вёрстки перед деплоем не отработает"
        )

    proc = subprocess.run(
        [str(binary), "detect", *TARGETS, "--no-advisory", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # 0 — чисто, 2 — есть находки, остальное означает, что упал сам детектор.
    if proc.returncode not in (0, 2):
        pytest.fail(
            f"детектор завершился с кодом {proc.returncode}:\n"
            f"{proc.stdout}\n{proc.stderr}"
        )

    if proc.returncode == 0:
        return

    # Разбираем JSON ради внятного сообщения; если формат вдруг сменился —
    # показываем сырой вывод, но тест всё равно валим.
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"детектор нашёл проблемы вёрстки:\n{proc.stdout}")

    lines = []
    for finding in _iter_findings(report):
        where = finding.get("file") or finding.get("path") or "?"
        try:
            where = str(Path(where).relative_to(ROOT))
        except ValueError:
            pass
        line = finding.get("line")
        rule = finding.get("antipattern") or finding.get("id") or finding.get("rule") or "?"
        snippet = finding.get("snippet") or finding.get("message") or ""
        at = f"{where}:{line}" if line else where
        lines.append(f"  [{rule}] {at} — {snippet}")

    pytest.fail(
        "детектор вёрстки нашёл {n} шт.:\n{body}\n\n"
        "Либо чинить, либо — если это осознанное решение — вносить в "
        "`.impeccable/config.json` вместе с объяснением, почему.".format(
            n=len(lines), body="\n".join(lines) or proc.stdout
        )
    )


def _iter_findings(report: object):
    """JSON-схема детектора между версиями может ехать — берём и плоский
    список находок, и раскладку по файлам."""
    if isinstance(report, dict):
        if "antipattern" in report or "id" in report or "rule" in report:
            yield report
            return
        for key in ("findings", "results", "antipatterns"):
            value = report.get(key)
            if isinstance(value, list):
                for item in value:
                    yield from _iter_findings(item)
                return
        for value in report.values():
            if isinstance(value, (list, dict)):
                yield from _iter_findings(value)
    elif isinstance(report, list):
        for item in report:
            yield from _iter_findings(item)


if __name__ == "__main__":  # ручной прогон: python tests/test_ui_impeccable.py
    sys.exit(pytest.main([__file__, "-q"]))
