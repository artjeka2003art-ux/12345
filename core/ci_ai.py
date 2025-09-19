# core/ci_ai.py
from __future__ import annotations

import yaml

import os
import re
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel

from core.yaml_edit import load_yaml_preserve, dump_yaml_preserve, preview_and_write_yaml, build_ops_from_nl
from core.workflow_edit import apply_ops
from core.ci_init import git_auto_commit


console = Console()

def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"

def _list_workflows() -> list[Path]:
    p = Path(".github/workflows")
    if not p.exists():
        return []
    return sorted([x for x in p.glob("*.yml") if x.is_file()])

def _normalize_yaml_root(data):
    # Если пришёл tuple (ok, data) — вытащим data
    if isinstance(data, tuple) and len(data) == 2:
        data = data[1]

    # Если вдруг это строка YAML — попробуем распарсить
    if isinstance(data, str):
        try:
            data = yaml.safe_load(data)
        except Exception as e:
            raise ValueError(f"Не удалось распарсить YAML-строку: {e}")

    # Если multi-doc: берём первый словарь
    if isinstance(data, list):
        for doc in data:
            if isinstance(doc, dict):
                return doc
        raise ValueError("YAML содержит несколько документов, но ни один не является объектом (dict).")

    if not isinstance(data, dict):
        raise ValueError(f"YAML root должен быть объектом (dict), а не {type(data).__name__}")

    return data



def _read_workflow_name(path: Path) -> str:
    ok, data = load_yaml_preserve(str(path))
    if not ok:
        return ""
    try:
        data = _normalize_yaml_root(data)
    except Exception:
        return ""
    try:
        name = data.get("name")
        return str(name) if name else ""
    except Exception:
        return ""

def _present_workflow_menu(items: list[Path]) -> Optional[Path]:
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    # Покажем нумерованный список с name: из YAML
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

    # Можно ввести номер
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(items):
            return items[idx-1]

    # Или полное/частичное имя файла
    for p in items:
        if p.name == choice or p.name.startswith(choice):
            return p

    console.print(Panel.fit("Не понял выбор — беру первый.", border_style="yellow"))
    return items[0]


def _select_workflow(filename: Optional[str]) -> Optional[Path]:
    if filename:
        path = Path(".github/workflows") / filename
        if path.exists():
            return path
        # Допускаем, что пользователь ввёл без каталога и расширения
        p = Path(".github/workflows") / f"{filename}.yml"
        if p.exists():
            return p

    items = _list_workflows()
    return _present_workflow_menu(items)


def _detect_kind(yaml_data: Any) -> str:
    text = dump_yaml_preserve(yaml_data).lower()
    if "actions/setup-python" in text or "pip install" in text or "pytest" in text:
        return "python"
    if "actions/setup-node" in text or "npm " in text or "yarn " in text or "pnpm " in text:
        return "node"
    if "actions/setup-go" in text or "go build" in text:
        return "go"
    if "docker/login-action" in text or "build-push-action" in text or "uses: docker/" in text:
        return "docker"
    if "actions/setup-java" in text or "gradle " in text or "mvn " in text:
        return "java"
    if "actions/setup-dotnet" in text:
        return "dotnet"
    if "dtolnay/rust-toolchain" in text or "actions-rs" in text or "cargo " in text:
        return "rust"
    if "setup-php" in text:
        return "php"
    if "setup-ruby" in text or "bundle " in text:
        return "ruby"
    if "android-actions" in text or "gradlew assemble" in text:
        return "android"
    return "python"

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

    notes = apply_ops(data, ops)
    data = _normalize_workflow_yaml(data)
    saved, backup = preview_and_write_yaml(str(wf_path), data, auto_yes=auto_yes)
    if not saved:
        return

    if autopush:
        url = git_auto_commit(str(wf_path), f"ci: edit via GhostCMD — {features_text[:60]}")
        if url:
            console.print(Panel.fit(f"🔗 GitHub Actions: {url}", border_style="green"))

_ERR_PATTERNS = [
    (r"pytest(\s*:|:)? not found|ModuleNotFoundError: .*pytest", 
        lambda kind: [{"op": "set_run", "step": {"name": "test"}, "value": "pip install -r requirements.txt && pytest -q"}] if kind=="python" else []),
    (r"black(\s*:|:)? not found", 
        lambda kind: [{"op": "set_run", "step": {"name": "lint"}, "value": "pip install black && black --check ."}] if kind=="python" else []),
    (r"eslint(\s*:|:)? not found", 
        lambda kind: [{"op": "set_run", "step": {"name": "lint"}, "value": "npm install --save-dev eslint && npx eslint ."}] if kind=="node" else []),
    (r"Process completed with exit code 137|signal: killed|OutOfMemory", 
        lambda kind: [{"op": "set_timeout", "value": "20m"}]),
    (r"Command\s+'?npm'? not found", 
        lambda kind: [{"op": "set_run", "step": {"name": "test"}, "value": "npm ci && npm test --silent"}] if kind=="node" else []),
]

def _normalize_workflow_yaml(data: dict) -> dict:
    # Убираем случайный ключ 'true' на верхнем уровне
    if "true" in data and isinstance(data["true"], dict):
        if "on" not in data:
            data["on"] = data["true"]
        data.pop("true", None)

    # Чиним шаги
    for job in data.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        fixed_steps = []
        for step in steps:
            if not isinstance(step, dict):
                continue

            new_step = {}
            # name
            if "name" in step:
                new_step["name"] = step["name"]

            # uses
            if "uses" in step:
                new_step["uses"] = step["uses"]

            # with
            if "with" in step:
                new_step["with"] = step["with"]

            # run
            if "run" in step:
                run_val = step["run"]
                if isinstance(run_val, str) and "\n" in run_val:
                    # многострочный блок
                    new_step["run"] = run_val.strip("\n")
                else:
                    new_step["run"] = run_val

            # target / needs / env и т.д.
            for k, v in step.items():
                if k in ("name", "uses", "with", "run"):
                    continue
                new_step[k] = v

            fixed_steps.append(new_step)

        job["steps"] = fixed_steps

    return data




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

    data = _normalize_workflow_yaml(data)
    saved, backup = preview_and_write_yaml(str(wf_path), data, auto_yes=auto_yes)
    if not saved:
        return

    if autopush:
        url = git_auto_commit(str(wf_path), "ci: fix last failure via GhostCMD")
        if url:
            console.print(Panel.fit(f"🔗 GitHub Actions: {url}", border_style="green"))
