# core/ci_github.py
from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Tuple

def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"

def gh_exists() -> bool:
    return shutil.which("gh") is not None

def gh_version() -> str | None:
    if not gh_exists():
        return None
    code, out, err = _run(["gh", "--version"])
    if code == 0 and out:
        # обычно первая строка вида: "gh version 2.58.0 (2025-08-01)"
        return out.splitlines()[0]
    return None

def gh_is_authenticated() -> Tuple[bool, str]:
    """
    Возвращает (ok, detail). ok=True если gh авторизован.
    detail — человекочитаемый вывод gh auth status (stdout/stderr).
    """
    if not gh_exists():
        return False, "GitHub CLI (gh) не установлен."
    code, out, err = _run(["gh", "auth", "status"])
    detail = (out or err).strip()
    return (code == 0), (detail or "Нет деталей от gh auth status.")

def install_hint() -> str:
    osname = platform.system()
    if osname == "Darwin":
        return "Установить: brew install gh"
    if osname == "Linux":
        return "Установить: см. https://github.com/cli/cli#linux (apt/yum/pacman и т.д.)"
    if osname == "Windows":
        return "Установить: winget install --id GitHub.cli"
    return "Смотри инструкции: https://github.com/cli/cli"
