from __future__ import annotations
import subprocess
from rich.panel import Panel
from rich.console import Console

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
