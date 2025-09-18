# core/workflow_lint.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Set, Tuple
import re

try:
    from rich.table import Table
    from rich import print as rprint
    RICH = True
except Exception:
    RICH = False


@dataclass
class LintIssue:
    step: str | None     # None = проблема на уровне workflow
    problem: str         # кратко, что не так
    recommendation: str  # что сделать
    severity: str        # "ERROR" | "WARN"


def _collect_output_refs(wf) -> Set[Tuple[str, str]]:
    """
    Ищем использования ${{ steps.NAME.outputs.KEY }} во всех местах, где есть строки:
      - run
      - env значения
      - if_expr
      - cwd
    Возвращаем множество пар (NAME, KEY).
    """
    used: Set[Tuple[str, str]] = set()
    rx = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_\-]+)\.outputs\.([A-Za-z0-9_\-]+)\s*\}\}")
    for s in wf.steps:
        buckets: List[str] = []
        if getattr(s, "run", None):
            buckets.append(s.run)
        if getattr(s, "if_expr", None):
            buckets.append(s.if_expr)
        if getattr(s, "cwd", None):
            buckets.append(s.cwd)
        # env значения
        for v in (getattr(s, "env", {}) or {}).values():
            if isinstance(v, str):
                buckets.append(v)
        # скан
        for text in buckets:
            for m in rx.finditer(text):
                used.add((m.group(1), m.group(2)))
    return used


def _detect_cycles(wf) -> List[List[str]]:
    """
    По needs строим граф и ищем циклы (DFS).
    Возвращаем список циклов (каждый как список имён шагов).
    """
    name_to_idx = {s.name: i for i, s in enumerate(wf.steps)}
    graph: Dict[str, List[str]] = {s.name: list(s.needs or []) for s in wf.steps}

    visited: Dict[str, int] = {}  # 0=white,1=gray,2=black
    stack: List[str] = []
    cycles: List[List[str]] = []

    def dfs(u: str):
        visited[u] = 1
        stack.append(u)
        for v in graph.get(u, []):
            if v not in name_to_idx:
                # неизвестные зависимости уже ловятся в load_workflow, игнор
                continue
            color = visited.get(v, 0)
            if color == 0:
                dfs(v)
            elif color == 1:
                # нашли цикл: срез стека от v до u
                try:
                    k = stack.index(v)
                    cycles.append(stack[k:].copy())
                except ValueError:
                    pass
        stack.pop()
        visited[u] = 2

    for s in wf.steps:
        if visited.get(s.name, 0) == 0:
            dfs(s.name)
    return cycles


def lint_workflow(wf) -> List[LintIssue]:
    issues: List[LintIssue] = []

    # 1) Пустые run
    for s in wf.steps:
        run = (s.run or "").strip() if getattr(s, "run", None) is not None else ""
        if run == "":
            issues.append(LintIssue(
                step=s.name,
                problem="Пустое поле run",
                recommendation="Заполни run или удали шаг.",
                severity="ERROR",
            ))

    # 2) Проверка capture.*.regex (валидность регулярки)
    for s in wf.steps:
        cap = getattr(s, "capture", {}) or {}
        if not isinstance(cap, dict):
            continue
        for key, rule in cap.items():
            if not isinstance(rule, dict):
                continue
            rx = rule.get("regex")
            if not rx:
                continue
            try:
                re.compile(rx, flags=re.MULTILINE)
            except re.error as e:
                issues.append(LintIssue(
                    step=s.name,
                    problem=f"capture[{key}].regex: невалидная регулярка ({e})",
                    recommendation="Исправь синтаксис регулярного выражения.",
                    severity="ERROR",
                ))

    # 3) Невостребованные outputs (capture -> нигде не используются)
    used = _collect_output_refs(wf)   # пары (step, key)
    for s in wf.steps:
        cap = getattr(s, "capture", {}) or {}
        if not isinstance(cap, dict):
            continue
        for key in cap.keys():
            if (s.name, key) not in used:
                issues.append(LintIssue(
                    step=s.name,
                    problem=f"Неиспользуемый output '{key}'",
                    recommendation="Удали capture или начни использовать ${{ steps.%s.outputs.%s }}." % (s.name, key),
                    severity="WARN",
                ))

    # 4) Нереференсные (unreferenced) шаги — на них нет ссылок в needs
    # это не «фатальная» ошибка, но предупредим
    referenced = set()
    for s in wf.steps:
        for d in (s.needs or []):
            referenced.add(d)
    all_names = [s.name for s in wf.steps]
    for s in wf.steps:
        if s.name not in referenced:
            # если на шаг никто не ссылается — он «унреференсный»
            # (в нашем раннере он всё равно выполнится, поэтому это WARN)
            issues.append(LintIssue(
                step=s.name,
                problem="Шаг не используется другими (unreferenced)",
                recommendation="Проверь смысл шага; если должен быть связан, добавь needs у зависящих шагов.",
                severity="WARN",
            ))

    # 5) Циклы в зависимостях
    cycles = _detect_cycles(wf)
    for cyc in cycles:
        issues.append(LintIssue(
            step=None,
            problem=f"Цикл зависимостей: {' -> '.join(cyc)}",
            recommendation="Убери цикл в needs: граф должен быть ацикличным.",
            severity="ERROR",
        ))

    return issues


def print_lint_report(issues: List[LintIssue]) -> None:
    if not issues:
        print("[lint] Нет замечаний.") if not RICH else rprint("[bold green]✓ lint: замечаний нет[/bold green]")
        return
    if RICH:
        t = Table(title="Отчёт валидации workflow", show_lines=False)
        t.add_column("step")
        t.add_column("problem")
        t.add_column("recommendation")
        t.add_column("severity")
        for i in issues:
            t.add_row(i.step or "-", i.problem, i.recommendation, i.severity)
        rprint(t)
    else:
        print("Отчёт валидации:")
        for i in issues:
            print(f"- [{i.severity}] step={i.step or '-'} :: {i.problem} :: {i.recommendation}")


def has_errors(issues: List[LintIssue]) -> bool:
    return any(i.severity == "ERROR" for i in issues)
