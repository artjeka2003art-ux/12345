from __future__ import annotations
import subprocess
from rich.panel import Panel
from rich.console import Console
import os
from pathlib import Path
from rich.panel import Panel
console = Console()

def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"

def ci_list() -> None:
    code, out, err = _run(["gh", "workflow", "list"])
    if code == 0:
        console.print(Panel(out, title="GitHub Actions — workflows", border_style="cyan"))
    else:
        console.print(Panel.fit(f"[red]Ошибка:[/red]\n{err}", border_style="red"))

def ci_run(workflow: str | None = None) -> None:
    cmd = ["gh", "workflow", "run"]
    if workflow:
        cmd.append(workflow)
    code, out, err = _run(cmd)
    if code == 0:
        console.print(Panel(out or "Workflow запущен.", border_style="green"))
    else:
        console.print(Panel.fit(f"[red]Ошибка запуска:[/red]\n{err}", border_style="red"))

def ci_logs_last() -> None:
    code, out, err = _run(["gh", "run", "view", "--log"])
    if code == 0:
        console.print(Panel(out, title="Логи последнего запуска", border_style="blue"))
    else:
        console.print(Panel.fit(f"[red]Ошибка:[/red]\n{err}", border_style="red"))
        
def handle_ci_run(parts: list[str]):
    """
    ci run [filename]
    Запускает GitHub Actions workflow.
    """
    if len(parts) < 2:
        # список доступных workflow
        workflows = list(Path(".github/workflows").glob("*.yml"))
        if not workflows:
            print(Panel("❌ Нет доступных workflow в .github/workflows/", border_style="red"))
            return
        items = "\n".join(f"- {wf.name}" for wf in workflows)
        print(Panel(f"Укажи имя workflow.\nДоступные:\n{items}", border_style="yellow"))
        return

    filename = parts[1]
    workflow_path = Path(".github/workflows") / filename
    if not workflow_path.exists():
        workflows = list(Path(".github/workflows").glob("*.yml"))
        items = "\n".join(f"- {wf.name}" for wf in workflows) or "(пусто)"
        print(Panel(f"❌ Файл {filename} не найден.\nДоступные:\n{items}", border_style="red"))
        return

    # если файл найден → запускаем через gh
    os.system(f"gh workflow run {filename}")