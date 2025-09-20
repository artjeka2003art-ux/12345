# core/ci_ai.py
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple, List

import yaml
from rich.console import Console
from rich.panel import Panel

from core.yaml_edit import (
    load_yaml_preserve,
    dump_yaml_preserve,
    build_ops_from_nl,
)
from core.workflow_edit import apply_ops
from core.ci_init import git_auto_commit

console = Console()


# ------------------------------ shell utils ------------------------------

def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


# ------------------------------ workflows fs ------------------------------

def _list_workflows() -> list[Path]:
    p = Path(".github/workflows")
    if not p.exists():
        return []
    return sorted([x for x in p.glob("*.yml") if x.is_file()] + [x for x in p.glob("*.yaml") if x.is_file()])


def _normalize_yaml_root(data: Any) -> dict:
    """
    Приводим произвольный результат загрузки (tuple / str / list) к dict (корню workflow).
    """
    # load_yaml_preserve может вернуть (ok, data) — но сюда уже приходит только data
    # 1) если это сырой YAML-текст → распарсим
    if isinstance(data, str):
        try:
            data = yaml.safe_load(data)
        except Exception as e:
            raise ValueError(f"Не удалось распарсить YAML-строку: {e}")

    # 2) multi-doc YAML: берём первый dict-документ
    if isinstance(data, list):
        for doc in data:
            if isinstance(doc, dict):
                data = doc
                break

    if not isinstance(data, dict):
        raise ValueError(f"YAML root должен быть объектом (dict), а не {type(data).__name__}")

    return data


def _read_workflow_name(path: Path) -> str:
    ok, data = load_yaml_preserve(str(path))
    if not ok:
        return ""
    try:
        data = _normalize_yaml_root(data)
        name = data.get("name")
        return str(name) if name else ""
    except Exception:
        return ""


def _present_workflow_menu(items: list[Path]) -> Optional[Path]:
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    lines = []
    for i, p in enumerate(items, 1):
        human = _read_workflow_name(p)
        tail = f" — {human}" if human else ""
        lines.append(f"[{i}] {p.name}{tail}")
    console.print(Panel("\n".join(lines), title="Найдено несколько workflow", border_style="magenta"))

    try:
        choice = input("Выбери номер файла (Enter = 1, можно ввести имя файла): ").strip()
    except EOFError:
        choice = ""

    if choice == "":
        return items[0]

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(items):
            return items[idx - 1]

    for p in items:
        if p.name == choice or p.name.startswith(choice):
            return p

    console.print(Panel.fit("Не понял выбор — беру первый.", border_style="yellow"))
    return items[0]


def _select_workflow(filename: Optional[str]) -> Optional[Path]:
    if filename:
        base = Path(".github/workflows")
        cand = [base / filename, base / f"{filename}.yml", base / f"{filename}.yaml"]
        for p in cand:
            if p.exists():
                return p
    return _present_workflow_menu(_list_workflows())


# ------------------------------ YAML helpers ------------------------------
def _detect_kind(yaml_data: Any) -> str:
    """
    Определяет стек по содержимому workflow.
    ВАЖНО: Rust проверяется ПЕРВЫМ, чтобы не перепутать с Go.
    """
    try:
        text = dump_yaml_preserve(yaml_data).lower()
    except Exception:
        text = yaml.safe_dump(yaml_data, sort_keys=False).lower()

    # Rust — проверяем первым
    if "dtolnay/rust-toolchain" in text or "actions-rs" in text or "cargo " in text:
        return "rust"

    # Go
    if "actions/setup-go" in text or "\ngo " in text or "go build" in text:
        return "go"

    # Python
    if "actions/setup-python" in text or "pip install" in text or "pytest" in text:
        return "python"

    # Node.js
    if "actions/setup-node" in text or "npm " in text or "yarn " in text or "pnpm " in text:
        return "node"

    # Docker (ловим всё подряд: login, build, push)
    if (
        "docker/login-action" in text
        or "build-push-action" in text
        or "uses: docker/" in text
        or "docker build" in text
        or "docker push" in text
        or "docker run" in text
    ):
        return "docker"

    # Java
    if "actions/setup-java" in text or "gradle " in text or "mvn " in text:
        return "java"

    # .NET
    if "actions/setup-dotnet" in text:
        return "dotnet"

    # PHP
    if "setup-php" in text:
        return "php"

    # Ruby
    if "setup-ruby" in text or "bundle " in text:
        return "ruby"

    # Android
    if "android-actions" in text or "gradlew assemble" in text:
        return "android"

    # По умолчанию Python
    return "python"




def _normalize_workflow_yaml(data: dict) -> dict:
    """
    Приводим структуру к валидной для GitHub Actions:
      - корневой ключ событий: on
      - удаляем служебные поля из шагов
      - чиним run
      - убираем дубли шагов по имени
      - гарантируем workflow_dispatch: {}
    """
    # ---- 1) Корень: on
    if "true" in data:
        if "on" not in data:
            data["on"] = data["true"]
        data.pop("true", None)

    if True in data:  # YAML 1.1 иногда превращает on: в True
        if "on" not in data:
            data["on"] = data[True]  # type: ignore[index]
        data.pop(True, None)  # type: ignore[arg-type]

    if "on" not in data:
        data["on"] = {}

    # --- Гарантия: workflow_dispatch всегда есть и всегда dict
    if not isinstance(data["on"], dict):
        data["on"] = {}
    if not isinstance(data["on"].get("workflow_dispatch"), dict):
        data["on"]["workflow_dispatch"] = {}

    # Нормализуем branches
    for sect in ("push", "pull_request"):
        if isinstance(data["on"].get(sect), dict):
            br = data["on"][sect].get("branches")
            if isinstance(br, str):
                data["on"][sect]["branches"] = [br]

    # ---- 2) Шаги: чистим служебные поля, run и дубликаты
    jobs = data.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue

            fixed: list[dict] = []
            for step in steps:
                if not isinstance(step, dict):
                    continue

                new_step: dict = {}
                if "name" in step:
                    new_step["name"] = step["name"]
                if "uses" in step:
                    new_step["uses"] = step["uses"]
                if "with" in step and isinstance(step["with"], dict):
                    new_step["with"] = step["with"]
                if "run" in step:
                    run_val = step["run"]
                    if isinstance(run_val, str):
                        new_step["run"] = run_val.rstrip("\n")
                    else:
                        new_step["run"] = run_val

                for k, v in step.items():
                    if k in ("name", "uses", "with", "run", "target", "needs"):
                        continue
                    new_step[k] = v

                fixed.append(new_step)

            # Убираем дубли шагов
            seen = set()
            unique = []
            for st in fixed:
                nm = st.get("name")
                if nm and nm in seen:
                    continue
                if nm:
                    seen.add(nm)
                unique.append(st)
            job["steps"] = unique

    return data


def _force_dump_yaml(path: str, data: dict) -> tuple[bool, str]:
    """Жёсткий дамп YAML с фиксом workflow_dispatch."""
    try:
        text = yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
            indent=2
        )

        # FIX: нормализуем workflow_dispatch
        text = text.replace("workflow_dispatch:\n", "workflow_dispatch: {}\n")
        text = text.replace("workflow_dispatch: null", "workflow_dispatch: {}")

        backup = f"{path}.bak"
        with open(backup, "w") as f:
            f.write(text)
        with open(path, "w") as f:
            f.write(text)
        return True, backup
    except Exception as e:
        console.print(Panel.fit(f"❌ Ошибка дампа YAML: {e}", border_style="red"))
        return False, ""





# ------------------------------ CI features ------------------------------

_ERR_PATTERNS = [
    (r"pytest(\s*:|:)? not found|ModuleNotFoundError: .*pytest",
     lambda kind: [{"op": "set_run", "step": {"name": "test"},
                    "value": "pip install -r requirements.txt && pytest -q"}] if kind == "python" else []),
    (r"black(\s*:|:)? not found",
     lambda kind: [{"op": "set_run", "step": {"name": "lint"},
                    "value": "pip install black && black --check ."}] if kind == "python" else []),
    (r"eslint(\s*:|:)? not found",
     lambda kind: [{"op": "set_run", "step": {"name": "lint"},
                    "value": "npm install --save-dev eslint && npx eslint ."}] if kind == "node" else []),
    (r"Process completed with exit code 137|signal: killed|OutOfMemory",
     lambda kind: [{"op": "set_timeout", "value": "20m"}]),
    (r"Command\s+'?npm'? not found",
     lambda kind: [{"op": "set_run", "step": {"name": "test"},
                    "value": "npm ci && npm test --silent"}] if kind == "node" else []),
]


def ci_edit(features_text: str, filename: Optional[str] = None, *, auto_yes: bool = False, autopush: bool = True) -> None:
    wf_path = _select_workflow(filename)
    if not wf_path:
        console.print(Panel.fit("❌ .github/workflows/*.yml не найдено.", border_style="red"))
        return

    ok, raw = load_yaml_preserve(str(wf_path))
    if not ok:
        console.print(Panel.fit(f"❌ Не удалось прочитать YAML: {wf_path}", border_style="red"))
        return

    try:
        data = _normalize_yaml_root(raw)
    except Exception as e:
        console.print(Panel.fit(f"❌ {e}", border_style="red"))
        return

    kind = _detect_kind(data)
    ops = build_ops_from_nl(kind, features_text)
    if not ops:
        console.print(Panel.fit("ℹ️ Не нашёл, что править по описанию. Попробуй уточнить задачу.", border_style="yellow"))
        return

    # Применяем ровно один раз — исключаем дубли
    notes = apply_ops(data, ops)
    if notes:
        console.print(Panel("\n".join(notes), title="Изменения", border_style="cyan"))

    # Жёсткая нормализация перед записью
    data = _normalize_workflow_yaml(data)

    saved, backup = _force_dump_yaml(str(wf_path), data)
    if not saved:
        return

    if autopush:
        url = git_auto_commit(str(wf_path), f"ci: edit via GhostCMD — {features_text[:60]}")
        if url:
            console.print(Panel.fit(f"🔗 GitHub Actions: {url}", border_style="green"))


def _extract_error_tail(logs: str) -> str:
    lines = [l for l in logs.splitlines() if l.strip()]
    return "\n".join(lines[-60:])


def _build_ops_from_logs(kind: str, logs: str) -> list[dict]:
    ops: list[dict] = []
    for pat, builder in _ERR_PATTERNS:
        if re.search(pat, logs, flags=re.I):
            try:
                ops.extend(builder(kind))
            except Exception:
                pass
    return ops


def ci_fix_last(filename: Optional[str] = None, *, auto_yes: bool = False, autopush: bool = True) -> None:
    code, out, err = _run(["gh", "run", "view", "--log"])
    if code != 0:
        console.print(Panel.fit(f"❌ Не удалось получить логи: {err or out}", border_style="red"))
        return

    logs = out
    wf_path = _select_workflow(filename)
    if not wf_path:
        console.print(Panel.fit("❌ Не найден workflow для правки (.github/workflows/*.yml).", border_style="red"))
        return

    ok, raw = load_yaml_preserve(str(wf_path))
    if not ok:
        console.print(Panel.fit(f"❌ Не удалось прочитать YAML: {wf_path}", border_style="red"))
        return

    try:
        data = _normalize_yaml_root(raw)
    except Exception as e:
        console.print(Panel.fit(f"❌ {e}", border_style="red"))
        return

    kind = _detect_kind(data)
    ops = _build_ops_from_logs(kind, logs)

    try:
        from ghost_brain import analyze_error
        tail = _extract_error_tail(logs)
        m = re.search(r"Run (.+)", logs)
        last_cmd = m.group(1).strip() if m else "<workflow step>"
        tip = analyze_error(last_cmd, 1, tail, cwd=os.getcwd())
        title = tip.get("title", "Совет")
        explain = tip.get("explain", "")
        console.print(Panel.fit(f"🧠 {title}\n[dim]{explain}[/dim]", border_style="blue"))
    except Exception:
        pass

    if not ops:
        console.print(Panel.fit("ℹ️ Патч по логам не найден. Попробуй 'ci edit \"...\"'.", border_style="yellow"))
        return

    notes = apply_ops(data, ops)
    if notes:
        console.print(Panel("\n".join(notes), title="Изменения", border_style="cyan"))

    # Нормализация перед сохранением
    data = _normalize_workflow_yaml(data)

    saved, backup = _force_dump_yaml(str(wf_path), data)
    if not saved:
        return

    if autopush:
        url = git_auto_commit(str(wf_path), "ci: fix last failure via GhostCMD")
        if url:
            console.print(Panel.fit(f"🔗 GitHub Actions: {url}", border_style="green"))
