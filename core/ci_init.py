# core/ci_init.py
from __future__ import annotations

import os
import shutil
from pathlib import Path
from rich.panel import Panel
from rich.console import Console
import difflib

console = Console()

TEMPLATES = {
    "python": "core/templates/gha_python.yml",
    "node": "core/templates/gha_node.yml",
    "go": "core/templates/gha_go.yml",
    "docker": "core/templates/gha_docker.yml",
    "java": "core/templates/gha_java.yml",
    "dotnet": "core/templates/gha_dotnet.yml",
    "rust": "core/templates/gha_rust.yml",
    "php": "core/templates/gha_php.yml",
    "ruby": "core/templates/gha_ruby.yml",
    "android": "core/templates/gha_android.yml",
    "multi": "core/templates/gha_multi.yml",
}


def init_ci(target: str = "python", force: bool = False, outfile: str | None = None) -> str:
    """
    Создаёт .github/workflows/ci.yml из шаблона.
    target: python|node|go|docker
    force: если True — перезапишет существующий файл.
    Возвращает путь к созданному файлу.
    """
    if target not in TEMPLATES:
        raise ValueError(f"Неизвестный тип CI: {target}")

    src = Path(TEMPLATES[target])
    if not src.exists():
        raise FileNotFoundError(f"Шаблон не найден: {src}")

    workflows_dir = Path(".github/workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)
    # если передали явное имя файла
    if outfile:
        # допускаем как "ci_rust.yml", так и "ci_rust.yaml"
        dst = workflows_dir / outfile
    else:
        # по умолчанию старое поведение — один файл ci.yml
        dst = workflows_dir / "ci.yml"


    # читаем старый файл если есть
    old_text = None
    if dst.exists():
        old_text = dst.read_text()

        if not force:
            console.print(Panel.fit(
                f"[yellow]Файл {dst} уже существует.[/yellow]\n"
                f"Запусти с [bold]--force[/bold], если хочешь перезаписать.\n"
                f"Совет: сначала посмотри diff командой [bold]ci init {target} --force[/bold].",
                border_style="yellow"
            ))
            return str(dst)

    # читаем новый шаблон
    new_text = src.read_text()

# Автоматически гарантируем наличие workflow_dispatch
    if "workflow_dispatch" not in new_text:
        lines = []
        inserted = False
        for line in new_text.splitlines():
            lines.append(line)
            if line.strip().startswith("pull_request:") and not inserted:
                lines.append("  workflow_dispatch:")
                inserted = True
        if not inserted:
            # если почему-то нет блока pull_request — просто добавим в конец on:
            patched = []
            for line in lines:
                patched.append(line)
                if line.strip().startswith("on:") and not inserted:
                    patched.append("  workflow_dispatch:")
                    inserted = True
            lines = patched
        new_text = "\n".join(lines)

    # если был старый текст и включён force — показываем diff
    if old_text is not None and force:
        diff = difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=str(dst),
            tofile=str(src),
            lineterm=""
        )
        diff_text = "\n".join(diff) or "Нет различий."
        console.print(Panel.fit(
            f"[cyan]Diff изменений ({target}):[/cyan]\n\n{diff_text}",
            border_style="cyan", padding=(1,2)
        ))

    # записываем новый файл
    dst.write_text(new_text)
    console.print(Panel.fit(
        f"[green]Создан workflow для GitHub Actions ({target})[/green]\n{dst}",
        border_style="green"
    ))
    return str(dst)

    

